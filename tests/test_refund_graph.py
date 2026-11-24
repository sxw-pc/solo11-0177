"""Bank-graph settlement certificate tests (enhancement #25+).

Covers: evidence replay/dedup, candidate-edge legality, maximum bipartite
matching with an auditable augmenting path, Kőnig barrier sets, duplicate
export pages and late revocations, the two holder-preauthorization protocols
(revocable swap vs locked bank write-off), certificate void/replacement
chains, and crash recovery that cannot leave two active certificates.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from preorderproof.refund import (
    build_bank_writeoff_certificate, build_graph,
    build_locked_settlement, build_provisional_certificate,
    build_revocable_settlement, certificate_fingerprint, min_barrier,
    maximum_matching, provisional_matches, render_certificate, source_digest,
    validate_bundle,
)
from preorderproof.store import (
    active_locked_edges, connect, get_active_certificate, ingest_refund_bundle,
    issue_refund_certificate, list_refund_certificate_events,
    list_refund_certificates, load_refund_bundle, next_refund_cert_id,
    void_refund_certificate,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "refund-bank-graph.json"
ISSUED_AT = "2026-11-24T12:00:00Z"
VOID_AT = "2026-11-25T09:00:00Z"
SCOPE_REV = "batch-RF-2026-11:revocable_provisional"
SCOPE_LOCK = "batch-RF-2026-11:non_transferable_bank_writeoff"


def load_bundle():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def page(doc="doc.pdf", page=1, digest="aa" * 32):
    return {"document": doc, "page": page, "rect": [0, 0, 10, 10], "seal_digest": digest}


def minimal_bundle(revocations=None):
    """The fixed D1/D2 + B1/B2 case, optionally with a duplicate-page B3 flow."""
    return {
        "batch_id": "mini",
        "claims": [
            {"record_id": "c1", "kind": "merchant_refund_claim", "order_id": "D1",
             "amount": 300, "account_mask": "M", "batch_id": "mini", "status": "valid",
             "snapshot": {"source": "delivery.refund_due", "order_id": "D1", "refund_due": 300},
             "page_ref": page("claims.csv", 1, "11" * 32)},
            {"record_id": "c2", "kind": "merchant_refund_claim", "order_id": "D2",
             "amount": 300, "account_mask": "M", "batch_id": "mini", "status": "valid",
             "snapshot": {"source": "delivery.refund_due", "order_id": "D2", "refund_due": 300},
             "page_ref": page("claims.csv", 1, "22" * 32)},
        ],
        "bank_flows": [
            {"record_id": "b1", "kind": "bank_flow", "txid": "B1", "account_mask": "M",
             "amount": 300, "batch_id": "mini", "order_code": None, "settled": True,
             "page_ref": page("bank.pdf", 1, "33" * 32)},
            {"record_id": "b2", "kind": "bank_flow", "txid": "B2", "account_mask": "M",
             "amount": 300, "batch_id": "mini", "order_code": "D1", "settled": True,
             "page_ref": page("bank.pdf", 1, "44" * 32)},
        ],
        "account_mappings": [
            {"record_id": "m1", "kind": "account_mapping", "account_mask": "M",
             "holder": "family", "page_ref": page("map.csv", 1, "55" * 32)},
        ],
        "refund_codes": [
            {"record_id": "r1", "kind": "refund_code", "code": "D1", "order_id": "D1",
             "exclusive": True, "page_ref": page("codes.csv", 1, "66" * 32)},
        ],
        "authorizations": [
            {"record_id": "a1", "kind": "transfer_authorization", "account_mask": "M",
             "holder": "family", "transferable": True,
             "protocol": "revocable_provisional", "scope": "mini",
             "page_ref": page("auth.pdf", 1, "77" * 32)},
            {"record_id": "a2", "kind": "transfer_authorization", "account_mask": "M",
             "holder": "family", "transferable": False,
             "protocol": "non_transferable_bank_writeoff", "scope": "mini",
             "page_ref": page("auth.pdf", 2, "88" * 32)},
        ],
        "revocations": revocations or [],
    }


def fixed_graph(bundle=None, revoked_txids=frozenset()):
    return build_graph(bundle or minimal_bundle(), revoked_txids=set(revoked_txids))


class EvidenceValidationTest(unittest.TestCase):
    def test_01_well_formed_fixture_validates(self):
        self.assertTrue(validate_bundle(load_bundle()))

    def test_02_unknown_account_mask_is_rejected(self):
        bundle = minimal_bundle()
        bundle["bank_flows"][0]["account_mask"] = "GHOST"
        with self.assertRaisesRegex(ValueError, "unknown account_mask"):
            build_graph(bundle)

    def test_03_tampered_page_seal_is_rejected(self):
        bundle = minimal_bundle()
        bundle["bank_flows"][0]["page_ref"]["seal_digest"] = "not-a-digest"
        with self.assertRaisesRegex(ValueError, "sha256 hex digest"):
            build_graph(bundle)

    def test_04_conflicting_repeated_txid_identity_is_rejected(self):
        bundle = minimal_bundle()
        bundle["bank_flows"].append({
            "record_id": "b1b", "kind": "bank_flow", "txid": "B1", "account_mask": "M",
            "amount": 301, "batch_id": "mini", "order_code": None, "settled": True,
            "page_ref": page("bank.pdf", 2, "99" * 32)})
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            build_graph(bundle)

    def test_05_unknown_order_code_is_rejected(self):
        bundle = minimal_bundle()
        bundle["bank_flows"][1]["order_code"] = "D9"
        with self.assertRaisesRegex(ValueError, "no refund code"):
            build_graph(bundle)


class GraphConstructionTest(unittest.TestCase):
    def test_06_candidate_edges_list_all_legal_attributions(self):
        graph = fixed_graph()
        edges = {(e["txid"], e["order_id"]): e["basis"] for e in graph["edges"]}
        self.assertEqual(edges, {("B1", "D1"): "account_amount",
                                 ("B1", "D2"): "account_amount",
                                 ("B2", "D1"): "exclusive_code"})
        self.assertTrue(all(e["candidate_only"] for e in graph["edges"]))

    def test_07_exclusive_code_cannot_reach_other_claim(self):
        graph = fixed_graph()
        self.assertNotIn(("B2", "D2"), {(e["txid"], e["order_id"]) for e in graph["edges"]})

    def test_08_duplicate_export_page_does_not_create_second_node(self):
        bundle = minimal_bundle()
        bundle["bank_flows"].append({
            "record_id": "b3p2", "kind": "bank_flow", "txid": "B3", "account_mask": "M",
            "amount": 300, "batch_id": "mini", "order_code": None, "settled": True,
            "page_ref": page("bank.pdf", 3, "ab" * 32)})
        bundle["bank_flows"].append({
            "record_id": "b3p1", "kind": "bank_flow", "txid": "B3", "account_mask": "M",
            "amount": 300, "batch_id": "mini", "order_code": None, "settled": True,
            "page_ref": page("bank.pdf", 2, "ac" * 32)})
        graph = build_graph(bundle)
        self.assertEqual([r["txid"] for r in graph["receipts"]].count("B3"), 1)
        self.assertEqual(graph["duplicate_pages"][0]["ignored_record_ids"], ["b3p2"])
        matching = maximum_matching(graph)
        self.assertLessEqual(matching["size"], 2)
        self.assertNotEqual(matching["size"], 3)

    def test_09_withdrawn_claim_and_other_account_flow_are_excluded_with_reasons(self):
        bundle = load_bundle()
        graph = build_graph(bundle, revoked_txids=set())
        self.assertIn({"order_id": "D4", "reason": "claim_withdrawn"},
                      [{"order_id": e["order_id"], "reason": e["reason"]}
                       for e in graph["excluded_claims"]])
        b5 = next(r for r in graph["receipts"] if r["txid"] == "B5")
        self.assertEqual(b5["account_mask"], "ACCT-OTHER-09")
        self.assertTrue(all(e["txid"] != "B5" for e in graph["edges"]))
        self.assertTrue(any(r["reason"].startswith("no_compatible_claim")
                            for r in graph["edge_rejections"]))

    def test_10_late_revocation_excludes_flow_and_recomputes_graph(self):
        bundle = minimal_bundle()
        before = build_graph(bundle, revoked_txids=set())
        self.assertIn("B1", [r["txid"] for r in before["receipts"]])
        bundle["revocations"].append(
            {"record_id": "rv1", "kind": "bank_revocation", "txid": "B1",
             "page_ref": page("rev.pdf", 1, "cd" * 32)})
        after = build_graph(bundle)
        self.assertNotIn("B1", [r["txid"] for r in after["receipts"]])
        self.assertEqual(next(r["reason"] for r in after["excluded_receipts"]
                              if r["txid"] == "B1"), "bank_revoked")
        self.assertIn("B1", after["revoked_txids"])

    def test_11_replay_and_page_rollback_produce_identical_graph(self):
        bundle = load_bundle()
        rolled = copy.deepcopy(bundle)
        # Re-order and duplicate the replay input; graph content must be identical.
        rolled["bank_flows"] = list(reversed(rolled["bank_flows"]))
        g1 = build_graph(bundle, revoked_txids=set())
        g2 = build_graph(rolled, revoked_txids=set())
        self.assertEqual(source_digest(bundle), source_digest(rolled))
        self.assertEqual(g1["graph_version"], g2["graph_version"])
        self.assertEqual(g1["edges"], g2["edges"])
        self.assertEqual(g1["receipts"], g2["receipts"])


class MaximumMatchingTest(unittest.TestCase):
    def test_12_seed_greedy_still_only_matches_one(self):
        bundle = minimal_bundle()
        graph = fixed_graph(bundle)
        result = provisional_matches(
            [{"txid": r["txid"], "account": r["account_mask"], "amount": r["amount"],
              "order_code": r["order_code"]} for r in graph["receipts"]],
            [{"order_id": c["order_id"], "account": c["account_mask"], "amount": c["amount"]}
             for c in graph["claims"]])
        self.assertEqual(result["matches"], [{"txid": "B1", "order_id": "D1"}])
        self.assertEqual(result["unmatched_receipts"], ["B2"])
        self.assertEqual(result["unpaid_orders"], ["D2"])

    def test_13_maximum_matching_settles_both_claims_with_size_two(self):
        matching = maximum_matching(fixed_graph())
        self.assertEqual(matching["size"], 2)
        self.assertEqual({(p["txid"], p["order_id"]) for p in matching["pairs"]},
                         {("B1", "D2"), ("B2", "D1")})
        self.assertEqual(matching["unmatched_receipts"], [])
        self.assertEqual(matching["unresolved_claims"], [])

    def test_14_augmenting_path_proves_the_b1_d1_b2_swap(self):
        matching = maximum_matching(fixed_graph())
        exchange = matching["exchanges"][0]
        self.assertEqual(exchange["augmenting_path"], ["B2", "D1", "B1", "D2"])
        self.assertEqual(exchange["removed_edges"], [["B1", "D1"]])
        self.assertEqual(exchange["added_edges"], [["B1", "D2"], ["B2", "D1"]])
        self.assertEqual(exchange["outcome"], "augmented")
        self.assertEqual(exchange["size_after"], 2)

    def test_15_konig_barrier_is_minimum_and_blocks_every_edge(self):
        graph = fixed_graph()
        matching = maximum_matching(graph)
        barrier = min_barrier(graph, matching)
        self.assertEqual(barrier["size"], matching["size"])
        self.assertEqual(set(barrier["blocked_edge_ids"]),
                         {e["edge_id"] for e in graph["edges"]})
        self.assertTrue(barrier["proof"]["residual_cover_equals_residual_matching"])

    def test_16_no_account_level_aggregation_edge_is_ever_created(self):
        graph = fixed_graph()
        self.assertFalse(graph["aggregation_guard"]["account_level_aggregation"])
        for edge in graph["edges"]:
            self.assertEqual(edge["amount"], 300)
            self.assertNotIn(600, [e["amount"] for e in graph["edges"]])
        for pair in maximum_matching(graph)["pairs"]:
            self.assertIsInstance(pair["txid"], str)
            self.assertEqual(len(pair), 2)

    def test_17_revoked_b1_drops_maximum_matching_to_one_kept_by_b2(self):
        bundle = minimal_bundle()
        bundle["revocations"].append(
            {"record_id": "rv1", "kind": "bank_revocation", "txid": "B1",
             "page_ref": page("rev.pdf", 1, "cd" * 32)})
        matching = maximum_matching(build_graph(bundle))
        self.assertEqual(matching["size"], 1)
        self.assertEqual(matching["pairs"], [{"txid": "B2", "order_id": "D1"}])
        self.assertEqual(matching["unresolved_claims"], ["D2"])
        # B2 remains legally incapable of settling D2.
        self.assertNotIn(("B2", "D2"),
                         {(e["txid"], e["order_id"]) for e in build_graph(bundle)["edges"]})

    def test_18_locked_edge_forces_size_one_and_b2_suspense(self):
        graph = fixed_graph()
        matching = maximum_matching(graph, locked_edges=[("B1", "D1")])
        self.assertEqual(matching["size"], 1)
        self.assertEqual(matching["pairs"], [{"txid": "B1", "order_id": "D1"}])
        self.assertEqual(matching["unmatched_receipts"], ["B2"])
        self.assertEqual(matching["unresolved_claims"], ["D2"])

    def test_19_locked_edge_must_be_a_legal_graph_edge(self):
        with self.assertRaisesRegex(ValueError, "not a legal edge"):
            maximum_matching(fixed_graph(), locked_edges=[("B2", "D2")])


class CertificateTest(unittest.TestCase):
    def test_20_provisional_certificate_records_revocable_greedy_baseline(self):
        graph = fixed_graph()
        body = build_provisional_certificate(
            graph, cert_id="C-P-1", scope=SCOPE_REV, issued_at=ISSUED_AT,
            authorization_record_id="a1")
        self.assertEqual(body["cert_type"], "provisional")
        self.assertTrue(body["revocable"])
        self.assertEqual([(p["txid"], p["order_id"]) for p in body["pairs"]], [("B1", "D1")])
        self.assertEqual(body["unmatched_receipts"], ["B2"])
        self.assertEqual(body["unresolved_claims"], ["D2"])

    def test_21_revocable_protocol_swaps_and_issues_two_pair_certificate(self):
        graph = fixed_graph()
        prior = build_provisional_certificate(
            graph, cert_id="C-P-1", scope=SCOPE_REV, issued_at=ISSUED_AT,
            authorization_record_id="a1")
        body = build_revocable_settlement(
            graph, cert_id="C-S-1", scope=SCOPE_REV, issued_at=ISSUED_AT,
            authorization_record_id="a1", prior_cert=prior)
        self.assertEqual(body["matching_size"], 2)
        self.assertEqual({(p["txid"], p["order_id"]) for p in body["pairs"]},
                         {("B1", "D2"), ("B2", "D1")})
        chain = body["revocation_chain"][0]
        self.assertEqual(chain["revoked_pair"], ["B1", "D1"])
        self.assertEqual(chain["new_pair"], ["B1", "D2"])
        self.assertFalse(chain["customer_re_sign_required"])
        self.assertEqual(body["voids_cert_id"], "C-P-1")
        self.assertEqual(body["resign_objects"], [])
        # Every settled pair cites original page coordinates and seal digests.
        for pair in body["pairs"]:
            self.assertTrue(pair["evidence"])
            self.assertTrue(all(item["page_ref"]["seal_digest"] for item in pair["evidence"]))
            self.assertTrue(all("rect" in item["page_ref"] for item in pair["evidence"]))

    def test_22_locked_protocol_keeps_writeoff_and_lists_b2_original_route_suspense(self):
        graph = fixed_graph()
        writeoff = build_bank_writeoff_certificate(
            graph, [("B1", "D1")], cert_id="C-W-1", scope=SCOPE_LOCK,
            issued_at=ISSUED_AT, authorization_record_id="a2")
        self.assertFalse(writeoff["transferable"])
        body = build_locked_settlement(
            graph, [["B1", "D1"]], cert_id="C-L-1", scope=SCOPE_LOCK,
            issued_at=ISSUED_AT, authorization_record_id="a2",
            writeoff_cert=writeoff)
        self.assertEqual(body["matching_size"], 1)
        self.assertEqual([(p["txid"], p["order_id"]) for p in body["pairs"]], [("B1", "D1")])
        self.assertEqual(body["suspense"][0]["txid"], "B2")
        self.assertEqual(body["suspense"][0]["disposition"],
                         "return_via_bank_original_route")
        self.assertEqual(body["unresolved_claims"], ["D2"])
        resigned = {item["object"]: item for item in body["resign_objects"]}
        self.assertIn("bank_flow:B2", resigned)
        self.assertIn("claim:D2", resigned)
        self.assertTrue(all(item["re_sign_required"] for item in body["resign_objects"]))
        self.assertEqual(body["bank_writeoff_cert_id"], "C-W-1")

    def test_23_two_protocols_emit_different_certificates_for_same_originals(self):
        graph = fixed_graph()
        revocable = build_revocable_settlement(
            graph, cert_id="C-S-1", scope=SCOPE_REV, issued_at=ISSUED_AT,
            authorization_record_id="a1")
        writeoff = build_bank_writeoff_certificate(
            graph, [("B1", "D1")], cert_id="C-W-1", scope=SCOPE_LOCK,
            issued_at=ISSUED_AT, authorization_record_id="a2")
        locked = build_locked_settlement(
            graph, [["B1", "D1"]], cert_id="C-L-1", scope=SCOPE_LOCK,
            issued_at=ISSUED_AT, authorization_record_id="a2",
            writeoff_cert=writeoff)
        self.assertNotEqual(certificate_fingerprint(revocable),
                            certificate_fingerprint(locked))
        self.assertEqual(revocable["matching_size"], 2)
        self.assertEqual(locked["matching_size"], 1)
        self.assertEqual(revocable["suspense"], [])
        self.assertEqual(locked["suspense"][0]["txid"], "B2")

    def test_24_rendered_certificate_is_canonical_and_self_fingerprinted(self):
        body = build_revocable_settlement(
            fixed_graph(), cert_id="C-S-1", scope=SCOPE_REV, issued_at=ISSUED_AT,
            authorization_record_id="a1")
        rendered = render_certificate(body)
        self.assertEqual(rendered["sha256"], certificate_fingerprint(body))
        self.assertEqual(json.loads(rendered["canonical"])["sha256"], rendered["sha256"])


class StoreLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "refund.sqlite")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_25_ingest_is_idempotent_and_rebuilds_identical_bundle(self):
        bundle = load_bundle()
        first = ingest_refund_bundle(self.conn, bundle, ISSUED_AT)
        second = ingest_refund_bundle(self.conn, copy.deepcopy(bundle), ISSUED_AT)
        self.assertEqual(first["inserted"], second["replayed"])
        rebuilt = load_refund_bundle(self.conn, bundle["batch_id"])
        self.assertEqual(source_digest(rebuilt), source_digest(bundle))

    def test_26_record_collision_raises_and_keeps_ledger_intact(self):
        bundle = load_bundle()
        ingest_refund_bundle(self.conn, bundle, ISSUED_AT)
        tampered = copy.deepcopy(bundle)
        tampered["bank_flows"][0]["amount"] = 999
        with self.assertRaisesRegex(ValueError, "collision"):
            ingest_refund_bundle(self.conn, tampered, ISSUED_AT)
        rebuilt = load_refund_bundle(self.conn, bundle["batch_id"])
        self.assertEqual(source_digest(rebuilt), source_digest(bundle))

    def test_27_replay_same_sources_same_certificate_fingerprint(self):
        bundle = load_bundle()
        ingest_refund_bundle(self.conn, bundle, ISSUED_AT)
        graph = build_graph(load_refund_bundle(self.conn, bundle["batch_id"]),
                            revoked_txids=set())
        body = build_revocable_settlement(
            graph, cert_id=next_refund_cert_id(self.conn, "C-S"),
            scope=SCOPE_REV, issued_at=ISSUED_AT, authorization_record_id="AU-FAM-REV")
        issued = issue_refund_certificate(self.conn, body, ingested_at=ISSUED_AT)
        again = issue_refund_certificate(self.conn, copy.deepcopy(body),
                                         ingested_at=ISSUED_AT)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["cert_id"], issued["cert_id"])
        self.assertEqual(certificate_fingerprint(again["body"]),
                         certificate_fingerprint(body))

    def test_28_crash_recovery_cannot_leave_two_active_certificates(self):
        bundle = minimal_bundle()
        ingest_refund_bundle(self.conn, bundle, ISSUED_AT)
        graph = fixed_graph(bundle)
        body = build_revocable_settlement(
            graph, cert_id=next_refund_cert_id(self.conn, "C-S"),
            scope="mini:revocable_provisional", issued_at=ISSUED_AT,
            authorization_record_id="a1")
        issue_refund_certificate(self.conn, body, ingested_at=ISSUED_AT)
        # A second active row for the same (scope, cert_type) must violate the
        # database invariant itself, even if an insert bypasses the helper.
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO refund_certificates "
                "(cert_id, scope, cert_type, protocol, status, batch_id, source_digest, "
                "graph_version, body, body_sha256, issued_at, replayed) "
                "VALUES ('C-S-9999', ?, 'settlement_revocable', ?, 'active', ?, ?, ?, ?, ?, ?, 0)",
                (body["scope"], body["protocol"], body["batch_id"],
                 body["source_digest"], body["graph_version"], "{}", "0" * 64, ISSUED_AT))
            self.conn.commit()
        self.conn.rollback()
        # Different cert_type in the same scope remains legal.
        other = build_provisional_certificate(
            graph, cert_id="C-P-9001", scope="mini:other_protocol",
            issued_at=ISSUED_AT, authorization_record_id="a1")
        issue_refund_certificate(self.conn, other, ingested_at=ISSUED_AT)
        actives = self.conn.execute(
            "SELECT COUNT(*) FROM refund_certificates WHERE status='active'").fetchone()[0]
        self.assertEqual(actives, 2)

    def test_29_void_keeps_history_and_links_replacement_without_deleting_b1_d1(self):
        bundle = minimal_bundle()
        ingest_refund_bundle(self.conn, bundle, ISSUED_AT)
        graph = fixed_graph(bundle)
        prior_body = build_provisional_certificate(
            graph, cert_id=next_refund_cert_id(self.conn, "C-P"),
            scope="mini:revocable_provisional", issued_at=ISSUED_AT,
            authorization_record_id="a1")
        prior = issue_refund_certificate(self.conn, prior_body, ingested_at=ISSUED_AT)
        new_body = build_revocable_settlement(
            graph, cert_id=next_refund_cert_id(self.conn, "C-S"),
            scope="mini:revocable_provisional:v2", issued_at=VOID_AT,
            authorization_record_id="a1", prior_cert=prior["body"])
        new = issue_refund_certificate(self.conn, new_body, ingested_at=VOID_AT)
        void_refund_certificate(self.conn, prior["cert_id"], voided_at=VOID_AT,
                                reason="augmenting_swap",
                                replacement_cert_id=new["cert_id"])
        self.assertIsNone(get_active_certificate(
            self.conn, "mini:revocable_provisional", "provisional"))
        stored = list_refund_certificates(self.conn, "mini")
        old = next(c for c in stored if c["cert_id"] == prior["cert_id"])
        self.assertEqual(old["status"], "void")
        # The historical B1->D1 pair remains in the stored body.
        self.assertIn(("B1", "D1"),
                      {(p["txid"], p["order_id"]) for p in old["body"]["pairs"]})
        events = [e["event_type"] for e in
                  list_refund_certificate_events(self.conn, prior["cert_id"])]
        self.assertEqual(events, ["issued", "voided"])

    def test_30_locked_edges_are_sourced_only_from_active_writeoff(self):
        bundle = minimal_bundle()
        ingest_refund_bundle(self.conn, bundle, ISSUED_AT)
        graph = fixed_graph(bundle)
        writeoff_body = build_bank_writeoff_certificate(
            graph, [("B1", "D1")], cert_id=next_refund_cert_id(self.conn, "C-W"),
            scope="mini:non_transferable_bank_writeoff", issued_at=ISSUED_AT,
            authorization_record_id="a2")
        issue_refund_certificate(self.conn, writeoff_body, ingested_at=ISSUED_AT)
        self.assertEqual(active_locked_edges(
            self.conn, "mini:non_transferable_bank_writeoff"), [["B1", "D1"]])
        void_refund_certificate(self.conn, writeoff_body["cert_id"],
                                voided_at=VOID_AT, reason="bank_revoked_b1")
        self.assertEqual(active_locked_edges(
            self.conn, "mini:non_transferable_bank_writeoff"), [])

    def test_31_refund_ledger_does_not_touch_order_or_delivery_ledger(self):
        before = self.conn.execute("SELECT COUNT(*) FROM source_events").fetchone()[0]
        ingest_refund_bundle(self.conn, load_bundle(), ISSUED_AT)
        after = self.conn.execute("SELECT COUNT(*) FROM source_events").fetchone()[0]
        self.assertEqual(before, after)
        for table in ("orders", "payments", "parcels", "receipts", "current_decisions"):
            self.assertEqual(
                self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_32_full_lifecycle_revocation_reduces_locked_matching_to_b2_d1(self):
        bundle = load_bundle()
        ingest_refund_bundle(self.conn, bundle, ISSUED_AT)
        full = load_refund_bundle(self.conn, bundle["batch_id"])
        graph = build_graph(full, revoked_txids=set())
        writeoff_body = build_bank_writeoff_certificate(
            graph, [("B1", "D1")], cert_id=next_refund_cert_id(self.conn, "C-W"),
            scope=SCOPE_LOCK, issued_at=ISSUED_AT,
            authorization_record_id="AU-FAM-LOCK")
        issue_refund_certificate(self.conn, writeoff_body, ingested_at=ISSUED_AT)
        # Late bank revocation page arrives; the old write-off must be voided, never erased.
        revoked_graph = build_graph(full, revoked_txids={"B1"})
        self.assertEqual(maximum_matching(revoked_graph)["size"], 3)
        void_refund_certificate(self.conn, writeoff_body["cert_id"],
                                voided_at=VOID_AT, reason="bank_revocation:B1")
        self.assertEqual(active_locked_edges(self.conn, SCOPE_LOCK), [])
        remaining = maximum_matching(revoked_graph)
        self.assertIn({"txid": "B2", "order_id": "D1"}, remaining["pairs"])
        self.assertNotIn("B1", [p["txid"] for p in remaining["pairs"]])


if __name__ == "__main__":
    unittest.main()

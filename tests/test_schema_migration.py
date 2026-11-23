"""Dual-layer v1/v2 page ingestion: identity mapping, quarantine, and recovery.

The migration keeps two independently auditable books:

* raw receipts hold every original page byte (hash, page cursor, row locator,
  producer signature and schema_version) even when several rows normalize to the
  same semantic event;
* the semantic projection admits each distinct meaning exactly once, resolving
  reused external ids through the signed producer-revision contract or refusing
  them under the unique-external-id contract.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from preorderproof.offer import offer_versions
from preorderproof.schema_import import (
    file_hash,
    ingest_pages,
    ingest_pages_file,
    migration_table,
    project_generation,
    quarantine_list,
    receipt_integrity,
    recover_pending,
    stage_pages,
)
from preorderproof.store import connect, digest, ingest

ROOT = Path(__file__).resolve().parents[1]


def fixture(name: str) -> dict:
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


class SchemaMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "case.sqlite"
        self.conn = connect(self.db)
        # Fixed runnable baseline: offer-1 version 1, price 1000, due 2026-12-10.
        self.base = ingest(self.conn, "offer", fixture("offer-ok.json"))
        self.packages = fixture("offer-v2-pages.json")["packages"]
        self.v1_offer_digest = fixture("offer-v2-pages.json")["canonical_v1_offer_sha256"]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _ingest(self, key):
        return ingest_pages(self.conn, self.packages[key])

    def _cross_reference(self):
        return {(row["page_cursor"], row["row_no"]): row
                for row in migration_table(self.conn)}

    # ------------------------------------------------------------------ #
    # Contract A: reused external id with monotonic producer revisions.
    # ------------------------------------------------------------------ #
    def test_synonym_v2_resend_keeps_two_raw_rows_one_semantic_offer(self):
        result = self._ingest("contract_a_synonym")
        self.assertEqual((result["linked"], result["quarantined"],
                          result["semantic_inserted"], result["semantic_replayed"]),
                         (1, 0, 0, 1))
        # The business projection still holds exactly the original batch and offer.
        self.assertEqual(
            [r[0] for r in self.conn.execute("SELECT event_id FROM source_events")],
            ["lot-1", "offer-1"])
        versions = offer_versions(self.conn)["O1"]
        self.assertEqual([(v["version"], v["price"], v["due"]) for v in versions],
                         [(1, 1000, "2026-12-10")])
        # Both original pages are independently retrievable byte-for-byte.
        receipts = self.conn.execute("SELECT receipt_id, raw_sha256, schema_version "
                                     "FROM raw_receipts").fetchall()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["schema_version"], 2)
        cross = migration_table(self.conn)[0]
        self.assertEqual(cross["external_event_id"], "offer-1")
        self.assertEqual(cross["semantic_event_id"], "offer-1")
        self.assertEqual(cross["role"], "synonym_replay")
        self.assertIsNone(cross["quarantine_reason"])
        self.assertTrue(receipt_integrity(self.conn)["ok"])

    def test_signed_v2_revision_becomes_offer_version_two_without_losing_pages(self):
        self._ingest("contract_a_synonym")
        revision = self._ingest("contract_a_signed_revision")
        self.assertEqual((revision["linked"], revision["semantic_inserted"]), (1, 1))
        versions = offer_versions(self.conn)["O1"]
        self.assertEqual([(v["version"], v["due"], v["price"]) for v in versions],
                         [(1, "2026-12-10", 1000), (2, "2026-12-12", 1000)])
        identity = self.conn.execute(
            "SELECT * FROM event_identity_map WHERE producer_revision=2").fetchone()
        self.assertEqual(identity["internal_event_id"], "offer-1#rev2")
        self.assertEqual(identity["external_event_id"], "offer-1")
        self.assertEqual(identity["identity_contract"], "reused_external_id")
        self.assertEqual(identity["supersedes"], self.v1_offer_digest)
        # The v1 and synonym-v2 raw pages remain addressable alongside the revision.
        raw_count = self.conn.execute("SELECT COUNT(*) FROM raw_receipts").fetchone()[0]
        self.assertEqual(raw_count, 2)
        distinct_bytes = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT raw_sha256 FROM raw_receipts")}
        self.assertEqual(len(distinct_bytes), 2)
        roles = {row["role"] for row in migration_table(self.conn)
                 if row["semantic_event_id"]}
        self.assertEqual(roles, {"synonym_replay", "revision"})

    def test_revision_arriving_before_the_synonym_still_projects_by_revision_link(self):
        revision = self._ingest("contract_a_signed_revision")
        synonym = self._ingest("contract_a_synonym")
        self.assertEqual((revision["semantic_inserted"], synonym["semantic_replayed"]),
                         (1, 1))
        versions = offer_versions(self.conn)["O1"]
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertEqual(
            [r[0] for r in self.conn.execute(
                "SELECT event_id FROM source_events ORDER BY event_id")],
            ["lot-1", "offer-1", "offer-1#rev2"])

    def test_unsigned_reused_id_is_quarantined_and_never_overwrites(self):
        self._ingest("contract_a_signed_revision")
        result = self._ingest("contract_a_unsigned_reuse")
        self.assertEqual((result["linked"], result["quarantined"]), (0, 1))
        reasons = {q["reason"] for q in quarantine_list(self.conn)}
        self.assertEqual(reasons, {"unsigned_reuse_no_override"})
        # No upload-time overwrite: version 2 keeps its original due date.
        versions = offer_versions(self.conn)["O1"]
        self.assertEqual([(v["version"], v["due"]) for v in versions],
                         [(1, "2026-12-10"), (2, "2026-12-12")])
        # The quarantined raw bytes are still preserved for the merchant reply.
        row = self.conn.execute(
            "SELECT r.raw_body FROM quarantine q JOIN raw_receipts r "
            "ON r.receipt_id = q.receipt_id").fetchone()
        self.assertIn("due_date", row["raw_body"])

    def test_supersedes_digest_must_chain_to_the_previous_revision_page(self):
        broken = json.loads(json.dumps(self.packages["contract_a_signed_revision"]))
        broken["pages"][0]["supersedes"] = digest({"not": "the previous page"})
        result = ingest_pages(self.conn, broken)
        self.assertEqual(result["quarantined"], 1)
        self.assertEqual(result["details"][0]["reason"], "broken_revision_chain")
        self.assertEqual(offer_versions(self.conn)["O1"][0]["version"], 1)

    def test_stale_revision_number_is_quarantined(self):
        self._ingest("contract_a_signed_revision")
        stale = json.loads(json.dumps(self.packages["contract_a_signed_revision"]))
        # A second, separately delivered page reusing revision 2 with different bytes.
        stale["file_id"] = "pages-stale-revision"
        stale["pages"][0]["rows"][0]["event_at"] = "2026-11-09"
        result = ingest_pages(self.conn, stale)
        self.assertEqual(result["details"][0]["reason"], "stale_revision")

    # ------------------------------------------------------------------ #
    # Quarantine list: malformed rows, duplicate pages, competing revisions.
    # ------------------------------------------------------------------ #
    def test_broken_duplicate_and_competing_rows_enter_the_quarantine_list(self):
        result = self._ingest("contract_a_quarantine")
        reasons = {d["reason"] for d in result["details"]}
        self.assertEqual(reasons,
                         {"malformed_row", "duplicate_page", "competing_revision"})
        # The valid synonym row still projects; the two competing revision rows do not.
        self.assertEqual(result["linked"], 1)
        versions = offer_versions(self.conn)["O1"]
        self.assertEqual([v["version"] for v in versions], [1])
        cross = self._cross_reference()
        self.assertEqual(cross[(0, 0)]["quarantine_reason"], "malformed_row")
        self.assertEqual(cross[(0, 1)]["role"], "synonym_replay")
        self.assertEqual(cross[(0, 2)]["quarantine_reason"], "duplicate_page")
        self.assertEqual(cross[(1, 0)]["quarantine_reason"], "competing_revision")
        self.assertEqual(cross[(1, 1)]["quarantine_reason"], "competing_revision")
        self.assertTrue(receipt_integrity(self.conn)["ok"])

    def test_missing_page_cursor_quarantines_everything_after_the_gap(self):
        synonym = self.packages["contract_a_synonym"]["pages"][0]["rows"][0]
        envelope = {
            "file_id": "pages-broken-cursor", "producer": "merchant-alpha",
            "identity_contract": "reused_external_id", "schema_version": 2,
            "pages": [
                {"page_cursor": 0, "rows": [dict(synonym)]},
                {"page_cursor": 2, "rows": [dict(synonym)]},
            ],
        }
        result = ingest_pages(self.conn, envelope)
        self.assertEqual({d["reason"] for d in result["details"]}, {"broken_page"})
        self.assertEqual(result["linked"], 1)
        self.assertTrue(receipt_integrity(self.conn)["ok"])

    # ------------------------------------------------------------------ #
    # Contract B: unique external ids; reuse rules must not mix with A.
    # ------------------------------------------------------------------ #
    def test_contract_b_reissue_uses_a_fresh_external_id(self):
        result = self._ingest("contract_b_unique_id")
        self.assertEqual((result["linked"], result["quarantined"]), (1, 2))
        reissue = self.conn.execute(
            "SELECT * FROM event_identity_map WHERE internal_event_id='offer-3'").fetchone()
        self.assertEqual(reissue["identity_contract"], "unique_external_id")
        reissue_link = self.conn.execute(
            "SELECT role FROM raw_semantic_links WHERE internal_event_id='offer-3'").fetchone()
        self.assertEqual(reissue_link["role"], "reissue")
        versions = offer_versions(self.conn)["O1"]
        self.assertEqual([(v["version"], v["due"]) for v in versions],
                         [(1, "2026-12-10"), (2, "2026-12-12")])
        self.assertEqual({d["reason"] for d in result["details"]},
                         {"identity_reuse_violation"})

    def test_contracts_cannot_be_mixed_inside_one_batch(self):
        # A signed reused-id revision page smuggled into a unique-id batch must be
        # rejected rather than silently applying contract A's reuse rules.
        envelope = self.packages["contract_b_unique_id"]
        mixed = json.loads(json.dumps(envelope))
        mixed["pages"][2] = {
            "page_cursor": 2, "producer_revision": 2,
            "producer_signature": "sig-merchant-bravo-rev2",
            "supersedes": self.v1_offer_digest,
            "rows": [dict(envelope["pages"][2]["rows"][0])],
        }
        result = ingest_pages(self.conn, mixed)
        self.assertTrue(all(d["reason"] == "identity_reuse_violation"
                            for d in result["details"]))
        self.assertNotIn("offer-1#rev2",
                         [r[0] for r in self.conn.execute(
                             "SELECT internal_event_id FROM event_identity_map")])

    def test_second_producer_reusing_the_identity_namespace_is_quarantined(self):
        self._ingest("contract_a_signed_revision")
        intruder = json.loads(json.dumps(self.packages["contract_a_signed_revision"]))
        intruder["producer"] = "merchant-charlie"
        intruder["file_id"] = "pages-intruder"
        intruder["pages"][0]["rows"][0]["capacity"] = 9
        result = ingest_pages(self.conn, intruder)
        self.assertEqual(result["details"][0]["reason"], "identity_collision")
        # The intruder's bytes never reach either ledger.
        self.assertEqual(
            [r[0] for r in self.conn.execute("SELECT event_id FROM source_events")],
            ["lot-1", "offer-1", "offer-1#rev2"])
        self.assertTrue(receipt_integrity(self.conn)["ok"])

    def test_invalid_envelope_contract_is_rejected_before_any_raw_write(self):
        bad = json.loads(json.dumps(self.packages["contract_a_synonym"]))
        bad["identity_contract"] = "maybe_both"
        with self.assertRaises(ValueError):
            ingest_pages(self.conn, bad)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_receipts").fetchone()[0], 0)

    # ------------------------------------------------------------------ #
    # v1 paged files and idempotent replay.
    # ------------------------------------------------------------------ #
    def test_v1_pages_are_ingested_in_their_original_dialect(self):
        fresh = connect(":memory:")
        result = ingest_pages(fresh, self.packages["v1_pages"])
        self.assertEqual((result["linked"], result["quarantined"]), (2, 0))
        self.assertEqual(
            [r[0] for r in fresh.execute("SELECT event_id FROM source_events")],
            ["lot-1", "offer-1"])
        self.assertTrue(receipt_integrity(fresh)["ok"])
        fresh.close()

    def test_reingesting_the_same_package_converges_without_duplicate_events(self):
        first = self._ingest("contract_a_synonym")
        second = self._ingest("contract_a_synonym")
        self.assertEqual(first["generation_id"], second["generation_id"])
        self.assertEqual(second["semantic_inserted"], 0)
        self.assertEqual(second["semantic_replayed"], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM source_events").fetchone()[0], 2)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_receipts").fetchone()[0], 1)

    # ------------------------------------------------------------------ #
    # Crash boundaries: staging vs projection and orphan recovery.
    # ------------------------------------------------------------------ #
    def test_staged_generation_can_be_recovered_without_orphan_rows(self):
        package_dir = Path(self.tmp.name) / "packages"
        stage_pages(self.conn, self.packages["contract_a_signed_revision"],
                    package_dir=package_dir)
        # Simulated crash point: raw bytes landed, semantic projection never ran.
        self.conn.close()
        self.conn = connect(self.db)
        self.assertEqual(
            self.conn.execute("SELECT status FROM ingest_generations").fetchone()[0],
            "staged")
        recovery = recover_pending(self.conn, package_dir=package_dir)
        self.assertEqual(recovery["recovered_generations"], 1)
        self.assertTrue(recovery["receipt_integrity"]["ok"])
        self.assertEqual(
            self.conn.execute("SELECT status FROM ingest_generations").fetchone()[0],
            "projected")
        versions = offer_versions(self.conn)["O1"]
        self.assertEqual([v["version"] for v in versions], [1, 2])
        # Recovering again is a no-op.
        again = recover_pending(self.conn, package_dir=package_dir)
        self.assertEqual(again["recovered_generations"], 0)

    def test_landed_package_without_a_ledger_row_is_picked_up_by_recovery(self):
        package_dir = Path(self.tmp.name) / "packages"
        package_dir.mkdir()
        envelope = self.packages["contract_a_signed_revision"]
        generation_id = "gen-" + file_hash(envelope)[:16]
        (package_dir / f"{generation_id}.json").write_text(
            json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
        recovery = recover_pending(self.conn, package_dir=package_dir)
        self.assertEqual(recovery["recovered_generations"], 1)
        self.assertTrue(recovery["receipt_integrity"]["ok"])
        self.assertEqual(
            [v["version"] for v in offer_versions(self.conn)["O1"]], [1, 2])

    def test_file_based_ingestion_persists_the_exact_package_bytes(self):
        package_dir = Path(self.tmp.name) / "packages"
        source = Path(self.tmp.name) / "a2.json"
        envelope = self.packages["contract_a_signed_revision"]
        source.write_bytes(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
        result = ingest_pages_file(self.conn, source, package_dir=package_dir)
        self.assertEqual(result["semantic_inserted"], 1)
        landed = self.conn.execute(
            "SELECT bytes_path, package_sha256 FROM ingest_generations").fetchone()
        self.assertTrue(Path(landed["bytes_path"]).exists())
        self.assertEqual(landed["package_sha256"],
                         __import__("hashlib").sha256(source.read_bytes()).hexdigest())

    def test_reprojection_is_deterministic_and_keeps_the_raw_ledger_intact(self):
        self._ingest("contract_a_quarantine")
        raw_before = [dict(r) for r in self.conn.execute(
            "SELECT * FROM raw_receipts ORDER BY receipt_id")]
        generation_id = raw_before[0]["generation_id"]
        project_generation(self.conn, generation_id)
        raw_after = [dict(r) for r in self.conn.execute(
            "SELECT * FROM raw_receipts ORDER BY receipt_id")]
        self.assertEqual(raw_before, raw_after)
        self.assertEqual(len(quarantine_list(self.conn)), 4)
        self.assertTrue(receipt_integrity(self.conn)["ok"])


if __name__ == "__main__":
    unittest.main()

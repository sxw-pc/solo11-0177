import json
import unittest
from pathlib import Path

from preorderproof.privacy import (
    K_MIN,
    consumer_token,
    iso_week,
    publish,
    release_history,
    snapshot_persons,
)
from preorderproof.store import connect, connect_mapping, ingest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "fixtures" / "privacy-joined-releases.json").read_text(encoding="utf-8"))

INTERNAL_KEYS = {"event_id", "order_id", "consumer", "consumer_token", "parcel_id",
                 "receipt_id", "witness", "placed_at", "received_at"}


def stage(conn, *names):
    for name in names:
        batch = FIXTURE[name]
        for subsystem in ("payment", "delivery"):
            events = batch.get(subsystem)
            if events:
                ingest(conn, subsystem, events)


class PrivacyReleaseTest(unittest.TestCase):
    def setUp(self):
        self.store = connect(":memory:")
        self.mapping = connect_mapping(":memory:")

    def tearDown(self):
        self.store.close()
        self.mapping.close()

    def assert_no_internal_keys(self, node):
        if isinstance(node, dict):
            for key, value in node.items():
                self.assertNotIn(key, INTERNAL_KEYS)
                self.assert_no_internal_keys(value)
        elif isinstance(node, list):
            for item in node:
                self.assert_no_internal_keys(item)

    def test_snapshot_joins_three_tables_to_one_record_per_person(self):
        stage(self.store, "base_v1")
        persons = snapshot_persons(self.store)
        self.assertEqual(len(persons), 2)
        by_consumer = {p["consumer"]: p for p in persons}
        alice, bob = by_consumer["alice"], by_consumer["bob"]
        # the internal join follows store.py's order_id and parcel_id keys:
        # the 2026-11-03 return belongs to C1/alice, the 2026-11-04 signing to C2/bob
        self.assertEqual(alice["order_ids"], ["C1"])
        self.assertEqual(alice["return_status"], "returned")
        self.assertEqual(alice["received_at"], "2026-11-03")
        self.assertEqual(bob["order_ids"], ["C2"])
        self.assertEqual(bob["return_status"], "signed")
        self.assertEqual(bob["received_at"], "2026-11-04")
        # both fall into the same purchase and receipt week
        self.assertEqual(alice["purchase_week"], iso_week("2026-11-02"))
        self.assertEqual(alice["purchase_week"], bob["purchase_week"])
        self.assertEqual(alice["receipt_week"], bob["receipt_week"])

    def test_duplicate_receipts_and_late_return_never_double_count_person(self):
        stage(self.store, "base_v1", "duplicate_receipts")
        persons = snapshot_persons(self.store)
        self.assertEqual(len(persons), 2)  # repeated signings do not add people
        bob = [p for p in persons if p["consumer"] == "bob"][0]
        self.assertEqual(bob["return_status"], "returned")  # late return is his final state
        self.assertEqual(bob["receipt_week"], iso_week("2026-11-10"))
        alice = [p for p in persons if p["consumer"] == "alice"][0]
        self.assertEqual(alice["receipt_week"], iso_week("2026-11-03"))

    def test_replay_same_snapshot_yields_identical_persons(self):
        stage(self.store, "base_v1", "late_c3")
        other = connect(":memory:")
        try:
            stage(other, "base_v1", "late_c3")
            self.assertEqual(snapshot_persons(self.store), snapshot_persons(other))
        finally:
            other.close()

    def test_v1_release_k_anonymous_on_joined_view_without_stable_keys(self):
        stage(self.store, "base_v1")
        plan = publish(self.mapping, self.store, version="V1", route="B")
        self.assertEqual(plan["status"], "published")
        self.assertEqual(len(plan["groups"]), 1)
        group = plan["groups"][0]
        # the k-check runs on the joined view: one (purchase_week, receipt_week)
        # class of two people, not two singleton exact-date cells
        self.assertEqual((group["members"], group["returned"], group["signed"]), (2, 1, 1))
        self.assertGreaterEqual(group["members"], K_MIN)
        self.assertEqual(group["purchase_week"], iso_week("2026-11-02"))
        self.assertEqual(group["receipt_week"], iso_week("2026-11-03"))
        self.assertEqual(plan["dimensions"]["usable"],
                         ["purchase_week", "receipt_week", "return_status"])
        suppressed = {d["field"] for d in plan["dimensions"]["suppressed"]}
        self.assertTrue({"order_id", "consumer_token", "event_id", "received_at"} <= suppressed)
        self.assertTrue(plan["differential_proof"])
        # no unique receipt date, stable token, order id or raw event id leaks
        blob = json.dumps(plan, ensure_ascii=False)
        for leaked in ("C1", "C2", "alice", "bob", "P1", "P2", "R1r", "R2s",
                       "2026-11-02", "2026-11-03", "2026-11-04", "porder-1", "preceipt-1r",
                       consumer_token("alice"), consumer_token("bob")):
            self.assertNotIn(leaked, blob)
        self.assert_no_internal_keys(plan)

    def test_controlled_mapping_keeps_tokens_and_ids_out_of_release(self):
        stage(self.store, "base_v1")
        plan = publish(self.mapping, self.store, version="V1", route="A")
        rows = self.mapping.execute("SELECT * FROM privacy_mapping").fetchall()
        self.assertEqual(len(rows), 2)
        controlled = {r["consumer_token"]: r for r in rows}
        self.assertEqual(set(controlled), {consumer_token("alice"), consumer_token("bob")})
        self.assertIn("C1", json.loads(controlled[consumer_token("alice")]["order_ids"]))
        blob = json.dumps(plan, ensure_ascii=False)
        for token in controlled:
            self.assertNotIn(token, blob)

    def test_route_b_rejects_single_person_difference(self):
        stage(self.store, "base_v1")
        v1 = publish(self.mapping, self.store, version="V1", route="B")
        self.assertEqual(v1["status"], "published")
        stage(self.store, "late_c3")
        v2 = publish(self.mapping, self.store, version="V2", route="B")
        self.assertEqual(v2["status"], "rejected")
        self.assertEqual(v2["reason"], "single_person_difference")
        unsafe = [p for p in v2["differential_proof"] if p["verdict"] == "unsafe"]
        self.assertEqual(len(unsafe), 1)
        self.assertEqual(unsafe[0]["against"], "V1")
        self.assertEqual(unsafe[0]["delta_members"], 1)
        # the rejected release is not persisted; V1 stays the only accessible version
        self.assertEqual([r["version"] for r in release_history(self.mapping)], ["V1"])
        self.assert_no_internal_keys(v2)

    def test_route_b_holds_latecomer_until_delta_group_reaches_k(self):
        stage(self.store, "base_v1")
        publish(self.mapping, self.store, version="V1", route="B")
        stage(self.store, "late_c3")
        held = publish(self.mapping, self.store, version="V2", route="B")
        self.assertEqual(held["status"], "rejected")
        # the controlled store waits for another same-week newcomer, then V3 is safe
        stage(self.store, "late_c4")
        v3 = publish(self.mapping, self.store, version="V3", route="B")
        self.assertEqual(v3["status"], "published")
        self.assertEqual(len(v3["groups"]), 1)
        group = v3["groups"][0]
        self.assertEqual((group["members"], group["returned"], group["signed"]), (4, 2, 2))
        proof = [p for p in v3["differential_proof"] if p["against"] == "V1"][0]
        self.assertEqual((proof["delta_members"], proof["delta_returned"]), (2, 1))
        self.assertEqual(proof["verdict"], "safe")
        self.assertEqual([r["version"] for r in release_history(self.mapping)], ["V1", "V3"])

    def test_route_a_freezes_published_cohort_and_defers_latecomer(self):
        stage(self.store, "base_v1")
        v1 = publish(self.mapping, self.store, version="V1", route="A")
        stage(self.store, "late_c3")
        v2 = publish(self.mapping, self.store, version="V2", route="A")
        self.assertEqual(v2["status"], "published")
        carried = [g for g in v2["groups"] if g["carried"]]
        self.assertEqual(len(carried), 1)
        for key in ("cohort", "purchase_week", "receipt_week", "members", "returned", "signed"):
            self.assertEqual(carried[0][key], v1["groups"][0][key])
        # C3 is deferred to the next closed cohort and not yet published anywhere
        self.assertEqual(sum(g["members"] for g in v2["groups"]), 2)
        pending = [w for w in v2["withheld"] if not w["cohort"].endswith("#1")]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["members"], 1)
        self.assertEqual(pending[0]["reason"], f"below_k={K_MIN}")
        self.assertNotIn("returned", pending[0])  # the deferred person's status stays hidden
        proof = [p for p in v2["differential_proof"] if p["cohort"] == carried[0]["cohort"]][0]
        self.assertEqual((proof["delta_members"], proof["verdict"]), (0, "safe"))

    def test_route_a_closes_next_cohort_when_it_reaches_k(self):
        stage(self.store, "base_v1")
        publish(self.mapping, self.store, version="V1", route="A")
        stage(self.store, "late_c3", "late_c4")
        v2 = publish(self.mapping, self.store, version="V2", route="A")
        self.assertEqual(v2["status"], "published")
        self.assertEqual(len(v2["groups"]), 2)
        new = [g for g in v2["groups"] if not g["carried"]][0]
        self.assertEqual((new["members"], new["returned"], new["signed"]), (2, 1, 1))
        self.assertNotEqual(new["cohort"], [g for g in v2["groups"] if g["carried"]][0]["cohort"])
        proof = [p for p in v2["differential_proof"] if p["cohort"] == new["cohort"]][0]
        self.assertEqual(proof["verdict"], "safe")

    def test_routes_a_and_b_give_differently_shaped_safe_cohorts(self):
        stage(self.store, "base_v1")
        publish(self.mapping, self.store, version="V1", route="A")
        stage(self.store, "late_c3", "late_c4")
        route_a = publish(self.mapping, self.store, version="V2", route="A")
        store_b, mapping_b = connect(":memory:"), connect_mapping(":memory:")
        try:
            stage(store_b, "base_v1")
            publish(mapping_b, store_b, version="V1", route="B")
            stage(store_b, "late_c3", "late_c4")
            route_b = publish(mapping_b, store_b, version="V2", route="B")
        finally:
            store_b.close()
            mapping_b.close()
        # the same late C3 original yields different cohorts and sample sizes
        a_new = [g for g in route_a["groups"] if not g["carried"]][0]
        b_main = route_b["groups"][0]
        self.assertEqual(a_new["members"], 2)  # C3 surfaces only in the next closed cohort
        self.assertEqual(b_main["members"], 4)  # C3 merges into the republished same-week cohort
        self.assertNotEqual(a_new["cohort"], b_main["cohort"])
        for document in (route_a, route_b):
            self.assertTrue(document["differential_proof"])
            for proof in document["differential_proof"]:
                self.assertEqual(proof["verdict"], "safe")

    def test_joined_view_singletons_are_withheld(self):
        # after the late return, each (purchase_week, receipt_week) cell holds one
        # person; a release that only checked the orders table would still pass k=2
        stage(self.store, "base_v1", "duplicate_receipts")
        plan = publish(self.mapping, self.store, version="V1", route="B")
        self.assertEqual(plan["status"], "published")
        self.assertEqual(plan["groups"], [])
        self.assertEqual(len(plan["withheld"]), 2)
        self.assertTrue(all(w["members"] == 1 for w in plan["withheld"]))
        # a withheld cell publishes its member count but never its status breakdown
        for entry in plan["withheld"]:
            self.assertNotIn("returned", entry)
            self.assertNotIn("signed", entry)
        self.assert_no_internal_keys(plan)

    def test_republish_from_same_snapshot_is_deterministic(self):
        stage(self.store, "base_v1", "late_c3")
        first = publish(self.mapping, self.store, version="V1", route="B")
        store2, mapping2 = connect(":memory:"), connect_mapping(":memory:")
        try:
            stage(store2, "base_v1", "late_c3")
            second = publish(mapping2, store2, version="V1", route="B")
        finally:
            store2.close()
            mapping2.close()
        self.assertEqual(first, second)

    def test_earlier_release_stays_accessible_and_unmodified(self):
        stage(self.store, "base_v1")
        v1 = publish(self.mapping, self.store, version="V1", route="A")
        stage(self.store, "late_c3", "late_c4")
        publish(self.mapping, self.store, version="V2", route="A")
        history = release_history(self.mapping)
        self.assertEqual([h["version"] for h in history], ["V1", "V2"])
        self.assertEqual(history[0]["document"], v1)  # V1 is never withdrawn or edited


if __name__ == "__main__":
    unittest.main()

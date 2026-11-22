import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from preorderproof import __file__ as imported_module
from preorderproof.audit import audit, report
from preorderproof.store import connect, ingest

ROOT = Path(__file__).resolve().parents[1]


def fixture(name):
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


class PreorderProofTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "case.sqlite"
        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def put(self, subsystem, name):
        return ingest(self.conn, subsystem, fixture(name))

    def normal(self):
        self.put("offer", "offer-ok.json")
        self.put("payment", "payment-ok.json")
        self.put("delivery", "delivery-ok.json")
        return report(self.conn)["decisions"]

    def test_imported_module_is_this_repo(self):
        self.assertTrue(Path(imported_module).resolve().is_relative_to(ROOT))

    def test_three_business_tables_have_independent_entities(self):
        self.normal()
        for pair in [("offers", "batches"), ("orders", "payments"), ("parcels", "receipts")]:
            for table in pair:
                self.assertGreater(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_capacity_allocation_creates_waitlist(self):
        self.put("offer", "offer-ok.json")
        self.put("payment", "payment-ok.json")
        result = report(self.conn)["decisions"]
        self.assertTrue(result["order:C1"]["surfaces"]["deposit_reserves_capacity"])
        self.assertEqual(result["order:C2"]["rights_status"], "paid_waitlist")
        self.assertEqual(result["offer:O1"]["unallocated_capacity"], 1)

    def test_partial_receipt_keeps_one_unit_due(self):
        result = self.normal()["order:C1"]
        self.assertEqual((result["delivered_units"], result["outstanding_units"]), (1, 1))
        self.assertTrue(result["surfaces"]["partial_receipt_keeps_obligation"])

    def test_due_change_requires_consumer_consent(self):
        self.normal()
        self.put("offer", "offer-bad.json")
        result = report(self.conn)["decisions"]
        self.assertTrue(result["offer:O1"]["surfaces"]["changed_due_requires_consent"])
        self.assertEqual(result["order:C1"]["rights_status"], "new_due_unaccepted")
        self.assertGreater(result["order:C1"]["refund_due"], 0)

    def test_consent_reopens_delivery_right_without_erasing_history(self):
        self.normal()
        self.put("offer", "offer-bad.json")
        before = report(self.conn)["decisions"]["order:C1"]["status"]
        self.put("payment", "payment-bad.json")
        after = report(self.conn)["decisions"]["order:C1"]
        self.assertEqual(before, "refund_due")
        self.assertEqual(after["status"], "partial")
        self.assertTrue(after["consent_ok"])
        count = self.conn.execute("SELECT COUNT(*) FROM decision_history WHERE subject='order:C1'").fetchone()[0]
        self.assertGreaterEqual(count, 3)

    def test_overpayment_creates_debt_not_extra_stock(self):
        self.normal()
        self.put("offer", "offer-bad.json")
        self.put("payment", "payment-bad.json")
        result = report(self.conn)["decisions"]["order:C1"]
        self.assertEqual(result["overpayment_debt"], 300)
        self.assertEqual(result["ordered_units"], 2)

    def test_late_tail_does_not_gain_dispatch_right(self):
        self.put("offer", "offer-ok.json")
        self.put("payment", "payment-ok.json")
        self.put("payment", "payment-bad.json")
        result = report(self.conn)["decisions"]["order:C2"]
        self.assertFalse(result["surfaces"]["tail_paid_in_window"])
        self.assertTrue(result["surfaces"]["late_tail_releases_right"])

    def test_tail_expiry_releases_capacity_to_eligible_candidate(self):
        self.put("offer", "offer-ok.json")
        payments = [p for p in fixture("payment-ok.json") if p["event_id"] != "pay-t1"]
        payments.append({"event_id": "pay-t2", "event_at": "2026-12-04", "kind": "payment",
                         "txid": "T7", "order_id": "C2", "stage": "tail", "amount": 1800,
                         "paid_at": "2026-12-04"})
        ingest(self.conn, "payment", payments)
        before = report(self.conn)["decisions"]
        self.assertEqual(before["order:C2"]["rights_status"], "paid_waitlist")
        later = {"event_id": "clock-1", "event_at": "2026-12-06", "kind": "payment",
                 "txid": "T8", "order_id": "C2", "stage": "tail", "amount": 1, "paid_at": "2026-12-06"}
        ingest(self.conn, "payment", [later])
        after = report(self.conn)["decisions"]
        self.assertEqual(after["order:C1"]["rights_status"], "tail_expired")
        self.assertTrue(after["order:C2"]["surfaces"]["deposit_reserves_capacity"])

    def test_promise_same_version_conflict_freezes_offer(self):
        self.put("offer", "offer-ok.json")
        self.put("offer", "offer-bad.json")
        result = report(self.conn)["decisions"]["offer:O2"]
        self.assertEqual(result["status"], "promise_version_conflict")
        self.assertEqual(result["capacity"], 0)

    def test_sealed_batch_revokes_unfulfilled_right(self):
        self.normal()
        sealed = {"event_id": "seal-1", "event_at": "2026-12-09", "kind": "batch",
                  "batch_id": "B1", "quantity": 3, "certified_until": "2026-12-31", "status": "sealed"}
        ingest(self.conn, "offer", [sealed])
        result = report(self.conn)["decisions"]["order:C1"]
        self.assertEqual(result["rights_status"], "batch_unavailable")
        self.assertEqual(result["refund_due"], 2000)

    def test_wrong_batch_parcel_does_not_fulfil(self):
        self.normal()
        self.put("offer", "offer-bad.json")
        self.put("payment", "payment-bad.json")
        self.put("delivery", "delivery-bad.json")
        result = report(self.conn)["decisions"]["order:C1"]
        self.assertIn({"parcel_id": "P2", "reason": "wrong_batch"}, result["invalid_parcels"])
        self.assertEqual(result["delivered_units"], 0)

    def test_return_reopens_refund_and_compensation(self):
        self.normal()
        self.put("offer", "offer-bad.json")
        self.put("payment", "payment-bad.json")
        self.put("delivery", "delivery-bad.json")
        result = report(self.conn)["decisions"]["order:C1"]
        self.assertTrue(result["surfaces"]["late_return_reopens_refund"])
        self.assertEqual(result["refund_due"], 2000)
        self.assertEqual(result["compensation_due"], 200)

    def test_independent_return_inspection_releases_sellable_capacity(self):
        self.normal()
        self.put("delivery", "delivery-recovery.json")
        before = report(self.conn)["decisions"]["offer:O1"]
        self.assertEqual(before["recovered_capacity"], 0)
        self.put("offer", "offer-restock.json")
        after = report(self.conn)["decisions"]
        self.assertEqual(after["offer:O1"]["recovered_capacity"], 1)
        self.assertEqual(after["offer:O1"]["unallocated_capacity"], 2)
        self.assertEqual(after["order:C1"]["restocked_units"], 1)
        self.assertEqual(after["order:C1"]["refund_due"], 1000)

    def test_restock_promotes_paid_waitlist_across_all_three_ledgers(self):
        self.put("offer", "offer-ok.json")
        self.put("payment", "payment-ok.json")
        candidate = [
            {"event_id": "order-3", "event_at": "2026-11-06", "kind": "order",
             "order_id": "C3", "offer_id": "O1", "units": 2, "placed_at": "2026-11-06"},
            {"event_id": "pay-d3", "event_at": "2026-11-06", "kind": "payment",
             "txid": "T6", "order_id": "C3", "stage": "deposit", "amount": 200,
             "paid_at": "2026-11-06"},
            {"event_id": "pay-t3", "event_at": "2026-12-04", "kind": "payment",
             "txid": "T7", "order_id": "C3", "stage": "tail", "amount": 1800,
             "paid_at": "2026-12-04"},
        ]
        ingest(self.conn, "payment", candidate)
        self.put("delivery", "delivery-ok.json")
        self.put("delivery", "delivery-recovery.json")
        before = report(self.conn)["decisions"]["order:C3"]
        self.assertEqual(before["rights_status"], "paid_waitlist")
        self.put("offer", "offer-restock.json")
        after = report(self.conn)["decisions"]
        self.assertEqual(after["order:C3"]["rights_status"], "ready_to_dispatch")
        self.assertEqual(after["offer:O1"]["unallocated_capacity"], 0)
        self.assertEqual(after["order:C1"]["refund_due"], 1000)

    def test_unproven_or_late_restock_never_increases_capacity(self):
        self.normal()
        self.put("delivery", "delivery-bad.json")
        self.put("offer", "offer-restock-bad.json")
        result = report(self.conn)["decisions"]["offer:O1"]
        self.assertEqual(result["recovered_capacity"], 0)
        self.assertIn(result["restock_reviews"][0]["status"], {"after_promise_due", "return_not_proven"})

    def test_overcounted_return_cannot_be_restocked(self):
        self.normal()
        bad_return = {"event_id": "over-return", "event_at": "2026-12-09", "kind": "receipt",
                      "receipt_id": "RX", "parcel_id": "P1", "units": 2,
                      "receipt_type": "returned", "received_at": "2026-12-09",
                      "witness": "consumer-1"}
        ingest(self.conn, "delivery", [bad_return])
        review = {"event_id": "over-review", "event_at": "2026-12-09", "kind": "restock",
                  "review_id": "RVX", "receipt_id": "RX", "batch_id": "B1", "units": 2,
                  "inspected_at": "2026-12-09", "outcome": "accepted", "inspector": "independent-qc-1"}
        ingest(self.conn, "offer", [review])
        result = report(self.conn)["decisions"]
        self.assertTrue(result["order:C1"]["receipt_overcount"])
        self.assertEqual(result["offer:O1"]["recovered_capacity"], 0)

    def test_replay_is_idempotent(self):
        first = self.put("offer", "offer-ok.json")
        second = self.put("offer", "offer-ok.json")
        self.assertEqual((first["inserted"], second["inserted"], second["replayed"]), (2, 0, 2))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM decision_history").fetchone()[0], 1)

    def test_collision_rolls_back_whole_import(self):
        self.put("offer", "offer-ok.json")
        changed = copy.deepcopy(fixture("offer-ok.json"))
        changed[1]["capacity"] = 9
        changed.insert(0, {**changed[0], "event_id": "new-good"})
        with self.assertRaisesRegex(ValueError, "collision"):
            ingest(self.conn, "offer", changed)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM source_events").fetchone()[0], 2)

    def test_malformed_source_rolls_back(self):
        changed = fixture("payment-ok.json")
        changed[-1]["paid_at"] = "not a date"
        with self.assertRaisesRegex(ValueError, "ISO date"):
            ingest(self.conn, "payment", changed)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM source_events").fetchone()[0], 0)

    def test_raw_replay_audit_detects_tampering(self):
        self.normal()
        self.assertTrue(audit(self.conn)["ok"])
        self.conn.execute("UPDATE current_decisions SET decision='{}' WHERE subject='order:C1'")
        self.conn.commit()
        verdict = audit(self.conn)
        self.assertFalse(verdict["ok"])
        self.assertIn("order:C1", verdict["changed"])

    def test_cli_import_and_audit_on_fresh_database(self):
        for command, name in [("offer-import", "offer-ok.json"),
                              ("payment-import", "payment-ok.json"),
                              ("delivery-import", "delivery-ok.json")]:
            run = subprocess.run([sys.executable, "-m", "preorderproof", command,
                                  "--db", str(self.db), "--file", str(ROOT / "fixtures" / name)],
                                 cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
        run = subprocess.run([sys.executable, "-m", "preorderproof", "audit", "--db", str(self.db)],
                             cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(json.loads(run.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()

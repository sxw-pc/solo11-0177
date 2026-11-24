import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from preorderproof.audit import audit, report
from preorderproof.store import connect, ingest

ROOT = Path(__file__).resolve().parents[1]


def fixture(name):
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


def buffer_events(tag, samples):
    """Signed freezer buffer samples: (minute, current_a, ambient_c) on 2026-11-20."""
    return [
        {"event_id": f"buf-{tag}-{i}", "event_at": "2026-11-21", "kind": "buffer_sample",
         "buffer_id": "F1-local", "recorder": "F1",
         "observed_at": f"2026-11-20T08:{minute:02d}:00",
         "current_a": current, "ambient_c": ambient, "signature": f"sig-{tag}-{i}"}
        for i, (minute, current, ambient) in enumerate(samples, start=1)
    ]


# Scenario A: the freezer held 4°C through the gap; all three boxes stay deliverable.
SCENARIO_A = [(5, 4.2, 4.0), (10, 4.1, 4.0), (15, 4.3, 4.0)]
# Scenario B: 08:05-08:15 power loss, ambient rose to 20°C; thin packaging exceeds.
SCENARIO_B = [(5, 0.0, 14.0), (10, 0.0, 20.0), (15, 0.0, 20.0)]
# Partial recovery: only 08:05 is covered, so 08:05-08:20 stays unknown and the
# thin boxes remain unprovable (a 15-minute room-temperature window can hurt them).
PARTIAL = [(5, 4.2, 4.0)]


def assay_events(outcomes):
    """Independent activity assays per box: box_id -> (outcome, units_consumed)."""
    return [
        {"event_id": f"as-{i}", "event_at": "2026-11-22", "kind": "activity_assay",
         "assay_id": f"AS-{i}", "batch_id": "B1", "box_id": box_id,
         "units_consumed": consumed, "outcome": outcome,
         "assayed_at": "2026-11-22T10:00:00", "inspector": "independent-qc-1"}
        for i, (box_id, (outcome, consumed)) in enumerate(sorted(outcomes.items()), start=1)
    ]


class BatchTemperatureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "case.sqlite"
        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def put(self, subsystem, events):
        if isinstance(events, str):
            events = fixture(events)
        return ingest(self.conn, subsystem, events)

    def base(self):
        self.put("offer", "offer-ok.json")
        self.put("payment", "payment-ok.json")

    def gap(self):
        self.put("temperature", "batch-cold-gap.json")

    def decisions(self):
        return report(self.conn)["decisions"]

    def test_baseline_allocation_without_temperature_evidence(self):
        self.base()
        result = self.decisions()
        self.assertEqual(result["offer:O1"]["capacity"], 3)
        self.assertIsNone(result["offer:O1"]["temperature"])
        self.assertEqual(result["order:C1"]["rights_status"], "ready_to_dispatch")
        self.assertEqual(result["order:C2"]["rights_status"], "paid_waitlist")
        self.assertEqual(result["offer:O1"]["unallocated_capacity"], 1)
        self.assertNotIn("temperature:B1", result)

    def test_gap_log_defers_allocation_without_refund(self):
        self.base()
        self.gap()
        result = self.decisions()
        temp = result["temperature:B1"]
        self.assertEqual(temp["status"], "pending_evidence")
        self.assertEqual(temp["resolution"], "ambient_range")
        # The thick box is provably safe under every compatible scenario; the two
        # thin boxes may or may not be damaged, so the range keeps [1, 3].
        self.assertEqual(temp["proven_ok_units"], 1)
        self.assertEqual(temp["suspect_units"], 2)
        self.assertEqual(temp["pending_retest_units"], 0)
        self.assertEqual(temp["deliverable_units"], 1)
        self.assertEqual(temp["deliverable_range"], [1, 3])
        self.assertEqual(temp["declared"],
                         {"quantity": 3, "status": "good", "certified_until": "2026-12-31"})
        self.assertEqual(temp["gaps"], [{"recorder": "F1", "start": "2026-11-20T08:00:00",
                                         "end": "2026-11-20T08:20:00", "covered": False}])
        boxes = {b["box_id"]: b for b in temp["boxes"]}
        self.assertEqual(boxes["BOX1"]["verdict"], "suspect")
        self.assertEqual(boxes["BOX2"]["verdict"], "suspect")
        self.assertEqual(boxes["BOX3"]["verdict"], "proven_ok")
        self.assertEqual(boxes["BOX1"]["best_over_minutes"], 0.0)
        self.assertIsNone(boxes["BOX1"]["worst_over_minutes"])
        self.assertTrue(boxes["BOX1"]["open_exceedance"])
        self.assertEqual(boxes["BOX3"]["worst_over_minutes"], 0.0)
        self.assertEqual(result["offer:O1"]["capacity"], 1)
        self.assertEqual(result["offer:O1"]["status"], "available")
        for order in ("order:C1", "order:C2"):
            self.assertEqual(result[order]["rights_status"], "cold_chain_pending")
            self.assertTrue(result[order]["surfaces"]["cold_chain_pending"])
            self.assertEqual(result[order]["refund_due"], 0)
        lot = self.conn.execute("SELECT status, quantity FROM batches WHERE batch_id='B1'").fetchone()
        self.assertEqual(tuple(lot), ("good", 3))

    def test_signed_buffer_scenario_a_restores_full_capacity(self):
        self.base()
        self.gap()
        self.put("temperature", buffer_events("a", SCENARIO_A))
        result = self.decisions()
        temp = result["temperature:B1"]
        self.assertEqual(temp["status"], "resolved")
        self.assertEqual(temp["resolution"], "freezer_buffer")
        self.assertEqual(temp["deliverable_units"], 3)
        self.assertEqual(temp["proven_ok_units"], 3)
        self.assertEqual(temp["outage_intervals"], [])
        self.assertEqual(temp["liability_evidence"], "none")
        self.assertEqual([b["verdict"] for b in temp["boxes"]],
                         ["proven_ok", "proven_ok", "proven_ok"])
        self.assertEqual(result["offer:O1"]["capacity"], 3)
        self.assertEqual(result["order:C1"]["rights_status"], "ready_to_dispatch")
        self.assertEqual(result["order:C2"]["rights_status"], "paid_waitlist")
        self.assertEqual(result["offer:O1"]["unallocated_capacity"], 1)

    def test_signed_buffer_scenario_b_leaves_single_deliverable(self):
        self.base()
        self.gap()
        self.put("temperature", buffer_events("b", SCENARIO_B))
        result = self.decisions()
        temp = result["temperature:B1"]
        self.assertEqual(temp["status"], "resolved")
        self.assertEqual(temp["deliverable_units"], 1)
        self.assertEqual(temp["proven_ok_units"], 1)
        self.assertEqual(temp["proven_failed_units"], 2)
        self.assertEqual(temp["outage_intervals"], [{"recorder": "F1",
                                                     "start": "2026-11-20T08:05:00",
                                                     "end": "2026-11-20T08:15:00"}])
        self.assertEqual(temp["liability_evidence"], "freezer_buffer")
        boxes = {b["box_id"]: b for b in temp["boxes"]}
        self.assertEqual(boxes["BOX1"]["verdict"], "failed")
        self.assertEqual(boxes["BOX2"]["verdict"], "failed")
        self.assertEqual(boxes["BOX3"]["verdict"], "proven_ok")
        self.assertGreater(boxes["BOX1"]["over_minutes"], 5)
        self.assertLess(boxes["BOX3"]["over_minutes"], 5)
        # One shared freezer outage constrains identical packaging identically;
        # only the thicker insulation of BOX3 separates it from the thin boxes.
        self.assertEqual(boxes["BOX1"]["over_minutes"], boxes["BOX2"]["over_minutes"])
        self.assertNotEqual(boxes["BOX1"]["over_minutes"], boxes["BOX3"]["over_minutes"])
        self.assertEqual(result["offer:O1"]["capacity"], 1)
        self.assertEqual(result["order:C1"]["rights_status"], "paid_waitlist")
        self.assertEqual(result["order:C1"]["refund_due"], 2000)
        self.assertEqual(result["order:C2"]["rights_status"], "paid_waitlist")
        self.assertEqual(result["order:C2"]["refund_due"], 200)

    def test_partial_buffer_coverage_stays_pending(self):
        self.base()
        self.gap()
        self.put("temperature", buffer_events("p", PARTIAL))
        result = self.decisions()
        temp = result["temperature:B1"]
        self.assertEqual(temp["status"], "pending_evidence")
        self.assertEqual(temp["deliverable_units"], 1)
        self.assertEqual(temp["suspect_units"], 2)
        self.assertEqual(temp["gaps"], [{"recorder": "F1", "start": "2026-11-20T08:00:00",
                                         "end": "2026-11-20T08:20:00", "covered": False}])
        self.assertEqual(result["offer:O1"]["capacity"], 1)
        self.assertEqual(result["order:C1"]["rights_status"], "cold_chain_pending")
        self.assertEqual(result["order:C1"]["refund_due"], 0)

    def test_activity_sampling_decides_shippable_without_liability(self):
        self.base()
        self.gap()
        self.put("temperature", assay_events(
            {"BOX1": ("failed", 0), "BOX2": ("failed", 0), "BOX3": ("effective", 0)}))
        result = self.decisions()
        temp = result["temperature:B1"]
        self.assertEqual(temp["status"], "resolved")
        self.assertEqual(temp["resolution"], "activity_sampling")
        self.assertEqual(temp["deliverable_units"], 1)
        self.assertEqual(temp["assay_effective_units"], 1)
        self.assertEqual(temp["assay_failed_units"], 2)
        self.assertEqual(temp["outage_intervals"], [])
        self.assertEqual(temp["liability_evidence"], "none")
        boxes = {b["box_id"]: b for b in temp["boxes"]}
        self.assertEqual(boxes["BOX1"]["verdict"], "assay_failed")
        self.assertEqual(boxes["BOX3"]["verdict"], "assay_effective")
        self.assertEqual(result["offer:O1"]["capacity"], 1)
        self.assertEqual(result["order:C1"]["rights_status"], "paid_waitlist")
        self.assertEqual(result["order:C1"]["refund_due"], 2000)

    def test_sampling_consumption_deducts_deliverable(self):
        self.base()
        self.gap()
        self.put("temperature", assay_events(
            {"BOX1": ("effective", 0), "BOX2": ("effective", 0), "BOX3": ("effective", 1)}))
        result = self.decisions()
        temp = result["temperature:B1"]
        self.assertEqual(temp["deliverable_units"], 2)
        self.assertEqual(temp["consumed_units"], 1)
        self.assertEqual(result["offer:O1"]["capacity"], 2)
        self.assertEqual(result["order:C1"]["rights_status"], "ready_to_dispatch")
        self.assertEqual(result["order:C2"]["rights_status"], "paid_waitlist")
        self.assertEqual(result["offer:O1"]["unallocated_capacity"], 0)

    def test_sampling_all_effective_restores_three(self):
        self.base()
        self.gap()
        self.put("temperature", assay_events(
            {"BOX1": ("effective", 0), "BOX2": ("effective", 0), "BOX3": ("effective", 0)}))
        result = self.decisions()
        self.assertEqual(result["temperature:B1"]["deliverable_units"], 3)
        self.assertEqual(result["offer:O1"]["capacity"], 3)
        self.assertEqual(result["order:C1"]["rights_status"], "ready_to_dispatch")
        self.assertEqual(result["order:C2"]["rights_status"], "paid_waitlist")
        self.assertEqual(result["offer:O1"]["unallocated_capacity"], 1)

    def test_page_wraparound_and_forged_page_conflict(self):
        self.base()
        self.gap()
        temp = self.decisions()["temperature:B1"]
        # Boot-1 page numbers repeat boot-0 page numbers after the offline
        # restart; the assessment must not read that wrap as a conflict.
        self.assertEqual(temp["status"], "pending_evidence")
        self.assertEqual(len(temp["gaps"]), 1)
        forged = {"event_id": "tp-forge", "event_at": "2026-11-21", "kind": "temp_page",
                  "page_id": "F1-b1-p1b", "recorder": "F1", "boot": 1, "page_no": 1,
                  "observed_at": "2026-11-20T08:20:00", "ambient_c": 15.0}
        self.put("temperature", [forged])
        result = self.decisions()
        self.assertEqual(result["temperature:B1"]["status"], "evidence_conflict")
        self.assertTrue(any(f.startswith("page_conflict")
                            for f in result["temperature:B1"]["flags"]))
        self.assertEqual(result["offer:O1"]["capacity"], 0)
        self.assertEqual(result["order:C1"]["rights_status"], "cold_chain_pending")
        self.assertEqual(result["order:C1"]["refund_due"], 0)

    def test_freezer_relocation_cannot_dodge_shared_outage(self):
        self.base()
        self.gap()
        # A later mapping plus clean pages on another freezer must not clear BOX1:
        # contradictory placements taint the evidence instead of qualifying the box.
        cheat = [
            {"event_id": "sm-1b", "event_at": "2026-11-21", "kind": "slot_mapping",
             "mapping_id": "SM-1B", "box_id": "BOX1", "recorder": "F9", "slot": "Z1",
             "mapped_at": "2026-11-21T10:00:00"},
            {"event_id": "tp-91", "event_at": "2026-11-21", "kind": "temp_page",
             "page_id": "F9-b0-p1", "recorder": "F9", "boot": 0, "page_no": 1,
             "observed_at": "2026-11-20T08:00:00", "ambient_c": 4.0},
            {"event_id": "tp-92", "event_at": "2026-11-21", "kind": "temp_page",
             "page_id": "F9-b0-p2", "recorder": "F9", "boot": 0, "page_no": 2,
             "observed_at": "2026-11-20T08:20:00", "ambient_c": 4.0},
        ]
        self.put("temperature", cheat)
        result = self.decisions()
        temp = result["temperature:B1"]
        self.assertEqual(temp["status"], "evidence_conflict")
        self.assertTrue(any(f.startswith("mapping_relocation") for f in temp["flags"]))
        self.assertEqual(temp["deliverable_units"], 1)
        self.assertEqual(result["offer:O1"]["capacity"], 1)
        self.assertEqual(result["order:C1"]["rights_status"], "cold_chain_pending")
        self.assertEqual(result["order:C1"]["refund_due"], 0)

    def test_clock_calibration_corrects_recorder_clock(self):
        self.put("offer", [{"event_id": "lot-9", "event_at": "2026-11-01", "kind": "batch",
                            "batch_id": "B9", "quantity": 1, "certified_until": "2026-12-31",
                            "status": "good"}])
        events = [
            {"event_id": "cc-f2", "event_at": "2026-11-20", "kind": "clock_calibration",
             "calibration_id": "CC-F2", "recorder": "F2",
             "calibrated_at": "2026-11-20T07:00:00", "offset_seconds": 300},
            {"event_id": "tc-9", "event_at": "2026-11-15", "kind": "thermal_calibration",
             "calibration_id": "CAL-9", "packaging": "pouch", "tau_minutes": 8.0,
             "calibrated_at": "2026-11-15T10:00:00", "lab": "thermal-lab-1"},
            {"event_id": "sp-9", "event_at": "2026-11-19", "kind": "sensor_placement",
             "placement_id": "SP-9", "box_id": "BOX9", "sensor": "S9", "position": "shell",
             "positioned_at": "2026-11-19T09:00:00"},
            {"event_id": "sm-9", "event_at": "2026-11-19", "kind": "slot_mapping",
             "mapping_id": "SM-9", "box_id": "BOX9", "recorder": "F2", "slot": "B1",
             "mapped_at": "2026-11-19T09:00:00"},
            {"event_id": "ec-9", "event_at": "2026-11-19", "kind": "exposure_certificate",
             "certificate_id": "EC-9", "batch_id": "B9", "box_id": "BOX9", "units": 1,
             "packaging": "pouch", "core_limit_c": 8.0, "max_over_minutes": 5,
             "issuer": "warehouse-qc-1"},
            {"event_id": "tp-91", "event_at": "2026-11-20", "kind": "temp_page",
             "page_id": "F2-b0-p1", "recorder": "F2", "boot": 0, "page_no": 1,
             "observed_at": "2026-11-20T08:05:00", "ambient_c": 4.0},
            {"event_id": "tp-92", "event_at": "2026-11-20", "kind": "temp_page",
             "page_id": "F2-b0-p2", "recorder": "F2", "boot": 0, "page_no": 2,
             "observed_at": "2026-11-20T08:25:00", "ambient_c": 4.0},
            {"event_id": "buf-91", "event_at": "2026-11-21", "kind": "buffer_sample",
             "buffer_id": "F2-local", "recorder": "F2", "observed_at": "2026-11-20T08:10:00",
             "current_a": 0.0, "ambient_c": 20.0, "signature": "sig-91"},
            {"event_id": "buf-92", "event_at": "2026-11-21", "kind": "buffer_sample",
             "buffer_id": "F2-local", "recorder": "F2", "observed_at": "2026-11-20T08:15:00",
             "current_a": 0.0, "ambient_c": 20.0, "signature": "sig-92"},
            {"event_id": "buf-93", "event_at": "2026-11-21", "kind": "buffer_sample",
             "buffer_id": "F2-local", "recorder": "F2", "observed_at": "2026-11-20T08:20:00",
             "current_a": 0.0, "ambient_c": 20.0, "signature": "sig-93"},
        ]
        self.put("temperature", events)
        temp = self.decisions()["temperature:B9"]
        # The recorder ran five minutes fast; liability belongs to true time.
        self.assertEqual(temp["outage_intervals"], [{"recorder": "F2",
                                                     "start": "2026-11-20T08:05:00",
                                                     "end": "2026-11-20T08:15:00"}])
        self.assertEqual(temp["status"], "resolved")
        self.assertEqual(temp["deliverable_units"], 0)
        self.assertEqual(temp["boxes"][0]["verdict"], "failed")

    def test_shipped_parcels_do_not_regress(self):
        self.base()
        parcel = {"event_id": "parcel-9", "event_at": "2026-12-05", "kind": "parcel",
                  "parcel_id": "P9", "order_id": "C1", "batch_id": "B1", "units": 1,
                  "dispatched_at": "2026-12-05", "carrier": "carrier-1"}
        receipt = {"event_id": "receipt-9", "event_at": "2026-12-08", "kind": "receipt",
                   "receipt_id": "R9", "parcel_id": "P9", "units": 1,
                   "receipt_type": "signed", "received_at": "2026-12-08", "witness": "consumer-1"}
        self.put("delivery", [parcel, receipt])
        before = self.decisions()["order:C1"]
        self.assertEqual(before["delivered_units"], 1)
        self.gap()
        pending = self.decisions()["order:C1"]
        self.assertEqual(pending["rights_status"], "cold_chain_pending")
        self.assertEqual(pending["delivered_units"], 1)
        self.assertEqual(pending["refund_due"], 0)
        self.put("temperature", buffer_events("b", SCENARIO_B))
        result = self.decisions()
        self.assertEqual(result["offer:O1"]["capacity"], 1)
        c1 = result["order:C1"]
        self.assertEqual(c1["rights_status"], "paid_waitlist")
        self.assertEqual(c1["delivered_units"], 1)
        self.assertEqual(c1["valid_parcels"], ["P9"])
        self.assertEqual(c1["outstanding_units"], 1)
        self.assertEqual(c1["refund_due"], 1000)
        self.assertEqual(result["order:C2"]["refund_due"], 200)

    def test_eligibility_versions_and_audit(self):
        self.base()
        self.gap()
        self.put("temperature", buffer_events("b", SCENARIO_B))
        rep = report(self.conn)
        temp_hist = [h for h in rep["history"] if h["subject"] == "temperature:B1"]
        self.assertGreaterEqual(len(temp_hist), 2)
        self.assertEqual(temp_hist[0]["decision"]["status"], "pending_evidence")
        self.assertEqual(temp_hist[-1]["decision"]["status"], "resolved")
        self.assertEqual(temp_hist[-1]["decision"]["deliverable_units"], 1)
        offer_hist = [h for h in rep["history"] if h["subject"] == "offer:O1"]
        self.assertEqual([h["decision"]["capacity"] for h in offer_hist], [3, 3, 1, 1])
        c1_hist = [h for h in rep["history"] if h["subject"] == "order:C1"]
        self.assertEqual([h["decision"]["rights_status"] for h in c1_hist],
                         ["ready_to_dispatch", "cold_chain_pending", "paid_waitlist"])
        lot = self.conn.execute("SELECT status, quantity FROM batches WHERE batch_id='B1'").fetchone()
        self.assertEqual(tuple(lot), ("good", 3))
        pages = self.conn.execute(
            "SELECT COUNT(*) FROM source_events WHERE kind='temp_page'").fetchone()[0]
        self.assertEqual(pages, 8)
        self.assertTrue(audit(self.conn)["ok"])

    def test_missing_thermal_calibration_marks_pending_retest(self):
        self.base()
        events = [e for e in fixture("batch-cold-gap.json") if e["kind"] != "thermal_calibration"]
        self.put("temperature", events)
        result = self.decisions()
        temp = result["temperature:B1"]
        self.assertEqual(temp["status"], "pending_evidence")
        self.assertEqual(temp["pending_retest_units"], 3)
        self.assertEqual(temp["suspect_units"], 0)
        self.assertEqual([b["verdict"] for b in temp["boxes"]],
                         ["pending_retest", "pending_retest", "pending_retest"])
        self.assertEqual({b["detail"] for b in temp["boxes"]}, {"thermal_calibration_missing"})
        self.assertEqual(result["order:C1"]["rights_status"], "cold_chain_pending")
        self.assertEqual(result["order:C1"]["refund_due"], 0)

    def test_assay_requires_independent_inspector(self):
        self.base()
        self.gap()
        bad = assay_events({"BOX1": ("effective", 0)})
        bad[0]["inspector"] = "merchant"
        with self.assertRaisesRegex(ValueError, "independent"):
            self.put("temperature", bad)

    def test_cli_temperature_import(self):
        for command, name in [("offer-import", "offer-ok.json"),
                              ("payment-import", "payment-ok.json"),
                              ("temperature-import", "batch-cold-gap.json")]:
            run = subprocess.run([sys.executable, "-m", "preorderproof", command,
                                  "--db", str(self.db), "--file", str(ROOT / "fixtures" / name)],
                                 cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
        run = subprocess.run([sys.executable, "-m", "preorderproof", "report",
                              "--db", str(self.db)], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        decisions = json.loads(run.stdout)["decisions"]
        self.assertEqual(decisions["temperature:B1"]["status"], "pending_evidence")
        self.assertEqual(decisions["order:C1"]["rights_status"], "cold_chain_pending")


if __name__ == "__main__":
    unittest.main()

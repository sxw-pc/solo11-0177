import copy
import json
import tempfile
import unittest
from pathlib import Path

from preorderproof.audit import report as business_report
from preorderproof.campaign import (ESTIMAND_EXPOSED, ESTIMAND_ITT,
                                    InsufficientEvidence, build_report,
                                    derive_assignment, report_history)
from preorderproof.store import connect, digest, ingest

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = json.loads((ROOT / "fixtures" / "campaign-cohort-pages.json").read_text(encoding="utf-8"))


def business_fixture(name):
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


class CampaignLiftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "case.sqlite")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def put(self, group):
        return ingest(self.conn, "campaign", CAMPAIGN[group])

    def base(self, roster=True):
        self.put("plan")
        if roster:
            self.put("roster")
        self.put("exposure")
        self.put("orders")

    def history(self, estimand):
        return [h for h in report_history(self.conn, "exp-1")
                if h["subject"] == f"exp-1:{estimand}"]

    def test_itt_report_shows_reversal_with_locked_weights(self):
        self.base()
        rep = build_report(self.conn, "exp-1", ESTIMAND_ITT)
        naive = rep["naive_aggregate"]
        self.assertEqual(naive["aggregate_conversion_output"],
                         {"control": 0.26, "treatment": 0.64})
        self.assertEqual(naive["lift"], 0.38)
        visible = rep["visible_conversions"]
        self.assertEqual(visible["arm_totals"]["control"],
                         {"visitors": 100, "buyers": 26, "rate": 0.26})
        self.assertEqual(visible["arm_totals"]["treatment"],
                         {"visitors": 100, "buyers": 64, "rate": 0.64})
        self.assertEqual(visible["strata"]["high"]["lift"], -0.1)
        self.assertEqual(visible["strata"]["low"]["lift"], -0.1)
        std = visible["standardized"]
        self.assertEqual((std["control"], std["treatment"], std["lift"]), (0.5, 0.4, -0.1))
        self.assertEqual(rep["estimand"], "intention_to_treat")
        self.assertTrue(rep["causal_claim_allowed"])
        self.assertEqual(rep["denominator_scope"], "all_assigned_households")
        primary = rep["primary"]
        self.assertEqual(primary["arm_totals"]["control"]["visitors"], 120)
        self.assertEqual(primary["arm_totals"]["treatment"]["visitors"], 120)
        self.assertEqual(rep["unexposed_assigned"], {"control": 20, "treatment": 20})
        self.assertEqual(primary["standardized"]["lift"], 0.05)
        self.assertEqual(rep["weights"], {"high": 0.5, "low": 0.5})
        self.assertEqual(rep["weights_source"], "locked_plan")
        self.assertLess(rep["plan_locked_at"], rep["stats_cutoff"])
        self.assertTrue(rep["decision_scope"]["marketing_review_only"])
        self.assertEqual(len(self.history(ESTIMAND_ITT)), 1)
        self.assertEqual(len(self.history(ESTIMAND_EXPOSED)), 1)

    def test_exposed_only_report_is_associational_without_roster(self):
        self.base(roster=False)
        rep = build_report(self.conn, "exp-1", ESTIMAND_EXPOSED)
        self.assertEqual(rep["estimand"], "exposed_conditional_association")
        self.assertFalse(rep["causal_claim_allowed"])
        self.assertEqual(rep["denominator_scope"], "exposed_households_only")
        self.assertIn("不能称为随机因果增量", rep["estimand_statement"])
        self.assertIn("伪造名册", rep["estimand_statement"])
        self.assertEqual(rep["primary"]["arm_totals"]["control"]["visitors"], 100)
        self.assertEqual(rep["primary"]["arm_totals"]["treatment"]["visitors"], 100)
        self.assertEqual(rep["primary"]["standardized"]["lift"], -0.1)
        self.assertEqual(rep["naive_aggregate"]["lift"], 0.38)
        with self.assertRaises(InsufficientEvidence):
            build_report(self.conn, "exp-1", ESTIMAND_ITT)
        self.assertEqual(self.history(ESTIMAND_ITT), [])
        self.assertEqual(len(self.history(ESTIMAND_EXPOSED)), 1)

    def test_household_two_devices_lands_in_single_stratum(self):
        self.base()
        rep = build_report(self.conn, "exp-1", ESTIMAND_EXPOSED)
        totals = rep["primary"]["arm_totals"]
        self.assertEqual(totals["control"]["visitors"], 100)
        self.assertEqual(totals["treatment"]["visitors"], 100)
        conflicts = rep["household_conflicts"]
        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict["resolved_by"], "assignment_root")
        derived = derive_assignment("exp-1-root", conflict["household"])
        self.assertEqual(derived, ("treatment", "high"))
        self.assertEqual((conflict["resolved"]["arm"], conflict["resolved"]["cohort"]), derived)
        self.assertEqual({(o["cursor"], o["cohort"]) for o in conflict["observed"]},
                         {("exp-th-1", "high"), ("exp-tl-1", "low")})
        strata = rep["primary"]["strata"]
        self.assertEqual(strata["high"]["treatment"]["visitors"], 90)
        self.assertEqual(strata["low"]["treatment"]["visitors"], 10)
        multi = rep["reconciliation"]["multi_device_households"]
        self.assertEqual(len(multi), 2)
        self.assertTrue(all(len(m["devices"]) == 2 for m in multi))
        diag = rep["diagnostics"]["page_sum_naive"]
        self.assertEqual(diag["visitors_by_page_sum"]["treatment"],
                         {"high": 91, "low": 11})
        self.assertAlmostEqual(diag["standardized_lift"], -0.1083916, places=4)
        self.assertAlmostEqual(diag["se"], 0.083114, places=4)
        std = rep["primary"]["standardized"]
        self.assertAlmostEqual(std["se"], 0.0853105, places=4)
        self.assertNotAlmostEqual(diag["se"], std["se"], places=6)
        self.assertAlmostEqual(std["ci95"][0], -0.2672085, places=4)
        self.assertAlmostEqual(std["ci95"][1], 0.0672085, places=4)

    def test_late_page_recomputes_strata_and_links_correction(self):
        self.base()
        self.assertEqual(len(self.history(ESTIMAND_EXPOSED)), 1)
        self.put("late_exposure")
        exposed = self.history(ESTIMAND_EXPOSED)
        self.assertEqual(len(exposed), 2)
        rev1, rev2 = exposed
        self.assertEqual(rev2["supersedes"], rev1["report_hash"])
        self.assertEqual(rev1["report"]["primary"]["standardized"]["lift"], -0.1)
        self.assertEqual(rev1["report"]["naive_aggregate"]["lift"], 0.38)
        low1 = rev1["report"]["primary"]["strata"]["low"]
        low2 = rev2["report"]["primary"]["strata"]["low"]
        self.assertEqual(low1["control"]["visitors"], 90)
        self.assertEqual(low2["control"]["visitors"], 95)
        self.assertEqual(rev2["report"]["primary"]["strata"]["high"],
                         rev1["report"]["primary"]["strata"]["high"])
        self.assertAlmostEqual(rev2["report"]["primary"]["standardized"]["lift"],
                               -0.0947368, places=4)
        self.assertAlmostEqual(rev2["report"]["naive_aggregate"]["lift"],
                               0.3923810, places=4)
        itt = self.history(ESTIMAND_ITT)
        self.assertEqual(len(itt), 2)
        self.assertEqual(itt[1]["supersedes"], itt[0]["report_hash"])
        self.assertEqual(itt[0]["report"]["primary"], itt[1]["report"]["primary"])
        self.assertEqual(
            itt[1]["report"]["visible_conversions"]["strata"]["low"]["control"]["visitors"], 95)

    def test_cancel_and_reissue_correct_page_without_rewriting_history(self):
        self.base()
        self.put("reissue_low_page")
        exposed = self.history(ESTIMAND_EXPOSED)
        self.assertEqual(len(exposed), 2)
        old, new = exposed
        self.assertEqual(new["supersedes"], old["report_hash"])
        self.assertEqual(len(old["report"]["household_conflicts"]), 1)
        self.assertEqual(new["report"]["household_conflicts"], [])
        self.assertEqual(new["report"]["primary"]["standardized"]["lift"], -0.1)
        cursors = {p["cursor"] for p in new["report"]["evidence"]["pages"]}
        self.assertIn("exp-tl-2", cursors)
        self.assertNotIn("exp-tl-1", cursors)
        self.assertIn("exp-tl-1", new["report"]["evidence"]["cancelled_cursors"])
        old_cursors = {p["cursor"] for p in old["report"]["evidence"]["pages"]}
        self.assertIn("exp-tl-1", old_cursors)

    def test_replay_dedup_and_conflicting_or_tampered_pages_rejected(self):
        self.base()
        again = self.put("exposure")
        self.assertEqual((again["inserted"], again["replayed"]), (0, 4))
        self.assertEqual(len(report_history(self.conn, "exp-1")), 2)
        events_before = self.conn.execute("SELECT COUNT(*) FROM source_events").fetchone()[0]
        original = next(p for p in CAMPAIGN["exposure"] if p["cursor"] == "exp-tl-1")
        conflict = copy.deepcopy(original)
        conflict["event_id"] = "exp-tl-1-copy"
        conflict["entries"] = conflict["entries"][:5]
        conflict["signature"] = digest({k: v for k, v in conflict.items() if k != "signature"})
        with self.assertRaisesRegex(ValueError, "cursor"):
            ingest(self.conn, "campaign", [conflict])
        tampered = copy.deepcopy(CAMPAIGN["exposure"][0])
        tampered["event_id"] = "exp-ch-1-tampered"
        tampered["entries"] = tampered["entries"] + [tampered["entries"][0]]
        with self.assertRaisesRegex(ValueError, "signature"):
            ingest(self.conn, "campaign", [tampered])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM source_events").fetchone()[0],
                         events_before)
        self.assertEqual(len(report_history(self.conn, "exp-1")), 2)

    def test_plan_must_be_locked_before_cutoff_with_valid_weights(self):
        def plan_with(**changes):
            event = copy.deepcopy(CAMPAIGN["plan"][0])
            event.update(changes)
            event["signature"] = digest({k: v for k, v in event.items() if k != "signature"})
            return event

        with self.assertRaisesRegex(ValueError, "locked"):
            ingest(self.conn, "campaign", [plan_with(locked_at="2026-11-21")])
        with self.assertRaisesRegex(ValueError, "sum to 1"):
            ingest(self.conn, "campaign", [plan_with(cohort_weights={"high": 0.9, "low": 0.2})])
        with self.assertRaisesRegex(ValueError, "sampling_unit"):
            ingest(self.conn, "campaign", [plan_with(sampling_unit="device")])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM source_events").fetchone()[0], 0)

    def test_reconciliation_lists_platform_first_party_differences(self):
        self.base()
        rep = build_report(self.conn, "exp-1", ESTIMAND_EXPOSED)
        recon = rep["reconciliation"]
        self.assertEqual(recon["first_party_orders"], 91)
        self.assertEqual(recon["experiment_buyers"], 90)
        self.assertEqual(recon["platform_claimed_buyers"], 90)
        self.assertTrue(recon["buyer_totals_compatible"])
        self.assertEqual(len(recon["out_of_scope_orders"]), 1)
        self.assertEqual(recon["out_of_scope_orders"][0]["order_ids"], ["ORD-0091"])
        types = {d["type"] for d in recon["discrepancies"]}
        self.assertIn("order_outside_experiment", types)
        self.assertIn("household_stratum_conflict", types)
        self.assertIn("multi_device_household", types)
        self.assertIn("page_entries_exceed_unique_households", types)

    def test_campaign_evidence_never_changes_business_decisions(self):
        for subsystem, name in [("offer", "offer-ok.json"), ("payment", "payment-ok.json"),
                                ("delivery", "delivery-ok.json")]:
            ingest(self.conn, subsystem, business_fixture(name))
        before = business_report(self.conn)["decisions"]
        capacity_before = before["offer:O1"]["capacity"]
        self.base()
        after = business_report(self.conn)["decisions"]
        self.assertEqual(before, after)
        self.assertEqual(after["offer:O1"]["capacity"], capacity_before)
        self.assertTrue(all(k.startswith(("offer:", "order:")) for k in after))


if __name__ == "__main__":
    unittest.main()

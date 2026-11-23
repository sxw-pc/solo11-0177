"""Campaign lift evidence: stratified, reconciled, estimand-labelled reports.

The platform export only aggregates visitors/buyers per arm; when high-intent
households are over-assigned to treatment the naive aggregate flips sign. This
module rebuilds the effect from signed platform pages and the locked
stratification plan:

* weights come from the pre-registered plan, never from observed arm mix;
* the assignment root declared in the plan fixes each household's unique
  stratum, so one household exposed on two devices is never two subjects;
* with a platform-signed assignment roster the report is intention-to-treat;
  with signed exposure logs only it is an exposed-conditional association
  that must not be called a random causal increment;
* reports serve marketing review only and never touch offer capacity,
  consumer rights, or refund settlement.
"""
from __future__ import annotations

import hashlib
import json
import math
from fractions import Fraction

from .store import canonical, digest

ESTIMAND_ITT = "intention_to_treat"
ESTIMAND_EXPOSED = "exposed_conditional_association"
ESTIMANDS = (ESTIMAND_ITT, ESTIMAND_EXPOSED)
PAGE_KINDS = {"assignment_page", "exposure_page", "order_page"}
_NON_PAYLOAD_FIELDS = ("event_id", "event_at", "signature")

_STATEMENTS = {
    ESTIMAND_ITT: (
        "意向处理效果（ITT）：按实验前锁定的目标人群权重标准化，分母为全部经分配根随机分配的"
        "家庭，未看到广告者仍在分母；平台签名名册与分配根一致，可作随机因果解释。"),
    ESTIMAND_EXPOSED: (
        "已曝光条件关联效果：同一预注册目标人群权重下仅比较被曝光家庭；平台未提供签名分配名册，"
        "不能称为随机因果增量，未曝光家庭不在分母，也不得用曝光或点击页伪造名册补进分母。"),
}
_DENOMINATOR_NOTES = {
    ESTIMAND_ITT: "分母覆盖所有被分配家庭（含未曝光者）。",
    ESTIMAND_EXPOSED: "分母仅含被曝光家庭。",
}
_APPLICABLE_DECISIONS = {
    ESTIMAND_ITT: ["继续或停止该定向投放", "增减投放预算", "复盘素材与触达质量"],
    ESTIMAND_EXPOSED: ["复盘素材与触达质量"],
}


class InsufficientEvidence(Exception):
    """Signed evidence required for this estimand has not arrived yet."""


def aggregate_conversion(rows):
    totals = {}
    for row in rows:
        arm = row["arm"]
        slot = totals.setdefault(arm, {"visitors": 0, "buyers": 0})
        slot["visitors"] += row["visitors"]
        slot["buyers"] += row["buyers"]
    return {arm: item["buyers"] / item["visitors"] for arm, item in totals.items()}


def derive_assignment(root, household, arms=("control", "treatment"), cohorts=("high", "low")):
    """Assignment root: every household hashes to exactly one arm and cohort."""
    raw = hashlib.sha256(f"{root}|{household}".encode("utf-8")).digest()
    return sorted(arms)[raw[0] % len(arms)], sorted(cohorts)[raw[1] % len(cohorts)]


def _campaign_events(conn, experiment_id):
    events = []
    for row in conn.execute(
            "SELECT body FROM source_events WHERE subsystem='campaign' ORDER BY event_at, event_id"):
        body = json.loads(row["body"])
        if body.get("experiment_id") == experiment_id:
            events.append(body)
    return events


def _payload_key(event):
    return canonical({k: v for k, v in event.items() if k not in _NON_PAYLOAD_FIELDS})


def _locked_plan(events):
    plans = [e for e in events if e["kind"] == "campaign_plan"]
    if not plans:
        raise InsufficientEvidence("locked experiment plan has not arrived")
    if len({_payload_key(p) for p in plans}) > 1:
        raise ValueError("conflicting locked plans for one experiment")
    return plans[-1]


def _active_pages(events):
    """Apply cancel/reissue pages and deduplicate data pages by cursor."""
    groups, cancels, reissues = {}, {}, {}
    for event in events:
        kind = event["kind"]
        if kind in PAGE_KINDS:
            groups.setdefault(event["cursor"], []).append(event)
        elif kind == "page_cancel":
            cancels[event["target_cursor"]] = event
        elif kind == "page_reissue":
            reissues.setdefault(event["cursor"], []).append(event)
    cancelled = set(cancels) | {group[0]["target_cursor"] for group in reissues.values()}
    anomalies = []
    pages = {}
    for cursor, group in groups.items():
        if cursor in cancelled:
            continue
        if len({_payload_key(e) for e in group}) > 1:
            raise ValueError(f"page cursor conflict: {cursor}")
        pages[cursor] = group[0]
    for cursor, group in reissues.items():
        if cursor in cancelled:
            continue
        if len({_payload_key(e) for e in group}) > 1 or cursor in pages:
            raise ValueError(f"page cursor conflict: {cursor}")
        reissue = group[0]
        if reissue["target_cursor"] not in groups:
            anomalies.append({"type": "orphan_reissue", "cursor": cursor,
                              "target_cursor": reissue["target_cursor"]})
        pages[cursor] = {
            "kind": reissue["page_kind"], "cursor": cursor,
            "experiment_id": reissue["experiment_id"], "arm": reissue["arm"],
            "cohort": reissue["cohort"], "stats_cutoff": reissue["stats_cutoff"],
            "sampling_unit": reissue["sampling_unit"], "entries": reissue["entries"],
            "reported_visitors": reissue.get("reported_visitors"),
            "reported_buyers": reissue.get("reported_buyers"),
            "signature": reissue["signature"], "reissues": reissue["target_cursor"],
        }
    for target, cancel in cancels.items():
        if target not in groups:
            anomalies.append({"type": "orphan_cancel", "cursor": cancel["cursor"],
                              "target_cursor": target})
    return [pages[cursor] for cursor in sorted(pages)], sorted(cancelled), anomalies


def _check_cutoffs(plan, pages):
    for page in pages:
        if page["stats_cutoff"] != plan["stats_cutoff"]:
            raise ValueError(
                f"page {page['cursor']} cutoff {page['stats_cutoff']} "
                f"!= locked cutoff {plan['stats_cutoff']}")


def _assemble(plan, pages):
    arms, cohorts, root = plan["arms"], list(plan["cohort_weights"]), plan["assignment_root"]
    assigned, exposed, buyers, page_stats = {}, {}, {}, []
    for page in pages:
        entries = page.get("entries", [])
        if page["kind"] == "assignment_page":
            for entry in entries:
                household = entry["household"]
                derived = derive_assignment(root, household, arms, cohorts)
                if (page["arm"], page["cohort"]) != derived:
                    raise ValueError(
                        f"roster page {page['cursor']} contradicts assignment root for {household}")
                if household in assigned:
                    raise ValueError(f"household {household} assigned on two roster pages")
                assigned[household] = derived
        elif page["kind"] == "exposure_page":
            unique = set()
            for entry in entries:
                household = entry["household"]
                unique.add(household)
                exposed.setdefault(household, []).append({
                    "cursor": page["cursor"], "arm": page["arm"],
                    "cohort": page["cohort"], "device": entry["device"]})
            page_stats.append({"cursor": page["cursor"], "entries": len(entries),
                               "unique_households": len(unique),
                               "reported_visitors": page.get("reported_visitors"),
                               "reported_buyers": page.get("reported_buyers")})
        elif page["kind"] == "order_page":
            for entry in entries:
                buyers.setdefault(entry["household"], []).append(entry["order_id"])
    conflicts, multi_device = [], []
    for household in sorted(exposed):
        observations = exposed[household]
        derived = derive_assignment(root, household, arms, cohorts)
        labels = {(o["arm"], o["cohort"]) for o in observations}
        if labels != {derived}:
            seen = sorted({(o["cursor"], o["arm"], o["cohort"]) for o in observations})
            conflicts.append({
                "household": household,
                "observed": [{"cursor": c, "arm": a, "cohort": h} for c, a, h in seen],
                "resolved": {"arm": derived[0], "cohort": derived[1]},
                "resolved_by": "assignment_root"})
        devices = sorted({o["device"] for o in observations})
        if len(devices) > 1:
            multi_device.append({"household": household, "devices": devices})
    return {"assigned": assigned, "exposed": exposed, "buyers": buyers,
            "conflicts": conflicts, "multi_device": multi_device, "page_stats": page_stats}


def _grid(arms, cohorts):
    return {(arm, cohort): 0 for arm in arms for cohort in cohorts}


def _table(visitors, buyers, weights, arms):
    """Stratified rates, per-stratum lifts, and the weight-standardized lift."""
    weight = {cohort: Fraction(str(weights[cohort])) for cohort in weights}
    strata = {}
    standardized = {arm: Fraction(0) for arm in arms}
    variance = Fraction(0)
    complete = True
    for cohort in weights:
        cells, rates = {}, {}
        for arm in arms:
            v, b = visitors.get((arm, cohort), 0), buyers.get((arm, cohort), 0)
            rate = Fraction(b, v) if v else None
            rates[arm] = rate
            cells[arm] = {"visitors": v, "buyers": b,
                          "rate": float(rate) if rate is not None else None}
        if all(rates[arm] is not None for arm in arms):
            lift = rates[arms[1]] - rates[arms[0]]
            stratum_var = sum(
                (rates[arm] * (1 - rates[arm]) / visitors[(arm, cohort)] for arm in arms),
                Fraction(0))
            for arm in arms:
                standardized[arm] += weight[cohort] * rates[arm]
            variance += weight[cohort] ** 2 * stratum_var
            strata[cohort] = {**cells, "lift": float(lift),
                              "se": math.sqrt(float(stratum_var))}
        else:
            complete = False
            strata[cohort] = {**cells, "lift": None, "se": None}
    totals = {}
    for arm in arms:
        v = sum(visitors.get((arm, c), 0) for c in weights)
        b = sum(buyers.get((arm, c), 0) for c in weights)
        totals[arm] = {"visitors": v, "buyers": b,
                       "rate": float(Fraction(b, v)) if v else None}
    if complete:
        lift = standardized[arms[1]] - standardized[arms[0]]
        se = math.sqrt(float(variance))
        block = {arms[0]: float(standardized[arms[0]]),
                 arms[1]: float(standardized[arms[1]]),
                 "lift": float(lift), "se": se,
                 "ci95": [float(lift) - 1.96 * se, float(lift) + 1.96 * se]}
    else:
        block = {arms[0]: None, arms[1]: None, "lift": None, "se": None, "ci95": None}
    return {"strata": strata, "arm_totals": totals, "standardized": block}


def build_report(conn, experiment_id, estimand=ESTIMAND_EXPOSED):
    """Rebuild one estimand-labelled marketing report from signed evidence."""
    if estimand not in ESTIMANDS:
        raise ValueError(f"unknown estimand {estimand!r}")
    events = _campaign_events(conn, experiment_id)
    plan = _locked_plan(events)
    pages, cancelled, anomalies = _active_pages(events)
    _check_cutoffs(plan, pages)
    by_kind = {kind: [p for p in pages if p["kind"] == kind] for kind in PAGE_KINDS}
    if not by_kind["order_page"]:
        raise InsufficientEvidence("independent order reconciliation pages have not arrived")
    if not by_kind["exposure_page"]:
        raise InsufficientEvidence("signed exposure pages have not arrived")
    if estimand == ESTIMAND_ITT and not by_kind["assignment_page"]:
        raise InsufficientEvidence(
            "platform-signed assignment roster required; exposure or click pages "
            "must not be fabricated into a roster")
    arms, cohorts = plan["arms"], list(plan["cohort_weights"])
    root = plan["assignment_root"]
    weights = dict(plan["cohort_weights"])
    data = _assemble(plan, pages)

    exposed_visitors, exposed_buyers = _grid(arms, cohorts), _grid(arms, cohorts)
    for household in data["exposed"]:
        exposed_visitors[derive_assignment(root, household, arms, cohorts)] += 1
    for household in data["buyers"]:
        if household in data["exposed"]:
            exposed_buyers[derive_assignment(root, household, arms, cohorts)] += 1
    visible = _table(exposed_visitors, exposed_buyers, weights, arms)

    rows = [{"arm": arm, "cohort": cohort,
             "visitors": exposed_visitors[(arm, cohort)],
             "buyers": exposed_buyers[(arm, cohort)]}
            for arm in arms for cohort in cohorts]
    naive_rates = aggregate_conversion(rows)
    control_v = sum(exposed_visitors[(arms[0], c)] for c in cohorts)
    control_b = sum(exposed_buyers[(arms[0], c)] for c in cohorts)
    treat_v = sum(exposed_visitors[(arms[1], c)] for c in cohorts)
    treat_b = sum(exposed_buyers[(arms[1], c)] for c in cohorts)
    naive = {"method": "aggregate_conversion",
             "aggregate_conversion_output": naive_rates,
             "lift": float(Fraction(treat_b, treat_v) - Fraction(control_b, control_v))
             if control_v and treat_v else None,
             "warning": "未分层汇总可能被人群组结构反转方向，仅供对照。"}

    itt = unexposed = None
    if estimand == ESTIMAND_ITT:
        itt_visitors, itt_buyers = _grid(arms, cohorts), _grid(arms, cohorts)
        for household, stratum in data["assigned"].items():
            itt_visitors[stratum] += 1
        for household in data["buyers"]:
            if household in data["assigned"]:
                itt_buyers[data["assigned"][household]] += 1
        itt = _table(itt_visitors, itt_buyers, weights, arms)
        unexposed = {arm: sum(1 for household, stratum in data["assigned"].items()
                              if stratum[0] == arm and household not in data["exposed"])
                     for arm in arms}

    scope = data["assigned"] if estimand == ESTIMAND_ITT else data["exposed"]
    out_of_scope = [{"household": household, "order_ids": sorted(order_ids)}
                    for household, order_ids in sorted(data["buyers"].items())
                    if household not in scope]
    experiment_buyers = sum(1 for household in data["buyers"] if household in scope)
    claimed = sum((p.get("reported_buyers") or 0) for p in by_kind["exposure_page"])
    deltas = [{"cursor": s["cursor"], "entries": s["entries"],
               "unique_households": s["unique_households"]}
              for s in data["page_stats"] if s["entries"] != s["unique_households"]]
    discrepancies = (
        [{"type": "household_stratum_conflict", **c} for c in data["conflicts"]]
        + [{"type": "multi_device_household", **m} for m in data["multi_device"]]
        + [{"type": "order_outside_experiment", **o} for o in out_of_scope]
        + [{"type": "page_entries_exceed_unique_households", **d} for d in deltas]
        + anomalies)
    reconciliation = {
        "first_party_orders": sum(len(ids) for ids in data["buyers"].values()),
        "experiment_buyers": experiment_buyers,
        "out_of_scope_orders": out_of_scope,
        "platform_claimed_buyers": claimed,
        "buyer_totals_compatible": experiment_buyers == claimed,
        "page_entry_deltas": deltas,
        "multi_device_households": data["multi_device"],
        "discrepancies": discrepancies,
    }

    raw_visitors = _grid(arms, cohorts)
    for page in by_kind["exposure_page"]:
        raw_visitors[(page["arm"], page["cohort"])] += len(page.get("entries", []))
    naive_table = _table(raw_visitors, exposed_buyers, weights, arms)
    page_sum_naive = {
        "note": "若按页加总曝光而不按家庭去重：同一随机化单位会进入两个层，分母与不确定区间一起失真。",
        "visitors_by_page_sum": {arm: {c: raw_visitors[(arm, c)] for c in cohorts}
                                 for arm in arms},
        "standardized_lift": naive_table["standardized"]["lift"],
        "se": naive_table["standardized"]["se"],
    }

    is_itt = estimand == ESTIMAND_ITT
    report = {
        "experiment_id": experiment_id,
        "estimand": estimand,
        "estimand_statement": _STATEMENTS[estimand],
        "causal_claim_allowed": is_itt,
        "denominator_scope": "all_assigned_households" if is_itt else "exposed_households_only",
        "denominator_note": _DENOMINATOR_NOTES[estimand],
        "applicable_decisions": _APPLICABLE_DECISIONS[estimand],
        "sampling_unit": plan["sampling_unit"],
        "stats_cutoff": plan["stats_cutoff"],
        "plan_locked_at": plan["locked_at"],
        "weights": weights,
        "weights_source": "locked_plan",
        "assignment_root": root,
        "primary": itt if is_itt else visible,
        "naive_aggregate": naive,
        "reconciliation": reconciliation,
        "household_conflicts": data["conflicts"],
        "diagnostics": {"page_sum_naive": page_sum_naive},
        "decision_scope": {
            "marketing_review_only": True,
            "note": "本报告只决定营销复盘结论：不改 offer 可售量、rights 分配或退款清算。"},
        "evidence": {
            "pages": [{"cursor": p["cursor"], "kind": p["kind"], "arm": p["arm"],
                       "cohort": p["cohort"], "signature": p["signature"]} for p in pages],
            "cancelled_cursors": cancelled,
            "evidence_hash": digest(events),
        },
    }
    if is_itt:
        report["visible_conversions"] = visible
        report["unexposed_assigned"] = unexposed
    return report


def _store_report(conn, report):
    subject = f"{report['experiment_id']}:{report['estimand']}"
    report_hash = digest(report)
    row = conn.execute(
        "SELECT revision, report_hash FROM campaign_reports WHERE subject=? "
        "ORDER BY revision DESC LIMIT 1", (subject,)).fetchone()
    if row and row["report_hash"] == report_hash:
        return 0
    revision = (row["revision"] if row else 0) + 1
    supersedes = row["report_hash"] if row else ""
    conn.execute(
        "INSERT INTO campaign_reports VALUES (?,?,?,?,?,?)",
        (subject, revision, canonical(report), report_hash, supersedes,
         report["evidence"]["evidence_hash"]))
    return 1


def record_reports(conn):
    """Persist a new report revision per estimand whenever evidence changed."""
    experiments = [row[0] for row in conn.execute(
        "SELECT DISTINCT experiment_id FROM campaign_plans ORDER BY experiment_id")]
    recorded = 0
    for experiment_id in experiments:
        for estimand in ESTIMANDS:
            try:
                report = build_report(conn, experiment_id, estimand)
            except InsufficientEvidence:
                continue
            recorded += _store_report(conn, report)
    return recorded


def report_history(conn, experiment_id):
    subjects = [f"{experiment_id}:{estimand}" for estimand in ESTIMANDS]
    marks = ",".join("?" for _ in subjects)
    return [{"subject": row["subject"], "revision": row["revision"],
             "report": json.loads(row["report"]), "report_hash": row["report_hash"],
             "supersedes": row["supersedes"], "evidence_hash": row["evidence_hash"]}
            for row in conn.execute(
                f"SELECT * FROM campaign_reports WHERE subject IN ({marks}) "
                "ORDER BY subject, revision", subjects)]

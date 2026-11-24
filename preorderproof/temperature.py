"""Cold-chain evidence: paged recorder logs, clock calibration, box sensors,
slot mapping, exposure certificates, signed freezer buffers, activity assays.

Ambient pages from a recorder and the thermal inertia of each box are separate
evidence items: every box mapped to a freezer shares that freezer's outage
intervals, while each box's packaging calibration decides how fast its core
follows ambient. Missing pages open gaps that must not be interpolated away;
the assessment keeps the range of deliverable units compatible with the shared
freezer events until a signed buffer or an independent activity assay resolves
it. The merchant's batch declaration stays untouched in the source ledger.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta

from .store import rows

PAGE_PERIOD_S = 300.0       # nominal interval between recorder pages
GAP_TOLERANCE_S = 3.0       # clock jitter tolerated around the page period
ROOM_AMBIENT_C = 20.0       # warehouse room bound for a freezer without power
STEP_S = 15.0               # core-temperature integration step
AMBIENT_CONFLICT_C = 0.5    # same-instant readings may not disagree by more
MERGE_TOLERANCE_S = 30.0    # readings within this window are the same instant

_EPOCH = datetime(1970, 1, 1)


def _seconds(stamp: str) -> float:
    return (datetime.fromisoformat(stamp) - _EPOCH).total_seconds()


def _iso(seconds: float) -> str:
    return (_EPOCH + timedelta(seconds=round(seconds, 3))).isoformat()


def _latest_unique(items: list[dict], key_fields: tuple[str, ...],
                   body_fields: tuple[str, ...]) -> tuple[dict, list]:
    """Group rows by identity; identities with divergent bodies are conflicts."""
    groups = defaultdict(list)
    for item in items:
        groups[tuple(item[k] for k in key_fields)].append(item)
    keep, conflicts = {}, []
    for identity, group in groups.items():
        if len({tuple(x[f] for f in body_fields) for x in group}) > 1:
            conflicts.append(identity)
        else:
            keep[identity] = group[-1]
    return keep, conflicts


def _above_fraction(c0: float, c1: float, limit: float) -> float:
    """Fraction of a linear core-temperature step spent strictly above limit."""
    if c0 > limit and c1 > limit:
        return 1.0
    if c0 <= limit and c1 <= limit:
        return 0.0
    return (max(c0, c1) - limit) / abs(c1 - c0)


def _integrate(segments: list[tuple], tau_s: float, limit: float, core0: float) -> tuple[float, float]:
    """Integrate box core temperature toward ambient with time constant tau.

    segments: (t0_s, t1_s, ambient_at_t0, ambient_at_t1), ambient linear inside.
    Returns (seconds above limit, end core temperature).
    """
    core = core0
    over = 0.0
    for t0, t1, a0, a1 in segments:
        span = t1 - t0
        if span <= 0:
            continue
        steps = max(1, math.ceil(span / STEP_S))
        dt = span / steps
        for i in range(steps):
            ambient = a0 + (a1 - a0) * ((i + 0.5) / steps)
            nxt = ambient + (core - ambient) * math.exp(-dt / tau_s)
            over += _above_fraction(core, nxt, limit) * dt
            core = nxt
    return over, core


def _segments(points: list[tuple], fill: str) -> list[tuple]:
    """Piecewise ambient; gaps are filled, never interpolated across.

    fill="best" holds a gap at the colder boundary (freezer kept cooling);
    fill="worst" holds it at room temperature bounded by the boundaries.
    """
    segs = []
    for (t0, a0), (t1, a1) in zip(points, points[1:]):
        if t1 - t0 > PAGE_PERIOD_S + GAP_TOLERANCE_S:
            value = min(a0, a1) if fill == "best" else max(ROOM_AMBIENT_C, a0, a1)
            segs.append((t0, t1, value, value))
        else:
            segs.append((t0, t1, a0, a1))
    return segs


def _gaps(points: list[tuple]) -> list[tuple]:
    return [(a[0], b[0]) for a, b in zip(points, points[1:])
            if b[0] - a[0] > PAGE_PERIOD_S + GAP_TOLERANCE_S]


def temperature_accounts(conn) -> dict[str, dict]:
    raw = {table: rows(conn, table) for table in (
        "exposure_certificates", "activity_assays", "sensor_placements", "slot_mappings",
        "thermal_calibrations", "clock_calibrations", "temp_pages", "buffer_samples")}
    certificates, cert_conflicts = _latest_unique(
        raw["exposure_certificates"], ("certificate_id",),
        ("batch_id", "box_id", "units", "packaging", "core_limit_c", "max_over_minutes", "issuer"))
    assays, assay_conflicts = _latest_unique(
        raw["activity_assays"], ("assay_id",),
        ("batch_id", "box_id", "units_consumed", "outcome", "assayed_at", "inspector"))
    placements, placement_conflicts = _latest_unique(
        raw["sensor_placements"], ("placement_id",), ("box_id", "sensor", "position", "positioned_at"))
    mappings, mapping_conflicts = _latest_unique(
        raw["slot_mappings"], ("mapping_id",), ("box_id", "recorder", "slot", "mapped_at"))
    thermal, thermal_conflicts = _latest_unique(
        raw["thermal_calibrations"], ("calibration_id",),
        ("packaging", "tau_minutes", "calibrated_at", "lab"))
    clocks, clock_conflicts = _latest_unique(
        raw["clock_calibrations"], ("calibration_id",), ("recorder", "calibrated_at", "offset_seconds"))
    pages, page_conflicts = _latest_unique(
        raw["temp_pages"], ("recorder", "boot", "page_no"), ("observed_at", "ambient_c"))
    buffers, buffer_conflicts = _latest_unique(
        raw["buffer_samples"], ("buffer_id", "observed_at"),
        ("recorder", "current_a", "ambient_c", "signature"))

    # Conflicting evidence taints the entity it describes; taint is conservative.
    batch_taint_flags = defaultdict(set)
    recorder_taint_flags = defaultdict(set)
    box_taint_flags = defaultdict(set)
    packaging_taint_flags = defaultdict(set)
    for row in raw["exposure_certificates"]:
        if (row["certificate_id"],) in cert_conflicts:
            batch_taint_flags[row["batch_id"]].add(f"certificate_conflict:{row['certificate_id']}")
    for row in raw["activity_assays"]:
        if (row["assay_id"],) in assay_conflicts:
            batch_taint_flags[row["batch_id"]].add(f"assay_conflict:{row['assay_id']}")
    for row in raw["temp_pages"]:
        identity = (row["recorder"], row["boot"], row["page_no"])
        if identity in page_conflicts:
            recorder_taint_flags[row["recorder"]].add(
                f"page_conflict:{row['recorder']}:{row['boot']}:{row['page_no']}")
    for row in raw["buffer_samples"]:
        if (row["buffer_id"], row["observed_at"]) in buffer_conflicts:
            recorder_taint_flags[row["recorder"]].add(
                f"buffer_conflict:{row['buffer_id']}:{row['observed_at']}")
    for row in raw["slot_mappings"]:
        if (row["mapping_id"],) in mapping_conflicts:
            box_taint_flags[row["box_id"]].add(f"mapping_conflict:{row['mapping_id']}")
    for row in raw["sensor_placements"]:
        if (row["placement_id"],) in placement_conflicts:
            box_taint_flags[row["box_id"]].add(f"placement_conflict:{row['placement_id']}")
    for row in raw["thermal_calibrations"]:
        if (row["calibration_id"],) in thermal_conflicts:
            packaging_taint_flags[row["packaging"]].add(f"thermal_conflict:{row['calibration_id']}")
    for row in raw["clock_calibrations"]:
        if (row["calibration_id"],) in clock_conflicts:
            recorder_taint_flags[row["recorder"]].add(f"clock_conflict:{row['calibration_id']}")

    # Clock-corrected, merged ambient timelines per recorder (pages + signed buffer).
    clock_by_recorder = defaultdict(list)
    for cal in clocks.values():
        clock_by_recorder[cal["recorder"]].append((_seconds(cal["calibrated_at"]), cal["offset_seconds"]))
    for entries in clock_by_recorder.values():
        entries.sort()

    def corrected(recorder: str, stamp: str) -> float:
        t = _seconds(stamp)
        offset = 0
        for cal_at, offset_seconds in clock_by_recorder.get(recorder, []):
            if cal_at <= t:
                offset = offset_seconds
        return t - offset

    points_by_recorder = defaultdict(list)  # (t_s, ambient_c, event_id, current_a|None, kind)
    for page in pages.values():
        points_by_recorder[page["recorder"]].append(
            (corrected(page["recorder"], page["observed_at"]), float(page["ambient_c"]),
             page["event_id"], None, "page"))
    for sample in buffers.values():
        points_by_recorder[sample["recorder"]].append(
            (corrected(sample["recorder"], sample["observed_at"]), float(sample["ambient_c"]),
             sample["event_id"], float(sample["current_a"]), "buffer"))
    timelines = {}
    for recorder, points in points_by_recorder.items():
        points.sort(key=lambda p: (p[0], p[2]))
        merged = []
        for point in points:
            if merged and abs(point[0] - merged[-1][0]) <= MERGE_TOLERANCE_S:
                previous = merged[-1]
                if abs(point[1] - previous[1]) > AMBIENT_CONFLICT_C:
                    recorder_taint_flags[recorder].add(
                        f"ambient_conflict:{recorder}:{_iso(point[0])}")
                current = previous[3] if previous[3] is not None else point[3]
                merged[-1] = (previous[0], previous[1], previous[2], current, previous[4])
            else:
                merged.append(point)
        timelines[recorder] = merged
    outages = defaultdict(list)
    for recorder, points in timelines.items():
        group = []
        for t in [p[0] for p in points if p[4] == "buffer" and p[3] is not None and p[3] <= 0.0]:
            if group and t - group[-1] > PAGE_PERIOD_S + GAP_TOLERANCE_S:
                outages[recorder].append((group[0], group[-1]))
                group = []
            group.append(t)
        if group:
            outages[recorder].append((group[0], group[-1]))

    lots = {}
    for lot in rows(conn, "batches"):
        current = lots.get(lot["batch_id"])
        if not current or (lot["event_at"], lot["event_id"]) > (current["event_at"], current["event_id"]):
            lots[lot["batch_id"]] = lot

    certs_by_batch = defaultdict(dict)
    cert_fields = ("batch_id", "box_id", "units", "packaging", "core_limit_c", "max_over_minutes", "issuer")
    for cert in certificates.values():
        box_id = cert["box_id"]
        existing = certs_by_batch[cert["batch_id"]].get(box_id)
        if existing and tuple(existing[f] for f in cert_fields) != tuple(cert[f] for f in cert_fields):
            batch_taint_flags[cert["batch_id"]].add(f"certificate_overlap:{box_id}")
            continue
        certs_by_batch[cert["batch_id"]][box_id] = cert
    assays_by_batch = defaultdict(lambda: defaultdict(list))
    for assay in assays.values():
        assays_by_batch[assay["batch_id"]][assay["box_id"]].append(assay)
    # A box may be re-slotted within one freezer, but mappings that move it
    # between freezers contradict each other: no placement is then trustworthy.
    recorders_by_box = defaultdict(set)
    for mapping in mappings.values():
        recorders_by_box[mapping["box_id"]].add(mapping["recorder"])
    relocated_boxes = {box_id for box_id, recorders in recorders_by_box.items() if len(recorders) > 1}
    for box_id in relocated_boxes:
        box_taint_flags[box_id].add(f"mapping_relocation:{box_id}")
    mapping_by_box = {}
    for mapping in mappings.values():
        if mapping["box_id"] in relocated_boxes:
            continue
        current = mapping_by_box.get(mapping["box_id"])
        if not current or (mapping["mapped_at"], mapping["event_id"]) > (current["mapped_at"], current["event_id"]):
            mapping_by_box[mapping["box_id"]] = mapping
    placed_boxes = {p["box_id"] for p in placements.values()}
    thermal_by_packaging = {}
    for cal in thermal.values():
        current = thermal_by_packaging.get(cal["packaging"])
        if not current or (cal["calibrated_at"], cal["event_id"]) > (current["calibrated_at"], current["event_id"]):
            thermal_by_packaging[cal["packaging"]] = cal

    result = {}
    batch_ids = sorted(set(certs_by_batch) | set(assays_by_batch))
    for batch_id in batch_ids:
        flags = set(batch_taint_flags.get(batch_id, set()))
        tainted = bool(flags)
        certs = certs_by_batch.get(batch_id, {})
        lot = lots.get(batch_id)
        certified_units = sum(c["units"] for c in certs.values())
        declared_units = lot["quantity"] if lot else None
        uncertified = max(0, declared_units - certified_units) if declared_units is not None else 0
        if declared_units is not None and certified_units > declared_units:
            flags.add("certificate_overcount")
            tainted = True
        if uncertified:
            flags.add("certificate_shortfall")
        totals = defaultdict(int)
        deliverable = 0
        boxes = []
        gaps, outage_intervals = set(), set()
        evidence = {"pages": set(), "buffers": set(), "assays": set(), "certificates": set(), "calibrations": set()}
        any_assay = any_buffer = any_pages = False
        for box_id, cert in sorted(certs.items()):
            evidence["certificates"].add(cert["event_id"])
            entry = {"box_id": box_id, "units": cert["units"], "packaging": cert["packaging"],
                     "recorder": None, "slot": None, "verdict": None, "deliverable_units": 0,
                     "over_minutes": None, "best_over_minutes": None, "worst_over_minutes": None,
                     "open_exceedance": False, "assay": None, "detail": None}
            boxes.append(entry)
            for source in (box_taint_flags.get(box_id), packaging_taint_flags.get(cert["packaging"])):
                if source:
                    flags |= source
                    tainted = True
            box_assays = sorted(assays_by_batch.get(batch_id, {}).get(box_id, []),
                                key=lambda a: (a["assayed_at"], a["event_id"]))
            if box_assays:
                # Independent activity sampling decides what may ship now; it says
                # nothing about when the freezer stopped, so no liability accrues.
                any_assay = True
                consumed = sum(a["units_consumed"] for a in box_assays)
                evidence["assays"].update(a["event_id"] for a in box_assays)
                latest = box_assays[-1]
                entry["assay"] = {"outcome": latest["outcome"], "consumed_units": consumed,
                                  "assayed_at": latest["assayed_at"], "inspector": latest["inspector"]}
                if consumed > cert["units"]:
                    entry["verdict"] = "pending_retest"
                    entry["detail"] = "assay_overconsumption"
                    flags.add(f"assay_overconsumption:{box_id}")
                    tainted = True
                    totals["pending_retest"] += cert["units"]
                elif latest["outcome"] == "effective":
                    shippable = cert["units"] - consumed
                    entry["verdict"] = "assay_effective"
                    entry["deliverable_units"] = shippable
                    totals["assay_effective"] += shippable
                    totals["consumed"] += consumed
                    deliverable += shippable
                else:
                    entry["verdict"] = "assay_failed"
                    totals["assay_failed"] += cert["units"]
                    totals["consumed"] += consumed
                continue
            mapping = mapping_by_box.get(box_id)
            if not mapping:
                entry["verdict"], entry["detail"] = "pending_retest", "box_unmapped"
                totals["pending_retest"] += cert["units"]
                continue
            entry["recorder"], entry["slot"] = mapping["recorder"], mapping["slot"]
            flags |= recorder_taint_flags.get(mapping["recorder"], set())
            if box_id not in placed_boxes:
                entry["verdict"], entry["detail"] = "pending_retest", "sensor_missing"
                totals["pending_retest"] += cert["units"]
                continue
            cal = thermal_by_packaging.get(cert["packaging"])
            if not cal:
                entry["verdict"], entry["detail"] = "pending_retest", "thermal_calibration_missing"
                totals["pending_retest"] += cert["units"]
                continue
            evidence["calibrations"].add(cal["event_id"])
            points = timelines.get(mapping["recorder"], [])
            page_points = [p for p in points if p[4] == "page"]
            if mapping["recorder"] in recorder_taint_flags:
                tainted = True
                entry["verdict"], entry["detail"] = "pending_retest", "recorder_evidence_conflict"
                totals["pending_retest"] += cert["units"]
                continue
            if len(page_points) < 2:
                entry["verdict"], entry["detail"] = "pending_retest", "temperature_pages_missing"
                totals["pending_retest"] += cert["units"]
                continue
            any_pages = True
            evidence["pages"].update(p[2] for p in page_points)
            if any(p[4] == "buffer" for p in points):
                any_buffer = True
                evidence["buffers"].update(p[2] for p in points if p[4] == "buffer")
            xy = [(p[0], p[1]) for p in points]
            page_xy = [(p[0], p[1]) for p in page_points]
            merged_gaps = _gaps(xy)
            for g0, g1 in _gaps(page_xy):
                covered = not any(h0 < g1 - 1 and h1 > g0 + 1 for h0, h1 in merged_gaps)
                gaps.add((mapping["recorder"], _iso(g0), _iso(g1), covered))
            for o0, o1 in outages.get(mapping["recorder"], []):
                outage_intervals.add((mapping["recorder"], _iso(o0), _iso(o1)))
            tau_s = float(cal["tau_minutes"]) * 60.0
            limit = float(cert["core_limit_c"])
            max_over_s = float(cert["max_over_minutes"]) * 60.0
            if not merged_gaps:
                over_s, end_core = _integrate(_segments(xy, "best"), tau_s, limit, xy[0][1])
                open_end = end_core > limit
                entry["open_exceedance"] = open_end
                entry["over_minutes"] = None if open_end else round(over_s / 60, 3)
                if open_end or over_s > max_over_s:
                    entry["verdict"] = "failed"
                    totals["proven_failed"] += cert["units"]
                else:
                    entry["verdict"] = "proven_ok"
                    entry["deliverable_units"] = cert["units"]
                    totals["proven_ok"] += cert["units"]
                    deliverable += cert["units"]
            else:
                best_s, _ = _integrate(_segments(xy, "best"), tau_s, limit, xy[0][1])
                worst_s, worst_end = _integrate(_segments(xy, "worst"), tau_s, limit, xy[0][1])
                worst_open = worst_end > limit
                entry["best_over_minutes"] = round(best_s / 60, 3)
                entry["worst_over_minutes"] = None if worst_open else round(worst_s / 60, 3)
                entry["open_exceedance"] = worst_open
                if not worst_open and worst_s <= max_over_s:
                    entry["verdict"] = "proven_ok"
                    entry["deliverable_units"] = cert["units"]
                    totals["proven_ok"] += cert["units"]
                    deliverable += cert["units"]
                elif best_s > max_over_s:
                    entry["verdict"] = "failed"
                    totals["proven_failed"] += cert["units"]
                else:
                    entry["verdict"] = "suspect"
                    totals["suspect"] += cert["units"]
        totals["pending_retest"] += uncertified
        for box_id, box_assays in sorted(assays_by_batch.get(batch_id, {}).items()):
            if box_id not in certs:
                flags.add(f"assay_without_certificate:{box_id}")
        if not certs:
            flags.add("certificates_missing")
        if tainted:
            status = "evidence_conflict"
        elif totals["suspect"] == 0 and totals["pending_retest"] == 0 and certs:
            status = "resolved"
        else:
            status = "pending_evidence"
        if any_assay and any_buffer:
            resolution = "activity_sampling+freezer_buffer"
        elif any_assay:
            resolution = "activity_sampling"
        elif any_buffer:
            resolution = "freezer_buffer"
        elif any_pages:
            resolution = "ambient_range" if any(not g[3] for g in gaps) else "ambient_pages"
        else:
            resolution = "insufficient_evidence"
        result[batch_id] = {
            "batch_id": batch_id,
            "status": status,
            "resolution": resolution,
            "declared": {"quantity": lot["quantity"], "status": lot["status"],
                         "certified_until": lot["certified_until"]} if lot else None,
            "proven_ok_units": totals["proven_ok"],
            "suspect_units": totals["suspect"],
            "proven_failed_units": totals["proven_failed"],
            "pending_retest_units": totals["pending_retest"],
            "assay_effective_units": totals["assay_effective"],
            "assay_failed_units": totals["assay_failed"],
            "consumed_units": totals["consumed"],
            "deliverable_units": deliverable,
            "deliverable_range": [deliverable, deliverable + totals["suspect"]],
            "outage_intervals": [{"recorder": r, "start": s, "end": e}
                                 for r, s, e in sorted(outage_intervals)],
            "liability_evidence": "freezer_buffer" if outage_intervals else "none",
            "gaps": [{"recorder": r, "start": s, "end": e, "covered": c}
                     for r, s, e, c in sorted(gaps)],
            "boxes": boxes,
            "flags": sorted(flags),
            "evidence": {k: sorted(v) for k, v in evidence.items()},
        }
    return result

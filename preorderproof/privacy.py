"""Pseudonymous exports and the privacy-preserving research release protocol.

`pseudonymous_orders` and `public_joined_rows` are the naive provisional
export, kept as the baseline counterexample: they keep stable order keys and
exact receipt dates, so a joined publication can be linked back to one
customer's return record.

Everything below builds the offline research dataset that cannot be linked
back to a person. Orders, parcels and receipts are joined inside the store,
collapsed to one record per consumer, generalised to ISO weeks, and published
only as k-anonymous groups. Original identifiers and consumer tokens stay in
the controlled mapping store (`store.connect_mapping`); researchers only ever
see group statistics. Every candidate release is checked against every
still-accessible earlier release, so two versions can never be subtracted to
expose a single person's return status. Two pre-authorised routes exist for
late arrivals:

A  Freeze every published week's original cohort. Late arrivals wait for the
   next closed research cohort; the old release stays valid and unchanged,
   sacrificing recent coverage.
B  Hold the late arrival in the controlled store until the delta between the
   old and the new release covers at least k people whose return status is
   published merged, then republish the same-week cohort, sacrificing
   timeliness for same-week coverage.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date

from .delivery import order_delivery_outcomes
from .rights import _unique_latest
from .store import canonical, digest, rows

K_MIN = 2
ROUTES = ("A", "B")
USABLE_DIMENSIONS = ("purchase_week", "receipt_week", "return_status")
GROUP_STATISTICS = ("members", "returned", "signed")
SUPPRESSED_DIMENSIONS = (
    ("order_id", "stable cross-version key; kept only in the controlled mapping store"),
    ("consumer_token", "stable pseudonym; kept only in the controlled mapping store"),
    ("consumer", "direct identifier; never leaves the source store"),
    ("placed_at", "exact purchase date generalised to purchase_week"),
    ("received_at", "exact receipt date generalised to receipt_week"),
    ("event_id", "raw store identity; researchers must not cite it"),
    ("parcel_id", "internal join key between orders and receipts"),
    ("receipt_id", "internal join key between parcels and outcomes"),
    ("witness", "internal fulfilment evidence"),
)


def pseudonymous_orders(orders):
    return [
        {"order_id": order["order_id"],
         "consumer_token": hashlib.sha256(order["consumer"].encode()).hexdigest()[:12],
         "placed_at": order["placed_at"]}
        for order in orders
    ]


def public_joined_rows(orders, parcels, receipts):
    """Naively join provisional pseudonyms to precise receipt records."""
    by_order = {row["order_id"]: row for row in pseudonymous_orders(orders)}
    parcel_order = {row["parcel_id"]: row["order_id"] for row in parcels}
    return [
        {**by_order[parcel_order[row["parcel_id"]]],
         "received_at": row["received_at"], "receipt_type": row["receipt_type"]}
        for row in receipts
        if row["parcel_id"] in parcel_order and parcel_order[row["parcel_id"]] in by_order
    ]


def iso_week(day: str) -> str:
    """Coarsest date granularity this protocol publishes: the ISO week."""
    year, week, _ = date.fromisoformat(day).isocalendar()
    return f"{year}-W{week:02d}"


def consumer_token(consumer: str) -> str:
    """Fixed pseudonym for one consumer; controlled mapping store only."""
    return hashlib.sha256(consumer.encode()).hexdigest()[:12]


def snapshot_persons(conn) -> list[dict]:
    """Join orders/parcels/receipts from one SQLite snapshot, one row per person.

    The internal join follows store.py's order_id and parcel_id keys, then
    collapses to a single record per consumer: duplicate receipts and
    late-arriving returns never make the same person two people. The records
    still carry identifiers and exact dates and must stay inside the
    publisher; only aggregated groups may be released.
    """
    outcomes = order_delivery_outcomes(conn)
    orders, _ = _unique_latest(
        rows(conn, "orders"), "order_id", ("offer_id", "units", "placed_at", "consumer"))
    persons: dict[str, dict] = {}
    for order_id in sorted(orders):
        order = orders[order_id]
        token = consumer_token(order["consumer"])
        person = persons.setdefault(token, {
            "consumer_token": token, "consumer": order["consumer"],
            "order_ids": [], "placed_ats": [], "outcomes": []})
        person["order_ids"].append(order_id)
        person["placed_ats"].append(order["placed_at"])
        outcome = outcomes.get(order_id)
        if outcome:
            person["outcomes"].append(outcome)
    result = []
    for token in sorted(persons):
        person = persons[token]
        record = {
            "consumer_token": token,
            "consumer": person["consumer"],
            "order_ids": sorted(person["order_ids"]),
            "purchase_week": iso_week(min(person["placed_ats"])),
        }
        if person["outcomes"]:
            record["received_at"] = max(o["received_at"] for o in person["outcomes"])
            record["receipt_week"] = iso_week(record["received_at"])
            record["return_status"] = (
                "returned" if any(o["status"] == "returned" for o in person["outcomes"]) else "signed")
        result.append(record)
    return result


def controlled_mapping_rows(persons) -> list[dict]:
    """Rows for the controlled mapping store: the only place where original
    order ids and fixed consumer tokens are kept together."""
    return [{"consumer_token": p["consumer_token"], "consumer": p["consumer"],
             "order_ids": list(p["order_ids"])} for p in persons]


def _person_state(person: dict) -> dict:
    return {"receipt_week": person["receipt_week"], "return_status": person["return_status"]}


def _cells(purchase_week: str, members: list[dict]) -> list[dict]:
    """Equivalence classes of one cohort on the joined view, keyed by week."""
    by_week = defaultdict(list)
    for member in members:
        by_week[member["receipt_week"]].append(member)
    return [{"purchase_week": purchase_week, "receipt_week": receipt_week,
             "members": len(group),
             "returned": sum(1 for m in group if m["return_status"] == "returned"),
             "signed": sum(1 for m in group if m["return_status"] == "signed")}
            for receipt_week, group in sorted(by_week.items())]


def _withheld(cell: dict, cohort_id: str, k: int) -> dict:
    """A suppressed cell publishes only its member count, never the
    return-status breakdown of a below-k group."""
    return {"purchase_week": cell["purchase_week"], "receipt_week": cell["receipt_week"],
            "members": cell["members"], "cohort": cohort_id, "reason": f"below_k={k}"}


def _route_a(measured, cohorts, priors, k):
    """Frozen-cohort route: published weeks are carried forward unchanged and
    late arrivals assemble into the next closed research cohort."""
    cohorts = [dict(c, members=dict(c["members"])) for c in cohorts]
    by_token = {p["consumer_token"]: p for p in measured}
    assigned = {token for c in cohorts for token in c["members"]}
    updates = {}
    for person in measured:
        token = person["consumer_token"]
        if token in assigned:
            continue
        week = person["purchase_week"]
        pending = [c for c in cohorts if c["purchase_week"] == week and c["state"] == "pending"]
        if pending:
            cohort = pending[-1]
        else:
            seq = max((c["seq"] for c in cohorts if c["purchase_week"] == week), default=0) + 1
            cohort = {"cohort_id": f"{week}#{seq}", "purchase_week": week, "seq": seq,
                      "state": "pending", "members": {}}
            cohorts.append(cohort)
        cohort["members"][token] = _person_state(person)
        assigned.add(token)
    groups, withheld, proof = [], [], []
    for cohort in sorted(cohorts, key=lambda c: c["cohort_id"]):
        cid = cohort["cohort_id"]
        if cohort["state"] == "published":
            for prior in reversed(priors):
                found = [g for g in prior["document"]["groups"] if g["cohort"] == cid]
                if found:
                    for group in found:
                        groups.append({**group, "carried": True})
                    proof.append({
                        "cohort": cid, "against": prior["version"],
                        "delta_members": 0, "delta_returned": 0, "verdict": "safe",
                        "detail": "frozen cohort carried forward unchanged; the earlier release "
                                  "stays valid and is never withdrawn"})
                    break
            else:
                raise ValueError(f"published cohort missing from prior releases: {cid}")
            continue
        cohort["members"] = {t: _person_state(by_token[t])
                             for t in sorted(cohort["members"]) if t in by_token}
        members = [by_token[t] for t in sorted(cohort["members"])]
        cells = _cells(cohort["purchase_week"], members)
        if members and all(cell["members"] >= k for cell in cells):
            for cell in cells:
                groups.append({**cell, "cohort": cid, "carried": False})
            cohort["state"] = "published"
            proof.append({
                "cohort": cid, "against": None, "delta_members": len(members),
                "delta_returned": sum(1 for m in members if m["return_status"] == "returned"),
                "verdict": "safe",
                "detail": "new closed cohort; no earlier release contains it"})
        else:
            for cell in cells:
                withheld.append(_withheld(cell, cid, k))
        updates[cid] = cohort
    return groups, withheld, proof, updates, False


def _route_b(measured, cohorts, priors, k):
    """Merged-delta route: republish the same-week cohort only when the change
    against every still-accessible earlier release covers at least k people."""
    before = {c["cohort_id"]: dict(c["members"]) for c in cohorts}
    by_week = defaultdict(list)
    for person in measured:
        by_week[person["purchase_week"]].append(person)
    proof, planned, rejected = [], {}, False
    for week in sorted(by_week):
        cid = f"{week}#1"
        members = sorted(by_week[week], key=lambda p: p["consumer_token"])
        current = {p["consumer_token"]: _person_state(p) for p in members}
        prior_states = before.get(cid, {})
        added = sorted(set(current) - set(prior_states))
        removed = sorted(set(prior_states) - set(current))
        drift = sorted(t for t in set(current) & set(prior_states)
                       if current[t] != prior_states[t])
        changed = len(added) + len(removed) + len(drift)
        prior_docs = [p for p in priors
                      if any(g["cohort"] == cid for g in p["document"]["groups"])]
        if not prior_docs:
            proof.append({
                "cohort": cid, "against": None, "delta_members": len(current),
                "delta_returned": sum(1 for t in current
                                      if current[t]["return_status"] == "returned"),
                "verdict": "safe",
                "detail": "new cohort; no earlier release contains it"})
        for prior in prior_docs:
            prior_groups = [g for g in prior["document"]["groups"] if g["cohort"] == cid]
            delta_members = len(added) + len(removed)
            delta_returned = (sum(1 for t in current if current[t]["return_status"] == "returned")
                              - sum(g["returned"] for g in prior_groups))
            if 0 < changed < k:
                proof.append({
                    "cohort": cid, "against": prior["version"],
                    "delta_members": delta_members, "delta_returned": delta_returned,
                    "verdict": "unsafe",
                    "detail": "single-person difference: subtracting the still-accessible "
                              "earlier release would isolate one person's return status"})
                rejected = True
            else:
                detail = ("no membership or state change; republished figures are identical"
                          if changed == 0 else
                          f"changed group of {changed} people is at least k={k} and their return "
                          "status is published merged, so no single-person difference forms")
                proof.append({
                    "cohort": cid, "against": prior["version"],
                    "delta_members": delta_members, "delta_returned": delta_returned,
                    "verdict": "safe", "detail": detail})
        planned[cid] = {"cohort_id": cid, "purchase_week": week, "seq": 1,
                        "members": current, "cells": _cells(week, members)}
    if rejected:
        return [], [], proof, {}, True
    groups, withheld, updates = [], [], {}
    for cid in sorted(planned):
        cohort = planned[cid]
        publishable = [c for c in cohort["cells"] if c["members"] >= k]
        for cell in publishable:
            groups.append({**cell, "cohort": cid, "carried": False})
        for cell in cohort["cells"]:
            if cell["members"] < k:
                withheld.append(_withheld(cell, cid, k))
        updates[cid] = {"cohort_id": cid, "purchase_week": cohort["purchase_week"], "seq": 1,
                        "state": "published" if publishable else "pending",
                        "members": cohort["members"]}
    return groups, withheld, proof, updates, False


def plan_release(persons, *, version, route, cohorts, priors, as_of, k=K_MIN) -> dict:
    """Plan one release from internal person records.

    `cohorts` and `priors` come from the controlled mapping store, so the
    differential check covers every still-accessible earlier release instead
    of assuming researchers forgot them. The returned document contains only
    group statistics; the caller persists it when the plan is publishable.
    """
    if route not in ROUTES:
        raise ValueError(f"route must be one of {ROUTES}")
    measured = [p for p in persons if "receipt_week" in p]
    unmeasured = len(persons) - len(measured)
    if route == "A":
        groups, withheld, proof, updates, rejected = _route_a(measured, cohorts, priors, k)
    else:
        groups, withheld, proof, updates, rejected = _route_b(measured, cohorts, priors, k)
    document = {
        "version": version, "route": route, "as_of": as_of, "k": k,
        "dimensions": {
            "usable": list(USABLE_DIMENSIONS),
            "statistics": list(GROUP_STATISTICS),
            "suppressed": [{"field": field, "reason": reason}
                           for field, reason in SUPPRESSED_DIMENSIONS],
        },
        "differential_proof": proof,
        "evidence_hash": digest(persons),
    }
    if rejected:
        document.update({
            "status": "rejected",
            "reason": "single_person_difference",
            "detail": "publishing now would let anyone holding the earlier release subtract "
                      "the two versions and isolate one person's return status",
        })
    else:
        document.update({
            "status": "published",
            "groups": groups,
            "withheld": withheld,
            "unmeasured_orders": unmeasured,
        })
    return {"document": document, "cohort_updates": updates if not rejected else {}}


def _load_cohorts(mapping_conn, route) -> list[dict]:
    return [{"cohort_id": r["cohort_id"], "purchase_week": r["purchase_week"],
             "seq": r["seq"], "state": r["state"], "members": json.loads(r["members"])}
            for r in mapping_conn.execute(
                "SELECT * FROM privacy_cohorts WHERE route=? ORDER BY cohort_id", (route,))]


def _load_releases(mapping_conn) -> list[dict]:
    return [{"version": r["version"], "route": r["route"], "as_of": r["as_of"],
             "document": json.loads(r["body"])}
            for r in mapping_conn.execute("SELECT * FROM privacy_releases ORDER BY rowid")]


def release_history(mapping_conn) -> list[dict]:
    """Every still-accessible earlier release; old versions are never withdrawn."""
    return _load_releases(mapping_conn)


def _upsert_mapping(mapping_conn, mapping_rows) -> None:
    for row in mapping_rows:
        mapping_conn.execute(
            "INSERT INTO privacy_mapping VALUES (?,?,?) "
            "ON CONFLICT(consumer_token) DO UPDATE SET order_ids=excluded.order_ids",
            (row["consumer_token"], row["consumer"], json.dumps(row["order_ids"])))


def _save_cohorts(mapping_conn, route, updates) -> None:
    for cohort in updates.values():
        mapping_conn.execute(
            "INSERT INTO privacy_cohorts VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(route, cohort_id) DO UPDATE SET "
            "state=excluded.state, members=excluded.members",
            (route, cohort["cohort_id"], cohort["purchase_week"], cohort["seq"],
             cohort["state"], json.dumps(cohort["members"], sort_keys=True)))


def publish(mapping_conn, store_conn, *, version, route, k=K_MIN) -> dict:
    """Build a research release from one SQLite snapshot and persist it if safe.

    Original identifiers and consumer tokens are written only to the
    controlled mapping store. The returned document is the researcher-facing
    package: usable dimensions, suppressed dimensions, group member counts
    and the differential-risk proof against every earlier release. A plan
    that would expose a single-person difference is rejected and nothing is
    released.
    """
    persons = snapshot_persons(store_conn)
    as_of = store_conn.execute(
        "SELECT MAX(event_at) FROM source_events").fetchone()[0] or "0001-01-01"
    if mapping_conn.execute(
            "SELECT 1 FROM privacy_releases WHERE version=?", (version,)).fetchone():
        raise ValueError(f"release version already published: {version}")
    cohorts = _load_cohorts(mapping_conn, route)
    priors = _load_releases(mapping_conn)
    result = plan_release(persons, version=version, route=route, cohorts=cohorts,
                          priors=priors, as_of=as_of, k=k)
    document = result["document"]
    with mapping_conn:
        _upsert_mapping(mapping_conn, controlled_mapping_rows(persons))
        if document["status"] == "published":
            _save_cohorts(mapping_conn, route, result["cohort_updates"])
            mapping_conn.execute(
                "INSERT INTO privacy_releases VALUES (?,?,?,?,?)",
                (version, route, as_of, canonical(document), digest(document)))
    return document

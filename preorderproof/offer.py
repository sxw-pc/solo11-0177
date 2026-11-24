"""Merchant promise and certified stock decisions."""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from .store import rows
from .temperature import temperature_accounts


def offer_accounts(conn, temperature: dict[str, dict] | None = None) -> dict[str, dict]:
    temperature = temperature_accounts(conn) if temperature is None else temperature
    versions = defaultdict(list)
    for row in rows(conn, "offers"):
        versions[row["offer_id"]].append(row)
    batches = defaultdict(list)
    for row in rows(conn, "batches"):
        batches[row["batch_id"]].append(row)
    result = {}
    for offer_id, events in versions.items():
        top_version = max(row["version"] for row in events)
        latest = [row for row in events if row["version"] == top_version]
        signatures = {
            (r["batch_id"], r["capacity"], r["price"], r["deposit"], r["due"],
             r["deposit_deadline"], r["tail_deadline"], r["grace_days"], r["status"])
            for r in latest
        }
        conflict = len(signatures) > 1
        offer = sorted(latest, key=lambda x: (x["event_at"], x["event_id"]))[-1]
        lot_events = batches.get(offer["batch_id"], [])
        lot = sorted(lot_events, key=lambda x: (x["event_at"], x["event_id"]))[-1] if lot_events else None
        same_time = [x for x in lot_events if lot and x["event_at"] == lot["event_at"]]
        lot_conflict = len({(x["quantity"], x["certified_until"], x["status"]) for x in same_time}) > 1
        lot_valid = bool(lot and not lot_conflict and lot["status"] == "good"
                         and date.fromisoformat(lot["certified_until"]) >= date.fromisoformat(offer["due"]))
        assessment = temperature.get(offer["batch_id"])
        # Cold-chain evidence, not the merchant's declaration, bounds how many units
        # may still be promised; the declaration itself stays untouched in the ledger.
        effective_quantity = lot["quantity"] if lot else 0
        if assessment:
            effective_quantity = min(effective_quantity, assessment["deliverable_units"])
        active = not conflict and offer["status"] == "active" and lot_valid
        previous = [x for x in events if x["version"] < top_version]
        previous_due = sorted(previous, key=lambda x: (x["version"], x["event_at"]))[-1]["due"] if previous else None
        due_change = (date.fromisoformat(offer["due"]) - date.fromisoformat(previous_due)).days if previous_due else 0
        result[offer_id] = {
            "offer": offer, "lot": lot, "version_conflict": conflict,
            "lot_conflict": lot_conflict, "lot_valid": lot_valid,
            "active": active, "capacity": min(offer["capacity"], effective_quantity) if active else 0,
            "temperature_pending": bool(assessment and assessment["status"] != "resolved"),
            "temperature": None if not assessment else {
                "status": assessment["status"],
                "resolution": assessment["resolution"],
                "deliverable_units": assessment["deliverable_units"],
                "deliverable_range": assessment["deliverable_range"],
                "proven_ok_units": assessment["proven_ok_units"],
                "suspect_units": assessment["suspect_units"],
                "pending_retest_units": assessment["pending_retest_units"],
                "liability_evidence": assessment["liability_evidence"],
            },
            "previous_due": previous_due, "due_change_days": due_change,
            "reason": "promise_version_conflict" if conflict else
                      "offer_withdrawn" if offer["status"] != "active" else
                      "batch_conflict" if lot_conflict else
                      "batch_unavailable" if not lot_valid else "available",
        }
    return result

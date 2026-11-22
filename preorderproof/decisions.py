"""Cross-ledger verdicts with ten separately observable decision surfaces."""
from __future__ import annotations

from .delivery import fulfilment_accounts
from .offer import offer_accounts
from .rights import order_accounts
from .recovery import inspected_returns


def evaluate(conn) -> dict[str, dict]:
    offers = offer_accounts(conn)
    last_event = conn.execute("SELECT MAX(event_at) FROM source_events").fetchone()[0] or "0001-01-01"
    rights = order_accounts(conn, offers, last_event)
    fulfilment = fulfilment_accounts(conn, offers, rights, last_event)
    recovered, restocked_by_order, recovery_log = inspected_returns(conn, offers, rights, fulfilment)
    if recovered:
        rights = order_accounts(conn, offers, last_event, recovered)
        fulfilment = fulfilment_accounts(conn, offers, rights, last_event, restocked_by_order)
    result = {}
    for offer_id, state in offers.items():
        offer = state["offer"]
        result[f"offer:{offer_id}"] = {
            "subject": offer_id, "kind": "offer", "version": offer["version"],
            "status": "available" if state["active"] else state["reason"],
            "surfaces": {
                "batch_quality": state["lot_valid"],
                "promise_version_conflict": state["version_conflict"],
                "changed_due_requires_consent": state["due_change_days"] > offer["grace_days"],
            },
            "capacity": state["capacity"],
            "recovered_capacity": recovered.get(offer_id, 0),
            "unallocated_capacity": state.get("unallocated_capacity", state["capacity"]),
            "restock_reviews": [x for x in recovery_log if x.get("offer_id") == offer_id],
            "batch_id": offer["batch_id"], "due": offer["due"],
            "previous_due": state["previous_due"],
        }
    for order_id, right in rights.items():
        fulfil = fulfilment[order_id]
        result[f"order:{order_id}"] = {
            "subject": order_id, "kind": "order", "offer_id": right["order"]["offer_id"],
            "status": fulfil["status"], "rights_status": right["status"],
            "surfaces": {
                "deposit_reserves_capacity": right["deposit_ok"] and right["allocated"],
                "tail_paid_in_window": right["tail_ok"],
                "overpayment_creates_debt": right["overpayment_debt"] > 0,
                "late_tail_releases_right": right.get("tail_late", False) and not right["tail_ok"],
                "parcel_matches_frozen_batch": bool(fulfil["valid_parcels"]),
                "partial_receipt_keeps_obligation": 0 < fulfil["delivered_units"] < fulfil["ordered_units"],
                "late_return_reopens_refund": fulfil["returned_units"] > 0 and fulfil["refund_due"] > 0,
            },
            "paid_total": right["paid_total"],
            "consent_required": right["consent_required"],
            "consent_ok": right.get("consent_ok", False),
            **fulfil,
        }
    return result

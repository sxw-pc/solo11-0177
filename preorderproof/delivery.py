"""Carrier handoff, signed quantities, returns, and remedy decisions."""
from __future__ import annotations

from collections import defaultdict

from .rights import _unique_latest
from .store import rows


def order_delivery_outcomes(conn) -> dict[str, dict]:
    """Final per-order delivery outcome from the same SQLite snapshot.

    Built for the privacy research release: parcels and receipts collapse by
    identity (latest event wins, as in fulfilment accounting), and repeated
    signings or a late return after signing merge into one outcome per order,
    so one order never counts as two people. Only receipts witnessed by the
    order's own consumer count, mirroring fulfilment_accounts. The outcome
    keeps the exact received_at for internal use; the release layer is
    responsible for generalising it before anything is published.
    """
    orders, _ = _unique_latest(
        rows(conn, "orders"), "order_id", ("offer_id", "units", "placed_at", "consumer"))
    parcels, _ = _unique_latest(
        rows(conn, "parcels"), "parcel_id", ("order_id", "batch_id", "units", "dispatched_at", "carrier"))
    receipts, _ = _unique_latest(
        rows(conn, "receipts"), "receipt_id", ("parcel_id", "units", "receipt_type", "received_at", "witness"))
    parcel_order = {parcel_id: parcel["order_id"] for parcel_id, parcel in parcels.items()}
    by_order = defaultdict(list)
    for receipt in receipts.values():
        order_id = parcel_order.get(receipt["parcel_id"])
        order = orders.get(order_id) if order_id else None
        if order and receipt["witness"] == order["consumer"]:
            by_order[order_id].append(receipt)
    outcomes = {}
    for order_id, order_receipts in by_order.items():
        latest = max((r["received_at"], r["receipt_id"]) for r in order_receipts)[0]
        outcomes[order_id] = {
            "status": "returned" if any(r["receipt_type"] == "returned" for r in order_receipts) else "signed",
            "received_at": latest,
            "receipt_ids": sorted(r["receipt_id"] for r in order_receipts),
        }
    return outcomes


def fulfilment_accounts(conn, offers: dict[str, dict], orders: dict[str, dict], as_of: str,
                       restocked_by_order: dict[str, int] | None = None) -> dict[str, dict]:
    parcels, parcel_conflicts = _unique_latest(
        rows(conn, "parcels"), "parcel_id", ("order_id", "batch_id", "units", "dispatched_at", "carrier")
    )
    receipts, receipt_conflicts = _unique_latest(
        rows(conn, "receipts"), "receipt_id", ("parcel_id", "units", "receipt_type", "received_at", "witness")
    )
    by_parcel = defaultdict(list)
    for receipt in receipts.values():
        if receipt["receipt_id"] not in receipt_conflicts:
            by_parcel[receipt["parcel_id"]].append(receipt)
    by_order = defaultdict(list)
    for parcel in parcels.values():
        by_order[parcel["order_id"]].append(parcel)
    result = {}
    for order_id, right in orders.items():
        order = right["order"]
        offer_state = offers.get(order["offer_id"])
        units = order["units"]
        valid_parcels = []
        invalid_parcels = []
        signed = returned = 0
        valid_returns = []
        for parcel in sorted(by_order[order_id], key=lambda x: (x["dispatched_at"], x["parcel_id"])):
            reason = None
            if parcel["parcel_id"] in parcel_conflicts:
                reason = "parcel_identity_conflict"
            elif not offer_state or parcel["batch_id"] != offer_state["offer"]["batch_id"]:
                reason = "wrong_batch"
            elif not right["allocated"] or not right["tail_ok"]:
                reason = "no_paid_delivery_right"
            elif parcel["carrier"] in {"merchant", "unwitnessed"}:
                reason = "no_independent_carrier"
            elif sum(p["units"] for p in valid_parcels) + parcel["units"] > units:
                reason = "over_dispatched"
            if reason:
                invalid_parcels.append({"parcel_id": parcel["parcel_id"], "reason": reason})
                continue
            valid_parcels.append(parcel)
            for receipt in by_parcel[parcel["parcel_id"]]:
                if receipt["witness"] != order["consumer"]:
                    continue
                if receipt["receipt_type"] == "signed":
                    signed += receipt["units"]
                else:
                    returned += receipt["units"]
                    valid_returns.append({"receipt_id": receipt["receipt_id"], "units": receipt["units"],
                                          "batch_id": parcel["batch_id"], "received_at": receipt["received_at"]})
        delivered = max(0, min(units, signed - returned))
        receipt_overcount = signed > sum(p["units"] for p in valid_parcels) or returned > signed
        if receipt_overcount:
            delivered = 0
        restocked = min((restocked_by_order or {}).get(order_id, 0), returned)
        outstanding = max(0, units - delivered - restocked)
        due = offer_state["offer"]["due"] if offer_state else None
        past_due = bool(due and as_of > due)
        price = offer_state["offer"]["price"] if offer_state else 0
        # Amounts are minor currency units. A lost right refunds the paid balance;
        # an otherwise valid but late part earns a bounded contract remedy.
        right_lost = right["status"] in {
            "promise_version_conflict", "offer_withdrawn", "batch_conflict", "batch_unavailable",
            "new_due_unaccepted", "order_conflict", "paid_waitlist", "unknown_offer",
            "deposit_missing_or_late", "tail_expired",
        }
        refund_due = right["paid_total"] if right_lost else min(
            right["paid_total"], restocked * price + (outstanding * price if past_due else 0)
        )
        compensation = min(outstanding * price // 10, 5000) if past_due and right["allocated"] and right["tail_ok"] else 0
        status = ("refund_due" if right_lost and right["paid_total"] else
                  "right_blocked" if right_lost else
                  "fulfilled" if delivered == units else
                  "return_refund" if restocked and not outstanding else
                  "partial_late" if delivered and past_due else
                  "partial" if delivered else
                  "late" if past_due else right["status"])
        result[order_id] = {
            "status": status, "ordered_units": units, "delivered_units": delivered,
            "outstanding_units": outstanding, "signed_units": signed,
            "returned_units": returned, "receipt_overcount": receipt_overcount,
            "restocked_units": restocked, "valid_returns": valid_returns,
            "valid_parcels": [p["parcel_id"] for p in valid_parcels],
            "invalid_parcels": invalid_parcels, "refund_due": refund_due,
            "compensation_due": compensation, "overpayment_debt": right["overpayment_debt"],
            "past_due": past_due,
        }
    return result

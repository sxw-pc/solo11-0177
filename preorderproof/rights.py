"""Consumer deposit, balance, consent, and capacity rights."""
from __future__ import annotations

from collections import defaultdict
from .store import rows


def _unique_latest(items: list[dict], key: str, fields: tuple[str, ...]) -> tuple[dict[str, dict], set[str]]:
    grouped = defaultdict(list)
    for item in items:
        grouped[item[key]].append(item)
    conflicts = {identity for identity, group in grouped.items()
                 if len({tuple(x[f] for f in fields) for x in group}) > 1}
    return {identity: sorted(group, key=lambda x: x["event_id"])[-1]
            for identity, group in grouped.items()}, conflicts


def order_accounts(conn, offers: dict[str, dict], as_of: str,
                   recovered_capacity: dict[str, int] | None = None) -> dict[str, dict]:
    orders, order_conflicts = _unique_latest(rows(conn, "orders"), "order_id", ("offer_id", "units", "placed_at"))
    payments, payment_conflicts = _unique_latest(rows(conn, "payments"), "txid", ("order_id", "stage", "amount", "paid_at"))
    consent_rows = rows(conn, "consents")
    by_consent = defaultdict(list)
    for item in consent_rows:
        by_consent[item["order_id"]].append(item)
    by_payment = defaultdict(list)
    for payment in payments.values():
        if payment["txid"] not in payment_conflicts:
            by_payment[payment["order_id"]].append(payment)
    staged = {}
    for order_id, order in orders.items():
        offer_state = offers.get(order["offer_id"])
        if not offer_state:
            staged[order_id] = {"order": order, "status": "unknown_offer", "allocated": False,
                                "paid_total": 0, "overpayment_debt": 0, "consent_required": False,
                                "deposit_ok": False, "tail_ok": False, "deposit_at": "9999-12-31"}
            continue
        offer = offer_state["offer"]
        valid = by_payment.get(order_id, [])
        deposits = [p for p in valid if p["stage"] == "deposit" and p["paid_at"] <= offer["deposit_deadline"]]
        tails = [p for p in valid if p["stage"] == "tail" and p["paid_at"] <= offer["tail_deadline"]]
        deposit_sum = sum(p["amount"] for p in deposits)
        tail_sum = sum(p["amount"] for p in tails)
        all_paid = sum(p["amount"] for p in valid)
        deposit_due = offer["deposit"] * order["units"]
        tail_due = (offer["price"] - offer["deposit"]) * order["units"]
        consent_required = (offer_state["due_change_days"] > offer["grace_days"]
                            and order["placed_at"] < offer["event_at"])
        consent_ok = not consent_required or any(
            c["due"] == offer["due"] and c["consumer"] not in {"merchant", ""}
            and c["accepted_at"] <= offer["due"] for c in by_consent[order_id]
        )
        deposit_ok = deposit_sum >= deposit_due
        tail_ok = tail_sum >= tail_due
        extra = max(0, all_paid - offer["price"] * order["units"])
        staged[order_id] = {
            "order": order, "status": "pending", "allocated": False,
            "paid_total": all_paid, "overpayment_debt": extra,
            "consent_required": consent_required, "consent_ok": consent_ok,
            "deposit_ok": deposit_ok, "tail_ok": tail_ok,
            "deposit_at": min((p["paid_at"] for p in deposits), default="9999-12-31"),
            "invalid_payment_txids": sorted(payment_conflicts & {p["txid"] for p in rows(conn, "payments") if p["order_id"] == order_id}),
            "deposit_late": bool([p for p in valid if p["stage"] == "deposit" and p["paid_at"] > offer["deposit_deadline"]]),
            "tail_late": bool([p for p in valid if p["stage"] == "tail" and p["paid_at"] > offer["tail_deadline"]]),
        }
        if order_id in order_conflicts:
            staged[order_id]["status"] = "order_conflict"
        elif not offer_state["active"]:
            staged[order_id]["status"] = offer_state["reason"]
        elif not deposit_ok:
            staged[order_id]["status"] = "deposit_missing_or_late"
        elif not consent_ok:
            staged[order_id]["status"] = "new_due_unaccepted"
        elif not tail_ok and as_of > offer["tail_deadline"]:
            staged[order_id]["status"] = "tail_expired"
    for offer_id, state in offers.items():
        candidates = [(oid, acc) for oid, acc in staged.items()
                      if acc["order"]["offer_id"] == offer_id and acc["status"] == "pending"]
        remaining = state["capacity"] + (recovered_capacity or {}).get(offer_id, 0)
        for order_id, acc in sorted(candidates, key=lambda item: (item[1]["deposit_at"], item[0])):
            units = acc["order"]["units"]
            if remaining < units:
                acc["status"] = "paid_waitlist"
                continue
            remaining -= units
            acc["allocated"] = True
            acc["status"] = "ready_to_dispatch" if acc["tail_ok"] else "tail_due_or_late"
        state["unallocated_capacity"] = remaining
    return staged

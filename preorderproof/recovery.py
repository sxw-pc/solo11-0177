"""Independent inspection binds a physical return to reusable batch capacity."""
from __future__ import annotations

from collections import defaultdict

from .store import rows


def inspected_returns(conn, offers: dict, rights: dict, fulfilment: dict) -> tuple[dict, dict, list]:
    available_returns = {}
    for order_id, decision in fulfilment.items():
        if decision["receipt_overcount"]:
            continue
        for item in decision["valid_returns"]:
            available_returns[item["receipt_id"]] = (order_id, item)
    by_review = defaultdict(list)
    for review in rows(conn, "restocks"):
        by_review[review["review_id"]].append(review)
    recovered = defaultdict(int)
    by_order = defaultdict(int)
    applied = defaultdict(int)
    log = []
    for review_id, group in sorted(by_review.items()):
        review = sorted(group, key=lambda x: x["event_id"])[-1]
        if len({(x["receipt_id"], x["batch_id"], x["units"], x["inspected_at"], x["outcome"])
                for x in group}) > 1:
            log.append({"review_id": review_id, "status": "review_conflict"})
            continue
        linked = available_returns.get(review["receipt_id"])
        if not linked:
            log.append({"review_id": review_id, "status": "return_not_proven"})
            continue
        order_id, returned = linked
        right = rights[order_id]
        offer_id = right["order"]["offer_id"]
        offer_state = offers[offer_id]
        if review["outcome"] != "accepted":
            reason = "inspection_rejected"
        elif review["batch_id"] != returned["batch_id"]:
            reason = "wrong_return_batch"
        elif review["inspected_at"] < returned["received_at"]:
            reason = "inspection_before_return"
        elif review["inspected_at"] > offer_state["offer"]["due"]:
            reason = "after_promise_due"
        elif not offer_state["active"]:
            reason = "batch_or_promise_unavailable"
        elif applied[review["receipt_id"]] + review["units"] > returned["units"]:
            reason = "return_overcredited"
        else:
            reason = "recovered"
            applied[review["receipt_id"]] += review["units"]
            recovered[offer_id] += review["units"]
            by_order[order_id] += review["units"]
        log.append({"review_id": review_id, "receipt_id": review["receipt_id"],
                    "offer_id": offer_id, "order_id": order_id, "status": reason,
                    "units": review["units"] if reason == "recovered" else 0})
    return dict(recovered), dict(by_order), log

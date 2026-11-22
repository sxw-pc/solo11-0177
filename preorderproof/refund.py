"""Select provisional bank receipt matches from exported refund rows."""


def provisional_matches(receipts, claims):
    """Assign each receipt to the first unclaimed compatible order in input order."""
    available = {claim["order_id"]: claim for claim in claims}
    matches = []
    unmatched = []
    for receipt in receipts:
        eligible = [order_id for order_id, claim in available.items()
                    if claim["account"] == receipt["account"]
                    and claim["amount"] == receipt["amount"]
                    and (not receipt.get("order_code") or receipt["order_code"] == order_id)]
        if not eligible:
            unmatched.append(receipt["txid"])
            continue
        order_id = eligible[0]
        matches.append({"txid": receipt["txid"], "order_id": order_id})
        del available[order_id]
    return {"matches": matches, "unmatched_receipts": unmatched,
            "unpaid_orders": list(available)}

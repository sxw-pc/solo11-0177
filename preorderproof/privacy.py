"""Provisional pseudonymous order export for analytics."""

import hashlib


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

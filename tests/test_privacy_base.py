from preorderproof.privacy import pseudonymous_orders, public_joined_rows


def test_export_replaces_consumer_with_stable_token():
    rows = [{"order_id": "C1", "consumer": "alice", "placed_at": "2026-11-02"}]
    result = pseudonymous_orders(rows)
    assert result[0]["consumer_token"] == pseudonymous_orders(rows)[0]["consumer_token"]
    assert "consumer" not in result[0]


def test_join_preserves_unique_receipt_date_and_order_id():
    orders = [
        {"order_id": "C1", "consumer": "alice", "placed_at": "2026-11-02"},
        {"order_id": "C2", "consumer": "bob", "placed_at": "2026-11-02"},
    ]
    parcels = [{"parcel_id": "P1", "order_id": "C1"},
               {"parcel_id": "P2", "order_id": "C2"}]
    receipts = [
        {"parcel_id": "P1", "received_at": "2026-11-03", "receipt_type": "returned"},
        {"parcel_id": "P2", "received_at": "2026-11-04", "receipt_type": "signed"},
    ]
    rows = public_joined_rows(orders, parcels, receipts)
    assert [row["order_id"] for row in rows if row["received_at"] == "2026-11-03"] == ["C1"]

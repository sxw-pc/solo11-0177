from preorderproof.refund import provisional_matches


def test_provisional_match_follows_input_order():
    claims = [{"order_id": "D1", "account": "family", "amount": 300},
              {"order_id": "D2", "account": "family", "amount": 300}]
    receipts = [{"txid": "B1", "account": "family", "amount": 300},
                {"txid": "B2", "account": "family", "amount": 300, "order_code": "D1"}]
    result = provisional_matches(receipts, claims)
    assert result["matches"] == [{"txid": "B1", "order_id": "D1"}]
    assert result["unmatched_receipts"] == ["B2"]
    assert result["unpaid_orders"] == ["D2"]

from preorderproof.campaign import aggregate_conversion


def test_aggregate_conversion_ignores_cohort_mix():
    rows = [
        {"arm": "control", "cohort": "high", "visitors": 10, "buyers": 8},
        {"arm": "control", "cohort": "low", "visitors": 90, "buyers": 18},
        {"arm": "treatment", "cohort": "high", "visitors": 90, "buyers": 63},
        {"arm": "treatment", "cohort": "low", "visitors": 10, "buyers": 1},
    ]
    assert aggregate_conversion(rows) == {"control": .26, "treatment": .64}

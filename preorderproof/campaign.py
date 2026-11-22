"""Provisional campaign conversion calculation."""


def aggregate_conversion(rows):
    totals = {}
    for row in rows:
        arm = row["arm"]
        slot = totals.setdefault(arm, {"visitors": 0, "buyers": 0})
        slot["visitors"] += row["visitors"]
        slot["buyers"] += row["buyers"]
    return {arm: item["buyers"] / item["visitors"] for arm, item in totals.items()}

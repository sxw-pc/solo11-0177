from preorderproof.route import estimate_minutes


def test_nominal_shortest_path():
    edges = [{"id": "AB", "from": "A", "to": "B", "nominal_minutes": 20},
             {"id": "BD", "from": "B", "to": "D", "nominal_minutes": 10},
             {"id": "AC", "from": "A", "to": "C", "nominal_minutes": 40},
             {"id": "CD", "from": "C", "to": "D", "nominal_minutes": 20}]
    assert estimate_minutes(edges, "A", "D") == {"minutes": 30, "edge_ids": ["AB", "BD"]}

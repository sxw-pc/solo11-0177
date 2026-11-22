"""Provisional carrier route time estimate."""


def estimate_minutes(edges, node, destination):
    """Find a shortest path using printed nominal minutes only."""
    frontier = [(0, node, [])]
    while frontier:
        frontier.sort(key=lambda item: item[0])
        minutes, current, path = frontier.pop(0)
        if current == destination:
            return {"minutes": minutes, "edge_ids": path}
        for edge in edges:
            if edge["from"] == current and edge["id"] not in path:
                frontier.append((minutes + edge["nominal_minutes"], edge["to"], path + [edge["id"]]))
    return None

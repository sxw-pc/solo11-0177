"""Recompute the report from raw evidence and compare persisted projections."""
from __future__ import annotations

import json

from .decisions import evaluate
from .store import canonical, digest, project


def report(conn) -> dict:
    decisions = evaluate(conn)
    history = [dict(x) for x in conn.execute(
        "SELECT subject, revision, decision, evidence_hash FROM decision_history ORDER BY subject, revision"
    )]
    return {
        "event_count": conn.execute("SELECT COUNT(*) FROM source_events").fetchone()[0],
        "decisions": decisions,
        "history": [{**item, "decision": json.loads(item["decision"])} for item in history],
    }


def audit(conn) -> dict:
    before = {x["subject"]: json.loads(x["decision"]) for x in conn.execute("SELECT * FROM current_decisions")}
    with conn:
        project(conn)
        after = evaluate(conn)
    return {"ok": before == after, "subjects": sorted(after),
            "evidence_hash": digest([dict(x) for x in conn.execute("SELECT * FROM source_events ORDER BY event_id")]),
            "changed": sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))}

"""Provisional business-date incremental export."""

from .store import digest


def export_since(conn, last_event_at):
    rows = [dict(row) for row in conn.execute(
        "SELECT event_id,event_at,body,sha256 FROM source_events "
        "WHERE event_at>? ORDER BY event_at,event_id", (last_event_at,)
    )]
    watermark = max((row["event_at"] for row in rows), default=last_event_at)
    return {"watermark": watermark, "events": rows, "root": digest(rows)}

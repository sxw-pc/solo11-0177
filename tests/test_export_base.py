from preorderproof.export import export_since
from preorderproof.store import connect


def test_business_date_watermark_skips_late_old_date():
    conn = connect(":memory:")
    conn.execute("INSERT INTO source_events VALUES(?,?,?,?,?,?)",
                 ("z-2", "payment", "payment", "2026-11-24", "{}", "sha-z"))
    first = export_since(conn, "")
    assert [row["event_id"] for row in first["events"]] == ["z-2"]
    conn.execute("INSERT INTO source_events VALUES(?,?,?,?,?,?)",
                 ("a-1", "payment", "payment", "2026-11-03", "{}", "sha-a"))
    assert export_since(conn, first["watermark"])["events"] == []
    conn.close()

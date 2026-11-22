import json
from pathlib import Path

from preorderproof.schema_import import ingest_offer_v2
from preorderproof.store import connect, ingest


def test_alias_adapter_drops_raw_v2_envelope():
    events = json.loads((Path(__file__).parents[1] / "fixtures" / "offer-ok.json").read_text())
    conn = connect(":memory:")
    ingest(conn, "offer", events)
    v2 = dict(events[1])
    v2["schema_version"] = 2
    v2["due_date"] = v2.pop("due")
    v2["price_cents"] = v2.pop("price")
    result = ingest_offer_v2(conn, v2)
    assert result["replayed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM source_events").fetchone()[0] == 2
    assert "schema_version" not in conn.execute(
        "SELECT body FROM source_events WHERE event_id=?", (v2["event_id"],)
    ).fetchone()[0]
    conn.close()

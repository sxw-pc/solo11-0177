"""Immutable source ledger, transactional projections, and decision history."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS source_events (
  event_id TEXT PRIMARY KEY, subsystem TEXT NOT NULL, kind TEXT NOT NULL,
  event_at TEXT NOT NULL, body TEXT NOT NULL, sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS offers (
  event_id TEXT PRIMARY KEY, offer_id TEXT NOT NULL, version INTEGER NOT NULL,
  batch_id TEXT NOT NULL, capacity INTEGER NOT NULL, price INTEGER NOT NULL,
  deposit INTEGER NOT NULL, due TEXT NOT NULL, deposit_deadline TEXT NOT NULL,
  tail_deadline TEXT NOT NULL, grace_days INTEGER NOT NULL, status TEXT NOT NULL,
  event_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
  event_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, quantity INTEGER NOT NULL,
  certified_until TEXT NOT NULL, status TEXT NOT NULL, event_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS restocks (
  event_id TEXT PRIMARY KEY, review_id TEXT NOT NULL, receipt_id TEXT NOT NULL,
  batch_id TEXT NOT NULL, units INTEGER NOT NULL, inspected_at TEXT NOT NULL,
  outcome TEXT NOT NULL, inspector TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  event_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, offer_id TEXT NOT NULL,
  units INTEGER NOT NULL, placed_at TEXT NOT NULL, consumer TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
  event_id TEXT PRIMARY KEY, txid TEXT NOT NULL, order_id TEXT NOT NULL,
  stage TEXT NOT NULL, amount INTEGER NOT NULL, paid_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consents (
  event_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, due TEXT NOT NULL,
  accepted_at TEXT NOT NULL, consumer TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS parcels (
  event_id TEXT PRIMARY KEY, parcel_id TEXT NOT NULL, order_id TEXT NOT NULL,
  batch_id TEXT NOT NULL, units INTEGER NOT NULL, dispatched_at TEXT NOT NULL,
  carrier TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
  event_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL, parcel_id TEXT NOT NULL,
  units INTEGER NOT NULL, receipt_type TEXT NOT NULL, received_at TEXT NOT NULL,
  witness TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS current_decisions (
  subject TEXT PRIMARY KEY, decision TEXT NOT NULL, evidence_hash TEXT NOT NULL,
  revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS decision_history (
  subject TEXT NOT NULL, revision INTEGER NOT NULL, decision TEXT NOT NULL,
  evidence_hash TEXT NOT NULL, PRIMARY KEY(subject, revision)
);
CREATE TABLE IF NOT EXISTS manual_pages (
  event_id TEXT PRIMARY KEY, page_id TEXT NOT NULL, source TEXT NOT NULL,
  language TEXT NOT NULL, edition TEXT NOT NULL, url TEXT NOT NULL,
  http_status TEXT NOT NULL, digest TEXT NOT NULL, redirects_to TEXT NOT NULL,
  event_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_anchors (
  event_id TEXT PRIMARY KEY, page_id TEXT NOT NULL, anchor TEXT NOT NULL,
  theme_uid TEXT NOT NULL, title TEXT NOT NULL, event_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_revisions (
  event_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL,
  product TEXT NOT NULL, edition TEXT NOT NULL, language TEXT NOT NULL,
  relation TEXT NOT NULL, from_theme_uid TEXT NOT NULL, to_theme_uid TEXT NOT NULL,
  to_theme_uids TEXT NOT NULL, section_uid TEXT NOT NULL, note TEXT NOT NULL,
  signed_by TEXT NOT NULL, digest TEXT NOT NULL, event_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_qrcodes (
  event_id TEXT PRIMARY KEY, qr_code TEXT NOT NULL, offer_id TEXT NOT NULL,
  edition TEXT NOT NULL, language TEXT NOT NULL, section_uid TEXT NOT NULL,
  printed_page TEXT NOT NULL, source_page_id TEXT NOT NULL, event_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_body_links (
  event_id TEXT PRIMARY KEY, link_id TEXT NOT NULL, language TEXT NOT NULL,
  edition TEXT NOT NULL, section_uid TEXT NOT NULL, target_ref TEXT NOT NULL,
  source_page_id TEXT NOT NULL, source_anchor TEXT NOT NULL, event_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_routes (
  event_id TEXT PRIMARY KEY, qr_code TEXT NOT NULL, strategy TEXT NOT NULL,
  language TEXT NOT NULL, first_url TEXT NOT NULL, status TEXT NOT NULL,
  supersedes TEXT NOT NULL, event_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_route_entries (
  event_id TEXT PRIMARY KEY, route_event_id TEXT NOT NULL,
  theme_uid TEXT NOT NULL, url TEXT NOT NULL, ordinal TEXT NOT NULL,
  event_at TEXT NOT NULL
);
"""

KIND_SUBSYSTEM = {
    "offer": "offer", "batch": "offer", "restock": "offer", "order": "payment",
    "payment": "payment", "consent": "payment", "parcel": "delivery",
    "receipt": "delivery", "manual_page": "manual", "manual_anchor": "manual",
    "manual_revision": "manual", "manual_qrcode": "manual",
    "manual_body_link": "manual", "manual_route": "manual",
    "manual_route_entry": "manual",
}

# Optional text fields per manual event kind: absent or "" is legal, anything
# present must still be nonempty text. Business events have no optional fields.
OPTIONAL_FIELDS = {
    "manual_page": {"digest", "redirects_to"},
    "manual_revision": {"note", "digest", "to_theme_uid"},
    "manual_qrcode": {"offer_id"},
    "manual_route": {"supersedes"},
}

PROJECTIONS = {
    "offer": ("offers", "offer_id", "version", "batch_id", "capacity", "price", "deposit",
              "due", "deposit_deadline", "tail_deadline", "grace_days", "status"),
    "batch": ("batches", "batch_id", "quantity", "certified_until", "status"),
    "restock": ("restocks", "review_id", "receipt_id", "batch_id", "units", "inspected_at", "outcome", "inspector"),
    "order": ("orders", "order_id", "offer_id", "units", "placed_at", "consumer"),
    "payment": ("payments", "txid", "order_id", "stage", "amount", "paid_at"),
    "consent": ("consents", "order_id", "due", "accepted_at", "consumer"),
    "parcel": ("parcels", "parcel_id", "order_id", "batch_id", "units", "dispatched_at", "carrier"),
    "receipt": ("receipts", "receipt_id", "parcel_id", "units", "receipt_type", "received_at", "witness"),
}

# Manual-evidence ledger. Rows are an append-only projection of captured
# supplier artefacts (PDF/web pages, OCR'd anchors, signed chapter revisions,
# printed QR codes, in-body hyperlinks and authorised old-code routes). The
# offer/payment/delivery ledgers never read these tables: a manual revision
# cannot recompute offer capacity, order rights, or refunds.
MANUAL_PROJECTIONS = {
    "manual_page": ("manual_pages", "page_id", "source", "language", "edition",
                    "url", "http_status", "digest", "redirects_to"),
    "manual_anchor": ("manual_anchors", "page_id", "anchor", "theme_uid", "title"),
    "manual_revision": ("manual_revisions", "revision_id", "product", "edition",
                        "language", "relation", "from_theme_uid", "to_theme_uid",
                        "to_theme_uids", "section_uid", "note", "signed_by", "digest"),
    "manual_qrcode": ("manual_qrcodes", "qr_code", "offer_id", "edition", "language",
                      "section_uid", "printed_page", "source_page_id"),
    "manual_body_link": ("manual_body_links", "link_id", "language", "edition",
                         "section_uid", "target_ref", "source_page_id", "source_anchor"),
    "manual_route": ("manual_routes", "qr_code", "strategy", "language", "first_url",
                     "status", "supersedes"),
    "manual_route_entry": ("manual_route_entries", "route_event_id", "theme_uid",
                           "url", "ordinal"),
}

MANUAL_TABLES = {spec[0] for spec in MANUAL_PROJECTIONS.values()}


def connect(db: str | Path) -> sqlite3.Connection:
    db = str(db)
    if db != ":memory:":
        Path(db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    allowed = {"offers", "batches", "restocks", "orders", "payments", "consents",
               "parcels", "receipts"} | MANUAL_TABLES
    if table not in allowed:
        raise ValueError("unknown table")
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY event_id")]


def project(conn: sqlite3.Connection) -> None:
    """Rebuild all business entities from immutable events; late evidence is ordinary input."""
    for table in {spec[0] for spec in PROJECTIONS.values()} | MANUAL_TABLES:
        conn.execute(f"DELETE FROM {table}")
    events = conn.execute(
        "SELECT * FROM source_events ORDER BY event_at, event_id"
    ).fetchall()
    for event in events:
        data = json.loads(event["body"])
        if event["kind"] in MANUAL_PROJECTIONS:
            table, *columns = MANUAL_PROJECTIONS[event["kind"]]
            values = [event["event_id"], *[data.get(col, "") for col in columns], event["event_at"]]
            names = ["event_id", *columns, "event_at"]
        else:
            table, *columns = PROJECTIONS[event["kind"]]
            values = [event["event_id"], *[data[col] for col in columns]]
            names = ["event_id", *columns]
            if table in {"offers", "batches"}:
                values.append(event["event_at"])
                names.append("event_at")
        q = ",".join("?" for _ in names)
        conn.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({q})", values)


def record_decisions(conn: sqlite3.Connection, decisions: dict[str, dict]) -> None:
    event_hash = digest([dict(x) for x in conn.execute("SELECT * FROM source_events ORDER BY event_id")])
    for subject, result in sorted(decisions.items()):
        body = canonical(result)
        old = conn.execute("SELECT decision, revision FROM current_decisions WHERE subject=?", (subject,)).fetchone()
        if old and old["decision"] == body:
            continue
        revision = (old["revision"] if old else 0) + 1
        conn.execute(
            "INSERT INTO decision_history VALUES (?,?,?,?)",
            (subject, revision, body, event_hash),
        )
        conn.execute(
            "INSERT INTO current_decisions VALUES (?,?,?,?) ON CONFLICT(subject) DO UPDATE SET "
            "decision=excluded.decision,evidence_hash=excluded.evidence_hash,revision=excluded.revision",
            (subject, body, event_hash, revision),
        )


def ingest(conn: sqlite3.Connection, subsystem: str, events: list[dict]) -> dict:
    from .validate import validate_event
    from .decisions import evaluate

    if not isinstance(events, list) or not events:
        raise ValueError("input must be a nonempty JSON event list")
    inserted = replayed = 0
    with conn:
        for event in events:
            validate_event(event, subsystem)
            event_id = event["event_id"]
            body = canonical(event)
            sha = digest(event)
            prior = conn.execute("SELECT sha256 FROM source_events WHERE event_id=?", (event_id,)).fetchone()
            if prior:
                if prior["sha256"] != sha:
                    raise ValueError(f"event_id collision: {event_id}")
                replayed += 1
                continue
            conn.execute(
                "INSERT INTO source_events VALUES (?,?,?,?,?,?)",
                (event_id, subsystem, event["kind"], event["event_at"], body, sha),
            )
            inserted += 1
        project(conn)
        record_decisions(conn, evaluate(conn))
    return {"inserted": inserted, "replayed": replayed, "decisions":
            conn.execute("SELECT COUNT(*) FROM current_decisions").fetchone()[0]}

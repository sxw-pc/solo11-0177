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
CREATE TABLE IF NOT EXISTS campaign_plans (
  event_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, sampling_unit TEXT NOT NULL,
  stats_cutoff TEXT NOT NULL, locked_at TEXT NOT NULL, assignment_root TEXT NOT NULL,
  signature TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_pages (
  event_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, arm TEXT NOT NULL,
  cohort TEXT NOT NULL, cursor TEXT NOT NULL, stats_cutoff TEXT NOT NULL,
  sampling_unit TEXT NOT NULL, signature TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_reports (
  subject TEXT NOT NULL, revision INTEGER NOT NULL, report TEXT NOT NULL,
  report_hash TEXT NOT NULL, supersedes TEXT NOT NULL, evidence_hash TEXT NOT NULL,
  PRIMARY KEY(subject, revision)
);
"""

KIND_SUBSYSTEM = {
    "offer": "offer", "batch": "offer", "restock": "offer", "order": "payment",
    "payment": "payment", "consent": "payment", "parcel": "delivery",
    "receipt": "delivery", "campaign_plan": "campaign",
    "assignment_page": "campaign", "exposure_page": "campaign",
    "order_page": "campaign", "page_cancel": "campaign", "page_reissue": "campaign",
}

_PAGE_PROJECTION = ("campaign_pages", "experiment_id", "arm", "cohort", "cursor",
                    "stats_cutoff", "sampling_unit", "signature")

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
    "campaign_plan": ("campaign_plans", "experiment_id", "sampling_unit", "stats_cutoff",
                      "locked_at", "assignment_root", "signature"),
    "assignment_page": _PAGE_PROJECTION,
    "exposure_page": _PAGE_PROJECTION,
    "order_page": _PAGE_PROJECTION,
    "page_cancel": _PAGE_PROJECTION,
    "page_reissue": _PAGE_PROJECTION,
}


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
    if table not in {"offers", "batches", "restocks", "orders", "payments", "consents", "parcels", "receipts"}:
        raise ValueError("unknown table")
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY event_id")]


def project(conn: sqlite3.Connection) -> None:
    """Rebuild all business entities from immutable events; late evidence is ordinary input."""
    for table in {spec[0] for spec in PROJECTIONS.values()}:
        conn.execute(f"DELETE FROM {table}")
    events = conn.execute(
        "SELECT * FROM source_events ORDER BY event_at, event_id"
    ).fetchall()
    for event in events:
        data = json.loads(event["body"])
        table, *columns = PROJECTIONS[event["kind"]]
        values = [event["event_id"], *[data[col] for col in columns]]
        extra = ["event_at"] if table in {"offers", "batches"} else []
        if extra:
            values.append(event["event_at"])
        names = ["event_id", *columns, *extra]
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
    from .campaign import record_reports

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
        record_reports(conn)
    return {"inserted": inserted, "replayed": replayed, "decisions":
            conn.execute("SELECT COUNT(*) FROM current_decisions").fetchone()[0]}

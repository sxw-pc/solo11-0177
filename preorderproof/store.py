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
CREATE TABLE IF NOT EXISTS temp_pages (
  event_id TEXT PRIMARY KEY, page_id TEXT NOT NULL, recorder TEXT NOT NULL,
  boot INTEGER NOT NULL, page_no INTEGER NOT NULL, observed_at TEXT NOT NULL,
  ambient_c REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS clock_calibrations (
  event_id TEXT PRIMARY KEY, calibration_id TEXT NOT NULL, recorder TEXT NOT NULL,
  calibrated_at TEXT NOT NULL, offset_seconds INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sensor_placements (
  event_id TEXT PRIMARY KEY, placement_id TEXT NOT NULL, box_id TEXT NOT NULL,
  sensor TEXT NOT NULL, position TEXT NOT NULL, positioned_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slot_mappings (
  event_id TEXT PRIMARY KEY, mapping_id TEXT NOT NULL, box_id TEXT NOT NULL,
  recorder TEXT NOT NULL, slot TEXT NOT NULL, mapped_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exposure_certificates (
  event_id TEXT PRIMARY KEY, certificate_id TEXT NOT NULL, batch_id TEXT NOT NULL,
  box_id TEXT NOT NULL, units INTEGER NOT NULL, packaging TEXT NOT NULL,
  core_limit_c REAL NOT NULL, max_over_minutes INTEGER NOT NULL, issuer TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS thermal_calibrations (
  event_id TEXT PRIMARY KEY, calibration_id TEXT NOT NULL, packaging TEXT NOT NULL,
  tau_minutes REAL NOT NULL, calibrated_at TEXT NOT NULL, lab TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS buffer_samples (
  event_id TEXT PRIMARY KEY, buffer_id TEXT NOT NULL, recorder TEXT NOT NULL,
  observed_at TEXT NOT NULL, current_a REAL NOT NULL, ambient_c REAL NOT NULL,
  signature TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activity_assays (
  event_id TEXT PRIMARY KEY, assay_id TEXT NOT NULL, batch_id TEXT NOT NULL,
  box_id TEXT NOT NULL, units_consumed INTEGER NOT NULL, outcome TEXT NOT NULL,
  assayed_at TEXT NOT NULL, inspector TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS current_decisions (
  subject TEXT PRIMARY KEY, decision TEXT NOT NULL, evidence_hash TEXT NOT NULL,
  revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS decision_history (
  subject TEXT NOT NULL, revision INTEGER NOT NULL, decision TEXT NOT NULL,
  evidence_hash TEXT NOT NULL, PRIMARY KEY(subject, revision)
);
"""

KIND_SUBSYSTEM = {
    "offer": "offer", "batch": "offer", "restock": "offer", "order": "payment",
    "payment": "payment", "consent": "payment", "parcel": "delivery",
    "receipt": "delivery", "temp_page": "temperature",
    "clock_calibration": "temperature", "sensor_placement": "temperature",
    "slot_mapping": "temperature", "exposure_certificate": "temperature",
    "thermal_calibration": "temperature", "buffer_sample": "temperature",
    "activity_assay": "temperature",
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
    "temp_page": ("temp_pages", "page_id", "recorder", "boot", "page_no", "observed_at", "ambient_c"),
    "clock_calibration": ("clock_calibrations", "calibration_id", "recorder", "calibrated_at", "offset_seconds"),
    "sensor_placement": ("sensor_placements", "placement_id", "box_id", "sensor", "position", "positioned_at"),
    "slot_mapping": ("slot_mappings", "mapping_id", "box_id", "recorder", "slot", "mapped_at"),
    "exposure_certificate": ("exposure_certificates", "certificate_id", "batch_id", "box_id", "units",
                             "packaging", "core_limit_c", "max_over_minutes", "issuer"),
    "thermal_calibration": ("thermal_calibrations", "calibration_id", "packaging", "tau_minutes",
                            "calibrated_at", "lab"),
    "buffer_sample": ("buffer_samples", "buffer_id", "recorder", "observed_at", "current_a",
                      "ambient_c", "signature"),
    "activity_assay": ("activity_assays", "assay_id", "batch_id", "box_id", "units_consumed",
                       "outcome", "assayed_at", "inspector"),
}

TABLES = {spec[0] for spec in PROJECTIONS.values()}


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
    if table not in TABLES:
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

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
CREATE TABLE IF NOT EXISTS refund_sources (
  record_id TEXT PRIMARY KEY, section TEXT NOT NULL, kind TEXT NOT NULL,
  batch_id TEXT NOT NULL, body TEXT NOT NULL, sha256 TEXT NOT NULL,
  ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refund_sources_batch ON refund_sources(batch_id);
CREATE TABLE IF NOT EXISTS refund_certificates (
  cert_id TEXT PRIMARY KEY, scope TEXT NOT NULL, cert_type TEXT NOT NULL,
  protocol TEXT NOT NULL, status TEXT NOT NULL, batch_id TEXT NOT NULL,
  source_digest TEXT NOT NULL, graph_version TEXT NOT NULL,
  body TEXT NOT NULL, body_sha256 TEXT NOT NULL, issued_at TEXT NOT NULL,
  voided_at TEXT, void_reason TEXT, replaces_cert_id TEXT, voids_cert_id TEXT,
  replayed INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_refund_active_cert
  ON refund_certificates(scope, cert_type) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS refund_certificate_events (
  event_no INTEGER PRIMARY KEY AUTOINCREMENT, cert_id TEXT NOT NULL,
  event_type TEXT NOT NULL, occurred_at TEXT NOT NULL, detail TEXT NOT NULL
);
"""

REFUND_SECTIONS = ("claims", "bank_flows", "account_mappings",
                   "refund_codes", "authorizations", "revocations")
REFUND_CERT_TYPES = {"provisional", "settlement_revocable",
                     "bank_writeoff", "settlement_locked", "void"}

KIND_SUBSYSTEM = {
    "offer": "offer", "batch": "offer", "restock": "offer", "order": "payment",
    "payment": "payment", "consent": "payment", "parcel": "delivery",
    "receipt": "delivery",
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


# --------------------------------------------------------------------------- #
# Refund evidence ledger and externally issued certificate register.
# This ledger is append-only and disjoint from source_events/projections:
# the refund collector never rewrites old orders, payments, or physical
# delivery records; rights/delivery snapshots enter only as claim evidence.
# --------------------------------------------------------------------------- #
def ingest_refund_bundle(conn, bundle: dict, ingested_at: str) -> dict:
    from .refund import validate_bundle

    validate_bundle(bundle)
    inserted = replayed = 0
    with conn:
        for section in REFUND_SECTIONS:
            for record in bundle.get(section, []):
                record_id = record["record_id"]
                body = canonical(record)
                sha = digest(record)
                prior = conn.execute(
                    "SELECT sha256, section, kind FROM refund_sources WHERE record_id=?",
                    (record_id,),
                ).fetchone()
                if prior:
                    if prior["sha256"] != sha:
                        raise ValueError(f"refund record_id collision: {record_id}")
                    replayed += 1
                    continue
                conn.execute(
                    "INSERT INTO refund_sources VALUES (?,?,?,?,?,?,?)",
                    (record_id, section, record["kind"], bundle["batch_id"],
                     body, sha, ingested_at),
                )
                inserted += 1
    return {"inserted": inserted, "replayed": replayed,
            "total": conn.execute("SELECT COUNT(*) FROM refund_sources").fetchone()[0]}


def load_refund_bundle(conn, batch_id: str) -> dict:
    """Reconstruct the offline bundle deterministically from stored records."""
    rows_by_section = {section: [] for section in REFUND_SECTIONS}
    for row in conn.execute(
        "SELECT * FROM refund_sources WHERE batch_id=? ORDER BY record_id", (batch_id,)
    ):
        rows_by_section[row["section"]].append(json.loads(row["body"]))
    if not any(rows_by_section.values()):
        raise ValueError(f"no refund evidence stored for batch {batch_id}")
    return {"batch_id": batch_id, **rows_by_section}


def next_refund_cert_id(conn, prefix: str) -> str:
    count = conn.execute("SELECT COUNT(*) FROM refund_certificates").fetchone()[0]
    return f"{prefix}-{count + 1:04d}"


def issue_refund_certificate(conn, body: dict, *, ingested_at: str) -> dict:
    """Persist one active certificate; a duplicate (crash/replay) returns the prior one.

    The partial unique index guarantees that exactly one active certificate
    exists per (scope, cert_type), so interrupted recovery can never leave two
    valid certificates for the same settlement scope.
    """
    from .refund import certificate_fingerprint

    cert_id = body["cert_id"]
    scope, cert_type = body["scope"], body["cert_type"]
    if cert_type not in REFUND_CERT_TYPES or cert_type == "void":
        raise ValueError("invalid certificate type")
    prior = conn.execute(
        "SELECT cert_id, body_sha256 FROM refund_certificates "
        "WHERE scope=? AND cert_type=? AND status='active'",
        (scope, cert_type),
    ).fetchone()
    if prior:
        if prior["body_sha256"] != certificate_fingerprint(body):
            raise ValueError(
                f"active {cert_type} certificate already issued for scope {scope}; "
                "issue a voided replacement instead of overwriting it")
        stored = conn.execute(
            "SELECT * FROM refund_certificates WHERE cert_id=?", (prior["cert_id"],)
        ).fetchone()
        return {"cert_id": stored["cert_id"], "replayed": True,
                "body": json.loads(stored["body"])}
    body_sha = certificate_fingerprint(body)
    with conn:
        conn.execute(
            "INSERT INTO refund_certificates "
            "(cert_id, scope, cert_type, protocol, status, batch_id, source_digest, "
            "graph_version, body, body_sha256, issued_at, replayed) "
            "VALUES (?,?,?,?, 'active', ?, ?, ?, ?, ?, ?, 0)",
            (cert_id, scope, cert_type, body["protocol"], body["batch_id"],
             body["source_digest"], body["graph_version"], canonical(body),
             body_sha, ingested_at),
        )
        conn.execute(
            "INSERT INTO refund_certificate_events (cert_id, event_type, occurred_at, detail) "
            "VALUES (?, 'issued', ?, ?)",
            (cert_id, ingested_at, canonical({"scope": scope, "cert_type": cert_type,
                                              "body_sha256": body_sha})),
        )
    return {"cert_id": cert_id, "replayed": False, "body": body}


def void_refund_certificate(conn, cert_id: str, *, voided_at: str, reason: str,
                            replacement_cert_id: str | None = None) -> dict:
    """Mark an issued certificate void without deleting that it happened."""
    prior = conn.execute(
        "SELECT * FROM refund_certificates WHERE cert_id=?", (cert_id,)
    ).fetchone()
    if prior is None:
        raise ValueError(f"unknown certificate {cert_id}")
    if prior["status"] != "active":
        return {"cert_id": cert_id, "replayed": True, "status": prior["status"]}
    with conn:
        conn.execute(
            "UPDATE refund_certificates SET status='void', voided_at=?, void_reason=?, "
            "replaces_cert_id=? WHERE cert_id=?",
            (voided_at, reason, replacement_cert_id, cert_id),
        )
        conn.execute(
            "INSERT INTO refund_certificate_events (cert_id, event_type, occurred_at, detail) "
            "VALUES (?, 'voided', ?, ?)",
            (cert_id, voided_at, canonical(
                {"reason": reason, "replacement_cert_id": replacement_cert_id})),
        )
        if replacement_cert_id is not None:
            conn.execute(
                "UPDATE refund_certificates SET voids_cert_id=? WHERE cert_id=?",
                (cert_id, replacement_cert_id),
            )
            conn.execute(
                "INSERT INTO refund_certificate_events (cert_id, event_type, occurred_at, detail) "
                "VALUES (?, 'replacement_link', ?, ?)",
                (replacement_cert_id, voided_at, canonical({"voids_cert_id": cert_id})),
            )
    return {"cert_id": cert_id, "replayed": False, "status": "void",
            "replacement_cert_id": replacement_cert_id}


def get_active_certificate(conn, scope: str, cert_type: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM refund_certificates WHERE scope=? AND cert_type=? AND status='active'",
        (scope, cert_type),
    ).fetchone()
    return None if row is None else {**dict(row), "body": json.loads(row["body"])}


def list_refund_certificates(conn, batch_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM refund_certificates"
    params = ()
    if batch_id is not None:
        sql += " WHERE batch_id=?"
        params = (batch_id,)
    sql += " ORDER BY cert_id"
    return [{**dict(row), "body": json.loads(row["body"])}
            for row in conn.execute(sql, params)]


def list_refund_certificate_events(conn, cert_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM refund_certificate_events"
    params = ()
    if cert_id is not None:
        sql += " WHERE cert_id=?"
        params = (cert_id,)
    sql += " ORDER BY event_no"
    return [dict(row) for row in conn.execute(sql, params)]


def active_locked_edges(conn, scope: str) -> list[list[str]]:
    """Locked edges contributed by active non-transferable bank write-offs."""
    writeoff = get_active_certificate(conn, scope, "bank_writeoff")
    if writeoff is None:
        return []
    return [[pair["txid"], pair["order_id"]] for pair in writeoff["body"]["pairs"]]

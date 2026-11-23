"""Dual-layer ingestion for merchant v1/v2 paged offer files.

Layer 1 (raw receipt) keeps every original byte: file hash, page cursor, row locator,
producer signature and schema_version, plus the receive timestamp. Layer 2 (semantic)
normalizes units, dates and field aliases into deterministic offer/batch events and
records which raw rows explain each semantic event, and which raw rows were isolated.

Two producer-signed migration contracts are supported, never mixed inside one batch:

* ``reused_external_id`` (contract A): a revision page may keep the external event id
  but must carry a monotonic ``producer_revision`` and a ``supersedes`` raw-page digest.
  (producer, external id, revision) maps to a unique internal event id.
* ``unique_external_id`` (contract B): changed semantics must be sent under a fresh,
  non-reusable external event id referencing the old id via the reissue page. Unsigned
  rows that reuse an old id with changed semantics are quarantined, never overwritten.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .store import KIND_SUBSYSTEM, admit_semantic_event, canonical, digest
from .validate import FIELD_ALIASES, validate_page_envelope, validate_raw_row

CONTRACT_REUSED_ID = "reused_external_id"
CONTRACT_UNIQUE_ID = "unique_external_id"
LEGACY_CONTRACT = "legacy"


# --------------------------------------------------------------------------- #
# Backwards-compatible v2 alias adapter (baseline schema_import tests).
# --------------------------------------------------------------------------- #
def normalize_v2_offer(event):
    normalized = dict(event)
    if normalized.pop("schema_version", 1) == 2:
        normalized["due"] = normalized.pop("due_date")
        normalized["price"] = normalized.pop("price_cents")
    return normalized


def ingest_offer_v2(conn, event):
    from .store import ingest
    return ingest(conn, "offer", [normalize_v2_offer(event)])


# --------------------------------------------------------------------------- #
# Deterministic normalization.
# --------------------------------------------------------------------------- #
def normalize_row(row: dict, schema_version: int) -> dict:
    """Normalize one raw row to canonical v1 field names, dropping envelope dialect."""
    normalized = dict(row)
    normalized.pop("schema_version", None)
    for alias, target in FIELD_ALIASES[row["kind"]].items():
        if schema_version == 2 and alias in normalized:
            normalized[target] = normalized.pop(alias)
    return normalized


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def file_hash(envelope: dict) -> str:
    return hashlib.sha256(canonical(envelope).encode("utf-8")).hexdigest()


def generation_id_for(envelope: dict) -> str:
    return "gen-" + file_hash(envelope)[:16]


def _atomic_write(path: Path, payload: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    directory = path.parent
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# --------------------------------------------------------------------------- #
# Layer 1: raw receipt staging.
# --------------------------------------------------------------------------- #
def stage_pages(conn: sqlite3.Connection, envelope: dict,
                package_dir: str | Path | None = None,
                file_bytes: bytes | None = None) -> str:
    """Persist raw bytes of every page row. Never interprets semantics here."""
    problem = validate_page_envelope(envelope)
    if problem:
        raise ValueError(problem)
    envelope_bytes = file_bytes if file_bytes is not None else canonical(envelope).encode("utf-8")
    fhash = hashlib.sha256(envelope_bytes).hexdigest()
    generation_id = "gen-" + fhash[:16]
    received_at = _now()
    bytes_path = None
    if package_dir is not None:
        directory = Path(package_dir)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{generation_id}.json"
        if not target.exists():
            _atomic_write(target, envelope_bytes)
        bytes_path = str(target.resolve())
    with conn:
        existing = conn.execute(
            "SELECT generation_id FROM ingest_generations WHERE file_hash=?", (fhash,)
        ).fetchone()
        if existing:
            return existing["generation_id"]
        conn.execute(
            "INSERT INTO ingest_generations VALUES (?,?,?,?,?,?,?)",
            (generation_id, fhash, canonical(envelope), "staged",
             received_at, bytes_path, fhash),
        )
        for page in envelope["pages"]:
            metadata = page if isinstance(page, dict) else {}
            cursor = metadata.get("page_cursor")
            for row_no, raw_row in enumerate(metadata.get("rows", [])):
                receipt_id = f"{generation_id}:p{cursor}:r{row_no}"
                conn.execute(
                    "INSERT INTO raw_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (receipt_id, generation_id, cursor, row_no, envelope["producer"],
                     int(metadata.get("producer_revision", 1) or 1),
                     metadata.get("producer_signature"),
                     int(metadata.get("schema_version", envelope.get("schema_version", 1)) or 1),
                     canonical(raw_row), digest(raw_row), received_at),
                )
    return generation_id


def ingest_pages_file(conn: sqlite3.Connection, path: str | Path,
                      package_dir: str | Path | None = None) -> dict:
    source = Path(path)
    envelope = json.loads(source.read_bytes().decode("utf-8"))
    return ingest_pages(conn, envelope, package_dir=package_dir,
                        file_bytes=source.read_bytes())


# --------------------------------------------------------------------------- #
# Layer 2: semantic projection with producer identity and quarantine.
# --------------------------------------------------------------------------- #
def _quarantine(conn, receipt: sqlite3.Row, reason: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO quarantine VALUES (?,?,?,?,?)",
        (receipt["receipt_id"], receipt["generation_id"], reason, detail,
         receipt["received_at"]),
    )


def _link(conn, receipt: sqlite3.Row, internal_event_id: str,
          semantic_event_id: str, role: str) -> None:
    conn.execute(
        "INSERT INTO raw_semantic_links VALUES (?,?,?,?,?)",
        (receipt["receipt_id"], receipt["generation_id"], internal_event_id,
         semantic_event_id, role),
    )


def _known_revisions(conn, producer: str, external_id: str) -> dict[int, sqlite3.Row]:
    return {
        row["producer_revision"]: row
        for row in conn.execute(
            "SELECT * FROM event_identity_map WHERE producer=? AND external_event_id=?",
            (producer, external_id),
        ).fetchall()
    }


def _previous_anchor_digest(conn, map_row: sqlite3.Row) -> str | None:
    """Anchor digest a revision page must put in ``supersedes``.

    The revision chain is anchored on semantic content, not on byte dialect: the v1
    original and its synonym v2 replay share one normalized digest, so a producer
    signing the original v1 page digest produces the same anchor regardless of the
    portal's delivery order. Synonym replays never advance the revision number.
    """
    return map_row["normalized_sha256"]


def project_generation(conn: sqlite3.Connection, generation_id: str) -> dict:
    """Interpret one staged generation. Each raw row ends linked or quarantined."""
    generation = conn.execute(
        "SELECT * FROM ingest_generations WHERE generation_id=?", (generation_id,)
    ).fetchone()
    if not generation:
        raise ValueError(f"unknown generation: {generation_id}")
    envelope = json.loads(generation["envelope"])
    contract = envelope["identity_contract"]
    receipts = conn.execute(
        "SELECT * FROM raw_receipts WHERE generation_id=? ORDER BY page_cursor, row_no",
        (generation_id,),
    ).fetchall()
    summary = {"generation_id": generation_id, "linked": 0, "quarantined": 0,
               "semantic_inserted": 0, "semantic_replayed": 0, "details": []}
    with conn:
        # Re-projection must converge: clear prior interpretations, keep raw bytes.
        conn.execute("DELETE FROM raw_semantic_links WHERE generation_id=?", (generation_id,))
        conn.execute("DELETE FROM quarantine WHERE generation_id=?", (generation_id,))

        cursors = sorted({r["page_cursor"] for r in receipts})
        expected_cursor = 0
        first_gap = None
        for cursor in cursors:
            if cursor != expected_cursor:
                first_gap = expected_cursor
                break
            expected_cursor += 1

        seen_raw: dict[str, str] = {}
        seen_locator: dict[tuple, str] = {}
        normalized: dict[str, dict] = {}
        # Competing revisions: same (producer, external id, revision), different bytes.
        locator_groups: dict[tuple, list] = {}
        for receipt in receipts:
            row = json.loads(receipt["raw_body"])
            ext_id = row.get("event_id") if isinstance(row, dict) else None
            locator = (receipt["producer"], ext_id, receipt["producer_revision"])
            locator_groups.setdefault(locator, []).append(receipt)
        competing = {
            receipt["receipt_id"]
            for group in locator_groups.values()
            if len({r["raw_sha256"] for r in group}) > 1
            for receipt in group
        }

        for receipt in receipts:
            row = json.loads(receipt["raw_body"])
            if first_gap is not None and receipt["page_cursor"] >= first_gap:
                reason, detail = "broken_page", (
                    f"page cursor {first_gap} missing before this page")
            elif receipt["receipt_id"] in competing:
                reason, detail = "competing_revision", (
                    "two mutually exclusive revisions share producer/external-id/revision")
            elif receipt["raw_sha256"] in seen_raw:
                reason, detail = "duplicate_page", (
                    f"identical raw row already received as {seen_raw[receipt['raw_sha256']]}")
            else:
                locator = (receipt["producer"], row.get("event_id"), receipt["producer_revision"])
                if locator in seen_locator:
                    reason, detail = "duplicate_page", (
                        f"row locator already used by {seen_locator[locator]}")
                else:
                    problem = validate_raw_row(row, receipt["schema_version"])
                    if problem:
                        reason, detail = "malformed_row", problem
                    else:
                        reason, detail = None, None
            if reason:
                _quarantine(conn, receipt, reason, detail)
                summary["quarantined"] += 1
                summary["details"].append({"receipt_id": receipt["receipt_id"],
                                           "reason": reason, "detail": detail})
                continue
            seen_raw[receipt["raw_sha256"]] = receipt["receipt_id"]
            seen_locator[locator] = receipt["receipt_id"]
            normalized[receipt["receipt_id"]] = normalize_row(row, receipt["schema_version"])

        for receipt in receipts:
            if receipt["receipt_id"] not in normalized:
                continue
            event = normalized[receipt["receipt_id"]]
            kind = event["kind"]
            external_id = event["event_id"]
            producer = receipt["producer"]
            revision = receipt["producer_revision"]
            signature = receipt["producer_signature"]
            page = next(p for p in envelope["pages"] if p.get("page_cursor") == receipt["page_cursor"])
            supersedes = page.get("supersedes")
            normalized_sha = digest(event)
            mapped = conn.execute(
                "SELECT * FROM event_identity_map WHERE normalized_sha256=?",
                (normalized_sha,),
            ).fetchone()
            if not mapped:
                legacy = conn.execute(
                    "SELECT event_id FROM source_events WHERE sha256=?", (normalized_sha,)
                ).fetchone()
                # Exact semantic replay of a classic-ledger row. Attach the portal
                # identity unless another producer already owns that internal id;
                # changed bytes from a different producer have a different digest and
                # never reach this branch, so they cannot hijack the legacy event.
                if legacy and not conn.execute(
                    "SELECT 1 FROM event_identity_map WHERE internal_event_id=?",
                    (legacy["event_id"],),
                ).fetchone():
                    conn.execute(
                        "INSERT INTO event_identity_map VALUES (?,?,?,?,?,?,?,?,?)",
                        (legacy["event_id"], producer, external_id, 1, LEGACY_CONTRACT,
                         generation_id, receipt["receipt_id"], normalized_sha, None))
                    mapped = conn.execute(
                        "SELECT * FROM event_identity_map WHERE normalized_sha256=?",
                        (normalized_sha,),
                    ).fetchone()
            if mapped:
                _link(conn, receipt, mapped["internal_event_id"],
                      mapped["internal_event_id"], "synonym_replay")
                summary["linked"] += 1
                summary["semantic_replayed"] += 1
                continue

            revisions = _known_revisions(conn, producer, external_id)
            # The classic ledger's sha256 is itself a normalized digest, which makes it
            # the delivery-order-independent anchor for the first producer revision.
            legacy_anchor = conn.execute(
                "SELECT sha256 FROM source_events WHERE event_id=?", (external_id,)
            ).fetchone()
            other_owner = conn.execute(
                "SELECT 1 FROM event_identity_map WHERE external_event_id=? AND producer<>?",
                (external_id, producer),
            ).fetchone()
            if other_owner and not revisions:
                reason, detail = "identity_collision", (
                    f"external id {external_id} is already owned by another producer")
            elif contract == CONTRACT_UNIQUE_ID and (revisions or legacy_anchor):
                # Contract B never accepts a reused external id with changed semantics,
                # whether or not the old identity is already known to this ledger.
                reason, detail = "identity_reuse_violation", (
                    "unique_external_id contract requires a fresh event id for changed semantics")
            elif not revisions and legacy_anchor and revision == 2:
                if not signature:
                    reason, detail = "unsigned_reuse_no_override", (
                        "external id reused with changed semantics but no producer revision signature")
                elif not supersedes:
                    reason, detail = "broken_revision_chain", "revision page must carry supersedes digest"
                elif supersedes != legacy_anchor["sha256"]:
                    reason, detail = "broken_revision_chain", (
                        "supersedes digest does not match the original v1 raw page")
                else:
                    reason, detail = None, None
            elif not revisions and contract == CONTRACT_REUSED_ID and revision > 1:
                reason, detail = "broken_revision_chain", (
                    f"no revision 1 exists for external id {external_id}")
            elif not revisions:
                reason, detail = None, None
            elif not signature:
                reason, detail = "unsigned_reuse_no_override", (
                    "external id reused with changed semantics but no producer revision signature")
            elif revision <= 1:
                reason, detail = "unsigned_reuse_no_override", (
                    "changed semantics under an existing external id require producer_revision > 1")
            else:
                reason, detail = None, None
                previous = revisions.get(revision - 1)
                if revision in revisions:
                    reason, detail = "stale_revision", f"revision {revision} already exists"
                elif previous is None:
                    reason, detail = "broken_revision_chain", (
                        f"expected producer_revision {max(revisions) + 1}")
                elif not supersedes:
                    reason, detail = "broken_revision_chain", "revision page must carry supersedes digest"
                elif supersedes != _previous_anchor_digest(conn, previous):
                    reason, detail = "broken_revision_chain", (
                        "supersedes digest does not match the previous revision raw page")
            if reason:
                _quarantine(conn, receipt, reason, detail)
                summary["quarantined"] += 1
                summary["details"].append({"receipt_id": receipt["receipt_id"],
                                           "reason": reason, "detail": detail})
                continue
            if revisions or revision > 1:
                internal_id = f"{external_id}#rev{revision}"
                role = "revision"
            else:
                internal_id = external_id
                role = "reissue" if contract == CONTRACT_UNIQUE_ID else "initial"

            if conn.execute("SELECT 1 FROM source_events WHERE event_id=?",
                            (internal_id,)).fetchone():
                _quarantine(conn, receipt, "event_id_collision",
                            f"internal event id {internal_id} already holds different bytes")
                summary["quarantined"] += 1
                continue
            if conn.execute("SELECT 1 FROM event_identity_map WHERE internal_event_id=?",
                            (internal_id,)).fetchone():
                _quarantine(conn, receipt, "identity_collision",
                            f"internal event id {internal_id} already mapped")
                summary["quarantined"] += 1
                continue
            try:
                # Reserve the identity mapping before touching the business ledger so a
                # unique-key breach isolates this row instead of poisoning the transaction.
                conn.execute(
                    "INSERT INTO event_identity_map VALUES (?,?,?,?,?,?,?,?,?)",
                    (internal_id, producer, external_id, revision, contract,
                     generation_id, receipt["receipt_id"], normalized_sha, supersedes))
                result = admit_semantic_event(conn, KIND_SUBSYSTEM[kind],
                                              {**event, "event_id": internal_id})
            except (ValueError, sqlite3.IntegrityError) as exc:
                # Defence in depth: the store's unique-key guard refused the body, so
                # isolate the row rather than aborting the whole generation.
                conn.execute(
                    "INSERT OR REPLACE INTO quarantine VALUES (?,?,?,?,?)",
                    (receipt["receipt_id"], generation_id, "event_id_collision",
                     str(exc), receipt["received_at"]))
                summary["quarantined"] += 1
                summary["details"].append(
                    {"receipt_id": receipt["receipt_id"],
                     "reason": "event_id_collision", "detail": str(exc)})
                continue
            _link(conn, receipt, internal_id, internal_id, role)
            summary["linked"] += 1
            if result["replayed"]:
                summary["semantic_replayed"] += 1
            else:
                summary["semantic_inserted"] += 1

        conn.execute(
            "UPDATE ingest_generations SET status='projected' WHERE generation_id=?",
            (generation_id,))
    return summary


def ingest_pages(conn: sqlite3.Connection, envelope: dict,
                 package_dir: str | Path | None = None,
                 file_bytes: bytes | None = None) -> dict:
    """Stage raw bytes and project semantics; one generation, one recovery boundary."""
    problem = validate_page_envelope(envelope)
    if problem:
        raise ValueError(problem)
    fhash = hashlib.sha256(
        file_bytes if file_bytes is not None else canonical(envelope).encode("utf-8")
    ).hexdigest()
    existing = conn.execute(
        "SELECT generation_id, status FROM ingest_generations WHERE file_hash=?", (fhash,)
    ).fetchone()
    if existing:
        if existing["status"] == "staged":
            return project_generation(conn, existing["generation_id"])
        return _generation_summary(conn, existing["generation_id"])
    generation_id = stage_pages(conn, envelope, package_dir=package_dir,
                                file_bytes=file_bytes)
    return project_generation(conn, generation_id)


def _generation_summary(conn, generation_id: str) -> dict:
    linked = conn.execute(
        "SELECT COUNT(*) FROM raw_semantic_links WHERE generation_id=?", (generation_id,)
    ).fetchone()[0]
    quarantined = conn.execute(
        "SELECT COUNT(*) FROM quarantine WHERE generation_id=?", (generation_id,)
    ).fetchone()[0]
    inserted = conn.execute(
        "SELECT COUNT(*) FROM raw_semantic_links WHERE generation_id=? AND role != 'synonym_replay'",
        (generation_id,)).fetchone()[0]
    return {"generation_id": generation_id, "linked": linked,
            "quarantined": quarantined, "semantic_inserted": inserted,
            "semantic_replayed": linked - inserted,
            "details": quarantine_list(conn, generation_id)}


def recover_pending(conn: sqlite3.Connection, package_dir: str | Path | None = None) -> dict:
    """Resume crash boundaries: staged generations and landed packages without a ledger row."""
    recovered = []
    if package_dir is not None:
        for path in sorted(Path(package_dir).glob("*.json")):
            payload = path.read_bytes()
            fhash = hashlib.sha256(payload).hexdigest()
            if not conn.execute("SELECT 1 FROM ingest_generations WHERE file_hash=?",
                                (fhash,)).fetchone():
                envelope = json.loads(payload.decode("utf-8"))
                generation_id = stage_pages(conn, envelope, package_dir=package_dir,
                                            file_bytes=payload)
                recovered.append(project_generation(conn, generation_id))
    for row in conn.execute(
        "SELECT generation_id FROM ingest_generations WHERE status='staged' ORDER BY received_at"
    ).fetchall():
        recovered.append(project_generation(conn, row["generation_id"]))
    return {"recovered_generations": len(recovered), "generations": recovered,
            "receipt_integrity": receipt_integrity(conn)}


# --------------------------------------------------------------------------- #
# Deliverables: migration cross-reference and quarantine list.
# --------------------------------------------------------------------------- #
def migration_table(conn: sqlite3.Connection) -> list[dict]:
    """Every raw page row with the semantic event it explains (or why it is isolated)."""
    return [dict(row) for row in conn.execute(
        "SELECT * FROM migration_cross_reference ORDER BY generation_id, page_cursor, row_no"
    )]


def quarantine_list(conn: sqlite3.Connection, generation_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM quarantine"
    params: tuple = ()
    if generation_id is not None:
        query += " WHERE generation_id=?"
        params = (generation_id,)
    query += " ORDER BY generation_id, receipt_id"
    return [dict(row) for row in conn.execute(query, params)]


def receipt_integrity(conn: sqlite3.Connection) -> dict:
    """No raw row may claim receipt without a business explanation or isolation record."""
    orphans = [dict(row) for row in conn.execute(
        """SELECT r.receipt_id FROM raw_receipts r
           LEFT JOIN raw_semantic_links l ON l.receipt_id = r.receipt_id
           LEFT JOIN quarantine q ON q.receipt_id = r.receipt_id
           WHERE l.receipt_id IS NULL AND q.receipt_id IS NULL"""
    )]
    dangling = [dict(row) for row in conn.execute(
        """SELECT l.receipt_id FROM raw_semantic_links l
           LEFT JOIN source_events s ON s.event_id = l.semantic_event_id
           WHERE s.event_id IS NULL"""
    )]
    missing_packages = [
        {"generation_id": row["generation_id"], "bytes_path": row["bytes_path"]}
        for row in conn.execute(
            "SELECT generation_id, bytes_path FROM ingest_generations WHERE bytes_path IS NOT NULL")
        if not Path(row["bytes_path"]).exists()
    ]
    return {"ok": not (orphans or dangling or missing_packages),
            "orphan_receipts": orphans, "dangling_links": dangling,
            "missing_packages": missing_packages}

"""Source contract validation. Business conflicts are decisions, malformed files are errors."""
from __future__ import annotations

from datetime import date

from .store import KIND_SUBSYSTEM, PROJECTIONS

SUPPORTED_SCHEMA_VERSIONS = (1, 2)
PAGE_KINDS = {"offer", "batch"}
# v2 renamed fields per kind. Aliases must normalize to exactly one canonical name.
FIELD_ALIASES = {
    "offer": {"due_date": "due", "price_cents": "price"},
    "batch": {},
}
IDENTITY_CONTRACTS = {"reused_external_id", "unique_external_id"}


def _date(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be ISO date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO date") from exc


def validate_event(event: object, subsystem: str) -> None:
    if not isinstance(event, dict):
        raise ValueError("event must be a JSON object")
    kind = event.get("kind")
    if KIND_SUBSYSTEM.get(kind) != subsystem:
        raise ValueError(f"kind {kind!r} is not accepted by {subsystem}")
    required = {"event_id", "event_at", "kind", *PROJECTIONS[kind][1:]}
    missing = required - event.keys()
    if missing:
        raise ValueError(f"missing {kind} fields: {sorted(missing)}")
    for key in required - {"kind", "event_at", "capacity", "price", "deposit", "version",
                            "grace_days", "quantity", "units", "amount"}:
        if not isinstance(event[key], str) or not event[key].strip():
            raise ValueError(f"{key} must be nonempty text")
    for key in ("event_at", "due", "deposit_deadline", "tail_deadline", "certified_until",
                "placed_at", "paid_at", "accepted_at", "dispatched_at", "received_at", "inspected_at"):
        if key in event:
            _date(event[key], key)
    for key in ("capacity", "price", "deposit", "version", "grace_days", "quantity", "units", "amount"):
        if key in event:
            if type(event[key]) is not int or event[key] < 0:
                raise ValueError(f"{key} must be a nonnegative integer")
    for key in ("capacity", "price", "quantity", "units", "amount", "version"):
        if key in event and event[key] == 0:
            raise ValueError(f"{key} must be positive")
    if kind == "offer":
        if event["deposit"] >= event["price"]:
            raise ValueError("deposit must be less than price")
        if event["status"] not in {"active", "withdrawn"}:
            raise ValueError("invalid offer status")
        if event["deposit_deadline"] > event["tail_deadline"]:
            raise ValueError("deposit deadline cannot exceed tail deadline")
    elif kind == "batch" and event["status"] not in {"good", "sealed"}:
        raise ValueError("invalid batch status")
    elif kind == "payment" and event["stage"] not in {"deposit", "tail"}:
        raise ValueError("invalid payment stage")
    elif kind == "restock":
        if event["outcome"] not in {"accepted", "rejected"}:
            raise ValueError("invalid restock outcome")
        if event["inspector"] in {"merchant", "consumer", "carrier"}:
            raise ValueError("restock requires an independent inspector")
    elif kind == "receipt":
        if event["receipt_type"] not in {"signed", "returned"}:
            raise ValueError("invalid receipt type")
        if event["witness"] == "merchant":
            raise ValueError("merchant cannot witness consumer receipt")


def validate_page_envelope(envelope: object) -> str | None:
    """Structural checks on a merchant paged-file envelope. None means accepted."""
    if not isinstance(envelope, dict):
        return "envelope must be a JSON object"
    for key in ("file_id", "producer", "identity_contract", "pages"):
        if key not in envelope:
            return f"missing envelope field: {key}"
    if not isinstance(envelope["file_id"], str) or not envelope["file_id"].strip():
        return "file_id must be nonempty text"
    if not isinstance(envelope["producer"], str) or not envelope["producer"].strip():
        return "producer must be nonempty text"
    if envelope["identity_contract"] not in IDENTITY_CONTRACTS:
        return (f"identity_contract must be one of {sorted(IDENTITY_CONTRACTS)}; "
                "reused-id and unique-id rules cannot be mixed in one batch")
    if not isinstance(envelope["pages"], list) or not envelope["pages"]:
        return "pages must be a nonempty list"
    for page in envelope["pages"]:
        if not isinstance(page, dict):
            return "each page must be a JSON object"
        if type(page.get("page_cursor")) is not int or page["page_cursor"] < 0:
            return "page_cursor must be a nonnegative integer"
        if not isinstance(page.get("rows"), list) or not page["rows"]:
            return "each page must carry a nonempty rows list"
        revision = page.get("producer_revision", 1)
        if type(revision) is not int or revision < 1:
            return "producer_revision must be a positive integer"
        if "producer_signature" in page and not (
                isinstance(page["producer_signature"], str)
                and page["producer_signature"].strip()):
            return "producer_signature must be nonempty text when present"
        if "supersedes" in page and not (
                isinstance(page["supersedes"], str) and page["supersedes"].strip()):
            return "supersedes must be a nonempty digest string when present"
    return None


def _is_int(value: object) -> bool:
    return type(value) is int and not isinstance(value, bool)


def validate_raw_row(row: object, schema_version: int) -> str | None:
    """Validate one v1/v2 page row in its original dialect. None means well formed.

    Malformed rows are quarantined rather than aborting the whole generation so that
    one broken page never hides the merchant's other receivable evidence.
    """
    if not isinstance(row, dict):
        return "row must be a JSON object"
    kind = row.get("kind")
    if kind not in PAGE_KINDS:
        return f"kind {kind!r} is not accepted on paged offer files"
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        return f"unsupported schema_version: {schema_version}"
    aliases = FIELD_ALIASES[kind]
    inverse = {target: alias for alias, target in aliases.items()} if schema_version == 2 else {}
    if schema_version == 2:
        missing_aliases = [alias for alias in aliases if alias not in row]
        canonical_leak = [target for target in aliases.values() if target in row]
        if missing_aliases:
            return f"v2 row missing renamed fields: {sorted(missing_aliases)}"
        if canonical_leak:
            return f"v2 row must not also carry v1 field names: {sorted(canonical_leak)}"
    elif any(alias in row for alias in aliases):
        return f"v1 row must not carry v2 field names: {sorted(a for a in aliases if a in row)}"
    required = {"event_id", "event_at", "kind", *PROJECTIONS[kind][1:]}
    if schema_version == 2:
        required = {inverse.get(name, name) for name in required}
    missing = required - row.keys()
    if missing:
        return f"missing {kind} fields: {sorted(missing)}"
    numeric_canonical = {"capacity", "price", "deposit", "version",
                         "grace_days", "quantity", "units", "amount"}
    numeric_keys = {inverse.get(name, name) for name in numeric_canonical} if schema_version == 2 \
        else numeric_canonical
    text_fields = required - ({"kind", "event_at"} | numeric_keys)
    for key in text_fields:
        if not isinstance(row[key], str) or not row[key].strip():
            return f"{key} must be nonempty text"
    due_key = inverse.get("due", "due")
    date_fields = {"event_at", "certified_until"} if kind == "batch" else {
        "event_at", due_key, "deposit_deadline", "tail_deadline"}
    for key in date_fields:
        if key in row:
            if not isinstance(row[key], str):
                return f"{key} must be ISO date"
            try:
                date.fromisoformat(row[key])
            except ValueError:
                return f"{key} must be ISO date"
    int_fields = (("capacity", "price", "deposit", "version", "grace_days") if kind == "offer"
                  else ("quantity",))
    int_fields = tuple(inverse.get(name, name) for name in int_fields)
    positive = {inverse.get(name, name)
                for name in {"capacity", "price", "version", "quantity"}}
    for key in int_fields:
        if key in row:
            if not _is_int(row[key]) or row[key] < 0:
                return f"{key} must be a nonnegative integer"
            if key in positive and row[key] == 0:
                return f"{key} must be positive"
    if kind == "offer":
        if row[inverse.get("deposit", "deposit")] >= row[inverse.get("price", "price")]:
            return "deposit must be less than price"
        if row["status"] not in {"active", "withdrawn"}:
            return "invalid offer status"
        deadline_due = row[due_key]
        if row["deposit_deadline"] > row["tail_deadline"]:
            return "deposit deadline cannot exceed tail deadline"
        if not isinstance(deadline_due, str):
            return "due must be ISO date"
    elif row["status"] not in {"good", "sealed"}:
        return "invalid batch status"
    return None

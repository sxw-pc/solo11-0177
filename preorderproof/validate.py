"""Source contract validation. Business conflicts are decisions, malformed files are errors."""
from __future__ import annotations

from datetime import date

from .store import KIND_SUBSYSTEM, MANUAL_PROJECTIONS, OPTIONAL_FIELDS, PROJECTIONS


def _date(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be ISO date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO date") from exc


NUMERIC_FIELDS = {"capacity", "price", "deposit", "version", "grace_days",
                  "quantity", "units", "amount"}


def validate_event(event: object, subsystem: str) -> None:
    if not isinstance(event, dict):
        raise ValueError("event must be a JSON object")
    kind = event.get("kind")
    if KIND_SUBSYSTEM.get(kind) != subsystem:
        raise ValueError(f"kind {kind!r} is not accepted by {subsystem}")
    spec = MANUAL_PROJECTIONS.get(kind) or PROJECTIONS[kind]
    optional = OPTIONAL_FIELDS.get(kind, set())
    required = {"event_id", "event_at", "kind",
                *[col for col in spec[1:] if col not in optional]}
    missing = required - event.keys()
    if missing:
        raise ValueError(f"missing {kind} fields: {sorted(missing)}")
    for key in ("event_id", "event_at", "kind"):
        if not isinstance(event[key], str) or not event[key].strip():
            raise ValueError(f"{key} must be nonempty text")
    for key in spec[1:]:
        if key in NUMERIC_FIELDS or key not in event:
            continue
        if not isinstance(event[key], str):
            raise ValueError(f"{key} must be text")
        if key not in optional and not event[key].strip():
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
    elif kind == "manual_page":
        if event["source"] not in {"supplier_pdf", "supplier_web", "ocr_toc",
                                   "qr_landing_rpa", "late_scan"}:
            raise ValueError("invalid manual page source")
    elif kind == "manual_revision" and event["relation"] not in {"split", "merge", "rename", "reuse"}:
        raise ValueError("invalid manual revision relation")
    elif kind == "manual_route":
        if event["strategy"] not in {"archive", "index", "forbidden_silent_single"}:
            raise ValueError("invalid manual route strategy")
        if event["status"] not in {"active", "superseded", "rejected"}:
            raise ValueError("invalid manual route status")

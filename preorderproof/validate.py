"""Source contract validation. Business conflicts are decisions, malformed files are errors."""
from __future__ import annotations

from datetime import date

from .store import KIND_SUBSYSTEM, PROJECTIONS, digest

_CAMPAIGN_PAGE_KINDS = {"assignment_page", "exposure_page", "order_page"}


def _date(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be ISO date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO date") from exc


def _campaign_event(event: dict) -> None:
    signed = {key: value for key, value in event.items() if key != "signature"}
    if event["signature"] != digest(signed):
        raise ValueError("campaign page signature mismatch")
    kind = event["kind"]
    if event.get("sampling_unit") != "household":
        raise ValueError("sampling_unit must be household")
    if kind == "campaign_plan":
        arms = event.get("arms")
        if (not isinstance(arms, list) or len(arms) != 2
                or not all(isinstance(a, str) and a.strip() for a in arms)):
            raise ValueError("arms must be a pair of nonempty labels")
        weights = event.get("cohort_weights")
        if not isinstance(weights, dict) or not weights:
            raise ValueError("cohort_weights must be a nonempty object")
        for label, weight in weights.items():
            if not isinstance(label, str) or not label.strip():
                raise ValueError("cohort labels must be nonempty text")
            if not isinstance(weight, (int, float)) or isinstance(weight, bool) or not 0 < weight < 1:
                raise ValueError("cohort weights must be fractions in (0, 1)")
        if abs(sum(weights.values()) - 1.0) > 1e-9:
            raise ValueError("cohort weights must sum to 1")
        if event["locked_at"] > event["stats_cutoff"]:
            raise ValueError("plan must be locked before the statistics cutoff")
        return
    if kind == "page_cancel":
        if not isinstance(event.get("target_cursor"), str) or not event["target_cursor"].strip():
            raise ValueError("target_cursor must be nonempty text")
        return
    page_kind = kind
    if kind == "page_reissue":
        if not isinstance(event.get("target_cursor"), str) or not event["target_cursor"].strip():
            raise ValueError("target_cursor must be nonempty text")
        if event["target_cursor"] == event["cursor"]:
            raise ValueError("reissue cannot target its own cursor")
        if event.get("page_kind") not in _CAMPAIGN_PAGE_KINDS:
            raise ValueError("page_kind must name a data page kind")
        page_kind = event["page_kind"]
    entries = event.get("entries")
    if not isinstance(entries, list):
        raise ValueError("entries must be a list")
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get("household"), str)
                or not entry["household"].strip()):
            raise ValueError("entries need a nonempty household")
        if page_kind == "exposure_page":
            if not isinstance(entry.get("device"), str) or not entry["device"].strip():
                raise ValueError("exposure entries need a device")
            _date(entry.get("exposed_at"), "exposed_at")
            if entry["exposed_at"] > event["stats_cutoff"]:
                raise ValueError("exposure after the statistics cutoff")
        elif page_kind == "order_page":
            if not isinstance(entry.get("order_id"), str) or not entry["order_id"].strip():
                raise ValueError("order entries need an order_id")
            _date(entry.get("ordered_at"), "ordered_at")
            if entry["ordered_at"] > event["stats_cutoff"]:
                raise ValueError("order after the statistics cutoff")


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
                "placed_at", "paid_at", "accepted_at", "dispatched_at", "received_at", "inspected_at",
                "stats_cutoff", "locked_at"):
        if key in event:
            _date(event[key], key)
    for key in ("capacity", "price", "deposit", "version", "grace_days", "quantity", "units", "amount",
                "reported_visitors", "reported_buyers"):
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
    if subsystem == "campaign":
        _campaign_event(event)

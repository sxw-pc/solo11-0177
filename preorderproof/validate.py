"""Source contract validation. Business conflicts are decisions, malformed files are errors."""

from datetime import date, datetime

from .store import KIND_SUBSYSTEM, PROJECTIONS


def _date(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be ISO date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO date") from exc


def _datetime(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be ISO datetime")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO datetime") from exc


NONNEGATIVE_INTS = ("capacity", "price", "deposit", "version", "grace_days", "quantity",
                    "units", "amount", "boot", "page_no", "units_consumed", "max_over_minutes")
POSITIVE_INTS = ("capacity", "price", "quantity", "units", "amount", "version")
NUMBERS = ("ambient_c", "core_limit_c", "tau_minutes", "current_a")
DATETIMES = ("observed_at", "calibrated_at", "positioned_at", "mapped_at", "assayed_at")
NONTEXT = {"kind", "event_at", *NONNEGATIVE_INTS, *POSITIVE_INTS, *NUMBERS, "offset_seconds"}


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
    for key in required - NONTEXT:
        if not isinstance(event[key], str) or not event[key].strip():
            raise ValueError(f"{key} must be nonempty text")
    for key in ("event_at", "due", "deposit_deadline", "tail_deadline", "certified_until",
                "placed_at", "paid_at", "accepted_at", "dispatched_at", "received_at", "inspected_at"):
        if key in event:
            _date(event[key], key)
    for key in DATETIMES:
        if key in event:
            _datetime(event[key], key)
    for key in NONNEGATIVE_INTS:
        if key in event:
            if type(event[key]) is not int or event[key] < 0:
                raise ValueError(f"{key} must be a nonnegative integer")
    if "offset_seconds" in event and type(event["offset_seconds"]) is not int:
        raise ValueError("offset_seconds must be an integer")
    for key in NUMBERS:
        if key in event:
            if not isinstance(event[key], (int, float)) or isinstance(event[key], bool):
                raise ValueError(f"{key} must be a number")
    for key in POSITIVE_INTS:
        if key in event and event[key] == 0:
            raise ValueError(f"{key} must be positive")
    if "tau_minutes" in event and event["tau_minutes"] <= 0:
        raise ValueError("tau_minutes must be positive")
    if "current_a" in event and event["current_a"] < 0:
        raise ValueError("current_a must be nonnegative")
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
    elif kind == "sensor_placement" and event["position"] not in {"shell", "core", "ambient"}:
        raise ValueError("invalid sensor position")
    elif kind == "activity_assay":
        if event["outcome"] not in {"effective", "failed"}:
            raise ValueError("invalid assay outcome")
        if event["inspector"] in {"merchant", "consumer", "carrier"}:
            raise ValueError("assay requires an independent inspector")

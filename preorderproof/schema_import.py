"""Minimal adapter for a supplier's renamed v2 offer fields."""

from .store import ingest


def normalize_v2_offer(event):
    normalized = dict(event)
    if normalized.pop("schema_version", 1) == 2:
        normalized["due"] = normalized.pop("due_date")
        normalized["price"] = normalized.pop("price_cents")
    return normalized


def ingest_offer_v2(conn, event):
    return ingest(conn, "offer", [normalize_v2_offer(event)])

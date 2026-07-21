"""Canonical price-payload normalization used by reviewed extension imports."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from math import isfinite
from typing import Any

PRICE_PAYLOAD_FIELDS = (
    "provenance_id",
    "ticker",
    "security_id",
    "provider_symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
    "dividends",
    "split_ratio",
    "actions_complete",
    "close_split_adjusted",
    "split_normalization_factor",
    "split_normalization_through",
    "source",
)


def normalize_extension_price_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return one strict, JSON-stable price row for provenance and storage."""
    normalized = {
        "provenance_id": _required(row, "provenance_id"),
        "ticker": _required(row, "ticker").upper(),
        "security_id": _required(row, "security_id"),
        "provider_symbol": _required(row, "provider_symbol").upper(),
        "date": _date_text(row.get("date"), "date"),
        "open": _optional_number(row.get("open"), "open"),
        "high": _optional_number(row.get("high"), "high"),
        "low": _optional_number(row.get("low"), "low"),
        "close": _positive_number(row.get("close"), "close"),
        "adj_close": _optional_number(row.get("adj_close"), "adj_close"),
        "volume": int(row["volume"]) if row.get("volume") is not None else None,
        "dividends": _non_negative_number(row.get("dividends", 0), "dividends"),
        "split_ratio": _positive_number(row.get("split_ratio", 1), "split_ratio"),
        "actions_complete": row.get("actions_complete") is True,
        "close_split_adjusted": row.get("close_split_adjusted"),
        "split_normalization_factor": _positive_number(
            row.get("split_normalization_factor"),
            "split_normalization_factor",
        ),
        "split_normalization_through": (
            _date_text(row["split_normalization_through"], "split_normalization_through")
            if row.get("split_normalization_through") is not None
            else None
        ),
        "source": _required(row, "source").lower(),
    }
    if row.get("actions_complete") is not True:
        raise ValueError("extension price requires complete corporate actions")
    if not isinstance(normalized["close_split_adjusted"], bool):
        raise ValueError("extension price requires a declared split-adjustment basis")
    if normalized["volume"] is not None and normalized["volume"] < 0:
        raise ValueError("extension price volume cannot be negative")
    return normalized


def canonical_price_payload_hash(rows: list[dict[str, Any]]) -> str:
    """Hash sorted normalized rows with no timestamps or provider noise."""
    normalized = [normalize_extension_price_row(row) for row in rows]
    normalized.sort(key=lambda row: (row["security_id"], row["date"]))
    payload = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _required(row: dict[str, Any], field: str) -> str:
    value = str(row.get(field) or "").strip()
    if not value:
        raise ValueError(f"extension price requires {field}")
    return value


def _date_text(value: Any, field: str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise ValueError(f"extension price has invalid {field}") from exc


def _optional_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    if not isfinite(parsed):
        raise ValueError(f"extension price {field} must be finite")
    return parsed


def _positive_number(value: Any, field: str) -> float:
    parsed = _optional_number(value, field)
    if parsed is None or parsed <= 0:
        raise ValueError(f"extension price {field} must be positive")
    return parsed


def _non_negative_number(value: Any, field: str) -> float:
    parsed = _optional_number(value, field)
    if parsed is None or parsed < 0:
        raise ValueError(f"extension price {field} must be non-negative")
    return parsed

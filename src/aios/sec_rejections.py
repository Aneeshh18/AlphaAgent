"""Structured SEC fundamental-ingest rejection evidence.

Human-readable ingest errors are useful for operators but are not a stable
policy interface.  These helpers keep the machine contract canonical while
retaining narrowly bounded compatibility with the historical v2 warning.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

SEC_FUNDAMENTAL_REJECTION_CODES = frozenset(
    {
        "future_period",
        "storage_conflict",
        "unsupported_context",
    }
)
_REJECTION_CODE = re.compile(r"[a-z][a-z0-9_]*")
_LEGACY_FUTURE_PERIOD_WARNING = re.compile(
    r"rejected [1-9][0-9]* row(?:\(s\)|s)? with period_end after filing date",
    re.IGNORECASE,
)


def canonical_rejection_codes(codes: Iterable[str] | None) -> str | None:
    """Return sorted, unique rejection codes as canonical JSON text."""

    if codes is None:
        return None
    if isinstance(codes, (str, bytes)):
        raise TypeError("rejection_codes must be an iterable of code strings")
    normalized: set[str] = set()
    for code in codes:
        if not isinstance(code, str):
            raise TypeError("rejection_codes must contain only strings")
        value = code.strip()
        if not _REJECTION_CODE.fullmatch(value):
            raise ValueError("rejection_codes must use lowercase snake_case identifiers")
        normalized.add(value)
    if not normalized:
        return None
    return json.dumps(sorted(normalized), separators=(",", ":"), ensure_ascii=True)


def decode_rejection_codes(value: Any) -> tuple[str, ...] | None:
    """Decode only canonical rejection-code JSON; malformed evidence is refused."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("stored rejection_codes must be canonical JSON text")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("stored rejection_codes is invalid JSON") from exc
    if (
        not isinstance(decoded, list)
        or not decoded
        or any(not isinstance(code, str) for code in decoded)
    ):
        raise ValueError("stored rejection_codes must be a non-empty string list")
    canonical = canonical_rejection_codes(decoded)
    if canonical != value or len(decoded) != len(set(decoded)):
        raise ValueError("stored rejection_codes must be sorted canonical JSON")
    return tuple(decoded)


def accepted_sec_fundamental_outcome(
    *,
    status: Any,
    error: Any,
    rejection_codes: Any,
) -> bool:
    """Return whether an outcome may support positive SEC row lineage.

    New warning outcomes must carry only the reviewed structured codes.  The
    sole text-only compatibility path is the historical future-period warning
    emitted before the structured column existed.
    """

    try:
        codes = decode_rejection_codes(rejection_codes)
    except (TypeError, ValueError):
        return False
    if status == "success":
        return codes is None and error is None
    if status != "warning":
        return False
    if codes is not None:
        return bool(
            codes
            and set(codes) <= SEC_FUNDAMENTAL_REJECTION_CODES
            and isinstance(error, str)
            and error.strip()
        )
    return bool(isinstance(error, str) and _LEGACY_FUTURE_PERIOD_WARNING.fullmatch(error.strip()))

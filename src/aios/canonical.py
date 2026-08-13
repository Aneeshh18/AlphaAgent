"""One canonical JSON contract for evidence hashing.

Content-addressed plans, activation receipts, and case fingerprints are only
comparable when identical logical data serializes to identical bytes. Several
modules historically defined their own private ``_canonical_json``; most agreed,
but not all, so this module makes the intended contract explicit and testable.

THE CONTRACT
------------
``sort_keys=True`` (key order is not information), ``separators=(",", ":")`` (no
incidental whitespace), ``ensure_ascii=False`` (one UTF-8 encoding of a
character rather than an escape), and ``allow_nan=False`` (``NaN``/``Infinity``
are not valid JSON and must never silently enter a hash).

KNOWN DIVERGENCES
-----------------
Three modules deliberately keep a different private implementation because they
belong to the active trial's frozen policy bundle and their existing bytes are
already bound into verified evidence:

- ``operations.py`` uses ``ensure_ascii=True`` and appends a trailing newline.
  Verified backup manifests were hashed with that exact form; changing it would
  invalidate every existing manifest checksum.
- ``forward_rollover.py`` and ``rollover_journal.py`` match this contract but
  cannot be edited without drifting the frozen bundle.

``tests/test_canonical_hashing.py`` pins that exception list, so a *new*
divergence fails rather than silently spreading.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

__all__ = [
    "canonical_bytes",
    "canonical_json",
    "canonical_sha256",
    "json_safe",
]


def json_safe(value: Any) -> Any:
    """Normalize dates, tuples, and decimals into stable JSON-native types.

    Serializing a ``date`` or ``Decimal`` directly raises, and a tuple and list
    describe the same sequence, so both must canonicalize identically.
    """
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def canonical_json(value: Any) -> str:
    """Return the canonical JSON text used for evidence hashing."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    """Return the exact bytes that a canonical hash is computed over."""
    return canonical_json(value).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Return the lowercase SHA-256 of one canonically serialized value."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()

"""One canonical JSON contract for every evidence hash.

Content-addressed plans, activation receipts and case fingerprints are only
comparable when identical logical data serializes to identical bytes. Several
modules define a private ``_canonical_json``; this suite pins them to one
contract and holds the known divergences on an explicit, justified list so a
*new* divergence fails here instead of silently spreading.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from aios import (
    companyfacts_replay,
    hosted_export,
    universe_change,
    universe_change_activation,
    universe_rollforward,
)
from aios.canonical import (
    canonical_bytes,
    canonical_json,
    canonical_sha256,
    json_safe,
)

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "aios"

# Modules that intentionally keep a private `_canonical_json` definition.
#
# The first three belong to the active forward trial's frozen policy bundle, so
# their bytes cannot change without drifting the trial. ``operations.py``
# additionally uses ``ensure_ascii=True`` plus a trailing newline, which is the
# exact form every existing verified backup manifest was hashed with.
#
# ``universe_change_activation.py`` is NOT a divergence: it composes the shared
# contract with ``json_safe`` first, because its payloads carry ``date`` and
# ``datetime`` values that plain ``json.dumps`` cannot serialize. It is asserted
# byte-identical to ``canonical_json(json_safe(...))`` below.
KNOWN_DIVERGENT = {
    "operations.py": "frozen bundle; ensure_ascii=True + trailing newline binds existing manifests",
    "forward_rollover.py": "frozen bundle; matches the contract but cannot be edited",
    "rollover_journal.py": "frozen bundle; matches the contract but cannot be edited",
    "universe_change_activation.py": (
        "composes the shared contract with json_safe for date payloads"
    ),
}

FIXTURE = {
    "zebra": 1,
    "alpha": {"nested": [3, 2, 1], "unicode": "café — naïve ✓"},
    "numbers": [0, -1, 2.5, 1e10],
    "empty": {},
    "null": None,
    "bool": True,
}


def _modules_defining_canonical_json() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_canonical_json":
                found[path.name] = path
    return found


def test_every_private_canonical_json_is_accounted_for() -> None:
    """A new private implementation must be justified, not silently added."""
    defined = set(_modules_defining_canonical_json())
    unexpected = defined - set(KNOWN_DIVERGENT)
    assert not unexpected, (
        "new private _canonical_json found; either use aios.canonical or add a "
        f"documented reason: {sorted(unexpected)}"
    )


@pytest.mark.parametrize(
    "module",
    [
        universe_rollforward,
        companyfacts_replay,
        hosted_export,
    ],
)
def test_adopting_modules_produce_the_shared_bytes(module) -> None:
    assert module._canonical_json(FIXTURE) == canonical_json(FIXTURE)


def test_activation_composes_the_shared_contract_with_json_safe() -> None:
    """Exempt from the plain contract, but not from being equivalent.

    Its payloads carry dates, so it normalizes first. On JSON-native input it
    must still produce exactly the shared bytes.
    """
    assert universe_change_activation._canonical_json(FIXTURE) == canonical_json(
        FIXTURE
    )
    dated = {"day": date(2026, 8, 7), "pair": (1, 2)}
    assert universe_change_activation._canonical_json(dated) == canonical_json(
        json_safe(dated)
    )


def test_universe_change_hash_matches_the_shared_contract() -> None:
    assert universe_change._canonical_payload_sha256(FIXTURE) == canonical_sha256(
        FIXTURE
    )


def test_hosted_export_hash_matches_the_shared_contract() -> None:
    assert hosted_export._json_sha256(FIXTURE) == canonical_sha256(FIXTURE)


def test_contract_is_sorted_compact_utf8_and_finite() -> None:
    text = canonical_json({"b": 1, "a": 2})
    assert text == '{"a":2,"b":1}'
    # No incidental whitespace, and one UTF-8 character rather than an escape.
    assert canonical_json({"k": "é"}) == '{"k":"é"}'
    assert canonical_bytes({"k": "é"}) == '{"k":"é"}'.encode()
    for invalid in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            canonical_json({"k": invalid})


def test_hash_is_stable_across_key_insertion_order() -> None:
    first = {"a": 1, "b": {"c": 2, "d": 3}}
    second = {"b": {"d": 3, "c": 2}, "a": 1}
    assert canonical_sha256(first) == canonical_sha256(second)


def test_hash_changes_when_content_changes() -> None:
    assert canonical_sha256({"a": 1}) != canonical_sha256({"a": 2})


def test_json_safe_normalizes_dates_tuples_and_decimals() -> None:
    normalized = json_safe(
        {
            "day": date(2026, 8, 7),
            "moment": datetime(2026, 8, 7, 21, 30, tzinfo=UTC),
            "pair": (1, 2),
            "amount": Decimal("1.10"),
        }
    )
    assert normalized["day"] == "2026-08-07"
    assert normalized["moment"].startswith("2026-08-07T21:30")
    assert normalized["pair"] == [1, 2]
    assert normalized["amount"] == "1.10"
    # A tuple and a list describe the same sequence and must hash identically.
    assert canonical_sha256(json_safe({"x": (1, 2)})) == canonical_sha256({"x": [1, 2]})


def test_canonical_output_round_trips_through_json() -> None:
    assert json.loads(canonical_json(FIXTURE)) == FIXTURE


def test_operations_divergence_is_real_and_documented() -> None:
    """Pin the known divergence so it cannot be 'fixed' without a decision.

    ``operations.py`` is frozen and its exact bytes are bound into verified
    backup manifests. This asserts the difference still exists as described; if
    the module is ever unfrozen and unified, update KNOWN_DIVERGENT too.
    """
    from aios import operations

    produced = operations._canonical_json({"k": "é"})
    assert isinstance(produced, bytes)
    assert produced.endswith(b"\n")
    assert produced != canonical_bytes({"k": "é"})
    assert "operations.py" in KNOWN_DIVERGENT

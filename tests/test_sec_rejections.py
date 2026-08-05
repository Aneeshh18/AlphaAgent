from __future__ import annotations

import pytest

from aios.sec_rejections import (
    accepted_sec_fundamental_outcome,
    canonical_rejection_codes,
    decode_rejection_codes,
)


def test_rejection_codes_are_sorted_unique_canonical_json() -> None:
    encoded = canonical_rejection_codes(["unsupported_context", "future_period", "future_period"])

    assert encoded == '["future_period","unsupported_context"]'
    assert decode_rejection_codes(encoded) == (
        "future_period",
        "unsupported_context",
    )


@pytest.mark.parametrize(
    "value",
    [
        '["unsupported_context","future_period"]',
        '["future_period","future_period"]',
        "[]",
        '{"future_period":true}',
        "not-json",
    ],
)
def test_noncanonical_stored_rejection_codes_fail_closed(value: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        decode_rejection_codes(value)


def test_sec_warning_acceptance_uses_structured_known_codes() -> None:
    assert accepted_sec_fundamental_outcome(
        status="warning",
        error="operator wording may change",
        rejection_codes='["future_period","storage_conflict"]',
    )
    assert not accepted_sec_fundamental_outcome(
        status="warning",
        error="operator wording may change",
        rejection_codes='["unreviewed_policy"]',
    )
    assert not accepted_sec_fundamental_outcome(
        status="success",
        error=None,
        rejection_codes='["future_period"]',
    )
    assert not accepted_sec_fundamental_outcome(
        status="success",
        error="provider verification failed",
        rejection_codes=None,
    )
    assert not accepted_sec_fundamental_outcome(
        status="warning",
        error=None,
        rejection_codes='["future_period"]',
    )


def test_historical_future_period_warning_remains_narrowly_compatible() -> None:
    assert accepted_sec_fundamental_outcome(
        status="warning",
        error="Rejected 4 rows with period_end after filing date",
        rejection_codes=None,
    )
    assert not accepted_sec_fundamental_outcome(
        status="warning",
        error="SEC returned no fundamental rows",
        rejection_codes=None,
    )


def test_rejection_code_normalizer_rejects_free_form_values() -> None:
    with pytest.raises(ValueError, match="lowercase snake_case"):
        canonical_rejection_codes(["Future Period"])
    with pytest.raises(TypeError, match="iterable"):
        canonical_rejection_codes("future_period")

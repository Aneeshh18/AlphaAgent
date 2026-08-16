"""Reviewed legal-share actions kept separate from provider price factors.

Yahoo's ``Stock Splits`` field is useful evidence about price continuity, but
it is not a legal share-count ledger.  A provider factor may combine a split
with a distribution, or describe a distribution that changes no share count.
Only records in this module may project that evidence into ``split_ratio``.

The policy is deliberately code-versioned.  Adding an action therefore
requires a reviewed release and cannot silently change replay of an existing
raw-snapshot parser.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isclose, isfinite
from typing import Any

CORPORATE_ACTION_POLICY_VERSION = "reviewed-us-corporate-actions-v1"
YFINANCE_NO_ACTION_FACTOR = 1.0
_FACTOR_ABS_TOLERANCE = 1e-12


@dataclass(frozen=True)
class ReviewedCorporateAction:
    """One exact identity/date review of provider and legal action terms."""

    review_id: str
    security_id: str
    provider: str
    provider_symbol: str
    effective_date: date
    provider_price_continuity_factor: float
    legal_share_ratio: float
    distribution_handling: str
    actions_complete: bool
    known_date: date
    source_urls: tuple[str, ...]
    note: str


# Honeywell's 2026 separation combines a 1-for-2 reverse split with the HONA
# distribution.  The legal share ratio is reviewed, but holder-value continuity
# remains incomplete until the distributed security can be represented.  It is
# therefore useful evidence and intentionally not an eligible market-action row.
_REVIEWED_ACTIONS = (
    ReviewedCorporateAction(
        review_id="hon-2026-06-29-reverse-split-hona-v1",
        security_id="aios:bounded:sp500:hon",
        provider="yfinance",
        provider_symbol="HON",
        effective_date=date(2026, 6, 29),
        provider_price_continuity_factor=0.9535,
        legal_share_ratio=0.5,
        distribution_handling="unsupported_distributed_security",
        actions_complete=False,
        known_date=date(2026, 6, 5),
        source_urls=(
            "https://investor.honeywell.com/reverse-stock-split",
            "https://www.sec.gov/Archives/edgar/data/773840/"
            "000077384026000072/hon-20260605.htm",
        ),
        note=(
            "The provider factor preserves quoted-price continuity and does not "
            "encode Honeywell's legal 1-for-2 reverse split. The HONA distribution "
            "is not yet modeled, so total-return use must fail closed."
        ),
    ),
)


def reviewed_corporate_actions() -> tuple[ReviewedCorporateAction, ...]:
    """Return the immutable records in the current policy release."""

    return _REVIEWED_ACTIONS


def project_yfinance_action(
    row: dict[str, Any],
    *,
    security_id: str,
    provider_symbol: str,
) -> dict[str, Any]:
    """Project one v4 Yahoo row into reviewed legal-share semantics.

    Rows with an explicit provider no-action value need no event-level review.
    Every non-unit provider factor requires an exact security, symbol, date and
    factor match.  Unknown or incomplete actions remain visible and ineligible.
    """

    projected = dict(row)
    if "provider_price_continuity_factor" not in projected:
        return projected

    provider_factor = _positive_factor(
        projected["provider_price_continuity_factor"],
        "provider price-continuity factor",
    )
    projected["corporate_action_policy_version"] = CORPORATE_ACTION_POLICY_VERSION
    projected["corporate_action_review_id"] = None
    projected["split_ratio"] = 1.0

    if isclose(
        provider_factor,
        YFINANCE_NO_ACTION_FACTOR,
        rel_tol=0.0,
        abs_tol=_FACTOR_ABS_TOLERANCE,
    ):
        projected["actions_complete"] = True
        projected["corporate_action_review_status"] = "provider_reported_none"
        return projected

    row_date = date.fromisoformat(str(projected["date"]))
    matches = [
        action
        for action in _REVIEWED_ACTIONS
        if action.security_id == security_id
        and action.provider == "yfinance"
        and action.provider_symbol == provider_symbol.upper()
        and action.effective_date == row_date
    ]
    if len(matches) > 1:  # pragma: no cover - protected by policy tests
        raise RuntimeError("corporate-action policy contains duplicate exact reviews")
    if not matches:
        projected["actions_complete"] = False
        projected["corporate_action_review_status"] = "unreviewed_provider_action"
        return projected

    action = matches[0]
    if not isclose(
        provider_factor,
        action.provider_price_continuity_factor,
        rel_tol=0.0,
        abs_tol=_FACTOR_ABS_TOLERANCE,
    ):
        projected["actions_complete"] = False
        projected["corporate_action_review_status"] = "provider_factor_mismatch"
        return projected

    projected["split_ratio"] = action.legal_share_ratio
    projected["actions_complete"] = action.actions_complete
    projected["corporate_action_review_id"] = action.review_id
    projected["corporate_action_review_status"] = (
        "reviewed_complete" if action.actions_complete else "reviewed_incomplete"
    )
    projected["corporate_action_distribution_handling"] = action.distribution_handling
    return projected


def _positive_factor(value: Any, label: str) -> float:
    try:
        factor = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not isfinite(factor) or factor <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return factor

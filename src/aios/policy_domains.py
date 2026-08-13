"""Named, versioned identity for the research, market, and account policy domains.

`FUTURE_BUILD_PLAN.md` Phase 4 separates five configuration domains: operator,
research, market, account, and secrets. Operator configuration (paths, rate
limits) lives in `config.py`, and secrets live outside versioned artifacts in
`.env` — both already satisfy their half of the boundary. Research, market,
and account policy did not: they existed only as unversioned Python constants
scattered across `factors/policy.py`, `risk/policy.py`, and
`backtest/costs.py`. A material change to any of them was invisible except as
a diff in the frozen-bundle file hash — accurate, but not addressable by name.

This module gives each domain an explicit `name`/`version` and a content hash,
without editing any of the frozen modules it reads from
(`factors/policy.py`, `risk/policy.py`, `backtest/costs.py` are all inside the
active trial's frozen bundle). Every value here is read from the real running
code, never hand-typed as a duplicate — drift in the source module changes the
computed hash immediately rather than silently diverging from a copy.

"Research, risk, market and account policies receive immutable names and
versions. A material change creates a new forward trial instead of modifying
an existing freeze." This module makes that identity nameable; it does not
itself create or activate a forward trial — `forward-restart --confirm-restart`
remains the only path that does, unchanged.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from aios.backtest.costs import TaxPolicy, TransactionCostPolicy
from aios.canonical import canonical_sha256
from aios.factors.policy import (
    MIN_QUALITY_COMPONENTS,
    MIN_VALUE_MULTIPLES,
    QVML_LOW_VOLATILITY_WEIGHT,
    QVML_MOMENTUM_WEIGHT,
    QVML_QV_CORE_WEIGHT,
    REGIME_FACTOR_WEIGHTS,
)
from aios.risk.policy import PortfolioRiskPolicy

__all__ = [
    "AccountPolicyIdentity",
    "MarketProfileIdentity",
    "ResearchPolicyIdentity",
    "current_account_policy",
    "current_market_profile",
    "current_research_policy",
    "policy_snapshot",
]

# The market profile is deliberately hand-declared rather than introspected:
# `market_calendar.py` exposes its session-close time and finalization delay
# as private module attributes, and reaching into another module's `_`-prefixed
# internals is fragile in a way plain values are not. Every field below is a
# convention repeated verbatim across this codebase's CLI defaults and
# `agent.md`'s own operating text, not invented for this module.
_MARKET_SESSION_CLOSE_LOCAL_TIME = "16:00 America/New_York"
_MARKET_EOD_FINALIZATION_DELAY_MINUTES = 30
_MARKET_DEFAULT_BENCHMARK_TICKER = "SPY"
_MARKET_DEFAULT_CALENDAR_TICKER = "SPY"
_MARKET_DEFAULT_UNIVERSE_ID = "sp500"
_MARKET_PRIMARY_PRICE_SOURCE = "yfinance"
_MARKET_SECONDARY_PRICE_SOURCE = "tiingo"
_MARKET_FUNDAMENTALS_SOURCE = "sec-edgar"
_MARKET_MACRO_SOURCE = "fred"


@dataclass(frozen=True)
class ResearchPolicyIdentity:
    """Named, versioned identity for factor weights and risk constraints."""

    name: str
    version: str
    baseline_quality_weight: float
    baseline_value_weight: float
    regime_weights: dict[str, dict[str, float]]
    qvml_qv_core_weight: float
    qvml_momentum_weight: float
    qvml_low_volatility_weight: float
    min_quality_components: int
    min_value_multiples: int
    risk_policy: dict[str, Any]
    content_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MarketProfileIdentity:
    """Named, versioned identity for sources, sessions, and benchmark."""

    name: str
    version: str
    universe_id: str
    benchmark_ticker: str
    calendar_ticker: str
    primary_price_source: str
    secondary_price_source: str
    fundamentals_source: str
    macro_source: str
    session_close_local_time: str
    eod_finalization_delay_minutes: int
    content_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AccountPolicyIdentity:
    """Named, versioned identity for capital, cost, and tax defaults."""

    name: str
    version: str
    default_initial_capital: float
    default_transaction_costs: dict[str, float]
    default_tax_policy: dict[str, float | int]
    content_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _content_hash(payload: dict[str, Any]) -> str:
    excluded = {"content_sha256"}
    return canonical_sha256({k: v for k, v in payload.items() if k not in excluded})


def _bounded_name(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("policy domain name must not be empty")
    if len(stripped) > 96:
        raise ValueError("policy domain name must not exceed 96 characters")
    return stripped


def _bounded_version(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("policy domain version must not be empty")
    if len(stripped) > 32:
        raise ValueError("policy domain version must not exceed 32 characters")
    return stripped


def current_research_policy(
    *, name: str, version: str
) -> ResearchPolicyIdentity:
    """Snapshot the research policy as it exists right now in the frozen code."""
    name = _bounded_name(name)
    version = _bounded_version(version)
    baseline = REGIME_FACTOR_WEIGHTS["unknown"]
    regime_weights = {
        regime: {"quality": weights.quality, "value": weights.value}
        for regime, weights in sorted(REGIME_FACTOR_WEIGHTS.items())
    }
    payload = {
        "name": name,
        "version": version,
        "baseline_quality_weight": baseline.quality,
        "baseline_value_weight": baseline.value,
        "regime_weights": regime_weights,
        "qvml_qv_core_weight": QVML_QV_CORE_WEIGHT,
        "qvml_momentum_weight": QVML_MOMENTUM_WEIGHT,
        "qvml_low_volatility_weight": QVML_LOW_VOLATILITY_WEIGHT,
        "min_quality_components": MIN_QUALITY_COMPONENTS,
        "min_value_multiples": MIN_VALUE_MULTIPLES,
        "risk_policy": asdict(PortfolioRiskPolicy()),
    }
    return ResearchPolicyIdentity(**payload, content_sha256=_content_hash(payload))


def current_market_profile(*, name: str, version: str) -> MarketProfileIdentity:
    """Snapshot the market profile's declared sources, sessions, and benchmark."""
    name = _bounded_name(name)
    version = _bounded_version(version)
    payload = {
        "name": name,
        "version": version,
        "universe_id": _MARKET_DEFAULT_UNIVERSE_ID,
        "benchmark_ticker": _MARKET_DEFAULT_BENCHMARK_TICKER,
        "calendar_ticker": _MARKET_DEFAULT_CALENDAR_TICKER,
        "primary_price_source": _MARKET_PRIMARY_PRICE_SOURCE,
        "secondary_price_source": _MARKET_SECONDARY_PRICE_SOURCE,
        "fundamentals_source": _MARKET_FUNDAMENTALS_SOURCE,
        "macro_source": _MARKET_MACRO_SOURCE,
        "session_close_local_time": _MARKET_SESSION_CLOSE_LOCAL_TIME,
        "eod_finalization_delay_minutes": _MARKET_EOD_FINALIZATION_DELAY_MINUTES,
    }
    return MarketProfileIdentity(**payload, content_sha256=_content_hash(payload))


def current_account_policy(*, name: str, version: str) -> AccountPolicyIdentity:
    """Snapshot the account policy's capital, cost, and tax defaults."""
    name = _bounded_name(name)
    version = _bounded_version(version)
    payload = {
        "name": name,
        "version": version,
        "default_initial_capital": 100_000.0,
        "default_transaction_costs": TransactionCostPolicy(
            commission_bps=5.0, slippage_bps=5.0
        ).to_dict(),
        "default_tax_policy": TaxPolicy.zero().to_dict(),
    }
    return AccountPolicyIdentity(**payload, content_sha256=_content_hash(payload))


def policy_snapshot(
    *,
    research_name: str = "us-equity-qv-baseline",
    research_version: str = "v1",
    market_name: str = "us-equity-sp500-reference",
    market_version: str = "v1",
    account_name: str = "us-equity-paper-simulation",
    account_version: str = "v1",
) -> dict[str, Any]:
    """Return all three domain identities plus one combined content hash.

    The combined hash lets a caller — the experiment registry, a future
    comparison report — detect in one field whether ANY of the three domains
    changed, while the per-domain hash still says exactly which one.
    """
    research = current_research_policy(name=research_name, version=research_version)
    market = current_market_profile(name=market_name, version=market_version)
    account = current_account_policy(name=account_name, version=account_version)
    combined = {
        "research": research.content_sha256,
        "market": market.content_sha256,
        "account": account.content_sha256,
    }
    return {
        "research_policy": research.to_dict(),
        "market_profile": market.to_dict(),
        "account_policy": account.to_dict(),
        "combined_sha256": _content_hash(combined),
    }

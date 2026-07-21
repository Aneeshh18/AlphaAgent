"""PIT-safe Momentum and Low-Volatility factors from raw daily actions.

The first implementation deliberately uses transparent, conventional signals:

* Momentum: 12-minus-1-month total return, represented by 252 trading-session
  history with the latest 21 sessions skipped.
* Low Volatility: annualized sample standard deviation of the latest 252 daily
  total returns.

Returns use closes plus explicit dividend actions. Split ratios are applied
only for providers whose close is not already split-normalized; this avoids
double-counting Yahoo's retrospectively split-normalized close series. Scores
are cross-sectional percentile ranks; higher is always better.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from math import isfinite, prod, sqrt
from statistics import stdev

from structlog import get_logger

from aios.factors import common as fc
from aios.market_calendar import us_equity_sessions
from aios.storage.store import Store, get_store

log = get_logger(__name__)

TRADING_SESSIONS_PER_YEAR = 252
REQUIRED_PRICE_OBSERVATIONS = TRADING_SESSIONS_PER_YEAR + 1
MOMENTUM_SKIP_SESSIONS = 21
MAX_PRICE_STALENESS_DAYS = 7


@dataclass
class MarketFactorSnapshot:
    ticker: str
    as_of: str
    momentum_12_1: float | None = None
    annualized_volatility: float | None = None
    momentum_score: float | None = None
    low_volatility_score: float | None = None
    price_observations: int = 0
    momentum_start_date: date | None = None
    momentum_end_date: date | None = None
    latest_price_date: date | None = None
    missing: list[str] = field(default_factory=list)


def _number(value: object, *, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if isfinite(result) else default


def _daily_total_returns(rows: list[dict]) -> tuple[list[float], list[str]]:
    """Convert ordered raw close/action rows into split/dividend total returns."""
    if len(rows) < 2:
        return [], []
    returns: list[float] = []
    missing: list[str] = []
    previous_close = _number(rows[0].get("close"))
    if previous_close is None or previous_close <= 0:
        return [], ["invalid_close"]

    for row in rows[1:]:
        close = _number(row.get("close"))
        split_ratio = _number(row.get("split_ratio"), default=1.0)
        dividend = _number(row.get("dividends"), default=0.0)
        if close is None or close <= 0:
            missing.append("invalid_close")
            break
        if split_ratio is None or split_ratio <= 0:
            missing.append("invalid_split_ratio")
            break
        if dividend is None or dividend < 0:
            missing.append("invalid_dividend")
            break
        split_multiplier = 1.0 if row.get("close_split_adjusted") is True else split_ratio
        gross_return = split_multiplier * (close + dividend) / previous_close
        if not isfinite(gross_return) or gross_return <= 0:
            missing.append("invalid_total_return")
            break
        returns.append(gross_return - 1.0)
        previous_close = close
    return returns, missing


def compute_market_factors_raw(
    ticker: str,
    as_of: str | date,
    store: Store | None = None,
) -> MarketFactorSnapshot:
    """Compute raw 12-1 Momentum and one-year Low Volatility for one ticker."""
    store = store or get_store()
    as_of_text = str(as_of)
    snap = MarketFactorSnapshot(ticker=ticker.upper(), as_of=as_of_text)
    rows = fc.factor_price_history(
        store,
        ticker,
        as_of_text,
        observations=REQUIRED_PRICE_OBSERVATIONS,
    )
    snap.price_observations = len(rows)
    if len(rows) < REQUIRED_PRICE_OBSERVATIONS:
        snap.missing.append(f"minimum_price_observations:{REQUIRED_PRICE_OBSERVATIONS}")
        return snap
    if any(row.get("actions_complete") is not True for row in rows):
        snap.missing.append("corporate_actions_unverified")
        return snap
    split_bases = {row.get("close_split_adjusted") for row in rows}
    if None in split_bases:
        snap.missing.append("split_adjustment_basis_unknown")
        return snap
    if len(split_bases) != 1:
        snap.missing.append("mixed_split_adjustment_basis")
        return snap

    latest_date = rows[-1]["date"]
    if not isinstance(latest_date, date):
        latest_date = date.fromisoformat(str(latest_date))
    snap.latest_price_date = latest_date
    decision_date = date.fromisoformat(as_of_text)
    staleness = (decision_date - latest_date).days
    if staleness < 0 or staleness > MAX_PRICE_STALENESS_DAYS:
        snap.missing.append(f"stale_latest_price:{staleness}")
        return snap

    # A count of 253 observations is not sufficient on its own: a ticker
    # transition or provider gap could otherwise turn a months-long absence
    # into one apparent daily return. Require the exact exchange-session path
    # from the first observation through the decision date.
    row_dates = [
        row["date"]
        if isinstance(row["date"], date)
        else date.fromisoformat(str(row["date"]))
        for row in rows
    ]
    expected_dates = us_equity_sessions(row_dates[0], decision_date + timedelta(days=1))
    if row_dates != expected_dates:
        snap.missing.append("noncontiguous_price_sessions")
        return snap

    daily_returns, invalid = _daily_total_returns(rows)
    if invalid:
        snap.missing.extend(invalid)
        return snap
    if len(daily_returns) != TRADING_SESSIONS_PER_YEAR:
        snap.missing.append(f"minimum_daily_returns:{TRADING_SESSIONS_PER_YEAR}")
        return snap

    momentum_end_index = len(rows) - 1 - MOMENTUM_SKIP_SESSIONS
    momentum_returns = daily_returns[:momentum_end_index]
    if len(momentum_returns) != TRADING_SESSIONS_PER_YEAR - MOMENTUM_SKIP_SESSIONS:
        snap.missing.append("momentum_window_unavailable")
        return snap

    snap.momentum_start_date = rows[0]["date"]
    snap.momentum_end_date = rows[momentum_end_index]["date"]
    snap.momentum_12_1 = prod(1.0 + value for value in momentum_returns) - 1.0
    snap.annualized_volatility = stdev(daily_returns) * sqrt(TRADING_SESSIONS_PER_YEAR)
    return snap


def compute_market_factors_ranked(
    tickers: list[str],
    as_of: str | date,
    store: Store | None = None,
) -> dict[str, MarketFactorSnapshot]:
    """Compute market-factor inputs and cross-sectional scores for a universe."""
    store = store or get_store()
    snapshots: dict[str, MarketFactorSnapshot] = {}
    for ticker in tickers:
        try:
            snapshots[ticker.upper()] = compute_market_factors_raw(ticker, as_of, store)
        except Exception as exc:
            log.error("market_factors.compute_failed", ticker=ticker, error=str(exc))

    momentum_peers = [
        snap.momentum_12_1 for snap in snapshots.values() if snap.momentum_12_1 is not None
    ]
    volatility_peers = [
        snap.annualized_volatility
        for snap in snapshots.values()
        if snap.annualized_volatility is not None
    ]
    for snap in snapshots.values():
        if snap.momentum_12_1 is not None and momentum_peers:
            percentile = fc.percentile_rank(snap.momentum_12_1, momentum_peers)
            snap.momentum_score = None if percentile is None else percentile * 100
        else:
            snap.missing.append("momentum_unavailable")
        if snap.annualized_volatility is not None and volatility_peers:
            percentile = fc.percentile_rank(
                -snap.annualized_volatility,
                [-value for value in volatility_peers],
            )
            snap.low_volatility_score = None if percentile is None else percentile * 100
        else:
            snap.missing.append("low_volatility_unavailable")
    return snapshots

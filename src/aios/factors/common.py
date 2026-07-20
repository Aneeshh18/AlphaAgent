"""Shared PIT-correct helpers used across all factors.

These functions are the SINGLE SOURCE OF TRUTH for reading fundamentals.
Both Quality and Value factors go through them, guaranteeing identical
point-in-time semantics and preventing logic drift between modules.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from itertools import pairwise

from aios.storage.store import Store

_ANNUAL_PERIOD = re.compile(r"^FY(?P<year>\d{4})$")
_QUARTER_PERIOD = re.compile(r"^Q(?P<quarter>[1-4])_(?P<year>\d{4})$")

# Every fundamental currently consumed by Quality or Value. A decision-scoped
# snapshot fetches this set once for each ticker/date, allowing both factors to
# reuse identical PIT rows without maintaining a stale cross-run cache.
_FACTOR_METRICS = (
    "capex",
    "cash",
    "cfo",
    "current_assets",
    "current_liabilities",
    "debt_total",
    "depreciation",
    "eps_diluted",
    "gross_profit",
    "net_income",
    "operating_income",
    "revenue",
    "shares_out",
    "stockholders_equity",
    "total_assets",
)


class FactorDataCache:
    """Immutable-read cache for one factor decision and one Store instance."""

    def __init__(self, store: Store, tickers: list[str] | None = None) -> None:
        self.store = store
        self._fundamentals: dict[tuple[str, str], dict[str, list[dict]]] = {}
        self._metric_values: dict[tuple[str, str, str, bool], float | None] = {}
        self._histories: dict[tuple[str, str, str], list[dict]] = {}
        self._ttm_values: dict[tuple[str, str, str], float | None] = {}
        self._prices: dict[tuple[str, str], float | None] = {}
        self._financials: dict[str, bool] = {}
        self._preload_financial_classification(tickers or [])

    @property
    def fundamental_snapshot_count(self) -> int:
        return len(self._fundamentals)

    def _preload_financial_classification(self, tickers: list[str]) -> None:
        normalized = sorted({ticker.upper() for ticker in tickers})
        if not normalized:
            return
        placeholders = ",".join("?" for _ in normalized)
        rows = self.store.query(
            f"SELECT ticker, sic_code FROM securities WHERE ticker IN ({placeholders})",
            tuple(normalized),
        )
        sic_by_ticker = {row["ticker"]: row.get("sic_code") for row in rows}
        for ticker in normalized:
            sic_code = sic_by_ticker.get(ticker)
            digits = "" if sic_code is None else "".join(c for c in str(sic_code) if c.isdigit())
            self._financials[ticker] = digits.startswith(FINANCIALS_SIC_PREFIXES)

    def _rows(self, ticker: str, as_of: str) -> dict[str, list[dict]]:
        key = (ticker.upper(), as_of)
        cached = self._fundamentals.get(key)
        if cached is not None:
            return cached
        grouped: dict[str, list[dict]] = {}
        for row in self.store.pit_factor_fundamentals(ticker, as_of, list(_FACTOR_METRICS)):
            grouped.setdefault(row["metric"], []).append(row)
        self._fundamentals[key] = grouped
        return grouped

    def metric_value(
        self,
        ticker: str,
        as_of: str,
        metric: str,
        use_quarter: bool,
    ) -> float | None:
        key = (ticker.upper(), as_of, metric, use_quarter)
        if key in self._metric_values:
            return self._metric_values[key]
        rows = self._rows(ticker, as_of).get(metric, [])
        latest = max(
            rows,
            key=lambda row: (row["as_of_date"], row["period_end"]),
            default=None,
        )
        value = None if latest is None else latest["quarter_value" if use_quarter else "value"]
        self._metric_values[key] = value
        return value

    def history(self, ticker: str, as_of: str, metric: str) -> list[dict]:
        key = (ticker.upper(), as_of, metric)
        cached = self._histories.get(key)
        if cached is not None:
            return cached
        rows = [
            {
                "period_end": row["period_end"],
                "fiscal_period": row["fiscal_period"],
                "quarter_value": row["quarter_value"],
            }
            for row in self._rows(ticker, as_of).get(metric, [])
            if row["quarter_value"] is not None
        ]
        self._histories[key] = rows
        return rows

    def latest_price(self, ticker: str, as_of: str) -> float | None:
        key = (ticker.upper(), as_of)
        if key not in self._prices:
            row = self.store.latest_price(ticker, as_of)
            close = None if row is None else row["close"]
            self._prices[key] = float(close) if close is not None else None
        return self._prices[key]

    def is_financials(self, ticker: str) -> bool:
        normalized = ticker.upper()
        if normalized not in self._financials:
            rows = self.store.query(
                "SELECT sic_code FROM securities WHERE ticker = ?",
                (normalized,),
            )
            sic_code = rows[0].get("sic_code") if rows else None
            digits = "" if sic_code is None else "".join(c for c in str(sic_code) if c.isdigit())
            self._financials[normalized] = digits.startswith(FINANCIALS_SIC_PREFIXES)
        return self._financials[normalized]


_ACTIVE_FACTOR_CACHE: ContextVar[FactorDataCache | None] = ContextVar(
    "active_factor_cache",
    default=None,
)


@contextmanager
def factor_cache_scope(
    store: Store,
    tickers: list[str] | None = None,
) -> Iterator[FactorDataCache]:
    """Share PIT reads inside one decision and discard them on scope exit."""
    current = _ACTIVE_FACTOR_CACHE.get()
    if current is not None and current.store is store:
        yield current
        return
    cache = FactorDataCache(store, tickers)
    token = _ACTIVE_FACTOR_CACHE.set(cache)
    try:
        yield cache
    finally:
        _ACTIVE_FACTOR_CACHE.reset(token)


def _factor_cache(store: Store) -> FactorDataCache | None:
    cache = _ACTIVE_FACTOR_CACHE.get()
    return cache if cache is not None and cache.store is store else None


def metric_value(
    store: Store, ticker: str, as_of: str, metric: str, use_quarter: bool
) -> float | None:
    """Latest value of a metric known as-of `as_of`.

    use_quarter=True  → flow metric, returns the span-selected quarter_value
    use_quarter=False → instant/balance metric, returns the raw value
    """
    cache = _factor_cache(store)
    if cache is not None:
        return cache.metric_value(ticker, as_of, metric, use_quarter)
    rows = store.pit_fundamentals(ticker, as_of, metrics=[metric])
    if not rows:
        return None
    r = rows[0]
    if use_quarter:
        return r.get("quarter_value")
    return r.get("value")


def deduped_history(store: Store, ticker: str, as_of: str, metric: str) -> list[dict]:
    """PIT-deduped history for a metric: latest quarter_value per period_end.

    Dedupes by period_end keeping the most recent as_of_date (latest restatement).
    Sorted ascending by period_end. Only rows known as-of `as_of`.
    """
    cache = _factor_cache(store)
    if cache is not None:
        return cache.history(ticker, as_of, metric)
    return store.fundamental_history(ticker, as_of, metric)


def _annual_year(row: dict) -> int | None:
    match = _ANNUAL_PERIOD.fullmatch(str(row.get("fiscal_period") or "").strip().upper())
    return int(match.group("year")) if match else None


def _quarter_key(row: dict) -> tuple[int, int] | None:
    """Return ``(fiscal_year, fiscal_quarter)`` for an exact SEC-style label."""
    match = _QUARTER_PERIOD.fullmatch(str(row.get("fiscal_period") or "").strip().upper())
    if match is None:
        return None
    return int(match.group("year")), int(match.group("quarter"))


def _next_quarter(key: tuple[int, int]) -> tuple[int, int]:
    year, quarter = key
    return (year, quarter + 1) if quarter < 4 else (year + 1, 1)


def _sum_consecutive_standalone_quarters(hist: list[dict]) -> float | None:
    """Sum the latest four exact, sequential standalone fiscal quarters."""
    standalone = [
        row
        for row in hist
        if not str(row.get("fiscal_period") or "").strip().upper().startswith("FY")
    ]
    if len(standalone) < 4:
        return None

    last_four = standalone[-4:]
    keys = [_quarter_key(row) for row in last_four]
    if any(key is None for key in keys):
        return None
    exact_keys = [key for key in keys if key is not None]
    if any(current != _next_quarter(previous) for previous, current in pairwise(exact_keys)):
        return None
    return sum(float(row["quarter_value"]) for row in last_four)


def ttm_sum(store: Store, ticker: str, as_of: str, metric: str) -> float | None:
    """Trailing-twelve-months value of a flow metric, PIT-correct.

    TTM = latest_annual + Σ(post_annual_quarters) − Σ(matching_prior_year_quarters)

    Roll-forwards require exact fiscal-quarter identity: Q1 in the new fiscal
    year subtracts Q1 from the annual fiscal year, Q2 subtracts Q2, and so on.
    Missing, duplicate, or non-consecutive quarters fail closed instead of
    returning an annual fallback that no longer represents the trailing year.
    """
    cache = _factor_cache(store)
    cache_key = (ticker.upper(), as_of, metric)
    if cache is not None and cache_key in cache._ttm_values:
        return cache._ttm_values[cache_key]

    hist = deduped_history(store, ticker, as_of, metric)
    value = _ttm_sum_from_history(hist)
    if cache is not None:
        cache._ttm_values[cache_key] = value
    return value


def _ttm_sum_from_history(hist: list[dict]) -> float | None:
    """Apply the strict TTM policy to an already PIT-deduped history."""
    if not hist:
        return None

    annual_rows = [
        row for row in hist if str(row.get("fiscal_period") or "").strip().upper().startswith("FY")
    ]
    if not annual_rows:
        return _sum_consecutive_standalone_quarters(hist)

    latest_annual = annual_rows[-1]
    annual_end = latest_annual["period_end"]
    annual_value = float(latest_annual["quarter_value"])

    post_annual = [
        row
        for row in hist
        if row["period_end"] > annual_end
        and not str(row.get("fiscal_period") or "").strip().upper().startswith("FY")
    ]
    if not post_annual:
        return annual_value

    annual_year = _annual_year(latest_annual)
    post_keys = [_quarter_key(row) for row in post_annual]
    if annual_year is None or any(key is None for key in post_keys):
        return None
    exact_post_keys = [key for key in post_keys if key is not None]
    if len(exact_post_keys) > 4 or exact_post_keys != [
        (annual_year + 1, quarter) for quarter in range(1, len(exact_post_keys) + 1)
    ]:
        return None

    prior_by_key: dict[tuple[int, int], list[dict]] = {}
    for row in hist:
        if row["period_end"] > annual_end:
            continue
        key = _quarter_key(row)
        if key is not None:
            prior_by_key.setdefault(key, []).append(row)

    matching_prior: list[dict] = []
    for _post_year, quarter in exact_post_keys:
        matches = prior_by_key.get((annual_year, quarter), [])
        if len(matches) != 1:
            return None
        matching_prior.append(matches[0])

    added = sum(float(row["quarter_value"]) for row in post_annual)
    subtracted = sum(float(row["quarter_value"]) for row in matching_prior)
    return annual_value + added - subtracted


def shift_year(as_of: str) -> str:
    """Subtract one year from a YYYY-MM-DD string."""
    try:
        y, m, d = as_of.split("-")
        return f"{int(y) - 1}-{m}-{d}"
    except Exception:
        return as_of


def latest_price(store: Store, ticker: str, as_of: str) -> float | None:
    """Most recent close price on or before `as_of` (PIT-correct market data)."""
    cache = _factor_cache(store)
    if cache is not None:
        return cache.latest_price(ticker, as_of)
    row = store.latest_price(ticker, as_of)
    if row is None:
        return None
    c = row["close"]
    return float(c) if c is not None else None


def market_cap(
    store: Store, ticker: str, as_of: str, shares_out: float | None = None
) -> float | None:
    """Market capitalization as-of `as_of` = latest_price × shares_outstanding."""
    price = latest_price(store, ticker, as_of)
    if price is None:
        return None
    if shares_out is None:
        shares_out = metric_value(store, ticker, as_of, "shares_out", use_quarter=False)
    if shares_out is None:
        return None
    return price * shares_out


def percentile_rank(value: float, peers: list[float]) -> float | None:
    """Percentile rank of `value` within `peers` (0.0–1.0).

    Higher value = higher percentile. Cheaper stocks (low P/E) should be
    inverted by the caller before ranking.
    """
    peers = [p for p in peers if p is not None]
    if not peers:
        return None
    n_below = sum(1 for p in peers if value > p)
    return n_below / len(peers)


def enterprise_value(
    store: Store,
    ticker: str,
    as_of: str,
    mcap: float | None,
    debt_total: float | None,
    cash: float | None,
) -> float | None:
    """Enterprise value = market cap + total debt − cash."""
    if mcap is None or debt_total is None or cash is None:
        return None
    return mcap + debt_total - cash


# --- Sector classification (for financials-specific factor models) -----------
# SIC code ranges that indicate a bank/depository/financial institution.
# These business models don't fit the standard ROIC formula (deposits = "debt"
# is operational, not financial leverage), so they need a parallel quality path.
FINANCIALS_SIC_PREFIXES = (
    "60",  # Depository Institutions (banks): 6021, 6022, 6029...
    "61",  # Non-depository Credit Institutions
    "64",  # Insurance
)


def is_financials(store: Store, ticker: str) -> bool:
    """Detect if a ticker is a bank/financial institution via SIC code."""
    cache = _factor_cache(store)
    if cache is not None:
        return cache.is_financials(ticker)
    rows = store.query("SELECT sic_code FROM securities WHERE ticker = ?", (ticker.upper(),))
    if not rows or not rows[0].get("sic_code"):
        return False
    sic = str(rows[0]["sic_code"]).strip()
    # SIC codes can be "6021" or "National Commercial Banks / 6021"
    digits = "".join(c for c in sic if c.isdigit())
    return digits.startswith(FINANCIALS_SIC_PREFIXES)

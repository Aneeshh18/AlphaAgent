"""Shared PIT-correct helpers used across all factors.

These functions are the SINGLE SOURCE OF TRUTH for reading fundamentals.
Both Quality and Value factors go through them, guaranteeing identical
point-in-time semantics and preventing logic drift between modules.
"""

from __future__ import annotations

from aios.storage.store import Store


def metric_value(
    store: Store, ticker: str, as_of: str, metric: str, use_quarter: bool
) -> float | None:
    """Latest value of a metric known as-of `as_of`.

    use_quarter=True  → flow metric, returns the span-selected quarter_value
    use_quarter=False → instant/balance metric, returns the raw value
    """
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
    return store.fundamental_history(ticker, as_of, metric)


def ttm_sum(store: Store, ticker: str, as_of: str, metric: str) -> float | None:
    """Trailing-twelve-months value of a flow metric, PIT-correct.

    TTM = latest_annual + Σ(post_annual_quarters) − Σ(matching_prior_year_quarters)
    """
    hist = deduped_history(store, ticker, as_of, metric)
    if len(hist) < 4:
        return None

    annual_rows = [r for r in hist if str(r["fiscal_period"]).startswith("FY")]
    if not annual_rows:
        return sum(r["quarter_value"] for r in hist[-4:])

    latest_annual = annual_rows[-1]
    annual_end = latest_annual["period_end"]
    annual_val = latest_annual["quarter_value"]

    post_annual = [
        r
        for r in hist
        if r["period_end"] > annual_end and not str(r["fiscal_period"]).startswith("FY")
    ]
    if not post_annual:
        return annual_val

    pre_annual_quarters = [
        r
        for r in hist
        if r["period_end"] <= annual_end and not str(r["fiscal_period"]).startswith("FY")
    ]
    n = len(post_annual)
    if len(pre_annual_quarters) < n:
        return annual_val
    matching_prior = pre_annual_quarters[-n:]

    add = sum(r["quarter_value"] for r in post_annual)
    sub = sum(r["quarter_value"] for r in matching_prior)
    return annual_val + add - sub


def shift_year(as_of: str) -> str:
    """Subtract one year from a YYYY-MM-DD string."""
    try:
        y, m, d = as_of.split("-")
        return f"{int(y) - 1}-{m}-{d}"
    except Exception:
        return as_of


def latest_price(store: Store, ticker: str, as_of: str) -> float | None:
    """Most recent close price on or before `as_of` (PIT-correct market data)."""
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
    rows = store.query("SELECT sic_code FROM securities WHERE ticker = ?", (ticker.upper(),))
    if not rows or not rows[0].get("sic_code"):
        return False
    sic = str(rows[0]["sic_code"]).strip()
    # SIC codes can be "6021" or "National Commercial Banks / 6021"
    digits = "".join(c for c in sic if c.isdigit())
    return digits.startswith(FINANCIALS_SIC_PREFIXES)

"""Quality factor — the highest-weight factor in the strategy.

ACADEMIC BASIS
--------------
- Novy-Marx (2013): gross profitability (gp/assets) is a clean quality signal.
- Piotroski (2000): F-Score (0-9) predicts returns for value stocks.
- Altman (1968, 2005): Z-Score predicts bankruptcy risk.

All computations are POINT-IN-TIME: they read fundamentals whose as_of_date <=
the decision date, so backtests and live analysis never see future data.

FORMULAS (TTM = trailing twelve months, sum of last 4 quarters' flow values)
----------
  ROIC          = NOPAT / invested_capital
                  NOPAT             = operating_income_ttm * (1 - tax_rate)
                  invested_capital  = debt_total + stockholders_equity
                  (tax_rate assumed 21% US federal — simplified, documented)
  FCF margin    = (cfo_ttm - capex_ttm) / revenue_ttm
  Gross margin  = gross_profit_ttm / revenue_ttm
  Piotroski F   = 0-9 score from 9 binary profitability/leverage/efficiency tests
  Altman Z      = bankruptcy risk score (higher = safer; <1.81 = distress zone)

Composite Quality Score: percentile-ranked blend of the above (excluding Z,
which is a veto/safety signal, not a "more is better" signal).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from structlog import get_logger

from aios.storage.store import Store, get_store

log = get_logger(__name__)

ASSUMED_TAX_RATE = 0.21  # US federal corporate rate; documented simplification.


@dataclass
class QualitySnapshot:
    """PIT-correct quality metrics for one ticker as of one date."""

    ticker: str
    as_of: str
    # Core signals
    roic: float | None = None
    fcf_margin: float | None = None
    gross_margin: float | None = None
    piotroski_f: int | None = None
    piotroski_evaluated: int | None = None  # how many of 9 criteria had data
    altman_z: float | None = None
    # Raw inputs kept for transparency / debugging
    ttm_revenue: float | None = None
    ttm_operating_income: float | None = None
    ttm_cfo: float | None = None
    ttm_capex: float | None = None
    ttm_gross_profit: float | None = None
    invested_capital: float | None = None
    # Diagnostics
    inputs_complete: bool = False
    missing: list[str] = field(default_factory=list)
    # Financials-specific (banks) — None for non-financials
    _is_financials: bool = False
    _bank_roe: float | None = None
    _bank_equity_ratio: float | None = None
    _bank_net_margin: float | None = None


def _metric_value(
    store: Store, ticker: str, as_of: str, metric: str, use_quarter: bool
) -> float | None:
    """Get the latest value of a metric known as_of `as_of`.

    use_quarter=True for flow metrics (we use quarter_value), False for
    instant/balance metrics (we use the raw value).
    """
    rows = store.pit_fundamentals(ticker, as_of, metrics=[metric])
    if not rows:
        return None
    r = rows[0]
    if use_quarter:
        return r.get("quarter_value")
    return r.get("value")


def _deduped_history(
    store: Store, ticker: str, as_of: str, metric: str
) -> list[dict]:
    """Return PIT-deduped history for a metric: latest quarter_value per period_end.

    Dedupes by period_end keeping the most recent as_of_date (latest restatement),
    all known as-of `as_of`. Sorted ascending by period_end.
    """
    sql = """
        WITH ranked AS (
            SELECT period_end, fiscal_period, quarter_value,
                   ROW_NUMBER() OVER (
                       PARTITION BY period_end
                       ORDER BY as_of_date DESC
                   ) AS rn
            FROM fundamentals
            WHERE ticker = ?
              AND metric = ?
              AND as_of_date <= CAST(? AS DATE)
              AND quarter_value IS NOT NULL
        )
        SELECT period_end, fiscal_period, quarter_value FROM ranked
        WHERE rn = 1
        ORDER BY period_end ASC
    """
    return store.query(sql, (ticker, metric, str(as_of)))


def _ttm_sum(
    store: Store, ticker: str, as_of: str, metric: str
) -> float | None:
    """Trailing-twelve-months value of a flow metric, PIT-correct.

    THE TTM PITFALL (this is why the naive "sum last 4 quarters" is wrong):
    Companies report both a fiscal-year ANNUAL row AND its constituent quarters.
    For Apple: FY2024 annual (ends 2024-09-28) = $123B, but Q1+Q2+Q3 of that
    same year are ALSO stored as separate rows. Summing "last 4 distinct
    period_ends" → annual + 3 of its own quarters = double-counted (~2x too big).

    CORRECT TTM (standard finance formula):
      TTM = latest_annual
            + Σ(quarters AFTER the annual, in the new fiscal year so far)
            - Σ(matching quarters from the PRIOR fiscal year)

    Example (as of early 2025, after FY2024 annual filed):
      TTM = FY2024_full_year + Q1_2025 - Q1_2024
    The "+Q1_2025" rolls the year forward one quarter; "-Q1_2024" drops the
    matching old quarter so we keep exactly 12 months.

    If no annual is known yet, fall back to summing the latest 4 consecutive
    quarters (works when only intra-year data exists).
    """
    hist = _deduped_history(store, ticker, as_of, metric)
    if len(hist) < 4:
        return None

    # Identify the latest ANNUAL period (fiscal_period like 'FY20xx').
    annual_rows = [r for r in hist if str(r["fiscal_period"]).startswith("FY")]
    if not annual_rows:
        # No annual reported yet: sum last 4 quarters (best effort).
        last4 = hist[-4:]
        return sum(r["quarter_value"] for r in last4)

    latest_annual = annual_rows[-1]
    annual_end = latest_annual["period_end"]
    annual_val = latest_annual["quarter_value"]

    # Quarters strictly AFTER the annual period_end = new fiscal year so far.
    post_annual = [r for r in hist if r["period_end"] > annual_end
                   and not str(r["fiscal_period"]).startswith("FY")]
    # If we have post-annual quarters, we need the matching prior-year ones.
    if not post_annual:
        # The annual IS the TTM (no new quarters filed since annual).
        return annual_val

    # Build the prior-year matching set: quarters ending just before the annual.
    # The count of prior-year quarters to subtract = len(post_annual).
    pre_annual_quarters = [r for r in hist if r["period_end"] <= annual_end
                           and not str(r["fiscal_period"]).startswith("FY")]
    # Take the last N quarters before/at the annual (N = number of post_annual).
    n = len(post_annual)
    matching_prior = pre_annual_quarters[-n:] if len(pre_annual_quarters) >= n else None
    if matching_prior is None:
        # Not enough prior-year history to do clean TTM; fall back to annual.
        return annual_val

    add = sum(r["quarter_value"] for r in post_annual)
    sub = sum(r["quarter_value"] for r in matching_prior)
    ttm = annual_val + add - sub
    return ttm


# ----------------------------------------------------------------------
# Sub-scores
# ----------------------------------------------------------------------
def _piotroski_f_score(
    store: Store,
    ticker: str,
    as_of: str,
    prior_as_of: str,
    ttm_net_income: float | None,
    ttm_cfo: float | None,
    ttm_gross_profit: float | None,
    ttm_revenue: float | None,
    cur_roa: float | None,
    prior_roa: float | None,
    total_assets: float | None,
    debt_total: float | None,
    current_assets: float | None,
    current_liabilities: float | None,
    shares_out: float | None,
) -> tuple[int | None, int]:
    """Compute the FULL Piotroski F-Score (0-9). Piotroski (2000).

    Returns (score, criteria_evaluated). If prior-year data is missing for some
    criteria, those criteria are skipped (not awarded) and criteria_evaluated < 9.
    The caller can tell a partial score from a full one.

    The 9 criteria:
      PROFITABILITY
        1. ROA > 0
        2. CFO > 0 (operating cash flow positive)
        3. ROA rising (current > prior year)
        4. Earnings quality: CFO > Net Income
      LEVERAGE / FUNDING
        5. Leverage falling (debt/assets lower than prior year) — proxy: long-term
           debt ratio. We use debt_total/assets.
        6. Liquidity rising (current ratio higher than prior year)
        7. No new equity issuance (shares_out <= prior year, i.e. no dilution)
      EFFICIENCY
        8. Gross margin rising (TTM gross profit / TTM revenue)
        9. Asset turnover rising (TTM revenue / total assets)
    """
    score = 0
    evaluated = 0

    # --- Profitability (4) ---
    # 1. ROA > 0
    evaluated += 1
    if cur_roa is not None and cur_roa > 0:
        score += 1
    # 2. CFO > 0
    evaluated += 1
    if ttm_cfo is not None and ttm_cfo > 0:
        score += 1
    # 3. Rising ROA
    evaluated += 1
    if cur_roa is not None and prior_roa is not None and cur_roa > prior_roa:
        score += 1
    # 4. Earnings quality: CFO > Net Income
    evaluated += 1
    if ttm_cfo is not None and ttm_net_income is not None and ttm_cfo > ttm_net_income:
        score += 1

    # --- Leverage / Funding (3) — need prior-year balance sheet ---
    prior_debt = _metric_value(store, ticker, prior_as_of, "debt_total", use_quarter=False)
    prior_assets = _metric_value(store, ticker, prior_as_of, "total_assets", use_quarter=False)
    prior_ca = _metric_value(store, ticker, prior_as_of, "current_assets", use_quarter=False)
    prior_cl = _metric_value(store, ticker, prior_as_of, "current_liabilities", use_quarter=False)
    prior_shares = _metric_value(store, ticker, prior_as_of, "shares_out", use_quarter=False)

    # 5. Lower leverage (debt/assets falling)
    if (
        debt_total is not None and total_assets and total_assets > 0
        and prior_debt is not None and prior_assets and prior_assets > 0
    ):
        evaluated += 1
        if (debt_total / total_assets) < (prior_debt / prior_assets):
            score += 1
    # 6. Rising current ratio (CA/CL)
    if (
        current_assets is not None and current_liabilities and current_liabilities > 0
        and prior_ca is not None and prior_cl and prior_cl > 0
    ):
        evaluated += 1
        if (current_assets / current_liabilities) > (prior_ca / prior_cl):
            score += 1
    # 7. No new equity issuance (shares not growing)
    if shares_out is not None and prior_shares is not None and prior_shares > 0:
        evaluated += 1
        # Allow tiny noise (< 1% growth = buyback noise). Genuine issuance > 1%.
        if shares_out <= prior_shares * 1.01:
            score += 1

    # --- Efficiency (2) — need prior-year revenue/gross profit ---
    prior_ttm_revenue = _ttm_sum(store, ticker, prior_as_of, "revenue")
    prior_ttm_gp = _ttm_sum(store, ticker, prior_as_of, "gross_profit")
    # 8. Rising gross margin
    if (
        ttm_gross_profit and ttm_revenue and ttm_revenue > 0
        and prior_ttm_gp and prior_ttm_revenue and prior_ttm_revenue > 0
    ):
        evaluated += 1
        if (ttm_gross_profit / ttm_revenue) > (prior_ttm_gp / prior_ttm_revenue):
            score += 1
    # 9. Rising asset turnover (revenue / assets)
    if (
        ttm_revenue and total_assets and total_assets > 0
        and prior_ttm_revenue and prior_assets and prior_assets > 0
    ):
        evaluated += 1
        if (ttm_revenue / total_assets) > (prior_ttm_revenue / prior_assets):
            score += 1

    return score, evaluated


def _altman_z(
    total_assets: float,
    current_assets: float,
    current_liabilities: float,
    retained_earnings_proxy: float | None,
    ttm_operating_income: float,
    ttm_revenue: float,
    market_cap: float | None,
) -> float | None:
    """Altman Z-Score (original 1968 model for public manufacturers).

    Z = 1.2*X1 + 1.4*X2 + 3.3*X3 + 0.6*X4 + 1.0*X5
      X1 = working capital / total assets
      X2 = retained earnings / total assets
      X3 = EBIT / total assets  (we use operating_income as EBIT proxy)
      X4 = market value equity / book liabilities
      X5 = sales / total assets

    Note: X4 needs market cap. If None, we return a Z' (private-firm variant)
    partial and flag it. For now, require market_cap; later we wire prices in.
    """
    if total_assets <= 0:
        return None
    x1 = (current_assets - current_liabilities) / total_assets
    x2 = (retained_earnings_proxy or 0) / total_assets
    x3 = ttm_operating_income / total_assets
    if market_cap is None:
        # Use book equity as X4 fallback (Z' private model weighting differs,
        # but this gives an indicative number flagged as such).
        return None
    # Total liabilities as the X4 denominator
    # (caller doesn't pass total_liabilities separately; approximate via
    # assets - equity if needed. For cleanliness, accept it can be None.)
    x4 = None  # set below via store when we have total_liabilities
    x5 = ttm_revenue / total_assets
    # X4 needs total_liabilities — defer; return partial without X4 weighting
    # is wrong, so we return None and compute it fully elsewhere.
    _ = (x1, x2, x3, x4, x5)
    return None  # placeholder; Altman Z needs total_liabilities + market_cap


# ----------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------
def compute_quality(
    ticker: str,
    as_of: str | date,
    store: Store | None = None,
    market_cap: float | None = None,
) -> QualitySnapshot:
    """Compute the full quality snapshot for `ticker` as of `as_of` (PIT)."""
    store = store or get_store()
    as_of = str(as_of)
    snap = QualitySnapshot(ticker=ticker.upper(), as_of=as_of)
    missing: list[str] = []

    # --- TTM flow metrics (sum of last 4 quarters) ---
    snap.ttm_revenue = _ttm_sum(store, ticker, as_of, "revenue")
    snap.ttm_operating_income = _ttm_sum(store, ticker, as_of, "operating_income")
    snap.ttm_cfo = _ttm_sum(store, ticker, as_of, "cfo")
    snap.ttm_capex = _ttm_sum(store, ticker, as_of, "capex")
    snap.ttm_gross_profit = _ttm_sum(store, ticker, as_of, "gross_profit")

    # --- Balance-sheet (instant) metrics: latest known ---
    total_assets = _metric_value(store, ticker, as_of, "total_assets", use_quarter=False)
    stockholders_equity = _metric_value(store, ticker, as_of, "stockholders_equity", use_quarter=False)
    debt_total = _metric_value(store, ticker, as_of, "debt_total", use_quarter=False)
    current_assets = _metric_value(store, ticker, as_of, "current_assets", use_quarter=False)
    current_liabilities = _metric_value(store, ticker, as_of, "current_liabilities", use_quarter=False)
    shares_out = _metric_value(store, ticker, as_of, "shares_out", use_quarter=False)

    for name, val in {
        "ttm_revenue": snap.ttm_revenue,
        "ttm_operating_income": snap.ttm_operating_income,
        "ttm_cfo": snap.ttm_cfo,
        "ttm_capex": snap.ttm_capex,
        "total_assets": total_assets,
        "stockholders_equity": stockholders_equity,
    }.items():
        if val is None:
            missing.append(name)

    # --- ROIC (NOPAT / average invested capital) ---
    # Uses (beginning + ending) / 2 for invested capital — the standard
    # conservative choice. NOPAT = operating_income * (1 - tax_rate).
    # Methodology note: this may differ from Bloomberg/Damodaran figures
    # which use stricter IC definitions (ex-cash, multi-year avg, etc.).
    if (
        snap.ttm_operating_income is not None
        and debt_total is not None
        and stockholders_equity is not None
    ):
        ic_now = debt_total + stockholders_equity
        # Prior-year balance sheet for averaging
        prior_as_of = _shift_year(as_of)
        prior_debt = _metric_value(store, ticker, prior_as_of, "debt_total", use_quarter=False)
        prior_equity = _metric_value(store, ticker, prior_as_of, "stockholders_equity", use_quarter=False)
        ic_prior = None
        if prior_debt is not None and prior_equity is not None:
            ic_prior = prior_debt + prior_equity
        invested_capital = (ic_now + ic_prior) / 2 if ic_prior else ic_now
        snap.invested_capital = invested_capital
        if invested_capital > 0:
            nopat = snap.ttm_operating_income * (1 - ASSUMED_TAX_RATE)
            snap.roic = nopat / invested_capital

    # --- FCF margin ---
    if (
        snap.ttm_cfo is not None
        and snap.ttm_capex is not None
        and snap.ttm_revenue
        and snap.ttm_revenue > 0
    ):
        fcf = snap.ttm_cfo - abs(snap.ttm_capex)
        snap.fcf_margin = fcf / snap.ttm_revenue

    # --- Gross margin ---
    if snap.ttm_gross_profit and snap.ttm_revenue and snap.ttm_revenue > 0:
        snap.gross_margin = snap.ttm_gross_profit / snap.ttm_revenue

    # --- Piotroski F-Score (full 9 criteria) ---
    ttm_net_income = _ttm_sum(store, ticker, as_of, "net_income")
    prior_as_of = _shift_year(as_of)
    if total_assets and total_assets > 0 and ttm_net_income is not None:
        cur_roa = ttm_net_income / total_assets
        prior_ni = _ttm_sum(store, ticker, prior_as_of, "net_income")
        prior_assets = _metric_value(store, ticker, prior_as_of, "total_assets", use_quarter=False)
        prior_roa = (
            prior_ni / prior_assets if prior_ni and prior_assets and prior_assets > 0 else None
        )
        snap.piotroski_f, snap.piotroski_evaluated = _piotroski_f_score(
            store, ticker, as_of, prior_as_of,
            ttm_net_income, snap.ttm_cfo, snap.ttm_gross_profit, snap.ttm_revenue,
            cur_roa, prior_roa,
            total_assets, debt_total, current_assets, current_liabilities, shares_out,
        )

    snap.inputs_complete = len(missing) == 0
    snap.missing = missing
    log.info(
        "quality.computed",
        ticker=ticker,
        as_of=as_of,
        roic=snap.roic,
        fcf_margin=snap.fcf_margin,
        gross_margin=snap.gross_margin,
        piotroski=snap.piotroski_f,
        complete=snap.inputs_complete,
        missing=missing,
    )
    return snap


def _shift_year(as_of: str) -> str:
    """Subtract one year from a YYYY-MM-DD date string (rough, for PIT lookback)."""
    try:
        y, m, d = as_of.split("-")
        return f"{int(y) - 1}-{m}-{d}"
    except Exception:
        return as_of


# ----------------------------------------------------------------------
# Financials-specific quality (banks / depositories)
# ----------------------------------------------------------------------
def compute_quality_financials(
    ticker: str,
    as_of: str,
    store: Store,
    snap: "QualitySnapshot",
) -> "QualitySnapshot":
    """Quality model for banks/financials where ROIC is meaningless.

    Bank business model: customer deposits ARE the funding (operational, not
    financial leverage). The standard ROIC denominator (debt + equity) treats
    deposits as leverage and produces nonsense numbers.

    Bank quality metrics used here:
      ROE           = net_income / stockholders_equity  (THE bank profitability metric)
      Equity ratio  = equity / total_assets             (capital strength; higher = safer)
      Efficiency    = 1 − (interest_expense / revenue)  (cost efficiency proxy)
      Net margin    = net_income / revenue              (profitability)

    ROIC is left as None (intentionally) — it's not meaningful for banks.
    """
    from aios.factors import common as fc

    prior_as_of = _shift_year(as_of)
    missing: list[str] = []

    ttm_net_income = fc.ttm_sum(store, ticker, as_of, "net_income")
    ttm_revenue = fc.ttm_sum(store, ticker, as_of, "revenue")
    ttm_int_exp = fc.ttm_sum(store, ticker, as_of, "interest_expense")
    equity = fc.metric_value(store, ticker, as_of, "stockholders_equity", False)
    total_assets = fc.metric_value(store, ticker, as_of, "total_assets", False)

    snap.ttm_revenue = ttm_revenue

    # ROE — the primary bank profitability metric
    if ttm_net_income is not None and equity and equity > 0:
        snap.roic = None  # explicitly not meaningful for banks
        snap._bank_roe = ttm_net_income / equity  # stashed; composite reads it
    elif equity is None:
        missing.append("stockholders_equity")

    # Equity ratio (capital strength)
    if equity is not None and total_assets and total_assets > 0:
        snap._bank_equity_ratio = equity / total_assets
    elif total_assets is None:
        missing.append("total_assets")

    # Net margin
    if ttm_net_income is not None and ttm_revenue and ttm_revenue > 0:
        snap._bank_net_margin = ttm_net_income / ttm_revenue

    # FCF margin — still meaningful for banks (operating cash flow matters)
    ttm_cfo = fc.ttm_sum(store, ticker, as_of, "cfo")
    ttm_capex = fc.ttm_sum(store, ticker, as_of, "capex")
    if ttm_cfo is not None and ttm_capex is not None and ttm_revenue and ttm_revenue > 0:
        snap.fcf_margin = (ttm_cfo - abs(ttm_capex)) / ttm_revenue

    # Piotroski (profitability subset still applies to banks)
    if total_assets and total_assets > 0 and ttm_net_income is not None:
        cur_roa = ttm_net_income / total_assets
        prior_ni = fc.ttm_sum(store, ticker, prior_as_of, "net_income")
        prior_assets = fc.metric_value(store, ticker, prior_as_of, "total_assets", False)
        prior_roa = (
            prior_ni / prior_assets
            if prior_ni and prior_assets and prior_assets > 0
            else None
        )
        snap.piotroski_f, snap.piotroski_evaluated = _piotroski_f_score(
            store, ticker, as_of, prior_as_of,
            ttm_net_income, ttm_cfo, None, ttm_revenue,
            cur_roa, prior_roa,
            total_assets, None, None, None,
            fc.metric_value(store, ticker, as_of, "shares_out", False),
        )

    snap.inputs_complete = len(missing) == 0
    snap.missing = missing
    snap._is_financials = True
    log.info(
        "quality.computed.financials",
        ticker=ticker, as_of=as_of,
        roe=snap._bank_roe, equity_ratio=snap._bank_equity_ratio,
        net_margin=snap._bank_net_margin, complete=snap.inputs_complete,
    )
    return snap

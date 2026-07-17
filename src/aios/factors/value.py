"""Value factor — the margin-of-safety layer.

ACADEMIC BASIS: Fama-French (1992), Lakonishok et al. (1994).

A stock is "cheap" when its valuation multiples are LOW relative to peers.
We compute 5 multiples, then convert each to a percentile rank across the
universe (cheapness = low multiple → high percentile). The composite Value
score is the equal-weighted average of these percentiles.

MULTIPLES (all TTM-based, PIT-correct):
  P/E       = price / TTM EPS
  EV/EBITDA = enterprise_value / TTM EBITDA
  P/FCF     = market_cap / (TTM CFO − TTM capex)
  EV/Sales  = enterprise_value / TTM revenue
  P/B       = market_cap / stockholders_equity

All valuation needs market price → we read the latest close ≤ as_of.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from structlog import get_logger

from aios.factors import common as fc
from aios.factors.policy import MIN_VALUE_MULTIPLES
from aios.storage.store import Store, get_store

log = get_logger(__name__)


@dataclass
class ValueSnapshot:
    ticker: str
    as_of: str
    # Raw multiples (None if not computable)
    pe: float | None = None
    ev_ebitda: float | None = None
    p_fcf: float | None = None
    ev_sales: float | None = None
    p_b: float | None = None
    # Percentile ranks within universe (0.0–1.0; higher = cheaper/better)
    pe_pct: float | None = None
    ev_ebitda_pct: float | None = None
    p_fcf_pct: float | None = None
    ev_sales_pct: float | None = None
    p_b_pct: float | None = None
    # Composite
    value_score: float | None = None  # 0–100
    multiples_available: int = 0
    # Raw inputs for transparency
    price: float | None = None
    market_cap: float | None = None
    enterprise_value: float | None = None
    ttm_eps: float | None = None
    ttm_ebitda: float | None = None
    ttm_depreciation: float | None = None
    ttm_fcf: float | None = None
    ttm_revenue: float | None = None
    stockholders_equity: float | None = None
    inputs_complete: bool = False
    missing: list[str] = field(default_factory=list)


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    """Division guarded against None/zero/negative-denominator."""
    if numerator is None or denominator is None:
        return None
    if denominator == 0:
        return None
    return numerator / denominator


def compute_value_raw(
    ticker: str,
    as_of: str | date,
    store: Store | None = None,
) -> ValueSnapshot:
    """Compute raw valuation multiples for one ticker, PIT-correct.

    No percentile ranking here — that needs the universe and is done in
    `compute_value_ranked`. This function is the per-ticker building block.
    """
    store = store or get_store()
    as_of = str(as_of)
    snap = ValueSnapshot(ticker=ticker.upper(), as_of=as_of)
    missing: list[str] = []

    # --- Price + market cap ---
    snap.price = fc.latest_price(store, ticker, as_of)
    snap.stockholders_equity = fc.metric_value(store, ticker, as_of, "stockholders_equity", False)
    if snap.price is None:
        missing.append("price")
    else:
        shares = fc.metric_value(store, ticker, as_of, "shares_out", False)
        if shares is None:
            missing.append("shares_out")
        else:
            snap.market_cap = snap.price * shares

    # --- Enterprise value ---
    debt = fc.metric_value(store, ticker, as_of, "debt_total", False)
    cash = fc.metric_value(store, ticker, as_of, "cash", False)
    snap.enterprise_value = fc.enterprise_value(store, ticker, as_of, snap.market_cap, debt, cash)
    if snap.enterprise_value is None:
        if debt is None:
            missing.append("debt_total")
        if cash is None:
            missing.append("cash")

    # --- TTM flow metrics ---
    snap.ttm_eps = fc.ttm_sum(store, ticker, as_of, "eps_diluted")
    ttm_operating_income = fc.ttm_sum(store, ticker, as_of, "operating_income")
    snap.ttm_depreciation = fc.ttm_sum(store, ticker, as_of, "depreciation")
    # Never substitute net income for EBITDA. Some historical databases may
    # still contain the old mislabeled `ebitda` rows; they are intentionally
    # ignored here until a clean operating-income + D&A pair is available.
    if ttm_operating_income is not None and snap.ttm_depreciation is not None:
        snap.ttm_ebitda = ttm_operating_income + abs(snap.ttm_depreciation)
    ttm_cfo = fc.ttm_sum(store, ticker, as_of, "cfo")
    ttm_capex = fc.ttm_sum(store, ticker, as_of, "capex")
    snap.ttm_revenue = fc.ttm_sum(store, ticker, as_of, "revenue")

    if ttm_cfo is not None and ttm_capex is not None:
        snap.ttm_fcf = ttm_cfo - abs(ttm_capex)

    # --- Multiples ---
    snap.pe = _safe_ratio(snap.price, snap.ttm_eps)
    snap.ev_ebitda = _safe_ratio(snap.enterprise_value, snap.ttm_ebitda)
    snap.p_fcf = _safe_ratio(snap.market_cap, snap.ttm_fcf)
    snap.ev_sales = _safe_ratio(snap.enterprise_value, snap.ttm_revenue)
    snap.p_b = _safe_ratio(snap.market_cap, snap.stockholders_equity)

    # Negatives/zero earnings make P/E meaningless → flag, don't rank.
    if snap.ttm_eps is not None and snap.ttm_eps <= 0:
        snap.pe = None
        missing.append("eps_nonpositive")

    snap.inputs_complete = len(missing) == 0
    snap.missing = missing
    return snap


def compute_value_ranked(
    tickers: list[str],
    as_of: str | date,
    store: Store | None = None,
) -> dict[str, ValueSnapshot]:
    """Compute raw multiples for every ticker, then percentile-rank each multiple.

    Cheapness convention: a LOW multiple = cheap = HIGH percentile.
    So we invert before ranking (rank of -value).
    """
    store = store or get_store()
    snaps: dict[str, ValueSnapshot] = {}
    for t in tickers:
        try:
            snaps[t.upper()] = compute_value_raw(t, as_of, store)
        except Exception as e:
            log.error("value.compute_failed", ticker=t, error=str(e))

    def _peer_values(field_name: str) -> list[float]:
        out = []
        for s in snaps.values():
            v = getattr(s, field_name)
            if v is not None and v > 0:
                out.append(v)
        return out

    # For each multiple, percentile-rank. Low multiple → high percentile (cheap).
    for multiple, pct_field in [
        ("pe", "pe_pct"),
        ("ev_ebitda", "ev_ebitda_pct"),
        ("p_fcf", "p_fcf_pct"),
        ("ev_sales", "ev_sales_pct"),
        ("p_b", "p_b_pct"),
    ]:
        peers = _peer_values(multiple)
        for s in snaps.values():
            v = getattr(s, multiple)
            if v is None or v <= 0 or not peers:
                continue
            # Cheapness: invert so cheaper (lower) ranks higher.
            cheapness_rank = fc.percentile_rank(-v, [-p for p in peers])
            setattr(s, pct_field, cheapness_rank)

    # Composite = mean of available percentile ranks × 100.
    for s in snaps.values():
        pcts = [s.pe_pct, s.ev_ebitda_pct, s.p_fcf_pct, s.ev_sales_pct, s.p_b_pct]
        pcts = [p for p in pcts if p is not None]
        s.multiples_available = len(pcts)
        if len(pcts) >= MIN_VALUE_MULTIPLES:
            s.value_score = (sum(pcts) / len(pcts)) * 100
        else:
            s.missing.append(f"minimum_value_multiples:{MIN_VALUE_MULTIPLES}")

    return snaps

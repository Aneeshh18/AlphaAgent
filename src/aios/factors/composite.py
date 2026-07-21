"""Composite factor engine — blends Quality + Value into the QV ranking.

STRATEGY ALIGNMENT (from the strategy doc, Module 4):
  Quality: highest-weight factor by default (25-35%)
  Value:   15-25%

The default weights are selected by the release-aware macro regime. The
initial regime tilts are a transparent policy hypothesis; they must be
validated by the future PIT backtest before being treated as evidence of
alpha. If the macro snapshot is missing or not PIT-ready, the engine uses the
baseline 60% Quality / 40% Value blend and marks the row explicitly.

Both sub-factors output a 0-100 score. The composite is a weighted blend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from time import perf_counter

from structlog import get_logger

from aios.factors import common as fc
from aios.factors.market_factors import compute_market_factors_ranked
from aios.factors.policy import (
    BASELINE_FACTOR_WEIGHTS,
    MIN_QUALITY_COMPONENTS,
    FactorWeights,
    QVMLFactorWeights,
    qvml_weights_for_regime,
    weights_for_regime,
)
from aios.factors.quality import compute_quality
from aios.factors.value import compute_value_ranked
from aios.macro.regime import MacroRegimeSnapshot, compute_regime
from aios.storage.store import Store, get_store

log = get_logger(__name__)

# Compatibility aliases for callers that used the original fixed policy.
QUALITY_WEIGHT = BASELINE_FACTOR_WEIGHTS.quality
VALUE_WEIGHT = BASELINE_FACTOR_WEIGHTS.value


@dataclass
class CompositeRow:
    ticker: str
    as_of: str
    # Quality (0-100)
    quality_score: float | None = None
    # Value (0-100)
    value_score: float | None = None
    momentum_score: float | None = None
    low_volatility_score: float | None = None
    # Composite QV (0-100)
    qv_score: float | None = None
    # Experimental four-factor composite. QV remains the certified default.
    qvml_score: float | None = None
    # Letter grade
    grade: str = "N/A"
    qvml_grade: str = "N/A"
    # Sub-metrics for display
    quality_components_available: int = 0
    value_multiples_available: int = 0
    roic: float | None = None
    fcf_margin: float | None = None
    gross_margin: float | None = None
    piotroski_f: int | None = None
    pe: float | None = None
    ev_ebitda: float | None = None
    p_fcf: float | None = None
    ev_sales: float | None = None
    p_b: float | None = None
    market_cap: float | None = None
    price: float | None = None
    momentum_12_1: float | None = None
    annualized_volatility: float | None = None
    market_price_observations: int = 0
    # Macro overlay evidence and the actual weights used for QV.
    macro_regime: str = "unknown"
    quality_weight: float = QUALITY_WEIGHT
    value_weight: float = VALUE_WEIGHT
    qvml_quality_weight: float = 0.36
    qvml_value_weight: float = 0.24
    qvml_momentum_weight: float = 0.25
    qvml_low_volatility_weight: float = 0.15
    regime_pit_ready: bool = False
    # Rank within universe
    quality_rank: int | None = None
    value_rank: int | None = None
    qv_rank: int | None = None
    qvml_rank: int | None = None
    missing: list[str] = field(default_factory=list)


def _grade(score: float | None) -> str:
    if score is None:
        return "N/A"
    if score >= 85:
        return "A+"
    if score >= 70:
        return "A"
    if score >= 55:
        return "B"
    if score >= 40:
        return "C"
    return "D"


def compute_composite(
    tickers: list[str],
    as_of: str | date,
    store: Store | None = None,
    regime_snapshot: MacroRegimeSnapshot | None = None,
    *,
    include_market_factors: bool = False,
) -> list[CompositeRow]:
    """Full QV composite ranking for a universe as-of a date.

    Returns a list sorted by qv_score descending (best opportunities first).
    """
    store = store or get_store()
    as_of = str(as_of)

    # Compute the regime once per universe. Accepting an injected snapshot is
    # useful for callers that already computed the PIT evidence and avoids a
    # second read, but never accepts a snapshot for a different decision date.
    snapshot = regime_snapshot or compute_regime(as_of, store)
    if snapshot.as_of != as_of:
        raise ValueError(
            f"regime snapshot date {snapshot.as_of} does not match composite date {as_of}"
        )
    regime_pit_ready = snapshot.is_pit_ready and snapshot.regime != "unknown"
    weights: FactorWeights = (
        weights_for_regime(snapshot.regime) if regime_pit_ready else BASELINE_FACTOR_WEIGHTS
    )
    macro_regime = snapshot.regime if regime_pit_ready else "unknown"
    qvml_weights: QVMLFactorWeights = qvml_weights_for_regime(macro_regime)

    # Quality and Value intentionally share one immutable decision-scoped
    # fundamental snapshot. The scope is discarded before this function
    # returns, so a later decision or post-ingest call can never reuse stale
    # rows. Public factor function signatures remain unchanged.
    factor_started = perf_counter()
    with fc.factor_cache_scope(store, tickers) as factor_cache:
        # 1. Optional price-only factors. Computing these first allows Value to
        # reuse the latest close from the same identity-safe history window.
        market_snaps = (
            compute_market_factors_ranked(tickers, as_of, store) if include_market_factors else {}
        )

        # 2. Value (universe-relative percentile ranks)
        value_snaps = compute_value_ranked(tickers, as_of, store)

        # 3. Quality per ticker + universe-relative rank
        q_snaps = {}
        for t in tickers:
            try:
                q_snaps[t.upper()] = compute_quality(t, as_of, store)
            except Exception as e:
                log.error("composite.quality_failed", ticker=t, error=str(e))

    # 4. Quality composite score (0-100). Industrial companies use ROIC, FCF
    #    margin, and gross margin. SIC-routed financials use ROE, equity ratio,
    #    and net margin because industrial capital and margin formulas are not
    #    meaningful for deposit-funded businesses. Piotroski is normalized by
    #    the number of criteria actually evaluated before percentile ranking.
    def _quality_components(s):
        piotroski = (
            s.piotroski_f / s.piotroski_evaluated
            if s.piotroski_f is not None and s.piotroski_evaluated
            else None
        )
        if s._is_financials:
            return {
                "bank_roe": s._bank_roe,
                "bank_equity_ratio": s._bank_equity_ratio,
                "bank_net_margin": s._bank_net_margin,
                "piotroski": piotroski,
            }
        return {
            "roic": s.roic,
            "fcf_margin": s.fcf_margin,
            "gross_margin": s.gross_margin,
            "piotroski": piotroski,
        }

    comp_names = [
        "roic",
        "fcf_margin",
        "gross_margin",
        "bank_roe",
        "bank_equity_ratio",
        "bank_net_margin",
        "piotroski",
    ]
    # Build peer lists for percentile ranking
    peer_lists = {c: [] for c in comp_names}
    for s in q_snaps.values():
        comps = _quality_components(s)
        for c in comp_names:
            v = comps.get(c)
            if v is not None:
                peer_lists[c].append(v)

    quality_scores: dict[str, float | None] = {}
    quality_component_counts: dict[str, int] = {}
    for t, s in q_snaps.items():
        comps = _quality_components(s)
        pcts = []
        for c in comp_names:
            v = comps.get(c)
            if v is None or not peer_lists[c]:
                continue
            pcts.append(fc.percentile_rank(v, peer_lists[c]) or 0.0)
        quality_component_counts[t] = len(pcts)
        quality_scores[t] = (
            (sum(pcts) / len(pcts) * 100) if len(pcts) >= MIN_QUALITY_COMPONENTS else None
        )

    # 5. Assemble rows
    rows: list[CompositeRow] = []
    for t in tickers:
        t = t.upper()
        qs = q_snaps.get(t)
        vs = value_snaps.get(t)
        ms = market_snaps.get(t)
        qscore = quality_scores.get(t)
        vscore = vs.value_score if vs else None
        momentum_score = ms.momentum_score if ms else None
        low_volatility_score = ms.low_volatility_score if ms else None

        row = CompositeRow(
            ticker=t,
            as_of=as_of,
            quality_score=qscore,
            value_score=vscore,
            momentum_score=momentum_score,
            low_volatility_score=low_volatility_score,
            quality_components_available=quality_component_counts.get(t, 0),
            value_multiples_available=vs.multiples_available if vs else 0,
            roic=qs.roic if qs else None,
            fcf_margin=qs.fcf_margin if qs else None,
            gross_margin=qs.gross_margin if qs else None,
            piotroski_f=qs.piotroski_f if qs else None,
            pe=vs.pe if vs else None,
            ev_ebitda=vs.ev_ebitda if vs else None,
            p_fcf=vs.p_fcf if vs else None,
            ev_sales=vs.ev_sales if vs else None,
            p_b=vs.p_b if vs else None,
            market_cap=vs.market_cap if vs else None,
            price=vs.price if vs else None,
            momentum_12_1=ms.momentum_12_1 if ms else None,
            annualized_volatility=ms.annualized_volatility if ms else None,
            market_price_observations=ms.price_observations if ms else 0,
            macro_regime=macro_regime,
            quality_weight=weights.quality,
            value_weight=weights.value,
            qvml_quality_weight=qvml_weights.quality,
            qvml_value_weight=qvml_weights.value,
            qvml_momentum_weight=qvml_weights.momentum,
            qvml_low_volatility_weight=qvml_weights.low_volatility,
            regime_pit_ready=regime_pit_ready,
        )
        # Composite QV is published only when both sub-factors clear their
        # coverage gates. Never normalize a one-sided score into a false QV.
        if qscore is not None and vscore is not None:
            row.qv_score = weights.quality * qscore + weights.value * vscore
            row.grade = _grade(row.qv_score)
        if (
            row.qv_score is not None
            and momentum_score is not None
            and low_volatility_score is not None
        ):
            row.qvml_score = (
                qvml_weights.quality * qscore
                + qvml_weights.value * vscore
                + qvml_weights.momentum * momentum_score
                + qvml_weights.low_volatility * low_volatility_score
            )
            row.qvml_grade = _grade(row.qvml_score)
        if row.quality_components_available < MIN_QUALITY_COMPONENTS:
            row.missing.append(f"minimum_quality_components:{MIN_QUALITY_COMPONENTS}")
        if qs and qs.missing:
            row.missing.extend([f"q:{m}" for m in qs.missing])
        if vs and vs.missing:
            row.missing.extend([f"v:{m}" for m in vs.missing])
        if include_market_factors and ms and ms.missing:
            row.missing.extend([f"market:{m}" for m in ms.missing])
        if include_market_factors and ms is None:
            row.missing.append("market:factor_snapshot_unavailable")
        if not regime_pit_ready:
            row.missing.append("macro_regime_pit_unavailable")
            row.missing.extend([f"macro:{m}" for m in snapshot.missing])
        rows.append(row)

    # 6. Assign ranks within universe
    def _assign_ranks(field_name: str, rank_field: str) -> None:
        valid = [r for r in rows if getattr(r, field_name) is not None]
        valid.sort(key=lambda r: getattr(r, field_name), reverse=True)
        for i, r in enumerate(valid, 1):
            setattr(r, rank_field, i)

    _assign_ranks("quality_score", "quality_rank")
    _assign_ranks("value_score", "value_rank")
    _assign_ranks("qv_score", "qv_rank")
    _assign_ranks("qvml_score", "qvml_rank")

    rows.sort(key=lambda r: r.qv_score if r.qv_score is not None else -1, reverse=True)
    log.info(
        "composite.computed",
        as_of=as_of,
        universe=len(rows),
        elapsed_seconds=round(perf_counter() - factor_started, 3),
        fundamental_snapshots=factor_cache.fundamental_snapshot_count,
        market_factors=include_market_factors,
        macro_regime=macro_regime,
        quality_weight=weights.quality,
        value_weight=weights.value,
        regime_pit_ready=regime_pit_ready,
    )
    return rows

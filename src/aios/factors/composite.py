"""Composite factor engine — blends Quality + Value into the QV ranking.

STRATEGY ALIGNMENT (from the strategy doc, Module 4):
  Quality: highest-weight factor by default (25-35%)
  Value:   15-25%

For this MVP we use fixed weights (Quality 60% / Value 40%) reflecting the
strategy's emphasis that quality is the strongest predictor. Weights will
become regime-adjusted in the next phase (macro overlay).

Both sub-factors output a 0-100 score. The composite is a weighted blend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from structlog import get_logger

from aios.factors import common as fc
from aios.factors.quality import compute_quality
from aios.factors.value import compute_value_ranked
from aios.storage.store import Store, get_store

log = get_logger(__name__)

QUALITY_WEIGHT = 0.60
VALUE_WEIGHT = 0.40


@dataclass
class CompositeRow:
    ticker: str
    as_of: str
    # Quality (0-100)
    quality_score: float | None = None
    # Value (0-100)
    value_score: float | None = None
    # Composite QV (0-100)
    qv_score: float | None = None
    # Letter grade
    grade: str = "N/A"
    # Sub-metrics for display
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
    # Rank within universe
    quality_rank: int | None = None
    value_rank: int | None = None
    qv_rank: int | None = None
    missing: list[str] = field(default_factory=list)


def _grade(score: float | None) -> str:
    if score is None:
        return "N/A"
    if score >= 85: return "A+"
    if score >= 70: return "A"
    if score >= 55: return "B"
    if score >= 40: return "C"
    return "D"


def compute_composite(
    tickers: list[str],
    as_of: str | date,
    store: Store | None = None,
) -> list[CompositeRow]:
    """Full QV composite ranking for a universe as-of a date.

    Returns a list sorted by qv_score descending (best opportunities first).
    """
    store = store or get_store()
    as_of = str(as_of)

    # 1. Value (universe-relative percentile ranks)
    value_snaps = compute_value_ranked(tickers, as_of, store)

    # 2. Quality per ticker + universe-relative rank
    q_snaps = {}
    for t in tickers:
        try:
            q_snaps[t.upper()] = compute_quality(t, as_of, store)
        except Exception as e:
            log.error("composite.quality_failed", ticker=t, error=str(e))

    # 3. Quality composite score (0-100) — blend of ROIC, FCF margin, gross margin,
    #    Piotroski. We rank each within the universe then average the percentiles.
    def _quality_components(s):
        return {
            "roic": s.roic,
            "fcf_margin": s.fcf_margin,
            "gross_margin": s.gross_margin,
            "piotroski": float(s.piotroski_f) if s.piotroski_f is not None else None,
        }

    comp_names = ["roic", "fcf_margin", "gross_margin", "piotroski"]
    # Build peer lists for percentile ranking
    peer_lists = {c: [] for c in comp_names}
    for s in q_snaps.values():
        comps = _quality_components(s)
        for c in comp_names:
            v = comps[c]
            if v is not None:
                peer_lists[c].append(v)

    quality_scores: dict[str, float] = {}
    for t, s in q_snaps.items():
        comps = _quality_components(s)
        pcts = []
        for c in comp_names:
            v = comps[c]
            if v is None or not peer_lists[c]:
                continue
            pcts.append(fc.percentile_rank(v, peer_lists[c]) or 0.0)
        quality_scores[t] = (sum(pcts) / len(pcts) * 100) if pcts else 0.0

    # 4. Assemble rows
    rows: list[CompositeRow] = []
    for t in tickers:
        t = t.upper()
        qs = q_snaps.get(t)
        vs = value_snaps.get(t)
        qscore = quality_scores.get(t)
        vscore = vs.value_score if vs else None

        row = CompositeRow(
            ticker=t,
            as_of=as_of,
            quality_score=qscore,
            value_score=vscore,
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
        )
        # Composite QV
        parts = []
        if qscore is not None:
            parts.append((QUALITY_WEIGHT, qscore))
        if vscore is not None:
            parts.append((VALUE_WEIGHT, vscore))
        if parts:
            # Normalize weights to what's available
            wsum = sum(w for w, _ in parts)
            row.qv_score = sum(w * v for w, v in parts) / wsum
            row.grade = _grade(row.qv_score)
        if qs and qs.missing:
            row.missing.extend([f"q:{m}" for m in qs.missing])
        if vs and vs.missing:
            row.missing.extend([f"v:{m}" for m in vs.missing])
        rows.append(row)

    # 5. Assign ranks within universe
    def _assign_ranks(field_name: str, rank_field: str) -> None:
        valid = [r for r in rows if getattr(r, field_name) is not None]
        valid.sort(key=lambda r: getattr(r, field_name), reverse=True)
        for i, r in enumerate(valid, 1):
            setattr(r, rank_field, i)

    _assign_ranks("quality_score", "quality_rank")
    _assign_ranks("value_score", "value_rank")
    _assign_ranks("qv_score", "qv_rank")

    rows.sort(key=lambda r: (r.qv_score if r.qv_score is not None else -1), reverse=True)
    log.info("composite.computed", as_of=as_of, universe=len(rows))
    return rows

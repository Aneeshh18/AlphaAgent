"""Deterministic, point-in-time macro regime classification.

This module intentionally has no backtest or factor-weight side effects. It
converts release-aware macro observations into a small, auditable snapshot
that later layers can consume. Every read goes through Store's PIT helpers,
which select the latest vintage known on the requested decision date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from aios.storage.store import Store, get_store

# Keep the first version deliberately small and interpretable. Additional
# indicators can be added after this core contract is covered by backtests.
GROWTH_SERIES = "A191RL1Q225SBEA"
INFLATION_SERIES = "CPIAUCSL"
CURVE_SERIES = "T10Y2Y"
VIX_SERIES = "VIXCLS"
CREDIT_SERIES = "BAA10Y"


@dataclass(frozen=True)
class MacroRegimeSnapshot:
    """The regime and its PIT evidence for one decision date."""

    as_of: str
    regime: str
    growth_state: str
    inflation_state: str
    curve_state: str
    stress_state: str
    metrics: dict[str, float | None] = field(default_factory=dict)
    release_dates: dict[str, str | None] = field(default_factory=dict)
    sources: dict[str, str | None] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    @property
    def is_pit_ready(self) -> bool:
        """Whether mandatory inputs exist and every evidence date is in-range."""
        if self.missing:
            return False
        return all(
            release_date <= self.as_of
            for release_date in self.release_dates.values()
            if release_date is not None
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation for CLI/report consumers."""
        return {
            "as_of": self.as_of,
            "regime": self.regime,
            "growth_state": self.growth_state,
            "inflation_state": self.inflation_state,
            "curve_state": self.curve_state,
            "stress_state": self.stress_state,
            "metrics": dict(self.metrics),
            "release_dates": dict(self.release_dates),
            "sources": dict(self.sources),
            "missing": list(self.missing),
            "is_pit_ready": self.is_pit_ready,
        }


def compute_regime(as_of: date | str, store: Store | None = None) -> MacroRegimeSnapshot:
    """Classify the macro regime using only information known by `as_of`.

    Mandatory inputs are real GDP growth, CPI year-over-year inflation, and at
    least one stress signal (VIX or the Baa/10Y credit spread). The yield-curve
    state is useful context but does not prevent a regime from being emitted.
    If a mandatory input is absent, the result is ``unknown`` and lists the
    exact missing evidence rather than guessing.
    """
    decision_date = _parse_date(as_of)
    as_of_text = decision_date.isoformat()
    db = store or get_store()

    histories = {
        series_id: db.pit_macro_history(series_id, decision_date)
        for series_id in (
            GROWTH_SERIES,
            INFLATION_SERIES,
            CURVE_SERIES,
            VIX_SERIES,
            CREDIT_SERIES,
        )
    }

    growth = _latest(histories[GROWTH_SERIES])
    inflation, inflation_prior = _latest_yoy_pair(histories[INFLATION_SERIES])
    curve = _latest(histories[CURVE_SERIES])
    vix = _latest(histories[VIX_SERIES])
    credit = _latest(histories[CREDIT_SERIES])

    metrics: dict[str, float | None] = {
        "growth_pct": _value(growth),
        "inflation_yoy_pct": inflation["yoy"] if inflation else None,
        "curve_spread_pct": _value(curve),
        "vix": _value(vix),
        "credit_spread_pct": _value(credit),
    }
    release_dates: dict[str, str | None] = {
        "growth": _release_date(growth),
        "inflation_latest": _release_date(inflation["latest"]) if inflation else None,
        "inflation_prior": _release_date(inflation_prior),
        "curve": _release_date(curve),
        "vix": _release_date(vix),
        "credit": _release_date(credit),
    }
    sources = {
        "growth": _source(growth),
        "inflation": _source(inflation["latest"]) if inflation else None,
        "curve": _source(curve),
        "vix": _source(vix),
        "credit": _source(credit),
    }

    missing: list[str] = []
    if growth is None:
        missing.append("growth")
    if inflation is None:
        missing.append("inflation_yoy_history")
    if vix is None and credit is None:
        missing.append("stress_signal")

    growth_state = _growth_state(metrics["growth_pct"])
    inflation_state = _inflation_state(metrics["inflation_yoy_pct"])
    curve_state = _curve_state(metrics["curve_spread_pct"])
    stress_state = _stress_state(metrics["vix"], metrics["credit_spread_pct"])
    regime = _regime_label(growth_state, inflation_state, stress_state, missing)

    snapshot = MacroRegimeSnapshot(
        as_of=as_of_text,
        regime=regime,
        growth_state=growth_state,
        inflation_state=inflation_state,
        curve_state=curve_state,
        stress_state=stress_state,
        metrics=metrics,
        release_dates=release_dates,
        sources=sources,
        missing=missing,
    )
    return snapshot


def _parse_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _latest(rows: list[dict]) -> dict | None:
    return rows[-1] if rows else None


def _latest_yoy_pair(rows: list[dict]) -> tuple[dict | None, dict | None]:
    """Return latest CPI YoY and the prior observation used to derive it."""
    latest = _latest(rows)
    if latest is None or latest.get("value") in (None, 0):
        return None, None
    target = latest["date"] - timedelta(days=330)
    prior_candidates = [row for row in rows if row["date"] <= target]
    prior = prior_candidates[-1] if prior_candidates else None
    if prior is None or prior.get("value") in (None, 0):
        return None, prior
    return {
        "yoy": (float(latest["value"]) / float(prior["value"]) - 1.0) * 100.0,
        "latest": latest,
    }, prior


def _value(row: dict | None) -> float | None:
    return float(row["value"]) if row and row.get("value") is not None else None


def _release_date(row: dict | None) -> str | None:
    if not row or row.get("release_date") is None:
        return None
    return str(row["release_date"])


def _source(row: dict | None) -> str | None:
    return str(row["source"]) if row and row.get("source") is not None else None


def _growth_state(value: float | None) -> str:
    if value is None:
        return "unknown"
    return "expansion" if value > 0 else "contraction"


def _inflation_state(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 2.0:
        return "low"
    if value < 3.0:
        return "moderate"
    return "high"


def _curve_state(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value <= -0.25:
        return "inverted"
    if value < 0.25:
        return "flat"
    return "steep"


def _stress_state(vix: float | None, credit: float | None) -> str:
    if vix is None and credit is None:
        return "unknown"
    if (vix is not None and vix >= 30.0) or (credit is not None and credit >= 3.0):
        return "high"
    if (vix is not None and vix >= 20.0) or (credit is not None and credit >= 2.0):
        return "elevated"
    return "calm"


def _regime_label(
    growth_state: str,
    inflation_state: str,
    stress_state: str,
    missing: list[str],
) -> str:
    if missing:
        return "unknown"
    if stress_state == "high":
        return "risk_off"
    if growth_state == "contraction" and inflation_state in {"moderate", "high"}:
        return "stagflation"
    if growth_state == "contraction" and inflation_state == "low":
        return "deflationary"
    if growth_state == "expansion" and inflation_state == "high":
        return "reflation"
    return "goldilocks"

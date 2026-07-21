"""Publication rules for factor scores.

Missing data is information. A score built from one surviving metric should
not look as authoritative as a score supported by the full factor set, so the
engine publishes coverage explicitly and gates the composite.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isclose, isfinite

MIN_QUALITY_COMPONENTS = 2
MIN_VALUE_MULTIPLES = 2
QVML_QV_CORE_WEIGHT = 0.60
QVML_MOMENTUM_WEIGHT = 0.25
QVML_LOW_VOLATILITY_WEIGHT = 0.15


@dataclass(frozen=True)
class FactorWeights:
    """Normalized Quality/Value weights used by the composite factor.

    These are an explicit starting policy, not a claim of backtest-validated
    alpha. Keeping the values in one typed object prevents accidental blends
    that do not sum to one or contain negative weights.
    """

    quality: float
    value: float

    def __post_init__(self) -> None:
        if not isfinite(self.quality) or not isfinite(self.value):
            raise ValueError("factor weights must be finite")
        if self.quality < 0 or self.value < 0:
            raise ValueError("factor weights cannot be negative")
        if not isclose(self.quality + self.value, 1.0, abs_tol=1e-9):
            raise ValueError("quality and value weights must sum to 1.0")


@dataclass(frozen=True)
class QVMLFactorWeights:
    """Experimental four-factor weights; the certified backtest remains QV."""

    quality: float
    value: float
    momentum: float
    low_volatility: float

    def __post_init__(self) -> None:
        values = (self.quality, self.value, self.momentum, self.low_volatility)
        if any(not isfinite(value) for value in values):
            raise ValueError("QVML weights must be finite")
        if any(value < 0 for value in values):
            raise ValueError("QVML weights cannot be negative")
        if not isclose(sum(values), 1.0, abs_tol=1e-9):
            raise ValueError("QVML weights must sum to 1.0")


# Baseline is retained for unknown/incomplete macro snapshots. The regime
# tilts are intentionally modest and interpretable until a PIT backtest can
# calibrate or reject them.
BASELINE_FACTOR_WEIGHTS = FactorWeights(quality=0.60, value=0.40)
REGIME_FACTOR_WEIGHTS: dict[str, FactorWeights] = {
    "goldilocks": BASELINE_FACTOR_WEIGHTS,
    "reflation": FactorWeights(quality=0.45, value=0.55),
    "stagflation": FactorWeights(quality=0.65, value=0.35),
    "deflationary": FactorWeights(quality=0.55, value=0.45),
    "risk_off": FactorWeights(quality=0.70, value=0.30),
    "unknown": BASELINE_FACTOR_WEIGHTS,
}


def weights_for_regime(regime: str) -> FactorWeights:
    """Return the configured weights, using baseline for an unknown label."""
    return REGIME_FACTOR_WEIGHTS.get(str(regime).lower(), BASELINE_FACTOR_WEIGHTS)


def qvml_weights_for_regime(regime: str) -> QVMLFactorWeights:
    """Add fixed M/L sleeves while preserving the regime's Q/V relative tilt."""
    qv = weights_for_regime(regime)
    return QVMLFactorWeights(
        quality=qv.quality * QVML_QV_CORE_WEIGHT,
        value=qv.value * QVML_QV_CORE_WEIGHT,
        momentum=QVML_MOMENTUM_WEIGHT,
        low_volatility=QVML_LOW_VOLATILITY_WEIGHT,
    )

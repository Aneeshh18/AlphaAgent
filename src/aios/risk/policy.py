"""Fail-closed, jurisdiction-neutral portfolio risk checks.

This module approves or rejects a proposed paper portfolio; it does not select
stocks. Final limits remain user-owned, but missing sector/liquidity evidence,
leverage, concentration, excessive turnover, and drawdown are never silently
accepted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from math import isfinite
from typing import Any, Literal

RiskStatus = Literal["pass", "fail"]
_EPSILON = 1e-12


@dataclass(frozen=True)
class PortfolioRiskPolicy:
    """Conservative defaults for a diversified, long-only paper portfolio."""

    minimum_positions: int = 10
    maximum_positions: int = 20
    maximum_position_weight: float = 0.10
    maximum_sector_weight: float = 0.25
    maximum_gross_exposure: float = 1.0
    maximum_rebalance_turnover: float = 1.0
    maximum_drawdown: float = 0.15
    maximum_order_adv_fraction: float = 0.05
    require_sector_data: bool = True
    require_liquidity_data: bool = True

    def __post_init__(self) -> None:
        if self.minimum_positions < 1:
            raise ValueError("minimum_positions must be positive")
        if self.maximum_positions < self.minimum_positions:
            raise ValueError("maximum_positions cannot be below the minimum")
        for name in (
            "maximum_position_weight",
            "maximum_sector_weight",
            "maximum_gross_exposure",
            "maximum_rebalance_turnover",
            "maximum_drawdown",
            "maximum_order_adv_fraction",
        ):
            value = getattr(self, name)
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.maximum_position_weight > self.maximum_gross_exposure:
            raise ValueError("position limit cannot exceed gross-exposure limit")
        if self.maximum_sector_weight > self.maximum_gross_exposure:
            raise ValueError("sector limit cannot exceed gross-exposure limit")
        if self.maximum_drawdown >= 1:
            raise ValueError("maximum_drawdown must be below 1")


@dataclass(frozen=True)
class TargetPosition:
    """One proposed target and the evidence needed by the risk gate."""

    ticker: str
    weight: float
    sector: str | None = None
    average_daily_dollar_volume: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", self.ticker.strip().upper())
        if self.sector is not None:
            object.__setattr__(self, "sector", self.sector.strip() or None)


@dataclass(frozen=True)
class RiskCheck:
    """One portfolio-level risk gate."""

    check: str
    label: str
    status: RiskStatus
    observed: str
    limit: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class PortfolioRiskAssessment:
    """Full deterministic result for a proposed target portfolio."""

    approved: bool
    gross_exposure: float
    cash_weight: float
    rebalance_turnover: float
    drawdown: float
    checks: tuple[RiskCheck, ...]

    @property
    def blockers(self) -> tuple[RiskCheck, ...]:
        return tuple(check for check in self.checks if check.status == "fail")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["checks"] = [check.to_dict() for check in self.checks]
        return result

    def raise_if_rejected(self) -> None:
        if self.approved:
            return
        reasons = ", ".join(check.check for check in self.blockers)
        raise ValueError(f"portfolio risk policy rejected the proposal: {reasons}")


def assess_portfolio_risk(
    targets: Sequence[TargetPosition],
    *,
    equity: float,
    peak_equity: float,
    current_weights: Mapping[str, float] | None = None,
    policy: PortfolioRiskPolicy | None = None,
) -> PortfolioRiskAssessment:
    """Evaluate a target portfolio without modifying portfolio state."""
    rules = policy or PortfolioRiskPolicy()
    checks: list[RiskCheck] = []
    normalized_current = {
        str(ticker).strip().upper(): weight
        for ticker, weight in (current_weights or {}).items()
    }

    tickers = [target.ticker for target in targets]
    duplicate_tickers = sorted({ticker for ticker in tickers if tickers.count(ticker) > 1})
    invalid_targets = [
        target.ticker or "<blank>"
        for target in targets
        if not target.ticker or not isfinite(target.weight)
    ]
    invalid_current = [
        ticker
        for ticker, weight in normalized_current.items()
        if not ticker or not isfinite(weight) or weight < 0
    ]
    inputs_ok = not duplicate_tickers and not invalid_targets and not invalid_current
    checks.append(
        RiskCheck(
            "input_integrity",
            "Proposal inputs",
            "pass" if inputs_ok else "fail",
            (
                "unique, finite targets"
                if inputs_ok
                else f"duplicates={duplicate_tickers}; invalid={invalid_targets + invalid_current}"
            ),
            "unique tickers and finite non-negative current weights",
            "Invalid inputs are rejected before any exposure calculation.",
        )
    )

    target_weights: dict[str, float] = {}
    for target in targets:
        if target.ticker and isfinite(target.weight):
            target_weights[target.ticker] = target_weights.get(target.ticker, 0.0) + target.weight
    negative = sorted(ticker for ticker, weight in target_weights.items() if weight < -_EPSILON)
    checks.append(
        RiskCheck(
            "long_only",
            "Long-only policy",
            "pass" if not negative else "fail",
            "no short targets" if not negative else ", ".join(negative),
            "all target weights at or above zero",
            "Short positions and implicit leverage are outside the current paper scope.",
        )
    )

    positive_targets = {
        ticker: weight for ticker, weight in target_weights.items() if weight > _EPSILON
    }
    position_count = len(positive_targets)
    count_ok = rules.minimum_positions <= position_count <= rules.maximum_positions
    checks.append(
        RiskCheck(
            "position_count",
            "Diversification by position count",
            "pass" if count_ok else "fail",
            str(position_count),
            f"{rules.minimum_positions}–{rules.maximum_positions}",
            "Too few or too many holdings are rejected rather than silently accepted.",
        )
    )

    gross_exposure = sum(abs(weight) for weight in target_weights.values() if isfinite(weight))
    cash_weight = max(0.0, 1.0 - sum(positive_targets.values()))
    gross_ok = gross_exposure <= rules.maximum_gross_exposure + _EPSILON
    checks.append(
        RiskCheck(
            "gross_exposure",
            "Total invested exposure",
            "pass" if gross_ok else "fail",
            f"{gross_exposure:.2%}",
            f"at most {rules.maximum_gross_exposure:.2%}",
            "The current policy does not permit leverage.",
        )
    )

    largest_ticker, largest_weight = max(
        positive_targets.items(), key=lambda item: item[1], default=("none", 0.0)
    )
    position_ok = largest_weight <= rules.maximum_position_weight + _EPSILON
    checks.append(
        RiskCheck(
            "position_concentration",
            "Largest single position",
            "pass" if position_ok else "fail",
            f"{largest_ticker} at {largest_weight:.2%}",
            f"at most {rules.maximum_position_weight:.2%}",
            "A high score never overrides the single-position limit.",
        )
    )

    missing_sectors = sorted(
        target.ticker for target in targets if target.weight > _EPSILON and not target.sector
    )
    sector_weights: dict[str, float] = {}
    for target in targets:
        if target.weight > _EPSILON and target.sector:
            sector_weights[target.sector] = sector_weights.get(target.sector, 0.0) + target.weight
    largest_sector, largest_sector_weight = max(
        sector_weights.items(), key=lambda item: item[1], default=("none", 0.0)
    )
    sector_ok = (
        (not rules.require_sector_data or not missing_sectors)
        and largest_sector_weight <= rules.maximum_sector_weight + _EPSILON
    )
    checks.append(
        RiskCheck(
            "sector_concentration",
            "Largest sector exposure",
            "pass" if sector_ok else "fail",
            (
                f"{largest_sector} at {largest_sector_weight:.2%}"
                if not missing_sectors
                else "missing: " + ", ".join(missing_sectors)
            ),
            f"complete sectors and at most {rules.maximum_sector_weight:.2%}",
            "Missing classification is not treated as diversified exposure.",
        )
    )

    turnover = _turnover(target_weights, normalized_current)
    turnover_ok = turnover <= rules.maximum_rebalance_turnover + _EPSILON
    checks.append(
        RiskCheck(
            "rebalance_turnover",
            "One-way rebalance turnover",
            "pass" if turnover_ok else "fail",
            f"{turnover:.2%}",
            f"at most {rules.maximum_rebalance_turnover:.2%}",
            "Turnover includes the change in cash and prevents hidden churn.",
        )
    )

    liquidity_failures: list[str] = []
    if not isfinite(equity) or equity <= 0:
        liquidity_failures.append("invalid_equity")
    else:
        target_by_ticker = {target.ticker: target for target in targets}
        for ticker in sorted(set(target_weights) | set(normalized_current)):
            delta = abs(target_weights.get(ticker, 0.0) - normalized_current.get(ticker, 0.0))
            if delta <= _EPSILON:
                continue
            evidence = target_by_ticker.get(ticker)
            adv = evidence.average_daily_dollar_volume if evidence is not None else None
            if adv is None or not isfinite(adv) or adv <= 0:
                if rules.require_liquidity_data:
                    liquidity_failures.append(f"{ticker}:missing_adv")
                continue
            if delta * equity / adv > rules.maximum_order_adv_fraction + _EPSILON:
                liquidity_failures.append(f"{ticker}:order_too_large")
    checks.append(
        RiskCheck(
            "liquidity",
            "Order size versus trading liquidity",
            "pass" if not liquidity_failures else "fail",
            "within limit" if not liquidity_failures else ", ".join(liquidity_failures),
            f"each order at most {rules.maximum_order_adv_fraction:.2%} of average daily value",
            "Liquidity evidence is required for every changed holding.",
        )
    )

    capital_ok = (
        isfinite(equity)
        and equity > 0
        and isfinite(peak_equity)
        and peak_equity > 0
        and equity <= peak_equity + _EPSILON
    )
    drawdown = equity / peak_equity - 1.0 if capital_ok else -1.0
    drawdown_ok = capital_ok and drawdown >= -rules.maximum_drawdown - _EPSILON
    checks.append(
        RiskCheck(
            "drawdown_halt",
            "Portfolio drawdown stop",
            "pass" if drawdown_ok else "fail",
            f"{drawdown:.2%}" if capital_ok else "invalid equity/peak evidence",
            f"not below {-rules.maximum_drawdown:.2%}",
            "A breached drawdown stops new risk until an explicit review.",
        )
    )

    approved = all(check.status == "pass" for check in checks)
    return PortfolioRiskAssessment(
        approved=approved,
        gross_exposure=gross_exposure,
        cash_weight=cash_weight,
        rebalance_turnover=turnover,
        drawdown=drawdown,
        checks=tuple(checks),
    )


def _turnover(target: Mapping[str, float], current: Mapping[str, float]) -> float:
    stock_l1 = sum(
        abs(target.get(ticker, 0.0) - current.get(ticker, 0.0))
        for ticker in set(target) | set(current)
    )
    target_cash = 1.0 - sum(target.values())
    current_cash = 1.0 - sum(current.values())
    return 0.5 * (stock_l1 + abs(target_cash - current_cash))

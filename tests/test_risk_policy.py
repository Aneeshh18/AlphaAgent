from __future__ import annotations

import pytest

from aios.risk import (
    PortfolioRiskPolicy,
    TargetPosition,
    assess_portfolio_risk,
)


def _diversified_targets() -> list[TargetPosition]:
    sectors = ("Technology", "Health", "Financials", "Industrials", "Consumer")
    return [
        TargetPosition(
            ticker=f"T{index}",
            weight=0.10,
            sector=sectors[index // 2],
            average_daily_dollar_volume=100_000_000.0,
        )
        for index in range(10)
    ]


def test_diversified_liquid_long_only_paper_target_passes() -> None:
    assessment = assess_portfolio_risk(
        _diversified_targets(),
        equity=100_000.0,
        peak_equity=100_000.0,
    )

    assert assessment.approved is True
    assert assessment.gross_exposure == pytest.approx(1.0)
    assert assessment.cash_weight == pytest.approx(0.0)
    assert assessment.rebalance_turnover == pytest.approx(1.0)
    assert assessment.blockers == ()


def test_risk_gate_fails_closed_on_concentration_missing_evidence_and_drawdown() -> None:
    targets = _diversified_targets()
    targets[0] = TargetPosition(
        ticker="T0",
        weight=0.30,
        sector=None,
        average_daily_dollar_volume=None,
    )

    assessment = assess_portfolio_risk(
        targets,
        equity=80_000.0,
        peak_equity=100_000.0,
    )

    blockers = {check.check for check in assessment.blockers}
    assert assessment.approved is False
    assert blockers >= {
        "gross_exposure",
        "position_concentration",
        "sector_concentration",
        "liquidity",
        "drawdown_halt",
    }
    with pytest.raises(ValueError, match="risk policy rejected"):
        assessment.raise_if_rejected()


def test_turnover_includes_cash_and_rejects_excessive_churn() -> None:
    policy = PortfolioRiskPolicy(maximum_rebalance_turnover=0.25)

    assessment = assess_portfolio_risk(
        _diversified_targets(),
        equity=100_000.0,
        peak_equity=100_000.0,
        current_weights={f"OLD{index}": 0.10 for index in range(10)},
        policy=policy,
    )

    assert assessment.rebalance_turnover == pytest.approx(1.0)
    assert "rebalance_turnover" in {check.check for check in assessment.blockers}


def test_risk_policy_rejects_impossible_limits() -> None:
    with pytest.raises(ValueError, match="maximum_positions"):
        PortfolioRiskPolicy(minimum_positions=10, maximum_positions=5)
    with pytest.raises(ValueError, match="maximum_drawdown"):
        PortfolioRiskPolicy(maximum_drawdown=1.0)

"""Tests for risk-adjusted metrics and the benchmark-significance verdict.

These exist because this repository produced two backtest windows in which
the ranking of two strategies against the same benchmark reversed
completely, and nothing in the artifact said so. Cumulative return alone
invited exactly the misreading that followed: a strategy was promoted to a
live forward trial on the window where it won, while the window where it
lost by 14.5 points sat in the same directory.

The verdict text is asserted here, not just the arithmetic. Its whole
purpose is to be read by a human deciding whether to act, so weakening it
should break a test.
"""

from __future__ import annotations

from aios.backtest.engine import (
    MINIMUM_PERIODS_FOR_ANY_INFERENCE,
    _sharpe_and_t_stat,
    _significance_verdict,
)


def test_sharpe_and_t_stat_are_none_without_dispersion() -> None:
    """Zero variance is undefined, not zero. Reporting 0.0 would read as measured."""
    assert _sharpe_and_t_stat([0.01, 0.01, 0.01], 252.0) == (None, None)


def test_sharpe_and_t_stat_are_none_below_two_observations() -> None:
    assert _sharpe_and_t_stat([], 252.0) == (None, None)
    assert _sharpe_and_t_stat([0.01], 252.0) == (None, None)


def test_sharpe_scales_with_the_period_frequency() -> None:
    """A quarterly series must not be annualized as if it were daily."""
    returns = [0.02, -0.01, 0.03, 0.01, -0.02, 0.04]
    daily, _ = _sharpe_and_t_stat(returns, 252.0)
    quarterly, _ = _sharpe_and_t_stat(returns, 4.0)
    assert daily is not None and quarterly is not None
    assert daily > quarterly


def test_positive_mean_return_gives_a_positive_t_stat() -> None:
    _sharpe, t_stat = _sharpe_and_t_stat([0.01, 0.02, 0.015, 0.005], 252.0)
    assert t_stat is not None and t_stat > 0


def test_verdict_refuses_to_interpret_a_short_window() -> None:
    """The real failure mode: a large return gap over very few rebalances.

    A high t-statistic must not rescue a sample this short — period count is
    checked first and on its own.
    """
    verdict = _significance_verdict(t_stat=5.0, completed_periods=6, paired_observations=313)
    assert "not interpretable" in verdict
    assert "6 completed rebalance(s)" in verdict


def test_verdict_calls_a_weak_result_noise_even_with_enough_periods() -> None:
    verdict = _significance_verdict(
        t_stat=0.62,
        completed_periods=MINIMUM_PERIODS_FOR_ANY_INFERENCE + 4,
        paired_observations=1200,
    )
    assert "indistinguishable from noise" in verdict
    assert "must not be reported as an edge" in verdict


def test_verdict_never_claims_a_validated_edge() -> None:
    """Even the strongest branch must stay hedged.

    Daily returns are autocorrelated, so an independent-observation t-stat
    overstates significance. No branch of this function may imply the result
    is confirmed.
    """
    verdict = _significance_verdict(
        t_stat=4.5,
        completed_periods=MINIMUM_PERIODS_FOR_ANY_INFERENCE + 10,
        paired_observations=2000,
    )
    assert "unconfirmed" in verdict
    assert "never as a validated edge" in verdict
    for text in (
        _significance_verdict(0.5, 40, 1000),
        _significance_verdict(4.5, 40, 1000),
        _significance_verdict(None, 40, 0),
    ):
        assert "validated edge" not in text or "never as a validated edge" in text


def test_verdict_handles_absent_data() -> None:
    verdict = _significance_verdict(t_stat=None, completed_periods=6, paired_observations=0)
    assert "insufficient data" in verdict


def test_real_world_short_window_gap_is_not_reported_as_an_edge() -> None:
    """Reproduces the actual numbers that misled this project.

    QVML returned +49.4% against SPY's +35.0% over six rebalances — a
    +14.4 point gap that looked decisive and was not: the paired daily
    excess return carries t=0.49.
    """
    verdict = _significance_verdict(
        t_stat=0.49,
        completed_periods=6,
        paired_observations=313,
    )
    assert "not interpretable" in verdict

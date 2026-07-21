from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import pytest

from aios.factors.market_factors import (
    REQUIRED_PRICE_OBSERVATIONS,
    _daily_total_returns,
    compute_market_factors_ranked,
    compute_market_factors_raw,
)
from aios.market_calendar import us_equity_sessions
from aios.storage.store import Store


def _price_rows(
    ticker: str,
    gross_returns: Iterable[float],
    *,
    actions_complete: bool = True,
) -> list[dict]:
    factors = list(gross_returns)
    dates = us_equity_sessions(date(2023, 1, 1), date(2025, 1, 1))[
        -(len(factors) + 1) :
    ]
    close = 100.0
    rows = [
        {
            "ticker": ticker,
            "date": dates[0],
            "close": close,
            "dividends": 0.0,
            "split_ratio": 1.0,
            "actions_complete": actions_complete,
            "source": "test",
        }
    ]
    for observation_date, gross_return in zip(dates[1:], factors, strict=True):
        close *= gross_return
        rows.append(
            {
                "ticker": ticker,
                "date": observation_date,
                "close": close,
                "dividends": 0.0,
                "split_ratio": 1.0,
                "actions_complete": actions_complete,
                "source": "test",
            }
        )
    return rows


def test_daily_total_return_applies_split_before_dividend_cash() -> None:
    returns, missing = _daily_total_returns(
        [
            {"close": 100.0},
            {"close": 50.0, "split_ratio": 2.0, "dividends": 1.0},
        ]
    )

    assert missing == []
    assert returns == pytest.approx([0.02])


def test_daily_total_return_does_not_reapply_split_to_normalized_close() -> None:
    returns, missing = _daily_total_returns(
        [
            {"close": 100.0},
            {
                "close": 101.0,
                "split_ratio": 10.0,
                "dividends": 0.0,
                "close_split_adjusted": True,
            },
        ]
    )

    assert missing == []
    assert returns == pytest.approx([0.01])


def test_market_factors_use_exact_12_minus_1_and_252_session_windows(tmp_path) -> None:
    store = Store(tmp_path / "market-factors.duckdb")
    try:
        daily_growth = 1.001
        store.upsert_prices(
            _price_rows("STEADY", [daily_growth] * (REQUIRED_PRICE_OBSERVATIONS - 1))
        )

        snapshot = compute_market_factors_raw("STEADY", "2024-12-31", store)

        assert snapshot.price_observations == REQUIRED_PRICE_OBSERVATIONS
        assert snapshot.momentum_12_1 == pytest.approx(daily_growth**231 - 1.0)
        assert snapshot.annualized_volatility == pytest.approx(0.0, abs=1e-12)
        assert snapshot.missing == []
    finally:
        store.close()


def test_low_volatility_score_rewards_the_less_volatile_security(tmp_path) -> None:
    store = Store(tmp_path / "low-vol-rank.duckdb")
    try:
        observations = REQUIRED_PRICE_OBSERVATIONS - 1
        store.upsert_prices(_price_rows("STEADY", [1.001] * observations))
        store.upsert_prices(
            _price_rows(
                "CHOPPY",
                [1.03 if index % 2 == 0 else 0.97 for index in range(observations)],
            )
        )

        snapshots = compute_market_factors_ranked(["STEADY", "CHOPPY"], "2024-12-31", store)

        assert snapshots["STEADY"].annualized_volatility < snapshots["CHOPPY"].annualized_volatility
        assert snapshots["STEADY"].low_volatility_score > snapshots["CHOPPY"].low_volatility_score
    finally:
        store.close()


def test_market_factors_fail_closed_without_verified_action_fields(tmp_path) -> None:
    store = Store(tmp_path / "unverified-actions.duckdb")
    try:
        store.upsert_prices(
            _price_rows(
                "LEGACY",
                [1.001] * (REQUIRED_PRICE_OBSERVATIONS - 1),
                actions_complete=False,
            )
        )

        snapshot = compute_market_factors_raw("LEGACY", "2024-12-31", store)

        assert snapshot.momentum_12_1 is None
        assert snapshot.annualized_volatility is None
        assert "corporate_actions_unverified" in snapshot.missing
    finally:
        store.close()


def test_market_factors_fail_closed_on_a_session_gap_with_enough_rows(tmp_path) -> None:
    store = Store(tmp_path / "session-gap.duckdb")
    try:
        rows = _price_rows("GAPPED", [1.001] * REQUIRED_PRICE_OBSERVATIONS)
        rows.pop(100)
        assert len(rows) == REQUIRED_PRICE_OBSERVATIONS
        store.upsert_prices(rows)

        snapshot = compute_market_factors_raw("GAPPED", "2024-12-31", store)

        assert snapshot.momentum_12_1 is None
        assert snapshot.annualized_volatility is None
        assert "noncontiguous_price_sessions" in snapshot.missing
    finally:
        store.close()

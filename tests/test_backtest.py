from __future__ import annotations

from datetime import date

import pytest

from aios.backtest import engine
from aios.factors.composite import CompositeRow
from aios.storage.store import Store


def _row(
    ticker: str,
    quality_score: float,
    value_score: float,
    *,
    regime: str = "reflation",
    pit_ready: bool = True,
) -> CompositeRow:
    return CompositeRow(
        ticker=ticker,
        as_of="2024-03-29",
        quality_score=quality_score,
        value_score=value_score,
        qv_score=quality_score * 0.45 + value_score * 0.55,
        macro_regime=regime,
        quality_weight=0.45 if pit_ready else 0.60,
        value_weight=0.55 if pit_ready else 0.40,
        regime_pit_ready=pit_ready,
    )


def _seed_prices(store: Store) -> None:
    rows = []
    values = {
        "2024-03-29": {"A": 100.0, "B": 100.0},
        "2024-04-01": {"A": 100.0, "B": 100.0},
        "2024-06-28": {"A": 110.0, "B": 90.0},
        "2024-07-01": {"A": 110.0, "B": 90.0},
        "2024-09-30": {"A": 121.0, "B": 81.0},
    }
    for observation_date, tickers in values.items():
        for ticker, price in tickers.items():
            rows.append(
                {
                    "ticker": ticker,
                    "date": observation_date,
                    "close": price,
                    "adj_close": price,
                    "source": "test",
                }
            )
    store.upsert_prices(rows)


def test_backtest_uses_next_session_and_compares_regime_policy(monkeypatch, tmp_path):
    store = Store(tmp_path / "backtest.duckdb")
    try:
        _seed_prices(store)
        calls: list[str] = []

        def fake_compute(tickers, as_of, store):
            calls.append(str(as_of))
            return [_row("A", 100, 0), _row("B", 0, 100)]

        monkeypatch.setattr(engine, "compute_composite", fake_compute)

        result = engine.run_qv_policy_backtest(
            "2024-01-01",
            "2024-09-30",
            tickers=["A", "B"],
            top_n=1,
            allow_current_universe=True,
            store=store,
        )

        assert calls == ["2024-03-29", "2024-06-28"]
        assert result.comparison_periods == 2
        assert all(period.status == "complete" for period in result.periods)
        first = result.periods[0]
        assert first.entry_date == "2024-04-01"
        assert first.exit_date == "2024-06-28"
        assert first.regime_selected == ("B",)
        assert first.baseline_selected == ("A",)
        assert first.regime_return == pytest.approx(-0.10)
        assert first.baseline_return == pytest.approx(0.10)
        assert result.regime_metrics.cumulative_return == pytest.approx(-0.19)
        assert result.baseline_metrics.cumulative_return == pytest.approx(0.21)
    finally:
        store.close()


def test_backtest_skips_non_pit_regime_by_default(monkeypatch, tmp_path):
    store = Store(tmp_path / "backtest-unknown.duckdb")
    try:
        _seed_prices(store)

        monkeypatch.setattr(
            engine,
            "compute_composite",
            lambda tickers, as_of, store: [
                _row("A", 100, 0, regime="unknown", pit_ready=False),
                _row("B", 0, 100, regime="unknown", pit_ready=False),
            ],
        )

        result = engine.run_qv_policy_backtest(
            date(2024, 1, 1),
            date(2024, 9, 30),
            tickers=["A", "B"],
            top_n=1,
            allow_current_universe=True,
            store=store,
        )

        assert result.comparison_periods == 0
        assert all(period.status == "skipped_regime_not_pit_ready" for period in result.periods)
        assert all("macro_regime_pit_unavailable" in period.missing for period in result.periods)
        assert result.regime_metrics.completed_periods == 0
    finally:
        store.close()


def test_backtest_config_rejects_unsupported_frequency():
    with pytest.raises(ValueError, match="quarterly"):
        engine.QVBacktestConfig("2024-01-01", "2024-09-30", rebalance_frequency="monthly")


def test_backtest_refuses_hard_data_quality_failure(monkeypatch, tmp_path):
    store = Store(tmp_path / "backtest-invalid.duckdb")
    try:
        monkeypatch.setattr(
            store,
            "data_quality_report",
            lambda: [{"check": "macro_unversioned_rows", "status": "fail"}],
        )

        with pytest.raises(ValueError, match="macro_unversioned_rows"):
            engine.run_qv_policy_backtest(
                "2024-01-01",
                "2024-09-30",
                tickers=["A"],
                top_n=1,
                store=store,
            )
    finally:
        store.close()


def test_backtest_uses_historical_membership_and_benchmark(monkeypatch, tmp_path):
    store = Store(tmp_path / "backtest-membership.duckdb")
    try:
        _seed_prices(store)
        store.upsert_universe_membership(
            [
                {
                    "universe_id": "demo",
                    "ticker": ticker,
                    "effective_start": "2024-01-01",
                    "effective_end": None,
                    "known_date": "2023-12-15",
                    "source": "test",
                }
                for ticker in ("A", "B")
            ]
        )
        store.upsert_security_identities(
            [
                {
                    "universe_id": "demo",
                    "ticker": ticker,
                    "effective_start": "2024-01-01",
                    "effective_end": None,
                    "security_id": f"aios:security:{ticker.lower()}",
                    "known_date": "2023-12-15",
                    "identity_status": "bounded_ticker",
                    "source": "test",
                }
                for ticker in ("A", "B")
            ]
        )
        monkeypatch.setattr(
            engine,
            "compute_composite",
            lambda tickers, as_of, store: [_row("A", 100, 0), _row("B", 0, 100)],
        )

        result = engine.run_qv_policy_backtest(
            "2024-01-01",
            "2024-09-30",
            universe_id="demo",
            top_n=1,
            benchmark_tickers=["A"],
            store=store,
        )

        assert result.tickers == ("A", "B")
        assert result.config.universe_id == "demo"
        assert result.benchmark_metrics["A"].completed_periods == 2
        assert result.regime_metrics.total_transaction_costs == 0
    finally:
        store.close()

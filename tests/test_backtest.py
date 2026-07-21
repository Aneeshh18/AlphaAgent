from __future__ import annotations

from datetime import date

import pytest

from aios.backtest import engine
from aios.factors.composite import CompositeRow
from aios.macro.regime import MacroRegimeSnapshot
from aios.storage.store import Store


def _row(
    ticker: str,
    quality_score: float,
    value_score: float,
    *,
    regime: str = "reflation",
    pit_ready: bool = True,
    momentum_score: float | None = None,
    low_volatility_score: float | None = None,
) -> CompositeRow:
    qvml_score = None
    if momentum_score is not None and low_volatility_score is not None:
        qvml_score = (
            quality_score * 0.27
            + value_score * 0.33
            + momentum_score * 0.25
            + low_volatility_score * 0.15
        )
    return CompositeRow(
        ticker=ticker,
        as_of="2024-03-29",
        quality_score=quality_score,
        value_score=value_score,
        qv_score=quality_score * 0.45 + value_score * 0.55,
        momentum_score=momentum_score,
        low_volatility_score=low_volatility_score,
        qvml_score=qvml_score,
        market_price_observations=(253 if momentum_score is not None else 0),
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


def _snapshot(as_of: str | date, *, ready: bool = True) -> MacroRegimeSnapshot:
    return MacroRegimeSnapshot(
        as_of=str(as_of),
        regime="reflation" if ready else "unknown",
        growth_state="expanding" if ready else "unknown",
        inflation_state="high" if ready else "unknown",
        curve_state="normal",
        stress_state="normal" if ready else "unknown",
        missing=[] if ready else ["growth"],
    )


def _use_pit_regime(monkeypatch) -> None:
    monkeypatch.setattr(engine, "compute_regime", lambda as_of, store: _snapshot(as_of))


def test_backtest_uses_next_session_and_compares_regime_policy(monkeypatch, tmp_path):
    store = Store(tmp_path / "backtest.duckdb")
    try:
        _seed_prices(store)
        _use_pit_regime(monkeypatch)
        calls: list[str] = []

        def fake_compute(tickers, as_of, store, regime_snapshot=None):
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
        assert [trade.side for trade in result.periods[0].regime_trades] == ["buy"]
        assert result.periods[1].regime_trades == ()
        assert result.regime_metrics.total_turnover == pytest.approx(1.0)
        assert result.regime_metrics.max_drawdown == pytest.approx(-0.19)
        assert result.baseline_metrics.max_drawdown == pytest.approx(0.0)
        assert result.regime_metrics.daily_observations == 5
        assert len(result.regime_equity_curve) == 5
        assert result.to_dict()["regime_equity_curve"][-1]["net_equity"] == pytest.approx(81_000.0)
    finally:
        store.close()


def test_backtest_skips_non_pit_regime_by_default(monkeypatch, tmp_path):
    store = Store(tmp_path / "backtest-unknown.duckdb")
    try:
        _seed_prices(store)

        monkeypatch.setattr(
            engine,
            "compute_regime",
            lambda as_of, store: _snapshot(as_of, ready=False),
        )
        monkeypatch.setattr(
            engine,
            "compute_composite",
            lambda tickers, as_of, store, regime_snapshot=None: [
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


def test_backtest_config_rejects_unknown_factor_model():
    with pytest.raises(ValueError, match="factor_model"):
        engine.QVBacktestConfig("2024-01-01", "2024-09-30", factor_model="mystery")


def test_qvml_backtest_requires_market_sleeves_and_preserves_baseline(monkeypatch, tmp_path):
    store = Store(tmp_path / "qvml-backtest.duckdb")
    try:
        store.upsert_prices(
            [
                {
                    "ticker": ticker,
                    "date": observation_date,
                    "close": price,
                    "source": "test",
                }
                for observation_date, prices in {
                    "2024-03-29": {"A": 100, "B": 100, "C": 100},
                    "2024-04-01": {"A": 100, "B": 100, "C": 100},
                    "2024-06-28": {"A": 110, "B": 90, "C": 105},
                }.items()
                for ticker, price in prices.items()
            ]
        )
        _use_pit_regime(monkeypatch)
        market_flags: list[bool] = []

        def fake_compute(
            tickers,
            as_of,
            store,
            regime_snapshot=None,
            *,
            include_market_factors=False,
        ):
            market_flags.append(include_market_factors)
            return [
                _row("A", 100, 0, momentum_score=0, low_volatility_score=0),
                _row("B", 0, 100, momentum_score=0, low_volatility_score=0),
                _row("C", 0, 0, momentum_score=80, low_volatility_score=80),
            ]

        monkeypatch.setattr(engine, "compute_composite", fake_compute)

        result = engine.run_qv_policy_backtest(
            "2024-01-01",
            "2024-06-28",
            tickers=["A", "B", "C"],
            top_n=1,
            factor_model="qvml",
            allow_current_universe=True,
            store=store,
        )

        assert market_flags == [True]
        assert result.config.factor_model == "qvml"
        assert result.periods[0].regime_selected == ("B",)
        assert result.periods[0].baseline_selected == ("A",)
        assert result.periods[0].momentum_weight == pytest.approx(0.25)
        assert result.periods[0].low_volatility_weight == pytest.approx(0.15)
        assert result.periods[0].qvml_scored_tickers == 3
        assert all(row.eligible for row in result.periods[0].factor_audit)
        assert all(row.factor_model == "qvml" for row in result.periods[0].regime_selection_audit)
    finally:
        store.close()


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
        _use_pit_regime(monkeypatch)
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
        store.execute(
            """
            UPDATE prices
            SET security_id = CASE ticker
                WHEN 'A' THEN 'aios:security:a'
                WHEN 'B' THEN 'aios:security:b'
            END
            WHERE ticker IN ('A', 'B')
            """
        )
        monkeypatch.setattr(store, "data_quality_report", lambda: [])
        monkeypatch.setattr(
            engine,
            "compute_composite",
            lambda tickers, as_of, store, regime_snapshot=None: [
                _row("A", 100, 0),
                _row("B", 0, 100),
            ],
        )
        monkeypatch.setattr(
            store,
            "universe_data_coverage",
            lambda universe_id, as_of, effective_on=None: [
                {
                    "ticker": ticker,
                    "security_id": f"aios:security:{ticker.lower()}",
                    "has_price_history": True,
                    "has_pit_fundamentals": True,
                    "latest_price_date": as_of,
                    "latest_fundamental_date": date(2024, 2, 1),
                }
                for ticker in ("A", "B")
            ],
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
        assert result.config.calendar_ticker == "A"
        assert result.benchmark_metrics["A"].completed_periods == 2
        assert result.benchmark_metrics["A"].daily_observations == 5
        assert result.benchmark_metrics["A"].cumulative_return == pytest.approx(0.21)
        assert len(result.benchmark_equity_curves["A"]) == 5
        assert all(
            period.price_basis == "provider_close_with_basis_aware_splits_and_dividends"
            for period in result.benchmark_periods["A"]
        )
        assert result.regime_metrics.total_transaction_costs == 0
        assert all(period.raw_complete_tickers == 2 for period in result.periods)
    finally:
        store.close()


def test_backtest_targets_membership_effective_on_next_session(monkeypatch, tmp_path):
    store = Store(tmp_path / "backtest-execution-membership.duckdb")
    try:
        _seed_prices(store)
        _use_pit_regime(monkeypatch)
        store.upsert_universe_membership(
            [
                {
                    "universe_id": "demo",
                    "ticker": "A",
                    "effective_start": "2024-01-01",
                    "effective_end": "2024-07-01",
                    "known_date": "2023-12-15",
                    "end_known_date": "2024-06-15",
                    "source": "test",
                },
                {
                    "universe_id": "demo",
                    "ticker": "B",
                    "effective_start": "2024-07-01",
                    "effective_end": None,
                    "known_date": "2024-06-15",
                    "end_known_date": None,
                    "source": "test",
                },
            ]
        )
        store.upsert_security_identities(
            [
                {
                    "universe_id": "demo",
                    "ticker": "A",
                    "effective_start": "2024-01-01",
                    "effective_end": "2024-07-01",
                    "security_id": "aios:security:a",
                    "known_date": "2023-12-15",
                    "identity_status": "bounded_ticker",
                    "source": "test",
                },
                {
                    "universe_id": "demo",
                    "ticker": "B",
                    "effective_start": "2024-07-01",
                    "effective_end": None,
                    "security_id": "aios:security:b",
                    "known_date": "2024-06-15",
                    "identity_status": "bounded_ticker",
                    "source": "test",
                },
            ]
        )
        store.execute(
            """
            UPDATE prices
            SET security_id = CASE ticker
                WHEN 'A' THEN 'aios:security:a'
                WHEN 'B' THEN 'aios:security:b'
            END
            WHERE ticker IN ('A', 'B')
            """
        )
        monkeypatch.setattr(store, "data_quality_report", lambda: [])
        calls: list[tuple[str, tuple[str, ...]]] = []

        def fake_compute(tickers, as_of, store, regime_snapshot=None):
            calls.append((str(as_of), tuple(tickers)))
            return [_row(ticker, 100, 100) for ticker in tickers]

        monkeypatch.setattr(engine, "compute_composite", fake_compute)
        monkeypatch.setattr(
            store,
            "universe_data_coverage",
            lambda universe_id, as_of, effective_on=None: [
                {
                    "ticker": ticker,
                    "security_id": f"aios:security:{ticker.lower()}",
                    "has_price_history": True,
                    "has_pit_fundamentals": True,
                    "latest_price_date": as_of,
                    "latest_fundamental_date": date(2024, 2, 1),
                }
                for ticker in (
                    row["ticker"]
                    for row in store.universe_membership_known_on(
                        universe_id,
                        as_of,
                        effective_on or as_of,
                    )
                )
            ],
        )

        result = engine.run_qv_policy_backtest(
            "2024-01-01",
            "2024-09-30",
            universe_id="demo",
            top_n=1,
            store=store,
        )

        assert calls == [
            ("2024-03-29", ("A",)),
            ("2024-06-28", ("B",)),
        ]
        assert [period.member_tickers for period in result.periods] == [("A",), ("B",)]
        assert all(period.status == "complete" for period in result.periods)
    finally:
        store.close()


def test_skipped_policy_period_is_excluded_from_all_paired_metrics(monkeypatch, tmp_path):
    store = Store(tmp_path / "backtest-paired.duckdb")
    try:
        store.upsert_prices(
            [
                {
                    "ticker": ticker,
                    "date": observation_date,
                    "close": price,
                    "adj_close": price,
                    "source": "test",
                }
                for ticker, observation_date, price in (
                    ("A", "2024-03-29", 100.0),
                    ("A", "2024-04-01", 100.0),
                    ("A", "2024-06-28", 110.0),
                    ("B", "2024-03-29", 100.0),
                    ("B", "2024-04-01", 100.0),
                )
            ]
        )
        _use_pit_regime(monkeypatch)
        monkeypatch.setattr(
            engine,
            "compute_composite",
            lambda tickers, as_of, store, regime_snapshot=None: [
                _row("A", 100, 0),
                _row("B", 0, 100),
            ],
        )

        result = engine.run_qv_policy_backtest(
            "2024-01-01",
            "2024-06-28",
            tickers=["A", "B"],
            top_n=1,
            allow_current_universe=True,
            benchmark_tickers=["A"],
            store=store,
        )

        assert result.periods[0].status == "skipped_missing_prices"
        assert result.comparison_periods == 0
        assert result.regime_metrics.completed_periods == 0
        assert result.baseline_metrics.completed_periods == 0
        assert result.benchmark_metrics["A"].completed_periods == 0
        assert result.benchmark_periods["A"][0].status == "skipped_unpaired_strategy_period"
    finally:
        store.close()


def test_factor_audit_records_stale_and_missing_evidence():
    rows = [_row("A", 75, 60), _row("B", 55, 50)]
    coverage = [
        {
            "ticker": "A",
            "security_id": "security-a",
            "has_price_history": True,
            "has_pit_fundamentals": True,
            "latest_price_date": date(2024, 3, 28),
            "latest_fundamental_date": date(2024, 2, 1),
        },
        {
            "ticker": "B",
            "security_id": "security-b",
            "has_price_history": True,
            "has_pit_fundamentals": False,
            "latest_price_date": date(2024, 3, 29),
            "latest_fundamental_date": None,
        },
    ]

    audit = engine._build_factor_audit(
        rows,
        coverage,
        date(2024, 3, 29),
        excluded_tickers={"A"},
    )

    assert [row.eligible for row in audit] == [False, False]
    assert "explicit_policy_exclusion" in audit[0].reasons
    assert "stale_price:2024-03-28" in audit[0].reasons
    assert "missing_pit_fundamentals" in audit[1].reasons

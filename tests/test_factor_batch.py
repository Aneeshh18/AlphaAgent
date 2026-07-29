from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from aios.factor_batch import DecisionScopedFactorStore
from aios.factors.composite import compute_composite
from aios.storage.store import Store


def _fundamental(
    ticker: str,
    period_end: str,
    as_of_date: str,
    fiscal_period: str,
    metric: str,
    value: float,
) -> dict:
    return {
        "ticker": ticker,
        "period_end": period_end,
        "as_of_date": as_of_date,
        "fiscal_period": fiscal_period,
        "statement": "test",
        "metric": metric,
        "value": value,
        "quarter_value": value,
        "unit": "USD",
        "source": "test",
    }


def _seed_factor_company(store: Store, ticker: str, scale: float) -> None:
    metric_values = {
        "capex": -8,
        "cash": 20,
        "cfo": 18,
        "current_assets": 80,
        "current_liabilities": 40,
        "debt_total": 30,
        "depreciation": 4,
        "eps_diluted": 3,
        "gross_profit": 55,
        "net_income": 12,
        "operating_income": 16,
        "revenue": 100,
        "shares_out": 10,
        "stockholders_equity": 70,
        "total_assets": 140,
    }
    rows = []
    for year, period_end, filed_on, year_scale in (
        (2022, "2022-12-31", "2023-02-15", 0.85),
        (2023, "2023-12-31", "2024-02-15", 1.0),
    ):
        rows.extend(
            _fundamental(
                ticker,
                period_end,
                filed_on,
                f"FY{year}",
                metric,
                value * scale * year_scale,
            )
            for metric, value in metric_values.items()
        )
    rows.extend(
        [
            _fundamental(
                ticker,
                "2023-12-31",
                "2024-03-01",
                "FY2023",
                "revenue",
                105 * scale,
            ),
            _fundamental(
                ticker,
                "2023-12-31",
                "2025-01-15",
                "FY2023",
                "revenue",
                999 * scale,
            ),
        ]
    )
    store.upsert_fundamentals(rows)
    store.upsert_prices(
        [
            {
                "ticker": ticker,
                "date": "2024-12-27",
                "close": 20 * scale,
                "actions_complete": True,
                "close_split_adjusted": False,
                "split_normalization_factor": 1.0,
                "source": "test",
            },
            {
                "ticker": ticker,
                "date": "2024-12-30",
                "close": 21 * scale,
                "actions_complete": True,
                "close_split_adjusted": False,
                "split_normalization_factor": 1.0,
                "source": "test",
            },
        ]
    )


@pytest.mark.parametrize("include_market_factors", [False, True])
def test_decision_scoped_store_matches_scalar_composite(
    monkeypatch,
    tmp_path,
    include_market_factors,
):
    store = Store(tmp_path / "factor-batch-composite.duckdb")
    try:
        tickers = ["ALPHA", "BETA"]
        _seed_factor_company(store, "ALPHA", 1.0)
        _seed_factor_company(store, "BETA", 1.4)
        query_count = 0
        original_query = store.query

        def counted_query(sql, params=None):
            nonlocal query_count
            query_count += 1
            return original_query(sql, params)

        monkeypatch.setattr(store, "query", counted_query)
        no_regime = SimpleNamespace(
            as_of="2024-12-31",
            is_pit_ready=False,
            regime="unknown",
            missing=[],
        )

        batch_rows = compute_composite(
            tickers,
            "2024-12-31",
            DecisionScopedFactorStore(store, tickers),
            regime_snapshot=no_regime,
            include_market_factors=include_market_factors,
        )
        batch_query_count = query_count
        scalar_rows = compute_composite(
            tickers,
            "2024-12-31",
            store,
            regime_snapshot=no_regime,
            include_market_factors=include_market_factors,
        )

        assert batch_query_count == 7
        assert [asdict(row) for row in batch_rows] == [asdict(row) for row in scalar_rows]
    finally:
        store.close()


def test_decision_scoped_store_falls_back_once_then_uses_scalar_per_ticker(
    monkeypatch,
    tmp_path,
):
    store = Store(tmp_path / "factor-batch-fallback.duckdb")
    try:
        tickers = ["ALPHA", "BETA"]
        for index, ticker in enumerate(tickers, start=1):
            store.upsert_fundamentals(
                [
                    _fundamental(
                        ticker,
                        "2023-12-31",
                        "2024-02-01",
                        "FY2023",
                        "revenue",
                        index * 100,
                    )
                ]
            )
            store.upsert_prices(
                [
                    {
                        "ticker": ticker,
                        "date": price_date,
                        "close": index * 10 + offset,
                        "actions_complete": True,
                        "close_split_adjusted": False,
                        "split_normalization_factor": 1.0,
                        "source": "test",
                    }
                    for price_date, offset in (
                        ("2024-01-31", 0),
                        ("2024-02-01", 1),
                    )
                ]
            )

        calls: Counter[str] = Counter()
        scalar_fundamentals = store.pit_factor_fundamentals
        scalar_latest = store.latest_price
        scalar_history = store.pit_factor_price_history

        def fail_batch(name):
            def failing(*_args, **_kwargs):
                calls[f"batch_{name}"] += 1
                raise RuntimeError(f"{name} batch unavailable")

            return failing

        def counted_fundamentals(*args, **kwargs):
            calls["scalar_fundamentals"] += 1
            return scalar_fundamentals(*args, **kwargs)

        def counted_latest(*args, **kwargs):
            calls["scalar_latest"] += 1
            return scalar_latest(*args, **kwargs)

        def counted_history(*args, **kwargs):
            calls["scalar_history"] += 1
            return scalar_history(*args, **kwargs)

        monkeypatch.setattr(
            store,
            "pit_factor_fundamentals_batch",
            fail_batch("fundamentals"),
        )
        monkeypatch.setattr(store, "pit_factor_latest_prices_batch", fail_batch("latest"))
        monkeypatch.setattr(
            store,
            "pit_factor_price_histories_batch",
            fail_batch("history"),
        )
        monkeypatch.setattr(store, "pit_factor_fundamentals", counted_fundamentals)
        monkeypatch.setattr(store, "latest_price", counted_latest)
        monkeypatch.setattr(store, "pit_factor_price_history", counted_history)

        factor_store = DecisionScopedFactorStore(store, tickers)
        for decision_date in ("2024-12-31", "2023-12-31"):
            for ticker in tickers:
                assert factor_store.pit_factor_fundamentals(
                    ticker,
                    decision_date,
                    ["revenue"],
                ) == scalar_fundamentals(ticker, decision_date, ["revenue"])
        for ticker in tickers:
            assert factor_store.latest_price(ticker, "2024-12-31") == scalar_latest(
                ticker,
                "2024-12-31",
            )
            assert factor_store.pit_factor_price_history(
                ticker,
                "2024-12-31",
                observations=2,
            ) == scalar_history(ticker, "2024-12-31", observations=2)

        assert calls == Counter(
            {
                "batch_fundamentals": 2,
                "scalar_fundamentals": 4,
                "batch_latest": 1,
                "scalar_latest": 2,
                "batch_history": 1,
                "scalar_history": 2,
            }
        )
    finally:
        store.close()


def test_decision_scoped_store_rejects_incomplete_batch_result(
    monkeypatch,
    tmp_path,
):
    store = Store(tmp_path / "factor-batch-invalid-keys.duckdb")
    try:
        store.upsert_fundamentals(
            [
                _fundamental(
                    "BETA",
                    "2023-12-31",
                    "2024-02-01",
                    "FY2023",
                    "revenue",
                    200,
                )
            ]
        )
        scalar_calls = 0
        scalar_fundamentals = store.pit_factor_fundamentals

        monkeypatch.setattr(
            store,
            "pit_factor_fundamentals_batch",
            lambda *_args, **_kwargs: {"ALPHA": []},
        )

        def counted_scalar(*args, **kwargs):
            nonlocal scalar_calls
            scalar_calls += 1
            return scalar_fundamentals(*args, **kwargs)

        monkeypatch.setattr(store, "pit_factor_fundamentals", counted_scalar)
        factor_store = DecisionScopedFactorStore(store, ["ALPHA", "BETA"])

        rows = factor_store.pit_factor_fundamentals(
            "BETA",
            "2024-12-31",
            ["revenue"],
        )
        assert rows[0]["value"] == 200
        assert scalar_calls == 1
    finally:
        store.close()


def test_new_decision_scoped_store_sees_later_restatement(tmp_path):
    store = Store(tmp_path / "factor-batch-scope.duckdb")
    try:
        store.upsert_fundamentals(
            [
                _fundamental(
                    "ALPHA",
                    "2023-12-31",
                    "2024-02-01",
                    "FY2023",
                    "revenue",
                    100,
                )
            ]
        )
        first = DecisionScopedFactorStore(store, ["ALPHA"])
        assert first.pit_factor_fundamentals(
            "ALPHA",
            "2024-12-31",
            ["revenue"],
        )[0]["value"] == 100

        store.upsert_fundamentals(
            [
                _fundamental(
                    "ALPHA",
                    "2023-12-31",
                    "2024-03-01",
                    "FY2023",
                    "revenue",
                    110,
                )
            ]
        )
        second = DecisionScopedFactorStore(store, ["ALPHA"])
        assert second.pit_factor_fundamentals(
            "ALPHA",
            "2024-12-31",
            ["revenue"],
        )[0]["value"] == 110
    finally:
        store.close()


def test_decision_scoped_store_batches_on_read_only_connection(tmp_path):
    db_path = tmp_path / "factor-batch-read-only.duckdb"
    store = Store(db_path)
    try:
        store.upsert_fundamentals(
            [
                _fundamental(
                    "ALPHA",
                    "2023-12-31",
                    "2024-02-01",
                    "FY2023",
                    "revenue",
                    100,
                )
            ]
        )
    finally:
        store.close()

    read_only_store = Store(db_path, read_only=True)
    try:
        factor_store = DecisionScopedFactorStore(read_only_store, ["ALPHA"])
        rows = factor_store.pit_factor_fundamentals(
            "ALPHA",
            "2024-12-31",
            ["revenue"],
        )
        assert rows[0]["value"] == 100
    finally:
        read_only_store.close()


def test_batch_temp_relations_are_cleaned_after_success_and_query_failure(
    monkeypatch,
    tmp_path,
):
    store = Store(tmp_path / "factor-batch-temp-cleanup.duckdb")
    try:
        store.upsert_fundamentals(
            [
                _fundamental(
                    "ALPHA",
                    "2023-12-31",
                    "2024-02-01",
                    "FY2023",
                    "revenue",
                    100,
                )
            ]
        )

        assert store.pit_factor_fundamentals_batch(
            ["ALPHA"],
            "2024-12-31",
            ["revenue"],
        )["ALPHA"][0]["value"] == 100
        assert store.query(
            """
            SELECT view_name
            FROM duckdb_views()
            WHERE view_name LIKE '_tmp_factor_%'
            """
        ) == []

        original_query = store.query

        def fail_data_query(sql, params=None):
            if "WITH selected AS" in sql:
                raise RuntimeError("forced factor data query failure")
            return original_query(sql, params)

        monkeypatch.setattr(store, "query", fail_data_query)
        with pytest.raises(RuntimeError, match="forced factor data query failure"):
            store.pit_factor_fundamentals_batch(
                ["ALPHA"],
                "2024-12-31",
                ["revenue"],
            )
        monkeypatch.setattr(store, "query", original_query)

        assert store.query(
            """
            SELECT view_name
            FROM duckdb_views()
            WHERE view_name LIKE '_tmp_factor_%'
            """
        ) == []
    finally:
        store.close()

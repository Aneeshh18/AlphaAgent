from __future__ import annotations

from datetime import date

from aios.factors.common import ttm_sum
from aios.factors.value import compute_value_raw
from aios.storage.store import Store


def _fundamental(
    ticker: str,
    period_end: str,
    as_of: str,
    metric: str,
    value: float,
    *,
    quarter_value: float | None = None,
    fiscal_period: str = "Q1_2023",
) -> dict:
    return {
        "ticker": ticker,
        "period_end": period_end,
        "as_of_date": as_of,
        "fiscal_period": fiscal_period,
        "statement": "income",
        "metric": metric,
        "value": value,
        "quarter_value": value if quarter_value is None else quarter_value,
        "unit": "USD",
        "source": "test",
    }


def test_pit_fundamentals_selects_only_known_latest_row(tmp_path):
    store = Store(tmp_path / "pit.duckdb")
    try:
        store.upsert_fundamentals([
            _fundamental("TEST", "2023-12-31", "2024-02-01", "revenue", 100),
            _fundamental("TEST", "2023-12-31", "2024-03-01", "revenue", 110),
            _fundamental("TEST", "2023-12-31", "2025-01-01", "revenue", 120),
        ])

        before_restatement = store.pit_fundamentals("TEST", "2024-02-15", ["revenue"])
        after_restatement = store.pit_fundamentals("TEST", "2024-12-31", ["revenue"])

        assert before_restatement[0]["value"] == 100
        assert after_restatement[0]["value"] == 110
    finally:
        store.close()


def test_price_upsert_preserves_provider_source(tmp_path):
    store = Store(tmp_path / "prices.duckdb")
    try:
        store.upsert_prices([{
            "ticker": "TEST",
            "date": date(2024, 1, 2),
            "open": 1,
            "high": 2,
            "low": 0.5,
            "close": 1.5,
            "adj_close": None,
            "volume": 100,
            "dividends": 0,
            "split_ratio": 1,
            "source": "stooq",
        }])

        assert store.price_on("TEST", "2024-01-02")["source"] == "stooq"
    finally:
        store.close()


def test_ttm_sum_uses_four_known_quarters(tmp_path):
    store = Store(tmp_path / "ttm.duckdb")
    try:
        rows = []
        for i, (period_end, quarter_value) in enumerate([
            ("2023-03-31", 10),
            ("2023-06-30", 20),
            ("2023-09-30", 30),
            ("2023-12-31", 40),
        ], start=1):
            rows.append(_fundamental(
                "TEST", period_end, "2024-02-01", "revenue", quarter_value,
                quarter_value=quarter_value, fiscal_period=f"Q{i}_2023",
            ))
        rows.append(_fundamental(
            "TEST", "2024-03-31", "2025-01-01", "revenue", 999,
            quarter_value=999, fiscal_period="Q1_2024",
        ))
        store.upsert_fundamentals(rows)

        assert ttm_sum(store, "TEST", "2024-12-31", "revenue") == 100
    finally:
        store.close()


def test_value_derives_ebitda_instead_of_using_mislabeled_net_income(tmp_path):
    store = Store(tmp_path / "value.duckdb")
    try:
        rows = []
        periods = ["2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31"]
        for i, period_end in enumerate(periods, start=1):
            as_of = "2024-02-01"
            fp = f"Q{i}_2023"
            rows.extend([
                _fundamental("TEST", period_end, as_of, "operating_income", 10,
                             fiscal_period=fp),
                _fundamental("TEST", period_end, as_of, "depreciation", 2,
                             fiscal_period=fp),
                _fundamental("TEST", period_end, as_of, "eps_diluted", 1,
                             fiscal_period=fp),
                _fundamental("TEST", period_end, as_of, "cfo", 8,
                             fiscal_period=fp),
                _fundamental("TEST", period_end, as_of, "capex", -2,
                             fiscal_period=fp),
                _fundamental("TEST", period_end, as_of, "revenue", 100,
                             fiscal_period=fp),
            ])
        # This row represents the historical bug: net income was stored under
        # the ebitda metric. The value factor must not read it.
        rows.append(_fundamental(
            "TEST", "2023-12-31", "2024-02-01", "ebitda", 999,
            fiscal_period="Q4_2023",
        ))
        rows.extend([
            _fundamental("TEST", "2023-12-31", "2024-02-01", "shares_out", 10),
            _fundamental("TEST", "2023-12-31", "2024-02-01", "debt_total", 20),
            _fundamental("TEST", "2023-12-31", "2024-02-01", "cash", 5),
            _fundamental("TEST", "2023-12-31", "2024-02-01", "stockholders_equity", 50),
        ])
        store.upsert_fundamentals(rows)
        store.upsert_prices([{
            "ticker": "TEST", "date": "2024-01-31",
            "open": 10, "high": 10, "low": 10, "close": 10,
            "adj_close": 10, "volume": 100, "dividends": 0, "split_ratio": 1,
            "source": "test",
        }])

        snap = compute_value_raw("TEST", "2024-02-01", store)

        assert snap.ttm_ebitda == 48
        assert snap.ev_ebitda == 115 / 48
    finally:
        store.close()

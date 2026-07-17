from datetime import date

import pytest

from aios.backtest.costs import TaxPolicy, TransactionCostPolicy, simulate_period
from aios.storage.store import Store


def test_costs_and_taxes_reduce_gross_return(tmp_path):
    store = Store(tmp_path / "costs.duckdb")
    try:
        store.upsert_prices(
            [
                {
                    "ticker": "A",
                    "date": "2024-04-01",
                    "close": 100.0,
                    "adj_close": 100.0,
                    "source": "test",
                },
                {
                    "ticker": "A",
                    "date": "2024-06-01",
                    "close": 105.0,
                    "adj_close": 105.0,
                    "dividends": 2.0,
                    "source": "test",
                },
                {
                    "ticker": "A",
                    "date": "2024-06-28",
                    "close": 110.0,
                    "adj_close": 112.1,
                    "source": "test",
                },
            ]
        )
        result = simulate_period(
            ("A",),
            date(2024, 3, 29),
            date(2024, 6, 28),
            store,
            initial_capital=1_000.0,
            transaction_costs=TransactionCostPolicy(
                commission_bps=100.0,
                slippage_bps=50.0,
                fixed_fee=10.0,
            ),
            tax_policy=TaxPolicy(
                short_term_rate=0.20,
                dividend_rate=0.10,
            ),
        )

        assert result.gross_return == pytest.approx(0.121)
        assert result.commission == 21.0
        assert result.slippage == 10.5
        assert result.fixed_fees == 20.0
        assert result.taxes == 22.0
        assert result.net_return == pytest.approx(0.0475)
        assert result.turnover == 2.1
    finally:
        store.close()

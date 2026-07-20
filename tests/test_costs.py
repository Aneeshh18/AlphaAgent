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


def test_period_follows_stable_security_across_ticker_change(tmp_path):
    store = Store(tmp_path / "ticker-change.duckdb")
    security_id = "aios:security:demo-common"
    memberships = [
        {
            "universe_id": "demo",
            "ticker": "OLD",
            "effective_start": "2024-01-01",
            "effective_end": "2024-07-01",
            "known_date": "2024-01-01",
            "source": "https://example.com/membership",
        },
        {
            "universe_id": "demo",
            "ticker": "NEW",
            "effective_start": "2024-07-01",
            "effective_end": None,
            "known_date": "2024-06-15",
            "source": "https://example.com/membership",
        },
    ]
    try:
        store.upsert_universe_membership(memberships)
        store.upsert_security_identities(
            [
                {
                    **membership,
                    "security_id": security_id,
                    "identity_status": "verified_ticker_change",
                }
                for membership in memberships
            ]
        )
        store.upsert_prices(
            [
                {
                    "ticker": "OLD",
                    "security_id": security_id,
                    "date": "2024-04-01",
                    "close": 100.0,
                    "adj_close": 100.0,
                    "source": "test",
                },
                {
                    "ticker": "NEW",
                    "security_id": security_id,
                    "date": "2024-09-30",
                    "close": 125.0,
                    "adj_close": 125.0,
                    "source": "test",
                },
            ]
        )

        result = simulate_period(
            ("OLD",),
            date(2024, 3, 29),
            date(2024, 9, 30),
            store,
            initial_capital=1_000.0,
            transaction_costs=TransactionCostPolicy.zero(),
            tax_policy=TaxPolicy.zero(),
            scheduled_entry_date=date(2024, 4, 1),
            scheduled_exit_date=date(2024, 9, 30),
        )

        assert result.net_return == pytest.approx(0.25)
        assert result.missing == ()
    finally:
        store.close()


def test_scheduled_date_missing_fails_instead_of_shortening_window(tmp_path):
    store = Store(tmp_path / "scheduled-date.duckdb")
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
                    "date": "2024-06-27",
                    "close": 110.0,
                    "adj_close": 110.0,
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
            transaction_costs=TransactionCostPolicy.zero(),
            tax_policy=TaxPolicy.zero(),
            scheduled_entry_date=date(2024, 4, 1),
            scheduled_exit_date=date(2024, 6, 28),
        )

        assert result.net_return is None
        assert "A:missing_scheduled_exit_price:2024-06-28" in result.missing
    finally:
        store.close()

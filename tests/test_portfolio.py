from datetime import date

import pytest

from aios.backtest.costs import TaxPolicy, TransactionCostPolicy
from aios.backtest.portfolio import PortfolioBook
from aios.storage.store import Store


def _book(
    store: Store,
    *,
    tax_policy: TaxPolicy | None = None,
    transaction_costs: TransactionCostPolicy | None = None,
    calendar_ticker: str = "A",
) -> PortfolioBook:
    return PortfolioBook(
        store,
        initial_capital=1_000.0,
        transaction_costs=transaction_costs or TransactionCostPolicy.zero(),
        tax_policy=tax_policy or TaxPolicy.zero(),
        calendar_ticker=calendar_ticker,
    )


def test_same_holding_persists_without_quarterly_round_trip(tmp_path):
    store = Store(tmp_path / "persistent.duckdb")
    try:
        store.upsert_prices(
            [
                {
                    "ticker": "A",
                    "date": observation_date,
                    "close": price,
                    "adj_close": price,
                    "source": "test",
                }
                for observation_date, price in (
                    ("2023-03-31", 100.0),
                    ("2023-04-03", 100.0),
                    ("2023-06-30", 110.0),
                    ("2023-07-03", 110.0),
                    ("2023-09-29", 120.0),
                )
            ]
        )
        book = _book(store)

        first = book.advance_period(
            ("A",),
            date(2023, 3, 31),
            date(2023, 4, 3),
            date(2023, 6, 30),
        )
        second = book.advance_period(
            ("A",),
            date(2023, 6, 30),
            date(2023, 7, 3),
            date(2023, 9, 29),
        )

        assert [trade.side for trade in first.trades] == ["buy"]
        assert first.turnover == pytest.approx(1.0)
        assert second.trades == ()
        assert second.turnover == 0.0
        assert second.open_tax_lots == 1
        assert second.period_return == pytest.approx(120.0 / 110.0 - 1.0)
        assert book.curve[0].date == date(2023, 3, 31)
        assert book.curve[-1].date == date(2023, 9, 29)
        assert book.curve[-1].equity == pytest.approx(1_200.0)
    finally:
        store.close()


def test_fifo_lot_ages_across_periods_and_realizes_long_term_gain(tmp_path):
    store = Store(tmp_path / "cross-quarter-tax.duckdb")
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
                    ("A", "2023-03-31", 100.0),
                    ("A", "2023-04-03", 100.0),
                    ("A", "2023-06-30", 110.0),
                    ("A", "2023-07-03", 110.0),
                    ("A", "2023-09-29", 120.0),
                    ("A", "2024-04-03", 150.0),
                    ("A", "2024-06-28", 150.0),
                    ("B", "2024-04-03", 50.0),
                    ("B", "2024-06-28", 55.0),
                )
            ]
        )
        book = _book(
            store,
            tax_policy=TaxPolicy(short_term_rate=0.40, long_term_rate=0.20),
        )
        book.advance_period(
            ("A",),
            date(2023, 3, 31),
            date(2023, 4, 3),
            date(2023, 6, 30),
        )
        held = book.advance_period(
            ("A",),
            date(2023, 6, 30),
            date(2023, 7, 3),
            date(2023, 9, 29),
        )
        switched = book.advance_period(
            ("B",),
            date(2023, 9, 29),
            date(2024, 4, 3),
            date(2024, 6, 28),
        )

        assert held.trades == ()
        assert [trade.side for trade in switched.trades] == ["sell", "buy"]
        assert switched.trades[0].realized_gain == pytest.approx(500.0)
        assert switched.short_term_tax == 0.0
        assert switched.long_term_tax == pytest.approx(100.0)
        assert switched.taxes == pytest.approx(100.0)
        assert switched.ending_holdings == ("B",)
    finally:
        store.close()


def test_split_dividend_and_daily_equity_use_raw_accounting(tmp_path):
    store = Store(tmp_path / "corporate-actions.duckdb")
    try:
        store.upsert_prices(
            [
                {
                    "ticker": "A",
                    "date": observation_date,
                    "close": price,
                    "adj_close": adjusted,
                    "dividends": dividend,
                    "split_ratio": split,
                    "source": "test",
                }
                for observation_date, price, adjusted, dividend, split in (
                    ("2024-03-28", 100.0, 100.0, 0.0, 1.0),
                    ("2024-04-01", 100.0, 100.0, 0.0, 1.0),
                    ("2024-05-01", 50.0, 51.0, 1.0, 2.0),
                    ("2024-06-28", 55.0, 57.0, 0.0, 1.0),
                )
            ]
        )
        book = _book(store, tax_policy=TaxPolicy(dividend_rate=0.25))

        result = book.advance_period(
            ("A",),
            date(2024, 3, 28),
            date(2024, 4, 1),
            date(2024, 6, 28),
        )

        assert result.dividend_tax == pytest.approx(5.0)
        assert result.ending_equity == pytest.approx(1_115.0)
        assert result.period_return == pytest.approx(0.115)
        assert [point.date for point in book.curve] == [
            date(2024, 3, 28),
            date(2024, 4, 1),
            date(2024, 5, 1),
            date(2024, 6, 28),
        ]
        assert book.curve[-1].accrued_taxes == pytest.approx(5.0)
    finally:
        store.close()


def test_stable_security_identity_prevents_false_ticker_change_sale(tmp_path):
    store = Store(tmp_path / "ticker-change-book.duckdb")
    security_id = "aios:security:renamed"
    assignments = [
        {
            "universe_id": "demo",
            "ticker": "OLD",
            "effective_start": "2024-01-01",
            "effective_end": "2024-07-01",
            "known_date": "2024-01-01",
            "source": "test",
        },
        {
            "universe_id": "demo",
            "ticker": "NEW",
            "effective_start": "2024-07-01",
            "effective_end": None,
            "known_date": "2024-06-15",
            "source": "test",
        },
    ]
    try:
        store.upsert_universe_membership(assignments)
        store.upsert_security_identities(
            [
                {
                    **assignment,
                    "security_id": security_id,
                    "identity_status": "verified_ticker_change",
                }
                for assignment in assignments
            ]
        )
        store.upsert_prices(
            [
                {
                    "ticker": ticker,
                    "security_id": row_security_id,
                    "date": observation_date,
                    "close": price,
                    "adj_close": price,
                    "source": "test",
                }
                for ticker, row_security_id, observation_date, price in (
                    ("MKT", None, "2024-03-28", 100.0),
                    ("MKT", None, "2024-04-01", 100.0),
                    ("MKT", None, "2024-06-28", 100.0),
                    ("MKT", None, "2024-07-01", 100.0),
                    ("MKT", None, "2024-09-30", 100.0),
                    ("OLD", security_id, "2024-04-01", 100.0),
                    ("OLD", security_id, "2024-06-28", 110.0),
                    ("NEW", security_id, "2024-07-01", 110.0),
                    ("NEW", security_id, "2024-09-30", 120.0),
                )
            ]
        )
        book = _book(store, calendar_ticker="MKT")
        book.advance_period(
            ("OLD",),
            date(2024, 3, 28),
            date(2024, 4, 1),
            date(2024, 6, 28),
        )
        renamed = book.advance_period(
            ("OLD",),
            date(2024, 6, 28),
            date(2024, 7, 1),
            date(2024, 9, 30),
        )

        assert renamed.trades == ()
        assert renamed.ending_holdings == ("NEW",)
        assert renamed.open_tax_lots == 1
    finally:
        store.close()


def test_mid_period_data_failure_rolls_back_entire_book(tmp_path):
    store = Store(tmp_path / "atomic-period.duckdb")
    try:
        store.upsert_prices(
            [
                {
                    "ticker": "A",
                    "date": observation_date,
                    "close": price,
                    "adj_close": price,
                    "dividends": dividend,
                    "source": "test",
                }
                for observation_date, price, dividend in (
                    ("2024-03-28", 100.0, 0.0),
                    ("2024-04-01", 100.0, 0.0),
                    ("2024-06-28", 110.0, 0.0),
                    ("2024-07-01", 110.0, 0.0),
                    ("2024-08-01", 115.0, -1.0),
                    ("2024-09-30", 120.0, 0.0),
                )
            ]
        )
        book = _book(store)
        book.advance_period(
            ("A",),
            date(2024, 3, 28),
            date(2024, 4, 1),
            date(2024, 6, 28),
        )
        before_curve = list(book.curve)
        before_cash = book.cash
        before_quantity = next(iter(book.positions.values())).quantity

        failed = book.advance_period(
            ("A",),
            date(2024, 6, 28),
            date(2024, 7, 1),
            date(2024, 9, 30),
        )

        assert failed.period_return is None
        assert failed.missing == ("invalid_corporate_action_value",)
        assert book.curve == before_curve
        assert book.cash == before_cash
        assert next(iter(book.positions.values())).quantity == before_quantity
        assert book.last_date == date(2024, 6, 28)
    finally:
        store.close()

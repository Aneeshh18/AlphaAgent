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


def test_portfolio_refuses_action_unverified_price_path(tmp_path):
    store = Store(tmp_path / "unverified-action-path.duckdb")
    try:
        store.upsert_prices(
            [
                {
                    "ticker": "A",
                    "date": observation_date,
                    "close": price,
                    "source": "stooq",
                    "actions_complete": False,
                }
                for observation_date, price in (
                    ("2024-03-28", 100.0),
                    ("2024-04-01", 100.0),
                    ("2024-06-28", 110.0),
                )
            ]
        )

        result = _book(store).advance_period(
            ("A",),
            date(2024, 3, 28),
            date(2024, 4, 1),
            date(2024, 6, 28),
        )

        assert result.period_return is None
        assert any("unverified_corporate_actions" in item for item in result.missing)
    finally:
        store.close()


def test_split_normalized_close_does_not_multiply_shares_again(tmp_path):
    store = Store(tmp_path / "split-normalized-close.duckdb")
    try:
        store.upsert_prices(
            [
                {
                    "ticker": "A",
                    "date": observation_date,
                    "close": price,
                    "split_ratio": split,
                    "actions_complete": True,
                    "close_split_adjusted": True,
                    "source": "yfinance",
                }
                for observation_date, price, split in (
                    ("2024-03-28", 100.0, 1.0),
                    ("2024-04-01", 100.0, 1.0),
                    ("2024-05-01", 101.0, 10.0),
                    ("2024-06-28", 110.0, 1.0),
                )
            ]
        )

        result = _book(store).advance_period(
            ("A",),
            date(2024, 3, 28),
            date(2024, 4, 1),
            date(2024, 6, 28),
        )

        assert result.ending_equity == pytest.approx(1_100.0)
        assert result.open_tax_lots == 1
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


def test_portfolio_state_round_trip_and_mark_only_advance(tmp_path):
    store = Store(tmp_path / "paper-state-round-trip.duckdb")
    try:
        store.upsert_prices(
            [
                {
                    "ticker": "A",
                    "date": observation_date,
                    "close": price,
                    "adj_close": price,
                    "actions_complete": True,
                    "close_split_adjusted": False,
                    "source": "test",
                }
                for observation_date, price in (
                    ("2024-03-28", 100.0),
                    ("2024-04-01", 100.0),
                    ("2024-06-28", 110.0),
                    ("2024-07-01", 115.0),
                )
            ]
        )
        book = _book(
            store,
            transaction_costs=TransactionCostPolicy(commission_bps=5),
        )
        book.advance_period(
            ("A",),
            date(2024, 3, 28),
            date(2024, 4, 1),
            date(2024, 6, 28),
        )

        restored = PortfolioBook.from_state(store, book.to_state())
        new_points = restored.mark_through(date(2024, 7, 1))

        assert restored.to_state()["positions"] == book.to_state()["positions"]
        assert restored.current_weights()["A"] > 0.99
        assert [point.date for point in new_points] == [date(2024, 7, 1)]
        assert restored.last_date == date(2024, 7, 1)
        assert restored.equity > book.equity
    finally:
        store.close()


def test_portfolio_state_rejects_missing_mark_for_open_position(tmp_path):
    store = Store(tmp_path / "invalid-paper-state.duckdb")
    try:
        state = _book(store).to_state()
        state["positions"] = [
            {
                "key": "ticker:A",
                "ticker": "A",
                "security_id": None,
                "lots": [
                    {
                        "ticker": "A",
                        "quantity": 1.0,
                        "cost_basis_per_share": 100.0,
                        "acquired_date": "2024-01-01",
                    }
                ],
            }
        ]

        with pytest.raises(ValueError, match="missing a position mark price"):
            PortfolioBook.from_state(store, state)
    finally:
        store.close()


def test_reviewed_stock_conversion_preserves_book_and_tax_lot(tmp_path):
    store = Store(tmp_path / "stock-conversion.duckdb")
    source_security_id = "aios:security:old"
    target_security_id = "aios:security:new"
    memberships = [
        {
            "universe_id": "demo",
            "ticker": ticker,
            "effective_start": "2024-01-01",
            "effective_end": None,
            "known_date": "2024-01-01",
            "source": "https://example.com/membership",
        }
        for ticker in ("OLD", "NEW")
    ]
    try:
        store.upsert_universe_membership(memberships)
        store.upsert_security_identities(
            [
                {
                    **membership,
                    "security_id": (
                        source_security_id
                        if membership["ticker"] == "OLD"
                        else target_security_id
                    ),
                    "identity_status": "bounded_ticker",
                }
                for membership in memberships
            ]
        )
        store.upsert_security_conversions(
            [
                {
                    "source_security_id": source_security_id,
                    "target_security_id": target_security_id,
                    "effective_date": "2024-05-01",
                    "known_date": "2024-05-01",
                    "share_ratio": 2.0,
                    "basis_policy": "carryover",
                    "review_status": "verified",
                    "verified_date": "2024-06-01",
                    "source": "https://www.sec.gov/Archives/example.htm",
                    "basis_source": "https://www.sec.gov/Archives/basis.htm",
                }
            ]
        )
        store.upsert_prices(
            [
                {
                    "ticker": ticker,
                    "security_id": security_id,
                    "date": observation_date,
                    "close": price,
                    "adj_close": price,
                    "actions_complete": True,
                    "close_split_adjusted": False,
                    "source": "test",
                }
                for ticker, security_id, observation_date, price in (
                    ("MKT", None, "2024-03-28", 100.0),
                    ("MKT", None, "2024-04-01", 100.0),
                    ("MKT", None, "2024-05-01", 100.0),
                    ("MKT", None, "2024-06-28", 100.0),
                    ("OLD", source_security_id, "2024-04-01", 100.0),
                    ("OLD", source_security_id, "2024-04-30", 110.0),
                    ("NEW", target_security_id, "2024-05-01", 60.0),
                    ("NEW", target_security_id, "2024-06-28", 66.0),
                )
            ]
        )

        book = _book(store, calendar_ticker="MKT")
        result = book.advance_period(
            ("OLD",),
            date(2024, 3, 28),
            date(2024, 4, 1),
            date(2024, 6, 28),
        )

        assert result.missing == ()
        assert result.ending_equity == pytest.approx(1_320.0)
        assert result.ending_holdings == ("NEW",)
        assert len(result.conversions) == 1
        assert result.conversions[0].source_quantity == pytest.approx(10.0)
        assert result.conversions[0].target_quantity == pytest.approx(20.0)
        position = next(iter(book.positions.values()))
        assert position.lots[0].acquired_date == date(2024, 4, 1)
        assert position.lots[0].cost_basis_per_share == pytest.approx(50.0)
        assert book.to_state()["conversions"][0]["target_ticker"] == "NEW"
    finally:
        store.close()


def test_paper_mark_through_applies_reviewed_stock_conversion(tmp_path):
    store = Store(tmp_path / "paper-conversion.duckdb")
    source_security_id = "aios:security:old"
    target_security_id = "aios:security:new"
    memberships = [
        {
            "universe_id": "demo",
            "ticker": ticker,
            "effective_start": "2024-01-01",
            "effective_end": None,
            "known_date": "2024-01-01",
            "source": "https://example.com/membership",
        }
        for ticker in ("OLD", "NEW")
    ]
    try:
        store.upsert_universe_membership(memberships)
        store.upsert_security_identities(
            [
                {
                    **membership,
                    "security_id": (
                        source_security_id
                        if membership["ticker"] == "OLD"
                        else target_security_id
                    ),
                    "identity_status": "bounded_ticker",
                }
                for membership in memberships
            ]
        )
        store.upsert_security_conversions(
            [
                {
                    "source_security_id": source_security_id,
                    "target_security_id": target_security_id,
                    "effective_date": "2024-05-01",
                    "known_date": "2024-05-01",
                    "share_ratio": 2.0,
                    "basis_policy": "carryover",
                    "review_status": "verified",
                    "verified_date": "2024-06-01",
                    "source": "https://www.sec.gov/Archives/example.htm",
                    "basis_source": "https://www.sec.gov/Archives/basis.htm",
                }
            ]
        )
        store.upsert_prices(
            [
                {
                    "ticker": ticker,
                    "security_id": security_id,
                    "date": observation_date,
                    "close": price,
                    "adj_close": price,
                    "actions_complete": True,
                    "close_split_adjusted": False,
                    "source": "test",
                }
                for ticker, security_id, observation_date, price in (
                    ("MKT", None, "2024-03-28", 100.0),
                    ("MKT", None, "2024-04-01", 100.0),
                    ("MKT", None, "2024-04-30", 100.0),
                    ("MKT", None, "2024-05-01", 100.0),
                    ("MKT", None, "2024-06-28", 100.0),
                    ("OLD", source_security_id, "2024-04-01", 100.0),
                    ("OLD", source_security_id, "2024-04-30", 110.0),
                    ("NEW", target_security_id, "2024-05-01", 60.0),
                    ("NEW", target_security_id, "2024-06-28", 66.0),
                )
            ]
        )
        book = _book(store, calendar_ticker="MKT")
        invested = book.advance_period(
            ("OLD",),
            date(2024, 3, 28),
            date(2024, 4, 1),
            date(2024, 4, 30),
        )
        assert invested.missing == ()

        points = book.mark_through(date(2024, 6, 28))

        assert points[-1].equity == pytest.approx(1_320.0)
        assert tuple(book.current_weights()) == ("NEW",)
        assert len(book.conversions) == 1
    finally:
        store.close()

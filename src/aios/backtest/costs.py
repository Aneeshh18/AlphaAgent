"""Explicit execution economics for the QV backtest.

This module keeps assumptions visible and testable. It does not encode a
jurisdiction's tax law: the caller supplies rates. The simulator uses FIFO
lots for the interval-level validation harness, nets gains and losses within
short- and long-term buckets, and does not model wash sales or tax filing
timing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from math import isfinite
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aios.storage.store import Store


@dataclass(frozen=True)
class TransactionCostPolicy:
    """Trading-cost assumptions, expressed in basis points and currency."""

    commission_bps: float = 0.0
    slippage_bps: float = 0.0
    fixed_fee: float = 0.0

    def __post_init__(self) -> None:
        for name, value in (
            ("commission_bps", self.commission_bps),
            ("slippage_bps", self.slippage_bps),
            ("fixed_fee", self.fixed_fee),
        ):
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")

    @classmethod
    def zero(cls) -> TransactionCostPolicy:
        """Return a no-friction policy for isolated factor tests."""
        return cls()

    def estimate(self, notional: float) -> tuple[float, float, float]:
        """Return commission, slippage, and fixed fee for one order."""
        if notional < 0 or not isfinite(notional):
            raise ValueError("trade notional must be finite and non-negative")
        commission = notional * self.commission_bps / 10_000.0
        slippage = notional * self.slippage_bps / 10_000.0
        return commission, slippage, self.fixed_fee

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class TaxPolicy:
    """Caller-supplied tax rates for realized gains and dividends."""

    short_term_rate: float = 0.0
    long_term_rate: float = 0.0
    dividend_rate: float = 0.0
    long_term_days: int = 365

    def __post_init__(self) -> None:
        for name, value in (
            ("short_term_rate", self.short_term_rate),
            ("long_term_rate", self.long_term_rate),
            ("dividend_rate", self.dividend_rate),
        ):
            if not isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.long_term_days < 1:
            raise ValueError("long_term_days must be at least 1")

    @classmethod
    def zero(cls) -> TaxPolicy:
        """Return a no-tax policy for tests or pre-tax diagnostics."""
        return cls()

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


@dataclass(frozen=True)
class TaxLot:
    """A FIFO lot used to classify a realized gain's holding period."""

    ticker: str
    quantity: float
    cost_basis_per_share: float
    acquired_date: date


@dataclass
class TaxLedger:
    """Realized gain/dividend ledger for one simulated policy interval."""

    short_term_gains: float = 0.0
    long_term_gains: float = 0.0
    dividend_income: float = 0.0

    def realize(
        self,
        lot: TaxLot,
        quantity: float,
        sale_price: float,
        sale_date: date,
        *,
        long_term_days: int,
    ) -> float:
        if quantity <= 0 or quantity > lot.quantity + 1e-9:
            raise ValueError("sale quantity must be positive and no larger than the tax lot")
        gain = quantity * (sale_price - lot.cost_basis_per_share)
        holding_days = (sale_date - lot.acquired_date).days
        if holding_days >= long_term_days:
            self.long_term_gains += gain
        else:
            self.short_term_gains += gain
        return gain

    def record_dividends(self, amount: float) -> None:
        if amount < 0:
            raise ValueError("dividend income cannot be negative")
        self.dividend_income += amount

    def taxes_due(self, policy: TaxPolicy) -> tuple[float, float, float, float]:
        """Return short-term, long-term, dividend, and total tax amounts.

        Losses offset gains in the same holding-period bucket. Cross-bucket
        offsets, carryforwards, and wash-sale rules are intentionally outside
        this phase and are called out in the result warning/documentation.
        """
        short_tax = max(self.short_term_gains, 0.0) * policy.short_term_rate
        long_tax = max(self.long_term_gains, 0.0) * policy.long_term_rate
        dividend_tax = self.dividend_income * policy.dividend_rate
        return short_tax, long_tax, dividend_tax, short_tax + long_tax + dividend_tax


@dataclass(frozen=True)
class FrictionResult:
    """Gross and net result for one equal-weight holding interval."""

    entry_date: date | None
    exit_date: date | None
    gross_return: float | None
    net_return: float | None
    transaction_costs: float
    commission: float
    slippage: float
    fixed_fees: float
    taxes: float
    short_term_tax: float
    long_term_tax: float
    dividend_tax: float
    turnover: float
    missing: tuple[str, ...] = ()


def simulate_period(
    tickers: tuple[str, ...],
    decision_date: date,
    next_decision_date: date,
    store: Store,
    *,
    initial_capital: float,
    transaction_costs: TransactionCostPolicy,
    tax_policy: TaxPolicy,
    scheduled_entry_date: date | None = None,
    scheduled_exit_date: date | None = None,
) -> FrictionResult:
    """Simulate equal-weight entry/exit with explicit costs and tax lots.

    A common entry and exit date is required for every selected security. When
    the engine supplies scheduled dates, every policy must price on those exact
    sessions; missing evidence fails the period instead of silently shortening
    it. Reviewed securities are queried by immutable ``security_id`` so a dated
    ticker change does not look like a sale or delisting. Adjusted close is used
    for gross total return when available; provider close and dividends are
    used for execution/tax accounting, with split ratios applied only when the
    provider close is not already split-normalized.
    """
    if initial_capital <= 0 or not isfinite(initial_capital):
        raise ValueError("initial_capital must be finite and positive")
    if not tickers:
        return _missing_result(("empty_portfolio",))

    by_ticker = {
        ticker: _holding_price_rows(
            ticker,
            decision_date,
            next_decision_date,
            store,
        )
        for ticker in tickers
    }
    date_sets = [{row["date"] for row in rows} for rows in by_ticker.values()]
    common_dates = set.intersection(*date_sets) if date_sets else set()
    entry_date = scheduled_entry_date or (min(common_dates) if common_dates else None)
    exit_date = scheduled_exit_date or (max(common_dates) if common_dates else None)
    missing: list[str] = []
    if entry_date is None:
        missing.append("common_entry_price")
    if exit_date is None:
        missing.append("common_exit_price")
    if scheduled_entry_date is not None:
        missing.extend(
            f"{ticker}:missing_scheduled_entry_price:{scheduled_entry_date}"
            for ticker, rows in by_ticker.items()
            if scheduled_entry_date not in {row["date"] for row in rows}
        )
    if scheduled_exit_date is not None:
        missing.extend(
            f"{ticker}:missing_scheduled_exit_price:{scheduled_exit_date}"
            for ticker, rows in by_ticker.items()
            if scheduled_exit_date not in {row["date"] for row in rows}
        )
    if missing:
        return _missing_result(tuple(sorted(set(missing))))
    if exit_date < entry_date:
        return _missing_result(("empty_holding_window",))
    missing.extend(
        f"{ticker}:unverified_corporate_actions:{row['date']}"
        for ticker, rows in by_ticker.items()
        for row in rows
        if row.get("actions_complete") is not True
    )
    missing.extend(
        f"{ticker}:split_adjustment_basis_unknown"
        for ticker, rows in by_ticker.items()
        if any(row.get("close_split_adjusted") is None for row in rows)
    )
    missing.extend(
        f"{ticker}:mixed_split_adjustment_basis"
        for ticker, rows in by_ticker.items()
        if len({row.get("close_split_adjusted") for row in rows}) > 1
    )
    if missing:
        return _missing_result(tuple(sorted(set(missing))))

    entry_rows: dict[str, dict] = {}
    exit_rows: dict[str, dict] = {}
    for ticker, rows in by_ticker.items():
        entries = [row for row in rows if row["date"] == entry_date]
        exits = [row for row in rows if row["date"] == exit_date]
        if len(entries) != 1:
            missing.append(f"{ticker}:ambiguous_entry_price")
        else:
            entry_rows[ticker] = entries[0]
        if len(exits) != 1:
            missing.append(f"{ticker}:ambiguous_exit_price")
        else:
            exit_rows[ticker] = exits[0]
    missing.extend(
        f"{ticker}:invalid_execution_price"
        for ticker in tickers
        if not _positive(entry_rows.get(ticker), "close")
        or not _positive(exit_rows.get(ticker), "close")
    )
    if missing:
        return _missing_result(tuple(sorted(set(missing))))

    allocation = initial_capital / len(tickers)
    gross_ending = 0.0
    commission = 0.0
    slippage = 0.0
    fixed_fees = 0.0
    turnover = 0.0
    ledger = TaxLedger()
    for ticker in tickers:
        entry = entry_rows[ticker]
        exit_row = exit_rows[ticker]
        entry_close = float(entry["close"])
        exit_close = float(exit_row["close"])
        entry_value = _return_price(entry)
        exit_value = _return_price(exit_row)
        if entry_value is None or exit_value is None or entry_value <= 0 or exit_value <= 0:
            missing.append(f"{ticker}:invalid_return_price")
            continue
        gross_ending += allocation * exit_value / entry_value

        entry_commission, entry_slippage, entry_fixed = transaction_costs.estimate(allocation)
        shares = allocation / entry_close
        basis_per_share = entry_close
        dividend_income = 0.0
        for action in by_ticker[ticker]:
            if action["date"] <= entry_date:
                continue
            split_ratio = _positive_number(action.get("split_ratio"), default=1.0)
            if split_ratio != 1.0 and action.get("close_split_adjusted") is not True:
                shares *= split_ratio
                basis_per_share /= split_ratio
            dividends = _positive_number(action.get("dividends"), default=0.0)
            dividend_income += shares * dividends
        exit_notional = shares * exit_close
        exit_commission, exit_slippage, exit_fixed = transaction_costs.estimate(exit_notional)
        commission += entry_commission + exit_commission
        slippage += entry_slippage + exit_slippage
        fixed_fees += entry_fixed + exit_fixed
        turnover += (allocation + exit_notional) / initial_capital
        ledger.record_dividends(dividend_income)
        ledger.realize(
            TaxLot(ticker, shares, basis_per_share, entry_date),
            shares,
            exit_close,
            exit_date,
            long_term_days=tax_policy.long_term_days,
        )

    if missing:
        return _missing_result(tuple(sorted(set(missing))))
    short_tax, long_tax, dividend_tax, taxes = ledger.taxes_due(tax_policy)
    transaction_total = commission + slippage + fixed_fees
    net_ending = gross_ending - transaction_total - taxes
    return FrictionResult(
        entry_date=entry_date,
        exit_date=exit_date,
        gross_return=gross_ending / initial_capital - 1.0,
        net_return=net_ending / initial_capital - 1.0,
        transaction_costs=transaction_total,
        commission=commission,
        slippage=slippage,
        fixed_fees=fixed_fees,
        taxes=taxes,
        short_term_tax=short_tax,
        long_term_tax=long_tax,
        dividend_tax=dividend_tax,
        turnover=turnover,
    )


def _holding_price_rows(
    ticker: str,
    decision_date: date,
    next_decision_date: date,
    store: Store,
) -> list[dict]:
    """Load one holding path without confusing a ticker label for identity."""
    security_id = store.security_id_for_ticker(ticker, decision_date)
    if security_id is not None:
        return store.query(
            """
            SELECT ticker, security_id, date, close, adj_close, dividends, split_ratio,
                   actions_complete, close_split_adjusted
            FROM prices
            WHERE security_id = ?
              AND date > ? AND date <= ?
            ORDER BY date, ticker
            """,
            (security_id, decision_date, next_decision_date),
        )
    return store.query(
        """
        SELECT ticker, security_id, date, close, adj_close, dividends, split_ratio,
               actions_complete, close_split_adjusted
        FROM prices
        WHERE ticker = ?
          AND date > ? AND date <= ?
        ORDER BY date
        """,
        (ticker, decision_date, next_decision_date),
    )


def benchmark_period_return(
    ticker: str,
    decision_date: date,
    next_decision_date: date,
    store: Store,
    *,
    scheduled_entry_date: date | None = None,
    scheduled_exit_date: date | None = None,
) -> tuple[float | None, date | None, date | None, str | None]:
    """Return a benchmark's adjusted-close total return for one interval."""
    entry_date = scheduled_entry_date or _common_price_date(
        (ticker,), decision_date, next_decision_date, store, ascending=True
    )
    exit_date = scheduled_exit_date or _common_price_date(
        (ticker,), decision_date, next_decision_date, store, ascending=False
    )
    if entry_date is None or exit_date is None:
        return None, entry_date, exit_date, "missing_benchmark_price"
    rows = store.query(
        """
        SELECT date, close, adj_close
        FROM prices
        WHERE ticker = ? AND date IN (?, ?)
        """,
        (ticker, entry_date, exit_date),
    )
    by_date = {row["date"]: row for row in rows}
    entry_value = _return_price(by_date.get(entry_date))
    exit_value = _return_price(by_date.get(exit_date))
    if entry_value is None or exit_value is None or entry_value <= 0:
        return None, entry_date, exit_date, "invalid_benchmark_price"
    return exit_value / entry_value - 1.0, entry_date, exit_date, None


def _common_price_date(
    tickers: tuple[str, ...],
    decision_date: date,
    next_decision_date: date,
    store: Store,
    *,
    ascending: bool,
) -> date | None:
    placeholders = ",".join("?" for _ in tickers)
    direction = "ASC" if ascending else "DESC"
    rows = store.query(
        f"""
        SELECT date
        FROM prices
        WHERE ticker IN ({placeholders})
          AND date > ? AND date <= ?
        GROUP BY date
        HAVING COUNT(DISTINCT ticker) = ?
        ORDER BY date {direction}
        LIMIT 1
        """,
        (*tickers, decision_date, next_decision_date, len(tickers)),
    )
    return rows[0]["date"] if rows else None


def _return_price(row: dict | None) -> float | None:
    if not row:
        return None
    value = row.get("adj_close")
    if value is None:
        value = row.get("close")
    return float(value) if value is not None else None


def _positive(row: dict | None, field: str) -> bool:
    if not row:
        return False
    value = row.get(field)
    return value is not None and float(value) > 0


def _positive_number(value: object, *, default: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _missing_result(missing: tuple[str, ...]) -> FrictionResult:
    return FrictionResult(
        entry_date=None,
        exit_date=None,
        gross_return=None,
        net_return=None,
        transaction_costs=0.0,
        commission=0.0,
        slippage=0.0,
        fixed_fees=0.0,
        taxes=0.0,
        short_term_tax=0.0,
        long_term_tax=0.0,
        dividend_tax=0.0,
        turnover=0.0,
        missing=missing,
    )

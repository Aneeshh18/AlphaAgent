"""Stateful portfolio accounting for point-in-time backtests.

The factor layer decides *what* should be held.  This module owns the separate
execution state: cash, immutable-security positions, FIFO tax lots, corporate
actions, transaction costs, and close-to-close equity observations.

The implementation is deliberately jurisdiction-neutral.  Tax rates are
supplied by the caller, gains and losses net inside their short/long buckets
over the full simulation, and the current accrued liability is reflected in
cash.  Wash sales, cross-bucket offsets, carryforwards, and filing calendars
remain outside this generic model.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import date
from math import isfinite
from typing import TYPE_CHECKING, Any

from aios.backtest.costs import TaxLedger, TaxLot, TaxPolicy, TransactionCostPolicy

if TYPE_CHECKING:
    from aios.storage.store import Store


_EPSILON = 1e-9


@dataclass(frozen=True)
class PortfolioTrade:
    """One deterministic close-price order in the execution audit."""

    date: date
    ticker: str
    security_id: str | None
    side: str
    quantity: float
    price: float
    notional: float
    commission: float
    slippage: float
    fixed_fee: float
    realized_gain: float = 0.0

    @property
    def transaction_cost(self) -> float:
        return self.commission + self.slippage + self.fixed_fee

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["date"] = self.date.isoformat()
        return result


@dataclass(frozen=True)
class PortfolioConversion:
    """One reviewed, non-trade share conversion applied to an open position."""

    date: date
    source_ticker: str
    target_ticker: str
    source_security_id: str
    target_security_id: str
    share_ratio: float
    source_quantity: float
    target_quantity: float
    basis_policy: str
    source: str
    basis_source: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["date"] = self.date.isoformat()
        return result


@dataclass(frozen=True)
class PortfolioEquityPoint:
    """One end-of-session portfolio valuation."""

    date: date
    equity: float
    cash: float
    gross_exposure: float
    position_count: int
    daily_return: float | None
    drawdown: float
    cumulative_transaction_costs: float
    accrued_taxes: float
    stale_tickers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["date"] = self.date.isoformat()
        result["stale_tickers"] = list(self.stale_tickers)
        return result


@dataclass(frozen=True)
class PortfolioPeriodResult:
    """State transition and accounting deltas for one rebalance interval."""

    entry_date: date | None
    exit_date: date | None
    starting_equity: float
    ending_equity: float | None
    period_return: float | None
    transaction_costs: float
    commission: float
    slippage: float
    fixed_fees: float
    taxes: float
    short_term_tax: float
    long_term_tax: float
    dividend_tax: float
    traded_notional: float
    turnover: float
    trades: tuple[PortfolioTrade, ...] = ()
    conversions: tuple[PortfolioConversion, ...] = ()
    ending_holdings: tuple[str, ...] = ()
    open_tax_lots: int = 0
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ResolvedSecurity:
    key: str
    ticker: str
    security_id: str | None


@dataclass(frozen=True)
class _ResolvedConversion:
    source_key: str
    target: _ResolvedSecurity
    effective_date: date
    share_ratio: float
    basis_policy: str
    source: str
    basis_source: str


@dataclass
class _Position:
    key: str
    ticker: str
    security_id: str | None
    lots: list[TaxLot] = field(default_factory=list)

    @property
    def quantity(self) -> float:
        return sum(lot.quantity for lot in self.lots)


class _PortfolioDataError(ValueError):
    def __init__(self, *missing: str) -> None:
        self.missing = tuple(sorted(set(missing)))
        super().__init__(", ".join(self.missing))


class PortfolioBook:
    """Mutable cash/position/lot state for one policy implementation.

    Rebalances happen at the supplied entry session's close.  Existing
    positions stay invested between the prior decision close and that entry
    close.  Target weights are equal-weight notional targets based on pre-trade
    equity.  Sells execute first; buys are proportionally scaled for costs so
    the book never finances fees with negative cash.
    """

    def __init__(
        self,
        store: Store,
        *,
        initial_capital: float,
        transaction_costs: TransactionCostPolicy,
        tax_policy: TaxPolicy,
        calendar_ticker: str | None,
    ) -> None:
        if initial_capital <= 0 or not isfinite(initial_capital):
            raise ValueError("initial_capital must be finite and positive")
        self.store = store
        self.initial_capital = float(initial_capital)
        self.transaction_cost_policy = transaction_costs
        self.tax_policy = tax_policy
        self.calendar_ticker = calendar_ticker
        self.cash = float(initial_capital)
        self.positions: dict[str, _Position] = {}
        self.last_prices: dict[str, float] = {}
        self.last_price_dates: dict[str, date] = {}
        self.tax_ledger = TaxLedger()
        self.short_term_tax = 0.0
        self.long_term_tax = 0.0
        self.dividend_tax = 0.0
        self.transaction_costs = 0.0
        self.commission = 0.0
        self.slippage = 0.0
        self.fixed_fees = 0.0
        self.conversions: list[PortfolioConversion] = []
        self.last_date: date | None = None
        self.peak_equity = float(initial_capital)
        self.curve: list[PortfolioEquityPoint] = []

    @property
    def accrued_taxes(self) -> float:
        return self.short_term_tax + self.long_term_tax + self.dividend_tax

    def clone(self) -> PortfolioBook:
        """Copy mutable accounting state while retaining the shared store."""
        clone = PortfolioBook(
            self.store,
            initial_capital=self.initial_capital,
            transaction_costs=self.transaction_cost_policy,
            tax_policy=self.tax_policy,
            calendar_ticker=self.calendar_ticker,
        )
        clone.cash = self.cash
        clone.positions = deepcopy(self.positions)
        clone.last_prices = dict(self.last_prices)
        clone.last_price_dates = dict(self.last_price_dates)
        clone.tax_ledger = deepcopy(self.tax_ledger)
        clone.short_term_tax = self.short_term_tax
        clone.long_term_tax = self.long_term_tax
        clone.dividend_tax = self.dividend_tax
        clone.transaction_costs = self.transaction_costs
        clone.commission = self.commission
        clone.slippage = self.slippage
        clone.fixed_fees = self.fixed_fees
        clone.conversions = list(self.conversions)
        clone.last_date = self.last_date
        clone.peak_equity = self.peak_equity
        clone.curve = list(self.curve)
        return clone

    @property
    def equity(self) -> float:
        """Current marked equity, or cash when the paper book is uninvested."""
        return self._equity()

    def current_weights(self) -> dict[str, float]:
        """Return current non-cash weights keyed by visible market ticker."""
        equity = self._equity()
        if equity <= 0:
            raise ValueError("portfolio equity must be positive")
        return {
            position.ticker: position.quantity * self.last_prices[key] / equity
            for key, position in self.positions.items()
        }

    def to_state(self) -> dict[str, Any]:
        """Serialize deterministic paper state without the shared Store handle."""
        return {
            "schema_version": 1,
            "initial_capital": self.initial_capital,
            "transaction_costs": self.transaction_cost_policy.to_dict(),
            "tax_policy": self.tax_policy.to_dict(),
            "calendar_ticker": self.calendar_ticker,
            "cash": self.cash,
            "positions": [
                {
                    "key": key,
                    "ticker": position.ticker,
                    "security_id": position.security_id,
                    "lots": [
                        {
                            "ticker": lot.ticker,
                            "quantity": lot.quantity,
                            "cost_basis_per_share": lot.cost_basis_per_share,
                            "acquired_date": lot.acquired_date.isoformat(),
                        }
                        for lot in position.lots
                    ],
                }
                for key, position in sorted(self.positions.items())
            ],
            "last_prices": dict(sorted(self.last_prices.items())),
            "last_price_dates": {
                key: value.isoformat() for key, value in sorted(self.last_price_dates.items())
            },
            "tax_ledger": asdict(self.tax_ledger),
            "short_term_tax": self.short_term_tax,
            "long_term_tax": self.long_term_tax,
            "dividend_tax": self.dividend_tax,
            "transaction_cost_total": self.transaction_costs,
            "commission_total": self.commission,
            "slippage_total": self.slippage,
            "fixed_fee_total": self.fixed_fees,
            "conversions": [conversion.to_dict() for conversion in self.conversions],
            "last_date": self.last_date.isoformat() if self.last_date else None,
            "peak_equity": self.peak_equity,
            "curve": [point.to_dict() for point in self.curve],
        }

    @classmethod
    def from_state(cls, store: Store, state: dict[str, Any]) -> PortfolioBook:
        """Restore validated paper state produced by :meth:`to_state`."""
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            raise ValueError("unsupported portfolio state schema")
        try:
            book = cls(
                store,
                initial_capital=float(state["initial_capital"]),
                transaction_costs=TransactionCostPolicy(**state["transaction_costs"]),
                tax_policy=TaxPolicy(**state["tax_policy"]),
                calendar_ticker=state.get("calendar_ticker"),
            )
            book.cash = float(state["cash"])
            positions: dict[str, _Position] = {}
            for raw_position in state.get("positions", []):
                key = str(raw_position["key"])
                if not key or key in positions:
                    raise ValueError("portfolio state has duplicate or blank position key")
                lots = [
                    TaxLot(
                        str(raw_lot["ticker"]).upper(),
                        float(raw_lot["quantity"]),
                        float(raw_lot["cost_basis_per_share"]),
                        date.fromisoformat(str(raw_lot["acquired_date"])),
                    )
                    for raw_lot in raw_position.get("lots", [])
                ]
                if not lots or any(
                    lot.quantity <= 0
                    or not isfinite(lot.quantity)
                    or lot.cost_basis_per_share <= 0
                    or not isfinite(lot.cost_basis_per_share)
                    for lot in lots
                ):
                    raise ValueError("portfolio state has an invalid tax lot")
                positions[key] = _Position(
                    key=key,
                    ticker=str(raw_position["ticker"]).upper(),
                    security_id=raw_position.get("security_id"),
                    lots=lots,
                )
            book.positions = positions
            book.last_prices = {
                str(key): float(value) for key, value in state.get("last_prices", {}).items()
            }
            book.last_price_dates = {
                str(key): date.fromisoformat(str(value))
                for key, value in state.get("last_price_dates", {}).items()
            }
            if set(book.positions) - set(book.last_prices):
                raise ValueError("portfolio state is missing a position mark price")
            if any(value <= 0 or not isfinite(value) for value in book.last_prices.values()):
                raise ValueError("portfolio state has an invalid mark price")
            ledger = state.get("tax_ledger", {})
            book.tax_ledger = TaxLedger(
                short_term_gains=float(ledger.get("short_term_gains", 0.0)),
                long_term_gains=float(ledger.get("long_term_gains", 0.0)),
                dividend_income=float(ledger.get("dividend_income", 0.0)),
            )
            book.short_term_tax = float(state.get("short_term_tax", 0.0))
            book.long_term_tax = float(state.get("long_term_tax", 0.0))
            book.dividend_tax = float(state.get("dividend_tax", 0.0))
            book.transaction_costs = float(state.get("transaction_cost_total", 0.0))
            book.commission = float(state.get("commission_total", 0.0))
            book.slippage = float(state.get("slippage_total", 0.0))
            book.fixed_fees = float(state.get("fixed_fee_total", 0.0))
            book.conversions = [
                PortfolioConversion(
                    date=date.fromisoformat(str(raw["date"])),
                    source_ticker=str(raw["source_ticker"]).upper(),
                    target_ticker=str(raw["target_ticker"]).upper(),
                    source_security_id=str(raw["source_security_id"]),
                    target_security_id=str(raw["target_security_id"]),
                    share_ratio=float(raw["share_ratio"]),
                    source_quantity=float(raw["source_quantity"]),
                    target_quantity=float(raw["target_quantity"]),
                    basis_policy=str(raw["basis_policy"]),
                    source=str(raw["source"]),
                    basis_source=str(raw["basis_source"]),
                )
                for raw in state.get("conversions", [])
            ]
            book.last_date = (
                date.fromisoformat(str(state["last_date"])) if state.get("last_date") else None
            )
            book.peak_equity = float(state["peak_equity"])
            book.curve = [
                PortfolioEquityPoint(
                    date=date.fromisoformat(str(raw["date"])),
                    equity=float(raw["equity"]),
                    cash=float(raw["cash"]),
                    gross_exposure=float(raw["gross_exposure"]),
                    position_count=int(raw["position_count"]),
                    daily_return=(
                        float(raw["daily_return"])
                        if raw.get("daily_return") is not None
                        else None
                    ),
                    drawdown=float(raw["drawdown"]),
                    cumulative_transaction_costs=float(
                        raw["cumulative_transaction_costs"]
                    ),
                    accrued_taxes=float(raw["accrued_taxes"]),
                    stale_tickers=tuple(str(value) for value in raw.get("stale_tickers", [])),
                )
                for raw in state.get("curve", [])
            ]
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc).startswith("portfolio state"):
                raise
            raise ValueError("invalid portfolio state payload") from exc
        if book.curve and book.last_date != book.curve[-1].date:
            raise ValueError("portfolio state date disagrees with its equity curve")
        if not isfinite(book.cash) or not isfinite(book.peak_equity) or book.peak_equity <= 0:
            raise ValueError("portfolio state has invalid capital values")
        return book

    def mark_through(self, end_date: date) -> tuple[PortfolioEquityPoint, ...]:
        """Advance an existing paper book without rebalancing."""
        if self.last_date is None:
            raise ValueError("paper book has no starting valuation date")
        if end_date < self.last_date:
            raise ValueError("paper mark date cannot move backward")
        if end_date == self.last_date:
            return ()
        snapshot = self.clone()
        initial_curve_length = len(self.curve)
        try:
            securities = tuple(
                _ResolvedSecurity(key, position.ticker, position.security_id)
                for key, position in self.positions.items()
            )
            securities, conversions = self._expand_security_conversions(
                securities,
                self.last_date,
                end_date,
            )
            labels = {security.key: security.ticker for security in securities}
            paths = self._load_paths(securities, self.last_date, end_date)
            calendar_dates = self._calendar_dates(self.last_date, end_date)
            if not calendar_dates or calendar_dates[-1] != end_date:
                raise _PortfolioDataError(f"missing_calendar_mark:{end_date}")
            for key, dated_rows in paths.items():
                ticker = labels.get(key, key)
                for observation_date, row in dated_rows.items():
                    if row.get("actions_complete") is not True:
                        raise _PortfolioDataError(
                            f"{ticker}:unverified_corporate_actions:{observation_date}"
                        )
                    if row.get("close_split_adjusted") is None:
                        raise _PortfolioDataError(f"{ticker}:split_adjustment_basis_unknown")
            for session_date in calendar_dates:
                rows_for_date = {
                    key: rows[session_date]
                    for key, rows in paths.items()
                    if session_date in rows
                }
                self._apply_conversions(session_date, conversions, rows_for_date)
                self._apply_session_rows(session_date, rows_for_date)
                self._record_equity(session_date)
            return tuple(self.curve[initial_curve_length:])
        except Exception:
            self._restore(snapshot)
            raise

    def advance_period(
        self,
        target_tickers: tuple[str, ...],
        decision_date: date,
        entry_date: date,
        exit_date: date,
    ) -> PortfolioPeriodResult:
        """Carry state through one interval as an atomic book transition."""
        snapshot = self.clone()
        starting_equity = self.curve[-1].equity if self.curve else self.initial_capital
        try:
            return self._advance_period(
                target_tickers,
                decision_date,
                entry_date,
                exit_date,
            )
        except _PortfolioDataError as exc:
            self._restore(snapshot)
            return self._missing_result(starting_equity, *exc.missing)

    def _advance_period(
        self,
        target_tickers: tuple[str, ...],
        decision_date: date,
        entry_date: date,
        exit_date: date,
    ) -> PortfolioPeriodResult:
        starting_equity = self._starting_equity(decision_date)
        if not target_tickers:
            raise _PortfolioDataError("empty_portfolio")
        if not decision_date < entry_date <= exit_date:
            raise _PortfolioDataError("invalid_execution_window")
        if self.last_date is not None and self.last_date != decision_date:
            raise _PortfolioDataError(
                f"portfolio_state_discontinuity:{self.last_date}:{decision_date}",
            )

        targets = self._resolve_targets(target_tickers, decision_date)
        securities = {target.key: target for target in targets.values()}
        securities.update(
            {
                key: _ResolvedSecurity(key, position.ticker, position.security_id)
                for key, position in self.positions.items()
            }
        )
        resolved_securities, conversions = self._expand_security_conversions(
            tuple(securities.values()),
            decision_date,
            exit_date,
        )
        paths = self._load_paths(resolved_securities, decision_date, exit_date)
        calendar_dates = self._calendar_dates(decision_date, exit_date)
        missing = self._preflight(
            targets,
            paths,
            calendar_dates,
            entry_date,
            exit_date,
            conversions,
        )
        if missing:
            raise _PortfolioDataError(*missing)

        before_costs = self.transaction_costs
        before_commission = self.commission
        before_slippage = self.slippage
        before_fixed = self.fixed_fees
        before_short_tax = self.short_term_tax
        before_long_tax = self.long_term_tax
        before_dividend_tax = self.dividend_tax
        before_conversions = len(self.conversions)
        trades: list[PortfolioTrade] = []
        for session_date in calendar_dates:
            rows_for_date = {
                key: rows[session_date] for key, rows in paths.items() if session_date in rows
            }
            self._apply_conversions(session_date, conversions, rows_for_date)
            self._apply_session_rows(session_date, rows_for_date)
            if session_date == entry_date:
                trades.extend(self._rebalance(targets, rows_for_date, entry_date))
            self._record_equity(session_date)

        ending_equity = self.curve[-1].equity
        traded_notional = sum(trade.notional for trade in trades)
        return PortfolioPeriodResult(
            entry_date=entry_date,
            exit_date=exit_date,
            starting_equity=starting_equity,
            ending_equity=ending_equity,
            period_return=ending_equity / starting_equity - 1.0,
            transaction_costs=self.transaction_costs - before_costs,
            commission=self.commission - before_commission,
            slippage=self.slippage - before_slippage,
            fixed_fees=self.fixed_fees - before_fixed,
            taxes=self.accrued_taxes - (before_short_tax + before_long_tax + before_dividend_tax),
            short_term_tax=self.short_term_tax - before_short_tax,
            long_term_tax=self.long_term_tax - before_long_tax,
            dividend_tax=self.dividend_tax - before_dividend_tax,
            traded_notional=traded_notional,
            turnover=traded_notional / starting_equity,
            trades=tuple(trades),
            conversions=tuple(self.conversions[before_conversions:]),
            ending_holdings=tuple(sorted(position.ticker for position in self.positions.values())),
            open_tax_lots=sum(len(position.lots) for position in self.positions.values()),
        )

    def _restore(self, snapshot: PortfolioBook) -> None:
        """Restore all mutable state after a rejected candidate transition."""
        self.cash = snapshot.cash
        self.positions = deepcopy(snapshot.positions)
        self.last_prices = dict(snapshot.last_prices)
        self.last_price_dates = dict(snapshot.last_price_dates)
        self.tax_ledger = deepcopy(snapshot.tax_ledger)
        self.short_term_tax = snapshot.short_term_tax
        self.long_term_tax = snapshot.long_term_tax
        self.dividend_tax = snapshot.dividend_tax
        self.transaction_costs = snapshot.transaction_costs
        self.commission = snapshot.commission
        self.slippage = snapshot.slippage
        self.fixed_fees = snapshot.fixed_fees
        self.conversions = list(snapshot.conversions)
        self.last_date = snapshot.last_date
        self.peak_equity = snapshot.peak_equity
        self.curve = list(snapshot.curve)

    def _starting_equity(self, decision_date: date) -> float:
        if self.last_date is None:
            self.last_date = decision_date
            self._record_equity(decision_date)
        return self.curve[-1].equity

    def _resolve_targets(
        self,
        tickers: tuple[str, ...],
        decision_date: date,
    ) -> dict[str, _ResolvedSecurity]:
        targets: dict[str, _ResolvedSecurity] = {}
        for ticker in tickers:
            normalized = ticker.upper()
            security_id = self.store.security_id_for_ticker(normalized, decision_date)
            key = security_id or f"ticker:{normalized}"
            if key in targets:
                raise _PortfolioDataError(f"duplicate_target_security:{normalized}")
            targets[key] = _ResolvedSecurity(key, normalized, security_id)
        return targets

    def _expand_security_conversions(
        self,
        securities: tuple[_ResolvedSecurity, ...],
        start: date,
        end: date,
    ) -> tuple[tuple[_ResolvedSecurity, ...], tuple[_ResolvedConversion, ...]]:
        """Add reviewed successor paths needed by mid-period conversions."""
        by_key = {security.key: security for security in securities}
        frontier = {
            security.security_id
            for security in securities
            if security.security_id is not None
        }
        resolved: list[_ResolvedConversion] = []
        visited: set[str] = set()
        while frontier:
            events = self.store.security_conversions_between(frontier, start, end)
            frontier = set()
            for row in events:
                source_security_id = str(row["source_security_id"])
                if source_security_id in visited:
                    continue
                visited.add(source_security_id)
                if source_security_id not in by_key:
                    raise _PortfolioDataError(
                        f"conversion_source_not_loaded:{source_security_id}"
                    )
                effective_date = row["effective_date"]
                target_security_id = str(row["target_security_id"])
                target_ticker = self.store.ticker_for_security_id(
                    target_security_id,
                    effective_date,
                )
                if target_ticker is None:
                    raise _PortfolioDataError(
                        f"conversion_target_ticker_missing:{target_security_id}:{effective_date}"
                    )
                target = _ResolvedSecurity(
                    target_security_id,
                    str(target_ticker).upper(),
                    target_security_id,
                )
                by_key.setdefault(target.key, target)
                resolved.append(
                    _ResolvedConversion(
                        source_key=source_security_id,
                        target=target,
                        effective_date=effective_date,
                        share_ratio=float(row["share_ratio"]),
                        basis_policy=str(row["basis_policy"]),
                        source=str(row["source"]),
                        basis_source=str(row["basis_source"]),
                    )
                )
                if target_security_id not in visited:
                    frontier.add(target_security_id)
        resolved.sort(key=lambda event: (event.effective_date, event.source_key))
        return tuple(by_key.values()), tuple(resolved)

    def _load_paths(
        self,
        securities: tuple[_ResolvedSecurity, ...],
        start: date,
        end: date,
    ) -> dict[str, dict[date, dict[str, Any]]]:
        paths: dict[str, dict[date, dict[str, Any]]] = {}
        for security in securities:
            if security.security_id is not None:
                rows = self.store.query(
                    """
                    SELECT ticker, security_id, date, close, dividends, split_ratio,
                           actions_complete, close_split_adjusted
                    FROM prices
                    WHERE security_id = ? AND date > ? AND date <= ?
                    ORDER BY date, ticker
                    """,
                    (security.security_id, start, end),
                )
            else:
                rows = self.store.query(
                    """
                    SELECT ticker, security_id, date, close, dividends, split_ratio,
                           actions_complete, close_split_adjusted
                    FROM prices
                    WHERE ticker = ? AND date > ? AND date <= ?
                    ORDER BY date
                    """,
                    (security.ticker, start, end),
                )
            by_date: dict[date, dict[str, Any]] = {}
            for row in rows:
                observation_date = row["date"]
                if observation_date in by_date:
                    raise _PortfolioDataError(
                        f"ambiguous_security_price:{security.ticker}:{observation_date}"
                    )
                by_date[observation_date] = row
            paths[security.key] = by_date
        return paths

    def _calendar_dates(self, start: date, end: date) -> list[date]:
        if self.calendar_ticker is None:
            rows = self.store.query(
                """
                SELECT DISTINCT date
                FROM prices
                WHERE date > ? AND date <= ?
                ORDER BY date
                """,
                (start, end),
            )
        else:
            rows = self.store.query(
                """
                SELECT date
                FROM prices
                WHERE ticker = ? AND date > ? AND date <= ?
                ORDER BY date
                """,
                (self.calendar_ticker, start, end),
            )
        return [row["date"] for row in rows]

    def _preflight(
        self,
        targets: dict[str, _ResolvedSecurity],
        paths: dict[str, dict[date, dict[str, Any]]],
        calendar_dates: list[date],
        entry_date: date,
        exit_date: date,
        conversions: tuple[_ResolvedConversion, ...],
    ) -> tuple[str, ...]:
        missing: list[str] = []
        calendar_set = set(calendar_dates)
        if entry_date not in calendar_set:
            missing.append(f"missing_calendar_entry:{entry_date}")
        if exit_date not in calendar_set:
            missing.append(f"missing_calendar_exit:{exit_date}")
        target_keys = set(targets)
        conversions_by_source = {event.source_key: event for event in conversions}
        labels = {
            **{key: target.ticker for key, target in targets.items()},
            **{key: position.ticker for key, position in self.positions.items()},
            **{event.target.key: event.target.ticker for event in conversions},
        }
        for event in conversions:
            if event.source_key in target_keys and event.effective_date <= entry_date:
                missing.append(
                    "target_converts_on_or_before_entry:"
                    f"{targets[event.source_key].ticker}:{event.effective_date}"
                )
            if event.effective_date not in calendar_set:
                missing.append(
                    f"conversion_off_calendar:{event.source_key}:{event.effective_date}"
                )
            if not self._valid_close(
                paths.get(event.target.key, {}).get(event.effective_date)
            ):
                missing.append(
                    "conversion_target_price_missing:"
                    f"{event.target.ticker}:{event.effective_date}"
                )

        required_at_entry = self._keys_after_conversions(
            set(targets) | set(self.positions),
            conversions_by_source,
            entry_date,
        )
        for key in required_at_entry:
            ticker = labels.get(key, key)
            if not self._valid_close(paths.get(key, {}).get(entry_date)):
                missing.append(f"{ticker}:missing_scheduled_entry_price:{entry_date}")
        required_at_exit = self._keys_after_conversions(
            set(targets),
            conversions_by_source,
            exit_date,
        )
        for key in required_at_exit:
            ticker = labels.get(key, key)
            if not self._valid_close(paths.get(key, {}).get(exit_date)):
                missing.append(f"{ticker}:missing_scheduled_exit_price:{exit_date}")
        for key, dated_rows in paths.items():
            ticker = labels.get(key, key)
            split_bases = {row.get("close_split_adjusted") for row in dated_rows.values()}
            if None in split_bases:
                missing.append(f"{ticker}:split_adjustment_basis_unknown")
            if len(split_bases) > 1:
                missing.append(f"{ticker}:mixed_split_adjustment_basis")
            for observation_date, row in dated_rows.items():
                if row.get("actions_complete") is not True:
                    missing.append(f"{ticker}:unverified_corporate_actions:{observation_date}")
        return tuple(sorted(set(missing)))

    @staticmethod
    def _keys_after_conversions(
        keys: set[str],
        conversions: dict[str, _ResolvedConversion],
        through: date,
    ) -> set[str]:
        output = set(keys)
        changed = True
        while changed:
            changed = False
            for source_key in list(output):
                event = conversions.get(source_key)
                if event is None or event.effective_date > through:
                    continue
                output.remove(source_key)
                output.add(event.target.key)
                changed = True
        return output

    def _security_label(
        self,
        key: str,
        targets: dict[str, _ResolvedSecurity],
    ) -> str:
        target = targets.get(key)
        if target is not None:
            return target.ticker
        return self.positions[key].ticker

    @staticmethod
    def _valid_close(row: dict[str, Any] | None) -> bool:
        if row is None or row.get("close") is None:
            return False
        value = float(row["close"])
        return isfinite(value) and value > 0

    def _apply_conversions(
        self,
        session_date: date,
        conversions: tuple[_ResolvedConversion, ...],
        rows: dict[str, dict[str, Any]],
    ) -> None:
        """Apply reviewed share conversions before the effective session mark."""
        for event in conversions:
            if event.effective_date != session_date or event.source_key not in self.positions:
                continue
            if event.basis_policy != "carryover":
                raise _PortfolioDataError(
                    f"unsupported_conversion_basis:{event.source_key}:{event.basis_policy}"
                )
            target_row = rows.get(event.target.key)
            if not self._valid_close(target_row):
                raise _PortfolioDataError(
                    f"conversion_target_price_missing:{event.target.ticker}:{session_date}"
                )
            source_position = self.positions.pop(event.source_key)
            source_quantity = source_position.quantity
            converted_lots = [
                TaxLot(
                    event.target.ticker,
                    lot.quantity * event.share_ratio,
                    lot.cost_basis_per_share / event.share_ratio,
                    lot.acquired_date,
                )
                for lot in source_position.lots
            ]
            target_position = self.positions.get(event.target.key)
            if target_position is None:
                target_position = _Position(
                    key=event.target.key,
                    ticker=event.target.ticker,
                    security_id=event.target.security_id,
                )
                self.positions[event.target.key] = target_position
            target_position.ticker = event.target.ticker
            target_position.lots.extend(converted_lots)
            self.last_prices.pop(event.source_key, None)
            self.last_price_dates.pop(event.source_key, None)
            target_quantity = source_quantity * event.share_ratio
            self.conversions.append(
                PortfolioConversion(
                    date=session_date,
                    source_ticker=source_position.ticker,
                    target_ticker=event.target.ticker,
                    source_security_id=event.source_key,
                    target_security_id=event.target.key,
                    share_ratio=event.share_ratio,
                    source_quantity=source_quantity,
                    target_quantity=target_quantity,
                    basis_policy=event.basis_policy,
                    source=event.source,
                    basis_source=event.basis_source,
                )
            )

    def _apply_session_rows(
        self,
        session_date: date,
        rows: dict[str, dict[str, Any]],
    ) -> None:
        for key, position in list(self.positions.items()):
            row = rows.get(key)
            if row is None:
                continue
            split_ratio = self._event_number(row.get("split_ratio"), default=1.0)
            if split_ratio != 1.0 and row.get("close_split_adjusted") is not True:
                position.lots = [
                    TaxLot(
                        lot.ticker,
                        lot.quantity * split_ratio,
                        lot.cost_basis_per_share / split_ratio,
                        lot.acquired_date,
                    )
                    for lot in position.lots
                ]
            dividend = self._event_number(row.get("dividends"), default=0.0)
            if dividend:
                income = position.quantity * dividend
                self.cash += income
                self.tax_ledger.record_dividends(income)
                self._sync_tax_liability()
            position.ticker = str(row["ticker"]).upper()
        for key, row in rows.items():
            if not self._valid_close(row):
                continue
            self.last_prices[key] = float(row["close"])
            self.last_price_dates[key] = session_date

    @staticmethod
    def _event_number(value: object, *, default: float) -> float:
        if value is None:
            return default
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise _PortfolioDataError("invalid_corporate_action_value") from exc
        if not isfinite(parsed) or parsed < 0:
            raise _PortfolioDataError("invalid_corporate_action_value")
        if default == 1.0 and parsed == 0:
            return default
        return parsed

    def _rebalance(
        self,
        targets: dict[str, _ResolvedSecurity],
        rows: dict[str, dict[str, Any]],
        trade_date: date,
    ) -> list[PortfolioTrade]:
        for key in set(targets) | set(self.positions):
            if not self._valid_close(rows.get(key)):
                ticker = self._security_label(key, targets)
                raise _PortfolioDataError(f"{ticker}:invalid_execution_price:{trade_date}")

        pre_trade_equity = self._equity()
        if pre_trade_equity <= 0:
            raise _PortfolioDataError("non_positive_portfolio_equity")
        target_value = pre_trade_equity / len(targets)
        current_values = {
            key: position.quantity * float(rows[key]["close"])
            for key, position in self.positions.items()
        }
        sells = {
            key: value - (target_value if key in targets else 0.0)
            for key, value in current_values.items()
            if value - (target_value if key in targets else 0.0) > pre_trade_equity * 1e-12
        }
        buys = {
            key: target_value - current_values.get(key, 0.0)
            for key in targets
            if target_value - current_values.get(key, 0.0) > pre_trade_equity * 1e-12
        }

        trades: list[PortfolioTrade] = []
        for key in sorted(sells):
            trades.append(self._sell(key, sells[key], rows[key], trade_date))

        if buys:
            variable_rate = (
                self.transaction_cost_policy.commission_bps
                + self.transaction_cost_policy.slippage_bps
            ) / 10_000.0
            fixed_total = len(buys) * self.transaction_cost_policy.fixed_fee
            if self.cash + _EPSILON < fixed_total:
                raise _PortfolioDataError("insufficient_cash_for_fixed_fees")
            proposed = sum(buys.values())
            affordable = max((self.cash - fixed_total) / (1.0 + variable_rate), 0.0)
            scale = min(1.0, affordable / proposed) if proposed else 0.0
            if scale <= 0:
                raise _PortfolioDataError("insufficient_cash_for_target_buys")
            for key in sorted(buys):
                trades.append(
                    self._buy(
                        targets[key],
                        buys[key] * scale,
                        rows[key],
                        trade_date,
                    )
                )
        if self.cash < -_EPSILON:
            raise _PortfolioDataError("negative_cash_after_rebalance")
        if abs(self.cash) <= _EPSILON:
            self.cash = 0.0
        return trades

    def _sell(
        self,
        key: str,
        requested_notional: float,
        row: dict[str, Any],
        trade_date: date,
    ) -> PortfolioTrade:
        position = self.positions[key]
        price = float(row["close"])
        quantity = min(requested_notional / price, position.quantity)
        if position.quantity - quantity <= _EPSILON:
            quantity = position.quantity
        notional = quantity * price
        commission, slippage, fixed_fee = self.transaction_cost_policy.estimate(notional)
        costs = commission + slippage + fixed_fee
        if costs > notional + _EPSILON:
            raise _PortfolioDataError(f"trade_cost_exceeds_sale_proceeds:{position.ticker}")
        net_sale_price = (notional - costs) / quantity
        remaining = quantity
        realized_gain = 0.0
        retained: list[TaxLot] = []
        for lot in position.lots:
            sold = min(remaining, lot.quantity)
            if sold > _EPSILON:
                realized_gain += self.tax_ledger.realize(
                    lot,
                    sold,
                    net_sale_price,
                    trade_date,
                    long_term_days=self.tax_policy.long_term_days,
                )
                remaining -= sold
            leftover = lot.quantity - sold
            if leftover > _EPSILON:
                retained.append(
                    TaxLot(
                        lot.ticker,
                        leftover,
                        lot.cost_basis_per_share,
                        lot.acquired_date,
                    )
                )
        if remaining > _EPSILON:
            raise _PortfolioDataError(f"tax_lot_quantity_mismatch:{position.ticker}")
        position.lots = retained
        self.cash += notional - costs
        self._record_costs(commission, slippage, fixed_fee)
        self._sync_tax_liability()
        if not position.lots:
            del self.positions[key]
            self.last_prices.pop(key, None)
            self.last_price_dates.pop(key, None)
        return PortfolioTrade(
            date=trade_date,
            ticker=str(row["ticker"]).upper(),
            security_id=position.security_id,
            side="sell",
            quantity=quantity,
            price=price,
            notional=notional,
            commission=commission,
            slippage=slippage,
            fixed_fee=fixed_fee,
            realized_gain=realized_gain,
        )

    def _buy(
        self,
        security: _ResolvedSecurity,
        notional: float,
        row: dict[str, Any],
        trade_date: date,
    ) -> PortfolioTrade:
        price = float(row["close"])
        commission, slippage, fixed_fee = self.transaction_cost_policy.estimate(notional)
        costs = commission + slippage + fixed_fee
        total_cash = notional + costs
        if total_cash > self.cash + _EPSILON:
            raise _PortfolioDataError(f"insufficient_cash_for_buy:{security.ticker}")
        quantity = notional / price
        if quantity <= 0:
            raise _PortfolioDataError(f"invalid_buy_quantity:{security.ticker}")
        basis_per_share = total_cash / quantity
        position = self.positions.get(security.key)
        if position is None:
            position = _Position(
                key=security.key,
                ticker=str(row["ticker"]).upper(),
                security_id=security.security_id,
            )
            self.positions[security.key] = position
        position.ticker = str(row["ticker"]).upper()
        position.lots.append(TaxLot(position.ticker, quantity, basis_per_share, trade_date))
        self.cash -= total_cash
        self.last_prices[security.key] = price
        self.last_price_dates[security.key] = trade_date
        self._record_costs(commission, slippage, fixed_fee)
        return PortfolioTrade(
            date=trade_date,
            ticker=position.ticker,
            security_id=security.security_id,
            side="buy",
            quantity=quantity,
            price=price,
            notional=notional,
            commission=commission,
            slippage=slippage,
            fixed_fee=fixed_fee,
        )

    def _record_costs(self, commission: float, slippage: float, fixed_fee: float) -> None:
        self.commission += commission
        self.slippage += slippage
        self.fixed_fees += fixed_fee
        self.transaction_costs += commission + slippage + fixed_fee

    def _sync_tax_liability(self) -> None:
        short_tax, long_tax, dividend_tax, total = self.tax_ledger.taxes_due(self.tax_policy)
        delta = total - self.accrued_taxes
        self.cash -= delta
        self.short_term_tax = short_tax
        self.long_term_tax = long_tax
        self.dividend_tax = dividend_tax

    def _equity(self) -> float:
        exposure = 0.0
        for key, position in self.positions.items():
            price = self.last_prices.get(key)
            if price is None:
                raise _PortfolioDataError(f"missing_mark_price:{position.ticker}")
            exposure += position.quantity * price
        return self.cash + exposure

    def _record_equity(self, observation_date: date) -> None:
        if self.curve and self.curve[-1].date == observation_date:
            return
        exposure = 0.0
        stale: list[str] = []
        for key, position in self.positions.items():
            price = self.last_prices.get(key)
            if price is None:
                raise _PortfolioDataError(f"missing_mark_price:{position.ticker}")
            exposure += position.quantity * price
            if self.last_price_dates.get(key) != observation_date:
                stale.append(position.ticker)
        equity = self.cash + exposure
        previous = self.curve[-1].equity if self.curve else None
        daily_return = equity / previous - 1.0 if previous and previous > 0 else None
        self.peak_equity = max(self.peak_equity, equity)
        drawdown = equity / self.peak_equity - 1.0
        self.curve.append(
            PortfolioEquityPoint(
                date=observation_date,
                equity=equity,
                cash=self.cash,
                gross_exposure=exposure,
                position_count=len(self.positions),
                daily_return=daily_return,
                drawdown=drawdown,
                cumulative_transaction_costs=self.transaction_costs,
                accrued_taxes=self.accrued_taxes,
                stale_tickers=tuple(sorted(stale)),
            )
        )
        self.last_date = observation_date

    def _missing_result(
        self,
        starting_equity: float,
        *missing: str,
    ) -> PortfolioPeriodResult:
        return PortfolioPeriodResult(
            entry_date=None,
            exit_date=None,
            starting_equity=starting_equity,
            ending_equity=None,
            period_return=None,
            transaction_costs=0.0,
            commission=0.0,
            slippage=0.0,
            fixed_fees=0.0,
            taxes=0.0,
            short_term_tax=0.0,
            long_term_tax=0.0,
            dividend_tax=0.0,
            traded_notional=0.0,
            turnover=0.0,
            missing=tuple(sorted(set(missing))),
        )

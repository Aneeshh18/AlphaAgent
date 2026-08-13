"""PIT-safe QV policy backtest with stateful portfolio accounting.

Quarterly decisions select equal-weight top-N holdings, entries use the first
market session strictly after the decision, and positions persist until later
rebalance deltas sell them. The engine keeps the important research assumptions
explicit:

* historical membership is required unless the caller opts into the current
  active securities table;
* transaction costs are charged only on traded rebalance notional;
* taxes use split-aware FIFO lots that persist across quarters; and
* benchmarks are explicit price-series inputs and are never silently replaced.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date
from hashlib import sha256
from math import sqrt
from typing import Any

from aios.backtest.costs import (
    TaxPolicy,
    TransactionCostPolicy,
    simulate_period,
)
from aios.backtest.portfolio import (
    PortfolioBook,
    PortfolioConversion,
    PortfolioEquityPoint,
    PortfolioPeriodResult,
    PortfolioTrade,
)
from aios.factors.composite import CompositeRow, compute_composite
from aios.factors.policy import (
    BASELINE_FACTOR_WEIGHTS,
    FactorWeights,
    QVMLFactorWeights,
    qvml_weights_for_regime,
    weights_for_regime,
)
from aios.macro.regime import MacroRegimeSnapshot, compute_regime
from aios.storage.store import Store, get_store

# Below this many completed rebalances, no return gap is interpretable — the
# quantity being measured is dominated by which handful of names happened to
# be selected. Set at 20 because a quarterly strategy needs roughly five
# years before period count alone stops being the binding constraint, and
# this repository's own history shows two short windows reversing the
# ranking of two strategies against the same benchmark.
MINIMUM_PERIODS_FOR_ANY_INFERENCE = 20


@dataclass(frozen=True)
class QVBacktestConfig:
    """Configuration for the QV policy-validation backtest."""

    start: str | date
    end: str | date
    top_n: int = 10
    rebalance_frequency: str = "quarterly"
    require_pit_regime: bool = True
    universe_id: str | None = None
    allow_current_universe: bool = False
    initial_capital: float = 100_000.0
    transaction_costs: TransactionCostPolicy = field(default_factory=TransactionCostPolicy.zero)
    tax_policy: TaxPolicy = field(default_factory=TaxPolicy.zero)
    benchmark_tickers: tuple[str, ...] = ()
    calendar_ticker: str | None = None
    excluded_tickers: tuple[str, ...] = ()
    factor_model: str = "qv"

    def __post_init__(self) -> None:
        start = _parse_date(self.start)
        end = _parse_date(self.end)
        if start >= end:
            raise ValueError("backtest start must be before end")
        if self.top_n < 1:
            raise ValueError("top_n must be at least 1")
        if self.rebalance_frequency != "quarterly":
            raise ValueError("only quarterly rebalancing is supported in this phase")
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        factor_model = self.factor_model.strip().lower()
        if factor_model not in {"qv", "qvml"}:
            raise ValueError("factor_model must be 'qv' or 'qvml'")
        object.__setattr__(self, "factor_model", factor_model)
        object.__setattr__(self, "start", start.isoformat())
        object.__setattr__(self, "end", end.isoformat())
        if self.universe_id is not None:
            universe_id = self.universe_id.strip()
            object.__setattr__(self, "universe_id", universe_id or None)
        normalized_benchmarks = tuple(
            sorted({ticker.strip().upper() for ticker in self.benchmark_tickers if ticker.strip()})
        )
        object.__setattr__(self, "benchmark_tickers", normalized_benchmarks)
        object.__setattr__(
            self,
            "excluded_tickers",
            tuple(
                sorted(
                    {ticker.strip().upper() for ticker in self.excluded_tickers if ticker.strip()}
                )
            ),
        )
        calendar_ticker = self.calendar_ticker.strip().upper() if self.calendar_ticker else None
        if calendar_ticker is None and normalized_benchmarks:
            calendar_ticker = normalized_benchmarks[0]
        object.__setattr__(self, "calendar_ticker", calendar_ticker)


@dataclass(frozen=True)
class FactorAuditRow:
    """One member's raw-input and factor-publication status at a decision."""

    ticker: str
    security_id: str | None
    eligible: bool
    has_price_history: bool | None
    has_pit_fundamentals: bool | None
    latest_price_date: str | None
    latest_fundamental_date: str | None
    quality_score: float | None
    value_score: float | None
    momentum_score: float | None
    low_volatility_score: float | None
    qv_score: float | None
    qvml_score: float | None
    quality_components_available: int
    value_multiples_available: int
    market_price_observations: int
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["reasons"] = list(self.reasons)
        return result


@dataclass(frozen=True)
class SelectionAuditRow:
    """Deterministic score evidence for one selected holding."""

    ticker: str
    quality_score: float
    value_score: float
    policy_score: float
    rank: int
    target_weight: float
    momentum_score: float | None = None
    low_volatility_score: float | None = None
    factor_model: str = "qv"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BenchmarkPeriod:
    """One benchmark observation aligned to a completed strategy period."""

    ticker: str
    decision_date: str
    next_decision_date: str
    entry_date: str | None
    exit_date: str | None
    period_return: float | None
    status: str
    warning: str | None = None
    price_basis: str = "provider_close_with_basis_aware_splits_and_dividends"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EquityCurvePoint:
    """Aligned net/gross end-of-session valuation for one policy."""

    date: str
    net_equity: float
    gross_equity: float
    net_cash: float
    gross_cash: float
    position_count: int
    net_daily_return: float | None
    gross_daily_return: float | None
    drawdown: float
    cumulative_transaction_costs: float
    accrued_taxes: float
    stale_tickers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["stale_tickers"] = list(self.stale_tickers)
        return result


@dataclass(frozen=True)
class BacktestPeriod:
    """One decision-to-decision interval and its audit evidence."""

    decision_date: str
    next_decision_date: str
    entry_date: str | None
    exit_date: str | None
    regime: str
    factor_model: str
    quality_weight: float
    value_weight: float
    momentum_weight: float
    low_volatility_weight: float
    eligible_tickers: int
    member_tickers: tuple[str, ...] = ()
    member_list_sha256: str = ""
    price_covered_tickers: int | None = None
    pit_fundamental_covered_tickers: int | None = None
    raw_complete_tickers: int | None = None
    quality_scored_tickers: int = 0
    value_scored_tickers: int = 0
    momentum_scored_tickers: int = 0
    low_volatility_scored_tickers: int = 0
    qvml_scored_tickers: int = 0
    factor_audit: tuple[FactorAuditRow, ...] = ()
    macro_snapshot: dict[str, Any] = field(default_factory=dict)
    regime_selected: tuple[str, ...] = ()
    baseline_selected: tuple[str, ...] = ()
    regime_selection_audit: tuple[SelectionAuditRow, ...] = ()
    baseline_selection_audit: tuple[SelectionAuditRow, ...] = ()
    regime_return: float | None = None
    baseline_return: float | None = None
    status: str = "skipped"
    missing: tuple[str, ...] = ()
    regime_gross_return: float | None = None
    baseline_gross_return: float | None = None
    regime_transaction_costs: float = 0.0
    baseline_transaction_costs: float = 0.0
    regime_taxes: float = 0.0
    baseline_taxes: float = 0.0
    regime_turnover: float = 0.0
    baseline_turnover: float = 0.0
    regime_commission: float = 0.0
    baseline_commission: float = 0.0
    regime_slippage: float = 0.0
    baseline_slippage: float = 0.0
    regime_fixed_fees: float = 0.0
    baseline_fixed_fees: float = 0.0
    regime_entry_date: str | None = None
    regime_exit_date: str | None = None
    baseline_entry_date: str | None = None
    baseline_exit_date: str | None = None
    regime_starting_capital: float | None = None
    baseline_starting_capital: float | None = None
    regime_ending_capital: float | None = None
    baseline_ending_capital: float | None = None
    regime_short_term_tax: float = 0.0
    baseline_short_term_tax: float = 0.0
    regime_long_term_tax: float = 0.0
    baseline_long_term_tax: float = 0.0
    regime_dividend_tax: float = 0.0
    baseline_dividend_tax: float = 0.0
    regime_traded_notional: float = 0.0
    baseline_traded_notional: float = 0.0
    regime_trades: tuple[PortfolioTrade, ...] = ()
    baseline_trades: tuple[PortfolioTrade, ...] = ()
    regime_conversions: tuple[PortfolioConversion, ...] = ()
    baseline_conversions: tuple[PortfolioConversion, ...] = ()
    regime_ending_holdings: tuple[str, ...] = ()
    baseline_ending_holdings: tuple[str, ...] = ()
    regime_open_tax_lots: int = 0
    baseline_open_tax_lots: int = 0

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["regime_selected"] = list(self.regime_selected)
        result["baseline_selected"] = list(self.baseline_selected)
        result["missing"] = list(self.missing)
        result["member_tickers"] = list(self.member_tickers)
        result["factor_audit"] = [row.to_dict() for row in self.factor_audit]
        result["regime_selection_audit"] = [row.to_dict() for row in self.regime_selection_audit]
        result["baseline_selection_audit"] = [
            row.to_dict() for row in self.baseline_selection_audit
        ]
        result["regime_trades"] = [trade.to_dict() for trade in self.regime_trades]
        result["baseline_trades"] = [trade.to_dict() for trade in self.baseline_trades]
        result["regime_conversions"] = [
            conversion.to_dict() for conversion in self.regime_conversions
        ]
        result["baseline_conversions"] = [
            conversion.to_dict() for conversion in self.baseline_conversions
        ]
        result["regime_ending_holdings"] = list(self.regime_ending_holdings)
        result["baseline_ending_holdings"] = list(self.baseline_ending_holdings)
        return result


@dataclass(frozen=True)
class BacktestMetrics:
    """Compound performance and friction summary for one policy."""

    completed_periods: int
    cumulative_return: float | None
    annualized_return: float | None
    annualized_volatility: float | None
    max_drawdown: float | None
    win_rate: float | None
    gross_cumulative_return: float | None = None
    total_transaction_costs: float = 0.0
    total_taxes: float = 0.0
    total_turnover: float = 0.0
    daily_observations: int = 0
    # Risk-adjusted return assuming a 0% risk-free rate. Stated explicitly
    # because a Sharpe quoted without its rate assumption is not comparable
    # to anyone else's.
    sharpe_ratio: float | None = None
    # t-statistic of the mean period return against zero. This tests only
    # "did this make money", never "did this beat the benchmark" — the
    # latter is the question that matters and lives in
    # `strategy_vs_benchmark_significance` on the result, because it needs
    # both return series.
    return_t_stat: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QVBacktestResult:
    """Full result, including skipped periods and benchmark comparisons."""

    config: QVBacktestConfig
    tickers: tuple[str, ...]
    periods: list[BacktestPeriod] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    benchmark_metrics: dict[str, BacktestMetrics] = field(default_factory=dict)
    benchmark_periods: dict[str, list[BenchmarkPeriod]] = field(default_factory=dict)
    benchmark_equity_curves: dict[str, list[PortfolioEquityPoint]] = field(default_factory=dict)
    regime_equity_curve: list[EquityCurvePoint] = field(default_factory=list)
    baseline_equity_curve: list[EquityCurvePoint] = field(default_factory=list)
    data_quality_report: list[dict[str, Any]] = field(default_factory=list)
    table_rowcounts: dict[str, int] = field(default_factory=dict)

    @property
    def regime_metrics(self) -> BacktestMetrics:
        return _compute_metrics(
            self.periods,
            "regime_return",
            "regime_gross_return",
            "regime_transaction_costs",
            "regime_taxes",
            "regime_turnover",
            self.regime_equity_curve,
        )

    @property
    def baseline_metrics(self) -> BacktestMetrics:
        return _compute_metrics(
            self.periods,
            "baseline_return",
            "baseline_gross_return",
            "baseline_transaction_costs",
            "baseline_taxes",
            "baseline_turnover",
            self.baseline_equity_curve,
        )

    @property
    def comparison_periods(self) -> int:
        return sum(period.status == "complete" for period in self.periods)

    def strategy_vs_benchmark_significance(self) -> dict[str, Any]:
        """Test the only question that matters: did this beat buy-and-hold?

        Cumulative return alone cannot answer that. A strategy holding ten
        equal-weighted names over a handful of rebalances can beat or trail
        an index by double digits on noise, and this repository has already
        produced exactly that: two windows in which the ranking of two
        strategies against the same benchmark reverses completely.

        This pairs the strategy and benchmark daily returns *by date* — an
        unpaired comparison would silently compare different trading days —
        and reports the mean daily excess with its t-statistic. It never
        returns a pass/fail flag, because a threshold would just relocate
        the cherry-picking rather than remove it. `verdict` states what the
        sample can and cannot support, in words.
        """
        results: dict[str, Any] = {}
        strategy_by_date = {
            point.date: point.net_daily_return
            for point in self.regime_equity_curve
            if point.net_daily_return is not None
        }
        for ticker, curve in self.benchmark_equity_curves.items():
            benchmark_by_date = {
                point.date: point.daily_return
                for point in curve
                if point.daily_return is not None
            }
            shared_dates = sorted(strategy_by_date.keys() & benchmark_by_date.keys())
            excess = [
                float(strategy_by_date[day]) - float(benchmark_by_date[day])
                for day in shared_dates
            ]
            _sharpe, t_stat = _sharpe_and_t_stat(excess, 252.0)
            mean_daily_excess = sum(excess) / len(excess) if excess else None
            results[ticker] = {
                "paired_observations": len(excess),
                "completed_periods": self.comparison_periods,
                "mean_daily_excess_return": mean_daily_excess,
                "excess_return_t_stat": t_stat,
                "verdict": _significance_verdict(
                    t_stat,
                    self.comparison_periods,
                    len(excess),
                ),
            }
        return results

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": asdict(self.config),
            "tickers": list(self.tickers),
            "regime_metrics": self.regime_metrics.to_dict(),
            "baseline_metrics": self.baseline_metrics.to_dict(),
            "strategy_vs_benchmark_significance": (
                self.strategy_vs_benchmark_significance()
            ),
            "benchmark_metrics": {
                ticker: metrics.to_dict() for ticker, metrics in self.benchmark_metrics.items()
            },
            "benchmark_periods": {
                ticker: [period.to_dict() for period in periods]
                for ticker, periods in self.benchmark_periods.items()
            },
            "benchmark_equity_curves": {
                ticker: [point.to_dict() for point in curve]
                for ticker, curve in self.benchmark_equity_curves.items()
            },
            "regime_equity_curve": [point.to_dict() for point in self.regime_equity_curve],
            "baseline_equity_curve": [point.to_dict() for point in self.baseline_equity_curve],
            "data_quality_report": list(self.data_quality_report),
            "table_rowcounts": dict(self.table_rowcounts),
            "comparison_periods": self.comparison_periods,
            "warnings": list(self.warnings),
            "periods": [period.to_dict() for period in self.periods],
        }


def run_qv_policy_backtest(
    start: str | date,
    end: str | date,
    *,
    tickers: list[str] | None = None,
    top_n: int = 10,
    require_pit_regime: bool = True,
    universe_id: str | None = None,
    allow_current_universe: bool = False,
    benchmark_tickers: list[str] | None = None,
    calendar_ticker: str | None = None,
    excluded_tickers: list[str] | None = None,
    factor_model: str = "qv",
    initial_capital: float = 100_000.0,
    transaction_costs: TransactionCostPolicy | None = None,
    tax_policy: TaxPolicy | None = None,
    store: Store | None = None,
) -> QVBacktestResult:
    """Run regime-aware QV/QVML beside its fixed-baseline counterpart."""
    db = store or get_store()
    config = QVBacktestConfig(
        start=start,
        end=end,
        top_n=top_n,
        require_pit_regime=require_pit_regime,
        universe_id=universe_id,
        allow_current_universe=allow_current_universe,
        initial_capital=initial_capital,
        transaction_costs=transaction_costs or TransactionCostPolicy.zero(),
        tax_policy=tax_policy or TaxPolicy.zero(),
        benchmark_tickers=tuple(benchmark_tickers or ()),
        calendar_ticker=calendar_ticker,
        excluded_tickers=tuple(excluded_tickers or ()),
        factor_model=factor_model,
    )
    quality_report = db.data_quality_report()
    failures = [row["check"] for row in quality_report if row["status"] == "fail"]
    if failures:
        raise ValueError(
            "backtest refused because data validation failed: "
            + ", ".join(failures)
            + ". Run `aios validate` and repair the data first."
        )

    decision_dates = _quarterly_decision_dates(
        db,
        config.start,
        config.end,
        calendar_ticker=config.calendar_ticker,
    )
    if len(decision_dates) < 2:
        raise ValueError("backtest range needs at least two quarterly decision dates")
    requested = _normalize_tickers(tickers, db) if tickers else None
    execution_dates = {
        decision_date: _scheduled_execution_dates(
            db,
            decision_date,
            next_decision_date,
            config.calendar_ticker,
        )
        for decision_date, next_decision_date in zip(
            decision_dates, decision_dates[1:], strict=False
        )
    }
    decision_universes = _resolve_decision_universes(
        db,
        decision_dates[:-1],
        requested,
        config.universe_id,
        config.allow_current_universe,
        {
            decision_date: execution_dates[decision_date][0] or decision_date
            for decision_date in decision_dates[:-1]
        },
    )
    all_tickers = sorted({ticker for members in decision_universes.values() for ticker in members})
    if not all_tickers:
        raise ValueError("backtest universe is empty")

    result = QVBacktestResult(
        config=config,
        tickers=tuple(all_tickers),
        data_quality_report=quality_report,
        table_rowcounts=db.table_rowcounts(),
        benchmark_periods={ticker: [] for ticker in config.benchmark_tickers},
    )
    if config.universe_id is None:
        result.warnings.append(
            "Historical membership was not supplied; this run uses the current/fixed universe "
            "and is not survivorship-bias safe."
        )
    else:
        result.warnings.append(
            "Each target universe is membership known at the decision close and effective "
            "on the scheduled execution date; announced additions/removals are not shifted "
            "backward into the factor snapshot."
        )
    if config.calendar_ticker is None:
        result.warnings.append(
            "No explicit market-calendar ticker was supplied; quarter ends are inferred "
            "from all stored prices and can drift as local data changes."
        )
    if not require_pit_regime:
        result.warnings.append(
            "require_pit_regime=False: unknown macro periods may use baseline weights "
            "and must not be interpreted as regime-policy evidence."
        )
    if config.tax_policy == TaxPolicy.zero():
        result.warnings.append(
            "Tax rates are zero; pass jurisdiction-appropriate assumptions to report "
            "after-tax returns."
        )
    if config.excluded_tickers:
        result.warnings.append("Explicit policy exclusions: " + ", ".join(config.excluded_tickers))
    if config.factor_model == "qvml":
        result.warnings.append(
            "Experimental QVML requires complete Q, V, Momentum, and Low-Volatility evidence; "
            "missing sleeves are never reweighted away."
        )
    result.warnings.append(
        "Positions and FIFO tax lots persist across quarters; each rebalance trades only the "
        "equal-weight target deltas at the scheduled close."
    )
    result.warnings.append(
        "Daily equity uses provider closes, explicit dividends, and split ratios only when "
        "the provider close is not already split-normalized. Missing "
        "individual session prices are carried forward and exposed as stale_tickers; scheduled "
        "entry and exit prices remain strict."
    )
    result.warnings.append(
        "The generic tax ledger nets gains/losses within short- and long-term buckets over the "
        "run. Wash sales, cross-bucket offsets, carryforwards, and filing calendars are not "
        "modeled."
    )

    regime_book = _new_portfolio_book(db, config)
    baseline_book = _new_portfolio_book(db, config)
    regime_gross_book = _new_portfolio_book(db, config, gross=True)
    baseline_gross_book = _new_portfolio_book(db, config, gross=True)
    benchmark_books = {
        ticker: _new_portfolio_book(db, config, gross=True) for ticker in config.benchmark_tickers
    }
    benchmark_state_contiguous = {ticker: True for ticker in config.benchmark_tickers}
    portfolio_state_contiguous = True
    for decision_date, next_decision_date in zip(decision_dates, decision_dates[1:], strict=False):
        scheduled_entry_date, scheduled_exit_date = execution_dates[decision_date]
        members = decision_universes[decision_date]
        coverage = (
            db.universe_data_coverage(
                config.universe_id,
                decision_date,
                scheduled_entry_date or decision_date,
            )
            if config.universe_id is not None
            else []
        )
        macro_snapshot = compute_regime(decision_date, db)
        composite_kwargs: dict[str, Any] = {}
        if config.factor_model == "qvml":
            composite_kwargs["include_market_factors"] = True
        rows = compute_composite(
            members,
            decision_date.isoformat(),
            db,
            regime_snapshot=macro_snapshot,
            **composite_kwargs,
        )
        member_set = set(members)
        factor_tickers = [row.ticker for row in rows]
        if len(factor_tickers) != len(member_set) or set(factor_tickers) != member_set:
            raise ValueError(
                f"factor output does not partition the PIT universe on {decision_date}"
            )
        if config.universe_id is not None:
            coverage_tickers = [str(row["ticker"]).upper() for row in coverage]
            if len(coverage_tickers) != len(member_set) or set(coverage_tickers) != member_set:
                raise ValueError(
                    f"coverage output does not partition the PIT universe on {decision_date}"
                )
        period = _evaluate_period(
            rows,
            members,
            coverage,
            macro_snapshot,
            set(config.excluded_tickers),
            decision_date,
            next_decision_date,
            scheduled_entry_date,
            scheduled_exit_date,
            config.top_n,
            config.require_pit_regime,
            config.factor_model,
        )
        if period.status == "ready":
            if not portfolio_state_contiguous:
                period = replace(
                    period,
                    status="skipped_portfolio_state_discontinuity",
                    missing=("prior_period_incomplete",),
                )
            else:
                (
                    period,
                    regime_book,
                    regime_gross_book,
                    baseline_book,
                    baseline_gross_book,
                ) = _simulate_portfolios(
                    period,
                    decision_date,
                    scheduled_entry_date,
                    scheduled_exit_date,
                    regime_book,
                    regime_gross_book,
                    baseline_book,
                    baseline_gross_book,
                )
        if period.status != "complete":
            portfolio_state_contiguous = False
        result.periods.append(period)
        for ticker in config.benchmark_tickers:
            if (
                period.status != "complete"
                or scheduled_entry_date is None
                or scheduled_exit_date is None
            ):
                benchmark_state_contiguous[ticker] = False
                result.benchmark_periods[ticker].append(
                    BenchmarkPeriod(
                        ticker=ticker,
                        decision_date=decision_date.isoformat(),
                        next_decision_date=next_decision_date.isoformat(),
                        entry_date=(
                            scheduled_entry_date.isoformat()
                            if scheduled_entry_date is not None
                            else None
                        ),
                        exit_date=(
                            scheduled_exit_date.isoformat()
                            if scheduled_exit_date is not None
                            else None
                        ),
                        period_return=None,
                        status="skipped_unpaired_strategy_period",
                        warning=period.status,
                    )
                )
                continue
            if not benchmark_state_contiguous[ticker]:
                result.benchmark_periods[ticker].append(
                    BenchmarkPeriod(
                        ticker=ticker,
                        decision_date=decision_date.isoformat(),
                        next_decision_date=next_decision_date.isoformat(),
                        entry_date=scheduled_entry_date.isoformat(),
                        exit_date=scheduled_exit_date.isoformat(),
                        period_return=None,
                        status="skipped_benchmark_state_discontinuity",
                        warning="prior_benchmark_period_incomplete",
                    )
                )
                continue
            candidate = benchmark_books[ticker].clone()
            benchmark_result = candidate.advance_period(
                (ticker,),
                decision_date,
                scheduled_entry_date,
                scheduled_exit_date,
            )
            benchmark_complete = benchmark_result.period_return is not None
            warning = ", ".join(benchmark_result.missing) or None
            result.benchmark_periods[ticker].append(
                BenchmarkPeriod(
                    ticker=ticker,
                    decision_date=decision_date.isoformat(),
                    next_decision_date=next_decision_date.isoformat(),
                    entry_date=(
                        benchmark_result.entry_date.isoformat()
                        if benchmark_result.entry_date is not None
                        else None
                    ),
                    exit_date=(
                        benchmark_result.exit_date.isoformat()
                        if benchmark_result.exit_date is not None
                        else None
                    ),
                    period_return=benchmark_result.period_return,
                    status="complete" if benchmark_complete else "skipped_missing_prices",
                    warning=warning,
                )
            )
            if benchmark_complete:
                benchmark_books[ticker] = candidate
            else:
                benchmark_state_contiguous[ticker] = False
                result.warnings.append(f"benchmark {ticker} {decision_date}: {warning}")

    if not result.comparison_periods:
        result.warnings.append("No period has complete PIT regime and price evidence.")
    result.regime_equity_curve = _combine_equity_curves(
        regime_book.curve,
        regime_gross_book.curve,
    )
    result.baseline_equity_curve = _combine_equity_curves(
        baseline_book.curve,
        baseline_gross_book.curve,
    )
    result.benchmark_equity_curves = {
        ticker: list(book.curve) for ticker, book in benchmark_books.items()
    }
    result.benchmark_metrics = {
        ticker: _compute_benchmark_metrics(
            result.benchmark_periods[ticker],
            result.benchmark_equity_curves[ticker],
        )
        for ticker in config.benchmark_tickers
    }
    for ticker, periods in result.benchmark_periods.items():
        completed = sum(period.status == "complete" for period in periods)
        if completed != result.comparison_periods:
            result.warnings.append(
                f"benchmark {ticker} has {completed} paired periods versus "
                f"{result.comparison_periods} completed strategy periods"
            )
    return result


def _new_portfolio_book(
    store: Store,
    config: QVBacktestConfig,
    *,
    gross: bool = False,
) -> PortfolioBook:
    return PortfolioBook(
        store,
        initial_capital=config.initial_capital,
        transaction_costs=(TransactionCostPolicy.zero() if gross else config.transaction_costs),
        tax_policy=TaxPolicy.zero() if gross else config.tax_policy,
        calendar_ticker=config.calendar_ticker,
    )


def _simulate_portfolios(
    period: BacktestPeriod,
    decision_date: date,
    entry_date: date | None,
    exit_date: date | None,
    regime_book: PortfolioBook,
    regime_gross_book: PortfolioBook,
    baseline_book: PortfolioBook,
    baseline_gross_book: PortfolioBook,
) -> tuple[BacktestPeriod, PortfolioBook, PortfolioBook, PortfolioBook, PortfolioBook]:
    """Advance four candidate books and commit only a fully paired transition."""
    if entry_date is None or exit_date is None:
        return (
            replace(
                period,
                status="skipped_scheduled_execution_date_unavailable",
                missing=("scheduled_execution_date_unavailable",),
            ),
            regime_book,
            regime_gross_book,
            baseline_book,
            baseline_gross_book,
        )
    candidates = (
        regime_book.clone(),
        regime_gross_book.clone(),
        baseline_book.clone(),
        baseline_gross_book.clone(),
    )
    regime_result = candidates[0].advance_period(
        period.regime_selected,
        decision_date,
        entry_date,
        exit_date,
    )
    regime_gross_result = candidates[1].advance_period(
        period.regime_selected,
        decision_date,
        entry_date,
        exit_date,
    )
    baseline_result = candidates[2].advance_period(
        period.baseline_selected,
        decision_date,
        entry_date,
        exit_date,
    )
    baseline_gross_result = candidates[3].advance_period(
        period.baseline_selected,
        decision_date,
        entry_date,
        exit_date,
    )
    missing = tuple(
        sorted(
            [f"regime:{item}" for item in regime_result.missing]
            + [f"regime_gross:{item}" for item in regime_gross_result.missing]
            + [f"baseline:{item}" for item in baseline_result.missing]
            + [f"baseline_gross:{item}" for item in baseline_gross_result.missing]
        )
    )
    complete = all(
        result.period_return is not None
        and result.entry_date == entry_date
        and result.exit_date == exit_date
        for result in (
            regime_result,
            regime_gross_result,
            baseline_result,
            baseline_gross_result,
        )
    )
    if missing or not complete:
        return (
            replace(
                period,
                status="skipped_missing_prices",
                missing=missing or ("incomplete_portfolio_transition",),
            ),
            regime_book,
            regime_gross_book,
            baseline_book,
            baseline_gross_book,
        )
    completed = _apply_portfolio_results(
        period,
        regime_result,
        regime_gross_result,
        baseline_result,
        baseline_gross_result,
    )
    return completed, *candidates


def _apply_portfolio_results(
    period: BacktestPeriod,
    regime: PortfolioPeriodResult,
    regime_gross: PortfolioPeriodResult,
    baseline: PortfolioPeriodResult,
    baseline_gross: PortfolioPeriodResult,
) -> BacktestPeriod:
    return replace(
        period,
        status="complete",
        missing=(),
        regime_return=regime.period_return,
        baseline_return=baseline.period_return,
        regime_gross_return=regime_gross.period_return,
        baseline_gross_return=baseline_gross.period_return,
        regime_transaction_costs=regime.transaction_costs,
        baseline_transaction_costs=baseline.transaction_costs,
        regime_taxes=regime.taxes,
        baseline_taxes=baseline.taxes,
        regime_turnover=regime.turnover,
        baseline_turnover=baseline.turnover,
        regime_commission=regime.commission,
        baseline_commission=baseline.commission,
        regime_slippage=regime.slippage,
        baseline_slippage=baseline.slippage,
        regime_fixed_fees=regime.fixed_fees,
        baseline_fixed_fees=baseline.fixed_fees,
        regime_entry_date=regime.entry_date.isoformat() if regime.entry_date else None,
        regime_exit_date=regime.exit_date.isoformat() if regime.exit_date else None,
        baseline_entry_date=baseline.entry_date.isoformat() if baseline.entry_date else None,
        baseline_exit_date=baseline.exit_date.isoformat() if baseline.exit_date else None,
        regime_starting_capital=regime.starting_equity,
        baseline_starting_capital=baseline.starting_equity,
        regime_ending_capital=regime.ending_equity,
        baseline_ending_capital=baseline.ending_equity,
        regime_short_term_tax=regime.short_term_tax,
        baseline_short_term_tax=baseline.short_term_tax,
        regime_long_term_tax=regime.long_term_tax,
        baseline_long_term_tax=baseline.long_term_tax,
        regime_dividend_tax=regime.dividend_tax,
        baseline_dividend_tax=baseline.dividend_tax,
        regime_traded_notional=regime.traded_notional,
        baseline_traded_notional=baseline.traded_notional,
        regime_trades=regime.trades,
        baseline_trades=baseline.trades,
        regime_conversions=regime.conversions,
        baseline_conversions=baseline.conversions,
        regime_ending_holdings=regime.ending_holdings,
        baseline_ending_holdings=baseline.ending_holdings,
        regime_open_tax_lots=regime.open_tax_lots,
        baseline_open_tax_lots=baseline.open_tax_lots,
    )


def _combine_equity_curves(
    net: list[PortfolioEquityPoint],
    gross: list[PortfolioEquityPoint],
) -> list[EquityCurvePoint]:
    if len(net) != len(gross):
        raise ValueError("net and gross portfolio curves are not aligned")
    combined: list[EquityCurvePoint] = []
    for net_point, gross_point in zip(net, gross, strict=True):
        if net_point.date != gross_point.date:
            raise ValueError("net and gross portfolio curve dates are not aligned")
        combined.append(
            EquityCurvePoint(
                date=net_point.date.isoformat(),
                net_equity=net_point.equity,
                gross_equity=gross_point.equity,
                net_cash=net_point.cash,
                gross_cash=gross_point.cash,
                position_count=net_point.position_count,
                net_daily_return=net_point.daily_return,
                gross_daily_return=gross_point.daily_return,
                drawdown=net_point.drawdown,
                cumulative_transaction_costs=net_point.cumulative_transaction_costs,
                accrued_taxes=net_point.accrued_taxes,
                stale_tickers=net_point.stale_tickers,
            )
        )
    return combined


def _resolve_decision_universes(
    store: Store,
    decision_dates: list[date],
    requested: list[str] | None,
    universe_id: str | None,
    allow_current_universe: bool,
    effective_dates: dict[date, date],
) -> dict[date, list[str]]:
    if universe_id is None:
        if not allow_current_universe:
            raise ValueError(
                "historical universe membership is required; import a CSV and pass "
                "--universe-id, or explicitly pass --allow-current-universe for a "
                "survivorship-biased diagnostic"
            )
        current = requested or _normalize_tickers(None, store)
        return {decision_date: current for decision_date in decision_dates}

    requested_set = set(requested or ())
    resolved: dict[date, list[str]] = {}
    for decision_date in decision_dates:
        effective_date = effective_dates[decision_date]
        members = {
            row["ticker"]
            for row in store.universe_membership_known_on(
                universe_id,
                decision_date,
                effective_date,
            )
        }
        if not members:
            raise ValueError(
                f"historical universe {universe_id!r} has no membership known on "
                f"{decision_date} and effective on {effective_date}; "
                "load effective_start/effective_end and known_date coverage first"
            )
        if requested_set:
            missing = sorted(requested_set - members)
            if missing:
                raise ValueError(
                    f"requested ticker(s) not in historical universe {universe_id!r} on "
                    f"execution date {effective_date}: {', '.join(missing)}"
                )
            members &= requested_set
        resolved[decision_date] = sorted(members)
    return resolved


def _evaluate_period(
    rows: list[CompositeRow],
    members: list[str],
    coverage: list[dict[str, Any]],
    macro_snapshot: MacroRegimeSnapshot,
    excluded_tickers: set[str],
    decision_date: date,
    next_decision_date: date,
    scheduled_entry_date: date | None,
    scheduled_exit_date: date | None,
    top_n: int,
    require_pit_regime: bool,
    factor_model: str,
) -> BacktestPeriod:
    decision_text = decision_date.isoformat()
    next_decision_text = next_decision_date.isoformat()
    regime_ready = macro_snapshot.is_pit_ready and macro_snapshot.regime != "unknown"
    regime = macro_snapshot.regime if regime_ready else "unknown"
    qv_weights = weights_for_regime(regime) if regime_ready else BASELINE_FACTOR_WEIGHTS
    if factor_model == "qvml":
        regime_weights: FactorWeights | QVMLFactorWeights = qvml_weights_for_regime(regime)
        baseline_weights: FactorWeights | QVMLFactorWeights = qvml_weights_for_regime("unknown")
    else:
        regime_weights = qv_weights
        baseline_weights = BASELINE_FACTOR_WEIGHTS
    factor_audit = _build_factor_audit(
        rows,
        coverage,
        decision_date,
        excluded_tickers=excluded_tickers,
        factor_model=factor_model,
    )
    eligible_tickers = {row.ticker for row in factor_audit if row.eligible}
    eligible = [row for row in rows if row.ticker in eligible_tickers]
    member_tickers = tuple(sorted({ticker.upper() for ticker in members}))
    coverage_rows = list(coverage)
    price_covered = (
        sum(bool(row.get("has_price_history")) for row in coverage_rows) if coverage_rows else None
    )
    fundamental_covered = (
        sum(bool(row.get("has_pit_fundamentals")) for row in coverage_rows)
        if coverage_rows
        else None
    )
    raw_complete = (
        sum(
            bool(row.get("has_price_history")) and bool(row.get("has_pit_fundamentals"))
            for row in coverage_rows
        )
        if coverage_rows
        else None
    )
    base_kwargs = {
        "decision_date": decision_text,
        "next_decision_date": next_decision_text,
        "entry_date": (
            scheduled_entry_date.isoformat() if scheduled_entry_date is not None else None
        ),
        "exit_date": (scheduled_exit_date.isoformat() if scheduled_exit_date is not None else None),
        "regime": regime,
        "factor_model": factor_model,
        "quality_weight": regime_weights.quality,
        "value_weight": regime_weights.value,
        "momentum_weight": getattr(regime_weights, "momentum", 0.0),
        "low_volatility_weight": getattr(regime_weights, "low_volatility", 0.0),
        "eligible_tickers": len(eligible),
        "member_tickers": member_tickers,
        "member_list_sha256": sha256("\n".join(member_tickers).encode()).hexdigest(),
        "price_covered_tickers": price_covered,
        "pit_fundamental_covered_tickers": fundamental_covered,
        "raw_complete_tickers": raw_complete,
        "quality_scored_tickers": sum(row.quality_score is not None for row in rows),
        "value_scored_tickers": sum(row.value_score is not None for row in rows),
        "momentum_scored_tickers": sum(row.momentum_score is not None for row in rows),
        "low_volatility_scored_tickers": sum(row.low_volatility_score is not None for row in rows),
        "qvml_scored_tickers": sum(row.qvml_score is not None for row in rows),
        "factor_audit": factor_audit,
        "macro_snapshot": macro_snapshot.to_dict(),
    }

    if scheduled_entry_date is None or scheduled_exit_date is None:
        return BacktestPeriod(
            **base_kwargs,
            status="skipped_scheduled_execution_date_unavailable",
            missing=("scheduled_execution_date_unavailable",),
        )
    if require_pit_regime and not regime_ready:
        return BacktestPeriod(
            **base_kwargs,
            status="skipped_regime_not_pit_ready",
            missing=("macro_regime_pit_unavailable",),
        )
    if len(eligible) < top_n:
        return BacktestPeriod(
            **base_kwargs,
            status="skipped_insufficient_factor_coverage",
            missing=(f"eligible_tickers:{len(eligible)}<top_n:{top_n}",),
        )

    regime_ranked = _rank_rows(eligible, regime_weights, factor_model)
    baseline_ranked = _rank_rows(eligible, baseline_weights, factor_model)
    regime_tickers = tuple(row.ticker for _, row in regime_ranked[:top_n])
    baseline_tickers = tuple(row.ticker for _, row in baseline_ranked[:top_n])
    regime_selection_audit = _selection_audit(regime_ranked, top_n, factor_model)
    baseline_selection_audit = _selection_audit(baseline_ranked, top_n, factor_model)
    return BacktestPeriod(
        **base_kwargs,
        regime_selected=regime_tickers,
        baseline_selected=baseline_tickers,
        regime_selection_audit=regime_selection_audit,
        baseline_selection_audit=baseline_selection_audit,
        status="ready",
    )


def _build_factor_audit(
    rows: list[CompositeRow],
    coverage: list[dict[str, Any]],
    decision_date: date,
    *,
    excluded_tickers: set[str] | None = None,
    factor_model: str = "qv",
) -> tuple[FactorAuditRow, ...]:
    """Partition every member into eligible or a deterministic exclusion reason."""
    coverage_by_ticker = {str(row["ticker"]).upper(): row for row in coverage}
    exclusions = excluded_tickers or set()
    audit: list[FactorAuditRow] = []
    for row in sorted(rows, key=lambda item: item.ticker):
        raw = coverage_by_ticker.get(row.ticker)
        has_price = bool(raw.get("has_price_history")) if raw is not None else None
        has_fundamentals = bool(raw.get("has_pit_fundamentals")) if raw is not None else None
        latest_price = raw.get("latest_price_date") if raw is not None else None
        latest_fundamental = raw.get("latest_fundamental_date") if raw is not None else None
        latest_price_text = str(latest_price) if latest_price is not None else None
        latest_fundamental_text = (
            str(latest_fundamental) if latest_fundamental is not None else None
        )
        reasons = list(row.missing)
        if raw is not None:
            if not has_price:
                reasons.append("missing_price_history")
            elif latest_price_text != decision_date.isoformat():
                reasons.append(f"stale_price:{latest_price_text or 'unknown'}")
            if not has_fundamentals:
                reasons.append("missing_pit_fundamentals")
        if row.quality_score is None:
            reasons.append("quality_score_unavailable")
        if row.value_score is None:
            reasons.append("value_score_unavailable")
        if factor_model == "qvml" and row.momentum_score is None:
            reasons.append("momentum_score_unavailable")
        if factor_model == "qvml" and row.low_volatility_score is None:
            reasons.append("low_volatility_score_unavailable")
        if row.ticker in exclusions:
            reasons.append("explicit_policy_exclusion")
        core_eligible = bool(
            row.quality_score is not None
            and row.value_score is not None
            and row.ticker not in exclusions
            and (
                raw is None
                or (
                    has_price
                    and latest_price_text == decision_date.isoformat()
                    and has_fundamentals
                )
            )
        )
        eligible = core_eligible and (
            factor_model == "qv"
            or (row.momentum_score is not None and row.low_volatility_score is not None)
        )
        audit.append(
            FactorAuditRow(
                ticker=row.ticker,
                security_id=(
                    str(raw.get("security_id")) if raw and raw.get("security_id") else None
                ),
                eligible=eligible,
                has_price_history=has_price,
                has_pit_fundamentals=has_fundamentals,
                latest_price_date=latest_price_text,
                latest_fundamental_date=latest_fundamental_text,
                quality_score=row.quality_score,
                value_score=row.value_score,
                momentum_score=row.momentum_score,
                low_volatility_score=row.low_volatility_score,
                qv_score=row.qv_score,
                qvml_score=row.qvml_score,
                quality_components_available=row.quality_components_available,
                value_multiples_available=row.value_multiples_available,
                market_price_observations=row.market_price_observations,
                reasons=tuple(sorted(set(reasons))),
            )
        )
    return tuple(audit)


def _selection_audit(
    ranked: list[tuple[float, CompositeRow]], top_n: int, factor_model: str
) -> tuple[SelectionAuditRow, ...]:
    target_weight = 1.0 / top_n
    return tuple(
        SelectionAuditRow(
            ticker=row.ticker,
            quality_score=float(row.quality_score),
            value_score=float(row.value_score),
            policy_score=float(score),
            rank=rank,
            target_weight=target_weight,
            momentum_score=row.momentum_score,
            low_volatility_score=row.low_volatility_score,
            factor_model=factor_model,
        )
        for rank, (score, row) in enumerate(ranked[:top_n], start=1)
    )


def _rank_rows(
    rows: list[CompositeRow],
    weights: FactorWeights | QVMLFactorWeights,
    factor_model: str,
) -> list[tuple[float, CompositeRow]]:
    scored: list[tuple[float, CompositeRow]] = []
    for row in rows:
        if row.quality_score is None or row.value_score is None:
            continue
        score = weights.quality * row.quality_score + weights.value * row.value_score
        if factor_model == "qvml":
            if (
                row.momentum_score is None
                or row.low_volatility_score is None
                or not isinstance(weights, QVMLFactorWeights)
            ):
                continue
            score += (
                weights.momentum * row.momentum_score
                + weights.low_volatility * row.low_volatility_score
            )
        scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], item[1].ticker))
    return scored


def _portfolio_return(
    tickers: tuple[str, ...],
    decision_date: date,
    next_decision_date: date,
    store: Store,
) -> tuple[float | None, list[date], list[str]]:
    """Compatibility helper returning the zero-cost gross interval return."""
    result = simulate_period(
        tickers,
        decision_date,
        next_decision_date,
        store,
        initial_capital=1.0,
        transaction_costs=TransactionCostPolicy.zero(),
        tax_policy=TaxPolicy.zero(),
    )
    dates = [value for value in (result.entry_date, result.exit_date) if value]
    return result.gross_return, dates, list(result.missing)


def _quarterly_decision_dates(
    store: Store,
    start: str,
    end: str,
    *,
    calendar_ticker: str | None = None,
) -> list[date]:
    if calendar_ticker is None:
        rows = store.query(
            """
            SELECT DISTINCT date
            FROM prices
            WHERE date >= ? AND date <= ?
            ORDER BY date
            """,
            (start, end),
        )
    else:
        rows = store.query(
            """
            SELECT DISTINCT date
            FROM prices
            WHERE ticker = ? AND date >= ? AND date <= ?
            ORDER BY date
            """,
            (calendar_ticker, start, end),
        )
    quarter_ends: dict[tuple[int, int], date] = {}
    for row in rows:
        observation_date = row["date"]
        key = (observation_date.year, (observation_date.month - 1) // 3 + 1)
        quarter_ends[key] = max(quarter_ends.get(key, observation_date), observation_date)
    return sorted(quarter_ends.values())


def _scheduled_execution_dates(
    store: Store,
    decision_date: date,
    next_decision_date: date,
    calendar_ticker: str | None,
) -> tuple[date | None, date | None]:
    """Return one shared next-session entry and quarter-end exit schedule."""
    ticker_clause = "" if calendar_ticker is None else "AND ticker = ?"
    params: tuple[Any, ...]
    if calendar_ticker is None:
        params = (decision_date, next_decision_date)
    else:
        params = (decision_date, next_decision_date, calendar_ticker)
    rows = store.query(
        f"""
        SELECT MIN(date) AS entry_date
        FROM prices
        WHERE date > ? AND date <= ?
          {ticker_clause}
        """,
        params,
    )
    entry_date = rows[0]["entry_date"] if rows else None
    if calendar_ticker is None:
        exit_rows = store.query(
            "SELECT 1 AS present FROM prices WHERE date = ? LIMIT 1",
            (next_decision_date,),
        )
    else:
        exit_rows = store.query(
            "SELECT 1 AS present FROM prices WHERE ticker = ? AND date = ? LIMIT 1",
            (calendar_ticker, next_decision_date),
        )
    return entry_date, next_decision_date if exit_rows else None


def _normalize_tickers(tickers: list[str] | None, store: Store) -> list[str]:
    if tickers is None:
        rows = store.query("SELECT ticker FROM securities WHERE is_active = TRUE ORDER BY ticker")
        values = [row["ticker"] for row in rows]
    else:
        values = tickers
    return sorted({str(ticker).strip().upper() for ticker in values if str(ticker).strip()})


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _compute_metrics(
    periods: list[BacktestPeriod],
    field_name: str,
    gross_field: str,
    costs_field: str,
    taxes_field: str,
    turnover_field: str,
    equity_curve: list[EquityCurvePoint],
) -> BacktestMetrics:
    completed = [
        (period, getattr(period, field_name))
        for period in periods
        if period.status == "complete" and getattr(period, field_name) is not None
    ]
    if not completed:
        return BacktestMetrics(0, None, None, None, None, None)
    returns = [float(value) for _, value in completed]
    total_costs = sum(float(getattr(period, costs_field)) for period, _ in completed)
    total_taxes = sum(float(getattr(period, taxes_field)) for period, _ in completed)
    total_turnover = sum(float(getattr(period, turnover_field)) for period, _ in completed)
    if equity_curve:
        return _metrics_from_equity_curve(
            returns,
            equity_curve,
            total_costs,
            total_taxes,
            total_turnover,
        )
    gross_returns = [
        float(getattr(period, gross_field))
        for period, _ in completed
        if getattr(period, gross_field) is not None
    ]
    return _metrics_from_returns(
        completed,
        returns,
        gross_returns,
        total_costs,
        total_taxes,
        total_turnover,
    )


def _compute_benchmark_metrics(
    periods: list[BenchmarkPeriod],
    curve: list[PortfolioEquityPoint],
) -> BacktestMetrics:
    returns = [
        float(period.period_return)
        for period in periods
        if period.status == "complete" and period.period_return is not None
    ]
    if not returns or not curve:
        return BacktestMetrics(0, None, None, None, None, None)
    aligned = [
        EquityCurvePoint(
            date=point.date.isoformat(),
            net_equity=point.equity,
            gross_equity=point.equity,
            net_cash=point.cash,
            gross_cash=point.cash,
            position_count=point.position_count,
            net_daily_return=point.daily_return,
            gross_daily_return=point.daily_return,
            drawdown=point.drawdown,
            cumulative_transaction_costs=0.0,
            accrued_taxes=0.0,
            stale_tickers=point.stale_tickers,
        )
        for point in curve
    ]
    return _metrics_from_equity_curve(returns, aligned, 0.0, 0.0, 0.0)


def _significance_verdict(
    t_stat: float | None,
    completed_periods: int,
    paired_observations: int,
) -> str:
    """State plainly what this sample can support, so a reader cannot over-read it.

    Deliberately conservative. A rebalance count in the single digits cannot
    establish an edge no matter how large the return gap looks, and saying so
    in the artifact is the only thing that reliably stops a lucky window from
    being promoted as a finding.
    """
    if t_stat is None or paired_observations < 2:
        return "insufficient data: no paired observations to compare"
    if completed_periods < MINIMUM_PERIODS_FOR_ANY_INFERENCE:
        return (
            f"not interpretable: {completed_periods} completed rebalance(s) is "
            "far too few to separate skill from luck, whatever the return gap"
        )
    if abs(t_stat) < 2.0:
        return (
            f"indistinguishable from noise (t={t_stat:.2f}): this result is "
            "consistent with chance and must not be reported as an edge"
        )
    return (
        f"statistically suggestive (t={t_stat:.2f}) but unconfirmed: daily "
        "returns are autocorrelated, so this overstates significance; treat "
        "as a hypothesis for out-of-sample testing, never as a validated edge"
    )


def _sharpe_and_t_stat(
    returns: list[float],
    periods_per_year: float,
) -> tuple[float | None, float | None]:
    """Return an annualized Sharpe (0% risk-free) and a t-stat against zero.

    Both are ``None`` below two observations or at zero dispersion, where
    neither quantity is defined. Reporting ``0.0`` there would read as a
    measured result rather than an absent one.

    The t-statistic treats period returns as independent, which overstates
    significance when returns are autocorrelated. It is a floor on the
    evidence required, never a proof of skill — and with the handful of
    rebalances a short window produces, it will correctly stay small.
    """
    if len(returns) < 2:
        return None, None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    deviation = sqrt(variance)
    if deviation <= 0:
        return None, None
    sharpe = (mean / deviation) * sqrt(periods_per_year)
    t_stat = mean / (deviation / sqrt(len(returns)))
    return sharpe, t_stat


def _metrics_from_equity_curve(
    period_returns: list[float],
    curve: list[EquityCurvePoint],
    total_costs: float,
    total_taxes: float,
    total_turnover: float,
) -> BacktestMetrics:
    first = curve[0]
    last = curve[-1]
    if first.net_equity <= 0 or first.gross_equity <= 0:
        raise ValueError("portfolio equity curve must start with positive equity")
    cumulative_return = last.net_equity / first.net_equity - 1.0
    gross_cumulative_return = last.gross_equity / first.gross_equity - 1.0
    first_date = date.fromisoformat(first.date)
    last_date = date.fromisoformat(last.date)
    years = max((last_date - first_date).days / 365.25, 1 / 365.25)
    annualized_return = (1.0 + cumulative_return) ** (1.0 / years) - 1.0
    daily_returns = [
        float(point.net_daily_return) for point in curve if point.net_daily_return is not None
    ]
    mean = sum(daily_returns) / len(daily_returns) if daily_returns else 0.0
    variance = (
        sum((daily_return - mean) ** 2 for daily_return in daily_returns) / (len(daily_returns) - 1)
        if len(daily_returns) > 1
        else 0.0
    )
    sharpe, t_stat = _sharpe_and_t_stat(daily_returns, 252.0)
    return BacktestMetrics(
        completed_periods=len(period_returns),
        cumulative_return=cumulative_return,
        annualized_return=annualized_return,
        annualized_volatility=sqrt(variance) * sqrt(252.0),
        max_drawdown=min(point.drawdown for point in curve),
        win_rate=sum(period_return > 0 for period_return in period_returns) / len(period_returns),
        gross_cumulative_return=gross_cumulative_return,
        total_transaction_costs=total_costs,
        total_taxes=total_taxes,
        total_turnover=total_turnover,
        daily_observations=len(curve),
        sharpe_ratio=sharpe,
        return_t_stat=t_stat,
    )


def _metrics_from_returns(
    periods: list[tuple[Any, Any, float]],
    returns: list[float],
    gross_returns: list[float],
    total_costs: float,
    total_taxes: float,
    total_turnover: float,
) -> BacktestMetrics:
    equity = 1.0
    gross_equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for period_return in returns:
        equity *= 1.0 + period_return
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
    for period_return in gross_returns:
        gross_equity *= 1.0 + period_return

    first_entry = min(_period_entry(record) for record in periods)
    last_exit = max(_period_exit(record) for record in periods)
    years = max((last_exit - first_entry).days / 365.25, 1 / 365.25)
    annualized_return = equity ** (1.0 / years) - 1.0
    mean = sum(returns) / len(returns)
    variance = (
        sum((period_return - mean) ** 2 for period_return in returns) / (len(returns) - 1)
        if len(returns) > 1
        else 0.0
    )
    sharpe, t_stat = _sharpe_and_t_stat(returns, 4.0)
    return BacktestMetrics(
        completed_periods=len(returns),
        cumulative_return=equity - 1.0,
        annualized_return=annualized_return,
        annualized_volatility=sqrt(variance) * sqrt(4.0),
        max_drawdown=max_drawdown,
        win_rate=sum(period_return > 0 for period_return in returns) / len(returns),
        gross_cumulative_return=gross_equity - 1.0,
        total_transaction_costs=total_costs,
        total_taxes=total_taxes,
        total_turnover=total_turnover,
        sharpe_ratio=sharpe,
        return_t_stat=t_stat,
    )


def _period_entry(period: Any) -> date:
    if isinstance(period, tuple) and len(period) >= 2:
        period = period[0] if hasattr(period[0], "entry_date") else period
    value = period.entry_date if hasattr(period, "entry_date") else period[0]
    return value if isinstance(value, date) else date.fromisoformat(value)


def _period_exit(period: Any) -> date:
    if isinstance(period, tuple) and len(period) >= 2:
        period = period[0] if hasattr(period[0], "exit_date") else period
    value = period.exit_date if hasattr(period, "exit_date") else period[1]
    return value if isinstance(value, date) else date.fromisoformat(value)

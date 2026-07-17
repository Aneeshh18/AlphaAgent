"""PIT-safe QV policy backtest with explicit implementation assumptions.

The engine is still a validation harness rather than a live order simulator:
quarterly decisions select equal-weight top-N holdings, entries use the first
common session strictly after the decision, and exits use the last common
session on or before the next decision. It now makes the important research
assumptions explicit:

* historical membership is required unless the caller opts into the current
  active securities table;
* transaction costs are charged on entry and exit notional;
* taxes use split-aware realized lots and supplied rates; and
* benchmarks are explicit price-series inputs and are never silently replaced.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from math import sqrt
from typing import Any

from aios.backtest.costs import (
    TaxPolicy,
    TransactionCostPolicy,
    benchmark_period_return,
    simulate_period,
)
from aios.factors.composite import CompositeRow, compute_composite
from aios.factors.policy import BASELINE_FACTOR_WEIGHTS, FactorWeights, weights_for_regime
from aios.storage.store import Store, get_store


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
    transaction_costs: TransactionCostPolicy = field(
        default_factory=TransactionCostPolicy.zero
    )
    tax_policy: TaxPolicy = field(default_factory=TaxPolicy.zero)
    benchmark_tickers: tuple[str, ...] = ()

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
        object.__setattr__(self, "start", start.isoformat())
        object.__setattr__(self, "end", end.isoformat())
        if self.universe_id is not None:
            universe_id = self.universe_id.strip()
            object.__setattr__(self, "universe_id", universe_id or None)
        normalized_benchmarks = tuple(
            sorted({ticker.strip().upper() for ticker in self.benchmark_tickers if ticker.strip()})
        )
        object.__setattr__(self, "benchmark_tickers", normalized_benchmarks)


@dataclass(frozen=True)
class BacktestPeriod:
    """One decision-to-decision interval and its audit evidence."""

    decision_date: str
    next_decision_date: str
    entry_date: str | None
    exit_date: str | None
    regime: str
    quality_weight: float
    value_weight: float
    eligible_tickers: int
    regime_selected: tuple[str, ...] = ()
    baseline_selected: tuple[str, ...] = ()
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

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["regime_selected"] = list(self.regime_selected)
        result["baseline_selected"] = list(self.baseline_selected)
        result["missing"] = list(self.missing)
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

    @property
    def regime_metrics(self) -> BacktestMetrics:
        return _compute_metrics(
            self.periods,
            "regime_return",
            "regime_gross_return",
            "regime_transaction_costs",
            "regime_taxes",
            "regime_turnover",
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
        )

    @property
    def comparison_periods(self) -> int:
        return sum(
            period.regime_return is not None and period.baseline_return is not None
            for period in self.periods
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": asdict(self.config),
            "tickers": list(self.tickers),
            "regime_metrics": self.regime_metrics.to_dict(),
            "baseline_metrics": self.baseline_metrics.to_dict(),
            "benchmark_metrics": {
                ticker: metrics.to_dict() for ticker, metrics in self.benchmark_metrics.items()
            },
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
    initial_capital: float = 100_000.0,
    transaction_costs: TransactionCostPolicy | None = None,
    tax_policy: TaxPolicy | None = None,
    store: Store | None = None,
) -> QVBacktestResult:
    """Run regime-aware QV beside fixed 60/40 with PIT universe controls."""
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
    )
    failures = [row["check"] for row in db.data_quality_report() if row["status"] == "fail"]
    if failures:
        raise ValueError(
            "backtest refused because data validation failed: "
            + ", ".join(failures)
            + ". Run `aios validate` and repair the data first."
        )

    decision_dates = _quarterly_decision_dates(db, config.start, config.end)
    if len(decision_dates) < 2:
        raise ValueError("backtest range needs at least two quarterly decision dates")
    requested = _normalize_tickers(tickers, db) if tickers else None
    decision_universes = _resolve_decision_universes(
        db,
        decision_dates[:-1],
        requested,
        config.universe_id,
        config.allow_current_universe,
    )
    all_tickers = sorted(
        {ticker for members in decision_universes.values() for ticker in members}
    )
    if not all_tickers:
        raise ValueError("backtest universe is empty")

    result = QVBacktestResult(config=config, tickers=tuple(all_tickers))
    if config.universe_id is None:
        result.warnings.append(
            "Historical membership was not supplied; this run uses the current/fixed universe "
            "and is not survivorship-bias safe."
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

    benchmark_returns: dict[str, list[tuple[date, date, float]]] = {
        ticker: [] for ticker in config.benchmark_tickers
    }
    regime_capital = config.initial_capital
    baseline_capital = config.initial_capital
    for decision_date, next_decision_date in zip(decision_dates, decision_dates[1:], strict=False):
        members = decision_universes[decision_date]
        rows = compute_composite(members, decision_date.isoformat(), db)
        period = _evaluate_period(
            rows,
            decision_date,
            next_decision_date,
            config.top_n,
            db,
            config.require_pit_regime,
            regime_capital,
            baseline_capital,
            config.transaction_costs,
            config.tax_policy,
        )
        result.periods.append(period)
        if period.regime_return is not None:
            regime_capital *= 1.0 + period.regime_return
        if period.baseline_return is not None:
            baseline_capital *= 1.0 + period.baseline_return
        for ticker in config.benchmark_tickers:
            benchmark, entry_date, exit_date, warning = benchmark_period_return(
                ticker, decision_date, next_decision_date, db
            )
            if benchmark is not None and entry_date is not None and exit_date is not None:
                benchmark_returns[ticker].append((entry_date, exit_date, benchmark))
            elif warning:
                result.warnings.append(f"benchmark {ticker} {decision_date}: {warning}")

    if not result.comparison_periods:
        result.warnings.append("No period has complete PIT regime and price evidence.")
    result.benchmark_metrics = {
        ticker: _compute_observation_metrics(observations)
        for ticker, observations in benchmark_returns.items()
    }
    return result


def _resolve_decision_universes(
    store: Store,
    decision_dates: list[date],
    requested: list[str] | None,
    universe_id: str | None,
    allow_current_universe: bool,
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
        members = {
            row["ticker"] for row in store.universe_membership_on(universe_id, decision_date)
        }
        if not members:
            raise ValueError(
                f"historical universe {universe_id!r} has no PIT membership on {decision_date}; "
                "load effective_start/effective_end and known_date coverage first"
            )
        if requested_set:
            missing = sorted(requested_set - members)
            if missing:
                raise ValueError(
                    f"requested ticker(s) not in historical universe {universe_id!r} on "
                    f"{decision_date}: {', '.join(missing)}"
                )
            members &= requested_set
        resolved[decision_date] = sorted(members)
    return resolved


def _evaluate_period(
    rows: list[CompositeRow],
    decision_date: date,
    next_decision_date: date,
    top_n: int,
    store: Store,
    require_pit_regime: bool,
    regime_capital: float,
    baseline_capital: float,
    transaction_costs: TransactionCostPolicy,
    tax_policy: TaxPolicy,
) -> BacktestPeriod:
    decision_text = decision_date.isoformat()
    next_decision_text = next_decision_date.isoformat()
    first = rows[0] if rows else None
    regime_ready = bool(
        first
        and first.regime_pit_ready
        and first.macro_regime != "unknown"
        and all(row.regime_pit_ready for row in rows)
    )
    regime = first.macro_regime if first else "unknown"
    regime_weights = weights_for_regime(regime) if regime_ready else BASELINE_FACTOR_WEIGHTS
    eligible = _eligible_rows(rows)
    base_kwargs = {
        "decision_date": decision_text,
        "next_decision_date": next_decision_text,
        "regime": regime,
        "quality_weight": regime_weights.quality,
        "value_weight": regime_weights.value,
        "eligible_tickers": len(eligible),
    }

    if require_pit_regime and not regime_ready:
        return BacktestPeriod(
            **base_kwargs,
            entry_date=None,
            exit_date=None,
            status="skipped_regime_not_pit_ready",
            missing=("macro_regime_pit_unavailable",),
        )
    if len(eligible) < top_n:
        return BacktestPeriod(
            **base_kwargs,
            entry_date=None,
            exit_date=None,
            status="skipped_insufficient_factor_coverage",
            missing=(f"eligible_tickers:{len(eligible)}<top_n:{top_n}",),
        )

    regime_tickers = tuple(row.ticker for _, row in _rank_rows(eligible, regime_weights)[:top_n])
    baseline_tickers = tuple(
        row.ticker for _, row in _rank_rows(eligible, BASELINE_FACTOR_WEIGHTS)[:top_n]
    )
    regime_result = simulate_period(
        regime_tickers,
        decision_date,
        next_decision_date,
        store,
        initial_capital=regime_capital,
        transaction_costs=transaction_costs,
        tax_policy=tax_policy,
    )
    baseline_result = simulate_period(
        baseline_tickers,
        decision_date,
        next_decision_date,
        store,
        initial_capital=baseline_capital,
        transaction_costs=transaction_costs,
        tax_policy=tax_policy,
    )
    missing = tuple(
        [f"regime:{item}" for item in regime_result.missing]
        + [f"baseline:{item}" for item in baseline_result.missing]
    )
    complete = regime_result.net_return is not None and baseline_result.net_return is not None
    entry_dates = [r.entry_date for r in (regime_result, baseline_result) if r.entry_date]
    exit_dates = [r.exit_date for r in (regime_result, baseline_result) if r.exit_date]
    return BacktestPeriod(
        **base_kwargs,
        entry_date=min(entry_dates).isoformat() if entry_dates else None,
        exit_date=max(exit_dates).isoformat() if exit_dates else None,
        regime_selected=regime_tickers,
        baseline_selected=baseline_tickers,
        regime_return=regime_result.net_return,
        baseline_return=baseline_result.net_return,
        status="complete" if complete else "skipped_missing_prices",
        missing=missing,
        regime_gross_return=regime_result.gross_return,
        baseline_gross_return=baseline_result.gross_return,
        regime_transaction_costs=regime_result.transaction_costs,
        baseline_transaction_costs=baseline_result.transaction_costs,
        regime_taxes=regime_result.taxes,
        baseline_taxes=baseline_result.taxes,
        regime_turnover=regime_result.turnover,
        baseline_turnover=baseline_result.turnover,
        regime_commission=regime_result.commission,
        baseline_commission=baseline_result.commission,
        regime_slippage=regime_result.slippage,
        baseline_slippage=baseline_result.slippage,
        regime_fixed_fees=regime_result.fixed_fees,
        baseline_fixed_fees=baseline_result.fixed_fees,
    )


def _eligible_rows(rows: list[CompositeRow]) -> list[CompositeRow]:
    return [
        row
        for row in rows
        if row.quality_score is not None and row.value_score is not None
    ]


def _rank_rows(
    rows: list[CompositeRow], weights: FactorWeights
) -> list[tuple[float, CompositeRow]]:
    scored = [
        (weights.quality * row.quality_score + weights.value * row.value_score, row)
        for row in rows
        if row.quality_score is not None and row.value_score is not None
    ]
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


def _quarterly_decision_dates(store: Store, start: str, end: str) -> list[date]:
    rows = store.query(
        """
        SELECT DISTINCT date
        FROM prices
        WHERE date >= ? AND date <= ?
        ORDER BY date
        """,
        (start, end),
    )
    quarter_ends: dict[tuple[int, int], date] = {}
    for row in rows:
        observation_date = row["date"]
        key = (observation_date.year, (observation_date.month - 1) // 3 + 1)
        quarter_ends[key] = max(quarter_ends.get(key, observation_date), observation_date)
    return sorted(quarter_ends.values())


def _normalize_tickers(tickers: list[str] | None, store: Store) -> list[str]:
    if tickers is None:
        rows = store.query(
            "SELECT ticker FROM securities WHERE is_active = TRUE ORDER BY ticker"
        )
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
) -> BacktestMetrics:
    completed = [
        (period, getattr(period, field_name))
        for period in periods
        if getattr(period, field_name) is not None
    ]
    if not completed:
        return BacktestMetrics(0, None, None, None, None, None)
    returns = [float(value) for _, value in completed]
    gross_returns = [
        float(getattr(period, gross_field))
        for period, _ in completed
        if getattr(period, gross_field) is not None
    ]
    return _metrics_from_returns(
        completed,
        returns,
        gross_returns,
        sum(float(getattr(period, costs_field)) for period, _ in completed),
        sum(float(getattr(period, taxes_field)) for period, _ in completed),
        sum(float(getattr(period, turnover_field)) for period, _ in completed),
    )


def _compute_observation_metrics(
    observations: list[tuple[date, date, float]],
) -> BacktestMetrics:
    if not observations:
        return BacktestMetrics(0, None, None, None, None, None)
    returns = [observation[2] for observation in observations]
    periods = [
        (entry_date, exit_date, value)
        for entry_date, exit_date, value in observations
    ]
    return _metrics_from_returns(periods, returns, returns, 0.0, 0.0, 0.0)


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

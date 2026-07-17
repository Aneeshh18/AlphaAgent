"""CLI entrypoint — `aios <command>`.

The single command surface for the foundation phase. Every daily job runs
through here. Designed to be wired to systemd/cron later.

Commands
--------
  aios doctor        — verify env, deps, DB, connectivity
  aios ingest-macro  — pull FRED + Treasury macro series
  aios build-universe-membership — reconcile spans with public change events
  aios import-universe — import PIT historical universe membership CSV
  aios build-security-identities — assign stable IDs to certified intervals
  aios import-security-identities — import audited stable identity assignments
  aios import-reference-identities — import issuer/CIK/provider mappings
  aios universe-coverage — audit member-level price and PIT fundamental coverage
  aios macro-regime  — classify the release-aware macro regime for a date
  aios backtest-qv   — validate regime weights against a fixed 60/40 policy
  aios ingest-ticker — pull EDGAR fundamentals + prices for one ticker
  aios ingest-batch  — pull many tickers (reads tickers.txt)
  aios status        — show row counts + latest dates per table
  aios audit         — show recent ingest outcomes
  aios validate      — run read-only data quality checks
  aios cleanup-legacy-ebitda — remove known-invalid legacy EBITDA rows
  aios cleanup-legacy-macro — remove replaced, unversioned macro copies
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Annotated

import duckdb
import typer
from rich.console import Console
from rich.table import Table

from aios import __version__
from aios.config import settings
from aios.storage.store import get_store

app = typer.Typer(
    name="aios",
    help="AI Investment Operating System — in-house build (Path B).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
TICKERS_FILE_ARGUMENT = typer.Argument(..., help="Text file, one ticker per line")
UNIVERSE_FILE_ARGUMENT = typer.Argument(
    ..., help="CSV with effective and known membership dates."
)
SECURITY_IDENTITY_FILE_ARGUMENT = typer.Argument(
    ..., help="Audited security identity assignment CSV."
)


@app.command()
def doctor() -> None:
    """Verify environment, dependencies, DB, and live connectivity."""
    console.rule(f"[bold]AI Investment OS v{__version__} — doctor[/bold]")
    ok = True

    # Env
    console.print(f"[cyan]SEC User-Agent:[/cyan] {settings.sec_user_agent}")
    console.print(f"[cyan]FRED API key set:[/cyan] {bool(settings.fred_api_key)}")
    console.print(f"[cyan]DuckDB path:[/cyan] {settings.duckdb_path}")

    # Deps
    deps = ("duckdb", "httpx", "tenacity", "pandas", "pydantic", "structlog", "typer", "rich")
    tbl = Table(title="Dependencies")
    tbl.add_column("package")
    tbl.add_column("status")
    for d in deps:
        try:
            __import__(d)
            tbl.add_row(d, "[green]ok[/green]")
        except ImportError:
            tbl.add_row(d, "[red]MISSING[/red]")
            ok = False
    console.print(tbl)

    # Optional deps
    opt = Table(title="Optional data-source deps")
    opt.add_column("package")
    opt.add_column("status")
    for d in ("yfinance", "fredapi", "pyarrow", "polars"):
        try:
            __import__(d)
            opt.add_row(d, "[green]ok[/green]")
        except ImportError:
            opt.add_row(d, "[yellow]not installed (needed later)[/yellow]")
    console.print(opt)

    # DB
    try:
        counts = get_store().table_rowcounts()
        db_tbl = Table(title="Database")
        db_tbl.add_column("table")
        db_tbl.add_column("rows")
        for t, n in counts.items():
            db_tbl.add_row(t, str(n))
        console.print(db_tbl)
    except Exception as e:
        console.print(f"[red]DB init failed:[/red] {e}")
        ok = False

    console.print()
    console.print(
        "[bold green]ALL GOOD[/bold green]"
        if ok
        else "[bold red]ISSUES FOUND — fix above[/bold red]"
    )
    sys.exit(0 if ok else 1)


@app.command("ingest-macro")
def ingest_macro(
    series_ids: Annotated[
        list[str] | None,
        typer.Option(
            "--series",
            help="Only fetch these FRED series; repeat the option for multiple IDs.",
        ),
    ] = None,
) -> None:
    """Pull macro series (FRED + Treasury)."""
    from aios.ingest.fred import MacroIngestError
    from aios.ingest.fred import ingest_macro as _run

    ids = [series_id.upper() for series_id in series_ids] if series_ids else None
    try:
        n = _run(ids)
    except MacroIngestError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Macro ingest done:[/green] {n} rows upserted.")


@app.command("import-universe")
def import_universe(
    path: Path = UNIVERSE_FILE_ARGUMENT,
    universe_id: str | None = typer.Option(
        None,
        "--universe-id",
        help="Default universe ID when the CSV does not include one.",
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        help="Provenance label; defaults to csv:<filename>.",
    ),
) -> None:
    """Import historical membership with release/known-date protection."""
    from aios.ingest.universe import ingest_membership_csv

    try:
        n = ingest_membership_csv(
            path,
            universe_id=universe_id,
            source=source,
        )
    except duckdb.IOException as exc:
        console.print(
            "[red]Universe import could not open DuckDB.[/red] "
            "Close Streamlit and any other AIOS command using the database, then retry."
        )
        raise typer.Exit(code=1) from exc
    except (OSError, ValueError) as exc:
        console.print(f"[red]Universe import refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Universe import done:[/green] {n} rows upserted.")


@app.command("build-universe-membership")
def build_universe_membership(
    baseline_spans: Annotated[
        Path,
        typer.Option(
            "--baseline-spans",
            help="Reference CSV with ticker,start_date,end_date.",
        ),
    ],
    events: Annotated[
        Path,
        typer.Option(
            "--events",
            help="Audited event CSV with effective_date, action, known_date, and source.",
        ),
    ],
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            help="Destination for the validated import-ready membership CSV.",
        ),
    ],
    start: Annotated[str, typer.Option(help="Certified baseline date, YYYY-MM-DD.")],
    end: Annotated[str, typer.Option(help="Last certified membership date, YYYY-MM-DD.")],
    baseline_source: Annotated[
        str,
        typer.Option(
            "--baseline-source",
            help="Immutable URL/version identifying the baseline reference file.",
        ),
    ],
    universe_id: Annotated[str, typer.Option("--universe-id")] = "sp500",
    require_official_sources: Annotated[
        bool,
        typer.Option(
            "--require-official-sources/--allow-nonofficial-sources",
            help="Require HTTPS issuer sources and S&P-hosted index announcements.",
        ),
    ] = True,
) -> None:
    """Build bounded PIT membership after reconciling every event boundary."""
    from aios.ingest.universe import (
        build_membership_from_events,
        load_effective_spans_csv,
        load_universe_events_csv,
        reconcile_event_boundaries,
        write_membership_csv,
    )

    try:
        coverage_start = date.fromisoformat(start)
        coverage_end = date.fromisoformat(end)
        spans = load_effective_spans_csv(baseline_spans)
        event_rows = load_universe_events_csv(
            events,
            universe_id=universe_id,
            require_official_sources=require_official_sources,
        )
        reconciliation = reconcile_event_boundaries(
            spans,
            event_rows,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        )
        rows = build_membership_from_events(
            spans,
            event_rows,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            universe_id=universe_id,
            baseline_source=baseline_source,
        )
        write_membership_csv(output, rows)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Universe build refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]Universe build done:[/green] {len(rows)} intervals written to {output}."
    )
    console.print(
        f"Certified window: {coverage_start} through {coverage_end}; "
        "rows close after the certified end date."
    )
    if reconciliation.date_conflicts:
        console.print(
            f"[yellow]Reference-date conflicts:[/yellow] "
            f"{len(reconciliation.date_conflicts)}; official event dates were used."
        )


@app.command("build-security-identities")
def build_security_identities(
    membership: Annotated[
        Path,
        typer.Option(
            "--membership",
            help="Certified universe membership CSV.",
        ),
    ],
    transitions: Annotated[
        Path,
        typer.Option(
            "--transitions",
            help="Verified same-security ticker transition CSV.",
        ),
    ],
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            help="Destination for the audited identity assignment CSV.",
        ),
    ],
    universe_id: Annotated[str, typer.Option("--universe-id")] = "sp500",
) -> None:
    """Build stable IDs without joining ordinary replacements or mergers."""
    from aios.ingest.security_identity import build_security_identity_csv

    try:
        rows = build_security_identity_csv(
            membership,
            transitions,
            output,
            universe_id=universe_id,
        )
    except (OSError, ValueError) as exc:
        console.print(f"[red]Security identity build refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    verified = sum(row["identity_status"] != "bounded_ticker" for row in rows)
    console.print(
        f"[green]Security identity build done:[/green] {len(rows)} interval "
        f"assignments written to {output}."
    )
    console.print(
        f"Verified transition intervals: {verified}; "
        f"bounded ticker intervals: {len(rows) - verified}."
    )


@app.command("import-security-identities")
def import_security_identities(
    path: Path = SECURITY_IDENTITY_FILE_ARGUMENT,
) -> None:
    """Import stable security IDs and attach them to universe intervals."""
    from aios.ingest.security_identity import ingest_security_identity_csv

    try:
        n = ingest_security_identity_csv(path)
    except duckdb.IOException as exc:
        console.print(
            "[red]Security identity import could not open DuckDB.[/red] "
            "Close Streamlit and any other AIOS command using the database, then retry."
        )
        raise typer.Exit(code=1) from exc
    except (OSError, ValueError) as exc:
        console.print(f"[red]Security identity import refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Security identity import done:[/green] {n} rows upserted.")


@app.command("import-reference-identities")
def import_reference_identities(
    issuer_ciks: Annotated[
        Path,
        typer.Option("--issuer-ciks", help="Reviewed issuer and SEC CIK CSV."),
    ],
    security_issuers: Annotated[
        Path,
        typer.Option(
            "--security-issuers",
            help="Reviewed security-to-issuer assignment CSV.",
        ),
    ],
    provider_symbols: Annotated[
        Path,
        typer.Option(
            "--provider-symbols",
            help="Reviewed bounded provider-symbol CSV.",
        ),
    ],
) -> None:
    """Import separate issuer, CIK, security-owner, and provider identities."""
    from aios.ingest.reference_identity import ingest_reference_identity_csvs

    try:
        counts = ingest_reference_identity_csvs(
            issuer_ciks,
            security_issuers,
            provider_symbols,
        )
    except duckdb.IOException as exc:
        console.print(
            "[red]Reference identity import could not open DuckDB.[/red] "
            "Close Streamlit and any other AIOS command using the database, then retry."
        )
        raise typer.Exit(code=1) from exc
    except (OSError, ValueError) as exc:
        console.print(f"[red]Reference identity import refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        "[green]Reference identity import done:[/green] "
        f"{counts['issuers']} issuers, {counts['cik_history']} CIK intervals, "
        f"{counts['security_issuers']} owner intervals, and "
        f"{counts['provider_symbols']} provider intervals upserted."
    )


@app.command("universe-coverage")
def universe_coverage(
    universe_id: Annotated[str, typer.Option("--universe-id")] = "sp500",
    as_of: Annotated[
        str | None,
        typer.Option(help="Decision date, YYYY-MM-DD; defaults to today."),
    ] = None,
    missing_output: Annotated[
        Path | None,
        typer.Option(
            "--missing-output",
            help="Optional text file for members missing prices or PIT fundamentals.",
        ),
    ] = None,
) -> None:
    """Audit data coverage without pretending aliases are interchangeable."""
    decision_date = as_of or date.today().isoformat()
    try:
        date.fromisoformat(decision_date)
        rows = get_store().universe_data_coverage(universe_id, decision_date)
    except ValueError as exc:
        console.print(f"[red]Coverage audit refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if not rows:
        console.print(
            f"[red]No PIT members for {universe_id!r} on {decision_date}.[/red] "
            "Check the certified universe window."
        )
        raise typer.Exit(code=1)

    price_count = sum(bool(row["has_price_history"]) for row in rows)
    fundamental_count = sum(bool(row["has_pit_fundamentals"]) for row in rows)
    complete = [
        row
        for row in rows
        if row["has_price_history"] and row["has_pit_fundamentals"]
    ]
    missing = [row for row in rows if row not in complete]
    summary = Table(title=f"{universe_id} data coverage on {decision_date}")
    summary.add_column("members", justify="right")
    summary.add_column("stable IDs", justify="right")
    summary.add_column("price history", justify="right")
    summary.add_column("PIT fundamentals", justify="right")
    summary.add_column("both", justify="right")
    summary.add_row(
        str(len(rows)),
        str(sum(bool(row["security_id"]) for row in rows)),
        str(price_count),
        str(fundamental_count),
        str(len(complete)),
    )
    console.print(summary)

    if missing:
        console.print(f"[yellow]Members missing one or both inputs:[/yellow] {len(missing)}")
        for row in missing[:25]:
            gaps = []
            if not row["has_price_history"]:
                gaps.append("prices")
            if not row["has_pit_fundamentals"]:
                gaps.append("fundamentals")
            console.print(f"  {row['ticker']}: {', '.join(gaps)}")
        if len(missing) > 25:
            console.print(f"  … and {len(missing) - 25} more")
    if missing_output is not None:
        missing_output.parent.mkdir(parents=True, exist_ok=True)
        missing_output.write_text(
            "\n".join(row["ticker"] for row in missing) + ("\n" if missing else ""),
            encoding="utf-8",
        )
        console.print(
            f"Missing-member review list written to {missing_output}. "
            "Do not bulk-ingest it before provider-symbol and CIK review."
        )


@app.command("macro-regime")
def macro_regime(
    as_of: str | None = typer.Option(
        None,
        help="Decision date (YYYY-MM-DD); defaults to today.",
    ),
) -> None:
    """Classify the macro regime using only vintages known by the date."""
    from aios.macro.regime import compute_regime

    decision_date = as_of or date.today().isoformat()
    snapshot = compute_regime(decision_date)
    console.rule(f"[bold]Macro regime as of {snapshot.as_of}[/bold]")
    console.print(f"[bold cyan]Regime:[/bold cyan] {snapshot.regime}")

    states = Table(title="State components")
    states.add_column("component")
    states.add_column("state")
    for component, state in (
        ("growth", snapshot.growth_state),
        ("inflation", snapshot.inflation_state),
        ("yield curve", snapshot.curve_state),
        ("stress", snapshot.stress_state),
    ):
        states.add_row(component, state)
    console.print(states)

    release_key = {
        "growth_pct": "growth",
        "inflation_yoy_pct": "inflation_latest",
        "curve_spread_pct": "curve",
        "vix": "vix",
        "credit_spread_pct": "credit",
    }
    evidence = Table(title="PIT evidence")
    evidence.add_column("metric")
    evidence.add_column("value")
    evidence.add_column("release date")
    for metric, value in snapshot.metrics.items():
        evidence.add_row(
            metric,
            str(value),
            str(snapshot.release_dates.get(release_key[metric])),
        )
    console.print(evidence)

    if snapshot.is_pit_ready:
        console.print("[green]PIT-ready: all mandatory inputs have release dates.[/green]")
    else:
        missing = ", ".join(snapshot.missing) or "evidence"
        console.print(f"[yellow]Not PIT-ready; missing: {missing}[/yellow]")


@app.command("backtest-qv")
def backtest_qv(
    start: str = typer.Option(..., help="First decision-date range, YYYY-MM-DD."),
    end: str = typer.Option(..., help="Last decision-date range, YYYY-MM-DD."),
    top_n: int = typer.Option(10, min=1, help="Equal-weight holdings selected per policy."),
    tickers: Annotated[
        list[str] | None,
        typer.Option(
            "--ticker",
            help="Limit the universe; repeat for multiple tickers. Defaults to active securities.",
        ),
    ] = None,
    require_pit_regime: bool = typer.Option(
        True,
        "--require-pit-regime/--allow-unknown-regime",
        help="Skip periods without release-aware macro evidence (recommended).",
    ),
    universe_id: str | None = typer.Option(
        None,
        "--universe-id",
        help="PIT historical universe ID; required unless --allow-current-universe is used.",
    ),
    allow_current_universe: bool = typer.Option(
        False,
        "--allow-current-universe",
        help="Explicitly allow current active securities (survivorship-biased diagnostic).",
    ),
    benchmarks: Annotated[
        list[str] | None,
        typer.Option(
            "--benchmark",
            help="Benchmark ticker; repeat for multiple benchmarks (e.g. SPY).",
        ),
    ] = None,
    initial_capital: float = typer.Option(
        100_000.0,
        min=1.0,
        help="Notional capital used to convert fixed fees and taxes into returns.",
    ),
    commission_bps: float = typer.Option(
        5.0,
        min=0.0,
        help="Commission assumption per side, in basis points.",
    ),
    slippage_bps: float = typer.Option(
        5.0,
        min=0.0,
        help="Slippage assumption per side, in basis points.",
    ),
    fixed_fee: float = typer.Option(
        0.0,
        min=0.0,
        help="Fixed currency fee per order.",
    ),
    short_term_tax_rate: float = typer.Option(
        0.0,
        min=0.0,
        max=1.0,
        help="Short-term realized-gain tax rate as a decimal (0.20 = 20%).",
    ),
    long_term_tax_rate: float = typer.Option(
        0.0,
        min=0.0,
        max=1.0,
        help="Long-term realized-gain tax rate as a decimal.",
    ),
    dividend_tax_rate: float = typer.Option(
        0.0,
        min=0.0,
        max=1.0,
        help="Dividend tax rate as a decimal.",
    ),
) -> None:
    """Compare QV policies after explicit costs/taxes and against benchmarks."""
    from aios.backtest import TaxPolicy, TransactionCostPolicy, run_qv_policy_backtest

    try:
        result = run_qv_policy_backtest(
            start,
            end,
            tickers=tickers,
            top_n=top_n,
            require_pit_regime=require_pit_regime,
            universe_id=universe_id,
            allow_current_universe=allow_current_universe,
            benchmark_tickers=benchmarks,
            initial_capital=initial_capital,
            transaction_costs=TransactionCostPolicy(
                commission_bps=commission_bps,
                slippage_bps=slippage_bps,
                fixed_fee=fixed_fee,
            ),
            tax_policy=TaxPolicy(
                short_term_rate=short_term_tax_rate,
                long_term_rate=long_term_tax_rate,
                dividend_rate=dividend_tax_rate,
            ),
        )
    except ValueError as exc:
        console.print(f"[red]Backtest refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.rule(f"[bold]QV policy backtest: {start} → {end}[/bold]")
    console.print(
        f"Universe: {len(result.tickers)} tickers • quarterly • top {top_n} equal-weight • "
        f"comparison periods: {result.comparison_periods}"
    )
    console.print(
        "Assumptions: "
        f"commission {commission_bps:.2f} bps/side • "
        f"slippage {slippage_bps:.2f} bps/side • fixed fee {fixed_fee:.2f} • "
        f"tax rates ST/LT/dividend {short_term_tax_rate:.2%}/"
        f"{long_term_tax_rate:.2%}/{dividend_tax_rate:.2%}"
    )

    def _pct(value: float | None) -> str:
        return "N/A" if value is None else f"{value * 100:.2f}%"

    summary = Table(title="Net policy comparison")
    summary.add_column("policy")
    summary.add_column("periods", justify="right")
    summary.add_column("cumulative", justify="right")
    summary.add_column("gross cumulative", justify="right")
    summary.add_column("annualized", justify="right")
    summary.add_column("volatility", justify="right")
    summary.add_column("max drawdown", justify="right")
    summary.add_column("win rate", justify="right")
    summary.add_column("costs", justify="right")
    summary.add_column("taxes", justify="right")
    summary.add_column("turnover", justify="right")
    for name, metrics in (
        ("regime-aware", result.regime_metrics),
        ("baseline 60/40", result.baseline_metrics),
    ):
        summary.add_row(
            name,
            str(metrics.completed_periods),
            _pct(metrics.cumulative_return),
            _pct(metrics.gross_cumulative_return),
            _pct(metrics.annualized_return),
            _pct(metrics.annualized_volatility),
            _pct(metrics.max_drawdown),
            _pct(metrics.win_rate),
            f"{metrics.total_transaction_costs:,.2f}",
            f"{metrics.total_taxes:,.2f}",
            f"{metrics.total_turnover:.2f}x",
        )
    console.print(summary)

    if result.benchmark_metrics:
        benchmark_table = Table(
            title="Benchmark total returns (adjusted close; no strategy costs/taxes)"
        )
        benchmark_table.add_column("benchmark")
        benchmark_table.add_column("periods", justify="right")
        benchmark_table.add_column("cumulative", justify="right")
        benchmark_table.add_column("annualized", justify="right")
        for ticker, metrics in result.benchmark_metrics.items():
            benchmark_table.add_row(
                ticker,
                str(metrics.completed_periods),
                _pct(metrics.cumulative_return),
                _pct(metrics.annualized_return),
            )
        console.print(benchmark_table)

    skipped = [period for period in result.periods if period.status != "complete"]
    if skipped:
        console.print(f"[yellow]Skipped periods:[/yellow] {len(skipped)}")
        for period in skipped[:5]:
            console.print(
                f"  {period.decision_date}: {period.status}"
                + (f" ({', '.join(period.missing)})" if period.missing else "")
            )
        if len(skipped) > 5:
            console.print(f"  … and {len(skipped) - 5} more")
    for warning in result.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")


def _ingest_one(
    ticker: str,
    with_prices: bool = True,
    with_fundamentals: bool = True,
    cik_map: dict[str, int] | None = None,
) -> None:
    """Shared ingest logic — callable outside Typer command context."""
    ticker = ticker.upper()
    console.rule(f"[bold]Ingest {ticker}[/bold]")
    if with_fundamentals:
        from aios.ingest.edgar import ingest_ticker as _edgar

        try:
            n = _edgar(ticker, cik_map=cik_map)
            console.print(f"[green]Fundamentals:[/green] {n} rows")
        except Exception as e:
            console.print(f"[red]EDGAR failed:[/red] {e}")
    if with_prices:
        from aios.ingest.prices import ingest_prices

        try:
            import time

            time.sleep(settings.yfinance_sleep_sec)
            n = ingest_prices(ticker)
            console.print(f"[green]Prices:[/green] {n} rows")
        except Exception as e:
            console.print(f"[red]Prices failed:[/red] {e}")


@app.command("ingest-ticker")
def ingest_ticker(
    ticker: str = typer.Argument(..., help="Ticker, e.g. AAPL"),
    with_prices: bool = typer.Option(True, "--prices/--no-prices"),
    with_fundamentals: bool = typer.Option(True, "--fundamentals/--no-fundamentals"),
) -> None:
    """Pull EDGAR fundamentals + yfinance prices for one ticker."""
    _ingest_one(ticker, with_prices=with_prices, with_fundamentals=with_fundamentals)


@app.command("ingest-issuer")
def ingest_issuer(
    issuer_id: str = typer.Argument(..., help="Reviewed issuer_id, not a ticker."),
) -> None:
    """Pull SEC fundamentals through reviewed issuer/CIK history."""
    from aios.ingest.edgar import ingest_issuer as _ingest_issuer

    try:
        n = _ingest_issuer(issuer_id)
    except Exception as exc:
        console.print(f"[red]Issuer ingest failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Fundamentals:[/green] {n} issuer-tagged rows")


@app.command("ingest-security-prices")
def ingest_security_prices(
    security_id: str = typer.Argument(..., help="Reviewed immutable security_id."),
    provider: Annotated[
        str | None,
        typer.Option(help="Reviewed provider (defaults to yfinance when available)."),
    ] = None,
    start: Annotated[
        str | None,
        typer.Option(help="Optional inclusive start date, YYYY-MM-DD."),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option(help="Optional exclusive end date, YYYY-MM-DD."),
    ] = None,
) -> None:
    """Pull prices through bounded symbols and relabel every row by date."""
    from aios.ingest.prices import ingest_security_prices as _ingest_security_prices

    try:
        if start:
            date.fromisoformat(start)
        if end:
            date.fromisoformat(end)
        n = _ingest_security_prices(
            security_id,
            provider=provider,
            start=start,
            end=end,
        )
    except Exception as exc:
        console.print(f"[red]Security price ingest failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Prices:[/green] {n} identity-tagged rows")


@app.command("ingest-batch")
def ingest_batch(
    tickers_file: Path = TICKERS_FILE_ARGUMENT,
) -> None:
    """Pull fundamentals + prices for every ticker in a file."""
    import time

    tickers = [
        t.strip().upper()
        for t in tickers_file.read_text().splitlines()
        if t.strip() and not t.startswith("#")
    ]
    console.print(f"Batch ingesting {len(tickers)} tickers...")
    cik_map = None
    if tickers:
        from aios.ingest.edgar import load_ticker_cik_map

        try:
            # One SEC map fetch for the whole batch, not one per ticker.
            cik_map = load_ticker_cik_map()
        except Exception as e:
            console.print(f"[yellow]SEC ticker map unavailable:[/yellow] {e}")
            # Continue with prices; fundamentals will be marked failed by the
            # per-ticker audit path without repeatedly retrying the map.
            cik_map = {}
    for i, t in enumerate(tickers, 1):
        console.rule(f"[{i}/{len(tickers)}] {t}")
        try:
            _ingest_one(t, with_prices=True, with_fundamentals=True, cik_map=cik_map)
        except Exception as e:
            console.print(f"[red]{t} failed:[/red] {e}")
        time.sleep(settings.yfinance_sleep_sec)


@app.command()
def status() -> None:
    """Show row counts + latest dates per table."""
    s = get_store()
    counts = s.table_rowcounts()
    tbl = Table(title="Storage status")
    tbl.add_column("table")
    tbl.add_column("rows")
    for t, n in counts.items():
        tbl.add_row(t, str(n))
    console.print(tbl)

    # Latest dates where it makes sense
    for table, col in (("prices", "date"), ("fundamentals", "as_of_date"), ("macro", "date")):
        try:
            row = s.query(f"SELECT MAX({col}) AS latest FROM {table}")[0]
            console.print(f"[cyan]Latest {table}.{col}:[/cyan] {row['latest']}")
        except Exception:
            pass


@app.command()
def audit(
    limit: int = typer.Option(20, min=1, max=200, help="Number of recent runs to show"),
) -> None:
    """Show recent ingest outcomes and errors."""
    rows = get_store().ingest_history(limit)
    if not rows:
        console.print("[yellow]No ingest runs have been recorded yet.[/yellow]")
        return

    tbl = Table(title=f"Recent ingest runs (latest {len(rows)})")
    for column in ("id", "source", "table_name", "rows_inserted", "status", "finished_at"):
        tbl.add_column(column)
    for row in rows:
        status = row["status"] or "unknown"
        color = "green" if status == "success" else "red"
        tbl.add_row(
            str(row["id"]),
            str(row["source"]),
            str(row["table_name"]),
            str(row["rows_inserted"] or 0),
            f"[{color}]{status}[/{color}]",
            str(row["finished_at"]),
        )
        if row["error"]:
            console.print(f"[red]run {row['id']} error:[/red] {row['error']}")
    console.print(tbl)


@app.command()
def validate() -> None:
    """Run read-only data quality checks before analysis or re-ingest."""
    report = get_store().data_quality_report()
    tbl = Table(title="Data quality")
    tbl.add_column("check")
    tbl.add_column("status")
    tbl.add_column("count", justify="right")
    tbl.add_column("detail")
    has_failure = False
    for row in report:
        status = row["status"]
        if status == "fail":
            has_failure = True
        color = {"ok": "green", "warn": "yellow", "fail": "red"}.get(status, "white")
        tbl.add_row(
            row["check"],
            f"[{color}]{status}[/{color}]",
            str(row["count"]),
            row["detail"],
        )
    console.print(tbl)
    if has_failure:
        raise typer.Exit(code=1)


@app.command("cleanup-legacy-ebitda")
def cleanup_legacy_ebitda(
    ticker: str | None = typer.Option(None, help="Limit cleanup to one ticker"),
) -> None:
    """Remove only the pre-correction, mislabeled EBITDA rows."""
    removed = get_store().purge_legacy_ebitda(ticker)
    scope = ticker.upper() if ticker else "all tickers"
    console.print(f"[green]Removed {removed} legacy EBITDA rows for {scope}.[/green]")


@app.command("cleanup-legacy-macro")
def cleanup_legacy_macro(
    series_ids: Annotated[
        list[str] | None,
        typer.Option(
            "--series",
            help="Only clean these series; repeat the option for multiple IDs.",
        ),
    ] = None,
) -> None:
    """Remove legacy macro copies only after replacement coverage is complete."""
    ids = [series_id.upper() for series_id in series_ids] if series_ids else None
    try:
        removed = get_store().purge_legacy_macro(ids)
    except ValueError as exc:
        console.print(f"[red]Macro cleanup refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    scope = ", ".join(ids) if ids else "all series"
    console.print(f"[green]Removed {removed} replaced legacy macro rows for {scope}.[/green]")


if __name__ == "__main__":
    app()

"""CLI entrypoint — `aios <command>`.

The single command surface for the foundation phase. Every daily job runs
through here. Designed to be wired to systemd/cron later.

Commands
--------
  aios doctor        — verify env, deps, DB, connectivity
  aios ingest-macro  — pull FRED + Treasury macro series
  aios ingest-ticker — pull EDGAR fundamentals + prices for one ticker
  aios ingest-batch  — pull many tickers (reads tickers.txt)
  aios status        — show row counts + latest dates per table
"""

from __future__ import annotations

import sys
from pathlib import Path

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
    tbl.add_column("package"); tbl.add_column("status")
    for d in deps:
        try:
            __import__(d)
            tbl.add_row(d, "[green]ok[/green]")
        except ImportError:
            tbl.add_row(d, "[red]MISSING[/red]"); ok = False
    console.print(tbl)

    # Optional deps
    opt = Table(title="Optional data-source deps")
    opt.add_column("package"); opt.add_column("status")
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
        db_tbl.add_column("table"); db_tbl.add_column("rows")
        for t, n in counts.items():
            db_tbl.add_row(t, str(n))
        console.print(db_tbl)
    except Exception as e:
        console.print(f"[red]DB init failed:[/red] {e}"); ok = False

    console.print()
    console.print("[bold green]ALL GOOD[/bold green]" if ok else "[bold red]ISSUES FOUND — fix above[/bold red]")
    sys.exit(0 if ok else 1)


@app.command("ingest-macro")
def ingest_macro() -> None:
    """Pull macro series (FRED + Treasury)."""
    from aios.ingest.fred import ingest_macro as _run
    n = _run()
    console.print(f"[green]Macro ingest done:[/green] {n} rows upserted.")


def _ingest_one(ticker: str, with_prices: bool = True, with_fundamentals: bool = True) -> None:
    """Shared ingest logic — callable outside Typer command context."""
    ticker = ticker.upper()
    console.rule(f"[bold]Ingest {ticker}[/bold]")
    if with_fundamentals:
        from aios.ingest.edgar import ingest_ticker as _edgar
        try:
            n = _edgar(ticker)
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


@app.command("ingest-batch")
def ingest_batch(
    tickers_file: Path = typer.Argument(..., help="Text file, one ticker per line"),
) -> None:
    """Pull fundamentals + prices for every ticker in a file."""
    import time
    tickers = [t.strip().upper() for t in tickers_file.read_text().splitlines() if t.strip() and not t.startswith("#")]
    console.print(f"Batch ingesting {len(tickers)} tickers...")
    for i, t in enumerate(tickers, 1):
        console.rule(f"[{i}/{len(tickers)}] {t}")
        try:
            _ingest_one(t, with_prices=True, with_fundamentals=True)
        except Exception as e:
            console.print(f"[red]{t} failed:[/red] {e}")
        time.sleep(settings.yfinance_sleep_sec)


@app.command()
def status() -> None:
    """Show row counts + latest dates per table."""
    s = get_store()
    counts = s.table_rowcounts()
    tbl = Table(title="Storage status")
    tbl.add_column("table"); tbl.add_column("rows")
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


if __name__ == "__main__":
    app()

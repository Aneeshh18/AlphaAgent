"""CLI entrypoint — `aios <command>`.

The single command surface for the foundation phase. Every daily job runs
through here. Designed to be wired to systemd/cron later.

Commands
--------
  aios doctor        — verify env, deps, DB, connectivity
  aios readiness     — fail-closed U.S. data/readiness report
  aios health        — one plain-language operating check
  aios backup        — checksum-verified local data/paper backup
  aios verify-backup — verify a backup without restoring it
  aios restore       — confirmed recovery with automatic rollback backup
  aios scheduler-install — enable local refresh/health/backup timers
  aios scheduler-status — show whether local timers are enabled and waiting
  aios scheduler-pause/resume — safely stop/start local timers
  aios scheduler-remove — disable and remove only AIOS-managed timer files
  aios dashboard     — open the local research dashboard
  aios paper-init    — create a local simulation-only portfolio
  aios forward-freeze — freeze policy/configuration for untouched monitoring
  aios forward-status — verify the active forward policy has not drifted
  aios paper-propose — build a reviewed QV simulation proposal
  aios paper-execute — explicitly simulate an approved proposal
  aios paper-mark    — update simulated holdings through a reviewed close
  aios paper-status  — show local simulation state in plain language
  aios refresh-us-current — refresh reviewed U.S. prices, filings, and macro
  aios ingest-macro  — pull FRED + Treasury macro series
  aios build-universe-membership — reconcile spans with public change events
  aios import-universe — import PIT historical universe membership CSV
  aios build-security-identities — assign stable IDs to certified intervals
  aios import-security-identities — import audited stable identity assignments
  aios import-reference-identities — import issuer/CIK/provider mappings
  aios import-security-conversions — import reviewed share-for-share events
  aios ingest-liquidation-prices — extend held removed securities to rebalance
  aios build-reference-batch — certify unchanged-ticker issuer/provider mappings
  aios plan-reference-window-batches — plan resumable current-member batches
  aios plan-historical-reference-batches — include removed-member provenance gaps
  aios build-reference-window-batch — certify per-ticker bounded windows
  aios merge-reference-batches — consolidate accepted reviewed batch rows
  aios ingest-reference-batch — import and ingest one certified identity batch
  aios refresh-price-actions — repair reviewed price rows fetched without actions
  aios build-factor-price-warmup — overlap-review pre-membership factor history
  aios ingest-factor-price-warmup — import an accepted factor warm-up batch
  aios universe-coverage — audit member-level price and PIT fundamental coverage
  aios macro-regime  — classify the release-aware macro regime for a date
  aios backtest-qv   — validate QV/QVML regime weights against a fixed baseline
  aios ingest-ticker — pull EDGAR fundamentals + prices for one ticker
  aios ingest-batch  — pull many tickers (reads tickers.txt)
  aios status        — show row counts + latest dates per table
  aios audit         — show recent ingest outcomes
  aios validate      — run read-only data quality checks
  aios cleanup-legacy-ebitda — remove known-invalid legacy EBITDA rows
  aios quarantine-invalid-fundamentals — isolate impossible fiscal-period rows
  aios cleanup-legacy-macro — remove replaced, unversioned macro copies
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import platform
import subprocess
import sys
from contextlib import suppress
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import duckdb
import typer
from rich.console import Console
from rich.table import Table

from aios import __version__
from aios.config import settings
from aios.readiness import assess_us_readiness
from aios.storage.store import get_store

app = typer.Typer(
    name="aios",
    help="AI Investment Operating System — in-house build (Path B).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
TICKERS_FILE_ARGUMENT = typer.Argument(..., help="Text file, one ticker per line")
UNIVERSE_FILE_ARGUMENT = typer.Argument(..., help="CSV with effective and known membership dates.")
SECURITY_IDENTITY_FILE_ARGUMENT = typer.Argument(
    ..., help="Audited security identity assignment CSV."
)
SECURITY_CONVERSION_FILE_ARGUMENT = typer.Argument(
    ..., help="Reviewed share-for-share security conversion CSV."
)
LIQUIDATION_EXTENSION_FILE_ARGUMENT = typer.Argument(
    ..., help="Reviewed post-membership ticker/provider extension CSV."
)


def _project_path(path: Path) -> Path:
    """Resolve a CLI data path consistently when invoked outside the repo."""
    return path if path.is_absolute() else settings.project_root / path


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


@app.command("health")
def health(
    strict: Annotated[
        bool,
        typer.Option(
            "--strict/--report-only",
            help="Return a failing exit code when an operating blocker remains.",
        ),
    ] = True,
) -> None:
    """Show whether normal research and paper monitoring are safe to open."""
    from aios.paper import (
        DEFAULT_ACCOUNT_RELATIVE_PATH,
        latest_paper_decision_date,
        paper_account_summary,
    )

    try:
        store = get_store()
        decision_date = latest_paper_decision_date(store)
        report = assess_us_readiness(decision_date, purpose="paper", store=store)
        account_path = settings.project_root / DEFAULT_ACCOUNT_RELATIVE_PATH
        paper_detail = "not initialized; research remains available"
        paper_ok = True
        if account_path.exists():
            try:
                summary = paper_account_summary(account_path, store)
                paper_detail = (
                    f"verified; ${summary['equity']:,.2f} simulated value, "
                    f"{len(summary['holdings'])} holding(s)"
                )
            except Exception as exc:
                paper_ok = False
                paper_detail = f"blocked; local paper state could not be verified ({exc})"
    except duckdb.IOException as exc:
        console.print(
            "[red]Health check could not open the local database.[/red] Close the "
            "dashboard or another AIOS command, then retry."
        )
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        console.print(f"[red]Health check failed safely:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    integrity = next(check for check in report.checks if check.check == "data_integrity")
    table = Table(title=f"AI Investment OS health — {decision_date}")
    table.add_column("area")
    table.add_column("status")
    table.add_column("plain-language detail")
    table.add_row(
        "U.S. research data",
        "[green]ready[/green]" if report.ready else "[red]blocked[/red]",
        "All current operating gates passed."
        if report.ready
        else f"{len(report.blockers)} blocking gate(s) need attention.",
    )
    table.add_row(
        "Database integrity",
        "[green]safe[/green]" if integrity.status != "fail" else "[red]blocked[/red]",
        integrity.observed,
    )
    table.add_row(
        "Paper simulation",
        "[green]verified[/green]" if paper_ok else "[red]blocked[/red]",
        paper_detail,
    )
    table.add_row(
        "Broker orders",
        "[yellow]disabled[/yellow]",
        "No broker is connected; this installation cannot place an order.",
    )
    console.print(table)
    console.print(
        f"Source dates — prices: {report.raw_prices_through}; filings: "
        f"{report.fundamentals_through}; macro releases: "
        f"{report.macro_releases_through}."
    )
    healthy = report.ready and integrity.status != "fail" and paper_ok
    if healthy:
        console.print(
            "[bold green]HEALTHY for supervised research and paper monitoring.[/bold green]"
        )
    else:
        console.print("[bold red]BLOCKED — do not create or record a paper proposal.[/bold red]")
    if strict and not healthy:
        raise typer.Exit(code=1)


@app.command("backup")
def backup(
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Backup directory; defaults to a timestamped folder under backups/.",
        ),
    ] = None,
) -> None:
    """Create a verified backup of DuckDB and local paper JSON; secrets are excluded."""
    from aios.operations import create_local_backup

    store = None
    try:
        store = get_store()
        store.execute("CHECKPOINT")
        store.close()
        destination = _project_path(output) if output is not None else None
        result = create_local_backup(
            settings.project_root,
            _project_path(settings.duckdb_path),
            output=destination,
            application_version=__version__,
        )
    except duckdb.IOException as exc:
        console.print(
            "[red]Backup could not lock the local database.[/red] Close the dashboard "
            "and any other AIOS command, then retry."
        )
        raise typer.Exit(code=1) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Backup failed safely:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        if store is not None:
            with suppress(Exception):
                store.close()
    console.print(f"[bold green]Verified backup created:[/bold green] {result.path}")
    console.print(
        f"{result.files} file(s), {result.bytes:,} bytes; manifest SHA-256 "
        f"{result.manifest_sha256}."
    )
    console.print("The backup excludes .env, logs, caches, and backtest artifacts.")


@app.command("verify-backup")
def verify_backup(
    path: Annotated[Path, typer.Argument(help="Backup directory to verify.")],
) -> None:
    """Verify every file and checksum in a local AIOS backup."""
    from aios.operations import verify_local_backup

    try:
        result = verify_local_backup(_project_path(path))
    except (OSError, ValueError) as exc:
        console.print(f"[red]Backup verification failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[bold green]Backup verified:[/bold green] {result.path} "
        f"({result.files} file(s), {result.bytes:,} bytes)."
    )


@app.command("restore")
def restore(
    path: Annotated[Path, typer.Argument(help="Verified backup directory to restore.")],
    confirm_restore: Annotated[
        bool,
        typer.Option(
            "--confirm-restore/--no-confirm-restore",
            help="Required acknowledgement that local data and paper state will be replaced.",
        ),
    ] = False,
) -> None:
    """Restore database and paper state after an automatic pre-restore backup."""
    from aios.operations import restore_local_backup
    from aios.storage.store import Store

    store = None
    restored_store = None
    try:
        store = get_store()
        store.execute("CHECKPOINT")
        store.close()
        database_path = _project_path(settings.duckdb_path)
        result = restore_local_backup(
            _project_path(path),
            settings.project_root,
            database_path,
            application_version=__version__,
            confirm=confirm_restore,
        )
        restored_store = Store(database_path)
        failures = [
            row for row in restored_store.data_quality_report() if row["status"] == "fail"
        ]
    except duckdb.IOException as exc:
        console.print(
            "[red]Restore could not lock the local database.[/red] Close the dashboard "
            "and every other AIOS command, then retry."
        )
        raise typer.Exit(code=1) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Restore refused safely:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        for connection in (restored_store, store):
            if connection is not None:
                with suppress(Exception):
                    connection.close()
    console.print(f"[bold green]Restore completed from:[/bold green] {result.source}")
    console.print(f"Automatic pre-restore safety backup: {result.safety_backup}")
    if failures:
        console.print(
            f"[bold red]The restored database has {len(failures)} hard validation "
            "failure(s). Do not use it; retain the safety backup above.[/bold red]"
        )
        raise typer.Exit(code=1)
    console.print(
        "[bold green]The restored database opened with zero hard validation failures.[/bold green]"
    )


@app.command("scheduler-install")
def scheduler_install(
    confirm_install: Annotated[
        bool,
        typer.Option(
            "--confirm-install/--no-confirm-install",
            help="Required acknowledgement before user-level timers are installed.",
        ),
    ] = False,
) -> None:
    """Install weekday refresh, weekly filings, and verified-backup user timers."""
    from aios.scheduler import install_user_scheduler

    try:
        result = install_user_scheduler(
            settings.project_root,
            confirm=confirm_install,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Scheduler installation refused safely:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[bold green]Local scheduler installed:[/bold green] {len(result.timers)} "
        f"timers in {result.unit_dir}."
    )
    console.print(
        "Weekday prices/macro run at 07:30, weekly filings Saturday at 09:00, "
        "and verified backups Sunday at 09:00 (computer local time)."
    )
    console.print(
        "[yellow]Close the dashboard before a scheduled run.[/yellow] DuckDB is a "
        "single-process local store; a collision fails safely and is visible in status/logs."
    )


@app.command("scheduler-status")
def scheduler_status() -> None:
    """Show whether each supported local timer is installed and waiting."""
    from aios.scheduler import user_scheduler_status

    try:
        status = user_scheduler_status()
    except (OSError, RuntimeError) as exc:
        console.print(f"[red]Scheduler status is unavailable:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    table = Table(title="AIOS local scheduler")
    table.add_column("timer")
    table.add_column("enabled")
    table.add_column("state")
    table.add_column("last run")
    table.add_column("next run")
    for timer, state in status.items():
        if not state.get("runtime_verified", True):
            table.add_row(
                timer,
                "yes" if state["enabled"] else "no",
                "not verified",
                "runtime unavailable",
                "runtime unavailable",
            )
            continue
        if state["service_result"] in {"success", "not-run"}:
            last_result = (
                "passed"
                if state["service_result"] == "success" and state["exit_status"] == "0"
                else "not run yet"
            )
        else:
            last_result = (
                f"failed ({state['service_result']}, exit {state['exit_status']})"
            )
        last_event = state.get("last_run", state["last_trigger"])
        last_run = (
            f"{last_result}; {last_event}"
            if last_event != "never"
            else last_result
        )
        table.add_row(
            timer,
            "yes" if state["enabled"] else "no",
            "waiting" if state["active"] else "stopped",
            last_run,
            state["next_trigger"],
        )
    console.print(table)
    if any(not state.get("runtime_verified", True) for state in status.values()):
        console.print(
            "[yellow]The systemd user manager did not answer within 5 seconds.[/yellow] "
            "Installed/enabled files are shown, but live waiting and run times could not "
            "be verified. Try again from your normal logged-in terminal."
        )


@app.command("scheduler-pause")
def scheduler_pause() -> None:
    """Disable local AIOS timers without deleting their configuration."""
    _set_scheduler_active(False)
    console.print("[green]Local AIOS timers are paused.[/green]")


@app.command("scheduler-resume")
def scheduler_resume() -> None:
    """Enable all previously installed local AIOS timers."""
    _set_scheduler_active(True)
    console.print("[green]Local AIOS timers are enabled.[/green]")


def _set_scheduler_active(active: bool) -> None:
    from aios.scheduler import set_user_scheduler_active

    try:
        set_user_scheduler_active(active)
    except (OSError, RuntimeError) as exc:
        console.print(f"[red]Scheduler change failed safely:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("scheduler-remove")
def scheduler_remove(
    confirm_remove: Annotated[
        bool,
        typer.Option(
            "--confirm-remove/--no-confirm-remove",
            help="Required acknowledgement before AIOS-managed timer files are removed.",
        ),
    ] = False,
) -> None:
    """Disable and remove only files previously managed by AIOS."""
    from aios.scheduler import remove_user_scheduler

    try:
        removed = remove_user_scheduler(confirm=confirm_remove)
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Scheduler removal refused safely:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]Local AIOS scheduler removed:[/green] {len(removed)} managed file(s)."
    )


@app.command("dashboard")
def dashboard(
    host: Annotated[
        str,
        typer.Option("--host", help="Network address for the dashboard."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", min=1, max=65535, help="Dashboard port."),
    ] = 8501,
    open_browser: Annotated[
        bool,
        typer.Option(
            "--open-browser/--no-browser",
            help="Open a browser locally; deployments should use --no-browser.",
        ),
    ] = True,
) -> None:
    """Open the research dashboard without requiring Streamlit knowledge."""
    if importlib.util.find_spec("streamlit") is None:
        console.print(
            "[red]Dashboard support is not installed.[/red] Run "
            '`.venv/bin/pip install -e ".[dev,dashboard]"`, then retry.'
        )
        raise typer.Exit(code=1)

    script = settings.project_root / "src" / "aios" / "dashboard.py"
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(script),
        "--server.address",
        host,
        "--server.port",
        str(port),
        "--server.headless",
        str(not open_browser).lower(),
        "--browser.gatherUsageStats",
        "false",
    ]
    console.print(f"[green]Opening AI Investment OS at http://{host}:{port}[/green]")
    completed = subprocess.run(command, cwd=settings.project_root, check=False)
    if completed.returncode:
        raise typer.Exit(code=completed.returncode)


@app.command("readiness")
def readiness(
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", help="Decision date (YYYY-MM-DD); defaults to today."),
    ] = None,
    purpose: Annotated[
        str,
        typer.Option(
            "--purpose",
            help="Use 'paper' for current monitoring or 'historical_research' for a bounded run.",
        ),
    ] = "paper",
    strict: Annotated[
        bool,
        typer.Option(
            "--strict/--report-only",
            help="Return a failing exit code when any readiness blocker remains.",
        ),
    ] = True,
    json_output: Annotated[
        Path | None,
        typer.Option("--json-output", help="Optional machine-readable report path."),
    ] = None,
) -> None:
    """Report whether U.S. evidence is safe for the requested operating use."""
    decision_date = as_of or date.today().isoformat()
    try:
        date.fromisoformat(decision_date)
        if purpose not in {"paper", "historical_research"}:
            raise ValueError("purpose must be 'paper' or 'historical_research'")
        report = assess_us_readiness(decision_date, purpose=purpose)
        if json_output is not None:
            json_output.parent.mkdir(parents=True, exist_ok=True)
            json_output.write_text(
                json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    except duckdb.IOException as exc:
        console.print(
            "[red]Readiness could not open DuckDB.[/red] Close the dashboard or another "
            "AIOS process using the database, then retry."
        )
        raise typer.Exit(code=1) from exc
    except (OSError, ValueError) as exc:
        console.print(f"[red]Readiness check refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.rule(f"[bold]U.S. readiness — {report.as_of} ({report.purpose})[/bold]")
    console.print(
        "[cyan]Certified historical research window:[/cyan] "
        f"{report.certified_research_from or 'unavailable'} through "
        f"{report.certified_research_through or 'unavailable'}"
    )
    console.print(
        "[cyan]Raw source clocks:[/cyan] "
        f"prices {report.raw_prices_through or 'none'}; "
        f"fundamentals {report.fundamentals_through or 'none'}; "
        f"macro releases {report.macro_releases_through or 'none'}"
    )
    table = Table(title="Operating gates")
    table.add_column("gate")
    table.add_column("status")
    table.add_column("observed")
    table.add_column("required")
    table.add_column("what it means")
    for check in report.checks:
        color = {"pass": "green", "warn": "yellow", "fail": "red"}[check.status]
        table.add_row(
            check.label,
            f"[{color}]{check.status}[/{color}]",
            check.observed,
            check.required,
            check.detail,
        )
    console.print(table)
    if json_output is not None:
        console.print(f"Machine-readable report written to {json_output}.")
    if report.ready:
        console.print("[bold green]READY for the requested use.[/bold green]")
    else:
        console.print(
            f"[bold red]BLOCKED:[/bold red] {len(report.blockers)} operating gate(s) failed."
        )
        if strict:
            raise typer.Exit(code=1)


@app.command("paper-init")
def paper_init(
    account: Annotated[
        Path,
        typer.Option("--account", help="Local paper-account JSON path."),
    ] = Path("data/paper/us_qv_sandbox.json"),
    initial_capital: Annotated[
        float,
        typer.Option("--initial-capital", min=1.0, help="Starting simulated dollars."),
    ] = 100_000.0,
    commission_bps: Annotated[
        float,
        typer.Option("--commission-bps", min=0.0, help="Simulated commission in basis points."),
    ] = 5.0,
    slippage_bps: Annotated[
        float,
        typer.Option("--slippage-bps", min=0.0, help="Simulated slippage in basis points."),
    ] = 5.0,
) -> None:
    """Create a local pre-tax sandbox; this never connects to a broker."""
    from aios.paper import initialize_paper_account

    destination = _project_path(account)
    try:
        document = initialize_paper_account(
            destination,
            get_store(),
            initial_capital=initial_capital,
            commission_bps=commission_bps,
            slippage_bps=slippage_bps,
        )
    except Exception as exc:
        console.print(f"[red]Paper account creation refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print("[bold green]Local simulation account created.[/bold green]")
    console.print(f"Account: {document.path}")
    console.print(
        "No broker is connected. Taxes are intentionally zero until your jurisdiction "
        "and account type are configured."
    )


@app.command("forward-freeze")
def forward_freeze(
    proposal: Annotated[
        Path | None,
        typer.Option(
            "--proposal",
            help="Reviewed baseline proposal; defaults to the newest local U.S. proposal.",
        ),
    ] = None,
    account: Annotated[
        Path,
        typer.Option("--account", help="Local paper-account JSON path."),
    ] = Path("data/paper/us_qv_sandbox.json"),
    output: Annotated[
        Path,
        typer.Option("--output", help="Checksum-protected forward-trial JSON path."),
    ] = Path("data/paper/us_qv_forward_trial.json"),
    confirm_freeze: Annotated[
        bool,
        typer.Option(
            "--confirm-freeze/--no-confirm-freeze",
            help="Required acknowledgement that policy changes restart forward evidence.",
        ),
    ] = False,
) -> None:
    """Freeze the supervised U.S. policy baseline; market data may still advance."""
    from aios.forward import create_forward_trial

    proposals_dir = settings.project_root / "data" / "paper" / "proposals"
    proposal_path = _project_path(proposal) if proposal is not None else None
    if proposal_path is None:
        candidates = sorted(proposals_dir.glob("us-qv-*.json"), reverse=True)
        if not candidates:
            console.print("[red]Forward freeze refused:[/red] no paper proposal exists")
            raise typer.Exit(code=1)
        proposal_path = candidates[0]
    try:
        document = create_forward_trial(
            settings.project_root,
            _project_path(output),
            _project_path(account),
            proposal_path,
            confirm=confirm_freeze,
        )
    except (OSError, ValueError) as exc:
        console.print(f"[red]Forward freeze refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print("[bold green]Untouched U.S. forward trial frozen.[/bold green]")
    console.print(f"Trial: {document.path}")
    console.print(f"Trial ID: {document.payload['trial_id']}")
    console.print(f"Policy bundle: {document.payload['policy_bundle_sha256']}")
    console.print(
        "Market data may advance normally. A factor, risk, cost, tax, calendar, readiness, "
        "or paper-policy change will now be reported as drift and block simulation."
    )


@app.command("forward-status")
def forward_status(
    trial: Annotated[
        Path,
        typer.Option("--trial", help="Checksum-protected forward-trial JSON path."),
    ] = Path("data/paper/us_qv_forward_trial.json"),
    account: Annotated[
        Path,
        typer.Option("--account", help="Local paper-account JSON path."),
    ] = Path("data/paper/us_qv_sandbox.json"),
) -> None:
    """Verify that the active forward policy and registered evidence are unchanged."""
    from aios.forward import assess_forward_trial

    try:
        status = assess_forward_trial(
            settings.project_root,
            _project_path(trial),
            _project_path(account),
        )
    except (OSError, ValueError) as exc:
        console.print(f"[red]Forward status unavailable:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.rule("[bold]U.S. untouched forward monitor[/bold]")
    console.print(f"Trial ID: {status.trial_id}")
    console.print(f"Registered proposals: {status.registered_proposals}")
    if status.ready:
        console.print("[bold green]UNCHANGED — forward policy evidence is intact.[/bold green]")
        return
    for issue in status.issues:
        console.print(f"[red]• {issue}[/red]")
    console.print("[bold red]DRIFTED — do not count newer observations in this trial.[/bold red]")
    raise typer.Exit(code=1)


@app.command("paper-propose")
def paper_propose(
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help="Reviewed decision close (YYYY-MM-DD); defaults to the latest safe SPY close.",
        ),
    ] = None,
    account: Annotated[
        Path,
        typer.Option("--account", help="Local paper-account JSON path."),
    ] = Path("data/paper/us_qv_sandbox.json"),
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Dated proposal path; a safe default is generated."),
    ] = None,
    top_n: Annotated[
        int,
        typer.Option("--top-n", min=10, max=20, help="Number of simulated holdings."),
    ] = 10,
    replace: Annotated[
        bool,
        typer.Option(
            "--replace/--no-replace",
            help="Explicitly replace an existing proposal for the same date.",
        ),
    ] = False,
) -> None:
    """Create a risk-checked research proposal; no trade is performed."""
    from aios.forward import (
        DEFAULT_FORWARD_RELATIVE_PATH,
        assess_forward_trial,
        read_forward_trial,
        register_forward_proposal,
    )
    from aios.paper import (
        create_paper_proposal,
        default_proposal_path,
        latest_paper_decision_date,
    )

    store = get_store()
    try:
        decision_date = date.fromisoformat(as_of) if as_of else latest_paper_decision_date(store)
        destination = (
            _project_path(output)
            if output is not None
            else default_proposal_path(settings.project_root, decision_date)
        )
        trial_path = settings.project_root / DEFAULT_FORWARD_RELATIVE_PATH
        if trial_path.exists():
            status = assess_forward_trial(
                settings.project_root,
                trial_path,
                _project_path(account),
            )
            if not status.ready:
                raise ValueError("active forward trial drifted: " + "; ".join(status.issues))
            trial = read_forward_trial(trial_path)
            if top_n != int(trial.payload["frozen_configuration"]["top_n"]):
                raise ValueError("top_n differs from the active forward trial")
            if replace and destination.exists():
                raise ValueError("registered forward proposals cannot be replaced")
        document = create_paper_proposal(
            _project_path(account),
            destination,
            decision_date,
            store,
            top_n=top_n,
            replace=replace,
        )
        if trial_path.exists():
            register_forward_proposal(
                settings.project_root,
                trial_path,
                _project_path(account),
                document.path,
            )
    except Exception as exc:
        console.print(f"[red]Paper proposal refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    payload = document.payload
    console.rule(f"[bold]U.S. paper proposal — {payload['decision_date']}[/bold]")
    console.print(f"Status: [bold]{payload['status']}[/bold]")
    console.print(f"Proposal: {document.path}")
    console.print(f"Next simulated close: {payload['scheduled_simulation_date']}")
    console.print(
        f"Factor-eligible stocks: {payload['factor_eligible_count']}; "
        f"risk-screened targets: {len(payload['targets'])}."
    )
    if payload["targets"]:
        table = Table(title="Research portfolio targets (simulation only)")
        table.add_column("rank")
        table.add_column("symbol")
        table.add_column("target")
        table.add_column("broad business group")
        for target in payload["targets"]:
            table.add_row(
                str(target["factor_rank"]),
                target["ticker"],
                f"{target['target_weight']:.1%}",
                target["sector"],
            )
        console.print(table)
    assessment = payload.get("risk_assessment")
    if assessment and not assessment["approved"]:
        blockers = [row["label"] for row in assessment["checks"] if row["status"] == "fail"]
        console.print("[red]Risk blockers:[/red] " + ", ".join(blockers))
    console.print(
        "[yellow]Research simulation only — this is not a personal buy/sell recommendation "
        "and nothing was sent to a broker.[/yellow]"
    )


@app.command("paper-execute")
def paper_execute(
    proposal: Annotated[
        Path,
        typer.Option("--proposal", help="Reviewed proposal JSON to simulate."),
    ],
    account: Annotated[
        Path,
        typer.Option("--account", help="Local paper-account JSON path."),
    ] = Path("data/paper/us_qv_sandbox.json"),
    confirm_simulated: Annotated[
        bool,
        typer.Option(
            "--confirm-simulated/--no-confirm-simulated",
            help="Required acknowledgement that this is a local simulation.",
        ),
    ] = False,
) -> None:
    """Simulate an approved next-session close after explicit confirmation."""
    from aios.forward import (
        DEFAULT_FORWARD_RELATIVE_PATH,
        require_registered_forward_proposal,
    )
    from aios.paper import execute_paper_proposal

    try:
        trial_path = settings.project_root / DEFAULT_FORWARD_RELATIVE_PATH
        if trial_path.exists():
            require_registered_forward_proposal(
                settings.project_root,
                trial_path,
                _project_path(account),
                _project_path(proposal),
            )
        result = execute_paper_proposal(
            _project_path(account),
            _project_path(proposal),
            get_store(),
            confirm_simulated=confirm_simulated,
        )
    except Exception as exc:
        console.print(f"[red]Simulated execution refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    execution = result["execution"]
    console.print(
        f"[bold green]Simulation recorded for {execution['execution_date']}.[/bold green] "
        f"{len(execution['trades'])} simulated trade(s), "
        f"${execution['transaction_costs']:,.2f} modeled costs."
    )
    console.print("No order was sent to a broker.")


@app.command("paper-mark")
def paper_mark(
    through: Annotated[
        str | None,
        typer.Option(
            "--through",
            help="Reviewed market close (YYYY-MM-DD); defaults to the latest safe SPY close.",
        ),
    ] = None,
    account: Annotated[
        Path,
        typer.Option("--account", help="Local paper-account JSON path."),
    ] = Path("data/paper/us_qv_sandbox.json"),
) -> None:
    """Update simulated holdings and the daily equity curve without rebalancing."""
    from aios.paper import latest_paper_decision_date, mark_paper_account

    store = get_store()
    try:
        mark_date = date.fromisoformat(through) if through else latest_paper_decision_date(store)
        result = mark_paper_account(_project_path(account), mark_date, store)
    except Exception as exc:
        console.print(f"[red]Paper mark refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]Simulation marked through {mark_date}:[/green] "
        f"{len(result['points'])} new daily point(s)."
    )


@app.command("paper-status")
def paper_status(
    account: Annotated[
        Path,
        typer.Option("--account", help="Local paper-account JSON path."),
    ] = Path("data/paper/us_qv_sandbox.json"),
) -> None:
    """Show the local simulated account without requiring DuckDB knowledge."""
    from aios.forward import DEFAULT_FORWARD_RELATIVE_PATH, assess_forward_trial
    from aios.paper import paper_account_summary

    try:
        summary = paper_account_summary(_project_path(account), get_store())
    except Exception as exc:
        console.print(f"[red]Paper status unavailable:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.rule("[bold]U.S. supervised paper monitor[/bold]")
    console.print("[bold yellow]Simulation only — no broker connection.[/bold yellow]")
    console.print(f"Last market date: {summary['last_market_date'] or 'not invested yet'}")
    console.print(f"Simulated account value: ${summary['equity']:,.2f}")
    console.print(f"Cash: ${summary['cash']:,.2f}")
    console.print(f"Current drawdown: {summary['drawdown']:.2%}")
    console.print(f"Recorded rebalances: {summary['execution_count']}")
    trial_path = settings.project_root / DEFAULT_FORWARD_RELATIVE_PATH
    if trial_path.exists():
        try:
            forward = assess_forward_trial(
                settings.project_root,
                trial_path,
                _project_path(account),
            )
        except (OSError, ValueError) as exc:
            console.print(f"[red]Forward-test baseline cannot be verified:[/red] {exc}")
        else:
            if forward.ready:
                console.print(
                    "Forward-test baseline: [green]unchanged[/green] "
                    f"({forward.registered_proposals} registered proposal(s))"
                )
            else:
                console.print("Forward-test baseline: [red]drift detected[/red]")
                for issue in forward.issues:
                    console.print(f"  • {issue}")
    else:
        console.print("Forward-test baseline: [yellow]not frozen yet[/yellow]")
    if summary["holdings"]:
        table = Table(title="Simulated holdings")
        table.add_column("symbol")
        table.add_column("portfolio weight")
        for holding in summary["holdings"]:
            table.add_row(holding["ticker"], f"{holding['weight']:.2%}")
        console.print(table)
    else:
        console.print(
            "No simulated holdings. A proposal remains separate until its scheduled "
            "close is reviewed and explicitly recorded."
        )


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


@app.command("refresh-us-current")
def refresh_us_current_command(
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help=(
                "Reviewed membership date; defaults to the newest viable snapshot "
                "within seven days."
            ),
        ),
    ] = None,
    prices: Annotated[
        bool,
        typer.Option("--prices/--no-prices", help="Refresh reviewed member and SPY prices."),
    ] = True,
    fundamentals: Annotated[
        bool,
        typer.Option(
            "--fundamentals/--no-fundamentals",
            help="Refresh SEC filings through reviewed issuer/CIK identities.",
        ),
    ] = True,
    macro: Annotated[
        bool,
        typer.Option("--macro/--no-macro", help="Refresh release-aware macro vintages."),
    ] = True,
    json_output: Annotated[
        Path | None,
        typer.Option("--json-output", help="Optional machine-readable run summary."),
    ] = None,
) -> None:
    """Refresh the current reviewed U.S. universe without approving new members."""
    from aios.refresh import refresh_us_current

    def show_progress(kind: str, identity: str, index: int, total: int) -> None:
        if index == 1 or index == total or index % 25 == 0:
            console.print(f"  {kind}: {index}/{total} ({identity})")

    try:
        result = refresh_us_current(
            as_of,
            include_prices=prices,
            include_fundamentals=fundamentals,
            include_macro=macro,
            progress=show_progress,
        )
        if json_output is not None:
            destination = _project_path(json_output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    except duckdb.IOException as exc:
        console.print(
            "[red]Current refresh could not lock DuckDB.[/red] Close the dashboard "
            "and every other AIOS command, then retry."
        )
        raise typer.Exit(code=1) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Current U.S. refresh refused safely:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title=f"Current U.S. refresh — {result.as_of}")
    table.add_column("area")
    table.add_column("result", justify="right")
    table.add_row("reviewed membership date", result.membership_as_of or result.as_of)
    table.add_row("stored universe members", str(result.members))
    table.add_row("issuers attempted", str(result.issuers_attempted))
    table.add_row("member securities attempted", str(result.securities_attempted))
    table.add_row("macro rows", str(result.macro_rows))
    table.add_row("fundamental rows", str(result.fundamental_rows))
    table.add_row("member price rows", str(result.price_rows))
    table.add_row("SPY rows", str(result.benchmark_rows))
    table.add_row("visible warnings", str(len(result.warnings)))
    table.add_row("failures", str(len(result.failures)))
    console.print(table)
    console.print(
        "[yellow]Membership boundary:[/yellow] this refresh uses only already reviewed "
        "identities. New S&P announcements still require source review and import."
    )
    if json_output is not None:
        console.print(f"Run summary written to {_project_path(json_output)}.")
    for warning in result.warnings[:25]:
        console.print(
            f"  [yellow]{warning.kind} {warning.identity}:[/yellow] {warning.error}"
        )
    if len(result.warnings) > 25:
        console.print(
            f"  [yellow]... {len(result.warnings) - 25} additional warnings are in "
            "the JSON summary and ingest audit.[/yellow]"
        )
    if result.failures:
        for failure in result.failures[:25]:
            console.print(
                f"  [red]{failure.kind} {failure.identity}:[/red] {failure.error}"
            )
        if len(result.failures) > 25:
            console.print(
                f"  [red]... {len(result.failures) - 25} additional failures are in "
                "the JSON summary and ingest audit.[/red]"
            )
        raise typer.Exit(code=1)
    console.print(
        "[bold green]Refresh completed.[/bold green] Run `aios health` before creating "
        "or recording a paper proposal."
    )


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
        list[Path],
        typer.Option(
            "--events",
            help=(
                "Audited event CSV with effective_date, action, known_date, and source. "
                "Repeat --events to combine non-overlapping reviewed batches."
            ),
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
        merge_universe_event_batches,
        reconcile_event_boundaries,
        write_membership_csv,
    )

    try:
        coverage_start = date.fromisoformat(start)
        coverage_end = date.fromisoformat(end)
        spans = load_effective_spans_csv(baseline_spans)
        event_rows = merge_universe_event_batches(
            *(
                load_universe_events_csv(
                    event_path,
                    universe_id=universe_id,
                    require_official_sources=require_official_sources,
                )
                for event_path in events
            )
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
    console.print(f"[green]Universe build done:[/green] {len(rows)} intervals written to {output}.")
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


@app.command("import-security-conversions")
def import_security_conversions(
    path: Path = SECURITY_CONVERSION_FILE_ARGUMENT,
) -> None:
    """Import reviewed events that convert one security into another."""
    from aios.ingest.security_events import ingest_security_conversion_csv

    try:
        count = ingest_security_conversion_csv(path)
    except duckdb.IOException as exc:
        console.print(
            "[red]Security conversion import could not open DuckDB.[/red] "
            "Close the dashboard and any other AIOS command, then retry."
        )
        raise typer.Exit(code=1) from exc
    except (OSError, ValueError) as exc:
        console.print(f"[red]Security conversion import refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]Security conversion import done:[/green] {count} reviewed event(s) upserted."
    )


@app.command("ingest-liquidation-prices")
def ingest_liquidation_prices(
    path: Path = LIQUIDATION_EXTENSION_FILE_ARGUMENT,
) -> None:
    """Fetch held-security prices through the next rebalance only."""
    from aios.ingest.liquidation_prices import ingest_liquidation_extension_csv

    try:
        counts = ingest_liquidation_extension_csv(path)
    except duckdb.IOException as exc:
        console.print(
            "[red]Liquidation price import could not open DuckDB.[/red] "
            "Close the dashboard and any other AIOS command, then retry."
        )
        raise typer.Exit(code=1) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Liquidation price import refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        "[green]Liquidation price import done:[/green] "
        f"{counts['extensions']} extension(s), {counts['prices']} price rows."
    )


@app.command("build-reference-batch")
def build_reference_batch(
    tickers_file: Path = TICKERS_FILE_ARGUMENT,
    batch_name: Annotated[
        str,
        typer.Option("--batch-name", help="Safe file prefix for this reviewed batch."),
    ] = "reference_batch",
    start: Annotated[
        str,
        typer.Option(help="Certified inclusive start date, YYYY-MM-DD."),
    ] = "2023-08-01",
    end: Annotated[
        str,
        typer.Option(help="Certified exclusive end date, YYYY-MM-DD."),
    ] = "2025-01-01",
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Directory for manifests and review CSV."),
    ] = Path("data/reference_batches"),
    universe_id: Annotated[str, typer.Option("--universe-id")] = "sp500",
    provider: Annotated[str, typer.Option(help="Explicit price provider to certify.")] = (
        "yfinance"
    ),
    verified_date: Annotated[
        str | None,
        typer.Option(help="Evidence review date; defaults to today."),
    ] = None,
) -> None:
    """Certify only unchanged, full-window securities from independent sources."""
    from aios.ingest.reference_batch import (
        build_stable_reference_batch,
        load_batch_tickers,
        write_reference_batch,
    )

    try:
        tickers = load_batch_tickers(tickers_file)
        result = build_stable_reference_batch(
            tickers,
            universe_id=universe_id,
            start=start,
            end=end,
            provider=provider,
            verified_date=verified_date,
        )
        paths = write_reference_batch(
            result,
            output_dir=output_dir,
            batch_name=batch_name,
        )
    except Exception as exc:
        console.print(f"[red]Reference batch build refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]Reference batch built:[/green] {result['accepted']} accepted, "
        f"{result['rejected']} rejected."
    )
    for label, path in paths.items():
        console.print(f"  {label}: {path}")
    if result["rejected"]:
        for row in result["review_rows"]:
            if row["review_status"] == "rejected":
                console.print(f"  [yellow]{row['ticker']}:[/yellow] {row['reason']}")
        raise typer.Exit(code=1)


@app.command("build-reference-window-batch")
def build_reference_window_batch(
    windows_file: Annotated[
        Path,
        typer.Argument(help="CSV with one ticker,start,end window per row."),
    ],
    batch_name: Annotated[
        str,
        typer.Option("--batch-name", help="Safe file prefix for this reviewed batch."),
    ] = "reference_window_batch",
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Directory for manifests and review CSV."),
    ] = Path("data/reference_batches"),
    universe_id: Annotated[str, typer.Option("--universe-id")] = "sp500",
    provider: Annotated[str, typer.Option(help="Explicit price provider to certify.")] = (
        "yfinance"
    ),
    verified_date: Annotated[
        str | None,
        typer.Option(help="Evidence review date; defaults to today."),
    ] = None,
) -> None:
    """Certify strict identities over independently bounded ticker windows."""
    from aios.ingest.reference_batch import (
        build_stable_reference_window_batch,
        load_batch_windows,
        write_reference_batch,
    )

    try:
        windows = load_batch_windows(windows_file)
        result = build_stable_reference_window_batch(
            windows,
            universe_id=universe_id,
            provider=provider,
            verified_date=verified_date,
        )
        paths = write_reference_batch(
            result,
            output_dir=output_dir,
            batch_name=batch_name,
        )
    except Exception as exc:
        console.print(f"[red]Reference window batch build refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]Reference window batch built:[/green] {result['accepted']} accepted, "
        f"{result['rejected']} rejected."
    )
    for label, path in paths.items():
        console.print(f"  {label}: {path}")
    if result["rejected"]:
        for row in result["review_rows"]:
            if row["review_status"] == "rejected":
                console.print(f"  [yellow]{row['ticker']}:[/yellow] {row['reason']}")
        raise typer.Exit(code=1)


@app.command("plan-reference-window-batches")
def plan_reference_window_batches(
    as_of: Annotated[str, typer.Option(help="Active-universe date, YYYY-MM-DD.")],
    start_floor: Annotated[
        str,
        typer.Option(help="Earliest extension date, YYYY-MM-DD."),
    ],
    end: Annotated[str, typer.Option(help="Exclusive certified end, YYYY-MM-DD.")],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Directory for deterministic window CSVs."),
    ] = Path("data/reference_batches"),
    universe_id: Annotated[str, typer.Option("--universe-id")] = "sp500",
    provider: Annotated[str, typer.Option(help="Provider whose coverage is required.")] = (
        "yfinance"
    ),
    batch_prefix: Annotated[
        str,
        typer.Option(help="Safe filename prefix before the batch number."),
    ] = "reference_batch",
    batch_size: Annotated[
        int,
        typer.Option(min=1, help="Maximum ticker windows per batch."),
    ] = 25,
    start_number: Annotated[
        int,
        typer.Option(min=1, help="First batch number to write."),
    ] = 1,
) -> None:
    """Plan only active members missing complete current reference coverage."""
    from aios.ingest.reference_batch import (
        plan_missing_reference_windows,
        write_reference_window_batches,
    )

    try:
        windows = plan_missing_reference_windows(
            universe_id=universe_id,
            as_of=as_of,
            start_floor=start_floor,
            end=end,
            provider=provider,
        )
        paths = write_reference_window_batches(
            windows,
            output_dir=output_dir,
            batch_prefix=batch_prefix,
            batch_size=batch_size,
            start_number=start_number,
        )
    except (OSError, ValueError) as exc:
        console.print(f"[red]Reference window plan refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if not paths:
        console.print(
            f"[green]Reference coverage is complete for {universe_id} on {as_of}.[/green]"
        )
        return
    console.print(
        f"[green]Reference window plan:[/green] {len(windows)} missing members in "
        f"{len(paths)} batch(es)."
    )
    for path in paths:
        console.print(f"  {path}")


@app.command("plan-historical-reference-batches")
def plan_historical_reference_batches(
    start: Annotated[str, typer.Option(help="Inclusive research start, YYYY-MM-DD.")],
    end: Annotated[str, typer.Option(help="Exclusive research end, YYYY-MM-DD.")],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Directory for deterministic window CSVs."),
    ] = Path("data/reference_batches"),
    universe_id: Annotated[str, typer.Option("--universe-id")] = "sp500",
    batch_prefix: Annotated[
        str,
        typer.Option(help="Safe filename prefix before the batch number."),
    ] = "historical_reference_batch",
    batch_size: Annotated[
        int,
        typer.Option(min=1, help="Maximum ticker windows per batch."),
    ] = 25,
    start_number: Annotated[
        int,
        typer.Option(min=1, help="First batch number to write."),
    ] = 1,
) -> None:
    """Plan survivorship-safe reference gaps, including securities later removed."""
    from aios.ingest.reference_batch import (
        plan_historical_reference_gaps,
        write_reference_window_batches,
    )

    try:
        windows = plan_historical_reference_gaps(
            universe_id=universe_id,
            start=start,
            end=end,
        )
        paths = (
            write_reference_window_batches(
                windows,
                output_dir=output_dir,
                batch_prefix=batch_prefix,
                batch_size=batch_size,
                start_number=start_number,
            )
            if windows
            else []
        )
    except (OSError, ValueError) as exc:
        console.print(f"[red]Historical reference plan refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if not paths:
        console.print(
            f"[green]Historical reference coverage is complete for {universe_id} "
            f"from {start} through {end}.[/green]"
        )
        return
    console.print(
        f"[green]Historical reference plan:[/green] {len(windows)} provenance gap(s) "
        f"in {len(paths)} batch(es), including removed members."
    )
    for path in paths:
        console.print(f"  {path}")


@app.command("merge-reference-batches")
def merge_reference_batches(
    batches: Annotated[
        list[Path],
        typer.Option(
            "--batch",
            help="Path stem before _issuer_ciks.csv; repeat for each reviewed batch.",
        ),
    ],
    batch_name: Annotated[
        str,
        typer.Option("--batch-name", help="Filename prefix for consolidated manifests."),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Directory for consolidated manifests."),
    ] = Path("data/reference_batches"),
) -> None:
    """Consolidate accepted rows without carrying failed reviews forward."""
    from aios.ingest.reference_batch import (
        merge_reference_batch_files,
        write_reference_batch,
    )

    try:
        result = merge_reference_batch_files(batches)
        paths = write_reference_batch(
            result,
            output_dir=output_dir,
            batch_name=batch_name,
        )
    except (OSError, ValueError) as exc:
        console.print(f"[red]Reference batch merge refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]Reference batches merged:[/green] {result['accepted']} accepted reviews."
    )
    for label, path in paths.items():
        console.print(f"  {label}: {path}")


@app.command("ingest-reference-batch")
def ingest_reference_batch(
    issuer_ciks: Annotated[
        Path,
        typer.Option("--issuer-ciks", help="Certified issuer/CIK batch CSV."),
    ],
    security_issuers: Annotated[
        Path,
        typer.Option("--security-issuers", help="Certified security-owner batch CSV."),
    ],
    provider_symbols: Annotated[
        Path,
        typer.Option("--provider-symbols", help="Certified provider-symbol batch CSV."),
    ],
    start: Annotated[str, typer.Option(help="Inclusive ingest start, YYYY-MM-DD.")],
    end: Annotated[str, typer.Option(help="Exclusive ingest end, YYYY-MM-DD.")],
    companyfacts_zip: Annotated[
        Path | None,
        typer.Option(
            "--companyfacts-zip",
            help="Optional local official SEC companyfacts.zip for bulk facts reads.",
        ),
    ] = None,
) -> None:
    """Atomically import a reviewed batch, then ingest each identity independently."""
    from aios.ingest.reference_batch import ingest_reviewed_reference_batch

    try:
        summary = ingest_reviewed_reference_batch(
            issuer_ciks,
            security_issuers,
            provider_symbols,
            start=start,
            end=end,
            companyfacts_zip_path=companyfacts_zip,
        )
    except Exception as exc:
        console.print(f"[red]Reference batch ingest failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    counts = summary["reference_counts"]
    console.print(
        "[green]Reference batch ingest done:[/green] "
        f"{counts['issuers']} issuers, {counts['security_issuers']} owners, "
        f"{counts['provider_symbols']} provider mappings; "
        f"{summary['fundamental_rows']} fundamental rows and "
        f"{summary['price_rows']} price rows."
    )
    if companyfacts_zip is not None:
        console.print(f"  Company Facts source: {summary['companyfacts_source']}")
    if summary["failures"]:
        for failure in summary["failures"]:
            console.print(f"  [red]{failure['kind']} {failure['id']}:[/red] {failure['error']}")
        raise typer.Exit(code=1)


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
    complete = [row for row in rows if row["has_price_history"] and row["has_pit_fundamentals"]]
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
    calendar: str | None = typer.Option(
        None,
        "--calendar",
        help="Ticker whose sessions define quarter ends and all execution dates (e.g. SPY).",
    ),
    explain_tickers: Annotated[
        list[str] | None,
        typer.Option(
            "--explain-ticker",
            help="Show member/eligibility evidence for a ticker; repeat as needed.",
        ),
    ] = None,
    excluded_tickers: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude-ticker",
            help="Explicitly exclude a ticker from factor eligibility; repeat as needed.",
        ),
    ] = None,
    factor_model: str = typer.Option(
        "qv",
        "--factor-model",
        help="Selection model: certified baseline 'qv' or experimental 'qvml'.",
    ),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Write a reproducible JSON audit artifact to this path.",
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
    """Compare QV/QVML policies after explicit costs/taxes and benchmarks."""
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
            calendar_ticker=calendar,
            excluded_tickers=excluded_tickers,
            factor_model=factor_model,
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

    model_label = result.config.factor_model.upper()
    console.rule(f"[bold]{model_label} policy backtest: {start} → {end}[/bold]")
    console.print(
        f"Universe union: {len(result.tickers)} tickers • quarterly • "
        f"top {top_n} equal-weight • "
        f"comparison periods: {result.comparison_periods}"
    )
    console.print(
        f"Calendar: {result.config.calendar_ticker or 'inferred from stored prices'} • "
        "execution: carry holdings → rebalance deltas at next session close"
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
    summary.add_column("daily obs", justify="right")
    for name, metrics in (
        ("regime-aware", result.regime_metrics),
        (f"baseline {model_label}", result.baseline_metrics),
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
            str(metrics.daily_observations),
        )
    console.print(summary)

    coverage_table = Table(title="Decision-level PIT eligibility audit")
    coverage_table.add_column("decision")
    coverage_table.add_column("members", justify="right")
    coverage_table.add_column("raw complete", justify="right")
    coverage_table.add_column("Q scored", justify="right")
    coverage_table.add_column("V scored", justify="right")
    if result.config.factor_model == "qvml":
        coverage_table.add_column("M scored", justify="right")
        coverage_table.add_column("L scored", justify="right")
    coverage_table.add_column("eligible", justify="right")
    coverage_table.add_column("regime")
    coverage_table.add_column("factor weights")
    coverage_table.add_column("status")
    for period in result.periods:
        cells = [
            period.decision_date,
            str(len(period.member_tickers)),
            ("N/A" if period.raw_complete_tickers is None else str(period.raw_complete_tickers)),
            str(period.quality_scored_tickers),
            str(period.value_scored_tickers),
        ]
        if result.config.factor_model == "qvml":
            cells.extend(
                [
                    str(period.momentum_scored_tickers),
                    str(period.low_volatility_scored_tickers),
                ]
            )
        cells.extend(
            [
                str(period.eligible_tickers),
                period.regime,
                (
                    f"{period.quality_weight:.0%}/{period.value_weight:.0%}/"
                    f"{period.momentum_weight:.0%}/{period.low_volatility_weight:.0%}"
                    if result.config.factor_model == "qvml"
                    else f"{period.quality_weight:.0%}/{period.value_weight:.0%}"
                ),
                period.status,
            ]
        )
        coverage_table.add_row(*cells)
    console.print(coverage_table)

    selections = Table(title="Selected holdings by policy")
    selections.add_column("decision")
    selections.add_column("regime-aware")
    selections.add_column(f"baseline {model_label}")
    for period in result.periods:
        selections.add_row(
            period.decision_date,
            ", ".join(period.regime_selected) or "—",
            ", ".join(period.baseline_selected) or "—",
        )
    console.print(selections)

    if result.benchmark_metrics:
        benchmark_table = Table(
            title="Persistent benchmark returns (basis-aware actions; no costs/taxes)"
        )
        benchmark_table.add_column("benchmark")
        benchmark_table.add_column("periods", justify="right")
        benchmark_table.add_column("cumulative", justify="right")
        benchmark_table.add_column("annualized", justify="right")
        benchmark_table.add_column("volatility", justify="right")
        benchmark_table.add_column("max drawdown", justify="right")
        for ticker, metrics in result.benchmark_metrics.items():
            benchmark_table.add_row(
                ticker,
                str(metrics.completed_periods),
                _pct(metrics.cumulative_return),
                _pct(metrics.annualized_return),
                _pct(metrics.annualized_volatility),
                _pct(metrics.max_drawdown),
            )
        console.print(benchmark_table)

    explanations = _backtest_ticker_explanations(result, explain_tickers or [])
    if explanations:
        explain_table = Table(title="Requested ticker evidence")
        explain_table.add_column("decision")
        explain_table.add_column("ticker")
        explain_table.add_column("status")
        explain_table.add_column("reason/evidence")
        for item in explanations:
            explain_table.add_row(
                item["decision_date"],
                item["ticker"],
                item["status"],
                ", ".join(item["reasons"]) or "eligible factor evidence",
            )
        console.print(explain_table)

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
    if output is not None:
        audit_path = _write_backtest_audit(output, result, explanations)
        console.print(f"[green]Audit artifact:[/green] {audit_path}")


def _backtest_ticker_explanations(result, tickers: list[str]) -> list[dict]:
    """Explain requested tickers without inventing reasons for non-members."""
    requested = sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()})
    explanations: list[dict] = []
    for period in result.periods:
        audit_by_ticker = {row.ticker: row for row in period.factor_audit}
        members = set(period.member_tickers)
        regime_selected = set(period.regime_selected)
        baseline_selected = set(period.baseline_selected)
        for ticker in requested:
            if ticker not in members:
                status = "not_member"
                reasons = ["not_in_pit_universe_on_decision_date"]
            else:
                audit = audit_by_ticker.get(ticker)
                if audit is None:
                    status = "excluded"
                    reasons = ["factor_row_unavailable"]
                elif not audit.eligible:
                    status = "excluded"
                    reasons = list(audit.reasons)
                elif ticker in regime_selected or ticker in baseline_selected:
                    selected_by = []
                    if ticker in regime_selected:
                        selected_by.append("regime")
                    if ticker in baseline_selected:
                        selected_by.append("baseline")
                    status = "selected"
                    reasons = [f"selected_by:{'+'.join(selected_by)}"]
                else:
                    status = "eligible_not_selected"
                    reasons = ["rank_below_top_n"]
            explanations.append(
                {
                    "decision_date": period.decision_date,
                    "ticker": ticker,
                    "status": status,
                    "reasons": reasons,
                }
            )
    return explanations


def _write_backtest_audit(output: Path, result, explanations: list[dict]) -> Path:
    """Atomically write a provenance-rich, secret-free backtest audit."""
    path = output.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    db_path = settings.duckdb_path
    if not db_path.is_absolute():
        db_path = settings.project_root / db_path
    commit = _git_output(["git", "rev-parse", "HEAD"])
    status = _git_output(["git", "status", "--porcelain"])
    diff = _git_bytes(["git", "diff", "--binary", "HEAD", "--"])
    untracked_count, untracked_sha256 = _untracked_tree_fingerprint()
    payload = {
        "schema_version": 4,
        "run_id": str(uuid4()),
        "generated_at": datetime.now(UTC).isoformat(),
        "application_version": __version__,
        "command": list(sys.argv),
        "runtime": {
            "python": sys.version,
            "platform": platform.platform(),
        },
        "repository": {
            "commit": commit or None,
            "dirty": bool(status),
            "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
            "untracked_file_count": untracked_count,
            "untracked_tree_sha256": untracked_sha256,
        },
        "database": {
            "path": str(db_path.resolve()),
            "sha256": _sha256_file(db_path) if db_path.exists() else None,
        },
        "ticker_explanations": explanations,
        "result": result.to_dict(),
    }
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return path


def _git_output(command: list[str]) -> str:
    return _git_bytes(command).decode(errors="replace").strip()


def _git_bytes(command: list[str]) -> bytes:
    completed = subprocess.run(
        command,
        cwd=settings.project_root,
        check=False,
        capture_output=True,
    )
    return completed.stdout if completed.returncode == 0 else b""


def _untracked_tree_fingerprint() -> tuple[int, str]:
    paths = [
        Path(value.decode(errors="surrogateescape"))
        for value in _git_bytes(["git", "ls-files", "-z", "--others", "--exclude-standard"]).split(
            b"\0"
        )
        if value
    ]
    digest = hashlib.sha256()
    files = 0
    for relative in sorted(paths, key=lambda path: path.as_posix()):
        absolute = settings.project_root / relative
        if not absolute.is_file():
            continue
        files += 1
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        with absolute.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return files, digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


@app.command("refresh-price-actions")
def refresh_price_actions(
    start: Annotated[
        str,
        typer.Option(help="Inclusive corrective window start, YYYY-MM-DD."),
    ],
    end: Annotated[
        str,
        typer.Option(help="Exclusive corrective window end, YYYY-MM-DD."),
    ],
    provider: Annotated[
        str,
        typer.Option(help="Reviewed action-capable provider."),
    ] = "yfinance",
    limit: Annotated[
        int | None,
        typer.Option(help="Optional candidate cap for a smoke test or resumable batch."),
    ] = None,
    tickers: Annotated[
        list[str] | None,
        typer.Option(
            "--ticker",
            help="Also refresh an explicit benchmark/calendar ticker; repeat as needed.",
        ),
    ] = None,
) -> None:
    """Correct rows fetched before explicit dividend/split actions were enabled."""
    import time

    from aios.ingest.prices import fetch_provider_prices
    from aios.ingest.prices import (
        ingest_security_prices as _ingest_security_prices,
    )

    try:
        window_start = date.fromisoformat(start)
        window_end = date.fromisoformat(end)
    except ValueError as exc:
        console.print(f"[red]Action refresh refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if window_end <= window_start:
        console.print("[red]Action refresh refused:[/red] end must follow start")
        raise typer.Exit(code=1)
    if limit is not None and limit < 1:
        console.print("[red]Action refresh refused:[/red] limit must be positive")
        raise typer.Exit(code=1)

    store = get_store()
    candidates = store.price_action_refresh_candidates(provider, window_start, window_end)
    if limit is not None:
        candidates = candidates[:limit]
    explicit_tickers = sorted(
        {ticker.strip().upper() for ticker in tickers or [] if ticker.strip()}
    )
    if not candidates and not explicit_tickers:
        console.print("[green]No reviewed price-action rows need refresh.[/green]")
        return

    if candidates:
        console.print(
            f"Refreshing {len(candidates)} reviewed securities from {start} through "
            f"{end} (exclusive) via {provider}. Re-running is safe and resumes only "
            "unresolved securities."
        )
    failures: list[tuple[str, str]] = []
    refreshed_rows = 0
    for index, security_id in enumerate(candidates, 1):
        console.rule(f"[{index}/{len(candidates)}] {security_id}")
        try:
            refreshed_rows += _ingest_security_prices(
                security_id,
                provider=provider,
                start=start,
                end=end,
                store=store,
            )
            remaining = store.unverified_price_action_count(
                security_id,
                provider,
                window_start,
                window_end,
            )
            if remaining:
                raise ValueError(f"{remaining} rows remain action-unverified")
        except Exception as exc:
            failures.append((security_id, str(exc)))
            console.print(f"[red]Refresh failed:[/red] {exc}")
        if index < len(candidates):
            time.sleep(settings.yfinance_sleep_sec)

    for ticker in explicit_tickers:
        console.rule(f"benchmark/calendar {ticker}")
        try:
            rows = fetch_provider_prices(provider, ticker, start, end)
            if not rows:
                raise ValueError("provider returned no rows")
            refreshed_rows += store.upsert_prices(rows)
            remaining = store.unverified_ticker_action_count(
                ticker,
                provider,
                window_start,
                window_end,
            )
            if remaining:
                raise ValueError(f"{remaining} rows remain action-unverified")
        except Exception as exc:
            failures.append((ticker, str(exc)))
            console.print(f"[red]Refresh failed:[/red] {exc}")

    console.print(
        f"[green]Price-action refresh stored {refreshed_rows} rows across "
        f"{len(candidates) + len(explicit_tickers) - len(failures)} identities.[/green]"
    )
    if failures:
        for security_id, error in failures:
            console.print(f"  [red]{security_id}:[/red] {error}")
        raise typer.Exit(code=1)


@app.command("build-factor-price-warmup")
def build_factor_price_warmup(
    output_dir: Annotated[
        Path,
        typer.Option(help="Ignored directory for review CSVs and compressed snapshots."),
    ] = Path("data/factor_price_warmup"),
    universe_id: Annotated[str, typer.Option("--universe-id")] = "sp500",
    start: Annotated[
        str,
        typer.Option(help="Inclusive identity-safe history start, YYYY-MM-DD."),
    ] = "2022-09-01",
    as_of: Annotated[
        str | None,
        typer.Option(
            help="Optional date selecting only membership and provider mappings active then."
        ),
    ] = None,
    security_ids: Annotated[
        list[str] | None,
        typer.Option(
            "--security-id",
            help="Optional immutable security ID to build; repeat as needed.",
        ),
    ] = None,
    only_missing: Annotated[
        bool,
        typer.Option(
            "--only-missing",
            help="With --as-of, build only incomplete 253-session factor histories.",
        ),
    ] = False,
    overlap_days: Annotated[
        int,
        typer.Option(help="Calendar days fetched after each verified provider anchor."),
    ] = 21,
    minimum_overlap_sessions: Annotated[
        int,
        typer.Option(help="Stored sessions that must exactly match the fresh provider fetch."),
    ] = 5,
    minimum_warmup_sessions: Annotated[
        int,
        typer.Option(help="Pre-anchor sessions required for an accepted snapshot."),
    ] = 210,
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Ignore valid cached snapshots and fetch again."),
    ] = False,
    allow_rejections: Annotated[
        bool,
        typer.Option(
            "--allow-rejections",
            help="Exit successfully after writing a batch with explicitly reviewed exclusions.",
        ),
    ] = False,
) -> None:
    """Build hashed security-level warm-up snapshots without backdating tickers."""
    from aios.ingest.factor_price_warmup import (
        build_factor_price_warmup as _build_factor_price_warmup,
    )

    try:
        date.fromisoformat(start)
        if as_of:
            date.fromisoformat(as_of)

        def show_progress(index: int, total: int, candidate: dict) -> None:
            console.rule(
                f"[{index}/{total}] {candidate['canonical_ticker']} "
                f"({candidate['provider']}:{candidate['provider_symbol']})"
            )

        result = _build_factor_price_warmup(
            output_dir,
            universe_id=universe_id,
            start=start,
            as_of=as_of,
            security_ids=security_ids,
            only_missing=only_missing,
            overlap_days=overlap_days,
            minimum_overlap_sessions=minimum_overlap_sessions,
            minimum_warmup_sessions=minimum_warmup_sessions,
            refresh=refresh,
            progress=show_progress,
            rejections_reviewed=allow_rejections,
        )
    except Exception as exc:
        console.print(f"[red]Warm-up build failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]Warm-up review complete:[/green] {result['accepted']} accepted, "
        f"{result['rejected']} rejected, {result['reused']} reused from cache."
    )
    if result["skipped_complete"]:
        console.print(
            f"  Skipped {result['skipped_complete']} securities whose factor windows "
            "were already complete."
        )
    console.print(f"  Review: {result['review_path']}")
    console.print(f"  Provenance: {result['provenance_path']}")
    console.print(f"  Manifest: {result['manifest_path']}")
    if result["rejected"]:
        for row in [r for r in result["review_rows"] if r["review_status"] == "rejected"][:25]:
            console.print(
                f"  [yellow]{row['canonical_ticker']} ({row['security_id']}):[/yellow] "
                f"{row['reason']}"
            )
        if not allow_rejections:
            console.print(
                "[yellow]Review every rejection, then rerun with --allow-rejections "
                "before treating the batch as complete.[/yellow]"
            )
            raise typer.Exit(code=1)


@app.command("ingest-factor-price-warmup")
def ingest_factor_price_warmup(
    batch_dir: Annotated[
        Path,
        typer.Argument(help="Directory produced by build-factor-price-warmup."),
    ] = Path("data/factor_price_warmup"),
) -> None:
    """Hash-check and atomically import accepted factor-price snapshots."""
    from aios.ingest.factor_price_warmup import (
        ingest_factor_price_warmup as _ingest_factor_price_warmup,
    )

    try:
        counts = _ingest_factor_price_warmup(batch_dir)
    except Exception as exc:
        console.print(f"[red]Warm-up ingest failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]Warm-up ingest complete:[/green] {counts['factor_prices']} rows from "
        f"{counts['snapshots']} reviewed security snapshots."
    )


@app.command("review-factor-price-warmup-rejections")
def review_factor_price_warmup_rejections(
    batch_dir: Annotated[
        Path,
        typer.Argument(help="Directory produced by build-factor-price-warmup."),
    ] = Path("data/factor_price_warmup"),
) -> None:
    """Approve preserved warm-up exclusions without re-fetching the provider."""
    from aios.ingest.factor_price_warmup import (
        mark_factor_price_warmup_rejections_reviewed,
    )

    try:
        count = mark_factor_price_warmup_rejections_reviewed(batch_dir)
    except Exception as exc:
        console.print(f"[red]Warm-up rejection review failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Warm-up exclusions reviewed:[/green] {count} preserved rejection(s).")


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


@app.command("quarantine-invalid-fundamentals")
def quarantine_invalid_fundamentals() -> None:
    """Move period_end-after-filing rows into a provenance quarantine table."""
    moved = get_store().quarantine_invalid_fundamental_periods()
    console.print(
        f"[green]Quarantined {moved} impossible fundamental rows; "
        "the source evidence remains in fundamentals_quarantine.[/green]"
    )


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

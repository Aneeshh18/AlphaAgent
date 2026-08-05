"""CLI entrypoint — `aios <command>`.

The single command surface for the foundation phase. Every daily job runs
through here. Designed to be wired to systemd/cron later.

Commands
--------
  aios doctor        — verify env, deps, DB, connectivity
  aios preflight     — one read-only operator status and safest next command
  aios readiness     — fail-closed U.S. data/readiness report
  aios health        — one plain-language operating check
  aios backup        — checksum-verified local data/paper backup
  aios upgrade-local-state — backup, rehearse, and apply local schema upgrades
  aios upgrade-local-state-recover — reconcile one prepared upgrade attempt
  aios verify-backup — verify a backup without restoring it
  aios verify-raw-snapshots — verify immutable provider payloads
  aios review-universe-current — safely roll forward unchanged S&P 500 coverage
  aios restore       — confirmed recovery with automatic rollback backup
  aios scheduler-install — enable local refresh/health/backup timers
  aios scheduler-status — show whether local timers are enabled and waiting
  aios scheduler-pause/resume — safely stop/start local timers
  aios scheduler-remove — disable and remove only AIOS-managed timer files
  aios alerts        — inspect durable local operating incidents
  aios alert-test    — verify the local incident lifecycle
  aios alert-ack     — acknowledge one unresolved incident
  aios alert-resolve — explicitly resolve one incident
  aios anomaly-scan  — detect evidence-bound data-quality review cases
  aios anomalies     — inspect governed data-quality review cases
  aios anomaly-show  — inspect one case and its immutable evidence history
  aios anomaly-ack   — assign and acknowledge one review case
  aios anomaly-resolve — record one governed case disposition
  aios companyfacts-v3-plan — preview an evidence-bound parser replay plan
  aios notifications — inspect the channel-neutral alert outbox
  aios notification-show — inspect one message and its delivery attempts
  aios notification-test — certify the local no-network delivery lifecycle
  aios email-status   — inspect secret-safe SMTP readiness and durable opt-in
  aios email-test     — explicitly send one external receipt test
  aios email-enable   — enable email only for future incident transitions
  aios email-disable  — stop future email delivery and hold unsent messages
  aios email-deliver  — run one bounded retry-safe SMTP delivery pass
  aios dashboard     — open the local research dashboard
  aios paper-init    — create a local simulation-only portfolio
  aios forward-freeze — freeze policy/configuration for untouched monitoring
  aios forward-rollover — preview/publish an exact plan; activation is disabled
  aios forward-rollover-recover — reconcile one interrupted rollover attempt
  aios forward-restart — prospectively replace and archive a drifted trial
  aios forward-status — verify the active forward policy has not drifted
  aios paper-propose — build a reviewed QV simulation proposal
  aios stress-review — run advisory deterministic stress tests on one proposal
  aios paper-review  — preflight one proposal without changing the account
  aios paper-execute — explicitly simulate an approved proposal
  aios paper-mark    — update simulated holdings through a reviewed close
  aios paper-status  — show local simulation state in plain language
  aios refresh-us-daily — recoverably certify the latest completed U.S. session
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
import os
import platform
import shlex
import subprocess
import sys
from contextlib import nullcontext, redirect_stdout, suppress
from datetime import UTC, date, datetime
from functools import wraps
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import duckdb
import typer
from rich.console import Console
from rich.table import Table

from aios import __version__
from aios.artifacts import publish_text_write_once
from aios.config import settings
from aios.readiness import assess_us_readiness
from aios.storage.store import get_store, store_scope

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
READINESS_REPORT_DIRECTORY = Path("data/reports/readiness")
PAPER_PROPOSAL_DIRECTORY = Path("data/paper/proposals")


def _exclusive_project_operation(operation: str):
    """Serialize supported workflows that mutate cross-store governed state."""

    def decorate(command):
        @wraps(command)
        def guarded(*args, **kwargs):
            from aios.maintenance import (
                MaintenanceLockBusyError,
                MaintenanceLockError,
                project_maintenance_lock,
            )

            previous_umask = os.umask(0o077)
            try:
                try:
                    with project_maintenance_lock(
                        settings.project_root,
                        operation=operation,
                    ):
                        return command(*args, **kwargs)
                except MaintenanceLockBusyError as exc:
                    console.print(
                        f"[yellow]Another AIOS mutation workflow is already running.[/yellow] {exc}"
                    )
                    raise typer.Exit(code=75) from exc
                except MaintenanceLockError as exc:
                    console.print(f"[red]AIOS could not establish its mutation lock:[/red] {exc}")
                    raise typer.Exit(code=1) from exc
            finally:
                os.umask(previous_umask)

        return guarded

    return decorate


def _project_path(path: Path) -> Path:
    """Resolve a CLI data path consistently when invoked outside the repo."""
    return path if path.is_absolute() else settings.project_root / path


def _absolute_without_symlink_resolution(path: Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _reject_output_symlink_ancestors(path: Path, *, label: str) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ValueError(f"{label} path cannot contain symlinks: {path}")


def _validate_generated_output_location(path: Path, *, label: str) -> Path:
    """Keep generated artifacts away from code and governed runtime state."""

    root = Path(settings.project_root).expanduser().resolve()
    candidate = _absolute_without_symlink_resolution(_project_path(path))
    _reject_output_symlink_ancestors(candidate, label=label)
    if candidate.resolve(strict=False) != candidate:
        raise ValueError(f"{label} path cannot resolve through a symlink: {candidate}")

    if _is_within(candidate, root) and not _is_within(candidate, root / "data"):
        raise ValueError(f"{label} inside the project must stay under the data directory")

    protected_directories = (
        root / "data" / "raw",
        root / "data" / "paper",
        root / "data" / "operations",
        root / "data" / "backups",
        root / "backups",
    )
    for protected in protected_directories:
        if candidate == protected or _is_within(candidate, protected):
            raise ValueError(f"{label} cannot target governed AIOS state: {candidate}")

    duckdb_setting = Path(getattr(settings, "duckdb_path", "data/aios.duckdb"))
    operations_setting = Path(
        getattr(settings, "operations_db_path", "data/operations/alerts.sqlite3")
    )
    database_files = (
        _absolute_without_symlink_resolution(_project_path(duckdb_setting)),
        _absolute_without_symlink_resolution(_project_path(operations_setting)),
    )
    protected_files = {
        *database_files,
        root / "data" / "operations" / "maintenance.lock",
    }
    protected_files.update(
        Path(f"{database}{suffix}") for database in database_files for suffix in ("-wal", "-shm")
    )
    if candidate in protected_files:
        raise ValueError(f"{label} cannot target governed AIOS state: {candidate}")
    return candidate


def _resolve_generated_output_path(
    path: Path,
    *,
    label: str,
    suffix: str | None = None,
) -> Path:
    """Resolve one generated file as an immutable, write-once artifact."""

    destination = _validate_generated_output_location(path, label=label)
    if suffix is not None and destination.suffix.lower() != suffix.lower():
        raise ValueError(f"{label} must use a {suffix} file")
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"{label} already exists: {destination}")
    return destination


def _resolve_generated_output_directory(path: Path, *, label: str) -> Path:
    """Resolve a mutable artifact workspace without aliases to governed files."""

    destination = _validate_generated_output_location(path, label=label)
    if destination.exists() and not destination.is_dir():
        raise ValueError(f"{label} must be a directory: {destination}")
    if destination.exists():
        for entry in destination.rglob("*"):
            if entry.is_symlink():
                raise ValueError(f"{label} cannot contain symlinks: {entry}")
            if entry.is_file() and entry.stat().st_nlink != 1:
                raise ValueError(f"{label} cannot contain hard-linked files: {entry}")
    return destination


def _resolve_paper_proposal_output_path(path: Path) -> Path:
    """Constrain proposal writes to the governed proposal namespace."""

    root = Path(settings.project_root).expanduser().resolve()
    allowed = (root / PAPER_PROPOSAL_DIRECTORY).resolve()
    candidate = _absolute_without_symlink_resolution(_project_path(path))
    _reject_output_symlink_ancestors(candidate, label="paper proposal output")
    if candidate.resolve(strict=False) != candidate or not _is_within(candidate, allowed):
        raise ValueError(
            f"paper proposal output must stay under {PAPER_PROPOSAL_DIRECTORY.as_posix()}"
        )
    if candidate == allowed or candidate.suffix.lower() != ".json":
        raise ValueError(
            "paper proposal output must be a .json file under "
            f"{PAPER_PROPOSAL_DIRECTORY.as_posix()}"
        )
    if candidate.exists() and (not candidate.is_file() or candidate.stat().st_nlink != 1):
        raise ValueError(f"paper proposal output must be one regular unaliased file: {candidate}")
    return candidate


def _has_unresolved_registered_proposal(
    trial_payload: dict[str, Any],
    account_payload: dict[str, Any],
) -> bool:
    """Fail closed unless every registered proposal has an exact execution record."""

    proposals = trial_payload.get("proposals")
    executions = account_payload.get("executions")
    if not isinstance(proposals, list):
        raise ValueError("forward proposal lifecycle evidence is invalid")
    if not isinstance(executions, list):
        raise ValueError("paper execution lifecycle evidence is invalid")

    executed_ids: set[str] = set()
    for execution in executions:
        if not isinstance(execution, dict):
            raise ValueError("paper execution lifecycle evidence is invalid")
        proposal_id = execution.get("proposal_id")
        if not isinstance(proposal_id, str) or not proposal_id.strip():
            raise ValueError("paper execution lifecycle evidence is invalid")
        executed_ids.add(proposal_id)

    for proposal in proposals:
        if not isinstance(proposal, dict):
            raise ValueError("forward proposal lifecycle evidence is invalid")
        proposal_id = proposal.get("proposal_id")
        if not isinstance(proposal_id, str) or not proposal_id.strip():
            raise ValueError("forward proposal lifecycle evidence is invalid")
        if proposal_id not in executed_ids:
            return True
    return False


def _resolve_readiness_report_path(path: Path) -> Path:
    """Restrict optional readiness artifacts to their write-once namespace."""

    root = settings.project_root.resolve()
    allowed = (root / READINESS_REPORT_DIRECTORY).resolve()
    requested = Path(path).expanduser()
    candidate = requested if requested.is_absolute() else root / requested
    for ancestor in (candidate, *candidate.parents):
        if ancestor.is_symlink():
            raise ValueError(f"readiness report path cannot contain symlinks: {candidate}")
        if ancestor == root:
            break
    destination = candidate.resolve()
    try:
        relative = destination.relative_to(allowed)
    except ValueError as exc:
        raise ValueError(
            f"readiness JSON output must stay under {READINESS_REPORT_DIRECTORY.as_posix()}"
        ) from exc
    if relative == Path(".") or destination.suffix.lower() != ".json":
        raise ValueError(
            "readiness JSON output must be a .json file under "
            f"{READINESS_REPORT_DIRECTORY.as_posix()}"
        )
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"readiness report already exists: {destination}")
    return destination


def _publish_text_write_once(path: Path, text: str) -> None:
    """Publish one same-filesystem artifact atomically without overwriting."""

    try:
        publish_text_write_once(path, text)
    except FileExistsError:
        raise ValueError(f"readiness report already exists: {path}") from None


def _incident_action_cli_arguments(
    actor: str,
    note: str,
    evidence_sha256: str,
) -> tuple[str, str, str]:
    """Validate mutation authority before an operations store can be opened."""
    normalized_actor = str(actor).strip()
    normalized_note = str(note).strip()
    normalized_sha256 = str(evidence_sha256).strip().lower()
    if not normalized_actor:
        raise ValueError("incident actor is required")
    if not normalized_note:
        raise ValueError("incident audit note is required")
    if len(normalized_actor) > 4000:
        raise ValueError("incident actor is too long")
    if len(normalized_note) > 4000:
        raise ValueError("incident audit note is too long")
    if len(normalized_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in normalized_sha256
    ):
        raise ValueError("incident evidence must be a 64-character SHA-256")
    return normalized_actor, normalized_note, normalized_sha256


def _incident_resolution_outcome_cli(outcome: str) -> str:
    normalized = str(outcome).strip()
    if normalized not in {"verified_recovery", "false_positive"}:
        raise ValueError(
            "incident outcome must be verified_recovery or false_positive"
        )
    return normalized


def _incident_evidence_cli_argument(evidence_sha256: str) -> str:
    normalized = str(evidence_sha256).strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("incident evidence must be a 64-character SHA-256")
    return normalized


def _emit_operational_alert(alert: Any) -> None:
    """Persist an application alert without changing the originating job result."""
    try:
        from aios.alerts import get_alert_store

        get_alert_store().emit(alert)
    except Exception as exc:
        console.print(f"[yellow]Local incident recording failed:[/yellow] {exc}")


def _emit_operational_alert_without_schema_migration(alert: Any) -> None:
    """Record an alert only when the incident ledger is already current.

    Backup must preserve the exact pre-migration state. Its failure path must
    therefore never initialize or migrate the operations database before a
    verified snapshot exists.
    """
    try:
        from aios.alerts import get_alert_store

        get_alert_store(
            _project_path(settings.operations_db_path),
            read_only=True,
        )
    except Exception as exc:
        console.print(
            "[yellow]Local incident recording was deferred to preserve the "
            f"pre-migration backup boundary:[/yellow] {exc}"
        )
        return
    _emit_operational_alert(alert)


def _record_scheduler_runtime_recovery(
    status: dict[str, dict[str, Any]],
) -> None:
    """Attest only a complete live scheduler observation for the exact generation."""
    try:
        from aios.alerts import (
            build_scheduler_recovery_evidence,
            get_alert_store,
        )

        store = get_alert_store()
        context = store.recovery_context("scheduler:runtime-unverified")
        if context is None:
            return
        recovery = build_scheduler_recovery_evidence(context, status)
        store.resolve_fingerprint(
            "scheduler:runtime-unverified",
            recovery=recovery,
        )
    except Exception as exc:
        console.print(
            "[yellow]Scheduler recovery attestation was not recorded:[/yellow] "
            f"{exc}"
        )


def _record_daily_cycle_recovery(run_id: str) -> None:
    """Resolve only the matching daily failure from an exact successful job row."""

    try:
        from aios.alerts import (
            build_daily_cycle_recovery_evidence,
            get_alert_store,
        )
        from aios.daily import DAILY_JOB_NAME

        store = get_alert_store()
        context = store.recovery_context("daily:us-cycle:failure")
        if context is None:
            return
        job = store.latest_job(DAILY_JOB_NAME)
        if job is None or job.run_id != run_id:
            raise ValueError("latest daily job does not match the completed run")
        target = date.fromisoformat(job.target_session)
        readiness = assess_us_readiness(
            target,
            purpose="paper",
            store=get_store(),
            today=target,
        )
        latest_event = store.events(context.incident_id, limit=1)[0]
        if (
            latest_event.get("event_type") == "resolved"
            and latest_event.get("proof_kind") == "daily_cycle_certified_v3"
        ):
            return
        recovery = build_daily_cycle_recovery_evidence(
            context,
            job,
            readiness,
            assessed_at=datetime.now(UTC),
        )
        store.resolve_fingerprint(
            "daily:us-cycle:failure",
            recovery=recovery,
        )
    except Exception as exc:
        console.print(
            "[yellow]Daily-cycle recovery attestation was not recorded:[/yellow] "
            f"{exc}"
        )


def _resolve_operational_alert(fingerprint: str) -> None:
    """Compatibility no-op: evidence-free incident closure is disabled."""
    del fingerprint


@app.command()
def doctor() -> None:
    """Verify environment, dependencies, DB, and live connectivity."""
    console.rule(f"[bold]AI Investment OS v{__version__} — doctor[/bold]")
    ok = True

    # Env
    console.print(f"[cyan]SEC User-Agent:[/cyan] {settings.sec_user_agent}")
    from aios.config import secret_value

    console.print(f"[cyan]FRED API key set:[/cyan] {bool(secret_value(settings.fred_api_key))}")
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
        with store_scope(read_only=True) as store:
            counts = store.table_rowcounts()
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
            help=(
                "Strict mode records incident transitions and fails on blockers; "
                "report-only performs a side-effect-free operating read."
            ),
        ),
    ] = True,
) -> None:
    """Show whether normal research and paper monitoring are safe to open."""
    from aios.forward import DEFAULT_FORWARD_RELATIVE_PATH, assess_forward_trial
    from aios.paper import (
        DEFAULT_ACCOUNT_RELATIVE_PATH,
        latest_paper_decision_date,
        paper_account_summary,
    )

    try:
        with store_scope(read_only=True) as store:
            decision_date = latest_paper_decision_date(store)
            report = assess_us_readiness(decision_date, purpose="paper", store=store)
            account_path = settings.project_root / DEFAULT_ACCOUNT_RELATIVE_PATH
            paper_detail = "not initialized; research remains available"
            paper_ok = True
            forward_detail = "not frozen; paper results are not an untouched forward trial"
            forward_ok = True
            forward_present = False
            forward_issue_count = 0
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
                forward_path = settings.project_root / DEFAULT_FORWARD_RELATIVE_PATH
                if forward_path.exists():
                    forward_present = True
                    try:
                        forward = assess_forward_trial(
                            settings.project_root,
                            forward_path,
                            account_path,
                        )
                        forward_ok = forward.ready
                        forward_issue_count = len(forward.issues)
                        forward_detail = (
                            f"unchanged; {forward.registered_proposals} registered proposal(s)"
                            if forward.ready
                            else f"blocked; {forward_issue_count} policy issue(s) require review"
                        )
                    except Exception as exc:
                        forward_ok = False
                        forward_issue_count = 1
                        forward_detail = f"blocked; forward evidence could not be verified ({exc})"
    except duckdb.IOException as exc:
        from aios.alerts import Alert, AlertSeverity

        if strict:
            _emit_operational_alert(
                Alert(
                    code="health_check_failed",
                    severity=AlertSeverity.CRITICAL,
                    title="AIOS health check could not open the database",
                    body="The operating health check failed before readiness could be verified.",
                    dedup_key="health:execution",
                    source_job="aios health",
                    payload={"error_type": type(exc).__name__},
                )
            )
        console.print(
            "[red]Health check could not open the local database.[/red] Close the "
            "dashboard or another AIOS command, then retry."
        )
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        from aios.alerts import Alert, AlertSeverity

        if strict:
            _emit_operational_alert(
                Alert(
                    code="health_check_failed",
                    severity=AlertSeverity.CRITICAL,
                    title="AIOS health check failed",
                    body="The operating health check failed before a safe result was produced.",
                    dedup_key="health:execution",
                    source_job="aios health",
                    payload={"error_type": type(exc).__name__},
                )
            )
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
        "Forward-test policy",
        (
            "[green]unchanged[/green]"
            if forward_present and forward_ok
            else "[yellow]not frozen[/yellow]"
            if not forward_present
            else "[red]blocked[/red]"
        ),
        forward_detail,
    )
    table.add_row(
        "Broker orders",
        "[yellow]disabled[/yellow]",
        "No broker is connected; this installation cannot place an order.",
    )
    console.print(table)
    decision_date_label = (
        "Certified decision date" if report.ready else "Blocked decision-date candidate"
    )
    console.print(
        f"{decision_date_label}: {decision_date}. Source dates — prices: "
        f"{report.raw_prices_through}; filings: "
        f"{report.fundamentals_through}; macro releases: "
        f"{report.macro_releases_through}."
    )
    if report.raw_prices_through and report.raw_prices_through > decision_date.isoformat():
        console.print(
            "Newer reviewed prices may value existing simulated holdings, but they do not "
            "advance new decisions until the dated universe is certified too."
        )
    research_healthy = report.ready and integrity.status != "fail" and paper_ok
    forward_blocked = forward_present and not forward_ok
    healthy = research_healthy and not forward_blocked
    if strict:
        if not research_healthy:
            from aios.alerts import Alert, AlertSeverity

            _emit_operational_alert(
                Alert(
                    code="readiness_blocked",
                    severity=AlertSeverity.CRITICAL,
                    title="Paper research readiness became blocked",
                    body="One or more fail-closed research-data gates require review.",
                    dedup_key="readiness:paper",
                    source_job="aios health",
                    payload={
                        "decision_date": str(decision_date),
                        "blockers": [check.check for check in report.blockers],
                        "paper_state_verified": paper_ok,
                    },
                )
            )
        if forward_blocked:
            from aios.alerts import Alert, AlertSeverity

            _emit_operational_alert(
                Alert(
                    code="forward_policy_drift",
                    severity=AlertSeverity.CRITICAL,
                    title="Frozen forward policy drifted",
                    body="Research data remains separate; simulated execution is blocked.",
                    dedup_key="forward:drift",
                    source_job="aios health",
                    payload={
                        "decision_date": str(decision_date),
                        "issue_count": forward_issue_count,
                    },
                )
            )

    if healthy:
        console.print(
            "[bold green]HEALTHY for supervised research and paper monitoring.[/bold green]"
        )
    elif research_healthy and forward_blocked:
        console.print(
            "[bold yellow]RESEARCH READY — forward paper execution is blocked until "
            "the policy trial is prospectively replaced.[/bold yellow]"
        )
    else:
        console.print("[bold red]BLOCKED — do not create or record a paper proposal.[/bold red]")
    if strict and not healthy:
        raise typer.Exit(code=1)


@app.command("preflight")
def preflight(
    proposal: Annotated[
        Path | None,
        typer.Option(
            "--proposal",
            help="Optional path; it must match an active-trial registration.",
        ),
    ] = None,
    review_paper: Annotated[
        bool,
        typer.Option(
            "--review-paper/--timing-only",
            help=(
                "Run the full governed paper review when its window is open; "
                "the default performs only the lightweight timing check."
            ),
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit only canonical machine-readable JSON."),
    ] = False,
    required: Annotated[
        list[str] | None,
        typer.Option(
            "--require",
            help=(
                "Fail unless this capability is available; repeat for research, "
                "proposal_creation, stress_review, paper_recording, operations, "
                "or real_capital."
            ),
        ),
    ] = None,
) -> None:
    """Report independent read-only capability states and one safe next action."""
    from contextlib import redirect_stderr, redirect_stdout
    from io import StringIO

    def fail_safely(exc: Exception, schema_version: str) -> None:
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "schema_version": schema_version,
                        "error": {
                            "type": type(exc).__name__,
                            "detail": str(exc),
                        },
                        "read_only": True,
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            console.print(f"[red]Operator preflight failed safely:[/red] {exc}")
            console.print(
                "No account, proposal, trial, incident, database, or broker state was changed."
            )

    requested = tuple(dict.fromkeys(required or ()))
    try:
        from aios.operator_preflight import (
            CAPABILITY_KEYS,
            PREFLIGHT_SCHEMA_VERSION,
            assess_operator_preflight,
        )
    except Exception as exc:
        fail_safely(exc, "operator-preflight.v1")
        raise typer.Exit(code=1) from exc

    unknown = [scope for scope in requested if scope not in CAPABILITY_KEYS]
    if unknown:
        allowed = ", ".join(CAPABILITY_KEYS)
        console.print(
            f"[red]Operator preflight refused:[/red] unknown capability "
            f"{unknown[0]!r}; choose one of: {allowed}"
        )
        raise typer.Exit(code=2)

    try:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            snapshot = assess_operator_preflight(
                proposal_path=_project_path(proposal) if proposal is not None else None,
                review_paper=review_paper,
            )
    except Exception as exc:
        fail_safely(exc, PREFLIGHT_SCHEMA_VERSION)
        raise typer.Exit(code=1) from exc

    unmet = [scope for scope in requested if not snapshot.capability(scope).available]
    if json_output:
        typer.echo(snapshot.canonical_json())
    else:
        console.rule("[bold]AIOS operator preflight[/bold]")
        console.print(f"Checked at: {snapshot.checked_at}")
        console.print(f"Certified decision date: {snapshot.decision_date or 'unavailable'}")
        console.print(f"Registered proposal: {snapshot.proposal_path or 'none'}")
        table = Table()
        table.add_column("Capability")
        table.add_column("State")
        table.add_column("Available now")
        table.add_column("Plain-language detail")
        for key in CAPABILITY_KEYS:
            capability = snapshot.capability(key)
            color = "green" if capability.available else "yellow"
            if capability.state in {"blocked", "critical", "expired", "unavailable"}:
                color = "red"
            table.add_row(
                capability.label,
                f"[{color}]{capability.state}[/{color}]",
                "yes" if capability.available else "no",
                capability.detail,
            )
        console.print(table)
        console.rule("[bold]Next safe action[/bold]")
        console.print(f"[bold]{snapshot.next_action.title}[/bold]")
        console.print(snapshot.next_action.detail)
        if snapshot.next_action.command is not None:
            console.print(f"[bold]{snapshot.next_action.command}[/bold]")
        elif snapshot.next_action.kind == "human_decision":
            console.print(
                "[yellow]Human confirmation is required; preflight intentionally "
                "generated no state-changing command.[/yellow]"
            )
        else:
            console.print("[yellow]Wait — no command should be run for this state.[/yellow]")
        console.print(
            "[dim]Read-only proof: no refresh, proposal, simulation record, incident "
            "transition, database write, or broker action was performed.[/dim]"
        )
    if unmet:
        raise typer.Exit(code=1)


@app.command("backup")
@_exclusive_project_operation("backup")
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
    from aios.storage.store import checkpoint_database_for_backup

    try:
        database_path = _project_path(settings.duckdb_path)
        checkpoint_database_for_backup(database_path)
        destination = _project_path(output) if output is not None else None
        result = create_local_backup(
            settings.project_root,
            database_path,
            operations_database_path=_project_path(settings.operations_db_path),
            output=destination,
            application_version=__version__,
        )
    except duckdb.IOException as exc:
        from aios.alerts import Alert, AlertSeverity

        _emit_operational_alert_without_schema_migration(
            Alert(
                code="backup_failed",
                severity=AlertSeverity.CRITICAL,
                title="Verified local backup failed",
                body="The backup could not acquire and checkpoint the analytical database.",
                dedup_key="backup:local",
                source_job="aios backup",
                payload={"error_type": type(exc).__name__},
            )
        )
        console.print(
            "[red]Backup could not lock the local database.[/red] Close the dashboard "
            "and any other AIOS command, then retry."
        )
        raise typer.Exit(code=1) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        from aios.alerts import Alert, AlertSeverity

        _emit_operational_alert_without_schema_migration(
            Alert(
                code="backup_failed",
                severity=AlertSeverity.CRITICAL,
                title="Verified local backup failed",
                body="The local backup did not complete verification.",
                dedup_key="backup:local",
                source_job="aios backup",
                payload={"error_type": type(exc).__name__},
            )
        )
        console.print(f"[red]Backup failed safely:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[bold green]Verified backup created:[/bold green] {result.path}")
    console.print(
        f"{result.files} file(s), {result.bytes:,} bytes; manifest SHA-256 "
        f"{result.manifest_sha256}."
    )
    console.print("The backup excludes .env, logs, caches, and backtest artifacts.")


@app.command("upgrade-local-state")
def upgrade_local_state_command(
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm",
            help="Confirm backup-first rehearsal and migration of local databases.",
        ),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option(
            "--backup-output",
            help=(
                "Explicit pre-upgrade directory under project backups/; must not "
                "already exist."
            ),
        ),
    ] = None,
) -> None:
    """Create an exact backup, rehearse migrations, then upgrade live state."""
    if not confirm:
        console.print(
            "[red]Local state upgrade refused:[/red] review the release and rerun "
            "with --confirm. No database or incident migration was opened."
        )
        raise typer.Exit(code=1)

    from aios.local_state_upgrade import upgrade_local_state
    from aios.maintenance import MaintenanceLockBusyError

    try:
        result = upgrade_local_state(
            settings.project_root,
            _project_path(settings.duckdb_path),
            _project_path(settings.operations_db_path),
            application_version=__version__,
            output=_project_path(output) if output is not None else None,
            confirm=True,
        )
    except MaintenanceLockBusyError as exc:
        console.print(
            f"[yellow]Another AIOS mutation workflow is already running.[/yellow] {exc}"
        )
        raise typer.Exit(code=75) from exc
    except (OSError, RuntimeError, TypeError, ValueError, duckdb.Error) as exc:
        console.print(f"[red]Local state upgrade failed:[/red] {exc}")
        console.print(
            "[yellow]Do not bypass the maintenance lease or retry through another "
            "writer. Preserve any reported pre-upgrade backup for review.[/yellow]"
        )
        raise typer.Exit(code=1) from exc

    console.print("[bold green]Local state upgrade verified.[/bold green]")
    console.print(
        f"Operations schema {result.operations_schema_before} → "
        f"{result.operations_schema_after}; {result.fundamentals:,} fundamentals "
        f"and {result.fundamental_versions:,} immutable versions."
    )
    console.print(f"Pre-upgrade backup: {result.backup.path}")
    console.print(f"Upgrade journal: {result.journal_directory}")
    console.print(f"Upgrade receipt: {result.receipt}")


@app.command("upgrade-local-state-recover")
def recover_local_state_upgrade_command(
    journal: Annotated[
        Path,
        typer.Option(
            "--journal",
            help="Exact prepared upgrade-attempt journal directory.",
        ),
    ],
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm-recovery",
            help="Confirm deterministic reconciliation of the exact attempt.",
        ),
    ] = False,
) -> None:
    """Resume or verify one checksum-chained local state upgrade attempt."""
    if not confirm:
        console.print(
            "[red]Local state upgrade recovery refused:[/red] rerun with "
            "--confirm-recovery after reviewing the journal and backup."
        )
        raise typer.Exit(code=1)

    from aios.local_state_upgrade import recover_local_state_upgrade
    from aios.maintenance import MaintenanceLockBusyError

    try:
        result = recover_local_state_upgrade(
            settings.project_root,
            _project_path(journal),
            _project_path(settings.duckdb_path),
            _project_path(settings.operations_db_path),
            confirm=True,
        )
    except MaintenanceLockBusyError as exc:
        console.print(
            f"[yellow]Another AIOS mutation workflow is already running.[/yellow] {exc}"
        )
        raise typer.Exit(code=75) from exc
    except (OSError, RuntimeError, TypeError, ValueError, duckdb.Error) as exc:
        console.print(f"[red]Local state upgrade recovery failed:[/red] {exc}")
        console.print(
            "[yellow]Preserve the exact backup and journal; do not start another "
            "upgrade attempt.[/yellow]"
        )
        raise typer.Exit(code=1) from exc

    console.print("[bold green]Local state upgrade recovery verified.[/bold green]")
    console.print(f"Pre-upgrade backup: {result.backup.path}")
    console.print(f"Upgrade journal: {result.journal_directory}")
    console.print(f"Verified receipt: {result.receipt}")


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


@app.command("restore-drill")
def restore_drill(
    path: Annotated[Path, typer.Argument(help="Verified backup directory to drill.")],
) -> None:
    """Restore into a disposable project and certify DB plus raw evidence."""
    from aios.operations import drill_local_backup

    try:
        result = drill_local_backup(
            _project_path(path),
            application_version=__version__,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Non-destructive restore drill failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[bold green]Non-destructive restore drill passed:[/bold green] {result.source}")
    console.print(
        f"{result.files} file(s), {result.bytes:,} bytes; "
        f"{result.raw_payloads} raw payload(s), "
        f"{result.replayed_snapshots} parsed replay(s), "
        f"{result.hard_failures} hard validation failure(s)."
    )
    console.print(f"Manifest SHA-256 {result.manifest_sha256}. Live state was untouched.")


@app.command("verify-raw-snapshots")
def verify_raw_snapshot_command() -> None:
    """Verify every registered immutable provider payload."""
    from aios.raw_snapshots import verify_raw_snapshots

    try:
        with store_scope(read_only=True) as store:
            result = verify_raw_snapshots(store=store)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Raw snapshot verification failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        "[bold green]Raw snapshots verified:[/bold green] "
        f"{result.payloads} payload(s), {result.original_bytes:,} original bytes, "
        f"{result.stored_bytes:,} stored bytes, "
        f"{result.replayed_snapshots} parsed replay(s)."
    )


@app.command("review-universe-current")
@_exclusive_project_operation("review-universe-current")
def review_universe_current() -> None:
    """Archive free source evidence and extend only an unchanged S&P 500 universe."""
    from aios.alerts import Alert, AlertSeverity
    from aios.universe_rollforward import roll_forward_sp500_coverage

    try:
        result = roll_forward_sp500_coverage()
    except duckdb.IOException as exc:
        _emit_operational_alert(
            Alert(
                code="universe_coverage_check_failed",
                severity=AlertSeverity.CRITICAL,
                title="Current universe review could not open the database",
                body="The dated reference window was not changed.",
                dedup_key="universe:coverage:execution",
                source_job="aios review-universe-current",
                payload={"error_type": type(exc).__name__},
            )
        )
        console.print(
            "[red]Universe review could not open DuckDB.[/red] Close the dashboard "
            "or another AIOS command, then retry. No reference dates were changed."
        )
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        _emit_operational_alert(
            Alert(
                code="universe_coverage_check_failed",
                severity=AlertSeverity.CRITICAL,
                title="Current universe evidence check failed",
                body=(
                    "Free source evidence could not be certified; all dated references "
                    "stayed closed."
                ),
                dedup_key="universe:coverage:execution",
                source_job="aios review-universe-current",
                payload={"error_type": type(exc).__name__},
            )
        )
        console.print(f"[red]Universe review failed safely:[/red] {exc}")
        console.print("No membership, identity, CIK, issuer, or provider date was extended.")
        raise typer.Exit(code=1) from exc

    table = Table(title="Current S&P 500 evidence review")
    table.add_column("evidence")
    table.add_column("result")
    table.add_row("Previous certified date", result.prior_coverage_through)
    table.add_row("Newest eligible market close", result.requested_coverage_through)
    table.add_row("Reviewed member set", f"{result.member_count} stocks")
    table.add_row("Official release records checked", str(result.official_release_count))
    table.add_row("Unreviewed change announcements", str(result.relevant_release_count))
    table.add_row("Identity/component mismatches", str(result.identity_mismatch_count))
    console.print(table)
    if result.review_required:
        _emit_operational_alert(
            Alert(
                code="universe_change_review_required",
                severity=AlertSeverity.WARNING,
                title="S&P 500 change or reference drift needs review",
                body="Automatic extension stopped before changing any dated reference.",
                dedup_key="universe:coverage:review",
                source_job="aios review-universe-current",
                payload={
                    "attestation_id": result.attestation_id,
                    "target": result.requested_coverage_through,
                    "announcement_candidates": result.relevant_release_count,
                    "identity_mismatches": result.identity_mismatch_count,
                },
            )
        )
        console.print(f"[bold red]REVIEW REQUIRED:[/bold red] {result.detail}")
        console.print(
            "The previous certified decision date remains usable; newer decisions stay blocked."
        )
        raise typer.Exit(code=1)

    if result.status == "up_to_date":
        console.print(
            "[bold green]Universe coverage is already current for the newest eligible "
            "close.[/bold green]"
        )
    else:
        console.print(
            "[bold green]Unchanged universe coverage extended safely through "
            f"{result.requested_coverage_through}.[/bold green]"
        )
        console.print(
            "Membership, security, issuer/CIK, and provider-symbol windows moved together "
            "in one transaction."
        )


@app.command("restore")
@_exclusive_project_operation("restore")
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
    from aios.storage.store import Store, checkpoint_database_for_backup

    restored_store = None
    try:
        database_path = _project_path(settings.duckdb_path)
        checkpoint_database_for_backup(database_path)
        result = restore_local_backup(
            _project_path(path),
            settings.project_root,
            database_path,
            operations_database_path=_project_path(settings.operations_db_path),
            application_version=__version__,
            confirm=confirm_restore,
        )
        restored_store = Store(database_path)
        failures = [row for row in restored_store.data_quality_report() if row["status"] == "fail"]
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
        if restored_store is not None:
            with suppress(Exception):
                restored_store.close()
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
    if result.operations_rescan_required:
        console.print(
            "[bold yellow]Operations review evidence is now stale.[/bold yellow] "
            "Inspect `aios anomaly-scan --preview`, then record the reviewed scan "
            "before relying on current case status."
        )
        if result.operations_incident_id:
            console.print(f"Tracked by operations incident: {result.operations_incident_id}")


@app.command("scheduler-install")
def scheduler_install(
    confirm_install: Annotated[
        bool,
        typer.Option(
            "--confirm-install/--no-confirm-install",
            help="Required acknowledgement before user-level timers are installed.",
        ),
    ] = False,
    keep_running_after_logout: Annotated[
        bool,
        typer.Option(
            "--keep-running-after-logout/--login-session-only",
            help=(
                "Keep the free local user scheduler alive when the desktop logs out. "
                "Recommended for automatic updates."
            ),
        ),
    ] = False,
) -> None:
    """Install core timers plus inactive optional email-delivery unit files."""
    from aios.scheduler import install_user_scheduler

    try:
        result = install_user_scheduler(
            settings.project_root,
            confirm=confirm_install,
            enable_linger=keep_running_after_logout,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Scheduler installation refused safely:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[bold green]Local scheduler installed:[/bold green] {len(result.timers)} "
        f"timers in {result.unit_dir}."
    )
    console.print(
        "After every U.S. weekday, one benchmark-first daily update runs at 02:00 "
        "New York time. It also checks three minutes after the user scheduler starts, "
        "so an interrupted or missed run catches up safely. "
        "Weekly filings run Saturday at 09:00, and verified backups Sunday at 09:00 "
        "(the weekly jobs use computer local time). The optional email worker files "
        "are installed but its timer remains off until an exact-config receipt test "
        "passes and `aios email-enable --confirm-enable` is run."
    )
    if result.linger_enabled:
        console.print(
            "[green]Keep-running-after-logout is enabled.[/green] The scheduler remains "
            "active while this computer is on, even after the desktop logs out."
        )
    else:
        console.print(
            "[yellow]Login-session-only mode.[/yellow] Startup catch-up is enabled, but "
            "a logged-out desktop cannot refresh until the next login. Reinstall with "
            "`--keep-running-after-logout` for unattended local updates."
        )
    console.print(
        "The dashboard uses short read-only database sessions. Scheduled writer jobs share "
        "a 30-minute wait queue, then each DuckDB open still waits up to five minutes for "
        "a brief reader overlap. Close the dashboard only for restore operations."
    )


@app.command("scheduler-status")
def scheduler_status(
    record_incidents: Annotated[
        bool,
        typer.Option(
            "--record-incidents/--report-only",
            help=(
                "Explicitly reconcile the scheduler-runtime incident, or keep the "
                "default report-only inspection."
            ),
        ),
    ] = False,
) -> None:
    """Show whether each supported local timer is installed and waiting."""
    from aios.scheduler import user_linger_status, user_scheduler_status

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
            last_result = f"failed ({state['service_result']}, exit {state['exit_status']})"
        last_event = state.get("last_run", state["last_trigger"])
        last_run = f"{last_result}; {last_event}" if last_event != "never" else last_result
        table.add_row(
            timer,
            "yes" if state["enabled"] else "no",
            "waiting" if state["active"] else "stopped",
            last_run,
            state["next_trigger"],
        )
    console.print(table)
    linger = user_linger_status()
    if linger is True:
        console.print("Keep running after desktop logout: [green]enabled[/green]")
    elif linger is False:
        console.print("Keep running after desktop logout: [yellow]disabled[/yellow]")
    else:
        console.print("Keep running after desktop logout: [yellow]not verified[/yellow]")
    from aios.scheduler import TIMER_NAMES

    runtime_unverified = set(status) != set(TIMER_NAMES) or any(
        state.get("runtime_verified") is not True for state in status.values()
    )
    if runtime_unverified:
        if record_incidents:
            from aios.alerts import Alert, AlertSeverity

            _emit_operational_alert(
                Alert(
                    code="scheduler_runtime_unverified",
                    severity=AlertSeverity.WARNING,
                    title="Local scheduler runtime could not be verified",
                    body=(
                        "Installed timer files are visible, but the user service manager "
                        "did not answer."
                    ),
                    dedup_key="scheduler:runtime-unverified",
                    source_job="aios scheduler-status",
                    payload={"timers": sorted(status)},
                )
            )
        console.print(
            "[yellow]The systemd user manager did not answer within 5 seconds.[/yellow] "
            "Installed/enabled files are shown, but live waiting and run times could not "
            "be verified. Try again from your normal logged-in terminal."
        )
    elif record_incidents:
        _record_scheduler_runtime_recovery(status)
    if not record_incidents:
        console.print(
            "[dim]Report-only inspection: the operating-incident lifecycle was not changed.[/dim]"
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
    console.print(f"[green]Local AIOS scheduler removed:[/green] {len(removed)} managed file(s).")


@app.command("alerts")
def alerts_command(
    unresolved: Annotated[
        bool,
        typer.Option(
            "--unresolved/--all",
            help="Show only open or acknowledged incidents, or include resolved history.",
        ),
    ] = False,
    blocking: Annotated[
        bool,
        typer.Option(
            "--blocking",
            help=(
                "Show exact operational blockers, including resolved incidents "
                "without a valid current-generation proof."
            ),
        ),
    ] = False,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=1000, help="Maximum incidents to display."),
    ] = 100,
) -> None:
    """Show durable local system failures and operating incidents."""
    from aios.alerts import get_alert_store

    try:
        if unresolved and blocking:
            raise ValueError("--unresolved and --blocking cannot be combined")
        incidents = get_alert_store(read_only=True).list(
            unresolved_only=unresolved,
            blocking_only=blocking,
            limit=limit,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Incident history is unavailable:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    table = Table(title="AIOS local incident history")
    table.add_column("incident ref", min_width=12, no_wrap=True)
    table.add_column("severity")
    table.add_column("state")
    table.add_column("resolution proof")
    table.add_column("last seen UTC", no_wrap=True)
    table.add_column("count", justify="right")
    table.add_column("summary")
    for incident in incidents:
        table.add_row(
            incident.incident_id[:12],
            incident.severity,
            incident.state,
            incident.resolution_proof_status,
            incident.last_seen_at[:16].replace("T", " "),
            str(incident.occurrence_count),
            incident.title,
        )
    console.print(table)
    if not incidents:
        console.print("No matching incidents are recorded.")
    console.print("Use `aios alert-show INCIDENT_REF` for structured failure evidence.")


@app.command("alert-show")
def alert_show(
    incident_id: Annotated[str, typer.Argument(help="Incident reference shown by `aios alerts`.")],
) -> None:
    """Show one incident and its append-only lifecycle events."""
    from aios.alerts import get_alert_store

    try:
        store = get_alert_store(read_only=True)
        incident = store.get(incident_id)
        events = store.events(incident_id)
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Incident detail is unavailable:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.rule(f"[bold]{incident.title}[/bold]")
    console.print(f"Incident: {incident.incident_id}")
    console.print(f"Code: {incident.code}")
    console.print(f"Severity/state: {incident.severity} / {incident.state}")
    console.print(f"Resolution proof: {incident.resolution_proof_status}")
    console.print(
        "Operational blocker: "
        + ("yes" if incident.operationally_blocking else "no")
    )
    console.print(f"Source: {incident.source_job}")
    console.print(f"First/last seen: {incident.first_seen_at} / {incident.last_seen_at}")
    console.print(f"Occurrences: {incident.occurrence_count}")
    console.print(f"Evidence SHA-256: {incident.evidence_sha256}")
    console.print(f"Detail: {incident.body}")
    console.print("Structured evidence:")
    console.print_json(json.dumps(incident.payload, sort_keys=True))
    event_table = Table(title="Lifecycle events")
    event_table.add_column("time")
    event_table.add_column("event")
    event_table.add_column("actor")
    event_table.add_column("outcome")
    event_table.add_column("note")
    for event in events:
        event_table.add_row(
            event["created_at"],
            event["event_type"],
            str(event.get("actor") or event.get("producer") or ""),
            str(
                event.get("resolution_outcome")
                or event.get("proof_kind")
                or ""
            ),
            str(event.get("note") or event.get("proof_error") or ""),
        )
    console.print(event_table)
    if (
        incident.fingerprint == "scheduler:runtime-unverified"
        and incident.resolution_proof_status in {"legacy_unproven", "invalid"}
    ):
        console.print(
            "[yellow]This scheduler incident still blocks unattended operation.[/yellow] "
            "Run `aios scheduler-status --record-incidents` from your normal "
            "logged-in session so every managed timer can be verified live."
        )
    if incident.resolution_proof_status == "legacy_unproven":
        console.print(
            "After reviewing the historical resolution evidence, append a manual "
            "proof with `aios alert-attest-resolution` and this exact evidence "
            "SHA-256. The incident projection will remain unchanged."
        )
    elif incident.resolution_proof_status == "invalid":
        console.print(
            "[red]This resolution proof is invalid and remains a forensic "
            "blocker.[/red] Manual attestation is refused."
        )
    if incident.state != "resolved":
        console.print(
            "Use this exact evidence SHA-256 with `aios alert-ack` or "
            "`aios alert-resolve`; stale evidence is refused."
        )


@app.command("alert-ack")
def alert_ack(
    incident_id: Annotated[
        str, typer.Argument(help="Unresolved incident reference to acknowledge.")
    ],
    actor: Annotated[
        str,
        typer.Option(
            "--actor",
            "--owner",
            help="Human actor or owner responsible for this incident review.",
        ),
    ],
    note: Annotated[
        str,
        typer.Option("--note", help="Required audit note describing the review."),
    ],
    evidence_sha256: Annotated[
        str,
        typer.Option(
            "--evidence-sha256",
            help="Exact current evidence hash shown by `aios alert-show`.",
        ),
    ],
) -> None:
    """Record that an operator has reviewed an unresolved incident."""
    try:
        actor, note, evidence_sha256 = _incident_action_cli_arguments(
            actor,
            note,
            evidence_sha256,
        )
        from aios.alerts import get_alert_store

        incident = get_alert_store().acknowledge(
            incident_id,
            actor=actor,
            note=note,
            expected_evidence_sha256=evidence_sha256,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Incident acknowledgement refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Incident acknowledged:[/green] {incident.incident_id}")


@app.command("alert-resolve")
def alert_resolve(
    incident_id: Annotated[str, typer.Argument(help="Incident reference to resolve explicitly.")],
    actor: Annotated[
        str,
        typer.Option(
            "--actor",
            "--owner",
            help="Human actor or owner responsible for the resolution.",
        ),
    ],
    note: Annotated[
        str,
        typer.Option("--note", help="Required evidence-based resolution note."),
    ],
    outcome: Annotated[
        str,
        typer.Option(
            "--outcome",
            help="Explicit manual outcome: verified_recovery or false_positive.",
        ),
    ],
    evidence_sha256: Annotated[
        str,
        typer.Option(
            "--evidence-sha256",
            help="Exact current evidence hash shown by `aios alert-show`.",
        ),
    ],
) -> None:
    """Resolve one incident while retaining its history."""
    try:
        actor, note, evidence_sha256 = _incident_action_cli_arguments(
            actor,
            note,
            evidence_sha256,
        )
        outcome = _incident_resolution_outcome_cli(outcome)
        from aios.alerts import get_alert_store

        incident = get_alert_store().resolve(
            incident_id,
            actor=actor,
            note=note,
            outcome=outcome,
            expected_evidence_sha256=evidence_sha256,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Incident resolution refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Incident resolved:[/green] {incident.incident_id}")


@app.command("alert-attest-resolution")
def alert_attest_resolution(
    incident_id: Annotated[
        str,
        typer.Argument(help="Legacy resolved incident reference to attest."),
    ],
    actor: Annotated[
        str,
        typer.Option(
            "--actor",
            "--owner",
            help="Human actor responsible for the historical resolution review.",
        ),
    ],
    note: Annotated[
        str,
        typer.Option(
            "--note",
            help="Required evidence-based note for the legacy resolution.",
        ),
    ],
    outcome: Annotated[
        str,
        typer.Option(
            "--outcome",
            help="Explicit manual outcome: verified_recovery or false_positive.",
        ),
    ],
    evidence_sha256: Annotated[
        str,
        typer.Option(
            "--evidence-sha256",
            help="Exact current evidence hash shown by `aios alert-show`.",
        ),
    ],
) -> None:
    """Append proof to a legacy resolution without changing its projection."""
    try:
        actor, note, evidence_sha256 = _incident_action_cli_arguments(
            actor,
            note,
            evidence_sha256,
        )
        outcome = _incident_resolution_outcome_cli(outcome)
        from aios.alerts import get_alert_store

        incident = get_alert_store().attest_legacy_resolution(
            incident_id,
            actor=actor,
            note=note,
            outcome=outcome,
            expected_evidence_sha256=evidence_sha256,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Incident resolution attestation refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]Legacy incident resolution attested:[/green] "
        f"{incident.incident_id}"
    )


@app.command("alert-reconcile-fundamentals")
def alert_reconcile_fundamentals(
    incident_id: Annotated[
        str,
        typer.Argument(help="Partial fundamentals incident reference."),
    ],
    evidence_sha256: Annotated[
        str,
        typer.Option(
            "--evidence-sha256",
            help="Exact current evidence hash shown by `aios alert-show`.",
        ),
    ],
    record: Annotated[
        bool,
        typer.Option(
            "--record/--preview",
            help="Append the producer proof, or validate it read-only.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the reconciliation proof as JSON."),
    ] = False,
) -> None:
    """Reconcile a partial refresh only after every named case is resolved."""

    from aios.alerts import get_alert_store

    try:
        evidence_sha256 = _incident_evidence_cli_argument(evidence_sha256)
        store = get_alert_store(read_only=not record)
        recovery = store.fundamentals_review_recovery(
            incident_id,
            expected_evidence_sha256=evidence_sha256,
        )
        incident = (
            store.resolve_fingerprint(
                recovery.fingerprint,
                recovery=recovery,
            )
            if record
            else None
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Fundamentals reconciliation refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    payload = {
        "schema_version": "fundamentals-review-reconciliation.v1",
        "status": "recorded" if record else "preview",
        "recorded": record,
        "incident_id": recovery.incident_id,
        "fingerprint": recovery.fingerprint,
        "generation_event_id": recovery.generation_event_id,
        "expected_evidence_sha256": recovery.expected_evidence_sha256,
        "observation": recovery.observation,
        "result": (
            {
                "state": incident.state,
                "resolution_proof_status": incident.resolution_proof_status,
                "operationally_blocking": incident.operationally_blocking,
            }
            if incident is not None
            else None
        ),
    }
    if json_output:
        console.print_json(json.dumps(payload, sort_keys=True))
        return
    if record:
        console.print(
            f"[green]Fundamentals incident reconciled:[/green] "
            f"{recovery.incident_id}"
        )
    else:
        console.print(
            f"[green]Fundamentals reconciliation is eligible:[/green] "
            f"{recovery.incident_id}"
        )
        console.print("No incident, research, readiness, paper, or broker state changed.")


@app.command("anomaly-scan")
def anomaly_scan(
    as_of: Annotated[
        datetime | None,
        typer.Option(
            "--as-of",
            formats=["%Y-%m-%d"],
            help=("Reviewed comparison date. Defaults to the latest certified research close."),
        ),
    ] = None,
    record: Annotated[
        bool,
        typer.Option(
            "--record/--preview",
            help=(
                "Persist deduplicated review cases, or only show the complete "
                "source-bound comparison."
            ),
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit one machine-readable scan report."),
    ] = False,
) -> None:
    """Detect governed data-quality cases without repairing research data."""
    from dataclasses import asdict

    from aios.alerts import get_alert_store
    from aios.anomalies import scan_sec_fundamental_coverage
    from aios.maintenance import (
        MaintenanceLockBusyError,
        project_maintenance_lock,
    )

    try:
        operation_guard = (
            project_maintenance_lock(
                settings.project_root,
                operation="anomaly-scan",
            )
            if record
            else nullcontext()
        )
        with operation_guard:
            with store_scope(read_only=True) as store:
                decision_date = as_of.date() if as_of is not None else None
                if decision_date is None:
                    readiness = assess_us_readiness(
                        purpose="historical_research",
                        store=store,
                    )
                    if readiness.certified_research_through is None:
                        raise ValueError(
                            "no certified research close is available; pass --as-of "
                            "only after the reviewed universe is initialized"
                        )
                    decision_date = date.fromisoformat(readiness.certified_research_through)
                scan = scan_sec_fundamental_coverage(
                    store=store,
                    as_of=decision_date,
                    project_root=settings.project_root,
                )
            cases = get_alert_store().record_anomaly_scan(scan) if record else ()
    except MaintenanceLockBusyError as exc:
        if json_output:
            console.print_json(
                json.dumps(
                    {
                        "schema_version": "data-quality-anomaly-scan.v1",
                        "status": "withheld",
                        "recorded": False,
                        "error": "another AIOS mutation workflow is running",
                    },
                    sort_keys=True,
                )
            )
        else:
            console.print(
                f"[yellow]Another AIOS mutation workflow is already running.[/yellow] {exc}"
            )
        raise typer.Exit(code=75) from exc
    except (OSError, RuntimeError, ValueError, duckdb.Error) as exc:
        if json_output:
            console.print_json(
                json.dumps(
                    {
                        "schema_version": "data-quality-anomaly-scan.v1",
                        "status": "withheld",
                        "recorded": False,
                        "error": str(exc),
                    },
                    sort_keys=True,
                )
            )
        else:
            console.print(f"[red]Data-quality scan withheld safely:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    payload = {
        "schema_version": "data-quality-anomaly-scan.v1",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "recorded" if record else "preview",
        "recorded": record,
        "scan": {
            "scan_id": scan.scan_id,
            "rule_bundle_version": scan.rule_bundle_version,
            "executed_rules": list(scan.executed_rules),
            "scope": scan.scope,
            "source_boundary_sha256": scan.source_boundary_sha256,
            "source_boundary_at": scan.source_boundary_at,
            "observation_count": len(scan.observations),
            "evidence": scan.evidence,
            "observations": [asdict(row) for row in scan.observations],
        },
        "cases": [asdict(row) for row in cases],
        "safety": {
            "research_data_changed": False,
            "readiness_overridden": False,
            "paper_state_changed": False,
            "broker_action": False,
        },
    }
    if json_output:
        console.print_json(json.dumps(payload, default=str, sort_keys=True))
        return

    mode = "Recorded" if record else "Preview"
    console.rule(f"[bold]{mode} data-quality review scan[/bold]")
    console.print(f"Reviewed comparison date: {decision_date.isoformat()}")
    console.print(f"Scan: {scan.scan_id}")
    console.print(
        "SEC fundamentals coverage: "
        f"{scan.evidence['covered_issuers']}/{scan.evidence['reviewed_issuers']} "
        f"({scan.evidence['coverage_rate']:.1%})"
    )
    console.print(
        f"Reviewed securities / issuers: {scan.evidence['reviewed_members']} / "
        f"{scan.evidence['reviewed_issuers']}"
    )
    table = Table(title="Evidence-bound review findings")
    table.add_column("severity")
    table.add_column("company")
    table.add_column("subject")
    table.add_column("confidence")
    table.add_column("summary")
    for observation in scan.observations:
        issuer = observation.evidence.get("issuer", {})
        table.add_row(
            observation.severity,
            str(issuer.get("canonical_ticker") or observation.subject_id),
            observation.subject_id,
            observation.confidence,
            observation.title,
        )
    console.print(table)
    if not scan.observations:
        console.print("[green]No SEC fundamentals coverage cases were detected.[/green]")
    elif record:
        console.print(
            f"[yellow]{len(cases)} governed review case(s) reconciled.[/yellow] "
            "No research value was changed."
        )
    else:
        console.print(
            "Preview only. Re-run with `--record` to reconcile the independent review ledger."
        )
    console.print(
        "The scan never repairs data, changes readiness, records a paper fill, "
        "or contacts a broker."
    )


@app.command("companyfacts-v3-plan")
def companyfacts_v3_plan(
    as_of: Annotated[
        datetime,
        typer.Option(
            "--as-of",
            formats=["%Y-%m-%d"],
            help="Decision date bounding reviewed identities and captured evidence.",
        ),
    ],
    issuer_ids: Annotated[
        list[str] | None,
        typer.Option(
            "--issuer-id",
            help="Optional reviewed issuer_id; repeat to restrict the plan.",
        ),
    ] = None,
    write_plan: Annotated[
        bool,
        typer.Option(
            "--write-plan",
            help="Publish the content-addressed review plan under data/reports.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit one machine-readable result."),
    ] = False,
) -> None:
    """Plan a Company Facts v3 replay without activation or provider access."""
    from aios.companyfacts_replay import (
        persist_companyfacts_v3_plan,
        preview_companyfacts_v3_replay,
    )

    try:
        decision_date = as_of.date()
        with store_scope(read_only=True) as store:
            preview = preview_companyfacts_v3_replay(
                settings.project_root,
                store=store,
                as_of=decision_date,
                issuer_ids=issuer_ids,
            )
        published = (
            persist_companyfacts_v3_plan(settings.project_root, preview)
            if write_plan
            else None
        )
    except (OSError, RuntimeError, TypeError, ValueError, duckdb.Error) as exc:
        if json_output:
            console.print_json(
                json.dumps(
                    {
                        "schema_version": "companyfacts-v3-plan-cli.v1",
                        "status": "withheld",
                        "error": str(exc),
                        "safety": {
                            "database_mutation": False,
                            "provider_fetch": False,
                            "paper_state_mutation": False,
                            "broker_action": False,
                            "activation": False,
                        },
                    },
                    sort_keys=True,
                )
            )
        else:
            console.print(f"[red]Company Facts replay plan withheld:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    envelope = {
        "schema_version": "companyfacts-v3-plan-cli.v1",
        "status": "published" if published is not None else "preview",
        "plan": preview.to_plan_envelope(),
        "publication": {
            "written": published is not None,
            "path": (
                published.relative_to(settings.project_root).as_posix()
                if published is not None
                else None
            ),
        },
        "safety": {
            "database_mutation": False,
            "provider_fetch": False,
            "paper_state_mutation": False,
            "broker_action": False,
            "activation": False,
        },
    }
    if json_output:
        console.print_json(json.dumps(envelope, sort_keys=True))
        return

    plan = preview.plan
    summary = plan["summary"]
    console.rule("[bold]Company Facts v3 replay plan[/bold]")
    console.print(f"Decision evidence through: {decision_date.isoformat()}")
    console.print(f"Plan SHA-256: {preview.plan_sha256}")
    console.print(
        "Eligible / ineligible issuers: "
        f"{summary['eligible_issuers']} / {summary['ineligible_issuers']}"
    )
    console.print(
        "Excluded source observations: "
        f"{summary['excluded_source_observations']}"
    )
    if published is not None:
        console.print(
            "[green]Review plan published:[/green] "
            f"{published.relative_to(settings.project_root)}"
        )
    else:
        console.print("Preview only. Use --write-plan to publish the exact plan.")
    console.print(
        "[yellow]Activation is unavailable.[/yellow] No database, provider, "
        "paper-account, or broker state was changed."
    )


@app.command("anomalies")
def anomalies_command(
    unresolved: Annotated[
        bool,
        typer.Option(
            "--unresolved/--all",
            help="Show unresolved review cases, or include resolved history.",
        ),
    ] = True,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=1000, help="Maximum cases to display."),
    ] = 100,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the case list as JSON."),
    ] = False,
) -> None:
    """Show governed data-quality review cases without changing their state."""
    from dataclasses import asdict

    from aios.alerts import get_alert_store

    try:
        store = get_alert_store(read_only=True)
        cases = store.anomaly_cases(unresolved_only=unresolved, limit=limit)
        summary = store.anomaly_summary()
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Data-quality case history is unavailable:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if json_output:
        console.print_json(
            json.dumps(
                {
                    "schema_version": "data-quality-anomaly-cases.v1",
                    "summary": summary,
                    "cases": [asdict(row) for row in cases],
                },
                default=str,
                sort_keys=True,
            )
        )
        return

    table = Table(title="AIOS data-quality review cases")
    table.add_column("case ref", min_width=12, no_wrap=True)
    table.add_column("severity")
    table.add_column("state")
    table.add_column("subject")
    table.add_column("owner")
    table.add_column("last seen", no_wrap=True)
    table.add_column("summary")
    for case in cases:
        table.add_row(
            case.case_id[:12],
            case.severity,
            case.state,
            case.subject_id,
            case.owner or "Unassigned",
            case.last_seen_at[:16].replace("T", " "),
            case.title,
        )
    console.print(table)
    if not cases:
        console.print("No matching data-quality review cases are recorded.")
    console.print("Use `aios anomaly-show CASE_REF` for source-bound evidence.")


@app.command("anomaly-show")
def anomaly_show(
    case_id: Annotated[
        str,
        typer.Argument(help="Case reference shown by `aios anomalies`."),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit case evidence and events as JSON."),
    ] = False,
) -> None:
    """Show one anomaly case and its append-only review history."""
    from dataclasses import asdict

    from aios.alerts import get_alert_store

    try:
        store = get_alert_store(read_only=True)
        case = store.anomaly_case(case_id)
        events = store.anomaly_case_events(case_id)
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Data-quality case detail is unavailable:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if json_output:
        console.print_json(
            json.dumps(
                {
                    "schema_version": "data-quality-anomaly-case.v1",
                    "case": asdict(case),
                    "events": events,
                },
                default=str,
                sort_keys=True,
            )
        )
        return

    console.rule(f"[bold]{case.title}[/bold]")
    console.print(f"Case: {case.case_id}")
    console.print(f"Rule: {case.rule_id}@{case.rule_version}")
    console.print(f"Severity/confidence: {case.severity} / {case.confidence}")
    console.print(f"State/owner: {case.state} / {case.owner or 'Unassigned'}")
    console.print(f"Subject: {case.subject_type} / {case.subject_id}")
    console.print(f"First/last seen: {case.first_seen_at} / {case.last_seen_at}")
    console.print(f"Occurrences: {case.occurrence_count}")
    console.print(f"Evidence SHA-256: {case.evidence_sha256}")
    console.print(f"Detail: {case.summary}")
    console.print("Old value:")
    console.print_json(json.dumps(case.old_value, sort_keys=True))
    console.print("New value:")
    console.print_json(json.dumps(case.new_value, sort_keys=True))
    console.print("Source-bound evidence:")
    console.print_json(json.dumps(case.evidence, sort_keys=True))
    console.print("Suggested checks:")
    for check in case.suggested_checks:
        console.print(f"  • {check}")
    event_table = Table(title="Review lifecycle")
    event_table.add_column("time")
    event_table.add_column("event")
    event_table.add_column("actor")
    event_table.add_column("note")
    for event in events:
        event_table.add_row(
            str(event.get("created_at") or ""),
            str(event.get("event_type") or ""),
            str(event.get("owner") or ""),
            str(event.get("note") or ""),
        )
    console.print(event_table)
    if case.state in {"open", "acknowledged"}:
        console.print(
            "Use this exact evidence SHA-256 with `aios anomaly-ack` or "
            "`aios anomaly-resolve`; stale evidence is refused."
        )


@app.command("anomaly-acceptance-check")
def anomaly_acceptance_check(
    case_id: Annotated[
        str,
        typer.Argument(help="Unresolved data-quality case reference."),
    ],
    evidence_sha256: Annotated[
        str,
        typer.Option(
            "--evidence-sha256",
            help="Exact evidence hash shown by `aios anomaly-show`.",
        ),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the validated contract as JSON."),
    ] = False,
) -> None:
    """Validate accepted-missingness evidence without changing case state."""

    from aios.alerts import get_alert_store

    try:
        contract = get_alert_store(read_only=True).review_anomaly_acceptance(
            case_id,
            expected_evidence_sha256=evidence_sha256,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Case acceptance check refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if json_output:
        console.print_json(json.dumps(contract, sort_keys=True))
        return
    console.print(
        "[green]Accepted-missingness contract verified:[/green] "
        f"{contract['contract_id']}"
    )
    console.print(
        "Coverage, readiness, score state, and predecessor facts remain unchanged."
    )


@app.command("anomaly-ack")
def anomaly_ack(
    case_id: Annotated[
        str,
        typer.Argument(help="Unresolved data-quality case reference."),
    ],
    owner: Annotated[
        str,
        typer.Option("--owner", help="Human owner responsible for this review."),
    ],
    note: Annotated[
        str,
        typer.Option("--note", help="Required audit note describing the first review."),
    ],
    evidence_sha256: Annotated[
        str,
        typer.Option(
            "--evidence-sha256",
            help="Exact evidence hash shown by `aios anomaly-show`.",
        ),
    ],
) -> None:
    """Assign and acknowledge one review case without changing research data."""
    from aios.alerts import get_alert_store

    try:
        case = get_alert_store().acknowledge_anomaly(
            case_id,
            owner=owner,
            note=note,
            expected_evidence_sha256=evidence_sha256,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Case acknowledgement refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Data-quality case acknowledged:[/green] {case.case_id} ({case.owner})")


@app.command("anomaly-resolve")
def anomaly_resolve(
    case_id: Annotated[
        str,
        typer.Argument(help="Data-quality case reference to disposition."),
    ],
    outcome: Annotated[
        str,
        typer.Option(
            "--outcome",
            help=("accepted, source_corrected, mapping_corrected, false_positive, or deferred."),
        ),
    ],
    note: Annotated[
        str,
        typer.Option("--note", help="Required evidence-based resolution note."),
    ],
    evidence_sha256: Annotated[
        str,
        typer.Option(
            "--evidence-sha256",
            help="Exact evidence hash shown by `aios anomaly-show`.",
        ),
    ],
    owner: Annotated[
        str | None,
        typer.Option(
            "--owner",
            help="Resolution owner; optional only when the case is already assigned.",
        ),
    ] = None,
    next_review: Annotated[
        datetime | None,
        typer.Option(
            "--next-review",
            formats=["%Y-%m-%d"],
            help="Required future review date when outcome is deferred.",
        ),
    ] = None,
    verification_scan: Annotated[
        str | None,
        typer.Option(
            "--verification-scan",
            help=(
                "Later same-scope scan with a distinct non-earlier source "
                "boundary, exact-rule execution, fingerprint absence, and "
                "source-provenanced clearance; required for source or mapping "
                "correction."
            ),
        ),
    ] = None,
) -> None:
    """Record one explicit disposition; never repair or waive a data gate."""
    from aios.alerts import get_alert_store

    try:
        case = get_alert_store().resolve_anomaly(
            case_id,
            outcome=outcome,
            note=note,
            expected_evidence_sha256=evidence_sha256,
            owner=owner,
            next_review_at=(
                f"{next_review.date().isoformat()}T00:00:00Z" if next_review is not None else None
            ),
            verification_scan_id=verification_scan,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Case disposition refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    state_label = "deferred" if case.state == "deferred" else "resolved"
    console.print(
        f"[green]Data-quality case {state_label}:[/green] {case.case_id} "
        f"({case.resolution_outcome})"
    )
    console.print("Research data and readiness gates were not changed.")


@app.command("alert-test")
def alert_test() -> None:
    """Verify local incident open and recovery recording without external delivery."""
    from aios.alerts import Alert, AlertSeverity, get_alert_store

    try:
        store = get_alert_store()
        incident = store.emit(
            Alert(
                code="local_alert_test",
                severity=AlertSeverity.INFO,
                title="Local alert path test",
                body="The independent incident ledger accepted a test event.",
                dedup_key="test:local-alert-path",
                source_job="aios alert-test",
                payload={"test": True},
                notify=False,
            )
        )
        store.resolve(
            incident.incident_id,
            actor="aios alert-test",
            note="Resolve the bounded local lifecycle test incident.",
            outcome="verified_recovery",
            expected_evidence_sha256=incident.evidence_sha256,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Local alert test failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[bold green]Local alert path passed:[/bold green] {incident.incident_id}")
    console.print(
        "The test incident was opened, logged, and resolved; no external message was sent."
    )


@app.command("notifications")
def notifications_command(
    pending: Annotated[
        bool,
        typer.Option(
            "--pending",
            help="Show only messages currently waiting for a configured delivery worker.",
        ),
    ] = False,
    needs_review: Annotated[
        bool,
        typer.Option(
            "--needs-review",
            help="Show only messages that exhausted retry policy.",
        ),
    ] = False,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=1000, help="Maximum messages to display."),
    ] = 100,
) -> None:
    """Inspect durable alert copies without sending an external message."""
    from aios.alerts import get_alert_store
    from aios.notifications import EMAIL_CHANNEL_NAME, EMAIL_ROUTE_ALIAS

    if pending and needs_review:
        console.print("[red]Choose either --pending or --needs-review, not both.[/red]")
        raise typer.Exit(code=1)
    state = "pending" if pending else ("dead_letter" if needs_review else None)
    try:
        store = get_alert_store(read_only=True)
        summary = store.notification_summary()
        messages = store.list_notifications(state=state, limit=limit)
        route = store.notification_route(
            EMAIL_CHANNEL_NAME,
            route_alias=EMAIL_ROUTE_ALIAS,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Notification outbox is unavailable:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    route_enabled = route is not None and route.state == "enabled"
    console.print(
        "[cyan]External email delivery:[/cyan] "
        + (
            "enabled for future incident changes."
            if route_enabled
            else "off — incidents and message copies remain safely local."
        )
    )
    console.print(
        "Held: {held}; waiting: {pending}; sending: {leased}; delivered: "
        "{delivered}; needs review: {dead_letter}.".format(**summary)
    )
    state_labels = {
        "held": "Held locally",
        "pending": "Waiting to retry",
        "leased": "Sending now",
        "delivered": "Sent",
        "dead_letter": "Needs review",
    }
    table = Table(title="AIOS notification outbox")
    table.add_column("notification ref", min_width=18, no_wrap=True)
    table.add_column("state")
    table.add_column("event")
    table.add_column("severity")
    table.add_column("created", no_wrap=True)
    table.add_column("attempts", justify="right")
    table.add_column("summary")
    for message in messages:
        table.add_row(
            message.notification_id[:20],
            state_labels[message.state],
            message.event_type,
            message.severity,
            message.created_at[:16].replace("T", " "),
            str(message.attempt_count),
            message.title,
        )
    console.print(table)
    if not messages:
        console.print("No matching notification messages are recorded.")
    console.print("Use `aios notification-show NOTIFICATION_REF` for message and attempt history.")


@app.command("notification-show")
def notification_show(
    notification_id: Annotated[
        str,
        typer.Argument(help="Notification reference shown by `aios notifications`."),
    ],
) -> None:
    """Show one immutable message and every attempted delivery."""
    from aios.alerts import get_alert_store

    try:
        store = get_alert_store(read_only=True)
        message = store.get_notification(notification_id)
        deliveries = store.notification_deliveries(notification_id)
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Notification detail is unavailable:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.rule(f"[bold]{message.title}[/bold]")
    console.print(f"Notification: {message.notification_id}")
    console.print(f"Incident: {message.incident_id or 'none (local test)'}")
    console.print(
        f"Event/severity/state: {message.event_type} / {message.severity} / {message.state}"
    )
    console.print(f"Source: {message.source_job}")
    console.print(f"Created/available: {message.created_at} / {message.available_at}")
    console.print(f"Attempts: {message.attempt_count}")
    console.print(f"Detail: {message.body}")
    console.print("Safe structured context:")
    console.print_json(json.dumps(message.payload, sort_keys=True))
    table = Table(title="Delivery attempts")
    table.add_column("attempt")
    table.add_column("channel")
    table.add_column("route")
    table.add_column("state")
    table.add_column("started")
    table.add_column("retry")
    table.add_column("safe error")
    for delivery in deliveries:
        table.add_row(
            str(delivery.attempt_number),
            delivery.channel,
            delivery.route_alias,
            delivery.state.replace("_", " "),
            delivery.started_at,
            delivery.retry_at or "—",
            delivery.error_type or "—",
        )
    console.print(table)
    if not deliveries:
        console.print("No delivery has been attempted. External alert delivery is off.")


@app.command("notification-test")
def notification_test() -> None:
    """Certify enqueue, lease, attempt, and completion without network access."""
    from aios.alerts import (
        AlertSeverity,
        NotificationRequest,
        get_alert_store,
    )
    from aios.notifications import LocalTestChannel, dispatch_notifications

    try:
        store = get_alert_store()
        message = store.enqueue_notification(
            NotificationRequest(
                idempotency_key=f"test:local-notification:{uuid4().hex}",
                event_type="test",
                severity=AlertSeverity.INFO,
                title="Local notification lifecycle test",
                body="This message is delivered only to the deterministic local test sink.",
                source_job="aios notification-test",
                payload={"external_delivery": False, "test": True},
            ),
            held=False,
        )
        result = dispatch_notifications(
            store,
            LocalTestChannel(),
            notification_id=message.notification_id,
        )
        completed = store.get_notification(message.notification_id)
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Local notification test failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if result.succeeded != 1 or completed.state != "delivered":
        console.print("[red]Local notification test did not reach a delivered state.[/red]")
        raise typer.Exit(code=1)
    console.print(
        f"[bold green]Local notification lifecycle passed:[/bold green] {message.notification_id}"
    )
    console.print(
        "The message was enqueued, leased, attempted, and completed locally. "
        "No network request, email, Slack message, or broker action occurred."
    )


@app.command("email-status")
def email_status() -> None:
    """Show whether SMTP is configured, tested, and durably enabled."""
    from aios.alerts import get_alert_store
    from aios.notifications import (
        EMAIL_CHANNEL_NAME,
        EMAIL_ROUTE_ALIAS,
        smtp_email_config,
    )
    from aios.scheduler import email_scheduler_status

    try:
        store = get_alert_store(read_only=True)
        route = store.notification_route(
            EMAIL_CHANNEL_NAME,
            route_alias=EMAIL_ROUTE_ALIAS,
        )
        summary = store.notification_summary()
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Email status is unavailable:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    try:
        config = smtp_email_config()
        configured = True
        config_detail = "complete; secret values and addresses are not displayed"
    except ValueError as exc:
        config = None
        configured = False
        config_detail = str(exc)
    try:
        timer = email_scheduler_status()
        timer_enabled = bool(timer["enabled"])
        timer_verified = bool(timer["runtime_verified"])
    except (OSError, RuntimeError, ValueError):
        timer = {}
        timer_enabled = False
        timer_verified = False

    enabled = route is not None and route.state == "enabled"
    matches = (
        enabled
        and config is not None
        and route is not None
        and route.config_fingerprint == config.fingerprint
    )
    tested = config is not None and _successful_email_test_exists(store, config.fingerprint)
    configuration_label = "[green]complete[/green]" if configured else "[yellow]incomplete[/yellow]"
    console.print(f"SMTP configuration: {configuration_label}")
    console.print(config_detail)
    console.print(
        f"Future incident email: {'[green]enabled[/green]' if enabled else '[yellow]off[/yellow]'}"
    )
    console.print(
        "Automatic email worker: "
        + (
            "[green]enabled[/green]"
            if timer_enabled and timer_verified
            else (
                "[yellow]runtime not verified[/yellow]" if timer_enabled else "[yellow]off[/yellow]"
            )
        )
    )
    console.print(
        "Current configuration receipt test: "
        + ("[green]passed[/green]" if tested else "[yellow]not passed[/yellow]")
    )
    if enabled:
        console.print(
            "Active configuration match: "
            + ("[green]yes[/green]" if matches else "[red]no — delivery fails closed[/red]")
        )
    console.print(
        f"Held historical/local messages: {summary['held']}; "
        f"waiting: {summary['pending']}; needs review: {summary['dead_letter']}."
    )
    console.print("No email was sent by this status command.")


@app.command("email-test")
def email_test(
    confirm_send: Annotated[
        bool,
        typer.Option(
            "--confirm-send/--no-confirm-send",
            help="Required acknowledgement before one real external test email is sent.",
        ),
    ] = False,
) -> None:
    """Send exactly one SMTP receipt test without enabling future alerts."""
    from aios.alerts import (
        AlertSeverity,
        NotificationRequest,
        get_alert_store,
    )
    from aios.notifications import dispatch_email_notifications, smtp_email_config

    if not confirm_send:
        console.print(
            "[yellow]No email sent.[/yellow] Re-run with --confirm-send after reviewing "
            "your local SMTP settings."
        )
        raise typer.Exit(code=1)
    message = None
    try:
        config = smtp_email_config()
        store = get_alert_store()
        message = store.enqueue_notification(
            NotificationRequest(
                idempotency_key=f"test:smtp-email:{uuid4().hex}",
                event_type="test",
                severity=AlertSeverity.INFO,
                title="AIOS external email receipt test",
                body=("This is a deliberate delivery test. It does not enable future alerts."),
                source_job="aios email-test",
                payload={
                    "config_fingerprint": config.fingerprint,
                    "email_test": True,
                    "external_delivery": True,
                },
            ),
            held=False,
        )
        result = dispatch_email_notifications(
            store,
            config,
            notification_id=message.notification_id,
            require_enabled_route=False,
        )
        completed = store.get_notification(message.notification_id)
        if result.succeeded != 1 or completed.state != "delivered":
            if completed.state == "pending":
                completed = store.hold_notification(
                    message.notification_id,
                    reason="email_test_failed",
                )
            console.print(
                f"[red]Email receipt test failed safely:[/red] {completed.notification_id}"
            )
            console.print(
                "The test remains local and will not be retried automatically. "
                "Inspect it with `aios notification-show NOTIFICATION_REF`."
            )
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        if message is not None:
            try:
                unfinished = store.get_notification(message.notification_id)
                if unfinished.state == "pending":
                    store.hold_notification(
                        message.notification_id,
                        reason="email_test_failed",
                    )
            except (OSError, RuntimeError, ValueError):
                pass
        console.print(f"[red]Email receipt test was refused safely:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[bold green]External email receipt accepted:[/bold green] {message.notification_id}"
    )
    console.print(
        "Future incident email is still off. Confirm receipt in your mailbox, then run "
        "`aios email-enable --confirm-enable`."
    )


@app.command("email-enable")
def email_enable(
    confirm_enable: Annotated[
        bool,
        typer.Option(
            "--confirm-enable/--no-confirm-enable",
            help="Enable email only for incident transitions created after this command.",
        ),
    ] = False,
) -> None:
    """Enable future SMTP alerts only after an exact-config receipt test."""
    from aios.alerts import get_alert_store
    from aios.notifications import (
        EMAIL_CHANNEL_NAME,
        EMAIL_ROUTE_ALIAS,
        smtp_email_config,
    )
    from aios.scheduler import set_email_scheduler_active

    if not confirm_enable:
        console.print(
            "[yellow]Email remains off.[/yellow] Enabling future alerts requires --confirm-enable."
        )
        raise typer.Exit(code=1)
    try:
        config = smtp_email_config()
        store = get_alert_store()
        if not _successful_email_test_exists(store, config.fingerprint):
            raise ValueError(
                "no successful receipt test matches the current email configuration; "
                "run `aios email-test --confirm-send` first"
            )
        set_email_scheduler_active(True)
        try:
            route = store.enable_notification_route(
                EMAIL_CHANNEL_NAME,
                config.fingerprint,
                route_alias=EMAIL_ROUTE_ALIAS,
            )
        except Exception:
            set_email_scheduler_active(False)
            raise
        summary = store.notification_summary()
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Email enable was refused safely:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[bold green]Future incident email enabled:[/bold green] {route.enabled_at}")
    console.print(
        f"{summary['held']} existing held message(s) remain held and will not be sent. "
        "Only new incident opens, escalations, reopenings, and eligible recoveries can "
        "enter the email queue."
    )
    console.print("The optional email worker now checks the queue every five minutes.")


@app.command("email-disable")
def email_disable(
    confirm_disable: Annotated[
        bool,
        typer.Option(
            "--confirm-disable/--no-confirm-disable",
            help="Required acknowledgement before future email is stopped.",
        ),
    ] = False,
) -> None:
    """Stop external email and safely hold unsent production messages."""
    from aios.alerts import get_alert_store
    from aios.notifications import EMAIL_CHANNEL_NAME, EMAIL_ROUTE_ALIAS
    from aios.scheduler import set_email_scheduler_active

    if not confirm_disable:
        console.print("[yellow]Email state was not changed.[/yellow] Use --confirm-disable.")
        raise typer.Exit(code=1)
    try:
        store = get_alert_store()
        existing = store.notification_route(
            EMAIL_CHANNEL_NAME,
            route_alias=EMAIL_ROUTE_ALIAS,
        )
        route = (
            store.disable_notification_route(
                EMAIL_CHANNEL_NAME,
                route_alias=EMAIL_ROUTE_ALIAS,
            )
            if existing is not None
            else None
        )
        set_email_scheduler_active(False)
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Email disable was refused safely:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        "[green]Future incident email is off.[/green]"
        + (f" Disabled at {route.disabled_at}." if route is not None else "")
    )
    console.print("Unsent production messages were held locally; nothing was deleted.")


@app.command("email-deliver")
def email_deliver(
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=100, help="Maximum messages this pass."),
    ] = 20,
) -> None:
    """Run one bounded SMTP delivery/retry pass for the enabled route."""
    from aios.alerts import get_alert_store
    from aios.notifications import (
        EMAIL_CHANNEL_NAME,
        EMAIL_ROUTE_ALIAS,
        dispatch_email_notifications,
        smtp_email_config,
    )

    try:
        store = get_alert_store()
        route = store.notification_route(
            EMAIL_CHANNEL_NAME,
            route_alias=EMAIL_ROUTE_ALIAS,
        )
        if route is None or route.state != "enabled":
            console.print("External email is off; no delivery was attempted.")
            return
        config = smtp_email_config()
        result = dispatch_email_notifications(store, config, limit=limit)
        summary = store.notification_summary()
        route_dead_letters = store.notification_route_dead_letter_count(
            EMAIL_CHANNEL_NAME,
            route_alias=EMAIL_ROUTE_ALIAS,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Email delivery failed closed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"Email pass: claimed {result.claimed}, delivered {result.succeeded}, "
        f"failed {result.failed}, newly needs review {result.dead_lettered}."
    )
    if result.failed or summary["pending"] or route_dead_letters:
        console.print(
            f"[yellow]{summary['pending']} queued message(s); "
            f"{route_dead_letters} email message(s) need review.[/yellow]"
        )
        raise typer.Exit(code=1)
    console.print("[green]Email delivery queue is clear.[/green]")


def _successful_email_test_exists(store: Any, config_fingerprint: str) -> bool:
    for message in store.list_notifications(state="delivered", limit=1000):
        if (
            message.event_type != "test"
            or message.payload.get("email_test") is not True
            or message.payload.get("config_fingerprint") != config_fingerprint
        ):
            continue
        if any(
            delivery.channel == "smtp-email"
            and delivery.route_alias == "primary"
            and delivery.state == "succeeded"
            for delivery in store.notification_deliveries(message.notification_id)
        ):
            return True
    return False


@app.command("alert-service-failure", hidden=True)
def alert_service_failure(
    unit: Annotated[str, typer.Option("--unit", help="Managed systemd service name.")],
) -> None:
    """Internal systemd OnFailure handler using only safe service result fields."""
    from aios.alerts import record_systemd_failure

    try:
        incident = record_systemd_failure(unit)
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]System failure could not be recorded:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[red]System failure recorded:[/red] {incident.incident_id} ({unit})")


@app.command("alert-service-recovered", hidden=True)
def alert_service_recovered(
    unit: Annotated[str, typer.Option("--unit", help="Managed systemd service name.")],
) -> None:
    """Internal post-success handler that closes a prior service incident."""
    from aios.alerts import record_systemd_recovery

    try:
        incident = record_systemd_recovery(unit)
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[yellow]System recovery could not be recorded:[/yellow] {exc}")
        raise typer.Exit(code=1) from exc
    if incident is not None and incident.state == "resolved":
        console.print(f"[green]Prior system failure resolved:[/green] {incident.incident_id}")
    else:
        console.print(
            "[yellow]Prior system failure retained:[/yellow] a successful unit "
            "name alone is not sufficient recovery evidence."
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
    normalized_host = host.strip().lower()
    try:
        is_loopback = (
            normalized_host == "localhost" or ip_address(normalized_host.strip("[]")).is_loopback
        )
    except ValueError:
        is_loopback = False
    if not is_loopback:
        console.print(
            "[red]Dashboard start refused:[/red] AIOS has no authentication or HTTPS "
            "boundary, so --host must be localhost or a loopback address."
        )
        raise typer.Exit(code=1)
    if importlib.util.find_spec("streamlit") is None:
        console.print(
            "[red]Dashboard support is not installed.[/red] Run "
            '`.venv/bin/pip install -e ".[dev,dashboard]"`, then retry.'
        )
        raise typer.Exit(code=1)

    script = Path(__file__).resolve().with_name("dashboard.py")
    if not script.is_file():
        console.print("[red]Packaged dashboard entrypoint is missing.[/red]")
        raise typer.Exit(code=1)
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
        typer.Option(
            "--as-of",
            help=(
                "Decision date (YYYY-MM-DD); defaults to the newest reviewed "
                "U.S. decision-date candidate."
            ),
        ),
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
        typer.Option(
            "--json-output",
            help=("Optional write-once .json path under data/reports/readiness."),
        ),
    ] = None,
) -> None:
    """Report whether U.S. evidence is safe for the requested operating use."""
    try:
        if purpose not in {"paper", "historical_research"}:
            raise ValueError("purpose must be 'paper' or 'historical_research'")
        with store_scope(read_only=True) as store:
            if as_of is None:
                from aios.paper import latest_paper_decision_date

                decision_date = latest_paper_decision_date(store).isoformat()
            else:
                date.fromisoformat(as_of)
                decision_date = as_of
            report = assess_us_readiness(
                decision_date,
                purpose=purpose,
                store=store,
            )
        if json_output is not None:
            destination = _resolve_readiness_report_path(json_output)
            _publish_text_write_once(
                destination,
                json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
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
    research_window_label = (
        "Certified historical research window"
        if report.ready
        else "Broad-coverage candidate window"
    )
    console.print(
        f"[cyan]{research_window_label}:[/cyan] "
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
@_exclusive_project_operation("paper-init")
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
        with store_scope(read_only=True) as store:
            document = initialize_paper_account(
                destination,
                store,
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
@_exclusive_project_operation("forward-freeze")
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
    """Verify forward evidence without changing incidents or governed state."""
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


def _run_forward_rollover_preview(
    *,
    as_of: str | None,
    account: Path,
    trial: Path,
    json_output: bool,
    write_plan: bool,
) -> None:
    """Build or persist one read-only prospective rollover plan."""
    from aios.forward_rollover import (
        persist_forward_rollover_plan,
        preview_forward_rollover,
    )
    from aios.operator_preflight import assess_operator_preflight
    from aios.paper import latest_paper_decision_date

    try:
        output_guard = redirect_stdout(sys.stderr) if json_output else nullcontext()
        with output_guard:
            checked = datetime.now(UTC)
            preflight = assess_operator_preflight(now=checked)
            with store_scope(read_only=True) as store:
                decision_date = (
                    date.fromisoformat(as_of)
                    if as_of
                    else latest_paper_decision_date(store)
                )
                readiness = assess_us_readiness(
                    decision_date,
                    purpose="paper",
                    store=store,
                )
                preview = preview_forward_rollover(
                    settings.project_root,
                    _project_path(trial),
                    _project_path(account),
                    store=store,
                    successor_decision_date=decision_date,
                    readiness_evidence=readiness.to_dict(),
                    operator_preflight_evidence=preflight.to_envelope(),
                    now=checked,
                )
            plan_artifact = (
                persist_forward_rollover_plan(settings.project_root, preview)
                if write_plan
                else None
            )
    except (OSError, RuntimeError, TypeError, ValueError, duckdb.Error) as exc:
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "document_kind": "aios.forward-rollover-preview",
                        "read_only": True,
                        "error": str(exc),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            console.print(f"[red]Forward rollover preview refused:[/red] {exc}")
            console.print(
                "[dim]No account, proposal, trial, database, incident, or broker "
                "state was changed.[/dim]"
            )
        raise typer.Exit(code=1) from exc

    if json_output:
        if plan_artifact is None:
            typer.echo(preview.canonical_json())
        else:
            output = preview.to_envelope()
            output["plan_artifact_path"] = plan_artifact.relative_to(
                settings.project_root.resolve()
            ).as_posix()
            typer.echo(
                json.dumps(
                    output,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            )
        if not preview.source_eligible:
            raise typer.Exit(code=1)
        return

    plan = preview.plan
    successor = plan["successor"]
    console.rule("[bold]Prospective forward rollover preview[/bold]")
    console.print(f"Plan SHA-256: {preview.plan_sha256}")
    console.print(
        f"Expired proposal: {plan['predecessor']['proposal_id']}"
        if plan["predecessor"]["proposal_id"] is not None
        else "Expired proposal: unavailable"
    )
    console.print(f"Successor decision close: {successor['decision_date']}")
    console.print(
        f"Successor proposal deadline: {successor['must_be_generated_before'] or 'unavailable'}"
    )
    if preview.source_eligible and preview.activation_available:
        console.print(
            "[bold green]ELIGIBLE FOR EXPLICIT REVIEWED ACTIVATION.[/bold green] "
            "Persist and independently review the write-once plan before activation."
        )
    elif preview.source_eligible:
        console.print(
            "[bold yellow]SOURCE ELIGIBLE; ACTIVATION DISABLED IN THIS BUILD.[/bold yellow] "
            "The write-once plan is available for independent review only."
        )
    else:
        console.print("[bold red]NOT ELIGIBLE.[/bold red]")
        for blocker in preview.observation["blockers"]:
            console.print(f"[red]• {blocker}[/red]")
    console.print(
        "[dim]Read-only proof: no account, proposal, trial, database, incident, "
        "fill, or broker state was changed.[/dim]"
    )
    if plan_artifact is not None:
        console.print(
            "[cyan]Stable write-once review plan:[/cyan] "
            f"{plan_artifact.relative_to(settings.project_root.resolve())}"
        )
    if not preview.source_eligible:
        raise typer.Exit(code=1)


def _forward_rollover_display_path(path: Path) -> str:
    """Display governed output paths relative to the project when possible."""

    absolute = _absolute_without_symlink_resolution(path)
    root = _absolute_without_symlink_resolution(settings.project_root)
    try:
        return absolute.relative_to(root).as_posix()
    except ValueError:
        return absolute.as_posix()


def _valid_lower_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _emit_forward_rollover_contract_error(
    message: str,
    *,
    json_output: bool,
    document_kind: str,
) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "document_kind": document_kind,
                    "status": "refused",
                    "state_change_started": False,
                    "error": message,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        console.print(f"[red]Forward rollover refused:[/red] {message}")
    raise typer.Exit(code=1)


@app.command("forward-rollover")
def forward_rollover(
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help="Later certified decision close; defaults to the latest safe close.",
        ),
    ] = None,
    account: Annotated[
        Path | None,
        typer.Option(
            "--account",
            help="Preview-only local paper-account JSON path.",
        ),
    ] = None,
    trial: Annotated[
        Path | None,
        typer.Option(
            "--trial",
            help="Preview-only active checksum-protected forward-trial path.",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print the canonical result as JSON."),
    ] = False,
    write_plan: Annotated[
        bool,
        typer.Option(
            "--write-plan",
            help=(
                "Publish the stable plan once under "
                "data/reports/forward_rollovers/plans; never writes paper state."
            ),
        ),
    ] = False,
    plan: Annotated[
        Path | None,
        typer.Option(
            "--plan",
            help=(
                "Dormant future activation contract: canonical content-addressed "
                "plan artifact. Current builds refuse activation."
            ),
        ),
    ] = None,
    plan_sha256: Annotated[
        str | None,
        typer.Option(
            "--plan-sha256",
            help=(
                "Dormant future activation contract: exact lowercase SHA-256. "
                "Current builds refuse activation."
            ),
        ),
    ] = None,
    confirm_rollover: Annotated[
        bool,
        typer.Option(
            "--confirm-rollover",
            help=(
                "Dormant future activation confirmation. Current builds refuse "
                "activation."
            ),
        ),
    ] = False,
) -> None:
    """Preview/publish a plan; activation is dormant and disabled in this build."""

    activation_fields = (plan is not None, plan_sha256 is not None, confirm_rollover)
    activation_requested = any(activation_fields)
    if activation_requested and not all(activation_fields):
        _emit_forward_rollover_contract_error(
            "activation requires --plan, --plan-sha256, and --confirm-rollover together",
            json_output=json_output,
            document_kind="aios.forward-rollover-activation",
        )
    if not activation_requested:
        _run_forward_rollover_preview(
            as_of=as_of,
            account=account or Path("data/paper/us_qv_sandbox.json"),
            trial=trial or Path("data/paper/us_qv_forward_trial.json"),
            json_output=json_output,
            write_plan=write_plan,
        )
        return

    assert plan is not None
    assert plan_sha256 is not None
    if as_of is not None or write_plan or account is not None or trial is not None:
        _emit_forward_rollover_contract_error(
            "activation rejects preview-only --as-of, --write-plan, --account, and --trial",
            json_output=json_output,
            document_kind="aios.forward-rollover-activation",
        )
    if not _valid_lower_sha256(plan_sha256):
        _emit_forward_rollover_contract_error(
            "--plan-sha256 must be exactly 64 lowercase hexadecimal characters",
            json_output=json_output,
            document_kind="aios.forward-rollover-activation",
        )

    from aios.forward_rollover import ROLLOVER_ACTIVATION_ENABLED

    if not ROLLOVER_ACTIVATION_ENABLED:
        _emit_forward_rollover_contract_error(
            "activation is disabled in this build pending owner-approved policy "
            "constants and a prospective market window",
            json_output=json_output,
            document_kind="aios.forward-rollover-activation",
        )

    from aios.forward_rollover import execute_forward_rollover_from_plan

    plan_file = _project_path(plan)
    try:
        result = execute_forward_rollover_from_plan(
            settings.project_root,
            plan_file,
            plan_sha256,
            database_path=_project_path(settings.duckdb_path),
            operations_database_path=_project_path(settings.operations_db_path),
            application_version=__version__,
            confirm=True,
        )
    except (OSError, RuntimeError, TypeError, ValueError, duckdb.Error) as exc:
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "document_kind": "aios.forward-rollover-activation",
                        "status": "failed",
                        "state_change_started": "unknown",
                        "plan_sha256": plan_sha256,
                        "recovery_required_if_attempt_exists": True,
                        "error": str(exc),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            console.print(f"[red]Forward rollover activation failed:[/red] {exc}")
            console.print(
                "[yellow]Do not assume that no state changed. If an attempt journal "
                "exists, run forward-rollover-recover with this exact plan and SHA "
                "before retrying.[/yellow]"
            )
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "document_kind": "aios.forward-rollover-activation",
                    "schema_version": "forward-rollover-activation.v1",
                    "status": "verified",
                    "plan_sha256": result.plan_sha256,
                    "attempt_id": result.attempt_id,
                    "predecessor_trial_id": result.predecessor_trial_id,
                    "successor_trial_id": result.successor_trial_id,
                    "active_trial": _forward_rollover_display_path(result.active_trial),
                    "archived_trial": _forward_rollover_display_path(
                        result.archived_trial
                    ),
                    "archived_proposal": _forward_rollover_display_path(
                        result.archived_proposal
                    ),
                    "successor_proposal": _forward_rollover_display_path(
                        result.successor_proposal
                    ),
                    "backup": {
                        "path": _forward_rollover_display_path(result.backup.path),
                        "files": result.backup.files,
                        "bytes": result.backup.bytes,
                        "manifest_sha256": result.backup.manifest_sha256,
                    },
                    "journal_directory": _forward_rollover_display_path(
                        result.journal_directory
                    ),
                    "verified_receipt": _forward_rollover_display_path(
                        result.verified_receipt
                    ),
                    "account_mutated": False,
                    "fill_recorded": False,
                    "broker_order_sent": False,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    else:
        console.rule("[bold green]Verified prospective forward rollover[/bold green]")
        console.print(f"Plan SHA-256: {result.plan_sha256}")
        console.print(f"Attempt: {result.attempt_id}")
        console.print(
            f"Trial: {result.predecessor_trial_id} → {result.successor_trial_id}"
        )
        console.print(f"Active trial: {_forward_rollover_display_path(result.active_trial)}")
        console.print(f"Verified receipt: {result.verified_receipt}")
        console.print(
            "[dim]The predecessor was archived unchanged. No account mutation, fill, "
            "or broker order was performed.[/dim]"
        )


@app.command("forward-rollover-recover")
def forward_rollover_recover(
    plan: Annotated[
        Path,
        typer.Option(
            "--plan",
            help="Canonical content-addressed plan artifact used by the attempt.",
        ),
    ],
    plan_sha256: Annotated[
        str,
        typer.Option(
            "--plan-sha256",
            help="Exact lowercase SHA-256 reviewed by the operator.",
        ),
    ],
    confirm_recovery: Annotated[
        bool,
        typer.Option(
            "--confirm-recovery",
            help="Explicitly approve deterministic reconciliation of the attempt.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print the canonical recovery result as JSON."),
    ] = False,
) -> None:
    """Reconcile interrupted attempts without constructing a new successor."""

    if not confirm_recovery:
        _emit_forward_rollover_contract_error(
            "recovery requires --confirm-recovery",
            json_output=json_output,
            document_kind="aios.forward-rollover-recovery",
        )
    if not _valid_lower_sha256(plan_sha256):
        _emit_forward_rollover_contract_error(
            "--plan-sha256 must be exactly 64 lowercase hexadecimal characters",
            json_output=json_output,
            document_kind="aios.forward-rollover-recovery",
        )

    from aios.forward_rollover import recover_forward_rollover_from_plan

    try:
        recovered = recover_forward_rollover_from_plan(
            settings.project_root,
            _project_path(plan),
            plan_sha256,
            confirm=True,
        )
    except (OSError, RuntimeError, TypeError, ValueError, duckdb.Error) as exc:
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "document_kind": "aios.forward-rollover-recovery",
                        "status": "failed",
                        "plan_sha256": plan_sha256,
                        "manual_investigation_required": True,
                        "error": str(exc),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            console.print(f"[red]Forward rollover recovery failed:[/red] {exc}")
            console.print(
                "[yellow]Do not retry activation. Preserve the plan and attempt "
                "journal for investigation.[/yellow]"
            )
        raise typer.Exit(code=1) from exc

    output = [
        {
            "attempt_id": item.attempt_id,
            "terminal_phase": item.terminal_phase,
            "active_trial": _forward_rollover_display_path(item.active_trial),
            "journal_directory": _forward_rollover_display_path(
                item.journal_directory
            ),
            "terminal_receipt": _forward_rollover_display_path(
                item.terminal_receipt
            ),
        }
        for item in recovered
    ]
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "document_kind": "aios.forward-rollover-recovery",
                    "schema_version": "forward-rollover-recovery.v1",
                    "status": "verified",
                    "plan_sha256": plan_sha256,
                    "attempts": output,
                    "new_successor_constructed": False,
                    "fill_recorded": False,
                    "broker_order_sent": False,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        return

    console.rule("[bold green]Forward rollover recovery verified[/bold green]")
    console.print(f"Plan SHA-256: {plan_sha256}")
    for item in output:
        console.print(f"{item['attempt_id']}: {item['terminal_phase']}")
    console.print(
        "[dim]Recovery constructed no new successor, fill, or broker order.[/dim]"
    )


@app.command("forward-rollover-preview", hidden=True)
def forward_rollover_preview(
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help="Later certified decision close; defaults to the latest safe close.",
        ),
    ] = None,
    account: Annotated[
        Path,
        typer.Option("--account", help="Local paper-account JSON path."),
    ] = Path("data/paper/us_qv_sandbox.json"),
    trial: Annotated[
        Path,
        typer.Option("--trial", help="Active checksum-protected forward-trial path."),
    ] = Path("data/paper/us_qv_forward_trial.json"),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print the canonical read-only plan as JSON."),
    ] = False,
    write_plan: Annotated[
        bool,
        typer.Option(
            "--write-plan",
            help=(
                "Publish the stable plan once under "
                "data/reports/forward_rollovers/plans; never writes paper state."
            ),
        ),
    ] = False,
) -> None:
    """Deprecated read-only alias for ``aios forward-rollover``."""

    _run_forward_rollover_preview(
        as_of=as_of,
        account=account,
        trial=trial,
        json_output=json_output,
        write_plan=write_plan,
    )


@app.command("forward-restart")
@_exclusive_project_operation("forward-restart")
def forward_restart(
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help="Reviewed decision close (YYYY-MM-DD); defaults to the latest safe close.",
        ),
    ] = None,
    account: Annotated[
        Path,
        typer.Option("--account", help="Local paper-account JSON path."),
    ] = Path("data/paper/us_qv_sandbox.json"),
    trial: Annotated[
        Path,
        typer.Option("--trial", help="Active checksum-protected forward-trial path."),
    ] = Path("data/paper/us_qv_forward_trial.json"),
    proposal_output: Annotated[
        Path | None,
        typer.Option("--proposal-output", help="New dated baseline proposal path."),
    ] = None,
    top_n: Annotated[
        int,
        typer.Option("--top-n", min=10, max=20, help="Number of simulated holdings."),
    ] = 10,
    confirm_restart: Annotated[
        bool,
        typer.Option(
            "--confirm-restart/--no-confirm-restart",
            help="Archive the drifted trial and activate a prospective replacement.",
        ),
    ] = False,
) -> None:
    """Create a new paper baseline and atomically replace a drifted trial."""
    from aios.forward import assess_forward_trial, replace_drifted_forward_trial
    from aios.paper import (
        create_paper_proposal,
        default_proposal_path,
        latest_paper_decision_date,
    )

    if not confirm_restart:
        console.print(
            "[red]Forward restart refused:[/red] explicit --confirm-restart approval is required"
        )
        raise typer.Exit(code=1)

    account_path = _project_path(account)
    trial_path = _project_path(trial)
    created_proposal: Path | None = None
    try:
        with store_scope(read_only=True) as store:
            status = assess_forward_trial(
                settings.project_root,
                trial_path,
                account_path,
            )
            if status.ready:
                raise ValueError("active forward trial is unchanged; continue the existing trial")
            decision_date = (
                date.fromisoformat(as_of) if as_of else latest_paper_decision_date(store)
            )
            destination = (
                _resolve_paper_proposal_output_path(proposal_output)
                if proposal_output is not None
                else _resolve_paper_proposal_output_path(
                    default_proposal_path(settings.project_root, decision_date)
                )
            )
            if destination.exists():
                raise ValueError(f"replacement proposal already exists: {destination}")
            proposal = create_paper_proposal(
                account_path,
                destination,
                decision_date,
                store,
                top_n=top_n,
            )
            created_proposal = proposal.path
            replacement = replace_drifted_forward_trial(
                settings.project_root,
                trial_path,
                account_path,
                proposal.path,
                confirm=True,
            )
    except Exception as exc:
        if created_proposal is not None:
            with suppress(OSError):
                created_proposal.unlink()
        console.print(f"[red]Forward restart refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    payload = replacement.active.payload
    console.print("[bold green]New prospective U.S. forward trial is active.[/bold green]")
    console.print(f"Trial ID: {payload['trial_id']}")
    console.print(f"Decision close: {payload['observation_start_decision_date']}")
    console.print(f"Baseline proposal: {created_proposal}")
    console.print(f"Archived predecessor: {replacement.archived_previous}")
    console.print(
        "No trade was executed. The proposal remains simulation-only until its reviewed "
        "next-session close and explicit confirmation."
    )


@app.command("paper-propose")
@_exclusive_project_operation("paper-propose")
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
        require_registered_forward_proposal,
    )
    from aios.paper import (
        ACCOUNT_DOCUMENT_KIND,
        PROPOSAL_DOCUMENT_KIND,
        create_paper_proposal,
        default_proposal_path,
        latest_paper_decision_date,
        read_paper_document,
    )

    try:
        with store_scope(read_only=True) as store:
            decision_date = (
                date.fromisoformat(as_of) if as_of else latest_paper_decision_date(store)
            )
            account_path = _project_path(account)
            destination = (
                _resolve_paper_proposal_output_path(output)
                if output is not None
                else _resolve_paper_proposal_output_path(
                    default_proposal_path(settings.project_root, decision_date)
                )
            )
            if replace and destination.exists():
                existing = read_paper_document(
                    destination,
                    expected_kind=PROPOSAL_DOCUMENT_KIND,
                )
                current_account = read_paper_document(
                    account_path,
                    expected_kind=ACCOUNT_DOCUMENT_KIND,
                )
                if existing.payload.get("account_id") != current_account.payload.get("account_id"):
                    raise ValueError("existing proposal belongs to a different paper account")
                if existing.payload.get("decision_date") != decision_date.isoformat():
                    raise ValueError("existing proposal belongs to a different decision date")
            trial_path = settings.project_root / DEFAULT_FORWARD_RELATIVE_PATH
            if trial_path.exists():
                status = assess_forward_trial(
                    settings.project_root,
                    trial_path,
                    account_path,
                )
                if not status.ready:
                    raise ValueError("active forward trial drifted: " + "; ".join(status.issues))
                trial = read_forward_trial(trial_path)
                if top_n != int(trial.payload["frozen_configuration"]["top_n"]):
                    raise ValueError("top_n differs from the active forward trial")
                current_account = read_paper_document(
                    account_path,
                    expected_kind=ACCOUNT_DOCUMENT_KIND,
                )
                if _has_unresolved_registered_proposal(
                    trial.payload,
                    current_account.payload,
                ):
                    raise ValueError(
                        "a registered forward proposal remains unresolved; do not "
                        "create or replace another proposal until the governed "
                        "proposal lifecycle closes it"
                    )
                if replace and destination.exists():
                    raise ValueError("registered forward proposals cannot be replaced")
            document = create_paper_proposal(
                account_path,
                destination,
                decision_date,
                store,
                top_n=top_n,
                replace=replace,
            )
            if trial_path.exists():
                try:
                    register_forward_proposal(
                        settings.project_root,
                        trial_path,
                        account_path,
                        document.path,
                    )
                except Exception as registration_error:
                    try:
                        current = read_paper_document(
                            document.path,
                            expected_kind=PROPOSAL_DOCUMENT_KIND,
                        )
                        if current.payload_sha256 != document.payload_sha256:
                            raise RuntimeError("new proposal changed before registration rollback")
                        document.path.unlink()
                    except Exception as cleanup_error:
                        raise RuntimeError(
                            "proposal registration failed and exact rollback could not "
                            "be verified; quarantine the unregistered proposal before "
                            "continuing the trial"
                        ) from cleanup_error
                    raise registration_error
                require_registered_forward_proposal(
                    settings.project_root,
                    trial_path,
                    account_path,
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


@app.command("stress-review")
def stress_review(
    proposal: Annotated[
        Path,
        typer.Option(
            "--proposal",
            help="Registered checksum-protected paper proposal to stress.",
        ),
    ],
    account: Annotated[
        Path,
        typer.Option("--account", help="Local paper-account JSON path."),
    ] = Path("data/paper/us_qv_sandbox.json"),
    trial: Annotated[
        Path,
        typer.Option("--trial", help="Active checksum-protected forward-trial JSON path."),
    ] = Path("data/paper/us_qv_forward_trial.json"),
    scenario: Annotated[
        list[str] | None,
        typer.Option(
            "--scenario",
            help="Run only this immutable scenario ID; repeat to select several.",
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Optional write-once JSON report path. No report is stored by default.",
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print the canonical report envelope as JSON."),
    ] = False,
) -> None:
    """Review proposal loss, concentration, and liquidity sensitivities without mutation."""
    from aios.risk.stress import review_registered_paper_proposal_stress

    account_path = _project_path(account)
    proposal_path = _project_path(proposal)
    trial_path = _project_path(trial)
    try:
        governed_review = review_registered_paper_proposal_stress(
            settings.project_root,
            trial_path,
            account_path,
            proposal_path,
            scenario_ids=scenario,
            output_path=_project_path(output) if output is not None else None,
        )
    except Exception as exc:
        if as_json:
            typer.echo(
                json.dumps(
                    {"error": "stress_review_refused", "detail": str(exc)},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                err=True,
            )
        else:
            console.print(f"[red]Stress review refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    report = governed_review.report
    artifact_path = governed_review.artifact_path
    payload = report.payload
    analysis = payload["analysis"]
    if as_json:
        typer.echo(
            json.dumps(
                report.envelope(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    else:
        console.rule("[bold]Proposal stress review — advisory only[/bold]")
        console.print(
            f"Proposal: [bold]{payload['source']['proposal_id']}[/bold] · "
            "source is proposal targets, not holdings, fills, or broker positions."
        )
        finding_count = sum(
            len(result["reference_limit_findings"])
            for result in analysis["scenarios"]
            if result["status"] == "calculated"
        )
        summary = analysis["summary"]
        calculation_coverage = str(analysis["calculation_coverage"]).replace("_", " ")
        selected_policy_count = summary["selected_numerical_policy_count"]
        selected_policy_label = "policy" if selected_policy_count == 1 else "policies"
        console.print(
            "Report generation: "
            f"[bold]{payload['report_generation_status']}[/bold] · "
            f"calculation coverage: [bold]{calculation_coverage}[/bold] · "
            f"input evidence: [bold]{analysis['input_evidence']}[/bold]"
        )
        console.print(
            "Numerical results: "
            f"[bold]{summary['calculated_numerical_result_count']} calculated[/bold] / "
            f"{summary['generated_numerical_result_count']} generated from "
            f"{selected_policy_count} selected {selected_policy_label} · "
            f"safeguard demonstrations: "
            f"{summary['selected_safeguard_demonstration_count']} · "
            f"advisory reference findings: [bold]{finding_count}[/bold]"
        )

        def render_results(title: str, results: list[dict[str, Any]]) -> None:
            if not results:
                return
            table = Table(title=title)
            table.add_column("Scenario ID", max_width=30, overflow="fold")
            table.add_column("State", max_width=8, no_wrap=True)
            table.add_column("Loss / equity", justify="right", no_wrap=True)
            table.add_column("Refs", justify="right", no_wrap=True)
            for result in results:
                loss = result.get("portfolio_loss")
                table.add_row(
                    str(result["scenario_id"]),
                    "ok" if result["status"] == "calculated" else "withheld",
                    f"${loss:,.0f} / {result['portfolio_loss_pct']:.1%}"
                    if loss is not None
                    else "withheld",
                    str(len(result["reference_limit_findings"]))
                    if result["status"] == "calculated"
                    else "n/a",
                )
            console.print(table)

        fixed_results = [
            result
            for result in analysis["scenarios"]
            if result["result_kind"] == "deterministic_mark_shock"
        ]
        proxy_results = [
            result
            for result in analysis["scenarios"]
            if result["result_kind"] == "statistical_loss_proxy"
        ]
        render_results("Deterministic mark-shock results", fixed_results)
        render_results(
            "Statistical loss proxies — separate from mark shocks",
            proxy_results,
        )

        calculated_results = [
            result for result in analysis["scenarios"] if result["status"] == "calculated"
        ]
        if calculated_results:
            console.print("[bold]Assumptions and advisory findings[/bold]")
        for result in analysis["scenarios"]:
            console.print(f"• [bold]{result['scenario_id']}[/bold]")
            if result["status"] != "calculated":
                console.print(
                    "  Numerical output withheld. Missing required evidence: "
                    + ", ".join(result["blockers"])
                )
                continue
            assumptions = result["assumptions"]
            if result["result_kind"] == "statistical_loss_proxy":
                console.print(
                    "  Loss proxy: "
                    f"{assumptions['standard_deviation_multiple']:g}σ over "
                    f"{assumptions['horizon_sessions']} sessions; verified volatility ×"
                    f"{assumptions['volatility_multiplier']:g}; constant cross-position "
                    f"correlation {assumptions['constant_correlation_assumption']:.2f}."
                )
                console.print(
                    "  Post-shock holdings, drawdown, concentration, and liquidity: "
                    "not applicable; Euler contributions are risk allocations, not returns."
                )
            else:
                selector = assumptions["selector"]
                if selector["kind"] == "all":
                    scope = "all proposal targets"
                elif selector["kind"] == "top_weighted":
                    scope = f"top {selector['count']} weighted proposal targets"
                else:
                    scope = f"{result.get('selected_sector')} sector targets"
                console.print(
                    f"  Mark transform: {scope} {assumptions['selected_return']:.1%}; "
                    f"other targets {assumptions['other_return']:.1%}; stressed ADV ×"
                    f"{assumptions['liquidity_adv_multiplier']:.2f}; generic exit horizon "
                    f"{assumptions['liquidation_horizon_sessions']} sessions."
                )
            findings = result["reference_limit_findings"]
            if findings:
                for finding in findings:
                    console.print(f"  Advisory: {finding['message']}")
            else:
                console.print("  Advisory reference findings: none.")

        safeguards = analysis["fail_closed_safeguards"]
        if safeguards:
            safeguard_table = Table(title="Fail-closed policy demonstrations")
            safeguard_table.add_column("Safeguard", max_width=34, overflow="fold")
            safeguard_table.add_column("Status", no_wrap=True)
            safeguard_table.add_column("Current evidence", no_wrap=True)
            for safeguard in safeguards:
                safeguard_table.add_row(
                    str(safeguard["label"]),
                    str(safeguard["status"]).replace("_", " "),
                    "gap present" if safeguard["live_evidence_gap"] else "present",
                )
            console.print(safeguard_table)
            console.print(
                "These rows document withholding behavior; no evidence outage was injected "
                "and no calculation was rerun."
            )
        if summary["largest_fixed_shock_scenario_id"] is not None:
            console.print(
                "Largest modeled loss among selected fixed mark shocks: "
                f"[bold]{summary['largest_fixed_shock_scenario_id']}[/bold] · "
                f"${summary['largest_fixed_shock_loss']:,.2f} "
                f"({summary['largest_fixed_shock_loss_pct']:.1%} of current equity)."
            )
        if summary["largest_statistical_proxy_scenario_id"] is not None:
            console.print(
                "Largest statistical proxy (not comparable to a historical mark shock): "
                f"[bold]{summary['largest_statistical_proxy_scenario_id']}[/bold] · "
                f"${summary['largest_statistical_proxy_loss']:,.2f} "
                f"({summary['largest_statistical_proxy_loss_pct']:.1%} of current equity)."
            )
        if payload["evidence"]["blockers"]:
            console.print("[red]Input-evidence gaps:[/red]")
            for blocker in payload["evidence"]["blockers"][:10]:
                console.print(f"  • {blocker}")
            remaining = len(payload["evidence"]["blockers"]) - 10
            if remaining > 0:
                console.print(f"  • ... and {remaining} more")
        if artifact_path is not None:
            console.print(f"Write-once report: {artifact_path}")
        console.print(
            "[yellow]Read-only reproducible stress evidence — historical labels calibrate "
            "magnitude only; not a forecast, constituent replay, approval, or trade "
            "instruction. The account, proposal, forward trial, incident ledger, and "
            "database were not changed, and no broker order was sent.[/yellow]"
        )
    if payload["report_generation_status"] != "complete":
        raise typer.Exit(code=1)


@app.command("paper-review")
def paper_review(
    proposal: Annotated[
        Path,
        typer.Option(
            "--proposal",
            help="Proposal JSON to preflight without recording or changing the account.",
        ),
    ],
    account: Annotated[
        Path,
        typer.Option("--account", help="Local paper-account JSON path."),
    ] = Path("data/paper/us_qv_sandbox.json"),
) -> None:
    """Project a fill and check every gate without changing the account."""
    from aios.forward import (
        DEFAULT_FORWARD_RELATIVE_PATH,
        require_registered_forward_proposal,
    )
    from aios.paper import review_paper_proposal_execution

    try:
        trial_path = settings.project_root / DEFAULT_FORWARD_RELATIVE_PATH
        require_registered_forward_proposal(
            settings.project_root,
            trial_path,
            _project_path(account),
            _project_path(proposal),
        )
        with store_scope(read_only=True) as store:
            result = review_paper_proposal_execution(
                _project_path(account),
                _project_path(proposal),
                store,
            )
    except Exception as exc:
        console.print(f"[red]Paper review refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    labels = {
        "waiting_for_scheduled_close": "waiting for the scheduled U.S. close",
        "waiting_for_execution_data": "waiting for reviewed closing-price data",
        "ready_for_confirmed_simulation": "ready for explicit local simulation",
        "expired": "expired — retrospective fills are blocked",
    }
    status = str(result["status"])
    color = "green" if result["ready"] else ("red" if status == "expired" else "yellow")
    console.rule("[bold]Paper proposal execution review[/bold]")
    console.print(f"Decision close: {result['decision_date']}")
    console.print(f"Scheduled simulated close: {result['execution_date']}")
    console.print(f"State: [{color}]{labels.get(status, status)}[/{color}]")
    console.print(str(result["detail"]))
    console.print(f"Allowed window (UTC): {result['executable_after']} to {result['expires_at']}.")
    if result.get("missing_count"):
        console.print(f"Missing reviewed execution evidence: {result['missing_count']} item(s).")
        for item in result["missing"][:6]:
            console.print(f"  • {item}")
        if result["missing_count"] > 6:
            console.print(f"  • ... and {result['missing_count'] - 6} more")
    if result["ready"]:
        console.print(
            f"Projected local trades: {result['projected_trade_count']}; "
            f"modeled costs: ${result['projected_transaction_costs']:,.2f}."
        )
        execute_command = shlex.join(
            [
                "aios",
                "paper-execute",
                "--proposal",
                str(proposal),
                "--account",
                str(account),
                "--confirm-simulated",
            ]
        )
        console.print("If you accept this local simulation before the window expires, run:")
        console.print(f"[bold]{execute_command}[/bold]")
    console.print(
        "[yellow]Read-only simulation preflight — the account was not changed and no "
        "order was sent to a broker.[/yellow]"
    )
    if status == "expired":
        raise typer.Exit(code=1)


@app.command("paper-execute")
@_exclusive_project_operation("paper-execute")
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
        require_registered_forward_proposal(
            settings.project_root,
            trial_path,
            _project_path(account),
            _project_path(proposal),
        )
        with store_scope(read_only=True) as store:
            result = execute_paper_proposal(
                _project_path(account),
                _project_path(proposal),
                store,
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
@_exclusive_project_operation("paper-mark")
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
    from aios.paper import latest_reviewed_market_close, mark_paper_account

    try:
        with store_scope(read_only=True) as store:
            mark_date = (
                date.fromisoformat(through) if through else latest_reviewed_market_close(store)
            )
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
        with store_scope(read_only=True) as store:
            summary = paper_account_summary(_project_path(account), store)
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
@_exclusive_project_operation("ingest-macro")
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
@_exclusive_project_operation("refresh-us-current")
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
        typer.Option(
            "--json-output",
            help="Optional write-once .json run summary outside governed state.",
        ),
    ] = None,
) -> None:
    """Refresh the current reviewed U.S. universe without approving new members."""
    from aios.refresh import refresh_us_current

    enabled_areas = [
        name
        for name, enabled in (
            ("prices", prices),
            ("fundamentals", fundamentals),
            ("macro", macro),
        )
        if enabled
    ]
    refresh_key = "-".join(enabled_areas) or "none"
    failure_fingerprint = f"refresh:{refresh_key}:failure"
    partial_fingerprint = f"refresh:{refresh_key}:partial"
    try:
        summary_destination = (
            _resolve_generated_output_path(
                json_output,
                label="current refresh summary",
                suffix=".json",
            )
            if json_output is not None
            else None
        )
    except ValueError as exc:
        console.print(f"[red]Current refresh output refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc

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
    except duckdb.IOException as exc:
        from aios.alerts import Alert, AlertSeverity

        _emit_operational_alert(
            Alert(
                code="current_refresh_failed",
                severity=AlertSeverity.CRITICAL,
                title="Current U.S. refresh failed",
                body="The refresh could not acquire the analytical database safely.",
                dedup_key=failure_fingerprint,
                source_job="aios refresh-us-current",
                payload={"areas": enabled_areas, "error_type": type(exc).__name__},
            )
        )
        console.print(
            "[red]Current refresh could not lock DuckDB.[/red] Close the dashboard "
            "and every other AIOS command, then retry."
        )
        raise typer.Exit(code=1) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        from aios.alerts import Alert, AlertSeverity

        _emit_operational_alert(
            Alert(
                code="current_refresh_failed",
                severity=AlertSeverity.CRITICAL,
                title="Current U.S. refresh failed",
                body="The refresh stopped before it produced a safe completed result.",
                dedup_key=failure_fingerprint,
                source_job="aios refresh-us-current",
                payload={"areas": enabled_areas, "error_type": type(exc).__name__},
            )
        )
        console.print(f"[red]Current U.S. refresh refused safely:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    summary_error: Exception | None = None
    if summary_destination is not None:
        try:
            publish_text_write_once(
                summary_destination,
                json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
            )
        except (OSError, ValueError) as exc:
            summary_error = exc

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
    if summary_destination is not None and summary_error is None:
        console.print(f"Run summary written to {summary_destination}.")
    for warning in result.warnings[:25]:
        console.print(f"  [yellow]{warning.kind} {warning.identity}:[/yellow] {warning.error}")
    if len(result.warnings) > 25:
        console.print(
            f"  [yellow]... {len(result.warnings) - 25} additional warnings are in "
            "the JSON summary and ingest audit.[/yellow]"
        )
    if result.failures:
        from aios.alerts import Alert, AlertSeverity

        _emit_operational_alert(
            Alert(
                code="current_refresh_failed",
                severity=AlertSeverity.CRITICAL,
                title="Current U.S. refresh completed with hard failures",
                body="One or more reviewed refresh items failed and require inspection.",
                dedup_key=failure_fingerprint,
                source_job="aios refresh-us-current",
                payload={
                    "areas": enabled_areas,
                    "failure_count": len(result.failures),
                    "identities": [failure.identity for failure in result.failures[:25]],
                },
            )
        )
        for failure in result.failures[:25]:
            console.print(f"  [red]{failure.kind} {failure.identity}:[/red] {failure.error}")
        if len(result.failures) > 25:
            console.print(
                f"  [red]... {len(result.failures) - 25} additional failures are in "
                "the JSON summary and ingest audit.[/red]"
            )
        raise typer.Exit(code=1)
    if result.warnings:
        from aios.alerts import Alert, AlertSeverity

        _emit_operational_alert(
            Alert(
                code="current_refresh_partial",
                severity=AlertSeverity.WARNING,
                title="Current U.S. refresh completed with warnings",
                body="The main refresh completed, but some reviewed items need inspection.",
                dedup_key=partial_fingerprint,
                source_job="aios refresh-us-current",
                payload={
                    "areas": enabled_areas,
                    "warning_count": len(result.warnings),
                    "identities": [warning.identity for warning in result.warnings[:25]],
                },
            )
        )
    console.print(
        "[bold green]Refresh completed.[/bold green] Run `aios health` before creating "
        "or recording a paper proposal."
    )
    if summary_error is not None:
        console.print(
            "[red]The refresh completed, but its optional summary was not published:[/red] "
            f"{summary_error}"
        )
        raise typer.Exit(code=1)


@app.command("refresh-us-daily")
@_exclusive_project_operation("refresh-us-daily")
def refresh_us_daily_command(
    force: Annotated[
        bool,
        typer.Option(
            "--force/--skip-if-current",
            help="Re-run providers even when this completed session already passed.",
        ),
    ] = False,
    json_output: Annotated[
        Path | None,
        typer.Option(
            "--json-output",
            help="Optional write-once .json run summary outside governed state.",
        ),
    ] = None,
) -> None:
    """Run the recoverable benchmark-first U.S. daily workflow."""
    from aios.alerts import Alert, AlertSeverity
    from aios.daily import run_us_daily_cycle

    try:
        summary_destination = (
            _resolve_generated_output_path(
                json_output,
                label="daily refresh summary",
                suffix=".json",
            )
            if json_output is not None
            else None
        )
    except ValueError as exc:
        console.print(f"[red]Daily refresh output refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    def show_progress(stage: str, detail: str) -> None:
        console.print(f"  [cyan]{stage.replace('_', ' ')}:[/cyan] {detail}")

    try:
        result = run_us_daily_cycle(force=force, progress=show_progress)
    except Exception as exc:
        _emit_operational_alert(
            Alert(
                code="daily_us_cycle_failed",
                severity=AlertSeverity.CRITICAL,
                title="Daily U.S. update did not complete",
                body=(
                    "The exact completed U.S. session was not certified. Older reviewed "
                    "research remains available."
                ),
                dedup_key="daily:us-cycle:failure",
                source_job="aios refresh-us-daily",
                payload={"error_type": type(exc).__name__},
            )
        )
        console.print(f"[red]Daily U.S. update stopped safely:[/red] {exc}")
        console.print(
            "No newer paper decision is approved. The next startup or scheduled run "
            "will retry the same idempotent workflow."
        )
        raise typer.Exit(code=1) from exc

    _record_daily_cycle_recovery(result.run_id)

    summary_error: Exception | None = None
    if summary_destination is not None:
        try:
            publish_text_write_once(
                summary_destination,
                json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
            )
        except (OSError, ValueError) as exc:
            summary_error = exc

    if result.interrupted_run_ids:
        incident = Alert(
            code="daily_us_cycle_interrupted",
            severity=AlertSeverity.WARNING,
            title="An earlier daily U.S. update was interrupted",
            body="A later run recovered and certified the target session.",
            dedup_key="daily:us-cycle:interrupted",
            source_job="aios refresh-us-daily",
            payload={"recovered_run_ids": list(result.interrupted_run_ids)},
        )
        _emit_operational_alert(incident)

    table = Table(title=f"Daily U.S. update — {result.target_session}")
    table.add_column("stage")
    table.add_column("verified result")
    table.add_row("Completed U.S. session", result.target_session)
    table.add_row("SPY bootstrap", f"{result.benchmark_rows} reviewed row(s)")
    table.add_row(
        "Dated S&P 500 universe",
        f"{result.universe_status} through {result.universe_coverage_through}",
    )
    table.add_row("Reviewed members", str(result.member_count))
    table.add_row("Member price rows", str(result.member_price_rows))
    table.add_row("Macro rows", str(result.macro_rows))
    table.add_row(
        "Certified research through",
        result.certified_research_through or "not certified",
    )
    console.print(table)
    if result.status == "already_current":
        console.print(
            "[green]Already current.[/green] No provider download was needed for this "
            "completed U.S. session."
        )
    else:
        console.print(
            "[bold green]Daily update completed safely.[/bold green] The benchmark, "
            "universe, member data, and exact-date readiness now agree."
        )
    if summary_destination is not None and summary_error is None:
        console.print(f"Run summary written to {summary_destination}.")
    if summary_error is not None:
        console.print(
            "[red]The daily update completed, but its optional summary was not "
            f"published:[/red] {summary_error}"
        )
        raise typer.Exit(code=1)


@app.command("import-universe")
@_exclusive_project_operation("import-universe")
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
        destination = _resolve_generated_output_path(
            output,
            label="universe membership artifact",
            suffix=".csv",
        )
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
        write_membership_csv(destination, rows)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Universe build refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]Universe build done:[/green] {len(rows)} intervals written to {destination}."
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
        destination = _resolve_generated_output_path(
            output,
            label="security identity artifact",
            suffix=".csv",
        )
        rows = build_security_identity_csv(
            membership,
            transitions,
            destination,
            universe_id=universe_id,
        )
    except (OSError, ValueError) as exc:
        console.print(f"[red]Security identity build refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    verified = sum(row["identity_status"] != "bounded_ticker" for row in rows)
    console.print(
        f"[green]Security identity build done:[/green] {len(rows)} interval "
        f"assignments written to {destination}."
    )
    console.print(
        f"Verified transition intervals: {verified}; "
        f"bounded ticker intervals: {len(rows) - verified}."
    )


@app.command("import-security-identities")
@_exclusive_project_operation("import-security-identities")
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
@_exclusive_project_operation("import-reference-identities")
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
@_exclusive_project_operation("import-security-conversions")
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
@_exclusive_project_operation("ingest-liquidation-prices")
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
        artifact_directory = _resolve_generated_output_directory(
            output_dir,
            label="reference batch directory",
        )
        tickers = load_batch_tickers(tickers_file)
        with store_scope(read_only=True) as store:
            result = build_stable_reference_batch(
                tickers,
                universe_id=universe_id,
                start=start,
                end=end,
                provider=provider,
                verified_date=verified_date,
                store=store,
            )
        paths = write_reference_batch(
            result,
            output_dir=artifact_directory,
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
        artifact_directory = _resolve_generated_output_directory(
            output_dir,
            label="reference window batch directory",
        )
        windows = load_batch_windows(windows_file)
        with store_scope(read_only=True) as store:
            result = build_stable_reference_window_batch(
                windows,
                universe_id=universe_id,
                provider=provider,
                verified_date=verified_date,
                store=store,
            )
        paths = write_reference_batch(
            result,
            output_dir=artifact_directory,
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
        artifact_directory = _resolve_generated_output_directory(
            output_dir,
            label="reference window plan directory",
        )
        with store_scope(read_only=True) as store:
            windows = plan_missing_reference_windows(
                universe_id=universe_id,
                as_of=as_of,
                start_floor=start_floor,
                end=end,
                provider=provider,
                store=store,
            )
        paths = write_reference_window_batches(
            windows,
            output_dir=artifact_directory,
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
        artifact_directory = _resolve_generated_output_directory(
            output_dir,
            label="historical reference plan directory",
        )
        with store_scope(read_only=True) as store:
            windows = plan_historical_reference_gaps(
                universe_id=universe_id,
                start=start,
                end=end,
                store=store,
            )
        paths = (
            write_reference_window_batches(
                windows,
                output_dir=artifact_directory,
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
        artifact_directory = _resolve_generated_output_directory(
            output_dir,
            label="merged reference batch directory",
        )
        result = merge_reference_batch_files(batches)
        paths = write_reference_batch(
            result,
            output_dir=artifact_directory,
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
@_exclusive_project_operation("ingest-reference-batch")
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
            help=(
                "Reserved and currently refused until bulk archive members have "
                "governed immutable-source lineage."
            ),
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
        missing_destination = (
            _resolve_generated_output_path(
                missing_output,
                label="coverage review list",
                suffix=".txt",
            )
            if missing_output is not None
            else None
        )
        date.fromisoformat(decision_date)
        with store_scope(read_only=True) as store:
            rows = store.universe_data_coverage(universe_id, decision_date)
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
    if missing_destination is not None:
        try:
            publish_text_write_once(
                missing_destination,
                "\n".join(row["ticker"] for row in missing) + ("\n" if missing else ""),
            )
        except (OSError, ValueError) as exc:
            console.print(f"[red]Coverage output refused:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        console.print(
            f"Missing-member review list written to {missing_destination}. "
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
    with store_scope(read_only=True) as store:
        snapshot = compute_regime(decision_date, store=store)
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
        audit_destination = (
            _resolve_generated_output_path(
                output,
                label="backtest audit artifact",
                suffix=".json",
            )
            if output is not None
            else None
        )
        with store_scope(read_only=True) as store:
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
                store=store,
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
    if audit_destination is not None:
        try:
            audit_path = _write_backtest_audit(
                audit_destination,
                result,
                explanations,
            )
        except (OSError, ValueError) as exc:
            console.print(f"[red]Backtest output refused:[/red] {exc}")
            raise typer.Exit(code=1) from exc
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
    path = Path(output)
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
    return publish_text_write_once(
        path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


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
@_exclusive_project_operation("ingest-ticker")
def ingest_ticker(
    ticker: str = typer.Argument(..., help="Ticker, e.g. AAPL"),
    with_prices: bool = typer.Option(True, "--prices/--no-prices"),
    with_fundamentals: bool = typer.Option(True, "--fundamentals/--no-fundamentals"),
) -> None:
    """Pull EDGAR fundamentals + yfinance prices for one ticker."""
    _ingest_one(ticker, with_prices=with_prices, with_fundamentals=with_fundamentals)


@app.command("ingest-issuer")
@_exclusive_project_operation("ingest-issuer")
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
@_exclusive_project_operation("ingest-security-prices")
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
@_exclusive_project_operation("refresh-price-actions")
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
        artifact_directory = _resolve_generated_output_directory(
            output_dir,
            label="factor warm-up directory",
        )
        date.fromisoformat(start)
        if as_of:
            date.fromisoformat(as_of)

        def show_progress(index: int, total: int, candidate: dict) -> None:
            console.rule(
                f"[{index}/{total}] {candidate['canonical_ticker']} "
                f"({candidate['provider']}:{candidate['provider_symbol']})"
            )

        with store_scope(read_only=True) as store:
            result = _build_factor_price_warmup(
                artifact_directory,
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
                store=store,
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
@_exclusive_project_operation("ingest-factor-price-warmup")
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
        artifact_directory = _resolve_generated_output_directory(
            batch_dir,
            label="factor warm-up review directory",
        )
        count = mark_factor_price_warmup_rejections_reviewed(artifact_directory)
    except Exception as exc:
        console.print(f"[red]Warm-up rejection review failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Warm-up exclusions reviewed:[/green] {count} preserved rejection(s).")


@app.command("ingest-batch")
@_exclusive_project_operation("ingest-batch")
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
    latest_dates: list[tuple[str, str, Any]] = []
    with store_scope(read_only=True) as store:
        counts = store.table_rowcounts()
        for table, column in (
            ("prices", "date"),
            ("fundamentals", "as_of_date"),
            ("macro", "date"),
        ):
            try:
                row = store.query(f"SELECT MAX({column}) AS latest FROM {table}")[0]
            except Exception:
                continue
            latest_dates.append((table, column, row["latest"]))

    tbl = Table(title="Storage status")
    tbl.add_column("table")
    tbl.add_column("rows")
    for t, n in counts.items():
        tbl.add_row(t, str(n))
    console.print(tbl)

    # Latest dates where it makes sense
    for table, column, latest in latest_dates:
        console.print(f"[cyan]Latest {table}.{column}:[/cyan] {latest}")


@app.command()
def audit(
    limit: int = typer.Option(20, min=1, max=200, help="Number of recent runs to show"),
) -> None:
    """Show recent ingest outcomes and errors."""
    with store_scope(read_only=True) as store:
        rows = store.ingest_history(limit)
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
    with store_scope(read_only=True) as store:
        report = store.data_quality_report()
        missing_close_check = next(
            (
                row
                for row in report
                if row["check"] == "prices_missing_close" and row["status"] == "fail"
            ),
            None,
        )
        missing_close_samples = (
            store.query(
                """
                SELECT ticker,
                       CAST(date AS VARCHAR) AS date,
                       COALESCE(NULLIF(source, ''), 'unknown') AS source
                FROM prices
                WHERE close IS NULL
                   OR NOT isfinite(close)
                   OR close <= 0
                ORDER BY date DESC, ticker, source
                LIMIT ?
                """,
                (5,),
            )
            if missing_close_check is not None
            else []
        )

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
    if missing_close_samples:
        console.print(
            "[bold]Affected rows for prices_missing_close "
            f"(showing {len(missing_close_samples)} of {missing_close_check['count']}):[/bold]"
        )
        sample_tbl = Table()
        sample_tbl.add_column("ticker")
        sample_tbl.add_column("date")
        sample_tbl.add_column("source")
        for row in missing_close_samples:
            sample_tbl.add_row(str(row["ticker"]), str(row["date"]), str(row["source"]))
        console.print(sample_tbl)
    if has_failure:
        raise typer.Exit(code=1)


@app.command("cleanup-legacy-ebitda")
@_exclusive_project_operation("cleanup-legacy-ebitda")
def cleanup_legacy_ebitda(
    ticker: str | None = typer.Option(None, help="Limit cleanup to one ticker"),
) -> None:
    """Remove only the pre-correction, mislabeled EBITDA rows."""
    removed = get_store().purge_legacy_ebitda(ticker)
    scope = ticker.upper() if ticker else "all tickers"
    console.print(f"[green]Removed {removed} legacy EBITDA rows for {scope}.[/green]")


@app.command("quarantine-invalid-fundamentals")
@_exclusive_project_operation("quarantine-invalid-fundamentals")
def quarantine_invalid_fundamentals() -> None:
    """Move period_end-after-filing rows into a provenance quarantine table."""
    moved = get_store().quarantine_invalid_fundamental_periods()
    console.print(
        f"[green]Quarantined {moved} impossible fundamental rows; "
        "the source evidence remains in fundamentals_quarantine.[/green]"
    )


@app.command("cleanup-legacy-macro")
@_exclusive_project_operation("cleanup-legacy-macro")
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

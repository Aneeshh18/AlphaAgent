"""Recoverable benchmark-first orchestration for one completed U.S. session."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from functools import partial
from typing import Any

from aios.alerts import AlertStore, JobStart, get_alert_store
from aios.market_calendar import latest_completed_us_equity_session
from aios.storage.store import Store, get_store

DAILY_JOB_NAME = "us-daily-refresh"
ProgressCallback = Callable[[str, str], None]


class USDailyCycleError(RuntimeError):
    """The daily workflow stopped without certifying its target session."""


@dataclass(frozen=True)
class USDailyCycleResult:
    """Plain, serializable outcome of one idempotent daily workflow."""

    run_id: str
    status: str
    target_session: str
    benchmark_rows: int
    universe_status: str
    universe_coverage_through: str | None
    member_count: int
    member_price_rows: int
    macro_rows: int
    warning_count: int
    certified_research_through: str | None
    interrupted_run_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_us_daily_cycle(
    *,
    now: datetime | None = None,
    store: Store | None = None,
    operations: AlertStore | None = None,
    force: bool = False,
    progress: ProgressCallback | None = None,
    benchmark_ingester: Callable[[str], int] | None = None,
    market_close_resolver: Callable[[Store, date], date] | None = None,
    universe_rollforward: Callable[..., Any] | None = None,
    current_refresher: Callable[..., Any] | None = None,
    readiness_assessor: Callable[..., Any] | None = None,
) -> USDailyCycleResult:
    """Refresh and certify the latest completed U.S. session in dependency order.

    The benchmark is refreshed first because it defines the eligible session
    boundary. An unchanged universe may then be extended through that boundary,
    after which member prices and macro releases can use the newly extended
    identity windows. A top-level job record remains ``running`` if the process
    is killed, allowing the next startup and the dashboard to detect it.
    """
    checked_at = now or datetime.now(UTC)
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("now must include a timezone")
    checked_at = checked_at.astimezone(UTC)
    target = latest_completed_us_equity_session(checked_at)
    db = store or get_store()
    ledger = operations or get_alert_store()
    previous = ledger.latest_job(DAILY_JOB_NAME)
    started = ledger.begin_job(DAILY_JOB_NAME, target.isoformat(), now=checked_at)

    if benchmark_ingester is None:
        from aios.ingest.prices import ingest_prices

        benchmark_ingester = partial(ingest_prices, store=db)
    if market_close_resolver is None:
        from aios.paper import latest_reviewed_market_close

        def resolve_market_close(active_store: Store, through: date) -> date:
            return latest_reviewed_market_close(active_store, today=through)

        market_close_resolver = resolve_market_close
    if universe_rollforward is None:
        from aios.universe_rollforward import roll_forward_sp500_coverage

        universe_rollforward = roll_forward_sp500_coverage
    if current_refresher is None:
        from aios.refresh import refresh_us_current

        current_refresher = refresh_us_current
    if readiness_assessor is None:
        from aios.readiness import assess_us_readiness

        readiness_assessor = assess_us_readiness

    try:
        if (
            not force
            and previous is not None
            and previous.state == "success"
            and previous.target_session == target.isoformat()
        ):
            report = readiness_assessor(
                target,
                purpose="paper",
                store=db,
                today=target,
            )
            if report.ready and report.certified_research_through == target.isoformat():
                return _finish_success(
                    ledger,
                    started,
                    status="already_current",
                    target=target,
                    benchmark_rows=0,
                    universe_status="already_current",
                    universe_coverage_through=target.isoformat(),
                    member_count=len(db.universe_membership_on("sp500", target)),
                    member_price_rows=0,
                    macro_rows=0,
                    warning_count=0,
                    certified_research_through=report.certified_research_through,
                )

        _progress(progress, "benchmark", f"Refreshing SPY through {target.isoformat()}.")
        benchmark_rows = _positive_rows(benchmark_ingester("SPY"), "SPY benchmark")
        benchmark_through = market_close_resolver(db, target)
        if benchmark_through < target:
            raise USDailyCycleError(
                "SPY did not produce an action-safe close for "
                f"{target.isoformat()} (latest reviewed close: {benchmark_through})."
            )

        _progress(
            progress,
            "universe",
            f"Checking announcements and identities through {target.isoformat()}.",
        )
        universe = universe_rollforward(store=db, now=checked_at)
        if universe.review_required:
            raise USDailyCycleError(
                "The S&P 500 evidence check requires human review; dated reference "
                "windows were not extended."
            )
        universe_through = date.fromisoformat(universe.requested_coverage_through)
        if universe_through < target:
            raise USDailyCycleError(
                "The dated S&P 500 universe remains behind the target session "
                f"({universe_through.isoformat()} versus {target.isoformat()})."
            )

        _progress(
            progress,
            "current_data",
            "Refreshing reviewed member prices and release-dated macro evidence.",
        )

        def member_progress(
            kind: str,
            identity: str,
            index: int,
            total: int,
        ) -> None:
            if index == 1 or index == total or index % 25 == 0:
                _progress(progress, kind, f"{index}/{total} ({identity})")

        current = current_refresher(
            target,
            today=target,
            include_prices=True,
            include_benchmark=False,
            include_fundamentals=False,
            include_macro=True,
            store=db,
            progress=member_progress,
        )
        if current.failures:
            sample = ", ".join(
                f"{failure.kind}:{failure.identity}" for failure in current.failures[:5]
            )
            raise USDailyCycleError(
                f"Member or macro refresh retained {len(current.failures)} hard "
                f"failure(s): {sample}"
            )

        _progress(progress, "readiness", f"Certifying the {target.isoformat()} close.")
        report = readiness_assessor(
            target,
            purpose="paper",
            store=db,
            today=target,
        )
        if not report.ready:
            blockers = ", ".join(check.label for check in report.blockers[:5])
            raise USDailyCycleError(
                f"Exact-date readiness remains blocked for {target.isoformat()}: {blockers}"
            )
        if report.certified_research_through != target.isoformat():
            raise USDailyCycleError(
                "Readiness checks passed only for an older broad-coverage session "
                f"({report.certified_research_through or 'unavailable'}); "
                f"the required target is {target.isoformat()}."
            )

        return _finish_success(
            ledger,
            started,
            status="completed",
            target=target,
            benchmark_rows=benchmark_rows,
            universe_status=str(universe.status),
            universe_coverage_through=universe.requested_coverage_through,
            member_count=int(current.members),
            member_price_rows=int(current.price_rows),
            macro_rows=int(current.macro_rows),
            warning_count=len(current.warnings),
            certified_research_through=report.certified_research_through,
        )
    except Exception as exc:
        try:
            ledger.finish_job(
                started.run.run_id,
                state="failed",
                detail=str(exc),
                payload={
                    "target_session": target.isoformat(),
                    "error_type": type(exc).__name__,
                },
            )
        except Exception as lifecycle_exc:
            raise USDailyCycleError(
                f"{exc} The independent job ledger also failed: {lifecycle_exc}"
            ) from exc
        raise


def _finish_success(
    ledger: AlertStore,
    started: JobStart,
    *,
    status: str,
    target: date,
    benchmark_rows: int,
    universe_status: str,
    universe_coverage_through: str | None,
    member_count: int,
    member_price_rows: int,
    macro_rows: int,
    warning_count: int,
    certified_research_through: str | None,
) -> USDailyCycleResult:
    result = USDailyCycleResult(
        run_id=started.run.run_id,
        status=status,
        target_session=target.isoformat(),
        benchmark_rows=benchmark_rows,
        universe_status=universe_status,
        universe_coverage_through=universe_coverage_through,
        member_count=member_count,
        member_price_rows=member_price_rows,
        macro_rows=macro_rows,
        warning_count=warning_count,
        certified_research_through=certified_research_through,
        interrupted_run_ids=started.interrupted_run_ids,
    )
    ledger.finish_job(
        started.run.run_id,
        state="success",
        detail=(
            "The exact completed U.S. session is certified."
            if status == "completed"
            else "The exact completed U.S. session was already certified."
        ),
        payload=result.to_dict(),
    )
    return result


def _positive_rows(value: int, label: str) -> int:
    rows = int(value)
    if rows <= 0:
        raise USDailyCycleError(f"{label} returned no reviewed rows")
    return rows


def _progress(callback: ProgressCallback | None, stage: str, detail: str) -> None:
    if callback is not None:
        callback(stage, detail)

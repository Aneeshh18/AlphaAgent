from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import aios.daily as daily_module
from aios import cli
from aios.alerts import AlertStore
from aios.daily import (
    DAILY_JOB_NAME,
    USDailyCycleError,
    USDailyCycleResult,
    run_us_daily_cycle,
)

NOW = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
TARGET = date(2026, 7, 23)


def _universe(**overrides):
    values = {
        "review_required": False,
        "requested_coverage_through": TARGET.isoformat(),
        "status": "extended",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _current(**overrides):
    values = {
        "failures": (),
        "warnings": (),
        "members": 503,
        "price_rows": 2_500,
        "macro_rows": 98_000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _readiness(**overrides):
    values = {
        "ready": True,
        "blockers": (),
        "certified_research_through": TARGET.isoformat(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_daily_cycle_runs_benchmark_universe_members_and_readiness_in_order(
    tmp_path,
) -> None:
    ledger = AlertStore(tmp_path / "alerts.sqlite3")
    events: list[str] = []
    refresh_kwargs: dict = {}

    def benchmark(ticker: str) -> int:
        events.append(f"benchmark:{ticker}")
        return 5

    def market_close(_store, through: date) -> date:
        events.append("benchmark-verified")
        assert through == TARGET
        return TARGET

    def universe(**kwargs):
        events.append("universe")
        assert kwargs["now"] == NOW
        return _universe()

    def current(*args, **kwargs):
        events.append("members")
        refresh_kwargs.update(args=args, **kwargs)
        kwargs["progress"]("prices", "security:test", 1, 1)
        return _current()

    def readiness(*args, **kwargs):
        events.append("readiness")
        assert args == (TARGET,)
        assert kwargs["today"] == TARGET
        return _readiness()

    result = run_us_daily_cycle(
        now=NOW,
        store=object(),  # type: ignore[arg-type]
        operations=ledger,
        benchmark_ingester=benchmark,
        market_close_resolver=market_close,
        universe_rollforward=universe,
        current_refresher=current,
        readiness_assessor=readiness,
    )

    assert events == [
        "benchmark:SPY",
        "benchmark-verified",
        "universe",
        "members",
        "readiness",
    ]
    assert refresh_kwargs["args"] == (TARGET,)
    assert refresh_kwargs["include_benchmark"] is False
    assert refresh_kwargs["include_fundamentals"] is False
    assert result.status == "completed"
    assert result.certified_research_through == TARGET.isoformat()
    assert ledger.latest_job(DAILY_JOB_NAME).state == "success"  # type: ignore[union-attr]


def test_daily_cycle_refuses_to_certify_when_benchmark_is_still_behind(
    tmp_path,
) -> None:
    ledger = AlertStore(tmp_path / "alerts.sqlite3")

    with pytest.raises(USDailyCycleError, match="SPY did not produce"):
        run_us_daily_cycle(
            now=NOW,
            store=object(),  # type: ignore[arg-type]
            operations=ledger,
            benchmark_ingester=lambda _ticker: 5,
            market_close_resolver=lambda _store, _target: TARGET - timedelta(days=1),
            universe_rollforward=lambda **_kwargs: pytest.fail(
                "universe must not advance without the benchmark"
            ),
            current_refresher=lambda *_args, **_kwargs: pytest.fail(
                "members must not refresh without the benchmark"
            ),
            readiness_assessor=lambda *_args, **_kwargs: pytest.fail(
                "readiness must not run without the benchmark"
            ),
        )

    latest = ledger.latest_job(DAILY_JOB_NAME)
    assert latest is not None
    assert latest.state == "failed"
    assert latest.target_session == TARGET.isoformat()


def test_daily_cycle_recovers_interrupted_run_and_skips_only_after_prior_success(
    tmp_path,
) -> None:
    ledger = AlertStore(tmp_path / "alerts.sqlite3")
    abandoned = ledger.begin_job(
        DAILY_JOB_NAME,
        TARGET.isoformat(),
        now=NOW - timedelta(hours=1),
        owner_pid=999_999_999,
        owner_boot_id="old-boot",
    )

    completed = run_us_daily_cycle(
        now=NOW,
        store=object(),  # type: ignore[arg-type]
        operations=ledger,
        benchmark_ingester=lambda _ticker: 5,
        market_close_resolver=lambda _store, _target: TARGET,
        universe_rollforward=lambda **_kwargs: _universe(),
        current_refresher=lambda *_args, **_kwargs: _current(),
        readiness_assessor=lambda *_args, **_kwargs: _readiness(),
    )
    assert completed.interrupted_run_ids == (abandoned.run.run_id,)

    current_store = SimpleNamespace(
        universe_membership_on=lambda universe_id, as_of: (
            [{"ticker": "TEST"}] * 503
            if universe_id == "sp500" and as_of == TARGET
            else []
        )
    )
    skipped = run_us_daily_cycle(
        now=NOW + timedelta(minutes=30),
        store=current_store,  # type: ignore[arg-type]
        operations=ledger,
        benchmark_ingester=lambda _ticker: pytest.fail("already-current run must not fetch"),
        market_close_resolver=lambda *_args: pytest.fail("already-current run must not resolve"),
        universe_rollforward=lambda **_kwargs: pytest.fail(
            "already-current run must not review"
        ),
        current_refresher=lambda *_args, **_kwargs: pytest.fail(
            "already-current run must not refresh"
        ),
        readiness_assessor=lambda *_args, **_kwargs: _readiness(),
    )

    assert skipped.status == "already_current"
    assert skipped.benchmark_rows == 0
    assert skipped.member_count == 503


def test_daily_cycle_cli_reports_exact_certified_session(
    monkeypatch,
    tmp_path,
) -> None:
    recovered: list[str] = []
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    result = USDailyCycleResult(
        run_id="job-test",
        status="completed",
        target_session=TARGET.isoformat(),
        benchmark_rows=5,
        universe_status="extended",
        universe_coverage_through=TARGET.isoformat(),
        member_count=503,
        member_price_rows=2_500,
        macro_rows=98_000,
        warning_count=0,
        certified_research_through=TARGET.isoformat(),
        interrupted_run_ids=(),
    )
    monkeypatch.setattr(daily_module, "run_us_daily_cycle", lambda **_kwargs: result)
    monkeypatch.setattr(cli, "_resolve_operational_alert", recovered.append)

    cli_result = CliRunner().invoke(cli.app, ["refresh-us-daily"])

    assert cli_result.exit_code == 0
    assert "2026-07-23" in cli_result.output
    assert "benchmark, universe, member data" in cli_result.output
    assert "daily:us-cycle:failure" in recovered

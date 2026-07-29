from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from aios import cli


class _ReadOnlyStore:
    def table_rowcounts(self) -> dict[str, int]:
        return {"prices": 3, "fundamentals": 2, "macro": 1}

    def query(self, sql: str) -> list[dict]:
        if "FROM prices" in sql:
            return [{"latest": "2026-07-28"}]
        if "FROM fundamentals" in sql:
            return [{"latest": "2026-07-24"}]
        if "FROM macro" in sql:
            return [{"latest": "2026-07-28"}]
        raise AssertionError(f"unexpected query: {sql}")

    def ingest_history(self, limit: int) -> list[dict]:
        assert limit == 5
        return [
            {
                "id": 7,
                "source": "test",
                "table_name": "prices",
                "rows_inserted": 3,
                "status": "success",
                "finished_at": "2026-07-28T22:00:00",
                "error": None,
            }
        ]

    def universe_data_coverage(self, universe_id: str, decision_date: str) -> list[dict]:
        assert universe_id == "sp500"
        assert decision_date == "2026-07-28"
        return [
            {
                "ticker": "AAA",
                "security_id": "sec-aaa",
                "has_price_history": True,
                "has_pit_fundamentals": True,
            }
        ]


@pytest.fixture
def read_only_scope(monkeypatch):
    store = _ReadOnlyStore()
    scopes: list[dict] = []

    def scoped_store(**kwargs):
        scopes.append(kwargs)
        return nullcontext(store)

    monkeypatch.setattr(cli, "store_scope", scoped_store)
    monkeypatch.setattr(
        cli,
        "get_store",
        lambda: pytest.fail("read-only operator commands must not open a writable store"),
    )
    return store, scopes


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["doctor"], "ALL GOOD"),
        (["status"], "Latest prices.date"),
        (["audit", "--limit", "5"], "Recent ingest runs"),
        (
            ["universe-coverage", "--as-of", "2026-07-28"],
            "sp500 data coverage on 2026-07-28",
        ),
    ],
)
def test_read_only_operator_commands_use_scoped_database(
    read_only_scope,
    arguments: list[str],
    expected: str,
) -> None:
    _, scopes = read_only_scope

    result = CliRunner().invoke(cli.app, arguments)

    assert result.exit_code == 0
    assert expected in result.output
    assert scopes == [{"read_only": True}]


def test_paper_init_reads_duckdb_through_a_scoped_connection(
    monkeypatch,
    tmp_path,
    read_only_scope,
) -> None:
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    store, scopes = read_only_scope
    account = tmp_path / "account.json"
    calls: list[tuple] = []

    def initialize(path, provided_store, **kwargs):
        calls.append((path, provided_store, kwargs))
        return SimpleNamespace(path=path)

    monkeypatch.setattr("aios.paper.initialize_paper_account", initialize)

    result = CliRunner().invoke(
        cli.app,
        ["paper-init", "--account", str(account)],
    )

    assert result.exit_code == 0
    assert scopes == [{"read_only": True}]
    assert calls == [
        (
            account,
            store,
            {
                "initial_capital": 100_000.0,
                "commission_bps": 5.0,
                "slippage_bps": 5.0,
            },
        )
    ]


@pytest.mark.parametrize(
    ("target", "arguments"),
    [
        ("aios.raw_snapshots.verify_raw_snapshots", ["verify-raw-snapshots"]),
        (
            "aios.ingest.reference_batch.plan_missing_reference_windows",
            [
                "plan-reference-window-batches",
                "--as-of",
                "2026-07-28",
                "--start-floor",
                "2023-08-01",
                "--end",
                "2026-07-29",
            ],
        ),
        (
            "aios.ingest.reference_batch.plan_historical_reference_gaps",
            [
                "plan-historical-reference-batches",
                "--start",
                "2023-08-01",
                "--end",
                "2026-07-29",
            ],
        ),
        ("aios.macro.regime.compute_regime", ["macro-regime", "--as-of", "2026-07-28"]),
        (
            "aios.backtest.run_qv_policy_backtest",
            ["backtest-qv", "--start", "2025-01-01", "--end", "2026-01-01"],
        ),
        (
            "aios.ingest.factor_price_warmup.build_factor_price_warmup",
            ["build-factor-price-warmup"],
        ),
    ],
)
def test_read_only_analysis_commands_inject_the_scoped_store(
    read_only_scope,
    monkeypatch,
    target: str,
    arguments: list[str],
) -> None:
    store, scopes = read_only_scope

    def stop_after_assertion(*_args, **kwargs):
        assert kwargs["store"] is store
        raise ValueError("stop after read-only store assertion")

    monkeypatch.setattr(target, stop_after_assertion)

    result = CliRunner().invoke(cli.app, arguments)

    assert result.exit_code != 0
    assert scopes == [{"read_only": True}]


@pytest.mark.parametrize(
    ("ready", "expected_exit"),
    [(True, 0), (False, 1)],
)
def test_forward_status_never_mutates_incident_lifecycle(
    monkeypatch,
    tmp_path,
    ready: bool,
    expected_exit: int,
) -> None:
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(
        "aios.forward.assess_forward_trial",
        lambda *_args, **_kwargs: SimpleNamespace(
            trial_id="trial-test",
            registered_proposals=1,
            ready=ready,
            issues=() if ready else ("frozen policy changed",),
        ),
    )
    monkeypatch.setattr(
        cli,
        "_emit_operational_alert",
        lambda *_args, **_kwargs: pytest.fail("status must not emit incidents"),
    )
    monkeypatch.setattr(
        cli,
        "_resolve_operational_alert",
        lambda *_args, **_kwargs: pytest.fail("status must not resolve incidents"),
    )

    result = CliRunner().invoke(cli.app, ["forward-status"])

    assert result.exit_code == expected_exit

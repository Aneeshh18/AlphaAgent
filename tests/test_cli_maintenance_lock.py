from __future__ import annotations

from contextlib import nullcontext
from datetime import date
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from aios import cli
from aios import forward as forward_module
from aios import paper as paper_module
from aios.maintenance import project_maintenance_lock
from aios.storage import store as store_module


def _settings(tmp_path):
    return SimpleNamespace(
        project_root=tmp_path,
        duckdb_path=tmp_path / "data" / "aios.duckdb",
        operations_db_path=tmp_path / "data" / "operations" / "alerts.sqlite3",
    )


def test_every_backup_covered_mutation_command_is_guarded() -> None:
    guarded = (
        cli.backup,
        cli.restore,
        cli.review_universe_current,
        cli.paper_init,
        cli.forward_freeze,
        cli.forward_restart,
        cli.paper_propose,
        cli.paper_execute,
        cli.paper_mark,
        cli.ingest_macro,
        cli.refresh_us_current_command,
        cli.refresh_us_daily_command,
        cli.import_universe,
        cli.import_security_identities,
        cli.import_reference_identities,
        cli.import_security_conversions,
        cli.ingest_liquidation_prices,
        cli.ingest_reference_batch,
        cli.ingest_ticker,
        cli.ingest_issuer,
        cli.ingest_security_prices,
        cli.refresh_price_actions,
        cli.ingest_factor_price_warmup,
        cli.ingest_batch,
        cli.cleanup_legacy_ebitda,
        cli.quarantine_invalid_fundamentals,
        cli.cleanup_legacy_macro,
    )

    assert all(hasattr(command, "__wrapped__") for command in guarded)


def test_busy_project_lock_refuses_before_opening_duckdb(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))
    monkeypatch.setattr(
        store_module,
        "checkpoint_database_for_backup",
        lambda path: pytest.fail("busy refusal must happen before DuckDB opens"),
    )

    with project_maintenance_lock(tmp_path, operation="test-owner"):
        result = CliRunner().invoke(cli.app, ["backup"])

    assert result.exit_code == 75
    assert "Another AIOS mutation workflow is already running" in result.output


def test_manual_refresh_and_backup_use_the_same_project_lock(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))

    with project_maintenance_lock(tmp_path, operation="backup-owner"):
        result = CliRunner().invoke(cli.app, ["refresh-us-current"])

    assert result.exit_code == 75
    assert "Another AIOS mutation workflow is already running" in result.output


def test_command_failure_releases_the_project_lock(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))
    monkeypatch.setattr(
        store_module,
        "checkpoint_database_for_backup",
        lambda path: (_ for _ in ()).throw(RuntimeError("injected failure")),
    )
    monkeypatch.setattr(
        cli,
        "_emit_operational_alert",
        lambda alert: pytest.fail("backup failure must not initialize an old ledger"),
    )

    result = CliRunner().invoke(cli.app, ["backup"])

    assert result.exit_code == 1
    assert "deferred to preserve the pre-migration backup" in result.output
    assert "boundary" in result.output
    with project_maintenance_lock(tmp_path, operation="after-failure") as lease:
        assert lease.operation == "after-failure"


def test_mutation_guard_uses_private_umask_and_restores_caller_state(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))
    observed: list[int] = []

    @cli._exclusive_project_operation("umask-test")
    def guarded_operation() -> None:
        current = cli.os.umask(0o077)
        cli.os.umask(current)
        observed.append(current)

    original = cli.os.umask(0o027)
    try:
        guarded_operation()
        restored = cli.os.umask(0o027)
        cli.os.umask(restored)
    finally:
        cli.os.umask(original)

    assert observed == [0o077]
    assert restored == 0o027


def test_paper_propose_removes_only_its_exact_file_when_registration_fails(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))
    monkeypatch.setattr(cli, "store_scope", lambda **kwargs: nullcontext(object()))
    trial_path = tmp_path / "data" / "paper" / "us_qv_forward_trial.json"
    trial_path.parent.mkdir(parents=True)
    trial_path.write_text("trial", encoding="utf-8")
    proposal_path = tmp_path / "data" / "paper" / "proposals" / "new.json"
    document = SimpleNamespace(
        path=proposal_path,
        payload_sha256="d" * 64,
        payload={"executions": [{"proposal_id": "prior-executed"}]},
    )

    monkeypatch.setattr(
        paper_module,
        "latest_paper_decision_date",
        lambda store: date(2026, 7, 28),
    )
    monkeypatch.setattr(
        paper_module,
        "default_proposal_path",
        lambda root, decision_date: proposal_path,
    )

    def create(*args, **kwargs):
        proposal_path.parent.mkdir(parents=True)
        proposal_path.write_text("proposal", encoding="utf-8")
        return document

    monkeypatch.setattr(paper_module, "create_paper_proposal", create)
    monkeypatch.setattr(
        paper_module,
        "read_paper_document",
        lambda *args, **kwargs: document,
    )
    monkeypatch.setattr(
        forward_module,
        "assess_forward_trial",
        lambda *args, **kwargs: SimpleNamespace(ready=True, issues=()),
    )
    monkeypatch.setattr(
        forward_module,
        "read_forward_trial",
        lambda path: SimpleNamespace(
            payload={
                "frozen_configuration": {"top_n": 10},
                "proposals": [{"proposal_id": "prior-executed"}],
            }
        ),
    )
    monkeypatch.setattr(
        forward_module,
        "register_forward_proposal",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected registration failure")
        ),
    )
    monkeypatch.setattr(
        forward_module,
        "require_registered_forward_proposal",
        lambda *args, **kwargs: pytest.fail("postcondition cannot run after failure"),
    )

    result = CliRunner().invoke(cli.app, ["paper-propose"])

    assert result.exit_code == 1
    assert "injected registration failure" in result.output
    assert not proposal_path.exists()


def test_paper_propose_refuses_another_unresolved_forward_proposal(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))
    monkeypatch.setattr(cli, "store_scope", lambda **kwargs: nullcontext(object()))
    trial_path = tmp_path / "data" / "paper" / "us_qv_forward_trial.json"
    trial_path.parent.mkdir(parents=True)
    trial_path.write_text("trial", encoding="utf-8")
    proposal_path = tmp_path / "data" / "paper" / "proposals" / "new.json"

    monkeypatch.setattr(
        paper_module,
        "latest_paper_decision_date",
        lambda store: date(2026, 7, 29),
    )
    monkeypatch.setattr(
        paper_module,
        "default_proposal_path",
        lambda root, decision_date: proposal_path,
    )
    monkeypatch.setattr(
        paper_module,
        "read_paper_document",
        lambda *args, **kwargs: SimpleNamespace(
            payload={"account_id": "sandbox", "executions": []}
        ),
    )
    monkeypatch.setattr(
        paper_module,
        "create_paper_proposal",
        lambda *args, **kwargs: pytest.fail(
            "proposal creation must not start while prior evidence is unresolved"
        ),
    )
    monkeypatch.setattr(
        forward_module,
        "assess_forward_trial",
        lambda *args, **kwargs: SimpleNamespace(ready=True, issues=()),
    )
    monkeypatch.setattr(
        forward_module,
        "read_forward_trial",
        lambda path: SimpleNamespace(
            payload={
                "frozen_configuration": {"top_n": 10},
                "proposals": [{"proposal_id": "prior-unresolved"}],
            }
        ),
    )

    result = CliRunner().invoke(cli.app, ["paper-propose"])

    assert result.exit_code == 1
    assert "registered forward proposal remains unresolved" in result.output
    assert not proposal_path.exists()


def test_paper_propose_fails_closed_on_malformed_forward_lifecycle(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))
    monkeypatch.setattr(cli, "store_scope", lambda **kwargs: nullcontext(object()))
    trial_path = tmp_path / "data" / "paper" / "us_qv_forward_trial.json"
    trial_path.parent.mkdir(parents=True)
    trial_path.write_text("trial", encoding="utf-8")
    proposal_path = tmp_path / "data" / "paper" / "proposals" / "new.json"

    monkeypatch.setattr(
        paper_module,
        "latest_paper_decision_date",
        lambda store: date(2026, 7, 29),
    )
    monkeypatch.setattr(
        paper_module,
        "default_proposal_path",
        lambda root, decision_date: proposal_path,
    )
    monkeypatch.setattr(
        paper_module,
        "read_paper_document",
        lambda *args, **kwargs: SimpleNamespace(payload={"executions": []}),
    )
    monkeypatch.setattr(
        paper_module,
        "create_paper_proposal",
        lambda *args, **kwargs: pytest.fail(
            "malformed lifecycle evidence must fail before proposal creation"
        ),
    )
    monkeypatch.setattr(
        forward_module,
        "assess_forward_trial",
        lambda *args, **kwargs: SimpleNamespace(ready=True, issues=()),
    )
    monkeypatch.setattr(
        forward_module,
        "read_forward_trial",
        lambda path: SimpleNamespace(
            payload={
                "frozen_configuration": {"top_n": 10},
                "proposals": [{"decision_date": "2026-07-27"}],
            }
        ),
    )

    result = CliRunner().invoke(cli.app, ["paper-propose"])

    assert result.exit_code == 1
    assert "forward proposal lifecycle evidence is invalid" in result.output
    assert not proposal_path.exists()

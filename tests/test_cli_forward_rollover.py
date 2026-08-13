from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from aios import cli
from aios import forward_rollover as rollover_module

PLAN_SHA256 = "a" * 64
PLAN_PATH = Path(f"data/reports/forward_rollovers/plans/{PLAN_SHA256}.json")


def _settings(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        project_root=root,
        duckdb_path=Path("data/aios.duckdb"),
        operations_db_path=Path("data/operations/alerts.sqlite3"),
    )


def test_forward_rollover_defaults_to_read_only_preview(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))

    def preview(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cli, "_run_forward_rollover_preview", preview)

    result = CliRunner().invoke(cli.app, ["forward-rollover", "--json"])

    assert result.exit_code == 0
    assert captured == {
        "as_of": None,
        "account": Path("data/paper/us_qv_sandbox.json"),
        "trial": Path("data/paper/us_qv_forward_trial.json"),
        "json_output": True,
        "write_plan": False,
    }


@pytest.mark.parametrize(
    "arguments",
    [
        ["--plan", str(PLAN_PATH)],
        ["--plan-sha256", PLAN_SHA256],
        ["--confirm-rollover"],
        ["--plan", str(PLAN_PATH), "--plan-sha256", PLAN_SHA256],
        ["--plan", str(PLAN_PATH), "--confirm-rollover"],
        ["--plan-sha256", PLAN_SHA256, "--confirm-rollover"],
    ],
)
def test_forward_rollover_rejects_partial_activation_contract_before_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arguments: list[str],
) -> None:
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(
        cli,
        "_run_forward_rollover_preview",
        lambda **_kwargs: pytest.fail("partial activation must not run preview"),
    )

    result = CliRunner().invoke(
        cli.app,
        ["forward-rollover", *arguments, "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["document_kind"] == "aios.forward-rollover-activation"
    assert payload["status"] == "refused"
    assert payload["state_change_started"] is False
    assert "requires --plan, --plan-sha256, and --confirm-rollover" in (payload["error"])


@pytest.mark.parametrize(
    "extra_arguments,error",
    [
        (["--as-of", "2026-07-29"], "rejects preview-only"),
        (["--write-plan"], "rejects preview-only"),
        (["--account", "account.json"], "rejects preview-only"),
        (["--trial", "trial.json"], "rejects preview-only"),
        (
            ["--plan-sha256", "A" * 64],
            "64 lowercase hexadecimal",
        ),
    ],
)
def test_forward_rollover_rejects_ambiguous_or_noncanonical_activation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    extra_arguments: list[str],
    error: str,
) -> None:
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))
    arguments = [
        "--plan",
        str(PLAN_PATH),
        "--plan-sha256",
        PLAN_SHA256,
        "--confirm-rollover",
    ]
    if "--plan-sha256" in extra_arguments:
        index = arguments.index("--plan-sha256")
        del arguments[index : index + 2]
    arguments.extend(extra_arguments)

    result = CliRunner().invoke(
        cli.app,
        ["forward-rollover", *arguments, "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["state_change_started"] is False
    assert error in payload["error"]


def test_forward_rollover_activation_dispatches_only_the_exact_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "settings", settings)
    monkeypatch.setattr(rollover_module, "ROLLOVER_ACTIVATION_ENABLED", True)
    captured: dict[str, object] = {}
    result_value = SimpleNamespace(
        plan_sha256=PLAN_SHA256,
        attempt_id="attempt-1",
        predecessor_trial_id="trial-old",
        successor_trial_id="trial-new",
        active_trial=tmp_path / "data/paper/us_qv_forward_trial.json",
        archived_trial=tmp_path / "data/paper/archive/trial-old.json",
        archived_proposal=tmp_path / "data/paper/archive/proposal-old.json",
        successor_proposal=tmp_path / "data/paper/proposals/proposal-new.json",
        backup=SimpleNamespace(
            path=tmp_path / "backups/rollover",
            files=7,
            bytes=4096,
            manifest_sha256="b" * 64,
        ),
        journal_directory=tmp_path / "data/paper/rollovers/key/attempt-1",
        verified_receipt=(tmp_path / "data/paper/rollovers/key/attempt-1/04-verified.json"),
    )

    def execute(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return result_value

    monkeypatch.setattr(
        "aios.forward_rollover.execute_forward_rollover_from_plan",
        execute,
    )
    monkeypatch.setattr(
        cli,
        "_run_forward_rollover_preview",
        lambda **_kwargs: pytest.fail("activation must not rebuild a preview"),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "forward-rollover",
            "--plan",
            str(PLAN_PATH),
            "--plan-sha256",
            PLAN_SHA256,
            "--confirm-rollover",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "verified"
    assert payload["plan_sha256"] == PLAN_SHA256
    assert payload["active_trial"] == "data/paper/us_qv_forward_trial.json"
    assert payload["account_mutated"] is False
    assert payload["fill_recorded"] is False
    assert payload["broker_order_sent"] is False
    assert captured["args"] == (
        tmp_path,
        tmp_path / PLAN_PATH,
        PLAN_SHA256,
    )
    assert captured["kwargs"] == {
        "database_path": tmp_path / settings.duckdb_path,
        "operations_database_path": tmp_path / settings.operations_db_path,
        "application_version": cli.__version__,
        "confirm": True,
    }


def test_forward_rollover_activation_failure_never_claims_no_state_changed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))
    monkeypatch.setattr(rollover_module, "ROLLOVER_ACTIVATION_ENABLED", True)
    monkeypatch.setattr(
        "aios.forward_rollover.execute_forward_rollover_from_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("simulated crash boundary")),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "forward-rollover",
            "--plan",
            str(PLAN_PATH),
            "--plan-sha256",
            PLAN_SHA256,
            "--confirm-rollover",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["state_change_started"] == "unknown"
    assert payload["recovery_required_if_attempt_exists"] is True
    assert "simulated crash boundary" in payload["error"]


def test_forward_rollover_complete_activation_contract_honors_emergency_disable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))
    monkeypatch.setattr(rollover_module, "ROLLOVER_ACTIVATION_ENABLED", False)

    result = CliRunner().invoke(
        cli.app,
        [
            "forward-rollover",
            "--plan",
            str(PLAN_PATH),
            "--plan-sha256",
            PLAN_SHA256,
            "--confirm-rollover",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "refused"
    assert payload["state_change_started"] is False
    assert "activation is disabled in this build" in payload["error"]


def test_forward_rollover_recovery_requires_explicit_confirmation_before_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))

    result = CliRunner().invoke(
        cli.app,
        [
            "forward-rollover-recover",
            "--plan",
            str(PLAN_PATH),
            "--plan-sha256",
            PLAN_SHA256,
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "refused"
    assert payload["state_change_started"] is False
    assert "--confirm-recovery" in payload["error"]


def test_forward_rollover_recovery_dispatches_without_new_successor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))
    captured: dict[str, object] = {}
    recovery = SimpleNamespace(
        plan_sha256=PLAN_SHA256,
        attempt_id="attempt-1",
        terminal_phase="verified",
        active_trial=tmp_path / "data/paper/us_qv_forward_trial.json",
        journal_directory=tmp_path / "data/paper/rollovers/key/attempt-1",
        terminal_receipt=(tmp_path / "data/paper/rollovers/key/attempt-1/04-verified.json"),
    )

    def recover(*args: object, **kwargs: object) -> tuple[object, ...]:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return (recovery,)

    monkeypatch.setattr(
        "aios.forward_rollover.recover_forward_rollover_from_plan",
        recover,
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "forward-rollover-recover",
            "--plan",
            str(PLAN_PATH),
            "--plan-sha256",
            PLAN_SHA256,
            "--confirm-recovery",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "verified"
    assert payload["attempts"][0]["terminal_phase"] == "verified"
    assert payload["new_successor_constructed"] is False
    assert payload["fill_recorded"] is False
    assert payload["broker_order_sent"] is False
    assert captured == {
        "args": (tmp_path, tmp_path / PLAN_PATH, PLAN_SHA256),
        "kwargs": {"confirm": True},
    }

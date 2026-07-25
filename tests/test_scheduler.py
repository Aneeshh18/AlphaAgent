from __future__ import annotations

from subprocess import CompletedProcess, TimeoutExpired

import pytest
from typer.testing import CliRunner

import aios.scheduler as scheduler_module
from aios import cli
from aios.scheduler import (
    ALERT_SERVICE_NAME,
    LEGACY_TIMER_NAMES,
    LEGACY_UNIT_NAMES,
    MANAGED_MARKER,
    TIMER_NAMES,
    UNIT_NAMES,
    SchedulerInstallResult,
    enable_user_linger,
    install_user_scheduler,
    remove_user_scheduler,
    render_systemd_units,
    set_user_scheduler_active,
    user_linger_status,
    user_scheduler_status,
)


def _project(tmp_path):
    project = tmp_path / "AI Invester"
    launcher = project / ".venv" / "bin" / "aios"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o755)
    return project


def test_rendered_scheduler_uses_supported_commands_and_no_dashboard_service(tmp_path) -> None:
    project = _project(tmp_path)
    units = render_systemd_units(project)

    assert set(units) == set(UNIT_NAMES)
    assert "refresh-us-daily" in units["aios-us-daily.service"]
    assert "Restart=on-failure" in units["aios-us-daily.service"]
    assert "AIOS_DUCKDB_LOCK_WAIT_SECONDS=300" in units["aios-us-daily.service"]
    assert (
        "Tue..Sat *-*-* 02:00:00 America/New_York"
        in units["aios-us-daily.timer"]
    )
    assert "OnStartupSec=3min" in units["aios-us-daily.timer"]
    assert "refresh-us-current --no-prices --no-macro" in units["aios-us-filings.service"]
    assert "ExecStartPost=" in units["aios-us-daily.service"]
    assert " health\n" in units["aios-us-daily.service"]
    assert "OnFailure=aios-alert@%n.service" in units["aios-us-daily.service"]
    assert "alert-service-recovered --unit %n" in units["aios-us-daily.service"]
    assert "alert-service-failure --unit %i" in units[ALERT_SERVICE_NAME]
    assert "OnFailure=" not in units[ALERT_SERVICE_NAME]
    assert " backup\n" in units["aios-backup.service"]
    assert "dashboard" not in "\n".join(units.values()).lower()
    assert "WorkingDirectory=/" in units["aios-us-daily.service"]
    assert "AI\\x20Invester" in units["aios-us-daily.service"]


def test_scheduler_install_requires_confirmation_and_enables_managed_timers(tmp_path) -> None:
    project = _project(tmp_path)
    unit_dir = tmp_path / "units"
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        return CompletedProcess(command, 0, "", "")

    with pytest.raises(ValueError, match="explicit confirmation"):
        install_user_scheduler(project, unit_dir=unit_dir, runner=runner)

    result = install_user_scheduler(
        project,
        confirm=True,
        unit_dir=unit_dir,
        runner=runner,
    )

    assert result.timers == TIMER_NAMES
    assert {path.name for path in result.units} == set(UNIT_NAMES)
    assert all(path.read_text(encoding="utf-8").startswith(MANAGED_MARKER) for path in result.units)
    assert calls == [
        ["systemctl", "--user", "disable", "--now", *LEGACY_TIMER_NAMES],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", *TIMER_NAMES],
    ]


def test_scheduler_install_replaces_only_managed_legacy_units(tmp_path) -> None:
    project = _project(tmp_path)
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    for name in LEGACY_UNIT_NAMES:
        (unit_dir / name).write_text(f"{MANAGED_MARKER}\n[Unit]\n", encoding="utf-8")

    install_user_scheduler(
        project,
        confirm=True,
        unit_dir=unit_dir,
        runner=lambda command, **_kwargs: CompletedProcess(command, 0, "", ""),
    )

    assert not any((unit_dir / name).exists() for name in LEGACY_UNIT_NAMES)
    assert all((unit_dir / name).is_file() for name in UNIT_NAMES)


def test_scheduler_install_refuses_unmanaged_unit(tmp_path) -> None:
    project = _project(tmp_path)
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    (unit_dir / UNIT_NAMES[0]).write_text("[Unit]\nDescription=someone else\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unmanaged scheduler file"):
        install_user_scheduler(
            project,
            confirm=True,
            unit_dir=unit_dir,
            runner=lambda *_args, **_kwargs: pytest.fail("systemctl must not run"),
        )


def test_scheduler_pause_resume_status_and_removal_are_bounded(tmp_path) -> None:
    project = _project(tmp_path)
    unit_dir = tmp_path / "units"
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        if "show" in command and command[3].endswith(".timer"):
            output = (
                "LastTriggerUSec=Tue 2026-07-21 07:31:00 IST\n"
                "NextElapseUSecRealtime=Wed 2026-07-22 07:31:00 IST\n"
            )
        elif "show" in command:
            output = "Result=success\nExecMainStatus=0\n"
        else:
            output = "active\n"
        return CompletedProcess(command, 0, output, "")

    install_user_scheduler(
        project,
        confirm=True,
        unit_dir=unit_dir,
        runner=runner,
    )
    calls.clear()

    set_user_scheduler_active(False, runner=runner)
    set_user_scheduler_active(True, runner=runner)
    status = user_scheduler_status(runner=runner)
    removed = remove_user_scheduler(
        confirm=True,
        unit_dir=unit_dir,
        runner=runner,
    )

    assert all(value["enabled"] is True and value["active"] is True for value in status.values())
    assert all(value["service_result"] == "success" for value in status.values())
    assert all(value["exit_status"] == "0" for value in status.values())
    assert all("2026-07-21" in str(value["last_run"]) for value in status.values())
    assert all("2026-07-22" in str(value["next_trigger"]) for value in status.values())
    assert {path.name for path in removed} == set(UNIT_NAMES)
    assert not any(path.exists() for path in removed)
    assert calls[0] == ["systemctl", "--user", "disable", "--now", *TIMER_NAMES]
    assert calls[1] == ["systemctl", "--user", "enable", "--now", *TIMER_NAMES]
    assert calls[-2] == [
        "systemctl",
        "--user",
        "disable",
        "--now",
        *TIMER_NAMES,
        *LEGACY_TIMER_NAMES,
    ]
    assert calls[-1] == ["systemctl", "--user", "daemon-reload"]


def test_scheduler_status_does_not_report_unrun_systemd_defaults_as_passed() -> None:
    def runner(command, **_kwargs):
        if "is-enabled" in command or "is-active" in command:
            return CompletedProcess(command, 0, "active\n", "")
        if "show" in command and command[3].endswith(".timer"):
            output = (
                "LastTriggerUSec=\n"
                "NextElapseUSecRealtime=Wed 2026-07-22 07:30:00 IST\n"
            )
        elif "show" in command:
            output = (
                "Result=success\n"
                "ExecMainStatus=0\n"
                "ExecMainStartTimestamp=\n"
                "ExecMainExitTimestamp=\n"
            )
        else:
            output = ""
        return CompletedProcess(command, 0, output, "")

    status = user_scheduler_status(runner=runner)

    assert all(value["last_run"] == "never" for value in status.values())
    assert all(value["service_result"] == "not-run" for value in status.values())
    assert all(value["exit_status"] == "unknown" for value in status.values())


def test_scheduler_status_times_out_to_installed_file_evidence(tmp_path) -> None:
    unit_dir = tmp_path / "systemd" / "user"
    wants = unit_dir / "timers.target.wants"
    wants.mkdir(parents=True)
    for timer in TIMER_NAMES:
        unit = unit_dir / timer
        unit.write_text(f"{MANAGED_MARKER}\n[Timer]\n", encoding="utf-8")
        (wants / timer).symlink_to(unit)

    def runner(command, **_kwargs):
        raise TimeoutExpired(command, 5.0)

    status = user_scheduler_status(runner=runner, unit_dir=unit_dir)

    assert all(value["enabled"] is True for value in status.values())
    assert all(value["runtime_verified"] is False for value in status.values())
    assert all(value["service_result"] == "unverified" for value in status.values())


def test_scheduler_status_inaccessible_user_bus_uses_file_evidence(tmp_path) -> None:
    unit_dir = tmp_path / "systemd" / "user"
    wants = unit_dir / "timers.target.wants"
    wants.mkdir(parents=True)
    for timer in TIMER_NAMES:
        unit = unit_dir / timer
        unit.write_text(f"{MANAGED_MARKER}\n[Timer]\n", encoding="utf-8")
        (wants / timer).symlink_to(unit)

    def runner(command, **_kwargs):
        return CompletedProcess(
            command,
            1,
            "",
            "Failed to connect to bus: Operation not permitted\n",
        )

    status = user_scheduler_status(runner=runner, unit_dir=unit_dir)

    assert all(value["enabled"] is True for value in status.values())
    assert all(value["runtime_verified"] is False for value in status.values())
    assert all(value["service_result"] == "unverified" for value in status.values())


def test_scheduler_status_cli_explains_unverified_runtime(monkeypatch) -> None:
    emitted = []
    fallback = {
        timer: {
            "enabled": True,
            "active": False,
            "last_trigger": "unavailable",
            "last_run": "unavailable",
            "next_trigger": "unavailable",
            "service_result": "unverified",
            "exit_status": "unknown",
            "runtime_verified": False,
        }
        for timer in TIMER_NAMES
    }
    monkeypatch.setattr(scheduler_module, "user_scheduler_status", lambda: fallback)
    monkeypatch.setattr(scheduler_module, "user_linger_status", lambda: None)
    monkeypatch.setattr(cli, "_emit_operational_alert", emitted.append)

    result = CliRunner().invoke(cli.app, ["scheduler-status"])

    assert result.exit_code == 0
    assert "not verified" in result.output
    assert "did not answer within 5 seconds" in result.output
    assert [alert.code for alert in emitted] == ["scheduler_runtime_unverified"]


def test_scheduler_cli_requires_confirmation_and_explains_safe_dashboard_overlap(
    monkeypatch,
    tmp_path,
) -> None:
    def fake_install(project_root, *, confirm, enable_linger):
        assert project_root == cli.settings.project_root
        assert enable_linger is False
        if not confirm:
            raise ValueError("scheduler installation requires explicit confirmation")
        return SchedulerInstallResult(tmp_path, (), TIMER_NAMES)

    monkeypatch.setattr(scheduler_module, "install_user_scheduler", fake_install)
    refused = CliRunner().invoke(cli.app, ["scheduler-install"])
    installed = CliRunner().invoke(
        cli.app,
        ["scheduler-install", "--confirm-install"],
    )

    assert refused.exit_code == 1
    assert "explicit" in refused.output and "confirmation" in refused.output
    assert installed.exit_code == 0
    assert "short read-only database sessions" in installed.output
    assert "five minutes" in installed.output


def test_scheduler_can_explicitly_enable_and_verify_linger(monkeypatch) -> None:
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        output = "yes\n" if command[:2] == ["loginctl", "show-user"] else ""
        return CompletedProcess(command, 0, output, "")

    with pytest.raises(ValueError, match="explicit confirmation"):
        enable_user_linger(runner=runner)
    enable_user_linger(confirm=True, runner=runner)
    assert user_linger_status(runner=runner) is True
    assert calls[0][0:2] == ["loginctl", "enable-linger"]
    assert calls[1][0:2] == ["loginctl", "show-user"]

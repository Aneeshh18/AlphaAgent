from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

from typer.testing import CliRunner

import aios.cli as cli


def test_dashboard_command_wraps_streamlit_for_local_or_deployed_use(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: object())

    def fake_run(command, *, cwd, check):
        captured.update(command=command, cwd=cwd, check=check)
        return CompletedProcess(command, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = CliRunner().invoke(
        cli.app,
        ["dashboard", "--host", "0.0.0.0", "--port", "9000", "--no-browser"],
    )

    assert result.exit_code == 0
    command = captured["command"]
    assert command[:4] == [cli.sys.executable, "-m", "streamlit", "run"]
    assert command[4] == str(Path(cli.__file__).resolve().with_name("dashboard.py"))
    assert command[command.index("--server.address") + 1] == "0.0.0.0"
    assert command[command.index("--server.port") + 1] == "9000"
    assert command[command.index("--server.headless") + 1] == "true"
    assert captured["cwd"] == cli.settings.project_root
    assert captured["check"] is False


def test_dashboard_command_explains_missing_optional_dependency(monkeypatch) -> None:
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: None)

    result = CliRunner().invoke(cli.app, ["dashboard"])

    assert result.exit_code == 1
    assert "Dashboard support is not installed" in result.output

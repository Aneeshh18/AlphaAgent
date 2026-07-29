from __future__ import annotations

from pathlib import Path

from aios.config import Settings


def _project_marker(root: Path) -> None:
    (root / "src" / "aios").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'test-aios'\n", encoding="utf-8")


def test_settings_discovers_project_root_from_a_nested_working_directory(
    monkeypatch,
    tmp_path,
) -> None:
    project = tmp_path / "project"
    _project_marker(project)
    nested = project / "operator" / "shell"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    configured = Settings(_env_file=None)

    assert configured.project_root == project


def test_settings_honors_explicit_aios_project_root(
    monkeypatch,
    tmp_path,
) -> None:
    project = tmp_path / "installed-project"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("AIOS_PROJECT_ROOT", str(project))

    configured = Settings(_env_file=None)

    assert configured.project_root == project


def test_settings_refuses_missing_explicit_project_root(
    monkeypatch,
    tmp_path,
) -> None:
    missing = tmp_path / "missing"
    monkeypatch.setenv("AIOS_PROJECT_ROOT", str(missing))

    try:
        Settings(_env_file=None)
    except ValueError as exc:
        assert "AIOS_PROJECT_ROOT is not an existing directory" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("missing explicit project root was accepted")

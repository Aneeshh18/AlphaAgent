from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from aios.config import Settings, secret_value
from aios.security import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    PrivatePathError,
    ensure_private_directory,
    ensure_private_file,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _project_marker(root: Path) -> None:
    (root / "src" / "aios").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'test-aios'\n", encoding="utf-8")


def test_private_directory_is_created_and_existing_access_is_tightened(tmp_path) -> None:
    private = tmp_path / "runtime" / "operations"
    private.mkdir(parents=True, mode=0o755)
    private.chmod(0o755)

    result = ensure_private_directory(private)

    assert result == private
    assert _mode(private) == PRIVATE_DIRECTORY_MODE


def test_private_directory_refuses_symbolic_links_and_files(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    ordinary_file = tmp_path / "ordinary"
    ordinary_file.write_text("not a directory", encoding="utf-8")

    with pytest.raises(PrivatePathError, match="symbolic links"):
        ensure_private_directory(alias)
    with pytest.raises(PrivatePathError, match="not a directory"):
        ensure_private_directory(ordinary_file)


def test_private_file_is_tightened_without_following_aliases(tmp_path) -> None:
    private = tmp_path / "secret"
    private.write_text("credential", encoding="utf-8")
    private.chmod(0o644)

    result = ensure_private_file(private)

    assert result == private
    assert _mode(private) == PRIVATE_FILE_MODE

    alias = tmp_path / "secret-alias"
    alias.symlink_to(private)
    with pytest.raises(PrivatePathError, match="symbolic links"):
        ensure_private_file(alias)


def test_private_file_refuses_hard_links(tmp_path) -> None:
    private = tmp_path / "secret"
    private.write_text("credential", encoding="utf-8")
    alias = tmp_path / "secret-hard-link"
    os.link(private, alias)

    with pytest.raises(PrivatePathError, match="non-hard-linked"):
        ensure_private_file(private)


def test_settings_are_frozen_and_credentials_are_masked(tmp_path) -> None:
    project = tmp_path / "project"
    _project_marker(project)
    configured = Settings(
        _env_file=None,
        AIOS_PROJECT_ROOT=project,
        fred_api_key="fred-secret",
        simfin_api_key="simfin-secret",
        tiingo_api_key="tiingo-secret",
        anthropic_api_key="anthropic-secret",
        zai_api_key="zai-secret",
        smtp_password="smtp-secret",
    )

    secret_fields = (
        "fred_api_key",
        "simfin_api_key",
        "tiingo_api_key",
        "anthropic_api_key",
        "zai_api_key",
        "smtp_password",
    )
    for field in secret_fields:
        assert isinstance(getattr(configured, field), SecretStr)
    assert secret_value(configured.fred_api_key) == "fred-secret"
    assert "fred-secret" not in repr(configured)
    assert "fred-secret" not in configured.model_dump_json()

    with pytest.raises(ValidationError, match="Instance is frozen"):
        configured.fred_api_key = SecretStr("replacement")


def test_settings_create_owner_only_runtime_directories(tmp_path) -> None:
    project = tmp_path / "project"
    _project_marker(project)
    configured = Settings(_env_file=None, AIOS_PROJECT_ROOT=project)

    configured.ensure_dirs()

    expected = (
        project / "data",
        project / "data" / "operations",
        project / "data" / "raw",
        project / "data" / "parquet",
        project / "logs",
    )
    assert all(path.is_dir() for path in expected)
    assert all(_mode(path) == PRIVATE_DIRECTORY_MODE for path in expected)


def test_settings_refuse_to_chmod_project_root_or_external_directory(tmp_path) -> None:
    project = tmp_path / "project"
    _project_marker(project)
    project.chmod(0o755)

    root_target = Settings(
        _env_file=None,
        AIOS_PROJECT_ROOT=project,
        duckdb_path=Path("aios.duckdb"),
    )
    with pytest.raises(ValueError, match="dedicated children"):
        root_target.ensure_dirs()
    assert _mode(project) == 0o755

    external = tmp_path / "shared-runtime"
    external.mkdir(mode=0o755)
    external_target = Settings(
        _env_file=None,
        AIOS_PROJECT_ROOT=project,
        raw_data_dir=external,
    )
    with pytest.raises(ValueError, match="dedicated children"):
        external_target.ensure_dirs()
    assert _mode(external) == 0o755


def test_streamlit_secret_files_are_ignored() -> None:
    project = Path(__file__).resolve().parents[1]
    ignored = set((project / ".gitignore").read_text(encoding="utf-8").splitlines())

    assert ".streamlit/secrets.toml" in ignored
    assert ".streamlit/secrets.*.toml" in ignored
    assert "!.streamlit/secrets.toml.example" in ignored

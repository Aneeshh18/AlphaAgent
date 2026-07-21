from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aios.operations import (
    create_local_backup,
    restore_local_backup,
    verify_local_backup,
)


def test_local_backup_includes_database_and_paper_but_excludes_secrets(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    account = project / "data" / "paper" / "account.json"
    proposal = project / "data" / "paper" / "proposals" / "proposal.json"
    database.parent.mkdir(parents=True)
    account.parent.mkdir(parents=True)
    proposal.parent.mkdir(parents=True)
    database.write_bytes(b"duckdb snapshot")
    account.write_text('{"account": true}', encoding="utf-8")
    proposal.write_text('{"proposal": true}', encoding="utf-8")
    (project / ".env").write_text("SECRET=must-not-copy\n", encoding="utf-8")

    result = create_local_backup(
        project,
        database,
        application_version="test",
        now=datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
    )

    assert result.files == 3
    assert result.path.name == "aios-20260721T120000Z"
    assert (result.path / "database" / "aios.duckdb").read_bytes() == b"duckdb snapshot"
    assert (result.path / "paper" / "account.json").is_file()
    assert (result.path / "paper" / "proposals" / "proposal.json").is_file()
    assert not (result.path / ".env").exists()
    assert verify_local_backup(result.path) == result


def test_backup_verification_fails_after_file_tampering(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"original")
    result = create_local_backup(
        project,
        database,
        output=tmp_path / "snapshot",
        application_version="test",
    )
    (result.path / "database" / "aios.duckdb").write_bytes(b"changed")

    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_local_backup(result.path)


def test_backup_verification_rejects_unmanifested_files(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"original")
    result = create_local_backup(
        project,
        database,
        output=tmp_path / "snapshot",
        application_version="test",
    )
    (result.path / "unexpected.txt").write_text("not in manifest", encoding="utf-8")

    with pytest.raises(ValueError, match="unmanifested or missing file"):
        verify_local_backup(result.path)


def test_backup_verification_rejects_symbolic_links(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"original")
    result = create_local_backup(
        project,
        database,
        output=tmp_path / "snapshot",
        application_version="test",
    )
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    (result.path / "unsafe-link").symlink_to(target)

    with pytest.raises(ValueError, match="symbolic link"):
        verify_local_backup(result.path)


def test_restore_is_confirmed_exact_and_keeps_pre_restore_safety_backup(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    account = project / "data" / "paper" / "account.json"
    database.parent.mkdir(parents=True)
    account.parent.mkdir(parents=True)
    database.write_bytes(b"reviewed-old-database")
    account.write_text('{"state": "reviewed-old"}', encoding="utf-8")
    source = create_local_backup(
        project,
        database,
        output=project / "backups" / "source",
        application_version="test",
    )

    database.write_bytes(b"current-database")
    account.write_text('{"state": "current"}', encoding="utf-8")
    extra = project / "data" / "paper" / "proposals" / "newer.json"
    extra.parent.mkdir(parents=True)
    extra.write_text('{"newer": true}', encoding="utf-8")

    with pytest.raises(ValueError, match="explicit confirmation"):
        restore_local_backup(
            source.path,
            project,
            database,
            application_version="test",
        )
    assert database.read_bytes() == b"current-database"

    restored = restore_local_backup(
        source.path,
        project,
        database,
        application_version="test",
        confirm=True,
        now=datetime(2026, 7, 21, 13, 0, tzinfo=UTC),
    )

    assert database.read_bytes() == b"reviewed-old-database"
    assert account.read_text(encoding="utf-8") == '{"state": "reviewed-old"}'
    assert not extra.exists()
    assert verify_local_backup(restored.safety_backup).path == restored.safety_backup
    assert (
        restored.safety_backup / "database" / "aios.duckdb"
    ).read_bytes() == b"current-database"

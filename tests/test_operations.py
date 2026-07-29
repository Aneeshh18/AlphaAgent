from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from aios.ingest.fred import TREASURY_PARSER_VERSION, parse_treasury_yield_curve_csv
from aios.operations import (
    create_local_backup,
    drill_local_backup,
    restore_local_backup,
    verify_local_backup,
)
from aios.raw_snapshots import (
    canonical_request_fingerprint,
    capture_raw_snapshot,
)
from aios.storage.store import Store


def test_local_backup_includes_database_and_paper_but_excludes_secrets(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    account = project / "data" / "paper" / "account.json"
    proposal = project / "data" / "paper" / "proposals" / "proposal.json"
    operations = project / "data" / "operations" / "alerts.sqlite3"
    raw_payload = project / "data" / "raw" / "example" / "prices" / "payload.json.gz"
    database.parent.mkdir(parents=True)
    account.parent.mkdir(parents=True)
    proposal.parent.mkdir(parents=True)
    operations.parent.mkdir(parents=True)
    raw_payload.parent.mkdir(parents=True)
    database.write_bytes(b"duckdb snapshot")
    account.write_text('{"account": true}', encoding="utf-8")
    proposal.write_text('{"proposal": true}', encoding="utf-8")
    with sqlite3.connect(operations) as connection:
        connection.execute("CREATE TABLE incidents (incident_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO incidents VALUES ('inc-test')")
    raw_payload.write_bytes(b"immutable-provider-evidence")
    (project / ".env").write_text("SECRET=must-not-copy\n", encoding="utf-8")

    result = create_local_backup(
        project,
        database,
        operations_database_path=operations,
        application_version="test",
        now=datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
    )

    assert result.files == 5
    assert result.path.name == "aios-20260721T120000Z"
    assert (result.path / "database" / "aios.duckdb").read_bytes() == b"duckdb snapshot"
    assert (result.path / "paper" / "account.json").is_file()
    assert (result.path / "paper" / "proposals" / "proposal.json").is_file()
    with sqlite3.connect(result.path / "operations" / "alerts.sqlite3") as connection:
        assert connection.execute("SELECT incident_id FROM incidents").fetchone() == (
            "inc-test",
        )
    assert (result.path / "raw" / "example" / "prices" / "payload.json.gz").read_bytes() == (
        b"immutable-provider-evidence"
    )
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


def test_restore_keeps_newer_live_incident_history_instead_of_rolling_it_back(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    operations = project / "data" / "operations" / "alerts.sqlite3"
    database.parent.mkdir(parents=True)
    operations.parent.mkdir(parents=True)
    database.write_bytes(b"reviewed-database")
    with sqlite3.connect(operations) as connection:
        connection.execute("CREATE TABLE events (name TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO events VALUES ('before-backup')")
    source = create_local_backup(
        project,
        database,
        operations_database_path=operations,
        output=project / "backups" / "source-with-incidents",
        application_version="test",
    )
    database.write_bytes(b"newer-database")
    with sqlite3.connect(operations) as connection:
        connection.execute("INSERT INTO events VALUES ('after-backup')")

    restore_local_backup(
        source.path,
        project,
        database,
        operations_database_path=operations,
        application_version="test",
        confirm=True,
    )

    with sqlite3.connect(operations) as connection:
        events = connection.execute("SELECT name FROM events ORDER BY name").fetchall()
    assert events == [("after-backup",), ("before-backup",)]


def test_restore_merges_raw_snapshots_without_deleting_newer_payloads(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    original = project / "data" / "raw" / "source" / "dataset" / "old.bin.gz"
    database.parent.mkdir(parents=True)
    original.parent.mkdir(parents=True)
    database.write_bytes(b"reviewed-database")
    original.write_bytes(b"old-immutable")
    source = create_local_backup(
        project,
        database,
        output=project / "backups" / "source-with-raw",
        application_version="test",
    )

    original.unlink()
    newer = project / "data" / "raw" / "source" / "dataset" / "new.bin.gz"
    newer.write_bytes(b"newer-immutable")
    restore_local_backup(
        source.path,
        project,
        database,
        application_version="test",
        confirm=True,
    )

    assert original.read_bytes() == b"old-immutable"
    assert newer.read_bytes() == b"newer-immutable"


def test_restore_drill_uses_disposable_project_and_replays_raw_evidence(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    store = Store(database)
    payload = b"Date,2 Yr,10 Yr,30 Yr\n07/24/2026,4.33,4.69,5.16\n"
    rows = parse_treasury_yield_curve_csv(payload)
    try:
        capture_raw_snapshot(
            payload,
            provider="us-treasury",
            dataset="daily-yield-curve",
            artifact_kind="exact_response",
            requested_at=datetime(2026, 7, 25, 1, 0, tzinfo=UTC),
            received_at=datetime(2026, 7, 25, 1, 0, 1, tzinfo=UTC),
            request_fingerprint=canonical_request_fingerprint(
                {"method": "GET", "url": "https://example.test/treasury.csv"}
            ),
            adapter_name="test-treasury",
            adapter_version="1",
            parser_version=TREASURY_PARSER_VERSION,
            content_type="text/csv",
            parsed_rows=rows,
            store=store,
            project_root=project,
        )
    finally:
        store.close()
    source = create_local_backup(
        project,
        database,
        output=project / "backups" / "source-for-drill",
        application_version="test",
    )
    live_hash = database.read_bytes()

    result = drill_local_backup(
        source.path,
        application_version="test",
        scratch_parent=tmp_path,
    )

    assert result.source == source.path
    assert result.raw_payloads == 1
    assert result.replayed_snapshots == 1
    assert result.hard_failures == 0
    assert database.read_bytes() == live_hash
    assert not list(tmp_path.glob("aios-restore-drill-*"))

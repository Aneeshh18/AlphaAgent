from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from aios import local_state_upgrade as upgrade_module
from aios.alerts import (
    ALERT_SCHEMA_VERSION,
    Alert,
    AlertSeverity,
    AlertStore,
)
from aios.local_state_upgrade import (
    LOCAL_STATE_UPGRADE_KIND,
    recover_local_state_upgrade,
    upgrade_local_state,
)
from aios.maintenance import project_maintenance_lock
from aios.operations import drill_local_backup, verify_local_backup
from aios.paper import canonical_payload_sha256
from aios.storage.store import (
    FUNDAMENTAL_EVIDENCE_VERSIONS_MIGRATION,
    UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,
    Store,
)


def _local_state(tmp_path):
    database = tmp_path / "data/aios.duckdb"
    Store(database).close()
    store = Store(database)
    try:
        store.execute(
            "DELETE FROM schema_migrations WHERE name = ?",
            (FUNDAMENTAL_EVIDENCE_VERSIONS_MIGRATION,),
        )
        store.execute("DELETE FROM fundamental_versions")
    finally:
        store.close()

    operations = tmp_path / "data/operations/alerts.sqlite3"
    alert_store = AlertStore(operations)
    incident = alert_store.emit(
        Alert(
            code="upgrade_test",
            severity=AlertSeverity.WARNING,
            title="Upgrade test",
            body="Preserve this exact incident and event.",
            dedup_key="upgrade:test",
            source_job="test",
            notify=False,
        ),
        now=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
    )
    with sqlite3.connect(operations) as connection:
        connection.execute("DROP TRIGGER incident_events_no_update")
        connection.execute("DROP TRIGGER incident_events_no_delete")
        connection.execute("DROP TRIGGER incident_events_resolution_proof_required")
        connection.execute("PRAGMA user_version = 6")
    return database, operations, incident


def test_local_state_upgrade_backs_up_rehearses_and_preserves_old_rows(tmp_path) -> None:
    database, operations, incident = _local_state(tmp_path)
    backup_path = tmp_path / "backups/pre-upgrade"

    result = upgrade_local_state(
        tmp_path,
        database,
        operations,
        application_version="test",
        output=backup_path,
        confirm=True,
        now=datetime(2026, 7, 31, 12, 5, tzinfo=UTC),
    )

    assert verify_local_backup(backup_path) == result.backup
    drill = drill_local_backup(
        backup_path,
        application_version="test",
    )
    assert drill.hard_failures == 0
    backup_operations = backup_path / "operations/alerts.sqlite3"
    with sqlite3.connect(backup_operations) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM incident_events").fetchone()[0] == 1

    backup_database = backup_path / "database/aios.duckdb"
    backup_store = Store(backup_database, read_only=True)
    try:
        marker = backup_store.query(
            "SELECT COUNT(*) AS n FROM schema_migrations WHERE name = ?",
            (FUNDAMENTAL_EVIDENCE_VERSIONS_MIGRATION,),
        )[0]["n"]
    finally:
        backup_store.close()
    assert marker == 0

    assert result.operations_schema_before == 6
    assert result.operations_schema_after == ALERT_SCHEMA_VERSION
    assert result.fundamentals == 0
    assert result.fundamental_versions == 0
    assert AlertStore(operations, read_only=True).get(incident.incident_id) == incident

    live_store = Store(database, read_only=True)
    try:
        live_marker = live_store.query(
            "SELECT COUNT(*) AS n FROM schema_migrations WHERE name = ?",
            (FUNDAMENTAL_EVIDENCE_VERSIONS_MIGRATION,),
        )[0]["n"]
    finally:
        live_store.close()
    assert live_marker == 1

    envelope = json.loads(result.receipt.read_text(encoding="utf-8"))
    assert envelope["document_kind"] == LOCAL_STATE_UPGRADE_KIND
    assert envelope["payload"]["phase"] == "verified"
    assert envelope["payload"]["state"]["rehearsal_contract_matched_live"] is True
    assert envelope["payload_sha256"] == canonical_payload_sha256(envelope["payload"])
    prepared = json.loads(
        (result.journal_directory / "00-prepared.json").read_text(encoding="utf-8")
    )
    assert (
        prepared["payload"]["state"]["backup"]["manifest_sha256"]
        == result.backup.manifest_sha256
    )
    phase_files = sorted(result.journal_directory.glob("*.json"))
    assert [path.name for path in phase_files] == [
        "00-prepared.json",
        "01-analytical_migrated.json",
        "02-operations_migrated.json",
        "03-verified.json",
    ]


def test_030_pre_upgrade_backup_of_020_database_restores_and_drills(tmp_path) -> None:
    database, operations, _incident = _local_state(tmp_path)
    legacy = Store(database)
    try:
        legacy.execute(
            "DELETE FROM schema_migrations WHERE name = ?",
            (UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,),
        )
        legacy.execute("DROP TABLE universe_constituent_change_activations")
    finally:
        legacy.close()

    result = upgrade_local_state(
        tmp_path,
        database,
        operations,
        application_version="0.3.0",
        output=tmp_path / "backups/pre-upgrade-020",
        confirm=True,
        now=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
    )

    manifest = json.loads(
        (result.backup.path / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["application_version"] == "0.3.0"
    assert manifest["database_compatibility_version"] == "0.2.0"
    drill = drill_local_backup(
        result.backup.path,
        application_version="0.3.0",
    )
    assert drill.hard_failures == 0

    live = Store(database, read_only=True)
    try:
        live.require_universe_change_activation_schema()
    finally:
        live.close()


def test_local_state_upgrade_requires_confirmation_before_backup_or_migration(
    tmp_path,
) -> None:
    database, operations, _incident = _local_state(tmp_path)
    database_before = database.read_bytes()
    operations_before = operations.read_bytes()

    with pytest.raises(ValueError, match="explicit confirmation"):
        upgrade_local_state(
            tmp_path,
            database,
            operations,
            application_version="test",
        )

    assert database.read_bytes() == database_before
    assert operations.read_bytes() == operations_before
    assert not (tmp_path / "backups").exists()


def test_rehearsal_failure_keeps_live_state_pre_upgrade(
    tmp_path,
    monkeypatch,
) -> None:
    database, operations, _incident = _local_state(tmp_path)
    backup_path = tmp_path / "backups/pre-upgrade"

    class RehearsalFailure:
        def __init__(self, *_args, **_kwargs) -> None:
            raise RuntimeError("injected rehearsal failure")

    monkeypatch.setattr(upgrade_module, "AlertStore", RehearsalFailure)

    with pytest.raises(RuntimeError, match="injected rehearsal failure"):
        upgrade_local_state(
            tmp_path,
            database,
            operations,
            application_version="test",
            output=backup_path,
            confirm=True,
        )

    assert verify_local_backup(backup_path).path == backup_path.resolve()
    with sqlite3.connect(operations) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
    live_store = Store(database, read_only=True)
    try:
        marker = live_store.query(
            "SELECT COUNT(*) AS n FROM schema_migrations WHERE name = ?",
            (FUNDAMENTAL_EVIDENCE_VERSIONS_MIGRATION,),
        )[0]["n"]
    finally:
        live_store.close()
    assert marker == 0


def test_public_local_state_upgrade_api_owns_the_maintenance_lease(tmp_path) -> None:
    database, operations, _incident = _local_state(tmp_path)
    with (
        project_maintenance_lock(tmp_path, operation="competing-writer"),
        pytest.raises(RuntimeError, match="mutation lease is already held"),
    ):
        upgrade_local_state(
            tmp_path,
            database,
            operations,
            application_version="test",
            output=tmp_path / "backups/pre-upgrade",
            confirm=True,
        )

    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("unsafe_kind", ["outside", "symlink"])
def test_local_state_upgrade_rejects_unsafe_backup_destination_before_checkpoint(
    tmp_path,
    monkeypatch,
    unsafe_kind: str,
) -> None:
    database, operations, _incident = _local_state(tmp_path)
    database_before = database.read_bytes()
    operations_before = operations.read_bytes()
    if unsafe_kind == "outside":
        output = tmp_path.parent / f"{tmp_path.name}-outside"
        expected = "must stay under"
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / "backups").symlink_to(outside, target_is_directory=True)
        output = tmp_path / "backups/pre-upgrade"
        expected = "symbolic links"
    monkeypatch.setattr(
        upgrade_module,
        "checkpoint_database_for_backup",
        lambda path: pytest.fail("unsafe output must fail before checkpoint"),
    )

    with pytest.raises(ValueError, match=expected):
        upgrade_local_state(
            tmp_path,
            database,
            operations,
            application_version="test",
            output=output,
            confirm=True,
        )

    assert database.read_bytes() == database_before
    assert operations.read_bytes() == operations_before


@pytest.mark.parametrize("failed_phase", [1, 2, 3])
def test_checksum_journal_recovers_every_post_mutation_publication_failure(
    tmp_path,
    monkeypatch,
    failed_phase: int,
) -> None:
    database, operations, _incident = _local_state(tmp_path)
    backup_path = tmp_path / "backups/pre-upgrade"
    write_phase = upgrade_module._write_upgrade_phase

    def inject_failure(journal, *, order, **kwargs):
        if order == failed_phase:
            raise OSError(f"injected phase {failed_phase} publication failure")
        return write_phase(journal, order=order, **kwargs)

    monkeypatch.setattr(upgrade_module, "_write_upgrade_phase", inject_failure)
    with pytest.raises(RuntimeError, match="requires recovery review"):
        upgrade_local_state(
            tmp_path,
            database,
            operations,
            application_version="test",
            output=backup_path,
            confirm=True,
        )

    journals = list(
        (tmp_path / "data/reports/local_state_upgrades/attempts").glob("*/*")
    )
    assert len(journals) == 1
    journal = journals[0]
    assert (journal / "00-prepared.json").is_file()
    assert verify_local_backup(backup_path).path == backup_path.resolve()

    monkeypatch.setattr(upgrade_module, "_write_upgrade_phase", write_phase)
    recovered = recover_local_state_upgrade(
        tmp_path,
        journal,
        database,
        operations,
        confirm=True,
    )

    assert recovered.journal_directory == journal
    assert recovered.receipt == journal / "03-verified.json"
    assert sorted(path.name for path in journal.glob("*.json")) == [
        "00-prepared.json",
        "01-analytical_migrated.json",
        "02-operations_migrated.json",
        "03-verified.json",
    ]

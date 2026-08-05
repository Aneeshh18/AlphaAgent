from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from aios.alerts import (
    Alert,
    AlertSeverity,
    AlertStore,
    AnomalyObservation,
    AnomalyScan,
    build_scheduler_recovery_evidence,
    canonical_anomaly_fingerprint,
)
from aios.forward import FORWARD_DOCUMENT_KIND
from aios.ingest.fred import TREASURY_PARSER_VERSION, parse_treasury_yield_curve_csv
from aios.operations import (
    create_local_backup,
    drill_local_backup,
    restore_local_backup,
    verify_local_backup,
)
from aios.paper import (
    ACCOUNT_DOCUMENT_KIND,
    PROPOSAL_DOCUMENT_KIND,
    canonical_payload_sha256,
    initialize_paper_account,
    read_paper_document,
)
from aios.raw_snapshots import (
    canonical_request_fingerprint,
    capture_raw_snapshot,
)
from aios.scheduler import TIMER_NAMES
from aios.storage.store import (
    UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,
    Store,
    checkpoint_database_for_backup,
)


def _alert(code: str) -> Alert:
    return Alert(
        code=code,
        severity=AlertSeverity.WARNING,
        title=f"{code} warning",
        body=f"{code} requires review.",
        dedup_key=f"test:{code}",
        source_job="test",
        notify=False,
    )


def test_backup_checkpoint_does_not_run_application_schema_migrations(tmp_path) -> None:
    database = tmp_path / "pre-migration.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("CREATE TABLE preserved_state(value INTEGER)")
        connection.execute("INSERT INTO preserved_state VALUES (7)")

    checkpoint_database_for_backup(database)

    with duckdb.connect(str(database), read_only=True) as connection:
        tables = {
            row[0]
            for row in connection.execute("SHOW TABLES").fetchall()
        }
        value = connection.execute("SELECT value FROM preserved_state").fetchone()
    assert tables == {"preserved_state"}
    assert value == (7,)


def test_backup_checkpoint_rejects_hardlink_and_symlink_ancestor(tmp_path) -> None:
    database = tmp_path / "database.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("CREATE TABLE preserved_state(value INTEGER)")
    hardlink = tmp_path / "database-hardlink.duckdb"
    hardlink.hardlink_to(database)

    with pytest.raises(ValueError, match="regular, unaliased"):
        checkpoint_database_for_backup(database)

    hardlink.unlink()
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = outside / "database.duckdb"
    database.rename(moved)
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic links"):
        checkpoint_database_for_backup(alias / "database.duckdb")


def _anomaly_scan() -> AnomalyScan:
    rule_id = "sec_fundamental_coverage"
    rule_version = "1"
    scope = "sec-fundamental-coverage:2026-07-27"
    subject_type = "issuer"
    subject_id = "issuer-test"
    observation = AnomalyObservation(
        fingerprint=canonical_anomaly_fingerprint(
            rule_id=rule_id,
            rule_version=rule_version,
            scope=scope,
            subject_type=subject_type,
            subject_id=subject_id,
        ),
        rule_id=rule_id,
        rule_version=rule_version,
        scope=scope,
        subject_type=subject_type,
        subject_id=subject_id,
        severity="medium",
        confidence="high",
        title="SEC fundamental coverage is missing",
        summary="The issuer has no accepted CompanyFacts rows.",
        old_value={"accepted_rows": 1},
        new_value={"accepted_rows": 0},
        evidence={"run_id": "run-test"},
        suggested_checks=("Inspect the immutable CompanyFacts payload.",),
    )
    return AnomalyScan(
        scan_id="scan-test",
        rule_bundle_version="sec-coverage-v1",
        scope=observation.scope,
        source_boundary_sha256=hashlib.sha256(b"boundary-test").hexdigest(),
        source_boundary_at=datetime(2026, 7, 28, 1, 0, tzinfo=UTC),
        executed_rules=("sec_fundamental_coverage@1",),
        observations=(observation,),
        evidence={"certified_close": "2026-07-27"},
    )


def _refresh_manifest_file(backup, relative: str) -> None:
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target = backup / relative
    for item in manifest["files"]:
        if item["path"] == relative:
            payload = target.read_bytes()
            item["bytes"] = len(payload)
            item["sha256"] = hashlib.sha256(payload).hexdigest()
            break
    else:  # pragma: no cover - test helper misuse
        raise AssertionError(f"manifest does not contain {relative}")
    manifest_path.write_text(
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _create_valid_database(path) -> None:
    store = Store(path)
    store.close()


def _write_checksum_envelope(path, *, kind: str, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "document_kind": kind,
        "payload_sha256": canonical_payload_sha256(payload),
        "payload": payload,
    }
    if kind in {ACCOUNT_DOCUMENT_KIND, PROPOSAL_DOCUMENT_KIND}:
        envelope["document_schema_version"] = 1
    path.write_text(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


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
    _create_valid_database(database)
    account.write_text('{"account": true}', encoding="utf-8")
    proposal.write_text('{"proposal": true}', encoding="utf-8")
    incident = AlertStore(operations).emit(_alert("before_backup"))
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
    assert (result.path / "database" / "aios.duckdb").read_bytes() == database.read_bytes()
    assert (result.path / "paper" / "account.json").is_file()
    assert (result.path / "paper" / "proposals" / "proposal.json").is_file()
    operations_backup = result.path / "operations" / "alerts.sqlite3"
    assert not (operations_backup.parent / f"{operations_backup.name}-wal").exists()
    assert not (operations_backup.parent / f"{operations_backup.name}-shm").exists()
    with sqlite3.connect(
        f"{operations_backup.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
    ) as connection:
        assert connection.execute("SELECT incident_id FROM incidents").fetchone() == (
            incident.incident_id,
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
    _create_valid_database(database)
    result = create_local_backup(
        project,
        database,
        output=tmp_path / "snapshot",
        application_version="test",
    )
    (result.path / "database" / "aios.duckdb").write_bytes(b"changed")

    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_local_backup(result.path)


def test_backup_creation_rejects_unreadable_or_non_aios_duckdb(tmp_path) -> None:
    project = tmp_path / "project"
    unreadable = project / "data" / "unreadable.duckdb"
    unreadable.parent.mkdir(parents=True)
    unreadable.write_bytes(b"not-a-duckdb")
    with pytest.raises(ValueError, match="readable DuckDB"):
        create_local_backup(
            project,
            unreadable,
            output=project / "backups/unreadable",
            application_version="0.3.0",
        )

    foreign = project / "data" / "foreign.duckdb"
    with duckdb.connect(str(foreign)) as connection:
        connection.execute("CREATE TABLE arbitrary(value INTEGER)")
    with pytest.raises(ValueError, match="reviewed AIOS 0.2"):
        create_local_backup(
            project,
            foreign,
            output=project / "backups/foreign",
            application_version="0.3.0",
        )

    assert not (project / "backups").exists()


def test_backup_verification_rejects_unmanifested_files(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    database.parent.mkdir(parents=True)
    _create_valid_database(database)
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
    _create_valid_database(database)
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


def test_backup_verification_accepts_legacy_schema_zero_incident_only_snapshot(
    tmp_path,
) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    operations = project / "data" / "operations" / "alerts.sqlite3"
    database.parent.mkdir(parents=True)
    operations.parent.mkdir(parents=True)
    _create_valid_database(database)
    with sqlite3.connect(operations) as connection:
        connection.execute("CREATE TABLE incidents (incident_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO incidents VALUES ('inc-legacy')")

    result = create_local_backup(
        project,
        database,
        operations_database_path=operations,
        output=project / "backups" / "schema-zero",
        application_version="test",
    )

    assert verify_local_backup(result.path) == result


def test_backup_verification_rejects_anomaly_schema_without_version(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    operations = project / "data" / "operations" / "alerts.sqlite3"
    database.parent.mkdir(parents=True)
    _create_valid_database(database)
    AlertStore(operations)
    with sqlite3.connect(operations) as connection:
        connection.execute("PRAGMA user_version = 0")

    with pytest.raises(ValueError, match="anomaly evidence without"):
        create_local_backup(
            project,
            database,
            operations_database_path=operations,
            output=project / "backups" / "unversioned-anomalies",
            application_version="test",
        )


def test_backup_verification_accepts_supported_v4_operations_snapshot(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    operations = project / "data" / "operations" / "alerts.sqlite3"
    database.parent.mkdir(parents=True)
    _create_valid_database(database)
    AlertStore(operations).emit(_alert("legacy"))
    with sqlite3.connect(operations) as connection:
        connection.executescript(
            """
            DROP TABLE anomaly_case_events;
            DROP TABLE anomaly_cases;
            DROP TABLE anomaly_scans;
            PRAGMA user_version = 4;
            """
        )

    result = create_local_backup(
        project,
        database,
        operations_database_path=operations,
        output=project / "backups" / "v4",
        application_version="test",
    )

    assert verify_local_backup(result.path) == result


def test_backup_verification_rejects_operations_foreign_key_corruption(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    operations = project / "data" / "operations" / "alerts.sqlite3"
    database.parent.mkdir(parents=True)
    _create_valid_database(database)
    AlertStore(operations)
    result = create_local_backup(
        project,
        database,
        operations_database_path=operations,
        output=project / "backups" / "foreign-key-corrupt",
        application_version="test",
    )
    backed_up_operations = result.path / "operations" / "alerts.sqlite3"
    with sqlite3.connect(backed_up_operations) as connection:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            INSERT INTO incident_events (
                event_id, incident_id, event_type, created_at, payload_json
            ) VALUES ('event-orphan', 'inc-missing', 'opened',
                      '2026-07-28T01:00:00Z', '{}')
            """
        )
    _refresh_manifest_file(result.path, "operations/alerts.sqlite3")

    with pytest.raises(ValueError, match="foreign-key check failed"):
        verify_local_backup(result.path)


def test_backup_verification_rejects_incomplete_v5_anomaly_schema(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    operations = project / "data" / "operations" / "alerts.sqlite3"
    database.parent.mkdir(parents=True)
    _create_valid_database(database)
    AlertStore(operations)
    result = create_local_backup(
        project,
        database,
        operations_database_path=operations,
        output=project / "backups" / "schema-corrupt",
        application_version="test",
    )
    backed_up_operations = result.path / "operations" / "alerts.sqlite3"
    with sqlite3.connect(backed_up_operations) as connection:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("DROP TRIGGER anomaly_case_events_no_update")
    _refresh_manifest_file(result.path, "operations/alerts.sqlite3")

    with pytest.raises(ValueError, match="anomaly schema is incomplete"):
        verify_local_backup(result.path)


@pytest.mark.parametrize(
    "trigger_name",
    [
        "incident_events_no_update",
        "incident_events_resolution_proof_required",
    ],
)
def test_backup_verification_rejects_missing_v7_incident_history_trigger(
    tmp_path,
    trigger_name: str,
) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    operations = project / "data" / "operations" / "alerts.sqlite3"
    database.parent.mkdir(parents=True)
    _create_valid_database(database)
    AlertStore(operations).emit(_alert("incident-history"))
    result = create_local_backup(
        project,
        database,
        operations_database_path=operations,
        output=project / "backups" / "incident-schema-corrupt",
        application_version="test",
    )
    backed_up_operations = result.path / "operations" / "alerts.sqlite3"
    with sqlite3.connect(backed_up_operations) as connection:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute(f"DROP TRIGGER {trigger_name}")
    _refresh_manifest_file(result.path, "operations/alerts.sqlite3")

    with pytest.raises(ValueError, match="incident schema is incomplete"):
        verify_local_backup(result.path)


def test_backup_verification_rejects_incident_action_proof_tampering(
    tmp_path,
) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    operations = project / "data" / "operations" / "alerts.sqlite3"
    database.parent.mkdir(parents=True)
    _create_valid_database(database)
    ledger = AlertStore(operations)
    incident = ledger.emit(_alert("incident-action-proof"))
    ledger.resolve(
        incident.incident_id,
        actor="ops@example.test",
        note="The observation was a bounded false positive.",
        outcome="false_positive",
        expected_evidence_sha256=incident.evidence_sha256,
    )
    result = create_local_backup(
        project,
        database,
        operations_database_path=operations,
        output=project / "backups" / "incident-proof-corrupt",
        application_version="test",
    )
    backed_up_operations = result.path / "operations" / "alerts.sqlite3"
    with sqlite3.connect(backed_up_operations) as connection:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("DROP TRIGGER incident_events_no_update")
        row = connection.execute(
            """
            SELECT event_id, payload_json
            FROM incident_events
            WHERE event_type = 'resolved'
            """
        ).fetchone()
        payload = json.loads(str(row[1]))
        payload["_aios_incident_action_audit_v1"]["note"] = "tampered"
        connection.execute(
            "UPDATE incident_events SET payload_json = ? WHERE event_id = ?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), row[0]),
        )
        connection.executescript(
            """
            CREATE TRIGGER incident_events_no_update
            BEFORE UPDATE ON incident_events
            BEGIN
                SELECT RAISE(ABORT, 'incident history is append-only');
            END;
            """
        )
    _refresh_manifest_file(result.path, "operations/alerts.sqlite3")

    with pytest.raises(ValueError, match="audit proof does not match"):
        verify_local_backup(result.path)


def test_backup_rejects_rehashed_producer_proof_with_impossible_observation_time(
    tmp_path,
) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    operations = project / "data" / "operations" / "alerts.sqlite3"
    database.parent.mkdir(parents=True)
    _create_valid_database(database)
    opened_at = datetime(2026, 7, 30, 2, 0, tzinfo=UTC)
    ledger = AlertStore(operations)
    incident = ledger.emit(
        Alert(
            code="scheduler_runtime_unverified",
            severity=AlertSeverity.WARNING,
            title="Scheduler runtime needs proof",
            body="The systemd user manager did not provide live evidence.",
            dedup_key="scheduler:runtime-unverified",
            source_job="aios scheduler-status",
            notify=False,
        ),
        now=opened_at,
    )
    context = ledger.recovery_context(incident.fingerprint)
    assert context is not None
    status = {
        timer: {
            "enabled": True,
            "active": True,
            "last_trigger": "Wed 2026-07-29 13:00:00 UTC",
            "last_run": "Wed 2026-07-29 13:05:00 UTC",
            "next_trigger": "Thu 2026-07-30 13:00:00 UTC",
            "service_result": "success",
            "exit_status": "0",
            "runtime_verified": True,
        }
        for timer in TIMER_NAMES
    }
    ledger.resolve_fingerprint(
        incident.fingerprint,
        recovery=build_scheduler_recovery_evidence(
            context,
            status,
            observed_at=opened_at + timedelta(minutes=1),
        ),
        now=opened_at + timedelta(minutes=2),
    )
    result = create_local_backup(
        project,
        database,
        operations_database_path=operations,
        output=project / "backups" / "producer-time-corrupt",
        application_version="test",
    )
    backed_up_operations = result.path / "operations" / "alerts.sqlite3"
    with sqlite3.connect(backed_up_operations) as connection:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("DROP TRIGGER incident_events_no_update")
        row = connection.execute(
            """
            SELECT event_id, payload_json
            FROM incident_events
            WHERE event_type = 'resolved'
            """
        ).fetchone()
        payload = json.loads(str(row[1]))
        proof = payload["_aios_incident_recovery_proof_v1"]
        proof["observed_at"] = (
            opened_at + timedelta(minutes=3)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        proof.pop("proof_sha256")
        canonical = json.dumps(
            proof,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        proof["proof_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
        connection.execute(
            "UPDATE incident_events SET payload_json = ? WHERE event_id = ?",
            (
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                row[0],
            ),
        )
        connection.executescript(
            """
            CREATE TRIGGER incident_events_no_update
            BEFORE UPDATE ON incident_events
            BEGIN
                SELECT RAISE(ABORT, 'incident history is append-only');
            END;
            """
        )
    _refresh_manifest_file(result.path, "operations/alerts.sqlite3")

    with pytest.raises(ValueError, match="invalid incident resolution proof"):
        verify_local_backup(result.path)


def test_backup_verification_rejects_anomaly_case_projection_tampering(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    operations = project / "data" / "operations" / "alerts.sqlite3"
    database.parent.mkdir(parents=True)
    _create_valid_database(database)
    ledger = AlertStore(operations)
    scan = _anomaly_scan()
    ledger.record_anomaly_scan(scan, now=scan.source_boundary_at)
    result = create_local_backup(
        project,
        database,
        operations_database_path=operations,
        output=project / "backups" / "case-corrupt",
        application_version="test",
    )
    backed_up_operations = result.path / "operations" / "alerts.sqlite3"
    with sqlite3.connect(backed_up_operations) as connection:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute(
            "UPDATE anomaly_cases SET current_evidence_sha256 = ?",
            ("0" * 64,),
        )
    _refresh_manifest_file(result.path, "operations/alerts.sqlite3")

    with pytest.raises(ValueError, match="evidence integrity check failed"):
        verify_local_backup(result.path)


def test_restore_is_confirmed_exact_and_keeps_pre_restore_safety_backup(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    account = project / "data" / "paper" / "account.json"
    store = Store(database)
    initialize_paper_account(account, store)
    store.close()
    source = create_local_backup(
        project,
        database,
        output=project / "backups" / "source",
        application_version="test",
    )
    source_database = (source.path / "database" / "aios.duckdb").read_bytes()
    source_account = (source.path / "paper" / "account.json").read_text(
        encoding="utf-8"
    )

    current = Store(database)
    current.execute("CREATE TABLE restore_test_marker (value INTEGER)")
    current.close()
    account.write_text('{"state": "current"}', encoding="utf-8")
    current_database = database.read_bytes()
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
    assert database.read_bytes() == current_database

    restored = restore_local_backup(
        source.path,
        project,
        database,
        application_version="test",
        confirm=True,
        now=datetime(2026, 7, 21, 13, 0, tzinfo=UTC),
    )

    assert database.read_bytes() == source_database
    assert account.read_text(encoding="utf-8") == source_account
    assert not extra.exists()
    assert verify_local_backup(restored.safety_backup).path == restored.safety_backup
    assert (
        restored.safety_backup / "database" / "aios.duckdb"
    ).read_bytes() == current_database


def test_restore_rejects_hash_consistent_corrupt_duckdb_before_live_swap(
    tmp_path,
) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    paper = project / "data" / "paper" / "live.json"
    _create_valid_database(database)
    paper.parent.mkdir(parents=True)
    paper.write_text('{"live": true}', encoding="utf-8")
    source = create_local_backup(
        project,
        database,
        output=project / "backups" / "corrupt-source",
        application_version="test",
    )
    backed_up_database = source.path / "database" / "aios.duckdb"
    backed_up_database.write_bytes(b"hash-consistent but not DuckDB")
    _refresh_manifest_file(source.path, "database/aios.duckdb")
    with pytest.raises(ValueError, match="readable DuckDB"):
        verify_local_backup(source.path)
    live_database = database.read_bytes()
    live_paper = paper.read_bytes()

    with pytest.raises(ValueError, match="readable DuckDB"):
        restore_local_backup(
            source.path,
            project,
            database,
            application_version="test",
            confirm=True,
        )

    assert database.read_bytes() == live_database
    assert paper.read_bytes() == live_paper
    assert not list((project / "backups").glob("pre-restore-*"))


def test_restore_rejects_incompatible_backup_application_before_live_swap(
    tmp_path,
) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    paper = project / "data" / "paper" / "live.json"
    _create_valid_database(database)
    paper.parent.mkdir(parents=True)
    paper.write_text('{"live": true}', encoding="utf-8")
    source = create_local_backup(
        project,
        database,
        output=project / "backups" / "incompatible-source",
        application_version="older",
    )
    live_database = database.read_bytes()
    live_paper = paper.read_bytes()

    with pytest.raises(ValueError, match="application version is incompatible"):
        restore_local_backup(
            source.path,
            project,
            database,
            application_version="current",
            confirm=True,
        )

    assert database.read_bytes() == live_database
    assert paper.read_bytes() == live_paper
    assert not list((project / "backups").glob("pre-restore-*"))


def test_restore_migrates_supported_020_backup_under_030(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    _create_valid_database(database)
    legacy = duckdb.connect(str(database))
    try:
        legacy.execute(
            "DELETE FROM schema_migrations WHERE name = ?",
            (UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,),
        )
        legacy.execute("DROP TABLE universe_constituent_change_activations")
    finally:
        legacy.close()
    source = create_local_backup(
        project,
        database,
        output=project / "backups" / "v020-source",
        application_version="0.2.0",
    )

    result = restore_local_backup(
        source.path,
        project,
        database,
        application_version="0.3.0",
        confirm=True,
    )

    assert result.source == source.path
    restored = Store(database, read_only=True)
    try:
        restored.require_universe_change_activation_schema()
        assert restored.query(
            "SELECT COUNT(*) AS n FROM universe_constituent_change_activations"
        ) == [{"n": 0}]
    finally:
        restored.close()


def test_restore_rejects_030_backup_missing_activation_capability(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    _create_valid_database(database)
    source = create_local_backup(
        project,
        database,
        output=project / "backups" / "corrupt-v030-source",
        application_version="0.3.0",
    )
    backup_database = source.path / "database" / "aios.duckdb"
    corrupted = duckdb.connect(str(backup_database))
    try:
        corrupted.execute(
            "DELETE FROM schema_migrations WHERE name = ?",
            (UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,),
        )
        corrupted.execute("DROP TABLE universe_constituent_change_activations")
    finally:
        corrupted.close()
    _refresh_manifest_file(source.path, "database/aios.duckdb")
    live_before = database.read_bytes()

    with pytest.raises(ValueError, match="compatibility does not match"):
        restore_local_backup(
            source.path,
            project,
            database,
            application_version="0.3.0",
            confirm=True,
        )

    assert database.read_bytes() == live_before
    assert not list((project / "backups").glob("pre-restore-*"))


def test_backup_verification_rejects_forged_database_compatibility(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    _create_valid_database(database)
    source = create_local_backup(
        project,
        database,
        output=project / "backups/source",
        application_version="0.3.0",
    )
    manifest_path = source.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["database_compatibility_version"] = "0.2.0"
    manifest_path.write_text(
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="compatibility does not match"):
        verify_local_backup(source.path)


def test_restore_rejects_hard_data_quality_failure_before_live_swap(tmp_path) -> None:
    candidate = tmp_path / "candidate"
    candidate_database = candidate / "data" / "aios.duckdb"
    invalid = Store(candidate_database)
    invalid.execute(
        """
        INSERT INTO fundamentals (
            ticker, period_end, as_of_date, metric, value
        ) VALUES ('BAD', DATE '2026-07-01', DATE '2099-01-01', 'assets', 1.0)
        """
    )
    invalid.close()
    source = create_local_backup(
        candidate,
        candidate_database,
        output=tmp_path / "hard-failure-source",
        application_version="test",
    )
    assert verify_local_backup(source.path).path == source.path

    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    paper = project / "data" / "paper" / "live.json"
    _create_valid_database(database)
    paper.parent.mkdir(parents=True)
    paper.write_text('{"live": true}', encoding="utf-8")
    live_database = database.read_bytes()
    live_paper = paper.read_bytes()

    with pytest.raises(ValueError, match="hard data-quality failure"):
        restore_local_backup(
            source.path,
            project,
            database,
            application_version="test",
            confirm=True,
        )

    assert database.read_bytes() == live_database
    assert paper.read_bytes() == live_paper


def test_restore_rejects_hash_consistent_invalid_paper_json_before_swap(
    tmp_path,
) -> None:
    candidate = tmp_path / "candidate"
    candidate_database = candidate / "data" / "aios.duckdb"
    invalid_paper = candidate / "data" / "paper" / "broken.json"
    _create_valid_database(candidate_database)
    invalid_paper.parent.mkdir(parents=True)
    invalid_paper.write_text("{not-json", encoding="utf-8")
    source = create_local_backup(
        candidate,
        candidate_database,
        output=tmp_path / "invalid-paper-source",
        application_version="test",
    )
    assert verify_local_backup(source.path).path == source.path

    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    live_paper = project / "data" / "paper" / "live.json"
    _create_valid_database(database)
    live_paper.parent.mkdir(parents=True)
    live_paper.write_text('{"live": true}', encoding="utf-8")
    database_before = database.read_bytes()
    paper_before = live_paper.read_bytes()

    with pytest.raises(ValueError, match="paper JSON is unreadable"):
        restore_local_backup(
            source.path,
            project,
            database,
            application_version="test",
            confirm=True,
        )

    assert database.read_bytes() == database_before
    assert live_paper.read_bytes() == paper_before


@pytest.mark.parametrize(
    ("relative", "kind", "payload"),
    [
        (
            "paper/account.json",
            ACCOUNT_DOCUMENT_KIND,
            {
                "account_schema_version": 1,
                "mode": "simulation_only",
                "broker_connected": False,
                "portfolio": {},
                "executions": [],
                "audit_events": [],
            },
        ),
        (
            "paper/proposals/proposal.json",
            PROPOSAL_DOCUMENT_KIND,
            {
                "proposal_schema_version": 1,
                "proposal_id": "proposal-checksum",
                "targets": [],
            },
        ),
        (
            "paper/forward_trials/archive.json",
            FORWARD_DOCUMENT_KIND,
            {
                "forward_schema_version": 1,
                "trial_id": "archived-checksum",
                "status": "active",
                "proposals": [],
            },
        ),
    ],
)
def test_restore_rejects_hash_consistent_invalid_paper_envelopes_before_swap(
    tmp_path,
    relative,
    kind,
    payload,
) -> None:
    candidate = tmp_path / "candidate"
    candidate_database = candidate / "data" / "aios.duckdb"
    candidate_document = candidate / "data" / relative
    _create_valid_database(candidate_database)
    _write_checksum_envelope(candidate_document, kind=kind, payload=payload)
    raw = json.loads(candidate_document.read_text(encoding="utf-8"))
    raw["payload"]["tampered_after_checksum"] = True
    candidate_document.write_text(
        json.dumps(raw, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    source = create_local_backup(
        candidate,
        candidate_database,
        output=tmp_path / f"invalid-{candidate_document.stem}",
        application_version="test",
    )
    assert verify_local_backup(source.path).path == source.path

    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    live_paper = project / "data" / "paper" / "live.json"
    _create_valid_database(database)
    live_paper.parent.mkdir(parents=True)
    live_paper.write_text('{"live": true}', encoding="utf-8")
    database_before = database.read_bytes()
    paper_before = live_paper.read_bytes()

    with pytest.raises(ValueError, match="checksum mismatch"):
        restore_local_backup(
            source.path,
            project,
            database,
            application_version="test",
            confirm=True,
        )

    assert database.read_bytes() == database_before
    assert live_paper.read_bytes() == paper_before


def test_restore_rejects_active_forward_cross_reference_before_swap(tmp_path) -> None:
    candidate = tmp_path / "candidate"
    candidate_database = candidate / "data" / "aios.duckdb"
    account_path = candidate / "data" / "paper" / "us_qv_sandbox.json"
    proposal_path = candidate / "data" / "paper" / "proposals" / "proposal.json"
    trial_path = candidate / "data" / "paper" / "us_qv_forward_trial.json"
    store = Store(candidate_database)
    account = initialize_paper_account(account_path, store)
    store.close()
    proposal_payload = {
        "proposal_schema_version": 1,
        "proposal_id": "proposal-cross-reference",
        "account_id": account.payload["account_id"],
        "account_payload_sha256": account.payload_sha256,
        "decision_date": "2026-07-29",
        "generated_at": "2026-07-29T12:00:00Z",
        "status": "approved_for_supervised_simulation",
        "targets": [],
    }
    _write_checksum_envelope(
        proposal_path,
        kind=PROPOSAL_DOCUMENT_KIND,
        payload=proposal_payload,
    )
    proposal = read_paper_document(
        proposal_path,
        expected_kind=PROPOSAL_DOCUMENT_KIND,
    )
    forward_payload = {
        "forward_schema_version": 1,
        "trial_id": "trial-cross-reference",
        "status": "active",
        "account_id": account.payload["account_id"],
        "proposals": [
            {
                "proposal_id": proposal.payload["proposal_id"],
                "decision_date": proposal.payload["decision_date"],
                "generated_at": proposal.payload["generated_at"],
                "path": "data/paper/proposals/proposal.json",
                "payload_sha256": "0" * 64,
                "status": proposal.payload["status"],
            }
        ],
    }
    _write_checksum_envelope(
        trial_path,
        kind=FORWARD_DOCUMENT_KIND,
        payload=forward_payload,
    )
    source = create_local_backup(
        candidate,
        candidate_database,
        output=tmp_path / "cross-reference-source",
        application_version="test",
    )
    assert verify_local_backup(source.path).path == source.path

    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    live_paper = project / "data" / "paper" / "live.json"
    _create_valid_database(database)
    live_paper.parent.mkdir(parents=True)
    live_paper.write_text('{"live": true}', encoding="utf-8")
    database_before = database.read_bytes()
    paper_before = live_paper.read_bytes()

    with pytest.raises(ValueError, match="proposal identity is inconsistent"):
        restore_local_backup(
            source.path,
            project,
            database,
            application_version="test",
            confirm=True,
        )

    assert database.read_bytes() == database_before
    assert live_paper.read_bytes() == paper_before


def test_restore_rejects_hash_consistent_invalid_raw_replay_before_swap(
    tmp_path,
) -> None:
    candidate = tmp_path / "candidate"
    candidate_database = candidate / "data" / "aios.duckdb"
    store = Store(candidate_database)
    payload = b"Date,2 Yr,10 Yr,30 Yr\n07/24/2026,4.33,4.69,5.16\n"
    rows = parse_treasury_yield_curve_csv(payload)
    try:
        captured = capture_raw_snapshot(
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
            project_root=candidate,
        )
    finally:
        store.close()
    source = create_local_backup(
        candidate,
        candidate_database,
        output=tmp_path / "invalid-raw-source",
        application_version="test",
    )
    raw_relative = Path(captured.relative_path)
    backup_relative = Path("raw") / raw_relative.relative_to(Path("data/raw"))
    backed_up_raw = source.path / backup_relative
    backed_up_raw.write_bytes(b"not-a-gzip-payload")
    _refresh_manifest_file(source.path, backup_relative.as_posix())
    assert verify_local_backup(source.path).path == source.path

    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    live_paper = project / "data" / "paper" / "live.json"
    _create_valid_database(database)
    live_paper.parent.mkdir(parents=True)
    live_paper.write_text('{"live": true}', encoding="utf-8")
    database_before = database.read_bytes()
    paper_before = live_paper.read_bytes()

    with pytest.raises(ValueError, match="raw snapshot"):
        restore_local_backup(
            source.path,
            project,
            database,
            application_version="test",
            confirm=True,
        )

    assert database.read_bytes() == database_before
    assert live_paper.read_bytes() == paper_before
    assert not (project / captured.relative_path).exists()


def test_restore_keeps_newer_live_incident_history_instead_of_rolling_it_back(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    operations = project / "data" / "operations" / "alerts.sqlite3"
    _create_valid_database(database)
    operations.parent.mkdir(parents=True)
    ledger = AlertStore(operations)
    before = ledger.emit(_alert("before_backup"))
    scan = _anomaly_scan()
    case = ledger.record_anomaly_scan(scan, now=scan.source_boundary_at)[0]
    source = create_local_backup(
        project,
        database,
        operations_database_path=operations,
        output=project / "backups" / "source-with-incidents",
        application_version="test",
    )
    newer = Store(database)
    newer.execute("CREATE TABLE restore_incident_marker (value INTEGER)")
    newer.close()
    after = ledger.emit(_alert("after_backup"))

    restored = restore_local_backup(
        source.path,
        project,
        database,
        operations_database_path=operations,
        application_version="test",
        confirm=True,
    )

    current = AlertStore(operations)
    incidents = {row.code: row for row in current.list(unresolved_only=False)}
    assert incidents["before_backup"].incident_id == before.incident_id
    assert incidents["after_backup"].incident_id == after.incident_id
    stale = incidents["restore_requires_anomaly_rescan"]
    assert stale.state == "open"
    assert stale.payload["required_action"] == "aios anomaly-scan --record"
    assert current.anomaly_case(case.case_id) == case
    assert restored.operations_rescan_required is True
    assert restored.operations_incident_id == stale.incident_id


def test_restore_merges_raw_snapshots_without_deleting_newer_payloads(tmp_path) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    original = project / "data" / "raw" / "source" / "dataset" / "old.bin.gz"
    _create_valid_database(database)
    original.parent.mkdir(parents=True)
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

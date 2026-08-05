from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from subprocess import CompletedProcess

import pytest
from typer.testing import CliRunner

import aios.alerts as alerts_module
from aios import cli
from aios.alerts import (
    Alert,
    AlertSeverity,
    AlertStore,
    record_systemd_failure,
    record_systemd_recovery,
)


def _alert(**overrides) -> Alert:
    values = {
        "code": "refresh_partial",
        "severity": AlertSeverity.WARNING,
        "title": "Refresh completed with warnings",
        "body": "Two providers need review.",
        "dedup_key": "refresh:current:partial",
        "source_job": "refresh-us-current",
        "payload": {"failed": 2},
    }
    values.update(overrides)
    return Alert(**values)


def test_incident_lifecycle_deduplicates_reopens_and_retains_events(tmp_path) -> None:
    store = AlertStore(tmp_path / "operations" / "alerts.sqlite3")
    first_time = datetime(2026, 7, 22, 1, 0, tzinfo=UTC)

    opened = store.emit(_alert(), now=first_time)
    repeated = store.emit(_alert(), now=first_time + timedelta(minutes=1))
    acknowledged = store.acknowledge(
        opened.incident_id,
        actor="ops@example.test",
        note="Reviewed the exact current warning evidence.",
        expected_evidence_sha256=repeated.evidence_sha256,
        now=first_time + timedelta(minutes=2),
    )
    reopened = store.emit(
        _alert(severity=AlertSeverity.CRITICAL),
        now=first_time + timedelta(minutes=3),
    )
    resolved = store.resolve(
        opened.incident_id,
        actor="ops@example.test",
        note="The later refresh completed without warnings.",
        outcome="verified_recovery",
        expected_evidence_sha256=reopened.evidence_sha256,
        now=first_time + timedelta(minutes=4),
    )

    assert repeated.incident_id == opened.incident_id
    assert repeated.occurrence_count == 2
    assert acknowledged.state == "acknowledged"
    assert reopened.state == "open"
    assert reopened.severity == "critical"
    assert reopened.occurrence_count == 3
    assert resolved.state == "resolved"
    assert store.list(unresolved_only=True) == []
    assert [event["event_type"] for event in store.events(opened.incident_id)] == [
        "resolved",
        "reopened",
        "acknowledged",
        "repeated",
        "opened",
    ]


def test_incident_payload_redacts_secret_shaped_fields(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")

    incident = store.emit(
        _alert(
            payload={
                "api_key": "must-not-store",
                "nested": {"Authorization": "must-not-store", "safe": "retained"},
            }
        )
    )

    assert incident.payload == {
        "api_key": "[REDACTED]",
        "nested": {"Authorization": "[REDACTED]", "safe": "retained"},
    }
    assert oct(store.path.stat().st_mode & 0o777) == "0o600"


def test_job_lifecycle_recovers_abandoned_run_and_retains_latest_status(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    first_time = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
    abandoned = store.begin_job(
        "us-daily-refresh",
        "2026-07-23",
        now=first_time,
        owner_pid=999_999_999,
        owner_boot_id="old-boot",
    )

    restarted = store.begin_job(
        "us-daily-refresh",
        "2026-07-23",
        now=first_time + timedelta(minutes=10),
        owner_boot_id="new-boot",
    )
    finished = store.finish_job(
        restarted.run.run_id,
        state="success",
        detail="All exact-date readiness gates passed.",
        payload={"certified_through": "2026-07-23", "api_key": "never-store"},
        now=first_time + timedelta(minutes=20),
    )

    assert restarted.interrupted_run_ids == (abandoned.run.run_id,)
    assert finished.state == "success"
    assert finished.payload == {
        "api_key": "[REDACTED]",
        "certified_through": "2026-07-23",
    }
    assert store.latest_job("us-daily-refresh") == finished


def test_job_lifecycle_refuses_a_second_live_owner(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    store.begin_job(
        "us-daily-refresh",
        "2026-07-23",
        owner_boot_id="same-boot",
    )

    with pytest.raises(RuntimeError, match="already running"):
        store.begin_job(
            "us-daily-refresh",
            "2026-07-23",
            owner_boot_id="same-boot",
        )


def test_systemd_failure_capture_is_bounded_and_proofless_recovery_is_retained(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")

    def runner(command, **_kwargs):
        assert command[:4] == [
            "systemctl",
            "--user",
            "show",
            "aios-us-current.service",
        ]
        return CompletedProcess(
            command,
            0,
            "Id=aios-us-current.service\nResult=exit-code\nExecMainStatus=1\n",
            "",
        )

    incident = record_systemd_failure(
        "aios-us-current.service", store=store, runner=runner
    )
    recovered = record_systemd_recovery("aios-us-current.service", store=store)

    assert incident.severity == "critical"
    assert incident.body.endswith("result exit-code and exit status 1.")
    assert incident.payload["systemd"]["ExecMainStatus"] == "1"
    assert recovered is None
    current = store.get(incident.incident_id)
    assert current.state == "open"
    assert current.operationally_blocking is True


def test_systemd_failure_capture_refuses_unmanaged_units(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")

    with pytest.raises(ValueError, match="unmanaged service"):
        record_systemd_failure("ssh.service", store=store)


def test_incident_store_refuses_symbolic_link_database(tmp_path) -> None:
    target = tmp_path / "target.sqlite3"
    target.touch()
    link = tmp_path / "alerts.sqlite3"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symbolic link"):
        AlertStore(link)


def test_incident_read_only_store_preserves_exact_database_bytes(tmp_path) -> None:
    path = tmp_path / "alerts.sqlite3"
    writable = AlertStore(path)
    incident = writable.emit(_alert())
    checkpoint = sqlite3.connect(path)
    try:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        checkpoint.execute("PRAGMA journal_mode = DELETE")
    finally:
        checkpoint.close()
    before = path.read_bytes()
    sidecars_before = {
        suffix: candidate.read_bytes()
        for suffix in ("-wal", "-shm")
        if (candidate := path.with_name(f"{path.name}{suffix}")).exists()
    }

    read_only = AlertStore(path, read_only=True)

    for _ in range(3):
        assert read_only.get(incident.incident_id) == incident
        assert read_only.list(unresolved_only=True) == [incident]
        assert [
            event["event_type"]
            for event in read_only.events(incident.incident_id)
        ] == ["opened"]
        assert read_only.incident_summary()["unresolved"] == 1
    assert path.read_bytes() == before
    assert {
        suffix: candidate.read_bytes()
        for suffix in ("-wal", "-shm")
        if (candidate := path.with_name(f"{path.name}{suffix}")).exists()
    } == sidecars_before


def test_incident_read_only_store_refuses_uncheckpointed_wal(tmp_path) -> None:
    path = tmp_path / "alerts.sqlite3"
    writable = AlertStore(path)
    writable.emit(_alert())
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            "UPDATE incidents SET body = body || ' [test]' "
            "WHERE code = 'refresh_partial'"
        )
        connection.commit()
        assert path.with_name(f"{path.name}-wal").stat().st_size > 0

        with pytest.raises(RuntimeError, match="uncheckpointed WAL"):
            AlertStore(path, read_only=True)
    finally:
        connection.close()


def test_existing_read_only_store_refuses_writer_wal_started_between_reads(
    tmp_path,
) -> None:
    path = tmp_path / "alerts.sqlite3"
    writable = AlertStore(path)
    incident = writable.emit(_alert())
    with sqlite3.connect(path) as checkpoint:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        checkpoint.execute("PRAGMA journal_mode = DELETE")
    read_only = AlertStore(path, read_only=True)
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute(
            "UPDATE incidents SET body = body || ' [concurrent]' "
            "WHERE incident_id = ?",
            (incident.incident_id,),
        )
        writer.commit()

        with pytest.raises(RuntimeError, match="uncheckpointed WAL"):
            read_only.get(incident.incident_id)
    finally:
        writer.close()


def test_read_only_query_refuses_main_file_change_after_immutable_read(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "alerts.sqlite3"
    writable = AlertStore(path)
    incident = writable.emit(_alert())
    with sqlite3.connect(path) as checkpoint:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        checkpoint.execute("PRAGMA journal_mode = DELETE")
    read_only = AlertStore(path, read_only=True)
    real_identity = alerts_module._stable_read_only_database_identity
    calls = 0

    def change_after_read(candidate):
        nonlocal calls
        calls += 1
        if calls == 2:
            with sqlite3.connect(path) as writer:
                writer.execute(
                    "UPDATE incidents SET body = body || ' [concurrent]' "
                    "WHERE incident_id = ?",
                    (incident.incident_id,),
                )
        return real_identity(candidate)

    monkeypatch.setattr(
        alerts_module,
        "_stable_read_only_database_identity",
        change_after_read,
    )

    with pytest.raises(RuntimeError, match="changed during a read-only query"):
        read_only.get(incident.incident_id)


def test_incident_read_only_store_refuses_schema_migration(tmp_path) -> None:
    path = tmp_path / "alerts.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA user_version = 3")
        connection.commit()
    finally:
        connection.close()
    before = path.read_bytes()

    with pytest.raises(ValueError, match="schema 3 does not match required schema 7"):
        AlertStore(path, read_only=True)

    assert path.read_bytes() == before


def test_writable_store_migrates_v4_to_v6_without_changing_existing_incidents(
    tmp_path,
) -> None:
    path = tmp_path / "alerts.sqlite3"
    original_store = AlertStore(path)
    original = original_store.emit(_alert())
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE anomaly_case_events")
        connection.execute("DROP TABLE anomaly_cases")
        connection.execute("DROP TABLE anomaly_scans")
        connection.execute("PRAGMA user_version = 4")
        connection.commit()
    finally:
        connection.close()

    migrated = AlertStore(path)

    assert migrated.get(original.incident_id) == original
    assert migrated.anomaly_cases() == []
    verification = sqlite3.connect(path)
    try:
        assert verification.execute("PRAGMA user_version").fetchone()[0] == 7
        tables = {
            row[0]
            for row in verification.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name LIKE 'anomaly_%'
                """
            )
        }
        assert tables == {
            "anomaly_case_events",
            "anomaly_cases",
            "anomaly_scans",
        }
        assert verification.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        verification.close()


def test_incident_references_require_a_unique_prefix_and_same_second_order_is_stable(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    moment = datetime(2026, 7, 22, 1, 0, tzinfo=UTC)
    first = store.emit(_alert(), now=moment)
    store.emit(_alert(dedup_key="refresh:other", code="other"), now=moment)
    store.resolve(
        first.incident_id[:12],
        actor="ops@example.test",
        note="Bounded reference-resolution test.",
        outcome="false_positive",
        expected_evidence_sha256=first.evidence_sha256,
        now=moment,
    )

    assert store.get(first.incident_id[:12]).state == "resolved"
    assert [event["event_type"] for event in store.events(first.incident_id[:12])] == [
        "resolved",
        "opened",
    ]
    with pytest.raises(ValueError, match="ambiguous"):
        store.get("inc-")


def test_alert_cli_tests_lists_and_inspects_local_history(monkeypatch, tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    modes: list[dict] = []

    def get_store(**kwargs):
        modes.append(kwargs)
        return store

    monkeypatch.setattr(alerts_module, "get_alert_store", get_store)

    test_result = CliRunner().invoke(cli.app, ["alert-test"])
    unresolved = store.emit(_alert())
    list_result = CliRunner().invoke(cli.app, ["alerts", "--unresolved"])
    incident_ref = unresolved.incident_id[:12]
    show_result = CliRunner().invoke(cli.app, ["alert-show", incident_ref])
    ack_result = CliRunner().invoke(
        cli.app,
        [
            "alert-ack",
            incident_ref,
            "--actor",
            "ops@example.test",
            "--note",
            "Reviewed the exact current warning evidence.",
            "--evidence-sha256",
            unresolved.evidence_sha256,
        ],
    )

    assert test_result.exit_code == 0
    assert "opened, logged, and resolved" in test_result.output
    assert list_result.exit_code == 0
    assert "warning" in list_result.output
    assert show_result.exit_code == 0
    assert "Two providers need review" in show_result.output
    assert ack_result.exit_code == 0
    assert store.get(unresolved.incident_id).state == "acknowledged"
    assert modes == [
        {},
        {"read_only": True},
        {"read_only": True},
        {},
    ]

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

import aios.alerts as alerts_module
from aios import cli
from aios.alerts import Alert, AlertSeverity, AlertStore

BASE_TIME = datetime(2026, 7, 30, 1, 0, tzinfo=UTC)


def _alert() -> Alert:
    return Alert(
        code="scheduler_runtime_unverified",
        severity=AlertSeverity.WARNING,
        title="Local scheduler runtime could not be verified",
        body="Installed timer files are visible, but runtime proof is unavailable.",
        dedup_key="scheduler:runtime-unverified",
        source_job="aios scheduler-status",
        payload={"timers": ["aios-us-current.timer"]},
    )


def test_operator_actions_use_current_evidence_cas_and_append_audit_proof(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    opened = store.emit(_alert(), now=BASE_TIME)

    with pytest.raises(ValueError, match="evidence changed"):
        store.acknowledge(
            opened.incident_id,
            actor="ops@example.test",
            note="Review the exact scheduler evidence.",
            expected_evidence_sha256="0" * 64,
            now=BASE_TIME + timedelta(minutes=1),
        )
    assert store.get(opened.incident_id).state == "open"
    assert [event["event_type"] for event in store.events(opened.incident_id)] == [
        "opened"
    ]

    acknowledged = store.acknowledge(
        opened.incident_id,
        actor="ops@example.test",
        note="Review the exact scheduler evidence.",
        expected_evidence_sha256=opened.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=1),
    )
    assert acknowledged.state == "acknowledged"
    assert acknowledged.evidence_sha256 != opened.evidence_sha256
    acknowledgement_event = store.events(opened.incident_id)[0]
    assert acknowledgement_event["event_type"] == "acknowledged"
    assert acknowledgement_event["actor"] == "ops@example.test"
    assert acknowledgement_event["note"] == "Review the exact scheduler evidence."
    assert (
        acknowledgement_event["expected_evidence_sha256"]
        == opened.evidence_sha256
    )
    assert (
        acknowledgement_event["resulting_evidence_sha256"]
        == acknowledged.evidence_sha256
    )
    assert len(acknowledgement_event["proof_sha256"]) == 64

    with pytest.raises(ValueError, match="evidence changed"):
        store.resolve(
            opened.incident_id,
            actor="ops@example.test",
            note="A later bounded check verified recovery.",
            outcome="verified_recovery",
            expected_evidence_sha256=opened.evidence_sha256,
            now=BASE_TIME + timedelta(minutes=2),
        )
    assert store.get(opened.incident_id).state == "acknowledged"

    resolved = store.resolve(
        opened.incident_id,
        actor="ops@example.test",
        note="A later bounded check verified recovery.",
        outcome="verified_recovery",
        expected_evidence_sha256=acknowledged.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=2),
    )
    assert resolved.state == "resolved"
    assert resolved.evidence_sha256 != acknowledged.evidence_sha256
    resolution_event = store.events(opened.incident_id)[0]
    assert resolution_event["event_type"] == "resolved"
    assert resolution_event["actor"] == "ops@example.test"
    assert resolution_event["resolution_outcome"] == "verified_recovery"
    assert (
        resolution_event["resulting_evidence_sha256"]
        == resolved.evidence_sha256
    )

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE incident_events SET payload_json = '{}' "
                "WHERE event_id = ?",
                (resolution_event["event_id"],),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM incident_events WHERE event_id = ?",
                (resolution_event["event_id"],),
            )


def test_v6_to_v7_migration_preserves_incident_and_event_rows(tmp_path) -> None:
    path = tmp_path / "alerts.sqlite3"
    original_store = AlertStore(path)
    original = original_store.emit(_alert(), now=BASE_TIME)

    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("DROP TRIGGER incident_events_no_update")
        connection.execute("DROP TRIGGER incident_events_no_delete")
        connection.execute(
            "DROP TRIGGER incident_events_resolution_proof_required"
        )
        connection.execute("PRAGMA user_version = 6")
        incident_rows_before = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM incidents ORDER BY incident_id"
            ).fetchall()
        ]
        event_rows_before = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM incident_events ORDER BY rowid"
            ).fetchall()
        ]

    migrated_store = AlertStore(path)
    assert migrated_store.get(original.incident_id) == original

    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM incidents ORDER BY incident_id"
            ).fetchall()
        ] == incident_rows_before
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM incident_events ORDER BY rowid"
            ).fetchall()
        ] == event_rows_before
        triggers = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_schema
                WHERE type = 'trigger' AND name LIKE 'incident_events_%'
                """
            ).fetchall()
        }
    assert triggers == {
        "incident_events_no_delete",
        "incident_events_no_update",
        "incident_events_resolution_proof_required",
    }


def test_v6_legacy_resolution_migrates_unchanged_and_remains_blocking(
    tmp_path,
) -> None:
    path = tmp_path / "alerts.sqlite3"
    store = AlertStore(path)
    opened = store.emit(_alert(), now=BASE_TIME)
    resolved_at = (
        (BASE_TIME + timedelta(minutes=1))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    with sqlite3.connect(path) as connection:
        for trigger in (
            "incident_events_no_update",
            "incident_events_no_delete",
            "incident_events_resolution_proof_required",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute(
            "UPDATE incidents SET state = 'resolved', resolved_at = ? "
            "WHERE incident_id = ?",
            (resolved_at, opened.incident_id),
        )
        connection.execute(
            """
            INSERT INTO incident_events (
                event_id, incident_id, event_type, created_at, payload_json
            ) VALUES ('evt-legacy-resolution', ?, 'resolved', ?, '{}')
            """,
            (opened.incident_id, resolved_at),
        )
        connection.execute("PRAGMA user_version = 6")
        rows_before = connection.execute(
            "SELECT * FROM incidents ORDER BY incident_id"
        ).fetchall()
        events_before = connection.execute(
            "SELECT * FROM incident_events ORDER BY rowid"
        ).fetchall()

    migrated = AlertStore(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT * FROM incidents ORDER BY incident_id"
        ).fetchall() == rows_before
        assert connection.execute(
            "SELECT * FROM incident_events ORDER BY rowid"
        ).fetchall() == events_before
    current = migrated.get(opened.incident_id)
    assert current.state == "resolved"
    assert current.resolution_proof_status == "legacy_unproven"
    assert current.operationally_blocking is True
    assert migrated.incident_summary()["unproven_resolved"] == 1


def test_unreserved_producer_payload_cannot_be_mistaken_for_action_proof(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    alert = _alert()
    incident = store.emit(
        Alert(
            code=alert.code,
            severity=alert.severity,
            title=alert.title,
            body=alert.body,
            dedup_key=alert.dedup_key,
            source_job=alert.source_job,
            payload={"incident_action": {"producer_field": True}},
        ),
        now=BASE_TIME,
    )

    assert store.events(incident.incident_id)[0]["payload"] == {
        "incident_action": {"producer_field": True}
    }


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("--actor", "   ", "incident actor is required"),
        ("--note", "   ", "incident audit note is required"),
        ("--evidence-sha256", "not-a-sha", "64-character"),
    ],
)
@pytest.mark.parametrize(
    "command_name",
    ["alert-ack", "alert-resolve", "alert-attest-resolution"],
)
def test_alert_actions_reject_invalid_mutation_arguments_before_store_open(
    monkeypatch,
    option: str,
    value: str,
    message: str,
    command_name: str,
) -> None:
    opened = 0

    def fail_if_opened(**_kwargs):
        nonlocal opened
        opened += 1
        raise AssertionError("operations store must not open")

    monkeypatch.setattr(alerts_module, "get_alert_store", fail_if_opened)
    arguments = {
        "--actor": "ops@example.test",
        "--note": "Review the current scheduler evidence.",
        "--evidence-sha256": "a" * 64,
    }
    arguments[option] = value
    command = [command_name, "inc-example"]
    for name, item in arguments.items():
        command.extend((name, item))
    if command_name in {"alert-resolve", "alert-attest-resolution"}:
        command.extend(("--outcome", "verified_recovery"))

    result = CliRunner().invoke(cli.app, command)

    assert result.exit_code == 1
    assert message in result.output
    assert opened == 0


@pytest.mark.parametrize(
    "command_name",
    ["alert-resolve", "alert-attest-resolution"],
)
def test_alert_resolve_rejects_invalid_outcome_before_store_open(
    monkeypatch,
    command_name,
) -> None:
    opened = 0

    def fail_if_opened(**_kwargs):
        nonlocal opened
        opened += 1
        raise AssertionError("operations store must not open")

    monkeypatch.setattr(alerts_module, "get_alert_store", fail_if_opened)

    result = CliRunner().invoke(
        cli.app,
        [
            command_name,
            "inc-example",
            "--actor",
            "ops@example.test",
            "--note",
            "Review the exact current evidence.",
            "--outcome",
            "accepted_risk",
            "--evidence-sha256",
            "a" * 64,
        ],
    )

    assert result.exit_code == 1
    assert "incident outcome must be" in result.output
    assert opened == 0

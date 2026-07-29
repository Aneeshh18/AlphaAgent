from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from typer.testing import CliRunner

import aios.alerts as alerts_module
from aios import cli
from aios.alerts import (
    ALERT_SCHEMA_VERSION,
    NOTIFICATION_MAX_ATTEMPTS,
    Alert,
    AlertSeverity,
    AlertStore,
    NotificationRequest,
)
from aios.notifications import (
    LocalTestChannel,
    NotificationChannelError,
    dispatch_notifications,
)
from aios.operations import create_local_backup

BASE_TIME = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)


def _alert(**overrides) -> Alert:
    values = {
        "code": "refresh_failed",
        "severity": AlertSeverity.WARNING,
        "title": "Current refresh failed",
        "body": "The latest reviewed refresh needs attention.",
        "dedup_key": "refresh:current:failure",
        "source_job": "refresh-us-current",
        "payload": {"area": "prices"},
    }
    values.update(overrides)
    return Alert(**values)


def _request(key: str = "test:notification") -> NotificationRequest:
    return NotificationRequest(
        idempotency_key=key,
        event_type="test",
        severity=AlertSeverity.INFO,
        title="Notification path test",
        body="No external message is sent.",
        source_job="test",
        payload={"test": True},
    )


def test_v2_migration_is_atomic_preserves_history_and_creates_no_backlog(tmp_path) -> None:
    path = tmp_path / "alerts.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE incidents (
                incident_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL UNIQUE,
                code TEXT NOT NULL,
                severity TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                source_job TEXT NOT NULL,
                state TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                acknowledged_at TEXT,
                resolved_at TEXT
            );
            INSERT INTO incidents VALUES (
                'inc-existing', 'existing:key', 'existing', 'warning',
                'Existing warning', 'Retain this incident', 'old-job', 'open',
                '2026-07-24T00:00:00Z', '2026-07-24T00:00:00Z', 1, '{}',
                NULL, NULL
            );
            INSERT INTO incidents VALUES (
                'inc-local-test', 'test:local-alert-path', 'local_alert_test', 'info',
                'Old local test', 'Retain history without notifying', 'aios alert-test',
                'resolved', '2026-07-23T00:00:00Z', '2026-07-23T00:00:01Z',
                1, '{}', NULL, '2026-07-23T00:00:01Z'
            );
            PRAGMA user_version = 2;
            """
        )

    store = AlertStore(path)

    assert store.get("inc-existing").state == "open"
    assert store.get("inc-existing").notifications_enabled is True
    assert store.get("inc-local-test").notifications_enabled is False
    assert store.notification_summary() == {
        "dead_letter": 0,
        "delivered": 0,
        "held": 0,
        "leased": 0,
        "pending": 0,
    }
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            ALERT_SCHEMA_VERSION
        )


def test_unknown_future_operations_schema_fails_closed(tmp_path) -> None:
    path = tmp_path / "alerts.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version = {ALERT_SCHEMA_VERSION + 1}")

    with pytest.raises(ValueError, match="newer than this AIOS"):
        AlertStore(path)


def test_incident_lifecycle_enqueues_only_actionable_transitions(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")

    incident = store.emit(_alert(), now=BASE_TIME)
    store.emit(_alert(), now=BASE_TIME + timedelta(minutes=1))
    store.emit(
        _alert(severity=AlertSeverity.CRITICAL),
        now=BASE_TIME + timedelta(minutes=2),
    )
    store.acknowledge(incident.incident_id, now=BASE_TIME + timedelta(minutes=3))
    store.emit(_alert(), now=BASE_TIME + timedelta(minutes=4))
    store.resolve(incident.incident_id, now=BASE_TIME + timedelta(minutes=5))

    messages = sorted(
        store.list_notifications(limit=20),
        key=lambda message: message.created_at,
    )
    assert [message.event_type for message in messages] == [
        "opened",
        "escalated",
        "reopened",
        "resolved",
    ]
    assert all(message.state == "held" for message in messages)
    assert len({message.source_event_id for message in messages}) == 4
    assert store.claim_notifications("local-test") == []


def test_incident_event_and_outbox_roll_back_together(monkeypatch, tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")

    def fail_enqueue(*_args, **_kwargs):
        raise ValueError("forced outbox failure")

    monkeypatch.setattr(
        AlertStore,
        "_insert_outbox_message",
        staticmethod(fail_enqueue),
    )
    with pytest.raises(ValueError, match="forced outbox failure"):
        store.emit(_alert(), now=BASE_TIME)

    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM incident_events").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0] == 0


def test_local_only_incident_policy_persists_through_resolution(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    incident = store.emit(_alert(notify=False), now=BASE_TIME)
    store.emit(_alert(), now=BASE_TIME + timedelta(minutes=1))
    resolved = store.resolve(incident.incident_id, now=BASE_TIME + timedelta(minutes=2))

    assert resolved.notifications_enabled is False
    assert store.list_notifications() == []


def test_incident_summary_is_exact_beyond_bounded_display_list(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    for index in range(105):
        store.emit(
            _alert(
                dedup_key=f"bulk:{index}",
                severity=(
                    AlertSeverity.CRITICAL
                    if index < 3
                    else AlertSeverity.WARNING
                ),
                notify=False,
            ),
            now=BASE_TIME,
        )

    assert len(store.list(limit=100)) == 100
    assert store.incident_summary()["unresolved"] == 105
    assert store.incident_summary()["critical_unresolved"] == 3


def test_idempotency_returns_exact_match_and_rejects_changed_content(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    original = store.enqueue_notification(_request(), held=False, now=BASE_TIME)
    repeated = store.enqueue_notification(_request(), held=False, now=BASE_TIME)

    assert repeated == original
    with pytest.raises(ValueError, match="idempotency key conflicts"):
        store.enqueue_notification(
            NotificationRequest(
                **{**_request().__dict__, "title": "Changed title"},
            ),
            held=False,
            now=BASE_TIME,
        )


def test_two_workers_cannot_claim_the_same_message(tmp_path) -> None:
    path = tmp_path / "alerts.sqlite3"
    store = AlertStore(path)
    message = store.enqueue_notification(_request(), held=False, now=BASE_TIME)
    barrier = Barrier(2)

    def claim() -> list[str]:
        worker = AlertStore(path)
        barrier.wait()
        return [
            item.message.notification_id
            for item in worker.claim_notifications(
                "local-test",
                route_alias="test",
                now=BASE_TIME,
            )
        ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: claim(), range(2)))

    assert sorted(results, key=len) == [[], [message.notification_id]]


def test_expired_lease_is_terminal_because_external_outcome_is_unknown(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    message = store.enqueue_notification(_request(), held=False, now=BASE_TIME)
    first = store.claim_notifications(
        "local-test",
        lease_seconds=30,
        now=BASE_TIME,
    )[0]

    second = store.claim_notifications(
        "local-test",
        lease_seconds=30,
        now=BASE_TIME + timedelta(seconds=31),
    )

    assert second == []
    assert store.get_notification(message.notification_id).state == "dead_letter"
    attempts = store.notification_deliveries(message.notification_id)
    assert [attempt.state for attempt in attempts] == ["ambiguous"]
    assert attempts[0].error_type == "lease_expired_outcome_unknown"
    with pytest.raises(ValueError, match="missing or already finished"):
        store.complete_notification_delivery(
            first.delivery.delivery_id,
            first.lease_token,
            succeeded=True,
            now=BASE_TIME + timedelta(seconds=31),
        )


def test_retry_backoff_is_bounded_and_dead_letters_after_five_attempts(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    message = store.enqueue_notification(_request(), held=False, now=BASE_TIME)
    current = BASE_TIME

    for attempt_number in range(1, NOTIFICATION_MAX_ATTEMPTS + 1):
        claim = store.claim_notifications("test", now=current)[0]
        result = store.complete_notification_delivery(
            claim.delivery.delivery_id,
            claim.lease_token,
            succeeded=False,
            error_type="TemporaryProviderError",
            failure_state="retryable_failure",
            now=current,
        )
        if attempt_number < NOTIFICATION_MAX_ATTEMPTS:
            assert result.state == "pending"
            retry_at = datetime.fromisoformat(result.available_at.replace("Z", "+00:00"))
            assert store.claim_notifications(
                "test",
                now=retry_at - timedelta(seconds=1),
            ) == []
            current = retry_at
        else:
            assert result.state == "dead_letter"

    attempts = store.notification_deliveries(message.notification_id)
    assert len(attempts) == NOTIFICATION_MAX_ATTEMPTS
    assert all(attempt.state == "retryable_failure" for attempt in attempts)


def test_permanent_failure_is_not_retried_and_ambiguous_is_auditable(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    permanent = store.enqueue_notification(
        _request("test:permanent"),
        held=False,
        now=BASE_TIME,
    )
    claim = store.claim_notifications(
        "test",
        notification_id=permanent.notification_id,
        now=BASE_TIME,
    )[0]
    result = store.complete_notification_delivery(
        claim.delivery.delivery_id,
        claim.lease_token,
        succeeded=False,
        error_type="InvalidDestination",
        failure_state="permanent_failure",
        now=BASE_TIME,
    )
    assert result.state == "dead_letter"

    class AmbiguousChannel:
        name = "fake"
        route_alias = "reviewed-test-route"

        def send(self, _message):
            raise NotificationChannelError(
                "ProviderTimeoutAfterSend",
                failure_state="ambiguous",
            )

    ambiguous = store.enqueue_notification(
        _request("test:ambiguous"),
        held=False,
        now=BASE_TIME,
    )
    summary = dispatch_notifications(
        store,
        AmbiguousChannel(),
        notification_id=ambiguous.notification_id,
        now=BASE_TIME,
    )

    assert summary.failed == 1
    assert store.get_notification(ambiguous.notification_id).state == "dead_letter"
    assert store.notification_deliveries(ambiguous.notification_id)[0].state == (
        "ambiguous"
    )


def test_unclassified_channel_exception_is_terminal_and_secret_free(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    message = store.enqueue_notification(
        _request("test:unclassified"),
        held=False,
        now=BASE_TIME,
    )

    class BrokenChannel:
        name = "broken-test"
        route_alias = "test"

        def send(self, _message):
            raise RuntimeError("provider text that must not be persisted")

    summary = dispatch_notifications(
        store,
        BrokenChannel(),
        notification_id=message.notification_id,
        now=BASE_TIME,
    )

    assert summary.dead_lettered == 1
    completed = store.get_notification(message.notification_id)
    assert completed.state == "dead_letter"
    assert completed.last_error_type == "internal_channel_error"
    delivery = store.notification_deliveries(message.notification_id)[0]
    assert delivery.error_type == "internal_channel_error"
    assert delivery.provider_response == {
        "delivery_phase": "channel_send",
        "provider_state": "internal_error",
    }


def test_provider_metadata_is_allowlisted_and_payload_secrets_are_redacted(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    request = NotificationRequest(
        **{
            **_request().__dict__,
            "payload": {
                "api_key": "never-store",
                "nested": {"Authorization": "never-store", "safe": "retained"},
            },
        }
    )
    message = store.enqueue_notification(request, held=False, now=BASE_TIME)
    claim = store.claim_notifications("test", now=BASE_TIME)[0]

    assert message.payload == {
        "api_key": "[REDACTED]",
        "nested": {"Authorization": "[REDACTED]", "safe": "retained"},
    }
    with pytest.raises(ValueError, match="unsupported fields"):
        store.complete_notification_delivery(
            claim.delivery.delivery_id,
            claim.lease_token,
            succeeded=True,
            provider_response={"raw_body": "must-not-store"},
            now=BASE_TIME,
        )


def test_local_test_channel_delivers_without_network_and_backup_keeps_history(
    tmp_path,
) -> None:
    project = tmp_path / "project"
    database = project / "data" / "aios.duckdb"
    operations = project / "data" / "operations" / "alerts.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"test database")
    store = AlertStore(operations)
    message = store.enqueue_notification(_request(), held=False, now=BASE_TIME)

    dispatched = dispatch_notifications(
        store,
        LocalTestChannel(),
        notification_id=message.notification_id,
        now=BASE_TIME,
    )
    backup = create_local_backup(
        project,
        database,
        operations_database_path=operations,
        application_version="test",
        now=BASE_TIME,
    )

    assert dispatched.succeeded == 1
    assert store.get_notification(message.notification_id).state == "delivered"
    with sqlite3.connect(
        backup.path / "operations" / "alerts.sqlite3",
    ) as connection:
        assert connection.execute(
            "SELECT state FROM notification_outbox WHERE notification_id = ?",
            (message.notification_id,),
        ).fetchone() == ("delivered",)
        assert connection.execute(
            "SELECT state FROM notification_deliveries WHERE notification_id = ?",
            (message.notification_id,),
        ).fetchone() == ("succeeded",)


def test_notification_cli_is_plain_language_and_alert_test_stays_local(
    monkeypatch,
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    incident = store.emit(_alert(), now=BASE_TIME)
    monkeypatch.setattr(alerts_module, "get_alert_store", lambda **_kwargs: store)

    listing = CliRunner().invoke(cli.app, ["notifications"])
    message = store.list_notifications()[0]
    detail = CliRunner().invoke(
        cli.app,
        ["notification-show", message.notification_id[:20]],
    )
    local_test = CliRunner().invoke(cli.app, ["notification-test"])
    before_alert_test = store.notification_summary()
    alert_test = CliRunner().invoke(cli.app, ["alert-test"])

    assert incident.notifications_enabled is True
    assert listing.exit_code == 0
    assert "External email delivery: off" in listing.output
    assert "Held: 1" in listing.output
    assert detail.exit_code == 0
    assert "No delivery has been attempted" in detail.output
    assert local_test.exit_code == 0
    assert "No network" in local_test.output
    assert "broker action occurred" in local_test.output
    assert alert_test.exit_code == 0
    assert store.notification_summary() == before_alert_test

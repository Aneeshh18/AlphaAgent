from __future__ import annotations

import hashlib
import smtplib
import sqlite3
import ssl
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

import aios.alerts as alerts_module
import aios.notifications as notifications_module
import aios.scheduler as scheduler_module
from aios import cli
from aios.alerts import (
    Alert,
    AlertSeverity,
    AlertStore,
    NotificationRequest,
)
from aios.config import Settings
from aios.notifications import (
    EMAIL_CHANNEL_NAME,
    EMAIL_ROUTE_ALIAS,
    DeliveryOutcome,
    NotificationChannelError,
    SmtpEmailChannel,
    SmtpEmailConfig,
    dispatch_email_notifications,
    dispatch_notifications,
    smtp_email_config,
)

BASE_TIME = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64


def _alert(key: str) -> Alert:
    return Alert(
        code="scheduled_refresh_failed",
        severity=AlertSeverity.WARNING,
        title="Scheduled refresh failed",
        body="The operating workflow needs review.",
        dedup_key=key,
        source_job="refresh-us-daily",
    )


def _config(**overrides) -> SmtpEmailConfig:
    values = {
        "host": "smtp.example.com",
        "port": 587,
        "security": "starttls",
        "username": "sender@example.com",
        "password": "app-specific-secret",
        "from_address": "sender@example.com",
        "to_address": "owner@example.com",
        "timeout_seconds": 15.0,
    }
    values.update(overrides)
    return SmtpEmailConfig(**values)


def _test_request(config: SmtpEmailConfig) -> NotificationRequest:
    return NotificationRequest(
        idempotency_key="test:smtp:one",
        event_type="test",
        severity=AlertSeverity.INFO,
        title="SMTP test",
        body="External receipt proof.",
        source_job="test",
        payload={
            "config_fingerprint": config.fingerprint,
            "email_test": True,
        },
    )


class _FakeSmtp:
    def __init__(self, *_args, **kwargs) -> None:
        self.kwargs = kwargs
        self.calls: list[object] = []
        self.email = None

    def __enter__(self):
        self.calls.append("enter")
        return self

    def __exit__(self, *_args):
        self.calls.append("exit")
        return False

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def starttls(self, *, context) -> None:
        assert context is not None
        self.calls.append("starttls")

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", username, password))

    def send_message(self, email):
        self.calls.append("send")
        self.email = email
        return {}


def test_smtp_config_requires_encrypted_complete_single_recipient_settings() -> None:
    incomplete = Settings(_env_file=None)
    with pytest.raises(ValueError) as exc_info:
        smtp_email_config(incomplete)
    detail = str(exc_info.value)
    assert "SMTP_PASSWORD" in detail
    assert "ALERT_EMAIL_TO" in detail

    configured = Settings(
        _env_file=None,
        smtp_host="smtp.example.com",
        smtp_port="465",
        smtp_security="tls",
        smtp_username="sender@example.com",
        smtp_password=SecretStr("never-print-this"),
        alert_email_from="sender@example.com",
        alert_email_to="owner@example.com",
    )
    route = smtp_email_config(configured)
    assert route.security == "tls"
    assert len(route.fingerprint) == 64
    assert "never-print-this" not in repr(route.fingerprint)

    with pytest.raises(ValueError, match="starttls or tls"):
        _config(security="plain")
    with pytest.raises(ValueError, match="exactly one"):
        _config(to_address="one@example.com,two@example.com")
    with pytest.raises(ValueError, match="line breaks"):
        _config(username="sender@example.com\nBcc: bad@example.com")

    invalid_optional_email = Settings(
        _env_file=None,
        smtp_host="smtp.example.com",
        smtp_port="not-a-port",
        smtp_username="sender@example.com",
        smtp_password=SecretStr("secret"),
        alert_email_from="sender@example.com",
        alert_email_to="owner@example.com",
    )
    with pytest.raises(ValueError, match="SMTP_PORT must be an integer"):
        smtp_email_config(invalid_optional_email)


def test_smtp_channel_uses_starttls_auth_and_stable_secret_free_message(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    notification = store.enqueue_notification(
        _test_request(_config()),
        held=False,
        now=BASE_TIME,
    )
    fake = _FakeSmtp()
    channel = SmtpEmailChannel(
        _config(),
        smtp_factory=lambda *_args, **_kwargs: fake,
        ssl_context_factory=lambda: object(),
    )

    outcome = channel.send(notification)

    assert fake.calls == [
        "enter",
        "ehlo",
        "starttls",
        "ehlo",
        ("login", "sender@example.com", "app-specific-secret"),
        "send",
        "exit",
    ]
    stable_id = hashlib.sha256(
        f"{EMAIL_ROUTE_ALIAS}:{notification.idempotency_key}".encode()
    ).hexdigest()[:40]
    assert fake.email["Message-ID"] == f"<aios-{stable_id}@example.com>"
    assert fake.email["X-AIOS-Notification-ID"] == notification.notification_id
    assert "app-specific-secret" not in fake.email.as_string()
    assert "not an investment recommendation" in fake.email.get_content()
    assert outcome.provider_response["external_delivery"] is True


def test_smtp_channel_supports_implicit_tls_without_plaintext_upgrade(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    notification = store.enqueue_notification(
        _test_request(_config(security="tls", port=465)),
        held=False,
        now=BASE_TIME,
    )
    fake = _FakeSmtp()
    context = object()
    channel = SmtpEmailChannel(
        _config(security="tls", port=465),
        smtp_factory=lambda *_args, **_kwargs: pytest.fail(
            "plain SMTP must not be opened for implicit TLS"
        ),
        smtp_ssl_factory=lambda *_args, **kwargs: (
            fake if kwargs["context"] is context else pytest.fail("wrong TLS context")
        ),
        ssl_context_factory=lambda: context,
    )

    channel.send(notification)

    assert "starttls" not in fake.calls
    assert ("login", "sender@example.com", "app-specific-secret") in fake.calls
    assert "send" in fake.calls


def test_smtp_channel_enforces_verified_tls_12_and_disables_key_logging(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    notification = store.enqueue_notification(
        _test_request(_config()),
        held=False,
        now=BASE_TIME,
    )
    fake = _FakeSmtp()
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    context.keylog_filename = str(tmp_path / "tls.keys")

    SmtpEmailChannel(
        _config(),
        smtp_factory=lambda *_args, **_kwargs: fake,
        ssl_context_factory=lambda: context,
    ).send(notification)

    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.keylog_filename is None


def test_smtp_failure_classification_distinguishes_permanent_and_ambiguous(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    message = store.enqueue_notification(
        _test_request(_config()),
        held=False,
        now=BASE_TIME,
    )

    class AuthenticationFailure(_FakeSmtp):
        def login(self, _username, _password):
            raise smtplib.SMTPAuthenticationError(535, b"denied")

    with pytest.raises(NotificationChannelError) as authentication:
        SmtpEmailChannel(
            _config(),
            smtp_factory=lambda *_args, **_kwargs: AuthenticationFailure(),
            ssl_context_factory=lambda: object(),
        ).send(message)
    assert authentication.value.error_type == "smtp_authentication_failed"
    assert authentication.value.failure_state == "permanent_failure"

    class DisconnectDuringSend(_FakeSmtp):
        def send_message(self, _email):
            raise smtplib.SMTPServerDisconnected("unknown delivery state")

    with pytest.raises(NotificationChannelError) as disconnected:
        SmtpEmailChannel(
            _config(),
            smtp_factory=lambda *_args, **_kwargs: DisconnectDuringSend(),
            ssl_context_factory=lambda: object(),
        ).send(message)
    assert disconnected.value.error_type == "smtp_delivery_uncertain"
    assert disconnected.value.failure_state == "ambiguous"


def test_schema_v3_route_migration_preserves_state_and_quarantines_old_rows(
    tmp_path,
) -> None:
    path = tmp_path / "alerts.sqlite3"
    store = AlertStore(path)
    route = store.enable_notification_route(
        EMAIL_CHANNEL_NAME,
        FINGERPRINT,
        route_alias=EMAIL_ROUTE_ALIAS,
        now=BASE_TIME,
    )
    incident = store.emit(_alert("legacy"), now=BASE_TIME + timedelta(minutes=1))
    legacy_message = next(
        message
        for message in store.list_notifications()
        if message.incident_id == incident.incident_id
    )

    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP INDEX notification_outbox_activation_idx;
            ALTER TABLE notification_outbox DROP COLUMN depends_on_notification_id;
            ALTER TABLE notification_outbox DROP COLUMN route_activation_id;
            ALTER TABLE notification_route_events DROP COLUMN activation_id;
            ALTER TABLE notification_routes DROP COLUMN activation_id;
            PRAGMA user_version = 3;
            """
        )

    migrated = AlertStore(path)
    migrated_route = migrated.notification_route(
        EMAIL_CHANNEL_NAME,
        route_alias=EMAIL_ROUTE_ALIAS,
    )
    assert migrated_route is not None
    assert migrated_route.route_id == route.route_id
    assert migrated_route.activation_id.startswith("activation-legacy-")
    preserved = migrated.get_notification(legacy_message.notification_id)
    assert preserved.state == "pending"
    assert preserved.route_activation_id is None
    assert migrated.claim_notifications(
        EMAIL_CHANNEL_NAME,
        route_alias=EMAIL_ROUTE_ALIAS,
        config_fingerprint=FINGERPRINT,
        now=BASE_TIME + timedelta(minutes=2),
    ) == []


def test_route_activation_never_releases_history_and_orders_recovery(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    historical = store.emit(_alert("historical"), now=BASE_TIME)
    historical_open = store.list_notifications()[0]
    assert historical_open.state == "held"

    store.enable_notification_route(
        EMAIL_CHANNEL_NAME,
        FINGERPRINT,
        route_alias=EMAIL_ROUTE_ALIAS,
        now=BASE_TIME + timedelta(minutes=1),
    )
    historical_recovery = store.resolve(
        historical.incident_id,
        now=BASE_TIME + timedelta(minutes=2),
    )
    assert historical_recovery.state == "resolved"
    assert all(
        message.state == "held"
        for message in store.list_notifications(limit=10)
        if message.incident_id == historical.incident_id
    )

    current = store.emit(_alert("current"), now=BASE_TIME + timedelta(minutes=3))
    store.resolve(current.incident_id, now=BASE_TIME + timedelta(minutes=4))
    current_messages = sorted(
        (
            message
            for message in store.list_notifications(limit=10)
            if message.incident_id == current.incident_id
        ),
        key=lambda message: message.created_at,
    )
    assert [message.state for message in current_messages] == ["pending", "pending"]

    first_claims = store.claim_notifications(
        EMAIL_CHANNEL_NAME,
        route_alias=EMAIL_ROUTE_ALIAS,
        config_fingerprint=FINGERPRINT,
        limit=10,
        now=BASE_TIME + timedelta(minutes=5),
    )
    assert [claim.message.event_type for claim in first_claims] == ["opened"]
    store.complete_notification_delivery(
        first_claims[0].delivery.delivery_id,
        first_claims[0].lease_token,
        succeeded=True,
        now=BASE_TIME + timedelta(minutes=5),
    )
    second_claims = store.claim_notifications(
        EMAIL_CHANNEL_NAME,
        route_alias=EMAIL_ROUTE_ALIAS,
        config_fingerprint=FINGERPRINT,
        limit=10,
        now=BASE_TIME + timedelta(minutes=6),
    )
    assert [claim.message.event_type for claim in second_claims] == ["resolved"]


def test_route_reconfiguration_quarantines_old_activation_and_claim_is_atomic(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    first_route = store.enable_notification_route(
        EMAIL_CHANNEL_NAME,
        FINGERPRINT,
        route_alias=EMAIL_ROUTE_ALIAS,
        now=BASE_TIME,
    )
    old_incident = store.emit(_alert("old-activation"), now=BASE_TIME)
    old_message = next(
        message
        for message in store.list_notifications()
        if message.incident_id == old_incident.incident_id
    )

    second_fingerprint = "b" * 64
    second_route = store.enable_notification_route(
        EMAIL_CHANNEL_NAME,
        second_fingerprint,
        route_alias=EMAIL_ROUTE_ALIAS,
        now=BASE_TIME + timedelta(minutes=1),
    )

    assert second_route.activation_id != first_route.activation_id
    quarantined = store.get_notification(old_message.notification_id)
    assert quarantined.state == "held"
    assert quarantined.last_error_type == "route_reconfigured"
    assert quarantined.route_activation_id == first_route.activation_id

    current = store.emit(_alert("new-activation"), now=BASE_TIME + timedelta(minutes=2))
    current_message = next(
        message
        for message in store.list_notifications()
        if message.incident_id == current.incident_id
    )
    assert current_message.route_activation_id == second_route.activation_id
    with pytest.raises(ValueError, match="configuration changed"):
        store.claim_notifications(
            EMAIL_CHANNEL_NAME,
            route_alias=EMAIL_ROUTE_ALIAS,
            config_fingerprint=FINGERPRINT,
            now=BASE_TIME + timedelta(minutes=3),
        )
    claims = store.claim_notifications(
        EMAIL_CHANNEL_NAME,
        route_alias=EMAIL_ROUTE_ALIAS,
        config_fingerprint=second_fingerprint,
        now=BASE_TIME + timedelta(minutes=3),
    )
    assert [claim.message.notification_id for claim in claims] == [
        current_message.notification_id
    ]


def test_disabled_email_test_bypass_requires_exact_reviewed_message(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    config = _config()
    legitimate = store.enqueue_notification(
        _test_request(config),
        held=False,
        now=BASE_TIME,
    )
    with pytest.raises(ValueError, match="full exact"):
        dispatch_email_notifications(
            store,
            config,
            notification_id=legitimate.notification_id[:20],
            require_enabled_route=False,
            now=BASE_TIME,
        )

    tampered = store.enqueue_notification(
        NotificationRequest(
            idempotency_key="test:smtp:tampered",
            event_type="test",
            severity=AlertSeverity.INFO,
            title="SMTP test",
            body="Not an approved external receipt test.",
            source_job="test",
            payload={
                "config_fingerprint": config.fingerprint,
                "email_test": False,
            },
        ),
        held=False,
        now=BASE_TIME,
    )
    with pytest.raises(ValueError, match="not the exact"):
        dispatch_email_notifications(
            store,
            config,
            notification_id=tampered.notification_id,
            require_enabled_route=False,
            now=BASE_TIME,
        )


def test_route_disable_holds_pending_and_reenable_does_not_release_it(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    route = store.enable_notification_route(
        EMAIL_CHANNEL_NAME,
        FINGERPRINT,
        route_alias=EMAIL_ROUTE_ALIAS,
        now=BASE_TIME,
    )
    incident = store.emit(_alert("pending"), now=BASE_TIME + timedelta(minutes=1))
    pending = next(
        message
        for message in store.list_notifications()
        if message.incident_id == incident.incident_id
    )
    assert pending.state == "pending"

    disabled = store.disable_notification_route(
        EMAIL_CHANNEL_NAME,
        route_alias=EMAIL_ROUTE_ALIAS,
        now=BASE_TIME + timedelta(minutes=2),
    )
    assert disabled.route_id == route.route_id
    assert disabled.state == "disabled"
    assert store.get_notification(pending.notification_id).state == "held"

    store.enable_notification_route(
        EMAIL_CHANNEL_NAME,
        FINGERPRINT,
        route_alias=EMAIL_ROUTE_ALIAS,
        now=BASE_TIME + timedelta(minutes=3),
    )
    assert store.get_notification(pending.notification_id).state == "held"
    assert [
        event["event_type"]
        for event in reversed(
            store.notification_route_events(
                EMAIL_CHANNEL_NAME,
                route_alias=EMAIL_ROUTE_ALIAS,
            )
        )
    ] == ["enabled", "disabled", "enabled"]


def test_failed_active_message_prevents_recovery_only_delivery(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    store.enable_notification_route(
        EMAIL_CHANNEL_NAME,
        FINGERPRINT,
        route_alias=EMAIL_ROUTE_ALIAS,
        now=BASE_TIME,
    )
    incident = store.emit(_alert("failure"), now=BASE_TIME + timedelta(minutes=1))
    store.resolve(incident.incident_id, now=BASE_TIME + timedelta(minutes=2))
    claim = store.claim_notifications(
        EMAIL_CHANNEL_NAME,
        route_alias=EMAIL_ROUTE_ALIAS,
        config_fingerprint=FINGERPRINT,
        now=BASE_TIME + timedelta(minutes=3),
    )[0]
    store.complete_notification_delivery(
        claim.delivery.delivery_id,
        claim.lease_token,
        succeeded=False,
        error_type="smtp_authentication_failed",
        failure_state="permanent_failure",
        now=BASE_TIME + timedelta(minutes=3),
    )

    messages = {
        message.event_type: message
        for message in store.list_notifications(limit=10)
        if message.incident_id == incident.incident_id
    }
    assert messages["opened"].state == "dead_letter"
    assert messages["resolved"].state == "held"
    assert messages["resolved"].last_error_type == "active_notification_not_delivered"
    assert (
        store.notification_route_dead_letter_count(
            EMAIL_CHANNEL_NAME,
            route_alias=EMAIL_ROUTE_ALIAS,
        )
        == 1
    )


def test_email_cli_requires_receipt_test_before_future_route_enable(
    monkeypatch,
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    config = _config()
    store.emit(_alert("old-held"), now=BASE_TIME)
    monkeypatch.setattr(alerts_module, "get_alert_store", lambda: store)
    monkeypatch.setattr(notifications_module, "smtp_email_config", lambda: config)
    monkeypatch.setattr(
        scheduler_module,
        "set_email_scheduler_active",
        lambda _active: None,
    )

    class AcceptedEmail:
        name = EMAIL_CHANNEL_NAME
        route_alias = EMAIL_ROUTE_ALIAS

        def send(self, message):
            return DeliveryOutcome(
                provider_message_id=f"<{message.notification_id}@example.com>",
                provider_response={
                    "accepted": True,
                    "channel": self.name,
                    "external_delivery": True,
                    "provider_state": "accepted_by_test_smtp",
                },
            )

    def fake_dispatch(
        target_store,
        config_value,
        *,
        notification_id=None,
        require_enabled_route=True,
        **_kwargs,
    ):
        return dispatch_notifications(
            target_store,
            AcceptedEmail(),
            notification_id=notification_id,
            config_fingerprint=(
                config_value.fingerprint if require_enabled_route else None
            ),
            test_config_fingerprint=(
                config_value.fingerprint if not require_enabled_route else None
            ),
        )

    monkeypatch.setattr(
        notifications_module,
        "dispatch_email_notifications",
        fake_dispatch,
    )
    runner = CliRunner()

    disabled_worker = runner.invoke(cli.app, ["email-deliver"])
    refused = runner.invoke(cli.app, ["email-enable", "--confirm-enable"])
    no_confirmation = runner.invoke(cli.app, ["email-test"])
    tested = runner.invoke(cli.app, ["email-test", "--confirm-send"])
    enabled = runner.invoke(cli.app, ["email-enable", "--confirm-enable"])

    assert disabled_worker.exit_code == 0
    assert "External email is off" in disabled_worker.output
    assert refused.exit_code == 1
    assert "no successful receipt test" in refused.output
    assert no_confirmation.exit_code == 1
    assert "No email sent" in no_confirmation.output
    assert tested.exit_code == 0, tested.output
    assert "Future incident email is still off" in tested.output
    assert enabled.exit_code == 0
    assert "existing held message(s) remain held" in enabled.output
    route = store.notification_route(
        EMAIL_CHANNEL_NAME,
        route_alias=EMAIL_ROUTE_ALIAS,
    )
    assert route is not None and route.state == "enabled"
    assert store.list_notifications(state="held")


def test_email_disable_stops_optional_worker_even_without_a_route(
    monkeypatch,
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    timer_states: list[bool] = []
    monkeypatch.setattr(alerts_module, "get_alert_store", lambda: store)
    monkeypatch.setattr(
        scheduler_module,
        "set_email_scheduler_active",
        timer_states.append,
    )

    result = CliRunner().invoke(
        cli.app,
        ["email-disable", "--confirm-disable"],
    )

    assert result.exit_code == 0
    assert "Future incident email is off" in result.output
    assert timer_states == [False]


def test_email_dispatch_requires_enabled_matching_route(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    config = _config()
    store.enqueue_notification(_test_request(config), held=False, now=BASE_TIME)

    with pytest.raises(ValueError, match="route is disabled"):
        dispatch_email_notifications(store, config)

    store.enable_notification_route(
        EMAIL_CHANNEL_NAME,
        "b" * 64,
        route_alias=EMAIL_ROUTE_ALIAS,
        now=BASE_TIME,
    )
    with pytest.raises(ValueError, match="configuration changed"):
        dispatch_email_notifications(store, config)

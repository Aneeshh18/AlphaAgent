"""Channel-neutral notification dispatch over the independent operations ledger."""

from __future__ import annotations

import hashlib
import json
import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime
from typing import Any, Protocol

from aios.alerts import AlertStore, NotificationMessage
from aios.config import Settings, settings

EMAIL_CHANNEL_NAME = "smtp-email"
EMAIL_ROUTE_ALIAS = "primary"
EMAIL_CONFIG_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DeliveryOutcome:
    """Safe provider metadata returned by one notification channel."""

    provider_message_id: str | None = None
    provider_response: dict[str, Any] = field(default_factory=dict)


class NotificationChannel(Protocol):
    """Minimal transport boundary for future email, Slack, or push adapters."""

    name: str
    route_alias: str

    def send(self, message: NotificationMessage) -> DeliveryOutcome:
        """Deliver one already-leased immutable message."""


@dataclass(frozen=True)
class DispatchSummary:
    """Result of one bounded dispatcher pass."""

    claimed: int
    succeeded: int
    failed: int
    dead_lettered: int


class NotificationChannelError(RuntimeError):
    """Safe channel failure classification without persisting exception text."""

    def __init__(
        self,
        error_type: str,
        *,
        failure_state: str,
        provider_response: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        self.failure_state = failure_state
        self.provider_response = provider_response or {}


class LocalTestChannel:
    """Deterministic no-network sink used to certify the outbox lifecycle."""

    name = "local-test"
    route_alias = "local-test"

    def send(self, message: NotificationMessage) -> DeliveryOutcome:
        return DeliveryOutcome(
            provider_message_id=f"local-{message.notification_id}",
            provider_response={
                "accepted": True,
                "channel": self.name,
                "external_delivery": False,
            },
        )


@dataclass(frozen=True)
class SmtpEmailConfig:
    """Validated encrypted SMTP route; the password is never persisted."""

    host: str
    port: int
    security: str
    username: str
    password: str
    from_address: str
    to_address: str
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        host = self.host.strip()
        if (
            not host
            or any(character.isspace() for character in host)
            or "://" in host
            or len(host) > 253
        ):
            raise ValueError("SMTP_HOST must be a valid host name")
        if not 1 <= self.port <= 65535:
            raise ValueError("SMTP_PORT must be between 1 and 65535")
        if self.security not in {"starttls", "tls"}:
            raise ValueError("SMTP_SECURITY must be starttls or tls")
        if not self.username.strip() or _contains_header_break(self.username):
            raise ValueError("SMTP_USERNAME is required and cannot contain line breaks")
        if not self.password:
            raise ValueError("SMTP_PASSWORD is required")
        _validate_email_address("ALERT_EMAIL_FROM", self.from_address)
        _validate_email_address("ALERT_EMAIL_TO", self.to_address)
        if not 5.0 <= self.timeout_seconds <= 30.0:
            raise ValueError("SMTP_TIMEOUT_SECONDS must be between 5 and 30")
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "security", self.security.strip().lower())
        object.__setattr__(self, "username", self.username.strip())
        object.__setattr__(self, "from_address", self.from_address.strip())
        object.__setattr__(self, "to_address", self.to_address.strip())

    @property
    def fingerprint(self) -> str:
        """Hash non-secret routing fields so config drift fails closed."""
        canonical = json.dumps(
            {
                "from_address": self.from_address.strip().lower(),
                "host": self.host.strip().lower(),
                "port": self.port,
                "schema_version": EMAIL_CONFIG_SCHEMA_VERSION,
                "security": self.security,
                "to_address": self.to_address.strip().lower(),
                "username": self.username.strip(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def smtp_email_config(
    configured_settings: Settings = settings,
) -> SmtpEmailConfig:
    """Build email configuration or list missing variable names without values."""
    password = configured_settings.smtp_password.get_secret_value()
    values = {
        "SMTP_HOST": configured_settings.smtp_host,
        "SMTP_USERNAME": configured_settings.smtp_username,
        "SMTP_PASSWORD": password,
        "ALERT_EMAIL_FROM": configured_settings.alert_email_from,
        "ALERT_EMAIL_TO": configured_settings.alert_email_to,
    }
    missing = [name for name, value in values.items() if not str(value).strip()]
    if missing:
        raise ValueError(
            "email configuration is incomplete; set " + ", ".join(sorted(missing))
        )
    try:
        port = int(configured_settings.smtp_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("SMTP_PORT must be an integer between 1 and 65535") from exc
    try:
        timeout_seconds = float(configured_settings.smtp_timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("SMTP_TIMEOUT_SECONDS must be a number from 5 to 30") from exc
    return SmtpEmailConfig(
        host=configured_settings.smtp_host,
        port=port,
        security=configured_settings.smtp_security.strip().lower(),
        username=configured_settings.smtp_username,
        password=password,
        from_address=configured_settings.alert_email_from,
        to_address=configured_settings.alert_email_to,
        timeout_seconds=timeout_seconds,
    )


class SmtpEmailChannel:
    """One-recipient encrypted SMTP transport with safe failure classification."""

    name = EMAIL_CHANNEL_NAME
    route_alias = EMAIL_ROUTE_ALIAS

    def __init__(
        self,
        config: SmtpEmailConfig,
        *,
        smtp_factory: Callable[..., Any] = smtplib.SMTP,
        smtp_ssl_factory: Callable[..., Any] = smtplib.SMTP_SSL,
        ssl_context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context,
    ) -> None:
        self.config = config
        self._smtp_factory = smtp_factory
        self._smtp_ssl_factory = smtp_ssl_factory
        self._ssl_context_factory = ssl_context_factory

    def send(self, message: NotificationMessage) -> DeliveryOutcome:
        email = _build_email_message(self.config, message)
        message_id = str(email["Message-ID"])
        stage = "connect"
        try:
            context = self._ssl_context_factory()
            _harden_ssl_context(context)
            if self.config.security == "tls":
                client_context = self._smtp_ssl_factory(
                    self.config.host,
                    self.config.port,
                    timeout=self.config.timeout_seconds,
                    context=context,
                )
            else:
                client_context = self._smtp_factory(
                    self.config.host,
                    self.config.port,
                    timeout=self.config.timeout_seconds,
                )
            with client_context as client:
                if self.config.security == "starttls":
                    client.ehlo()
                    client.starttls(context=context)
                    client.ehlo()
                stage = "authenticate"
                client.login(self.config.username, self.config.password)
                stage = "send"
                refused = client.send_message(email)
                if refused:
                    raise NotificationChannelError(
                        "smtp_recipient_refused",
                        failure_state="permanent_failure",
                        provider_response={
                            "delivery_phase": "send",
                            "provider_state": "recipient_refused",
                        },
                    )
                stage = "accepted"
        except NotificationChannelError:
            raise
        except smtplib.SMTPAuthenticationError as exc:
            raise NotificationChannelError(
                "smtp_authentication_failed",
                failure_state="permanent_failure",
                provider_response={
                    "delivery_phase": "authenticate",
                    "provider_state": "rejected",
                    "smtp_status_class": _smtp_status_class(exc.smtp_code),
                    "status_code": int(exc.smtp_code),
                },
            ) from exc
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            raise NotificationChannelError(
                "smtp_address_refused",
                failure_state="permanent_failure",
                provider_response={
                    "delivery_phase": stage,
                    "provider_state": "address_refused",
                },
            ) from exc
        except smtplib.SMTPNotSupportedError as exc:
            raise NotificationChannelError(
                "smtp_security_or_auth_unsupported",
                failure_state="permanent_failure",
                provider_response={
                    "delivery_phase": stage,
                    "provider_state": "unsupported",
                },
            ) from exc
        except smtplib.SMTPResponseException as exc:
            failure_state = (
                "retryable_failure"
                if 400 <= int(exc.smtp_code) < 500
                else "permanent_failure"
            )
            raise NotificationChannelError(
                f"smtp_response_{int(exc.smtp_code)}",
                failure_state=failure_state,
                provider_response={
                    "delivery_phase": stage,
                    "provider_state": "rejected",
                    "smtp_status_class": _smtp_status_class(exc.smtp_code),
                    "status_code": int(exc.smtp_code),
                },
            ) from exc
        except (ssl.SSLCertVerificationError, ssl.SSLError) as exc:
            raise NotificationChannelError(
                "smtp_tls_verification_failed",
                failure_state="permanent_failure",
                provider_response={
                    "delivery_phase": stage,
                    "provider_state": "tls_failed",
                },
            ) from exc
        except (smtplib.SMTPServerDisconnected, TimeoutError) as exc:
            if stage != "accepted":
                raise NotificationChannelError(
                    "smtp_delivery_uncertain"
                    if stage == "send"
                    else "smtp_connection_failed",
                    failure_state=(
                        "ambiguous" if stage == "send" else "retryable_failure"
                    ),
                    provider_response={
                        "delivery_phase": stage,
                        "provider_state": (
                            "outcome_unknown"
                            if stage == "send"
                            else "temporarily_unavailable"
                        ),
                    },
                ) from exc
        except (ConnectionError, OSError, smtplib.SMTPException) as exc:
            if stage != "accepted":
                raise NotificationChannelError(
                    "smtp_delivery_uncertain"
                    if stage == "send"
                    else "smtp_transport_failed",
                    failure_state=(
                        "ambiguous" if stage == "send" else "retryable_failure"
                    ),
                    provider_response={
                        "delivery_phase": stage,
                        "provider_state": (
                            "outcome_unknown"
                            if stage == "send"
                            else "temporarily_unavailable"
                        ),
                    },
                ) from exc
        if stage != "accepted":  # pragma: no cover - protected by branches above
            raise NotificationChannelError(
                "smtp_delivery_uncertain",
                failure_state="ambiguous",
                provider_response={
                    "delivery_phase": stage,
                    "provider_state": "outcome_unknown",
                },
            )
        return DeliveryOutcome(
            provider_message_id=message_id,
            provider_response={
                "accepted": True,
                "channel": self.name,
                "delivery_phase": "accepted",
                "external_delivery": True,
                "provider_state": "accepted_by_smtp",
            },
        )


def dispatch_email_notifications(
    store: AlertStore,
    config: SmtpEmailConfig,
    *,
    limit: int = 10,
    notification_id: str | None = None,
    require_enabled_route: bool = True,
    now: datetime | None = None,
) -> DispatchSummary:
    """Dispatch email only when the durable route matches current safe config."""
    if limit < 1 or limit > 100:
        raise ValueError("email dispatch limit must be between 1 and 100")
    if not require_enabled_route and notification_id is None:
        raise ValueError("an external email receipt test requires an exact notification ID")
    channel = SmtpEmailChannel(config)
    total = DispatchSummary(claimed=0, succeeded=0, failed=0, dead_lettered=0)
    remaining = limit
    while remaining:
        current = dispatch_notifications(
            store,
            channel,
            limit=1,
            notification_id=notification_id,
            config_fingerprint=(
                config.fingerprint if require_enabled_route else None
            ),
            test_config_fingerprint=(
                config.fingerprint if not require_enabled_route else None
            ),
            now=now,
        )
        total = DispatchSummary(
            claimed=total.claimed + current.claimed,
            succeeded=total.succeeded + current.succeeded,
            failed=total.failed + current.failed,
            dead_lettered=total.dead_lettered + current.dead_lettered,
        )
        remaining -= current.claimed
        if current.claimed == 0 or notification_id is not None:
            break
    return total


def dispatch_notifications(
    store: AlertStore,
    channel: NotificationChannel,
    *,
    limit: int = 10,
    notification_id: str | None = None,
    config_fingerprint: str | None = None,
    test_config_fingerprint: str | None = None,
    now: datetime | None = None,
) -> DispatchSummary:
    """Run one bounded pass without allowing channel failures to lose messages."""
    claims = store.claim_notifications(
        channel.name,
        route_alias=channel.route_alias,
        limit=limit,
        notification_id=notification_id,
        config_fingerprint=config_fingerprint,
        test_config_fingerprint=test_config_fingerprint,
        now=now,
    )
    succeeded = 0
    failed = 0
    dead_lettered = 0
    for claim in claims:
        try:
            outcome = channel.send(claim.message)
        except NotificationChannelError as exc:
            message = store.complete_notification_delivery(
                claim.delivery.delivery_id,
                claim.lease_token,
                succeeded=False,
                provider_response=exc.provider_response,
                error_type=exc.error_type,
                failure_state=exc.failure_state,
                now=now,
            )
            failed += 1
            dead_lettered += int(message.state == "dead_letter")
            continue
        except Exception:
            message = store.complete_notification_delivery(
                claim.delivery.delivery_id,
                claim.lease_token,
                succeeded=False,
                provider_response={
                    "delivery_phase": "channel_send",
                    "provider_state": "internal_error",
                },
                error_type="internal_channel_error",
                failure_state="permanent_failure",
                now=now,
            )
            failed += 1
            dead_lettered += int(message.state == "dead_letter")
            continue

        store.complete_notification_delivery(
            claim.delivery.delivery_id,
            claim.lease_token,
            succeeded=True,
            provider_message_id=outcome.provider_message_id,
            provider_response=outcome.provider_response,
            now=now,
        )
        succeeded += 1
    return DispatchSummary(
        claimed=len(claims),
        succeeded=succeeded,
        failed=failed,
        dead_lettered=dead_lettered,
    )


def _build_email_message(
    config: SmtpEmailConfig,
    message: NotificationMessage,
) -> EmailMessage:
    sender = config.from_address.strip()
    recipient = config.to_address.strip()
    sender_domain = sender.rsplit("@", 1)[1].lower()
    email = EmailMessage()
    email["From"] = sender
    email["To"] = recipient
    email["Subject"] = _email_subject(message)
    email["Date"] = format_datetime(datetime.now(UTC))
    stable_id = hashlib.sha256(
        f"{EMAIL_ROUTE_ALIAS}:{message.idempotency_key}".encode()
    ).hexdigest()[:40]
    email["Message-ID"] = f"<aios-{stable_id}@{sender_domain}>"
    email["Auto-Submitted"] = "auto-generated"
    email["X-Auto-Response-Suppress"] = "All"
    email["X-AIOS-Notification-ID"] = message.notification_id
    if message.incident_id:
        email["X-AIOS-Incident-ID"] = message.incident_id
    email.set_content(
        "\n".join(
            (
                "AI Investment OS operating alert",
                "",
                f"Severity: {message.severity.upper()}",
                f"Event: {message.event_type.replace('_', ' ').title()}",
                f"Summary: {message.title}",
                f"Detail: {message.body}",
                f"Source: {message.source_job}",
                f"Created: {message.created_at}",
                f"Incident: {message.incident_id or 'none'}",
                f"Notification: {message.notification_id}",
                "",
                "This is an operating-system alert, not an investment recommendation.",
                "No broker action was taken.",
            )
        )
    )
    return email


def _email_subject(message: NotificationMessage) -> str:
    title = " ".join(message.title.split())
    severity = message.severity.upper()
    return f"[AIOS][{severity}] {title}"[:180]


def _validate_email_address(label: str, value: str) -> None:
    candidate = value.strip()
    if not candidate or _contains_header_break(candidate) or "," in candidate:
        raise ValueError(f"{label} must contain exactly one bare email address")
    try:
        address = Address(addr_spec=candidate)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain a valid email address") from exc
    if not address.username or not address.domain or address.addr_spec != candidate:
        raise ValueError(f"{label} must contain a valid bare email address")


def _contains_header_break(value: str) -> bool:
    return "\r" in value or "\n" in value


def _harden_ssl_context(context: Any) -> None:
    """Require modern certificate-verified TLS and disable key logging."""
    if not isinstance(context, ssl.SSLContext):
        return
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    if hasattr(context, "keylog_filename"):
        context.keylog_filename = None


def _smtp_status_class(status_code: Any) -> str:
    try:
        value = int(status_code)
    except (TypeError, ValueError):
        return "unknown"
    return f"{value // 100}xx"

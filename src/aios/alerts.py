"""Durable local incidents that remain writable when the analytical DB is unavailable."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from aios.config import settings
from aios.scheduler import MANAGED_SERVICE_NAMES, STATUS_QUERY_TIMEOUT_SECONDS

ALERT_SCHEMA_VERSION = 4
MAX_PAYLOAD_BYTES = 64 * 1024
NOTIFICATION_MAX_ATTEMPTS = 5
NOTIFICATION_LEASE_SECONDS = 300
NOTIFICATION_RETRY_BASE_SECONDS = 60
NOTIFICATION_RETRY_MAX_SECONDS = 60 * 60
NOTIFICATION_EVENT_TYPES = {
    "active",
    "opened",
    "reopened",
    "escalated",
    "resolved",
    "test",
}
NOTIFICATION_ACTIVE_EVENT_TYPES = {"active", "opened", "reopened", "escalated"}
NOTIFICATION_STATES = {
    "held",
    "pending",
    "leased",
    "delivered",
    "dead_letter",
}
NOTIFICATION_DELIVERY_STATES = {
    "started",
    "succeeded",
    "retryable_failure",
    "permanent_failure",
    "ambiguous",
    "abandoned",
}
NOTIFICATION_ROUTE_STATES = {"enabled", "disabled"}
NOTIFICATION_ROUTE_EVENT_TYPES = {"enabled", "disabled", "reconfigured"}
SAFE_PROVIDER_RESPONSE_FIELDS = {
    "accepted",
    "channel",
    "delivery_phase",
    "external_delivery",
    "provider_state",
    "response_bytes",
    "response_sha256",
    "smtp_status_class",
    "status_code",
}
SYSTEMD_FAILURE_PROPERTIES = (
    "Id",
    "Result",
    "ExecMainCode",
    "ExecMainStatus",
    "ExecMainStartTimestamp",
    "ExecMainExitTimestamp",
)
_SECRET_MARKERS = ("secret", "token", "password", "authorization", "cookie", "api_key")


class AlertSeverity(StrEnum):
    """Operator-facing incident severity."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Alert:
    """One observed operational symptom before lifecycle reconciliation."""

    code: str
    severity: AlertSeverity
    title: str
    body: str
    dedup_key: str
    source_job: str
    payload: dict[str, Any] = field(default_factory=dict)
    notify: bool = True


@dataclass(frozen=True)
class Incident:
    """Current state for one deduplicated operational symptom."""

    incident_id: str
    fingerprint: str
    code: str
    severity: str
    title: str
    body: str
    source_job: str
    state: str
    first_seen_at: str
    last_seen_at: str
    occurrence_count: int
    payload: dict[str, Any]
    acknowledged_at: str | None
    resolved_at: str | None
    notifications_enabled: bool


@dataclass(frozen=True)
class JobRun:
    """Durable lifecycle state for one local scheduled workflow."""

    run_id: str
    job_name: str
    state: str
    target_session: str
    started_at: str
    finished_at: str | None
    owner_pid: int
    owner_boot_id: str
    detail: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class JobStart:
    """A newly started job plus abandoned runs recovered before it."""

    run: JobRun
    interrupted_run_ids: tuple[str, ...]


@dataclass(frozen=True)
class NotificationRequest:
    """One immutable channel-neutral message before it enters the outbox."""

    idempotency_key: str
    event_type: str
    severity: AlertSeverity
    title: str
    body: str
    source_job: str
    payload: dict[str, Any] = field(default_factory=dict)
    incident_id: str | None = None
    source_event_id: str | None = None


@dataclass(frozen=True)
class NotificationMessage:
    """Current durable state for one channel-neutral outbox message."""

    notification_id: str
    idempotency_key: str
    incident_id: str | None
    source_event_id: str | None
    route_activation_id: str | None
    depends_on_notification_id: str | None
    message_schema_version: int
    event_type: str
    severity: str
    title: str
    body: str
    source_job: str
    payload: dict[str, Any]
    state: str
    created_at: str
    available_at: str
    attempt_count: int
    lease_token: str | None
    lease_expires_at: str | None
    delivered_at: str | None
    dead_lettered_at: str | None
    last_error_type: str | None


@dataclass(frozen=True)
class NotificationDelivery:
    """One immutable delivery attempt for an outbox message."""

    delivery_id: str
    notification_id: str
    attempt_number: int
    channel: str
    route_alias: str
    state: str
    started_at: str
    finished_at: str | None
    provider_message_id: str | None
    provider_response: dict[str, Any]
    error_type: str | None
    retry_at: str | None


@dataclass(frozen=True)
class NotificationClaim:
    """One leased message plus the attempt that owns its lease."""

    message: NotificationMessage
    delivery: NotificationDelivery
    lease_token: str


@dataclass(frozen=True)
class NotificationRoute:
    """Durable opt-in state for one external notification destination."""

    route_id: str
    channel: str
    route_alias: str
    activation_id: str
    state: str
    config_fingerprint: str
    enabled_at: str | None
    disabled_at: str | None
    updated_at: str


class AlertStore:
    """Independent SQLite incident ledger with bounded concurrent writes."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        requested = Path(path).expanduser()
        if requested.is_symlink():
            raise ValueError(f"operations database cannot be a symbolic link: {requested}")
        self.path = requested.resolve()
        self.read_only = read_only
        if read_only:
            self._validate_read_only()
        else:
            self._initialize()

    def _validate_read_only(self) -> None:
        """Open only an already-current, checkpointed ledger without changing it."""
        if not self.path.is_file():
            raise ValueError(f"operations database is not initialized: {self.path}")
        wal_path = Path(f"{self.path}-wal")
        if wal_path.exists() and wal_path.stat().st_size:
            raise RuntimeError(
                "operations database has an uncheckpointed WAL; retry after the "
                "writer closes"
            )
        with self._connect() as connection:
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version != ALERT_SCHEMA_VERSION:
            raise ValueError(
                f"operations database schema {current_version} does not match "
                f"required schema {ALERT_SCHEMA_VERSION}; run a state-changing "
                "operations command to perform any supported migration"
            )

    def _initialize(self) -> None:
        if self.path.is_symlink():
            raise ValueError(f"operations database cannot be a symbolic link: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > ALERT_SCHEMA_VERSION:
                raise ValueError(
                    "operations database schema is newer than this AIOS installation"
                )
            incident_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(incidents)").fetchall()
            }
            incident_policy_migration = (
                """
                ALTER TABLE incidents
                ADD COLUMN notifications_enabled INTEGER NOT NULL DEFAULT 1
                    CHECK (notifications_enabled IN (0, 1));
                """
                if incident_columns and "notifications_enabled" not in incident_columns
                else ""
            )
            outbox_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(notification_outbox)"
                ).fetchall()
            }
            outbox_route_migration = ""
            if outbox_columns and "route_activation_id" not in outbox_columns:
                outbox_route_migration += """
                ALTER TABLE notification_outbox
                ADD COLUMN route_activation_id TEXT;
                """
            if outbox_columns and "depends_on_notification_id" not in outbox_columns:
                outbox_route_migration += """
                ALTER TABLE notification_outbox
                ADD COLUMN depends_on_notification_id TEXT
                    REFERENCES notification_outbox(notification_id);
                """
            route_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(notification_routes)"
                ).fetchall()
            }
            route_activation_migration = (
                """
                ALTER TABLE notification_routes
                ADD COLUMN activation_id TEXT;
                """
                if route_columns and "activation_id" not in route_columns
                else ""
            )
            route_event_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(notification_route_events)"
                ).fetchall()
            }
            route_event_activation_migration = (
                """
                ALTER TABLE notification_route_events
                ADD COLUMN activation_id TEXT;
                """
                if route_event_columns and "activation_id" not in route_event_columns
                else ""
            )
            connection.executescript(
                f"""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    code TEXT NOT NULL,
                    severity TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    source_job TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('open','acknowledged','resolved')),
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL CHECK (occurrence_count > 0),
                    payload_json TEXT NOT NULL,
                    acknowledged_at TEXT,
                    resolved_at TEXT,
                    notifications_enabled INTEGER NOT NULL DEFAULT 1
                        CHECK (notifications_enabled IN (0, 1))
                );
                CREATE INDEX IF NOT EXISTS incidents_state_last_seen_idx
                    ON incidents(state, last_seen_at DESC);
                CREATE TABLE IF NOT EXISTS incident_events (
                    event_id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
                    event_type TEXT NOT NULL CHECK (
                        event_type IN ('opened','repeated','acknowledged','resolved','reopened')
                    ),
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS incident_events_incident_created_idx
                    ON incident_events(incident_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS job_runs (
                    run_id TEXT PRIMARY KEY,
                    job_name TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('running','success','failed','interrupted')
                    ),
                    target_session TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    owner_pid INTEGER NOT NULL,
                    owner_boot_id TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS job_runs_name_started_idx
                    ON job_runs(job_name, started_at DESC);
                {incident_policy_migration}
                UPDATE incidents
                SET notifications_enabled = 0
                WHERE code = 'local_alert_test'
                   OR fingerprint = 'test:local-alert-path';
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    notification_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    incident_id TEXT REFERENCES incidents(incident_id),
                    source_event_id TEXT UNIQUE
                        REFERENCES incident_events(event_id),
                    route_activation_id TEXT,
                    depends_on_notification_id TEXT
                        REFERENCES notification_outbox(notification_id),
                    message_schema_version INTEGER NOT NULL
                        CHECK (message_schema_version = 1),
                    event_type TEXT NOT NULL CHECK (
                        event_type IN (
                            'active','opened','reopened','escalated','resolved','test'
                        )
                    ),
                    severity TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    source_job TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('held','pending','leased','delivered','dead_letter')
                    ),
                    created_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    delivered_at TEXT,
                    dead_lettered_at TEXT,
                    last_error_type TEXT,
                    CHECK (
                        (state = 'leased' AND lease_token IS NOT NULL
                         AND lease_expires_at IS NOT NULL)
                        OR
                        (state != 'leased' AND lease_token IS NULL
                         AND lease_expires_at IS NULL)
                    )
                );
                CREATE INDEX IF NOT EXISTS notification_outbox_due_idx
                    ON notification_outbox(state, available_at, created_at);
                CREATE INDEX IF NOT EXISTS notification_outbox_incident_idx
                    ON notification_outbox(incident_id, created_at DESC);
                {outbox_route_migration}
                CREATE INDEX IF NOT EXISTS notification_outbox_activation_idx
                    ON notification_outbox(route_activation_id, state, created_at);
                CREATE TABLE IF NOT EXISTS notification_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    notification_id TEXT NOT NULL
                        REFERENCES notification_outbox(notification_id),
                    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
                    channel TEXT NOT NULL,
                    route_alias TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'started','succeeded','retryable_failure',
                            'permanent_failure','ambiguous','abandoned'
                        )
                    ),
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    provider_message_id TEXT,
                    provider_response_json TEXT NOT NULL,
                    error_type TEXT,
                    retry_at TEXT,
                    UNIQUE(notification_id, attempt_number)
                );
                CREATE INDEX IF NOT EXISTS notification_deliveries_message_idx
                    ON notification_deliveries(notification_id, attempt_number DESC);
                CREATE TABLE IF NOT EXISTS notification_routes (
                    route_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    route_alias TEXT NOT NULL,
                    activation_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('enabled','disabled')),
                    config_fingerprint TEXT NOT NULL,
                    enabled_at TEXT,
                    disabled_at TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(channel, route_alias),
                    CHECK (
                        (state = 'enabled' AND enabled_at IS NOT NULL
                         AND disabled_at IS NULL)
                        OR
                        (state = 'disabled' AND disabled_at IS NOT NULL)
                    )
                );
                {route_activation_migration}
                UPDATE notification_routes
                SET activation_id = 'activation-legacy-' || lower(hex(randomblob(16)))
                WHERE activation_id IS NULL OR trim(activation_id) = '';
                CREATE TABLE IF NOT EXISTS notification_route_events (
                    event_id TEXT PRIMARY KEY,
                    route_id TEXT NOT NULL REFERENCES notification_routes(route_id),
                    event_type TEXT NOT NULL CHECK (
                        event_type IN ('enabled','disabled','reconfigured')
                    ),
                    created_at TEXT NOT NULL,
                    activation_id TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL
                );
                {route_event_activation_migration}
                UPDATE notification_route_events
                SET activation_id = COALESCE(
                    (
                        SELECT route.activation_id
                        FROM notification_routes AS route
                        WHERE route.route_id = notification_route_events.route_id
                    ),
                    'activation-legacy-event-' || lower(hex(randomblob(16)))
                )
                WHERE activation_id IS NULL OR trim(activation_id) = '';
                CREATE INDEX IF NOT EXISTS notification_route_events_route_idx
                    ON notification_route_events(route_id, created_at DESC);
                PRAGMA user_version = {ALERT_SCHEMA_VERSION};
                COMMIT;
                """
            )
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            connection = sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro&immutable=1",
                timeout=5.0,
                uri=True,
            )
        else:
            connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if self.read_only:
            connection.execute("PRAGMA query_only = ON")
        else:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def emit(self, alert: Alert, *, now: datetime | None = None) -> Incident:
        """Open, repeat, or reopen one incident by its stable deduplication key."""
        _validate_alert(alert)
        timestamp = _timestamp(now)
        payload_json = _payload_json(alert.payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM incidents WHERE fingerprint = ?",
                (alert.dedup_key,),
            ).fetchone()
            if existing is None:
                incident_id = f"inc-{uuid4().hex}"
                event_type = "opened"
                notification_event = "opened"
                connection.execute(
                    """
                    INSERT INTO incidents (
                        incident_id, fingerprint, code, severity, title, body, source_job,
                        state, first_seen_at, last_seen_at, occurrence_count, payload_json,
                        acknowledged_at, resolved_at, notifications_enabled
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, 1, ?, NULL, NULL, ?)
                    """,
                    (
                        incident_id,
                        alert.dedup_key,
                        alert.code,
                        alert.severity.value,
                        alert.title,
                        alert.body,
                        alert.source_job,
                        timestamp,
                        timestamp,
                        payload_json,
                        int(alert.notify),
                    ),
                )
            else:
                incident_id = str(existing["incident_id"])
                event_type = "repeated" if existing["state"] == "open" else "reopened"
                severity = _higher_severity(str(existing["severity"]), alert.severity.value)
                notification_event = (
                    "reopened"
                    if event_type == "reopened"
                    else (
                        "escalated"
                        if severity != str(existing["severity"])
                        else None
                    )
                )
                connection.execute(
                    """
                    UPDATE incidents
                    SET code = ?, severity = ?, title = ?, body = ?, source_job = ?,
                        state = 'open', last_seen_at = ?, occurrence_count = occurrence_count + 1,
                        payload_json = ?, acknowledged_at = NULL, resolved_at = NULL
                    WHERE incident_id = ?
                    """,
                    (
                        alert.code,
                        severity,
                        alert.title,
                        alert.body,
                        alert.source_job,
                        timestamp,
                        payload_json,
                        incident_id,
                    ),
                )
            event_id = self._insert_event(
                connection,
                incident_id,
                event_type,
                timestamp,
                payload_json,
            )
            row = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if (
                row is not None
                and bool(row["notifications_enabled"])
                and notification_event is not None
            ):
                self._enqueue_incident_notification(
                    connection,
                    row,
                    event_type=notification_event,
                    timestamp=timestamp,
                    source_event_id=event_id,
                )
        if row is None:  # pragma: no cover - protected by the transaction above
            raise RuntimeError("incident disappeared during write")
        return _incident_from_row(row)

    def acknowledge(self, incident_id: str, *, now: datetime | None = None) -> Incident:
        """Record that an operator has seen an unresolved incident."""
        timestamp = _timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            incident_id = self._resolve_incident_id(connection, incident_id)
            row = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown incident: {incident_id}")
            if row["state"] == "resolved":
                raise ValueError("a resolved incident cannot be acknowledged")
            connection.execute(
                """
                UPDATE incidents SET state = 'acknowledged', acknowledged_at = ?
                WHERE incident_id = ?
                """,
                (timestamp, incident_id),
            )
            self._insert_event(connection, incident_id, "acknowledged", timestamp, "{}")
            updated = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
        return _incident_from_row(updated)

    def resolve(
        self,
        incident_id: str,
        *,
        now: datetime | None = None,
    ) -> Incident:
        """Close one incident while retaining its complete event history."""
        timestamp = _timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            incident_id = self._resolve_incident_id(connection, incident_id)
            row = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown incident: {incident_id}")
            if row["state"] != "resolved":
                connection.execute(
                    """
                    UPDATE incidents SET state = 'resolved', resolved_at = ?
                    WHERE incident_id = ?
                    """,
                    (timestamp, incident_id),
                )
                event_id = self._insert_event(
                    connection,
                    incident_id,
                    "resolved",
                    timestamp,
                    "{}",
                )
            else:
                event_id = None
            updated = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if (
                row["state"] != "resolved"
                and updated is not None
                and bool(row["notifications_enabled"])
                and event_id is not None
            ):
                self._enqueue_incident_notification(
                    connection,
                    updated,
                    event_type="resolved",
                    timestamp=timestamp,
                    source_event_id=event_id,
                )
        return _incident_from_row(updated)

    def resolve_fingerprint(
        self,
        fingerprint: str,
        *,
        now: datetime | None = None,
    ) -> Incident | None:
        """Resolve an active incident by the producer's stable key; missing is a no-op."""
        timestamp = _timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM incidents WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if row is None:
                return None
            incident_id = str(row["incident_id"])
            if row["state"] != "resolved":
                connection.execute(
                    """
                    UPDATE incidents SET state = 'resolved', resolved_at = ?
                    WHERE incident_id = ?
                    """,
                    (timestamp, incident_id),
                )
                event_id = self._insert_event(
                    connection,
                    incident_id,
                    "resolved",
                    timestamp,
                    "{}",
                )
            else:
                event_id = None
            updated = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if (
                row["state"] != "resolved"
                and updated is not None
                and bool(row["notifications_enabled"])
                and event_id is not None
            ):
                self._enqueue_incident_notification(
                    connection,
                    updated,
                    event_type="resolved",
                    timestamp=timestamp,
                    source_event_id=event_id,
                )
        return _incident_from_row(updated)

    def get(self, incident_id: str) -> Incident:
        with self._connect() as connection:
            incident_id = self._resolve_incident_id(connection, incident_id)
            row = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown incident: {incident_id}")
        return _incident_from_row(row)

    def list(self, *, unresolved_only: bool = False, limit: int = 100) -> list[Incident]:
        if limit < 1 or limit > 1000:
            raise ValueError("incident limit must be between 1 and 1000")
        where = "WHERE state != 'resolved'" if unresolved_only else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM incidents {where}
                ORDER BY CASE state WHEN 'open' THEN 0 WHEN 'acknowledged' THEN 1 ELSE 2 END,
                         CASE severity
                             WHEN 'critical' THEN 0
                             WHEN 'warning' THEN 1
                             ELSE 2
                         END,
                         last_seen_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_incident_from_row(row) for row in rows]

    def incident_summary(self) -> dict[str, int]:
        """Return exact incident counts independent of the bounded display list."""
        summary = {
            "open": 0,
            "acknowledged": 0,
            "resolved": 0,
            "unresolved": 0,
            "critical_unresolved": 0,
            "total": 0,
        }
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT state, severity, COUNT(*) AS count
                FROM incidents
                GROUP BY state, severity
                """
            ).fetchall()
        for row in rows:
            state = str(row["state"])
            count = int(row["count"])
            summary[state] += count
            summary["total"] += count
            if state != "resolved":
                summary["unresolved"] += count
                if row["severity"] == "critical":
                    summary["critical_unresolved"] += count
        return summary

    def events(self, incident_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1000:
            raise ValueError("event limit must be between 1 and 1000")
        with self._connect() as connection:
            incident_id = self._resolve_incident_id(connection, incident_id)
            rows = connection.execute(
                """
                SELECT event_id, event_type, created_at, payload_json
                FROM incident_events WHERE incident_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT ?
                """,
                (incident_id, limit),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "created_at": row["created_at"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def enqueue_notification(
        self,
        request: NotificationRequest,
        *,
        held: bool = True,
        now: datetime | None = None,
    ) -> NotificationMessage:
        """Insert one immutable message or return its exact idempotent match."""
        if not isinstance(held, bool):
            raise ValueError("notification held flag must be boolean")
        _validate_notification_request(request)
        timestamp = _timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._insert_outbox_message(
                connection,
                request,
                timestamp=timestamp,
                held=held,
            )
        return _notification_from_row(row)

    def get_notification(self, notification_id: str) -> NotificationMessage:
        """Resolve one full or unambiguous shortened notification reference."""
        with self._connect() as connection:
            resolved = self._resolve_notification_id(connection, notification_id)
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE notification_id = ?",
                (resolved,),
            ).fetchone()
        if row is None:  # pragma: no cover - protected by reference resolution
            raise ValueError(f"unknown notification: {notification_id}")
        return _notification_from_row(row)

    def hold_notification(
        self,
        notification_id: str,
        *,
        reason: str,
    ) -> NotificationMessage:
        """Make one unsent message ineligible without deleting its audit history."""
        _validate_job_text("reason", reason)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            resolved = self._resolve_notification_id(connection, notification_id)
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE notification_id = ?",
                (resolved,),
            ).fetchone()
            if row is None:  # pragma: no cover - protected by reference resolution
                raise ValueError(f"unknown notification: {notification_id}")
            if row["state"] == "leased":
                raise RuntimeError("a leased notification cannot be held")
            if row["state"] == "delivered":
                return _notification_from_row(row)
            updated = connection.execute(
                """
                UPDATE notification_outbox
                SET state = 'held', lease_token = NULL, lease_expires_at = NULL,
                    dead_lettered_at = NULL, last_error_type = ?
                WHERE notification_id = ?
                RETURNING *
                """,
                (reason, resolved),
            ).fetchone()
        if updated is None:  # pragma: no cover - protected by transaction
            raise RuntimeError("notification disappeared while being held")
        return _notification_from_row(updated)

    def list_notifications(
        self,
        *,
        state: str | None = None,
        limit: int = 100,
    ) -> list[NotificationMessage]:
        """List recent outbox messages without claiming or changing them."""
        if limit < 1 or limit > 1000:
            raise ValueError("notification limit must be between 1 and 1000")
        normalized_state = state.replace("-", "_") if state else None
        if normalized_state is not None and normalized_state not in NOTIFICATION_STATES:
            raise ValueError(f"unknown notification state: {state}")
        where = "WHERE state = ?" if normalized_state is not None else ""
        parameters: tuple[Any, ...] = (
            (normalized_state, limit) if normalized_state is not None else (limit,)
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM notification_outbox
                {where}
                ORDER BY
                    CASE state
                        WHEN 'dead_letter' THEN 0
                        WHEN 'pending' THEN 1
                        WHEN 'leased' THEN 2
                        WHEN 'held' THEN 3
                        ELSE 4
                    END,
                    created_at DESC,
                    rowid DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [_notification_from_row(row) for row in rows]

    def notification_summary(self) -> dict[str, int]:
        """Return exact operator counts for every outbox state."""
        summary = {state: 0 for state in sorted(NOTIFICATION_STATES)}
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT state, COUNT(*) AS count
                FROM notification_outbox
                GROUP BY state
                """
            ).fetchall()
        for row in rows:
            summary[str(row["state"])] = int(row["count"])
        return summary

    def notification_route(
        self,
        channel: str,
        *,
        route_alias: str = "default",
    ) -> NotificationRoute | None:
        """Return the durable opt-in state for one external route."""
        _validate_channel(channel)
        _validate_channel(route_alias)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM notification_routes
                WHERE channel = ? AND route_alias = ?
                """,
                (channel, route_alias),
            ).fetchone()
        return _notification_route_from_row(row) if row is not None else None

    def enable_notification_route(
        self,
        channel: str,
        config_fingerprint: str,
        *,
        route_alias: str = "default",
        now: datetime | None = None,
    ) -> NotificationRoute:
        """Enable only future incident messages; existing held rows stay held."""
        _validate_channel(channel)
        _validate_channel(route_alias)
        _validate_config_fingerprint(config_fingerprint)
        config_fingerprint = config_fingerprint.strip().lower()
        timestamp = _timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_expired_notification_leases(
                connection,
                timestamp=timestamp,
            )
            existing = connection.execute(
                """
                SELECT * FROM notification_routes
                WHERE channel = ? AND route_alias = ?
                """,
                (channel, route_alias),
            ).fetchone()
            if (
                existing is not None
                and existing["state"] == "enabled"
                and existing["config_fingerprint"] == config_fingerprint
            ):
                return _notification_route_from_row(existing)
            if existing is not None:
                active_attempts = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM notification_deliveries
                        WHERE channel = ? AND route_alias = ? AND state = 'started'
                        """,
                        (channel, route_alias),
                    ).fetchone()[0]
                )
                if active_attempts:
                    raise RuntimeError(
                        "notification route has an active delivery; retry after its lease ends"
                    )
                connection.execute(
                    """
                    UPDATE notification_outbox
                    SET state = 'held', last_error_type = 'route_reconfigured'
                    WHERE state = 'pending'
                      AND event_type != 'test'
                      AND route_activation_id = ?
                    """,
                    (existing["activation_id"],),
                )
            activation_id = f"activation-{uuid4().hex}"
            if existing is None:
                route_id = f"route-{uuid4().hex}"
                event_type = "enabled"
                connection.execute(
                    """
                    INSERT INTO notification_routes (
                        route_id, channel, route_alias, activation_id,
                        state, config_fingerprint,
                        enabled_at, disabled_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'enabled', ?, ?, NULL, ?)
                    """,
                    (
                        route_id,
                        channel,
                        route_alias,
                        activation_id,
                        config_fingerprint,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                route_id = str(existing["route_id"])
                event_type = (
                    "reconfigured"
                    if existing["state"] == "enabled"
                    else "enabled"
                )
                connection.execute(
                    """
                    UPDATE notification_routes
                    SET state = 'enabled', activation_id = ?,
                        config_fingerprint = ?,
                        enabled_at = ?, disabled_at = NULL, updated_at = ?
                    WHERE route_id = ?
                    """,
                    (
                        activation_id,
                        config_fingerprint,
                        timestamp,
                        timestamp,
                        route_id,
                    ),
                )
            self._insert_notification_route_event(
                connection,
                route_id=route_id,
                event_type=event_type,
                timestamp=timestamp,
                activation_id=activation_id,
                config_fingerprint=config_fingerprint,
            )
            row = connection.execute(
                "SELECT * FROM notification_routes WHERE route_id = ?",
                (route_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - protected by transaction
            raise RuntimeError("notification route disappeared during enable")
        return _notification_route_from_row(row)

    def disable_notification_route(
        self,
        channel: str,
        *,
        route_alias: str = "default",
        now: datetime | None = None,
    ) -> NotificationRoute:
        """Disable a route and hold unsent production messages without deleting them."""
        _validate_channel(channel)
        _validate_channel(route_alias)
        timestamp = _timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_expired_notification_leases(
                connection,
                timestamp=timestamp,
            )
            existing = connection.execute(
                """
                SELECT * FROM notification_routes
                WHERE channel = ? AND route_alias = ?
                """,
                (channel, route_alias),
            ).fetchone()
            if existing is None:
                raise ValueError(f"notification route is not configured: {channel}")
            if existing["state"] == "disabled":
                return _notification_route_from_row(existing)
            active_attempts = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM notification_deliveries
                    WHERE channel = ? AND route_alias = ? AND state = 'started'
                    """,
                    (channel, route_alias),
                ).fetchone()[0]
            )
            if active_attempts:
                raise RuntimeError(
                    "notification route has an active delivery; retry after its lease ends"
                )
            route_id = str(existing["route_id"])
            config_fingerprint = str(existing["config_fingerprint"])
            activation_id = str(existing["activation_id"])
            connection.execute(
                """
                UPDATE notification_routes
                SET state = 'disabled', disabled_at = ?, updated_at = ?
                WHERE route_id = ?
                """,
                (timestamp, timestamp, route_id),
            )
            connection.execute(
                """
                UPDATE notification_outbox
                SET state = 'held', last_error_type = 'route_disabled'
                WHERE state = 'pending'
                  AND event_type != 'test'
                  AND route_activation_id = ?
                """,
                (activation_id,),
            )
            self._insert_notification_route_event(
                connection,
                route_id=route_id,
                event_type="disabled",
                timestamp=timestamp,
                activation_id=activation_id,
                config_fingerprint=config_fingerprint,
            )
            row = connection.execute(
                "SELECT * FROM notification_routes WHERE route_id = ?",
                (route_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - protected by transaction
            raise RuntimeError("notification route disappeared during disable")
        return _notification_route_from_row(row)

    def notification_route_events(
        self,
        channel: str,
        *,
        route_alias: str = "default",
        limit: int = 100,
    ) -> list[dict[str, str]]:
        """Return append-only route activation history without destination details."""
        if limit < 1 or limit > 1000:
            raise ValueError("route event limit must be between 1 and 1000")
        route = self.notification_route(channel, route_alias=route_alias)
        if route is None:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, event_type, created_at, activation_id,
                       config_fingerprint
                FROM notification_route_events
                WHERE route_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (route.route_id, limit),
            ).fetchall()
        return [
            {
                "event_id": str(row["event_id"]),
                "event_type": str(row["event_type"]),
                "created_at": str(row["created_at"]),
                "activation_id": str(row["activation_id"]),
                "config_fingerprint": str(row["config_fingerprint"]),
            }
            for row in rows
        ]

    def notification_route_dead_letter_count(
        self,
        channel: str,
        *,
        route_alias: str = "default",
    ) -> int:
        """Count exact unresolved delivery failures attempted through one route."""
        _validate_channel(channel)
        _validate_channel(route_alias)
        with self._connect() as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT outbox.notification_id)
                    FROM notification_outbox AS outbox
                    JOIN notification_deliveries AS delivery
                      ON delivery.notification_id = outbox.notification_id
                    WHERE outbox.state = 'dead_letter'
                      AND delivery.channel = ?
                      AND delivery.route_alias = ?
                    """,
                    (channel, route_alias),
                ).fetchone()[0]
            )

    def notification_deliveries(
        self,
        notification_id: str,
        *,
        limit: int = 100,
    ) -> list[NotificationDelivery]:
        """Return the append-only attempt history for one outbox message."""
        if limit < 1 or limit > 1000:
            raise ValueError("delivery limit must be between 1 and 1000")
        with self._connect() as connection:
            resolved = self._resolve_notification_id(connection, notification_id)
            rows = connection.execute(
                """
                SELECT * FROM notification_deliveries
                WHERE notification_id = ?
                ORDER BY attempt_number DESC
                LIMIT ?
                """,
                (resolved, limit),
            ).fetchall()
        return [_delivery_from_row(row) for row in rows]

    def claim_notifications(
        self,
        channel: str,
        *,
        route_alias: str = "default",
        limit: int = 10,
        lease_seconds: int = NOTIFICATION_LEASE_SECONDS,
        notification_id: str | None = None,
        config_fingerprint: str | None = None,
        test_config_fingerprint: str | None = None,
        now: datetime | None = None,
    ) -> list[NotificationClaim]:
        """Lease due messages and atomically start one auditable attempt each."""
        _validate_channel(channel)
        _validate_channel(route_alias)
        if limit < 1 or limit > 100:
            raise ValueError("notification claim limit must be between 1 and 100")
        if lease_seconds < 30 or lease_seconds > 3600:
            raise ValueError("notification lease must be between 30 and 3600 seconds")
        if config_fingerprint is not None and test_config_fingerprint is not None:
            raise ValueError("production and test notification claims cannot be combined")
        if config_fingerprint is not None:
            _validate_config_fingerprint(config_fingerprint)
            config_fingerprint = config_fingerprint.strip().lower()
        if test_config_fingerprint is not None:
            _validate_config_fingerprint(test_config_fingerprint)
            test_config_fingerprint = test_config_fingerprint.strip().lower()
            if notification_id is None:
                raise ValueError("an email test claim requires an exact notification ID")
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        timestamp = _timestamp(moment)
        lease_expires_at = _timestamp(moment + timedelta(seconds=lease_seconds))
        claims: list[NotificationClaim] = []

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_expired_notification_leases(connection, timestamp=timestamp)
            resolved_id = (
                self._resolve_notification_id(connection, notification_id)
                if notification_id is not None
                else None
            )
            claim_scope = "AND candidate.route_activation_id IS NULL"
            scope_parameters: list[Any] = []
            if config_fingerprint is not None:
                route = connection.execute(
                    """
                    SELECT * FROM notification_routes
                    WHERE channel = ? AND route_alias = ? AND state = 'enabled'
                    """,
                    (channel, route_alias),
                ).fetchone()
                if route is None:
                    raise ValueError("notification delivery route is disabled")
                if str(route["config_fingerprint"]) != config_fingerprint:
                    raise ValueError(
                        "notification configuration changed after activation; "
                        "re-enable it explicitly"
                    )
                activation_id = str(route["activation_id"])
                if not activation_id:
                    raise RuntimeError("enabled notification route has no activation")
                claim_scope = "AND candidate.route_activation_id = ?"
                scope_parameters.append(activation_id)
            elif test_config_fingerprint is not None:
                if resolved_id != notification_id:
                    raise ValueError(
                        "an email test claim requires the full exact notification ID"
                    )
                test_row = connection.execute(
                    """
                    SELECT * FROM notification_outbox
                    WHERE notification_id = ?
                    """,
                    (resolved_id,),
                ).fetchone()
                if test_row is None:  # pragma: no cover - resolved above
                    raise ValueError("unknown notification")
                test_payload = json.loads(str(test_row["payload_json"]))
                if (
                    test_row["event_type"] != "test"
                    or test_row["incident_id"] is not None
                    or test_row["route_activation_id"] is not None
                    or test_payload.get("email_test") is not True
                    or test_payload.get("config_fingerprint")
                    != test_config_fingerprint
                ):
                    raise ValueError(
                        "notification is not the exact current-configuration email test"
                    )
                claim_scope = "AND candidate.notification_id = ?"
                scope_parameters.append(resolved_id)
            elif resolved_id is not None:
                claim_scope += " AND candidate.notification_id = ?"
                scope_parameters.append(resolved_id)
            parameters: tuple[Any, ...] = (
                timestamp,
                *scope_parameters,
                limit,
            )
            rows = connection.execute(
                f"""
                SELECT candidate.*
                FROM notification_outbox AS candidate
                WHERE candidate.state = 'pending'
                  AND candidate.available_at <= ?
                  AND candidate.attempt_count < {NOTIFICATION_MAX_ATTEMPTS}
                  AND (
                      candidate.depends_on_notification_id IS NULL
                      OR EXISTS (
                          SELECT 1
                          FROM notification_outbox AS dependency
                          WHERE dependency.notification_id =
                                candidate.depends_on_notification_id
                            AND dependency.state = 'delivered'
                            AND dependency.route_activation_id =
                                candidate.route_activation_id
                      )
                  )
                  {claim_scope}
                ORDER BY
                    CASE candidate.severity
                        WHEN 'critical' THEN 0
                        WHEN 'warning' THEN 1
                        ELSE 2
                    END,
                    candidate.created_at,
                    candidate.rowid
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            for row in rows:
                notification_id_value = str(row["notification_id"])
                attempt_number = int(row["attempt_count"]) + 1
                lease_token = f"lease-{uuid4().hex}"
                delivery_id = f"delivery-{uuid4().hex}"
                updated = connection.execute(
                    """
                    UPDATE notification_outbox
                    SET state = 'leased', attempt_count = ?,
                        lease_token = ?, lease_expires_at = ?,
                        last_error_type = NULL
                    WHERE notification_id = ? AND state = 'pending'
                    RETURNING *
                    """,
                    (
                        attempt_number,
                        lease_token,
                        lease_expires_at,
                        notification_id_value,
                    ),
                ).fetchone()
                if updated is None:  # pragma: no cover - BEGIN IMMEDIATE serializes claims
                    continue
                connection.execute(
                    """
                    INSERT INTO notification_deliveries (
                        delivery_id, notification_id, attempt_number, channel, route_alias,
                        state, started_at, finished_at, provider_message_id,
                        provider_response_json, error_type, retry_at
                    ) VALUES (?, ?, ?, ?, ?, 'started', ?, NULL, NULL, '{}', NULL, NULL)
                    """,
                    (
                        delivery_id,
                        notification_id_value,
                        attempt_number,
                        channel,
                        route_alias,
                        timestamp,
                    ),
                )
                delivery_row = connection.execute(
                    "SELECT * FROM notification_deliveries WHERE delivery_id = ?",
                    (delivery_id,),
                ).fetchone()
                if delivery_row is None:  # pragma: no cover
                    raise RuntimeError("notification delivery disappeared during claim")
                claims.append(
                    NotificationClaim(
                        message=_notification_from_row(updated),
                        delivery=_delivery_from_row(delivery_row),
                        lease_token=lease_token,
                    )
                )
        return claims

    def complete_notification_delivery(
        self,
        delivery_id: str,
        lease_token: str,
        *,
        succeeded: bool,
        provider_message_id: str | None = None,
        provider_response: dict[str, Any] | None = None,
        error_type: str | None = None,
        failure_state: str | None = None,
        now: datetime | None = None,
    ) -> NotificationMessage:
        """Finish one owned attempt and deliver, retry, or dead-letter its message."""
        _validate_job_text("delivery_id", delivery_id)
        _validate_job_text("lease_token", lease_token)
        if provider_message_id is not None:
            _validate_job_text("provider_message_id", provider_message_id)
        if succeeded and (error_type is not None or failure_state is not None):
            raise ValueError("a successful notification delivery cannot have a failure")
        if not succeeded:
            if error_type is None:
                raise ValueError("a failed notification delivery requires an error type")
            _validate_job_text("error_type", error_type)
            failure_state = failure_state or "retryable_failure"
            if failure_state not in NOTIFICATION_DELIVERY_STATES - {
                "started",
                "succeeded",
                "abandoned",
            }:
                raise ValueError(f"unsupported notification failure state: {failure_state}")
        response_json = _provider_response_json(provider_response or {})
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        timestamp = _timestamp(moment)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            delivery = connection.execute(
                """
                SELECT * FROM notification_deliveries
                WHERE delivery_id = ? AND state = 'started'
                """,
                (delivery_id,),
            ).fetchone()
            if delivery is None:
                raise ValueError(
                    f"notification delivery is missing or already finished: {delivery_id}"
                )
            outbox = connection.execute(
                """
                SELECT * FROM notification_outbox
                WHERE notification_id = ? AND state = 'leased' AND lease_token = ?
                """,
                (delivery["notification_id"], lease_token),
            ).fetchone()
            if outbox is None:
                raise ValueError("notification lease is missing or no longer owned")
            if str(outbox["lease_expires_at"]) <= timestamp:
                raise ValueError("notification lease expired before delivery completion")

            if succeeded:
                connection.execute(
                    """
                    UPDATE notification_deliveries
                    SET state = 'succeeded', finished_at = ?,
                        provider_message_id = ?, provider_response_json = ?,
                        error_type = NULL, retry_at = NULL
                    WHERE delivery_id = ?
                    """,
                    (timestamp, provider_message_id, response_json, delivery_id),
                )
                updated = connection.execute(
                    """
                    UPDATE notification_outbox
                    SET state = 'delivered', lease_token = NULL,
                        lease_expires_at = NULL, delivered_at = ?,
                        dead_lettered_at = NULL, last_error_type = NULL
                    WHERE notification_id = ?
                    RETURNING *
                    """,
                    (timestamp, outbox["notification_id"]),
                ).fetchone()
            else:
                attempt_number = int(delivery["attempt_number"])
                should_retry = (
                    failure_state == "retryable_failure"
                    and attempt_number < NOTIFICATION_MAX_ATTEMPTS
                )
                retry_at = (
                    _notification_retry_at(moment, attempt_number)
                    if should_retry
                    else None
                )
                connection.execute(
                    """
                    UPDATE notification_deliveries
                    SET state = ?, finished_at = ?,
                        provider_message_id = ?, provider_response_json = ?,
                        error_type = ?, retry_at = ?
                    WHERE delivery_id = ?
                    """,
                    (
                        failure_state,
                        timestamp,
                        provider_message_id,
                        response_json,
                        error_type,
                        retry_at,
                        delivery_id,
                    ),
                )
                updated = connection.execute(
                    """
                    UPDATE notification_outbox
                    SET state = ?, available_at = ?, lease_token = NULL,
                        lease_expires_at = NULL, delivered_at = NULL,
                        dead_lettered_at = ?, last_error_type = ?
                    WHERE notification_id = ?
                    RETURNING *
                    """,
                    (
                        "pending" if should_retry else "dead_letter",
                        retry_at or timestamp,
                        None if should_retry else timestamp,
                        error_type,
                        outbox["notification_id"],
                    ),
                ).fetchone()
                if not should_retry:
                    self._hold_unreachable_recoveries(connection)
        if updated is None:  # pragma: no cover - protected by transaction
            raise RuntimeError("notification disappeared during delivery completion")
        return _notification_from_row(updated)

    def begin_job(
        self,
        job_name: str,
        target_session: str,
        *,
        now: datetime | None = None,
        owner_pid: int | None = None,
        owner_boot_id: str | None = None,
    ) -> JobStart:
        """Start one job and recover only demonstrably abandoned prior runs."""
        _validate_job_text("job_name", job_name)
        _validate_job_text("target_session", target_session)
        timestamp = _timestamp(now)
        pid = int(owner_pid if owner_pid is not None else os.getpid())
        if pid <= 0:
            raise ValueError("job owner_pid must be positive")
        boot_id = owner_boot_id or _current_boot_id()
        _validate_job_text("owner_boot_id", boot_id)
        run_id = f"job-{uuid4().hex}"
        interrupted_ids: list[str] = []

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            running = connection.execute(
                """
                SELECT * FROM job_runs
                WHERE job_name = ? AND state = 'running'
                ORDER BY started_at DESC
                """,
                (job_name,),
            ).fetchall()
            for row in running:
                previous_pid = int(row["owner_pid"])
                previous_boot = str(row["owner_boot_id"])
                if previous_boot == boot_id and _pid_is_alive(previous_pid):
                    raise RuntimeError(
                        f"{job_name} is already running as process {previous_pid}"
                    )
                interrupted_ids.append(str(row["run_id"]))
                connection.execute(
                    """
                    UPDATE job_runs
                    SET state = 'interrupted', finished_at = ?,
                        detail = 'A later startup found that this run never completed.'
                    WHERE run_id = ? AND state = 'running'
                    """,
                    (timestamp, row["run_id"]),
                )

            connection.execute(
                """
                INSERT INTO job_runs (
                    run_id, job_name, state, target_session, started_at, finished_at,
                    owner_pid, owner_boot_id, detail, payload_json
                ) VALUES (?, ?, 'running', ?, ?, NULL, ?, ?, ?, '{}')
                """,
                (
                    run_id,
                    job_name,
                    target_session,
                    timestamp,
                    pid,
                    boot_id,
                    "The local workflow is in progress.",
                ),
            )
            row = connection.execute(
                "SELECT * FROM job_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - protected by the transaction above
            raise RuntimeError("job run disappeared during start")
        return JobStart(_job_run_from_row(row), tuple(interrupted_ids))

    def finish_job(
        self,
        run_id: str,
        *,
        state: str,
        detail: str,
        payload: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> JobRun:
        """Finish exactly one running job as success or failure."""
        if state not in {"success", "failed"}:
            raise ValueError("finished job state must be 'success' or 'failed'")
        _validate_job_text("run_id", run_id)
        _validate_job_text("detail", detail)
        timestamp = _timestamp(now)
        payload_json = _payload_json(payload or {})
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                UPDATE job_runs
                SET state = ?, finished_at = ?, detail = ?, payload_json = ?
                WHERE run_id = ? AND state = 'running'
                RETURNING *
                """,
                (state, timestamp, detail, payload_json, run_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"job run is missing or no longer running: {run_id}")
        return _job_run_from_row(row)

    def latest_job(self, job_name: str) -> JobRun | None:
        """Return the newest durable lifecycle record for one workflow."""
        _validate_job_text("job_name", job_name)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM job_runs
                WHERE job_name = ?
                ORDER BY started_at DESC, rowid DESC
                LIMIT 1
                """,
                (job_name,),
            ).fetchone()
        return _job_run_from_row(row) if row is not None else None

    def _enqueue_incident_notification(
        self,
        connection: sqlite3.Connection,
        incident: sqlite3.Row,
        *,
        event_type: str,
        timestamp: str,
        source_event_id: str,
    ) -> sqlite3.Row:
        incident_id = str(incident["incident_id"])
        occurrence_count = int(incident["occurrence_count"])
        original_title = str(incident["title"])
        if event_type == "resolved":
            severity = AlertSeverity.INFO
            title = f"Recovered: {original_title}"
            body = "The previously reported operating condition is now resolved."
        elif event_type == "reopened":
            severity = AlertSeverity(str(incident["severity"]))
            title = f"Reopened: {original_title}"
            body = str(incident["body"])
        elif event_type == "escalated":
            severity = AlertSeverity(str(incident["severity"]))
            title = f"Escalated: {original_title}"
            body = str(incident["body"])
        elif event_type == "active":
            severity = AlertSeverity(str(incident["severity"]))
            title = f"Active incident: {original_title}"
            body = str(incident["body"])
        else:
            severity = AlertSeverity(str(incident["severity"]))
            title = original_title
            body = str(incident["body"])
        route = self._enabled_notification_route(connection)
        route_activation_id = (
            str(route["activation_id"]) if route is not None else None
        )
        depends_on_notification_id = None
        held = route is None
        hold_reason = "route_disabled" if held else None
        if event_type == "resolved" and route is not None:
            cycle_anchor = connection.execute(
                """
                SELECT created_at, rowid
                FROM incident_events
                WHERE incident_id = ?
                  AND event_type IN ('opened','reopened')
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (incident_id,),
            ).fetchone()
            if cycle_anchor is not None:
                dependency = connection.execute(
                    """
                    SELECT notification_id
                    FROM notification_outbox
                    WHERE incident_id = ?
                      AND route_activation_id = ?
                      AND event_type IN (
                          'active','opened','reopened','escalated'
                      )
                      AND state IN ('pending','leased','delivered')
                      AND created_at >= ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT 1
                    """,
                    (
                        incident_id,
                        route_activation_id,
                        cycle_anchor["created_at"],
                    ),
                ).fetchone()
                if dependency is not None:
                    depends_on_notification_id = str(
                        dependency["notification_id"]
                    )
            held = depends_on_notification_id is None
            if held:
                hold_reason = "active_notification_not_delivered"
        incident_payload = json.loads(str(incident["payload_json"]))
        return self._insert_outbox_message(
            connection,
            NotificationRequest(
                idempotency_key=(
                    f"incident-event:{source_event_id}"
                ),
                incident_id=incident_id,
                source_event_id=source_event_id,
                event_type=event_type,
                severity=severity,
                title=title,
                body=body,
                source_job=str(incident["source_job"]),
                payload={
                    "incident": incident_payload,
                    "incident_event": event_type,
                    "incident_state": str(incident["state"]),
                    "occurrence_count": occurrence_count,
                },
            ),
            timestamp=timestamp,
            held=held,
            route_activation_id=route_activation_id,
            depends_on_notification_id=depends_on_notification_id,
            last_error_type=hold_reason,
        )

    @staticmethod
    def _enabled_notification_route(
        connection: sqlite3.Connection,
    ) -> sqlite3.Row | None:
        rows = connection.execute(
            """
            SELECT * FROM notification_routes
            WHERE state = 'enabled'
            ORDER BY rowid
            LIMIT 2
            """
        ).fetchall()
        if len(rows) > 1:
            raise RuntimeError(
                "multiple external notification routes are enabled; incident write refused"
            )
        return rows[0] if rows else None

    @staticmethod
    def _insert_outbox_message(
        connection: sqlite3.Connection,
        request: NotificationRequest,
        *,
        timestamp: str,
        held: bool,
        route_activation_id: str | None = None,
        depends_on_notification_id: str | None = None,
        last_error_type: str | None = None,
    ) -> sqlite3.Row:
        _validate_notification_request(request)
        payload_json = _payload_json(request.payload)
        existing = connection.execute(
            "SELECT * FROM notification_outbox WHERE idempotency_key = ?",
            (request.idempotency_key,),
        ).fetchone()
        if existing is not None:
            expected = (
                request.incident_id,
                request.source_event_id,
                route_activation_id,
                depends_on_notification_id,
                request.event_type,
                request.severity.value,
                request.title,
                request.body,
                request.source_job,
                payload_json,
            )
            observed = (
                existing["incident_id"],
                existing["source_event_id"],
                existing["route_activation_id"],
                existing["depends_on_notification_id"],
                existing["event_type"],
                existing["severity"],
                existing["title"],
                existing["body"],
                existing["source_job"],
                existing["payload_json"],
            )
            if observed != expected:
                raise ValueError(
                    "notification idempotency key conflicts with different content"
                )
            return existing

        notification_id = f"notification-{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO notification_outbox (
                notification_id, idempotency_key, incident_id, source_event_id,
                route_activation_id, depends_on_notification_id,
                message_schema_version, event_type, severity, title, body,
                source_job, payload_json, state,
                created_at, available_at, attempt_count, lease_token,
                lease_expires_at, delivered_at, dead_lettered_at,
                last_error_type
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0,
                      NULL, NULL, NULL, NULL, ?)
            """,
            (
                notification_id,
                request.idempotency_key,
                request.incident_id,
                request.source_event_id,
                route_activation_id,
                depends_on_notification_id,
                request.event_type,
                request.severity.value,
                request.title,
                request.body,
                request.source_job,
                payload_json,
                "held" if held else "pending",
                timestamp,
                timestamp,
                last_error_type,
            ),
        )
        row = connection.execute(
            "SELECT * FROM notification_outbox WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        if row is None:  # pragma: no cover
            raise RuntimeError("notification disappeared during enqueue")
        return row

    @staticmethod
    def _recover_expired_notification_leases(
        connection: sqlite3.Connection,
        *,
        timestamp: str,
    ) -> None:
        expired = connection.execute(
            """
            SELECT * FROM notification_outbox
            WHERE state = 'leased' AND lease_expires_at <= ?
            ORDER BY lease_expires_at, rowid
            """,
            (timestamp,),
        ).fetchall()
        for row in expired:
            connection.execute(
                """
                UPDATE notification_deliveries
                SET state = 'ambiguous', finished_at = ?,
                    error_type = 'lease_expired_outcome_unknown', retry_at = NULL,
                    provider_response_json = ?
                WHERE notification_id = ? AND attempt_number = ?
                  AND state = 'started'
                """,
                (
                    timestamp,
                    _provider_response_json(
                        {
                            "delivery_phase": "worker",
                            "provider_state": "outcome_unknown",
                        }
                    ),
                    row["notification_id"],
                    int(row["attempt_count"]),
                ),
            )
            connection.execute(
                """
                UPDATE notification_outbox
                SET state = 'dead_letter', available_at = ?, lease_token = NULL,
                    lease_expires_at = NULL, delivered_at = NULL,
                    dead_lettered_at = ?,
                    last_error_type = 'lease_expired_outcome_unknown'
                WHERE notification_id = ?
                """,
                (
                    timestamp,
                    timestamp,
                    row["notification_id"],
                ),
            )
        connection.execute(
            f"""
            UPDATE notification_outbox
            SET state = 'dead_letter', dead_lettered_at = ?,
                last_error_type = COALESCE(
                    last_error_type, 'attempt_limit_exhausted'
                )
            WHERE state = 'pending'
              AND attempt_count >= {NOTIFICATION_MAX_ATTEMPTS}
            """,
            (timestamp,),
        )
        AlertStore._hold_unreachable_recoveries(connection)

    @staticmethod
    def _hold_unreachable_recoveries(connection: sqlite3.Connection) -> None:
        """Prevent recovery delivery when its exact active dependency failed."""
        connection.execute(
            """
            UPDATE notification_outbox
            SET state = 'held',
                last_error_type = 'active_notification_not_delivered'
            WHERE state = 'pending'
              AND event_type = 'resolved'
              AND depends_on_notification_id IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM notification_outbox AS dependency
                  WHERE dependency.notification_id =
                        notification_outbox.depends_on_notification_id
                    AND dependency.state = 'dead_letter'
              )
            """
        )

    @staticmethod
    def _resolve_notification_id(
        connection: sqlite3.Connection,
        reference: str,
    ) -> str:
        value = reference.strip()
        if not value:
            raise ValueError("notification reference is required")
        rows = connection.execute(
            """
            SELECT notification_id FROM notification_outbox
            WHERE notification_id = ?
               OR substr(notification_id, 1, length(?)) = ?
            ORDER BY CASE WHEN notification_id = ? THEN 0 ELSE 1 END
            LIMIT 2
            """,
            (value, value, value, value),
        ).fetchall()
        if not rows:
            raise ValueError(f"unknown notification: {reference}")
        if len(rows) > 1 and all(
            str(row["notification_id"]) != value for row in rows
        ):
            raise ValueError(f"notification reference is ambiguous: {reference}")
        return str(rows[0]["notification_id"])

    @staticmethod
    def _resolve_incident_id(connection: sqlite3.Connection, reference: str) -> str:
        value = reference.strip()
        if not value:
            raise ValueError("incident reference is required")
        rows = connection.execute(
            """
            SELECT incident_id FROM incidents
            WHERE incident_id = ? OR substr(incident_id, 1, length(?)) = ?
            ORDER BY CASE WHEN incident_id = ? THEN 0 ELSE 1 END
            LIMIT 2
            """,
            (value, value, value, value),
        ).fetchall()
        if not rows:
            raise ValueError(f"unknown incident: {reference}")
        if len(rows) > 1 and all(str(row["incident_id"]) != value for row in rows):
            raise ValueError(f"incident reference is ambiguous: {reference}")
        return str(rows[0]["incident_id"])

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        incident_id: str,
        event_type: str,
        timestamp: str,
        payload_json: str,
    ) -> str:
        event_id = f"evt-{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO incident_events (
                event_id, incident_id, event_type, created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (event_id, incident_id, event_type, timestamp, payload_json),
        )
        return event_id

    @staticmethod
    def _insert_notification_route_event(
        connection: sqlite3.Connection,
        *,
        route_id: str,
        event_type: str,
        timestamp: str,
        activation_id: str,
        config_fingerprint: str,
    ) -> str:
        if event_type not in NOTIFICATION_ROUTE_EVENT_TYPES:
            raise ValueError(f"unsupported notification route event: {event_type}")
        event_id = f"route-event-{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO notification_route_events (
                event_id, route_id, event_type, created_at,
                activation_id, config_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                route_id,
                event_type,
                timestamp,
                activation_id,
                config_fingerprint,
            ),
        )
        return event_id


def get_alert_store(
    path: Path | None = None,
    *,
    read_only: bool = False,
) -> AlertStore:
    """Open the independent operations ledger at its configured local path."""
    configured = path or settings.operations_db_path
    resolved = configured if configured.is_absolute() else settings.project_root / configured
    return AlertStore(resolved, read_only=read_only)


def record_systemd_failure(
    unit: str,
    *,
    store: AlertStore | None = None,
    runner: Any = subprocess.run,
) -> Incident:
    """Capture safe systemd result fields without storing raw journal or environment text."""
    _validate_managed_unit(unit)
    properties = _systemd_properties(unit, runner=runner)
    result = properties.get("Result", "unknown")
    status = properties.get("ExecMainStatus", "unknown")
    return (store or get_alert_store()).emit(
        Alert(
            code="scheduler_service_failed",
            severity=AlertSeverity.CRITICAL,
            title="Scheduled AIOS job failed",
            body=f"{unit} ended with result {result} and exit status {status}.",
            dedup_key=f"systemd:{unit}",
            source_job=unit,
            payload={"systemd": properties},
        )
    )


def record_systemd_recovery(unit: str, *, store: AlertStore | None = None) -> Incident | None:
    """Resolve the corresponding service incident after a later clean run."""
    _validate_managed_unit(unit)
    return (store or get_alert_store()).resolve_fingerprint(f"systemd:{unit}")


def _systemd_properties(unit: str, *, runner: Any) -> dict[str, str]:
    command = ["systemctl", "--user", "show", unit]
    for name in SYSTEMD_FAILURE_PROPERTIES:
        command.extend(("--property", name))
    executed = command
    timeout = STATUS_QUERY_TIMEOUT_SECONDS
    if runner is subprocess.run:
        executed = [
            "timeout",
            "--kill-after=1s",
            f"{STATUS_QUERY_TIMEOUT_SECONDS:g}s",
            *command,
        ]
        timeout += 2.0
    try:
        completed = runner(
            executed,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status_capture": "unavailable"}
    if completed.returncode:
        return {"status_capture": "unavailable"}
    properties: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in SYSTEMD_FAILURE_PROPERTIES and value.strip():
            properties[key] = value.strip()
    return properties or {"status_capture": "empty"}


def _validate_managed_unit(unit: str) -> None:
    if unit not in MANAGED_SERVICE_NAMES:
        raise ValueError(f"refusing alert event for unmanaged service: {unit}")


def _validate_alert(alert: Alert) -> None:
    if not isinstance(alert.severity, AlertSeverity):
        raise ValueError("alert severity is invalid")
    if not isinstance(alert.notify, bool):
        raise ValueError("alert notify flag must be boolean")
    for label, value in (
        ("code", alert.code),
        ("title", alert.title),
        ("body", alert.body),
        ("dedup_key", alert.dedup_key),
        ("source_job", alert.source_job),
    ):
        if not value.strip():
            raise ValueError(f"alert {label} is required")
        if len(value) > 4000:
            raise ValueError(f"alert {label} is too long")


def _validate_notification_request(request: NotificationRequest) -> None:
    if not isinstance(request.severity, AlertSeverity):
        raise ValueError("notification severity is invalid")
    if request.event_type not in NOTIFICATION_EVENT_TYPES:
        raise ValueError(f"unsupported notification event type: {request.event_type}")
    if request.incident_id is not None:
        _validate_bounded_text("incident_id", request.incident_id)
    if request.source_event_id is not None:
        _validate_bounded_text("source_event_id", request.source_event_id)
    if request.source_event_id is not None and request.incident_id is None:
        raise ValueError("a source event requires an incident")
    for label, value in (
        ("idempotency_key", request.idempotency_key),
        ("title", request.title),
        ("body", request.body),
        ("source_job", request.source_job),
    ):
        _validate_bounded_text(label, value)
    if not isinstance(request.payload, dict):
        raise ValueError("notification payload must be an object")


def _validate_channel(channel: str) -> None:
    _validate_bounded_text("channel", channel)
    if any(character.isspace() for character in channel):
        raise ValueError("notification channel cannot contain whitespace")


def _validate_config_fingerprint(config_fingerprint: str) -> None:
    value = config_fingerprint.strip().lower()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("notification configuration fingerprint must be SHA-256")


def _validate_job_text(label: str, value: str) -> None:
    try:
        _validate_bounded_text(label, value)
    except ValueError as exc:
        raise ValueError(f"job {exc}") from exc


def _validate_bounded_text(label: str, value: str) -> None:
    if not str(value).strip():
        raise ValueError(f"{label} is required")
    if len(str(value)) > 4000:
        raise ValueError(f"{label} is too long")


def _payload_json(payload: dict[str, Any]) -> str:
    sanitized = _redact(payload)
    encoded = json.dumps(sanitized, sort_keys=True, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        return json.dumps(
            {"payload_truncated": True, "original_bytes": len(encoded.encode("utf-8"))},
            sort_keys=True,
            separators=(",", ":"),
        )
    return encoded


def _provider_response_json(response: dict[str, Any]) -> str:
    if not isinstance(response, dict):
        raise ValueError("provider response metadata must be an object")
    unknown = sorted(set(response) - SAFE_PROVIDER_RESPONSE_FIELDS)
    if unknown:
        raise ValueError(
            "provider response metadata contains unsupported fields: "
            + ", ".join(unknown)
        )
    for key, value in response.items():
        if value is not None and not isinstance(value, (bool, int, str)):
            raise ValueError(f"provider response field {key} must be a safe scalar")
    return _payload_json(response)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if any(marker in str(key).lower() for marker in _SECRET_MARKERS)
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _timestamp(value: datetime | None) -> str:
    moment = (value or datetime.now(UTC)).astimezone(UTC)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _higher_severity(left: str, right: str) -> str:
    rank = {"info": 0, "warning": 1, "critical": 2}
    return left if rank[left] >= rank[right] else right


def _notification_retry_at(moment: datetime, attempt_number: int) -> str:
    delay = min(
        NOTIFICATION_RETRY_BASE_SECONDS * (2 ** max(attempt_number - 1, 0)),
        NOTIFICATION_RETRY_MAX_SECONDS,
    )
    return _timestamp(moment + timedelta(seconds=delay))


def _incident_from_row(row: sqlite3.Row) -> Incident:
    return Incident(
        incident_id=str(row["incident_id"]),
        fingerprint=str(row["fingerprint"]),
        code=str(row["code"]),
        severity=str(row["severity"]),
        title=str(row["title"]),
        body=str(row["body"]),
        source_job=str(row["source_job"]),
        state=str(row["state"]),
        first_seen_at=str(row["first_seen_at"]),
        last_seen_at=str(row["last_seen_at"]),
        occurrence_count=int(row["occurrence_count"]),
        payload=json.loads(row["payload_json"]),
        acknowledged_at=(
            str(row["acknowledged_at"]) if row["acknowledged_at"] is not None else None
        ),
        resolved_at=str(row["resolved_at"]) if row["resolved_at"] is not None else None,
        notifications_enabled=bool(row["notifications_enabled"]),
    )


def _notification_from_row(row: sqlite3.Row) -> NotificationMessage:
    return NotificationMessage(
        notification_id=str(row["notification_id"]),
        idempotency_key=str(row["idempotency_key"]),
        incident_id=(
            str(row["incident_id"]) if row["incident_id"] is not None else None
        ),
        source_event_id=(
            str(row["source_event_id"])
            if row["source_event_id"] is not None
            else None
        ),
        route_activation_id=(
            str(row["route_activation_id"])
            if row["route_activation_id"] is not None
            else None
        ),
        depends_on_notification_id=(
            str(row["depends_on_notification_id"])
            if row["depends_on_notification_id"] is not None
            else None
        ),
        message_schema_version=int(row["message_schema_version"]),
        event_type=str(row["event_type"]),
        severity=str(row["severity"]),
        title=str(row["title"]),
        body=str(row["body"]),
        source_job=str(row["source_job"]),
        payload=json.loads(row["payload_json"]),
        state=str(row["state"]),
        created_at=str(row["created_at"]),
        available_at=str(row["available_at"]),
        attempt_count=int(row["attempt_count"]),
        lease_token=str(row["lease_token"]) if row["lease_token"] is not None else None,
        lease_expires_at=(
            str(row["lease_expires_at"])
            if row["lease_expires_at"] is not None
            else None
        ),
        delivered_at=(
            str(row["delivered_at"]) if row["delivered_at"] is not None else None
        ),
        dead_lettered_at=(
            str(row["dead_lettered_at"])
            if row["dead_lettered_at"] is not None
            else None
        ),
        last_error_type=(
            str(row["last_error_type"])
            if row["last_error_type"] is not None
            else None
        ),
    )


def _delivery_from_row(row: sqlite3.Row) -> NotificationDelivery:
    return NotificationDelivery(
        delivery_id=str(row["delivery_id"]),
        notification_id=str(row["notification_id"]),
        attempt_number=int(row["attempt_number"]),
        channel=str(row["channel"]),
        route_alias=str(row["route_alias"]),
        state=str(row["state"]),
        started_at=str(row["started_at"]),
        finished_at=(
            str(row["finished_at"]) if row["finished_at"] is not None else None
        ),
        provider_message_id=(
            str(row["provider_message_id"])
            if row["provider_message_id"] is not None
            else None
        ),
        provider_response=json.loads(row["provider_response_json"]),
        error_type=(
            str(row["error_type"]) if row["error_type"] is not None else None
        ),
        retry_at=str(row["retry_at"]) if row["retry_at"] is not None else None,
    )


def _notification_route_from_row(row: sqlite3.Row) -> NotificationRoute:
    return NotificationRoute(
        route_id=str(row["route_id"]),
        channel=str(row["channel"]),
        route_alias=str(row["route_alias"]),
        activation_id=str(row["activation_id"]),
        state=str(row["state"]),
        config_fingerprint=str(row["config_fingerprint"]),
        enabled_at=(
            str(row["enabled_at"]) if row["enabled_at"] is not None else None
        ),
        disabled_at=(
            str(row["disabled_at"]) if row["disabled_at"] is not None else None
        ),
        updated_at=str(row["updated_at"]),
    )


def _job_run_from_row(row: sqlite3.Row) -> JobRun:
    return JobRun(
        run_id=str(row["run_id"]),
        job_name=str(row["job_name"]),
        state=str(row["state"]),
        target_session=str(row["target_session"]),
        started_at=str(row["started_at"]),
        finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
        owner_pid=int(row["owner_pid"]),
        owner_boot_id=str(row["owner_boot_id"]),
        detail=str(row["detail"]),
        payload=json.loads(row["payload_json"]),
    )


def _current_boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return "unavailable"
    return value or "unavailable"


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

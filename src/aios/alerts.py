"""Durable local incidents that remain writable when the analytical DB is unavailable."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from aios.config import settings
from aios.scheduler import (
    MANAGED_SERVICE_NAMES,
    STATUS_QUERY_TIMEOUT_SECONDS,
    TIMER_NAMES,
)

ALERT_SCHEMA_VERSION = 7
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
MANUAL_INCIDENT_RESOLUTION_OUTCOMES = {
    "verified_recovery",
    "false_positive",
}
INCIDENT_RESOLUTION_PROOF_STATUSES = {
    "not_applicable",
    "manual_verified_recovery",
    "manual_false_positive",
    "producer_verified_recovery",
    "legacy_unproven",
    "invalid",
}
PRODUCER_RECOVERY_PROOF_KINDS = {
    "daily_cycle_certified_v3",
    "fundamentals_review_reconciled",
    "scheduler_runtime_verified",
}
INCIDENT_ACTION_AUDIT_KEY = "_aios_incident_action_audit_v1"
INCIDENT_PRODUCER_RECOVERY_KEY = "_aios_incident_recovery_proof_v1"
REQUIRED_INCIDENT_TRIGGERS = frozenset(
    {
        "incident_events_no_delete",
        "incident_events_no_update",
        "incident_events_resolution_proof_required",
    }
)
SYNTHETIC_NON_GATING_INCIDENTS = {
    ("local_alert_test", "test:local-alert-path"),
}
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
ANOMALY_SEVERITIES = {"low", "medium", "high", "critical"}
ANOMALY_CONFIDENCES = {"low", "medium", "high"}
ANOMALY_CASE_STATES = {"open", "acknowledged", "deferred", "resolved"}
ANOMALY_RESOLUTION_OUTCOMES = {
    "accepted",
    "source_corrected",
    "mapping_corrected",
    "false_positive",
    "deferred",
}
SEC_FUNDAMENTALS_COVERAGE_RULE = "sec_fundamentals_coverage_missing@1.0.0"
SEC_PRE_PERIODIC_ACCEPTANCE_CONTRACT = "sec-pre-periodic-issuer.v1"
SEC_EMPTY_ROWSET_SHA256 = hashlib.sha256(b"[]").hexdigest()
ANOMALY_EVENT_TYPES = {
    "opened",
    "evidence_changed",
    "reopened",
    "acknowledged",
    "deferred",
    "resolved",
}
SEC_SOURCE_BOUNDARY_POLICY_V2 = {
    "policy_id": "sec-consumed-raw-snapshot-max-received-at",
    "version": 2,
    "evidence_scope": "snapshots_consumed_by_scan",
    "watermark": "maximum_received_at",
    "clock": "UTC",
}
SEC_SOURCE_BOUNDARY_POLICY_V1_TO_V2 = {
    "from_policy": {
        "policy_id": "sec-global-raw-snapshot-max-received-at",
        "version": 1,
    },
    "to_policy": SEC_SOURCE_BOUNDARY_POLICY_V2,
    "reason": "exclude_raw_snapshots_not_consumed_by_the_scan",
}


def _stable_read_only_database_identity(
    path: Path,
) -> tuple[int, int, int, int, int]:
    """Return main-file identity only when no WAL evidence is pending."""
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"operations database is not initialized: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"operations database must remain a regular file: {path}")
    wal_path = Path(f"{path}-wal")
    try:
        wal_metadata = wal_path.lstat()
    except FileNotFoundError:
        wal_metadata = None
    if wal_metadata is not None:
        if not stat.S_ISREG(wal_metadata.st_mode):
            raise RuntimeError(
                "operations database WAL sidecar is not a regular file"
            )
        if wal_metadata.st_size:
            raise RuntimeError(
                "operations database has an uncheckpointed WAL; retry after the "
                "writer closes"
            )
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def canonical_anomaly_fingerprint(
    *,
    rule_id: str,
    rule_version: str,
    scope: str,
    subject_type: str,
    subject_id: str,
) -> str:
    """Return the sole durable case identity for one normalized rule subject."""

    identity = {
        "rule_id": _anomaly_text("anomaly rule id", rule_id),
        "rule_version": _anomaly_text("anomaly rule version", rule_version),
        "scope": _anomaly_text("anomaly scope", scope),
        "subject_type": _anomaly_text("anomaly subject type", subject_type),
        "subject_id": _anomaly_text("anomaly subject id", subject_id),
    }
    canonical = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return "dqf-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    evidence_sha256: str
    resolution_proof_status: str
    operationally_blocking: bool


@dataclass(frozen=True)
class IncidentRecoveryContext:
    """Exact current incident generation required by a producer attestation."""

    incident_id: str
    fingerprint: str
    generation_event_id: str
    generation_created_at: str
    latest_resolution_created_at: str | None
    evidence_sha256: str


@dataclass(frozen=True)
class ProducerRecoveryEvidence:
    """Bounded positive observation bound to one exact incident generation."""

    incident_id: str
    fingerprint: str
    generation_event_id: str
    expected_evidence_sha256: str
    producer: str
    proof_kind: str
    observed_at: str | datetime
    observation: dict[str, Any]


@dataclass(frozen=True)
class IncidentResolutionAssessment:
    """Derived proof status for the latest append-ordered incident generation."""

    resolution_proof_status: str
    operationally_blocking: bool
    generation_event_id: str | None


@dataclass(frozen=True)
class _PreparedProducerRecovery:
    incident_id: str
    fingerprint: str
    generation_event_id: str
    expected_evidence_sha256: str
    producer: str
    proof_kind: str
    observed_at: str
    observation: dict[str, Any]
    observation_sha256: str


@dataclass(frozen=True)
class AnomalyObservation:
    """One rule finding with bounded, source-linked comparison evidence."""

    fingerprint: str
    rule_id: str
    rule_version: str
    scope: str
    subject_type: str
    subject_id: str
    severity: str
    confidence: str
    title: str
    summary: str
    old_value: dict[str, Any]
    new_value: dict[str, Any]
    evidence: dict[str, Any]
    suggested_checks: tuple[str, ...]


@dataclass(frozen=True)
class AnomalyScan:
    """One complete comparable rule-bundle scan at an immutable source boundary."""

    scan_id: str
    rule_bundle_version: str
    scope: str
    source_boundary_sha256: str
    source_boundary_at: str | datetime
    executed_rules: tuple[str, ...]
    observations: tuple[AnomalyObservation, ...]
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AnomalyCase:
    """Current governed review state for one stable anomaly fingerprint."""

    case_id: str
    fingerprint: str
    rule_id: str
    rule_version: str
    scope: str
    subject_type: str
    subject_id: str
    severity: str
    confidence: str
    title: str
    summary: str
    state: str
    owner: str | None
    first_seen_at: str
    last_seen_at: str
    occurrence_count: int
    old_value: dict[str, Any]
    new_value: dict[str, Any]
    evidence: dict[str, Any]
    suggested_checks: tuple[str, ...]
    evidence_sha256: str
    last_scan_id: str
    acknowledged_at: str | None
    resolution_outcome: str | None
    resolution_note: str | None
    resolved_at: str | None
    next_review_at: str | None
    verification_scan_id: str | None


@dataclass(frozen=True)
class _PreparedAnomalyObservation:
    observation: AnomalyObservation
    payload_json: str
    payload_sha256: str


@dataclass(frozen=True)
class _PreparedAnomalyScan:
    scan: AnomalyScan
    payload_sha256: str
    evidence_json: str
    observed_fingerprints_json: str
    observations: tuple[_PreparedAnomalyObservation, ...]


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
        with self._connect() as connection:
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            incident_triggers = {
                str(row["name"])
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_schema
                    WHERE type = 'trigger' AND name LIKE 'incident_events_%'
                    """
                ).fetchall()
            }
        if current_version != ALERT_SCHEMA_VERSION:
            raise ValueError(
                f"operations database schema {current_version} does not match "
                f"required schema {ALERT_SCHEMA_VERSION}; run a state-changing "
                "operations command to perform any supported migration"
            )
        missing_triggers = sorted(
            REQUIRED_INCIDENT_TRIGGERS - incident_triggers
        )
        if missing_triggers:
            raise ValueError(
                "operations database incident proof schema is incomplete; run a "
                "state-changing operations command to restore: "
                + ", ".join(missing_triggers)
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
            anomaly_event_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(anomaly_case_events)"
                ).fetchall()
            }
            anomaly_case_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(anomaly_cases)"
                ).fetchall()
            }
            if anomaly_case_columns:
                identity_columns = {
                    "case_id",
                    "fingerprint",
                    "rule_id",
                    "rule_version",
                    "scope",
                    "subject_type",
                    "subject_id",
                }
                if not identity_columns <= anomaly_case_columns:
                    raise RuntimeError(
                        "anomaly case identity schema is incomplete; "
                        "migration refused"
                    )
                for case in connection.execute(
                    """
                    SELECT case_id, fingerprint, rule_id, rule_version, scope,
                           subject_type, subject_id
                    FROM anomaly_cases
                    """
                ).fetchall():
                    self._verify_canonical_anomaly_case_identity(case)
            anomaly_event_sequence_migration = ""
            if (
                anomaly_event_columns
                and current_version < ALERT_SCHEMA_VERSION
            ):
                anomaly_event_sequence_migration = """
                DROP TRIGGER IF EXISTS anomaly_case_events_no_update;
                DROP INDEX IF EXISTS anomaly_case_events_case_created_idx;
                """
                if "event_sequence" not in anomaly_event_columns:
                    anomaly_event_sequence_migration += """
                    ALTER TABLE anomaly_case_events
                    ADD COLUMN event_sequence INTEGER;
                    """
                anomaly_event_sequence_migration += """
                UPDATE anomaly_case_events
                SET event_sequence = rowid
                WHERE event_sequence IS NULL;
                """
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
                CREATE TRIGGER IF NOT EXISTS incident_events_no_update
                BEFORE UPDATE ON incident_events
                BEGIN
                    SELECT RAISE(ABORT, 'incident history is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS incident_events_no_delete
                BEFORE DELETE ON incident_events
                BEGIN
                    SELECT RAISE(ABORT, 'incident history is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS
                    incident_events_resolution_proof_required
                BEFORE INSERT ON incident_events
                WHEN NEW.event_type = 'resolved'
                 AND CASE
                    WHEN json_valid(NEW.payload_json) = 0 THEN 1
                    WHEN (
                        COALESCE(
                            json_type(
                                NEW.payload_json,
                                '$._aios_incident_action_audit_v1'
                            ) = 'object',
                            0
                        )
                        +
                        COALESCE(
                            json_type(
                                NEW.payload_json,
                                '$._aios_incident_recovery_proof_v1'
                            ) = 'object',
                            0
                        )
                    ) != 1 THEN 1
                    ELSE 0
                 END
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'resolved incident event requires exactly one audit proof'
                    );
                END;
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
                CREATE TABLE IF NOT EXISTS anomaly_scans (
                    scan_id TEXT PRIMARY KEY,
                    payload_sha256 TEXT NOT NULL
                        CHECK (length(payload_sha256) = 64),
                    rule_bundle_version TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    source_boundary_sha256 TEXT NOT NULL
                        CHECK (length(source_boundary_sha256) = 64),
                    source_boundary_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    recorded_sequence INTEGER NOT NULL UNIQUE
                        CHECK (recorded_sequence > 0),
                    observation_count INTEGER NOT NULL
                        CHECK (observation_count >= 0),
                    evidence_json TEXT NOT NULL,
                    observed_fingerprints_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS anomaly_scans_recorded_idx
                    ON anomaly_scans(recorded_sequence DESC);
                CREATE TABLE IF NOT EXISTS anomaly_cases (
                    case_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    rule_id TEXT NOT NULL,
                    rule_version TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    severity TEXT NOT NULL CHECK (
                        severity IN ('low','medium','high','critical')
                    ),
                    confidence TEXT NOT NULL CHECK (
                        confidence IN ('low','medium','high')
                    ),
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('open','acknowledged','deferred','resolved')
                    ),
                    owner TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL CHECK (occurrence_count > 0),
                    current_evidence_sha256 TEXT NOT NULL
                        CHECK (length(current_evidence_sha256) = 64),
                    old_value_json TEXT NOT NULL,
                    new_value_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    suggested_checks_json TEXT NOT NULL,
                    disposition TEXT CHECK (
                        disposition IS NULL OR disposition IN (
                            'accepted','source_corrected','mapping_corrected',
                            'false_positive','deferred'
                        )
                    ),
                    resolution_note TEXT,
                    resolution_actor TEXT,
                    resolved_at TEXT,
                    next_review_at TEXT,
                    last_scan_id TEXT NOT NULL REFERENCES anomaly_scans(scan_id),
                    acknowledged_at TEXT,
                    verification_scan_id TEXT REFERENCES anomaly_scans(scan_id),
                    CHECK (
                        (state = 'open' AND owner IS NULL
                         AND disposition IS NULL AND resolution_note IS NULL
                         AND resolution_actor IS NULL AND resolved_at IS NULL
                         AND next_review_at IS NULL AND acknowledged_at IS NULL)
                        OR
                        (state = 'acknowledged' AND owner IS NOT NULL
                         AND disposition IS NULL AND resolution_note IS NULL
                         AND resolution_actor IS NULL AND resolved_at IS NULL
                         AND next_review_at IS NULL
                         AND acknowledged_at IS NOT NULL)
                        OR
                        (state = 'deferred' AND owner IS NOT NULL
                         AND disposition = 'deferred'
                         AND resolution_note IS NOT NULL
                         AND resolution_actor IS NOT NULL
                         AND resolved_at IS NULL AND next_review_at IS NOT NULL)
                        OR
                        (state = 'resolved' AND owner IS NOT NULL
                         AND disposition IN (
                             'accepted','source_corrected',
                             'mapping_corrected','false_positive'
                         )
                         AND resolution_note IS NOT NULL
                         AND resolution_actor IS NOT NULL
                         AND resolved_at IS NOT NULL AND next_review_at IS NULL)
                    )
                );
                CREATE INDEX IF NOT EXISTS anomaly_cases_state_severity_idx
                    ON anomaly_cases(state, severity, first_seen_at, case_id);
                CREATE INDEX IF NOT EXISTS anomaly_cases_subject_idx
                    ON anomaly_cases(rule_id, scope, subject_type, subject_id);
                CREATE TABLE IF NOT EXISTS anomaly_case_events (
                    event_id TEXT PRIMARY KEY,
                    event_sequence INTEGER NOT NULL UNIQUE
                        CHECK (event_sequence > 0),
                    case_id TEXT NOT NULL REFERENCES anomaly_cases(case_id),
                    scan_id TEXT REFERENCES anomaly_scans(scan_id),
                    event_type TEXT NOT NULL CHECK (
                        event_type IN (
                            'opened','evidence_changed','reopened',
                            'acknowledged','deferred','resolved'
                        )
                    ),
                    created_at TEXT NOT NULL,
                    actor TEXT,
                    note TEXT,
                    disposition TEXT CHECK (
                        disposition IS NULL OR disposition IN (
                            'accepted','source_corrected','mapping_corrected',
                            'false_positive','deferred'
                        )
                    ),
                    observation_sha256 TEXT,
                    payload_json TEXT NOT NULL
                );
                {anomaly_event_sequence_migration}
                CREATE INDEX IF NOT EXISTS anomaly_case_events_case_created_idx
                    ON anomaly_case_events(case_id, event_sequence DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS
                    anomaly_case_events_sequence_unique
                    ON anomaly_case_events(event_sequence);
                DROP INDEX IF EXISTS anomaly_case_observations_unique;
                CREATE UNIQUE INDEX IF NOT EXISTS anomaly_case_scan_observation_unique
                    ON anomaly_case_events(case_id, scan_id)
                    WHERE event_type IN (
                        'opened','evidence_changed','reopened'
                    );
                CREATE TRIGGER IF NOT EXISTS anomaly_scans_no_update
                BEFORE UPDATE ON anomaly_scans
                BEGIN
                    SELECT RAISE(ABORT, 'anomaly scan history is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS anomaly_scans_no_delete
                BEFORE DELETE ON anomaly_scans
                BEGIN
                    SELECT RAISE(ABORT, 'anomaly scan history is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS anomaly_case_events_no_update
                BEFORE UPDATE ON anomaly_case_events
                BEGIN
                    SELECT RAISE(ABORT, 'anomaly case history is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS anomaly_case_events_sequence_required
                BEFORE INSERT ON anomaly_case_events
                WHEN NEW.event_sequence IS NULL OR NEW.event_sequence <= 0
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'anomaly case event sequence is required'
                    );
                END;
                CREATE TRIGGER IF NOT EXISTS anomaly_case_events_no_delete
                BEFORE DELETE ON anomaly_case_events
                BEGIN
                    SELECT RAISE(ABORT, 'anomaly case history is append-only');
                END;
                PRAGMA user_version = {ALERT_SCHEMA_VERSION};
                COMMIT;
                """
            )
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        read_identity: tuple[int, int, int, int, int] | None = None
        if self.read_only:
            read_identity = _stable_read_only_database_identity(self.path)
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
            if read_identity is not None:
                current_identity = _stable_read_only_database_identity(self.path)
                if current_identity != read_identity:
                    raise RuntimeError(
                        "operations database changed during a read-only query; retry"
                    )

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

    def acknowledge(
        self,
        incident_id: str,
        *,
        actor: str,
        note: str,
        expected_evidence_sha256: str,
        now: datetime | None = None,
    ) -> Incident:
        """Record explicit reviewed ownership against the exact current evidence."""
        normalized_actor = _incident_action_text("incident actor", actor)
        normalized_note = _incident_action_text("incident acknowledgement note", note)
        expected_evidence = _incident_sha256(
            "expected incident evidence",
            expected_evidence_sha256,
        )
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
            if row["state"] == "acknowledged":
                raise ValueError("incident is already acknowledged")
            current_evidence = _incident_evidence_sha256(row)
            if current_evidence != expected_evidence:
                raise ValueError(
                    "incident evidence changed; inspect the current incident and retry"
                )
            connection.execute(
                """
                UPDATE incidents SET state = 'acknowledged', acknowledged_at = ?
                WHERE incident_id = ?
                """,
                (timestamp, incident_id),
            )
            updated = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if updated is None:  # pragma: no cover - protected by the transaction
                raise RuntimeError("incident disappeared during acknowledgement")
            updated_evidence = _incident_evidence_sha256(updated)
            self._insert_event(
                connection,
                incident_id,
                "acknowledged",
                timestamp,
                _incident_action_payload_json(
                    incident_id=incident_id,
                    event_type="acknowledged",
                    timestamp=timestamp,
                    actor=normalized_actor,
                    note=normalized_note,
                    resolution_outcome=None,
                    expected_evidence_sha256=expected_evidence,
                    resulting_evidence_sha256=updated_evidence,
                ),
            )
        return _incident_from_row(updated)

    def resolve(
        self,
        incident_id: str,
        *,
        actor: str,
        note: str,
        outcome: str,
        expected_evidence_sha256: str,
        now: datetime | None = None,
    ) -> Incident:
        """Close one exact incident generation with an immutable operator audit proof."""
        normalized_actor = _incident_action_text("incident actor", actor)
        normalized_note = _incident_action_text("incident resolution note", note)
        normalized_outcome = _manual_incident_resolution_outcome(outcome)
        expected_evidence = _incident_sha256(
            "expected incident evidence",
            expected_evidence_sha256,
        )
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
                raise ValueError("incident is already resolved")
            current_evidence = _incident_evidence_sha256(row)
            if current_evidence != expected_evidence:
                raise ValueError(
                    "incident evidence changed; inspect the current incident and retry"
                )
            connection.execute(
                """
                UPDATE incidents SET state = 'resolved', resolved_at = ?
                WHERE incident_id = ?
                """,
                (timestamp, incident_id),
            )
            updated = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if updated is None:  # pragma: no cover - protected by the transaction
                raise RuntimeError("incident disappeared during resolution")
            updated_evidence = _incident_evidence_sha256(updated)
            event_id = self._insert_event(
                connection,
                incident_id,
                "resolved",
                timestamp,
                _incident_action_payload_json(
                    incident_id=incident_id,
                    event_type="resolved",
                    timestamp=timestamp,
                    actor=normalized_actor,
                    note=normalized_note,
                    resolution_outcome=normalized_outcome,
                    expected_evidence_sha256=expected_evidence,
                    resulting_evidence_sha256=updated_evidence,
                    transitioned_state=True,
                ),
            )
            if (
                bool(row["notifications_enabled"])
            ):
                self._enqueue_incident_notification(
                    connection,
                    updated,
                    event_type="resolved",
                    timestamp=timestamp,
                    source_event_id=event_id,
                )
        return _incident_from_row(
            updated,
            resolution_proof_status=(
                "manual_verified_recovery"
                if normalized_outcome == "verified_recovery"
                else "manual_false_positive"
            ),
        )

    def attest_legacy_resolution(
        self,
        incident_id: str,
        *,
        actor: str,
        note: str,
        outcome: str,
        expected_evidence_sha256: str,
        now: datetime | None = None,
    ) -> Incident:
        """Append proof to one exact legacy resolution without rewriting state."""
        normalized_actor = _incident_action_text("incident actor", actor)
        normalized_note = _incident_action_text(
            "incident resolution attestation note",
            note,
        )
        normalized_outcome = _manual_incident_resolution_outcome(outcome)
        expected_evidence = _incident_sha256(
            "expected incident evidence",
            expected_evidence_sha256,
        )
        timestamp = _timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            incident_id = self._resolve_incident_id(connection, incident_id)
            row = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if row is None:  # pragma: no cover - protected by reference resolution
                raise ValueError(f"unknown incident: {incident_id}")
            if str(row["state"]) != "resolved":
                raise ValueError(
                    "resolution attestation applies only to an already-resolved incident"
                )
            assessment = classify_incident_resolution(connection, row)
            if assessment.resolution_proof_status == "invalid":
                raise ValueError(
                    "incident resolution proof is invalid; preserve it for "
                    "forensic review"
                )
            if assessment.resolution_proof_status != "legacy_unproven":
                raise ValueError(
                    "incident resolution already has a valid proof; attestation "
                    "retry refused"
                )
            current_evidence = _incident_evidence_sha256(row)
            if current_evidence != expected_evidence:
                raise ValueError(
                    "incident evidence changed; inspect the current incident and retry"
                )
            context = _incident_recovery_context(connection, row)
            attestation_moment = _incident_datetime(
                timestamp,
                label="resolution attestation time",
            )
            generation_moment = _incident_datetime(
                context.generation_created_at,
                label="incident generation time",
            )
            if attestation_moment < generation_moment:
                raise ValueError(
                    "resolution attestation cannot predate the current generation"
                )
            if context.latest_resolution_created_at is None:
                raise ValueError(
                    "legacy resolution has no append-ordered resolution event"
                )
            prior_resolution_moment = _incident_datetime(
                context.latest_resolution_created_at,
                label="legacy resolution time",
            )
            if attestation_moment <= prior_resolution_moment:
                raise ValueError(
                    "resolution attestation must be later than the legacy resolution"
                )
            self._insert_event(
                connection,
                incident_id,
                "resolved",
                timestamp,
                _incident_action_payload_json(
                    incident_id=incident_id,
                    event_type="resolved",
                    timestamp=timestamp,
                    actor=normalized_actor,
                    note=normalized_note,
                    resolution_outcome=normalized_outcome,
                    expected_evidence_sha256=expected_evidence,
                    resulting_evidence_sha256=current_evidence,
                    transitioned_state=False,
                ),
            )
            attested = classify_incident_resolution(connection, row)
            expected_status = (
                "manual_verified_recovery"
                if normalized_outcome == "verified_recovery"
                else "manual_false_positive"
            )
            if (
                attested.resolution_proof_status != expected_status
                or attested.operationally_blocking
            ):
                raise RuntimeError(
                    "resolution attestation failed its post-insert proof check"
                )
        return _incident_from_row(
            row,
            assessment=attested,
        )

    def resolve_fingerprint(
        self,
        fingerprint: str,
        *,
        recovery: ProducerRecoveryEvidence,
        now: datetime | None = None,
    ) -> Incident | None:
        """Record a typed, generation-bound producer recovery attestation."""
        normalized_fingerprint = _incident_action_text(
            "incident fingerprint",
            fingerprint,
        )
        timestamp = _timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM incidents WHERE fingerprint = ?",
                (normalized_fingerprint,),
            ).fetchone()
            if row is None:
                return None
            incident_id = str(row["incident_id"])
            context = _incident_recovery_context(connection, row)
            prepared = _prepare_producer_recovery(
                recovery,
                connection=connection,
                context=context,
                row=row,
                recorded_at=timestamp,
            )
            transitioned_state = str(row["state"]) != "resolved"
            if transitioned_state:
                connection.execute(
                    """
                    UPDATE incidents SET state = 'resolved', resolved_at = ?
                    WHERE incident_id = ?
                    """,
                    (timestamp, incident_id),
                )
            updated = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if updated is None:  # pragma: no cover - protected by the transaction
                raise RuntimeError("incident disappeared during producer recovery")
            resulting_evidence = _incident_evidence_sha256(updated)
            event_id = self._insert_event(
                connection,
                incident_id,
                "resolved",
                timestamp,
                _producer_recovery_payload_json(
                    prepared,
                    resulting_evidence_sha256=resulting_evidence,
                    transitioned_state=transitioned_state,
                ),
            )
            if (
                transitioned_state
                and bool(row["notifications_enabled"])
            ):
                self._enqueue_incident_notification(
                    connection,
                    updated,
                    event_type="resolved",
                    timestamp=timestamp,
                    source_event_id=event_id,
                )
        return _incident_from_row(
            updated,
            resolution_proof_status="producer_verified_recovery",
        )

    def recovery_context(
        self,
        fingerprint: str,
    ) -> IncidentRecoveryContext | None:
        """Return the exact current generation without changing incident state."""
        normalized = _incident_action_text("incident fingerprint", fingerprint)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM incidents WHERE fingerprint = ?",
                (normalized,),
            ).fetchone()
            if row is None:
                return None
            return _incident_recovery_context(connection, row)

    def fundamentals_review_recovery(
        self,
        incident_id: str,
        *,
        expected_evidence_sha256: str,
        observed_at: datetime | None = None,
    ) -> ProducerRecoveryEvidence:
        """Build a read-only proof that every partial-refresh case was reviewed."""

        expected_evidence = _incident_sha256(
            "expected incident evidence",
            expected_evidence_sha256,
        )
        with self._connect() as connection:
            resolved_id = self._resolve_incident_id(connection, incident_id)
            row = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?",
                (resolved_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - protected by reference resolution
                raise ValueError(f"unknown incident: {incident_id}")
            context = _incident_recovery_context(connection, row)
            if context.evidence_sha256 != expected_evidence:
                raise ValueError(
                    "incident evidence changed; inspect the current incident and retry"
                )
            observation = _fundamentals_review_observation(connection, row)
        return ProducerRecoveryEvidence(
            incident_id=context.incident_id,
            fingerprint=context.fingerprint,
            generation_event_id=context.generation_event_id,
            expected_evidence_sha256=context.evidence_sha256,
            producer="aios alert-reconcile-fundamentals",
            proof_kind="fundamentals_review_reconciled",
            observed_at=observed_at or datetime.now(UTC),
            observation=observation,
        )

    def get(self, incident_id: str) -> Incident:
        with self._connect() as connection:
            incident_id = self._resolve_incident_id(connection, incident_id)
            row = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            assessment = (
                classify_incident_resolution(connection, row)
                if row is not None
                else None
            )
        if row is None:
            raise ValueError(f"unknown incident: {incident_id}")
        return _incident_from_row(
            row,
            assessment=assessment,
        )

    def list(
        self,
        *,
        unresolved_only: bool = False,
        blocking_only: bool = False,
        limit: int = 100,
    ) -> list[Incident]:
        if limit < 1 or limit > 1000:
            raise ValueError("incident limit must be between 1 and 1000")
        if unresolved_only and blocking_only:
            raise ValueError(
                "incident list cannot combine unresolved_only and blocking_only"
            )
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
                """,
            ).fetchall()
            incidents = [
                _incident_from_row(
                    row,
                    assessment=classify_incident_resolution(connection, row),
                )
                for row in rows
            ]
        if blocking_only:
            incidents = [
                incident for incident in incidents if incident.operationally_blocking
            ]
        return incidents[:limit]

    def incident_summary(self) -> dict[str, int]:
        """Return exact incident counts independent of the bounded display list."""
        summary = {
            "open": 0,
            "acknowledged": 0,
            "resolved": 0,
            "unresolved": 0,
            "critical_unresolved": 0,
            "unproven_resolved": 0,
            "invalid_resolution_proof": 0,
            "operational_blocking": 0,
            "critical_operational_blocking": 0,
            "total": 0,
        }
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM incidents ORDER BY incident_id"
            ).fetchall()
            for row in rows:
                state = str(row["state"])
                summary[state] += 1
                summary["total"] += 1
                if state != "resolved":
                    summary["unresolved"] += 1
                    if row["severity"] == "critical":
                        summary["critical_unresolved"] += 1
                assessment = classify_incident_resolution(connection, row)
                if assessment.resolution_proof_status == "legacy_unproven":
                    summary["unproven_resolved"] += 1
                elif assessment.resolution_proof_status == "invalid":
                    summary["invalid_resolution_proof"] += 1
                if assessment.operationally_blocking:
                    summary["operational_blocking"] += 1
                    if row["severity"] == "critical":
                        summary["critical_operational_blocking"] += 1
        return summary

    def events(self, incident_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1000:
            raise ValueError("event limit must be between 1 and 1000")
        with self._connect() as connection:
            incident_id = self._resolve_incident_id(connection, incident_id)
            rows = connection.execute(
                """
                SELECT event_id, incident_id, event_type, created_at, payload_json
                FROM incident_events WHERE incident_id = ?
                ORDER BY rowid DESC LIMIT ?
                """,
                (incident_id, limit),
            ).fetchall()
        return [
            _incident_event_from_row(row, strict=False) for row in rows
        ]

    def record_anomaly_scan(
        self,
        scan: AnomalyScan,
        *,
        now: datetime | None = None,
    ) -> tuple[AnomalyCase, ...]:
        """Atomically persist one complete scan and reconcile its review cases."""
        prepared = _prepare_anomaly_scan(scan)
        fingerprints = tuple(row.observation.fingerprint for row in prepared.observations)
        cases: list[AnomalyCase] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_scan = connection.execute(
                "SELECT payload_sha256 FROM anomaly_scans WHERE scan_id = ?",
                (prepared.scan.scan_id,),
            ).fetchone()
            if existing_scan is not None:
                if str(existing_scan["payload_sha256"]) != prepared.payload_sha256:
                    raise ValueError(
                        f"anomaly scan id conflicts with different payload: "
                        f"{prepared.scan.scan_id}"
                    )
                return tuple(
                    self._anomaly_cases_for_fingerprints(connection, fingerprints)
                )

            recorded_at = _anomaly_timestamp(now)
            if _anomaly_moment(
                prepared.scan.source_boundary_at,
                label="scan source_boundary_at",
            ) > _anomaly_moment(recorded_at, label="scan recorded_at"):
                raise ValueError(
                    "anomaly source boundary cannot be later than its ledger "
                    "record time"
                )
            recorded_sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(recorded_sequence), 0) + 1
                    FROM anomaly_scans
                    """
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO anomaly_scans (
                    scan_id, payload_sha256, rule_bundle_version, scope,
                    source_boundary_sha256, source_boundary_at, recorded_at,
                    recorded_sequence, observation_count, evidence_json,
                    observed_fingerprints_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prepared.scan.scan_id,
                    prepared.payload_sha256,
                    prepared.scan.rule_bundle_version,
                    prepared.scan.scope,
                    prepared.scan.source_boundary_sha256,
                    str(prepared.scan.source_boundary_at),
                    recorded_at,
                    recorded_sequence,
                    len(prepared.observations),
                    prepared.evidence_json,
                    prepared.observed_fingerprints_json,
                ),
            )
            for item in prepared.observations:
                observation = item.observation
                row = connection.execute(
                    "SELECT * FROM anomaly_cases WHERE fingerprint = ?",
                    (observation.fingerprint,),
                ).fetchone()
                if row is None:
                    case_id = f"case-{uuid4().hex}"
                    connection.execute(
                        """
                        INSERT INTO anomaly_cases (
                            case_id, fingerprint, rule_id, rule_version, scope,
                            subject_type, subject_id, severity, confidence, title,
                            summary, state, owner, first_seen_at, last_seen_at,
                            occurrence_count, current_evidence_sha256,
                            old_value_json, new_value_json, evidence_json,
                            suggested_checks_json, disposition, resolution_note,
                            resolution_actor, resolved_at, next_review_at,
                            last_scan_id, acknowledged_at, verification_scan_id
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, ?, ?, 1,
                            ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, NULL, NULL
                        )
                        """,
                        (
                            case_id,
                            observation.fingerprint,
                            observation.rule_id,
                            observation.rule_version,
                            observation.scope,
                            observation.subject_type,
                            observation.subject_id,
                            observation.severity,
                            observation.confidence,
                            observation.title,
                            observation.summary,
                            recorded_at,
                            recorded_at,
                            item.payload_sha256,
                            _anomaly_object_json(
                                observation.old_value,
                                label="anomaly old value",
                            ),
                            _anomaly_object_json(
                                observation.new_value,
                                label="anomaly new value",
                            ),
                            _anomaly_object_json(
                                observation.evidence,
                                label="anomaly evidence",
                            ),
                            _anomaly_checks_json(observation.suggested_checks),
                            prepared.scan.scan_id,
                        ),
                    )
                    self._insert_anomaly_event(
                        connection,
                        case_id=case_id,
                        scan_id=prepared.scan.scan_id,
                        event_type="opened",
                        timestamp=recorded_at,
                        evidence_sha256=item.payload_sha256,
                        payload_json=item.payload_json,
                    )
                    current = connection.execute(
                        "SELECT * FROM anomaly_cases WHERE case_id = ?",
                        (case_id,),
                    ).fetchone()
                    self._verify_current_anomaly_evidence(connection, current)
                    cases.append(_anomaly_case_from_row(current))
                    continue

                self._verify_current_anomaly_evidence(
                    connection,
                    row,
                    pending_scan_id=prepared.scan.scan_id,
                )
                self._validate_anomaly_case_identity(row, observation)
                prior_scan = connection.execute(
                    """
                    SELECT source_boundary_at, rule_bundle_version, scope,
                           evidence_json
                    FROM anomaly_scans
                    WHERE scan_id = ?
                    """,
                    (row["last_scan_id"],),
                ).fetchone()
                if prior_scan is None:
                    raise RuntimeError(
                        "anomaly case has no source-boundary scan evidence"
                    )
                if _anomaly_moment(
                    prepared.scan.source_boundary_at,
                    label="scan source_boundary_at",
                ) < _anomaly_moment(
                    prior_scan["source_boundary_at"],
                    label="prior scan source_boundary_at",
                ) and not self._allows_sec_boundary_policy_upgrade(
                    prior_scan,
                    prepared,
                ):
                    raise ValueError(
                        "anomaly scan predates the current case source boundary: "
                        f"{observation.fingerprint}"
                    )
                corrected_case = (
                    str(row["state"]) == "resolved"
                    and str(row["disposition"])
                    in {"source_corrected", "mapping_corrected"}
                )
                if corrected_case:
                    verification = connection.execute(
                        """
                        SELECT source_boundary_at, recorded_at
                        FROM anomaly_scans
                        WHERE scan_id = ?
                        """,
                        (row["verification_scan_id"],),
                    ).fetchone()
                    if verification is None:
                        raise RuntimeError(
                            "corrected anomaly case has no verification evidence"
                        )
                    if _anomaly_moment(
                        prepared.scan.source_boundary_at,
                        label="scan source_boundary_at",
                    ) < _anomaly_moment(
                        verification["source_boundary_at"],
                        label="verification source_boundary_at",
                    ):
                        raise ValueError(
                            "anomaly scan predates the correction verification: "
                            f"{observation.fingerprint}"
                        )
                latest_event = connection.execute(
                    """
                    SELECT created_at
                    FROM anomaly_case_events
                    WHERE case_id = ?
                    ORDER BY event_sequence DESC
                    LIMIT 1
                    """,
                    (row["case_id"],),
                ).fetchone()
                if latest_event is not None and _anomaly_moment(
                    recorded_at,
                    label="scan recorded_at",
                ) < _anomaly_moment(
                    latest_event["created_at"],
                    label="latest anomaly event created_at",
                ):
                    raise ValueError(
                        "anomaly scan was recorded before the current case lifecycle: "
                        f"{observation.fingerprint}"
                    )
                same_evidence = (
                    str(row["current_evidence_sha256"]) == item.payload_sha256
                )
                if same_evidence and not corrected_case:
                    connection.execute(
                        """
                        UPDATE anomaly_cases
                        SET last_seen_at = ?, last_scan_id = ?
                        WHERE case_id = ?
                        """,
                        (
                            recorded_at,
                            prepared.scan.scan_id,
                            str(row["case_id"]),
                        ),
                    )
                else:
                    prior_state = str(row["state"])
                    reopened = prior_state in {"resolved", "deferred"}
                    severity = _higher_anomaly_severity(
                        str(row["severity"]),
                        observation.severity,
                    )
                    state = "open" if reopened else prior_state
                    owner = None if reopened else row["owner"]
                    acknowledged_at = None if reopened else row["acknowledged_at"]
                    connection.execute(
                        """
                        UPDATE anomaly_cases
                        SET severity = ?, confidence = ?, title = ?, summary = ?,
                            state = ?, owner = ?, last_seen_at = ?,
                            occurrence_count = occurrence_count + 1,
                            current_evidence_sha256 = ?, old_value_json = ?,
                            new_value_json = ?, evidence_json = ?,
                            suggested_checks_json = ?, disposition = NULL,
                            resolution_note = NULL, resolution_actor = NULL,
                            resolved_at = NULL, next_review_at = NULL,
                            last_scan_id = ?, acknowledged_at = ?,
                            verification_scan_id = NULL
                        WHERE case_id = ?
                        """,
                        (
                            severity,
                            observation.confidence,
                            observation.title,
                            observation.summary,
                            state,
                            owner,
                            recorded_at,
                            item.payload_sha256,
                            _anomaly_object_json(
                                observation.old_value,
                                label="anomaly old value",
                            ),
                            _anomaly_object_json(
                                observation.new_value,
                                label="anomaly new value",
                            ),
                            _anomaly_object_json(
                                observation.evidence,
                                label="anomaly evidence",
                            ),
                            _anomaly_checks_json(observation.suggested_checks),
                            prepared.scan.scan_id,
                            acknowledged_at,
                            str(row["case_id"]),
                        ),
                    )
                    self._insert_anomaly_event(
                        connection,
                        case_id=str(row["case_id"]),
                        scan_id=prepared.scan.scan_id,
                        event_type="reopened" if reopened else "evidence_changed",
                        timestamp=recorded_at,
                        evidence_sha256=item.payload_sha256,
                        payload_json=item.payload_json,
                    )
                current = connection.execute(
                    "SELECT * FROM anomaly_cases WHERE case_id = ?",
                    (str(row["case_id"]),),
                ).fetchone()
                self._verify_current_anomaly_evidence(connection, current)
                cases.append(_anomaly_case_from_row(current))
        return tuple(cases)

    def anomaly_cases(
        self,
        *,
        unresolved_only: bool = False,
        scope: str | None = None,
        limit: int = 100,
    ) -> list[AnomalyCase]:
        """List the bounded governed review queue."""
        if limit < 1 or limit > 1000:
            raise ValueError("anomaly case limit must be between 1 and 1000")
        clauses: list[str] = []
        parameters: list[Any] = []
        if unresolved_only:
            clauses.append("state != 'resolved'")
        if scope is not None:
            normalized_scope = _anomaly_text("scope", scope)
            clauses.append("scope = ?")
            parameters.append(normalized_scope)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self._connect() as connection:
            # Verify every mutable projection before applying state/scope filters.
            # Otherwise a direct projection edit could hide a case from the queue.
            all_rows = connection.execute(
                "SELECT * FROM anomaly_cases ORDER BY case_id"
            ).fetchall()
            for candidate in all_rows:
                self._verify_current_anomaly_evidence(connection, candidate)
            rows = connection.execute(
                f"""
                SELECT * FROM anomaly_cases {where}
                ORDER BY
                    CASE WHEN state = 'resolved' THEN 1 ELSE 0 END,
                    CASE severity
                        WHEN 'critical' THEN 0
                        WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2
                        ELSE 3
                    END,
                    CASE state
                        WHEN 'open' THEN 0
                        WHEN 'acknowledged' THEN 1
                        WHEN 'deferred' THEN 2
                        ELSE 3
                    END,
                    last_seen_at DESC,
                    case_id
                LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
            for row in rows:
                self._verify_current_anomaly_evidence(connection, row)
        return [_anomaly_case_from_row(row) for row in rows]

    def anomaly_case(self, case_id: str) -> AnomalyCase:
        """Return one case by exact ID or unique prefix."""
        with self._connect() as connection:
            resolved_id = self._resolve_anomaly_case_id(connection, case_id)
            row = connection.execute(
                "SELECT * FROM anomaly_cases WHERE case_id = ?",
                (resolved_id,),
            ).fetchone()
            self._verify_current_anomaly_evidence(connection, row)
        return _anomaly_case_from_row(row)

    def review_anomaly_acceptance(
        self,
        case_id: str,
        *,
        expected_evidence_sha256: str,
    ) -> dict[str, Any]:
        """Validate a rule-specific accepted-missingness contract read-only."""

        expected_evidence = _anomaly_sha256(
            "expected anomaly evidence",
            expected_evidence_sha256,
        )
        with self._connect() as connection:
            resolved_id = self._resolve_anomaly_case_id(connection, case_id)
            row = connection.execute(
                "SELECT * FROM anomaly_cases WHERE case_id = ?",
                (resolved_id,),
            ).fetchone()
            self._verify_current_anomaly_evidence(connection, row)
            self._require_current_anomaly_evidence(row, expected_evidence)
            return _validate_sec_pre_periodic_acceptance(connection, row)

    def anomaly_case_events(
        self,
        case_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return append-only case events newest first."""
        if limit < 1 or limit > 1000:
            raise ValueError("anomaly event limit must be between 1 and 1000")
        with self._connect() as connection:
            resolved_id = self._resolve_anomaly_case_id(connection, case_id)
            case = connection.execute(
                "SELECT * FROM anomaly_cases WHERE case_id = ?",
                (resolved_id,),
            ).fetchone()
            self._verify_current_anomaly_evidence(connection, case)
            rows = connection.execute(
                """
                SELECT event_id, event_sequence, scan_id, event_type, created_at,
                       actor, note, disposition, observation_sha256, payload_json
                FROM anomaly_case_events
                WHERE case_id = ?
                ORDER BY event_sequence DESC
                LIMIT ?
                """,
                (resolved_id, limit),
            ).fetchall()
        return [
            {
                "event_id": str(row["event_id"]),
                "event_sequence": int(row["event_sequence"]),
                "scan_id": str(row["scan_id"]) if row["scan_id"] is not None else None,
                "event_type": str(row["event_type"]),
                "created_at": str(row["created_at"]),
                "owner": str(row["actor"]) if row["actor"] is not None else None,
                "note": str(row["note"]) if row["note"] is not None else None,
                "resolution_outcome": (
                    str(row["disposition"]) if row["disposition"] is not None else None
                ),
                "evidence_sha256": (
                    str(row["observation_sha256"])
                    if row["observation_sha256"] is not None
                    else None
                ),
                "payload": json.loads(str(row["payload_json"])),
            }
            for row in rows
        ]

    def anomaly_summary(self) -> dict[str, int]:
        """Return exact case counts independent of list pagination."""
        summary = {
            "open": 0,
            "acknowledged": 0,
            "deferred": 0,
            "resolved": 0,
            "unresolved": 0,
            "critical_unresolved": 0,
            "high_unresolved": 0,
            "affected_subjects": 0,
            "total": 0,
        }
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM anomaly_cases ORDER BY case_id"
            ).fetchall()
            for row in rows:
                self._verify_current_anomaly_evidence(connection, row)
        unresolved_subjects: set[tuple[str, str]] = set()
        for row in rows:
            state = str(row["state"])
            summary[state] += 1
            summary["total"] += 1
            if state != "resolved":
                summary["unresolved"] += 1
                unresolved_subjects.add(
                    (str(row["subject_type"]), str(row["subject_id"]))
                )
                if row["severity"] == "critical":
                    summary["critical_unresolved"] += 1
                if row["severity"] == "high":
                    summary["high_unresolved"] += 1
        summary["affected_subjects"] = len(unresolved_subjects)
        return summary

    def acknowledge_anomaly(
        self,
        case_id: str,
        *,
        owner: str,
        note: str,
        expected_evidence_sha256: str,
        now: datetime | None = None,
    ) -> AnomalyCase:
        """Record explicit ownership without changing analytical evidence."""
        normalized_owner = _anomaly_text("owner", owner)
        normalized_note = _anomaly_text("acknowledgement note", note)
        expected_evidence = _anomaly_sha256(
            "expected anomaly evidence",
            expected_evidence_sha256,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = _anomaly_timestamp(now)
            resolved_id = self._resolve_anomaly_case_id(connection, case_id)
            row = connection.execute(
                "SELECT * FROM anomaly_cases WHERE case_id = ?",
                (resolved_id,),
            ).fetchone()
            self._verify_current_anomaly_evidence(connection, row)
            self._require_current_anomaly_evidence(row, expected_evidence)
            if str(row["state"]) == "acknowledged":
                prior = connection.execute(
                    """
                    SELECT actor, note, observation_sha256
                    FROM anomaly_case_events
                    WHERE case_id = ? AND event_type = 'acknowledged'
                    ORDER BY event_sequence DESC
                    LIMIT 1
                    """,
                    (resolved_id,),
                ).fetchone()
                if (
                    prior is not None
                    and str(prior["actor"]) == normalized_owner
                    and str(prior["note"]) == normalized_note
                    and str(prior["observation_sha256"]) == expected_evidence
                ):
                    return _anomaly_case_from_row(row)
                raise ValueError(
                    "anomaly case is already acknowledged with different "
                    "review content"
                )
            self._require_anomaly_transition_time(
                connection,
                row,
                timestamp,
            )
            if str(row["state"]) in {"resolved", "deferred"}:
                raise ValueError(
                    f"a {row['state']} anomaly case cannot be acknowledged"
                )
            connection.execute(
                """
                UPDATE anomaly_cases
                SET state = 'acknowledged', owner = ?, acknowledged_at = ?
                WHERE case_id = ?
                """,
                (normalized_owner, timestamp, resolved_id),
            )
            self._insert_anomaly_event(
                connection,
                case_id=resolved_id,
                scan_id=None,
                event_type="acknowledged",
                timestamp=timestamp,
                owner=normalized_owner,
                note=normalized_note,
                evidence_sha256=expected_evidence,
                payload_json=_anomaly_json(
                    {
                        "owner": normalized_owner,
                        "note": normalized_note,
                        "evidence_sha256": expected_evidence,
                    },
                    label="anomaly acknowledgement",
                ),
            )
            updated = connection.execute(
                "SELECT * FROM anomaly_cases WHERE case_id = ?",
                (resolved_id,),
            ).fetchone()
            self._verify_current_anomaly_evidence(connection, updated)
        return _anomaly_case_from_row(updated)

    def resolve_anomaly(
        self,
        case_id: str,
        *,
        outcome: str,
        note: str,
        expected_evidence_sha256: str,
        owner: str | None = None,
        next_review_at: str | datetime | None = None,
        verification_scan_id: str | None = None,
        now: datetime | None = None,
    ) -> AnomalyCase:
        """Apply one explicit audited disposition without repairing source data."""
        normalized_outcome = str(outcome).strip()
        if normalized_outcome not in ANOMALY_RESOLUTION_OUTCOMES:
            raise ValueError(f"unsupported anomaly resolution outcome: {outcome}")
        normalized_note = _anomaly_text("resolution note", note)
        expected_evidence = _anomaly_sha256(
            "expected anomaly evidence",
            expected_evidence_sha256,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = _anomaly_timestamp(now)
            resolved_id = self._resolve_anomaly_case_id(connection, case_id)
            row = connection.execute(
                "SELECT * FROM anomaly_cases WHERE case_id = ?",
                (resolved_id,),
            ).fetchone()
            self._verify_current_anomaly_evidence(connection, row)
            self._require_current_anomaly_evidence(row, expected_evidence)
            normalized_owner = (
                _anomaly_text("owner", owner)
                if owner is not None
                else str(row["owner"] or "").strip()
            )
            if not normalized_owner:
                raise ValueError(
                    "anomaly resolution requires a current or explicitly provided owner"
                )

            if normalized_outcome == "deferred":
                if verification_scan_id is not None:
                    raise ValueError("a deferred anomaly cannot use a verification scan")
                requested_review_time = (
                    _normalize_anomaly_time(
                        next_review_at,
                        label="next_review_at",
                        require_timezone=False,
                    )
                    if next_review_at is not None
                    else None
                )
                state = "deferred"
                resolved_at = None
                normalized_verification = None
            else:
                if next_review_at is not None:
                    raise ValueError(
                        "next_review_at is allowed only for a deferred anomaly"
                    )
                requested_review_time = None
                normalized_verification = (
                    _anomaly_text("verification scan id", verification_scan_id)
                    if verification_scan_id is not None
                    else None
                )
                state = "resolved"

            if str(row["state"]) in {"resolved", "deferred"}:
                exact_retry = (
                    str(row["state"]) == state
                    and str(row["owner"]) == normalized_owner
                    and str(row["disposition"]) == normalized_outcome
                    and str(row["resolution_note"]) == normalized_note
                    and (
                        str(row["next_review_at"])
                        if row["next_review_at"] is not None
                        else None
                    )
                    == requested_review_time
                    and (
                        str(row["verification_scan_id"])
                        if row["verification_scan_id"] is not None
                        else None
                    )
                    == normalized_verification
                )
                if exact_retry:
                    return _anomaly_case_from_row(row)
                if str(row["state"]) == "resolved":
                    raise ValueError(
                        "anomaly case is already resolved with different "
                        "disposition content"
                    )

            self._require_anomaly_transition_time(
                connection,
                row,
                timestamp,
            )
            if normalized_outcome == "deferred":
                review_time = _future_anomaly_time(next_review_at, after=timestamp)
            else:
                normalized_verification = self._validate_anomaly_verification_scan(
                    connection,
                    row,
                    verification_scan_id,
                    required=normalized_outcome
                    in {"source_corrected", "mapping_corrected"},
                    decision_at=timestamp,
                )
                review_time = None
                resolved_at = timestamp

            acceptance_contract = None
            if normalized_outcome == "accepted" and (
                _requires_sec_pre_periodic_acceptance(row)
            ):
                acceptance_contract = _validate_sec_pre_periodic_acceptance(
                    connection,
                    row,
                )

            connection.execute(
                """
                UPDATE anomaly_cases
                SET state = ?, owner = ?, disposition = ?, resolution_note = ?,
                    resolution_actor = ?, resolved_at = ?, next_review_at = ?,
                    verification_scan_id = ?
                WHERE case_id = ?
                """,
                (
                    state,
                    normalized_owner,
                    normalized_outcome,
                    normalized_note,
                    normalized_owner,
                    resolved_at,
                    review_time,
                    normalized_verification,
                    resolved_id,
                ),
            )
            payload = {
                "owner": normalized_owner,
                "note": normalized_note,
                "outcome": normalized_outcome,
                "next_review_at": review_time,
                "verification_scan_id": normalized_verification,
                "evidence_sha256": expected_evidence,
            }
            if acceptance_contract is not None:
                payload["acceptance_contract"] = acceptance_contract
            self._insert_anomaly_event(
                connection,
                case_id=resolved_id,
                scan_id=normalized_verification,
                event_type="deferred" if state == "deferred" else "resolved",
                timestamp=timestamp,
                owner=normalized_owner,
                note=normalized_note,
                outcome=normalized_outcome,
                evidence_sha256=expected_evidence,
                payload_json=_anomaly_json(payload, label="anomaly resolution"),
            )
            updated = connection.execute(
                "SELECT * FROM anomaly_cases WHERE case_id = ?",
                (resolved_id,),
            ).fetchone()
            self._verify_current_anomaly_evidence(connection, updated)
        return _anomaly_case_from_row(updated)

    @staticmethod
    def _anomaly_cases_for_fingerprints(
        connection: sqlite3.Connection,
        fingerprints: tuple[str, ...],
    ) -> list[AnomalyCase]:
        cases: list[AnomalyCase] = []
        for fingerprint in fingerprints:
            row = connection.execute(
                "SELECT * FROM anomaly_cases WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if row is not None:
                AlertStore._verify_current_anomaly_evidence(connection, row)
                cases.append(_anomaly_case_from_row(row))
        return cases

    @staticmethod
    def _validate_anomaly_case_identity(
        row: sqlite3.Row,
        observation: AnomalyObservation,
    ) -> None:
        stored = (
            str(row["rule_id"]),
            str(row["rule_version"]),
            str(row["scope"]),
            str(row["subject_type"]),
            str(row["subject_id"]),
        )
        incoming = (
            observation.rule_id,
            observation.rule_version,
            observation.scope,
            observation.subject_type,
            observation.subject_id,
        )
        if stored != incoming:
            raise ValueError(
                "anomaly fingerprint conflicts with a different rule or subject: "
                f"{observation.fingerprint}"
            )

    @staticmethod
    def _allows_sec_boundary_policy_upgrade(
        prior_scan: sqlite3.Row,
        prepared: _PreparedAnomalyScan,
    ) -> bool:
        """Allow only the audited one-way legacy SEC boundary correction."""
        expected_rule = "sec_fundamentals_coverage_missing@1.0.0"
        if (
            str(prior_scan["rule_bundle_version"])
            != "us-equity-data-quality.v1"
            or prepared.scan.rule_bundle_version != "us-equity-data-quality.v1"
            or str(prior_scan["scope"]) != prepared.scan.scope
            or not prepared.scan.scope.startswith("us-equity-reference:")
            or prepared.scan.executed_rules != (expected_rule,)
        ):
            return False
        try:
            prior_evidence = json.loads(str(prior_scan["evidence_json"]))
            current_evidence = json.loads(prepared.evidence_json)
        except json.JSONDecodeError:
            return False
        if not isinstance(prior_evidence, dict) or not isinstance(
            current_evidence,
            dict,
        ):
            return False
        safety_contract = {
            "data_repairs": 0,
            "readiness_overrides": 0,
            "paper_actions": 0,
            "broker_actions": 0,
        }
        legacy_contract = (
            "source_boundary_policy" not in prior_evidence
            and "source_boundary_policy_transition" not in prior_evidence
            and "used_snapshot_count" not in prior_evidence
            and prior_evidence.get("source_boundary_basis")
            == "utc_raw_snapshot_received_at"
            and prior_evidence.get("ingest_log_timestamp_basis")
            == "legacy_host_local_not_used_for_ordering"
            and prior_evidence.get("temporal_mode")
            == "retrospective_review_no_backfill"
            and prior_evidence.get("executed_rules") == [expected_rule]
            and prior_evidence.get("safety") == safety_contract
        )
        current_contract = (
            current_evidence.get("source_boundary_policy")
            == SEC_SOURCE_BOUNDARY_POLICY_V2
            and current_evidence.get("source_boundary_policy_transition")
            == SEC_SOURCE_BOUNDARY_POLICY_V1_TO_V2
            and current_evidence.get("source_boundary_basis")
            == "max_utc_received_at_of_snapshots_consumed_by_scan"
            and current_evidence.get("ingest_log_timestamp_basis")
            == "legacy_host_local_not_used_for_ordering"
            and current_evidence.get("temporal_mode")
            == "retrospective_review_no_backfill"
            and current_evidence.get("executed_rules") == [expected_rule]
            and current_evidence.get("safety") == safety_contract
            and isinstance(current_evidence.get("used_snapshot_count"), int)
            and int(current_evidence["used_snapshot_count"]) > 0
        )
        if not legacy_contract or not current_contract:
            return False
        proof = current_evidence.get("source_boundary_proof")
        if not isinstance(proof, dict):
            return False
        try:
            prior_observed_through = _anomaly_moment(
                prior_evidence["evidence_observed_through"],
                label="legacy evidence_observed_through",
            )
            current_observed_through = _anomaly_moment(
                current_evidence["evidence_observed_through"],
                label="current evidence_observed_through",
            )
            proof_observed_through = _anomaly_moment(
                proof["maximum_received_at"],
                label="source-boundary proof maximum_received_at",
            )
        except (KeyError, TypeError, ValueError):
            return False
        return (
            prior_observed_through
            == _anomaly_moment(
                prior_scan["source_boundary_at"],
                label="legacy source_boundary_at",
            )
            and current_observed_through
            == _anomaly_moment(
                prepared.scan.source_boundary_at,
                label="current source_boundary_at",
            )
            and proof_observed_through == current_observed_through
        )

    @staticmethod
    def _require_current_anomaly_evidence(
        row: sqlite3.Row,
        expected_evidence_sha256: str,
    ) -> None:
        if str(row["current_evidence_sha256"]) != expected_evidence_sha256:
            raise ValueError(
                "anomaly evidence changed after review; inspect the case again "
                "and use its current evidence SHA-256"
            )

    @staticmethod
    def _verify_current_anomaly_evidence(
        connection: sqlite3.Connection,
        row: sqlite3.Row | None,
        *,
        pending_scan_id: str | None = None,
    ) -> None:
        """Cross-check mutable case projection against immutable observation proof."""
        if row is None:
            raise RuntimeError("anomaly case disappeared during integrity verification")
        AlertStore._verify_canonical_anomaly_case_identity(row)
        case_id = str(row["case_id"])
        fingerprint = str(row["fingerprint"])
        expected_sha256 = str(row["current_evidence_sha256"])
        event = connection.execute(
            """
            SELECT scan_id, observation_sha256, payload_json
            FROM anomaly_case_events
            WHERE case_id = ?
              AND event_type IN ('opened','evidence_changed','reopened')
            ORDER BY event_sequence DESC
            LIMIT 1
            """,
            (case_id,),
        ).fetchone()
        if event is None:
            raise RuntimeError(
                f"anomaly case evidence integrity check failed: {case_id} "
                "has no immutable observation"
            )
        payload_json = str(event["payload_json"])
        actual_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if (
            str(event["observation_sha256"]) != expected_sha256
            or actual_sha256 != expected_sha256
        ):
            raise RuntimeError(
                f"anomaly case evidence integrity check failed: {case_id} "
                "observation hash mismatch"
            )
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:  # pragma: no cover - hash check catches tamper
            raise RuntimeError(
                f"anomaly case evidence integrity check failed: {case_id} "
                "observation payload is invalid"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"anomaly case evidence integrity check failed: {case_id} "
                "observation payload is not an object"
            )

        latest_scan = connection.execute(
            "SELECT evidence_json FROM anomaly_scans WHERE scan_id = ?",
            (row["last_scan_id"],),
        ).fetchone()
        if latest_scan is None:
            raise RuntimeError(
                f"anomaly case evidence integrity check failed: {case_id} "
                "has no current scan manifest"
            )
        try:
            scan_evidence = json.loads(str(latest_scan["evidence_json"]))
        except json.JSONDecodeError as exc:  # pragma: no cover - append-only table
            raise RuntimeError(
                f"anomaly case evidence integrity check failed: {case_id} "
                "scan evidence is invalid"
            ) from exc
        manifest = (
            scan_evidence.get("observation_manifest")
            if isinstance(scan_evidence, dict)
            else None
        )
        manifest_match = (
            isinstance(manifest, list)
            and any(
                isinstance(item, dict)
                and item.get("fingerprint") == fingerprint
                and item.get("evidence_sha256") == expected_sha256
                for item in manifest
            )
        )
        if not manifest_match:
            raise RuntimeError(
                f"anomaly case evidence integrity check failed: {case_id} "
                "is absent from the current scan manifest"
            )

        scalar_fields = {
            "fingerprint": "fingerprint",
            "rule_id": "rule_id",
            "rule_version": "rule_version",
            "scope": "scope",
            "subject_type": "subject_type",
            "subject_id": "subject_id",
            "confidence": "confidence",
            "title": "title",
            "summary": "summary",
        }
        if any(
            str(row[column]) != str(payload.get(payload_key))
            for column, payload_key in scalar_fields.items()
        ):
            raise RuntimeError(
                f"anomaly case evidence integrity check failed: {case_id} "
                "projection identity mismatch"
            )
        observed_severity = str(payload.get("severity"))
        stored_severity = str(row["severity"])
        if (
            observed_severity not in ANOMALY_SEVERITIES
            or stored_severity not in ANOMALY_SEVERITIES
            or _higher_anomaly_severity(stored_severity, observed_severity)
            != stored_severity
        ):
            raise RuntimeError(
                f"anomaly case evidence integrity check failed: {case_id} "
                "severity projection mismatch"
            )
        structured_fields = {
            "old_value_json": "old_value",
            "new_value_json": "new_value",
            "evidence_json": "evidence",
            "suggested_checks_json": "suggested_checks",
        }
        for column, payload_key in structured_fields.items():
            try:
                stored_value = json.loads(str(row[column]))
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"anomaly case evidence integrity check failed: {case_id} "
                    f"{column} is invalid"
                ) from exc
            if stored_value != payload.get(payload_key):
                raise RuntimeError(
                    f"anomaly case evidence integrity check failed: {case_id} "
                    f"{column} projection mismatch"
                )
        AlertStore._verify_anomaly_case_lifecycle(
            connection,
            row,
            pending_scan_id=pending_scan_id,
        )

    @staticmethod
    def _verify_canonical_anomaly_case_identity(row: sqlite3.Row) -> None:
        """Re-prove a persisted case fingerprint from its durable identity."""

        case_id = str(row["case_id"])
        try:
            expected = canonical_anomaly_fingerprint(
                rule_id=str(row["rule_id"]),
                rule_version=str(row["rule_version"]),
                scope=str(row["scope"]),
                subject_type=str(row["subject_type"]),
                subject_id=str(row["subject_id"]),
            )
        except (KeyError, ValueError) as exc:
            raise RuntimeError(
                "anomaly case fingerprint integrity check failed: "
                f"{case_id} identity is invalid"
            ) from exc
        if str(row["fingerprint"]) != expected:
            raise RuntimeError(
                "anomaly case fingerprint integrity check failed: "
                f"{case_id} fingerprint is not canonical"
            )

    @staticmethod
    def _verify_anomaly_case_lifecycle(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        pending_scan_id: str | None = None,
    ) -> None:
        """Replay immutable events and prove the mutable lifecycle projection."""
        case_id = str(row["case_id"])

        def fail(detail: str) -> None:
            raise RuntimeError(
                f"anomaly case lifecycle integrity check failed: "
                f"{case_id} {detail}"
            )

        events = connection.execute(
            """
            SELECT scan_id, event_type, created_at, actor, note, disposition,
                   observation_sha256, payload_json
            FROM anomaly_case_events
            WHERE case_id = ?
            ORDER BY event_sequence
            """,
            (case_id,),
        ).fetchall()
        if not events:
            fail("has no immutable lifecycle events")

        state: str | None = None
        owner: str | None = None
        first_seen_at: str | None = None
        acknowledged_at: str | None = None
        disposition: str | None = None
        resolution_note: str | None = None
        resolution_actor: str | None = None
        resolved_at: str | None = None
        next_review_at: str | None = None
        verification_scan_id: str | None = None
        evidence_sha256: str | None = None
        current_observation_scan_sequence: int | None = None
        occurrence_count = 0
        projected_severity: str | None = None
        prior_event_at: datetime | None = None

        for index, event in enumerate(events):
            event_type = str(event["event_type"])
            created_at = str(event["created_at"])
            created_moment = _anomaly_moment(
                created_at,
                label="anomaly event created_at",
            )
            if prior_event_at is not None and created_moment < prior_event_at:
                fail("event timestamps are out of sequence")
            prior_event_at = created_moment

            payload_json = str(event["payload_json"])
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError as exc:  # pragma: no cover - append-only table
                raise RuntimeError(
                    f"anomaly case lifecycle integrity check failed: "
                    f"{case_id} event payload is invalid"
                ) from exc
            if not isinstance(payload, dict):
                fail("event payload is not an object")

            event_scan_id = (
                str(event["scan_id"]) if event["scan_id"] is not None else None
            )
            event_actor = (
                str(event["actor"]) if event["actor"] is not None else None
            )
            event_note = str(event["note"]) if event["note"] is not None else None
            event_disposition = (
                str(event["disposition"])
                if event["disposition"] is not None
                else None
            )
            event_evidence_sha256 = (
                str(event["observation_sha256"])
                if event["observation_sha256"] is not None
                else None
            )

            if event_type in {"opened", "evidence_changed", "reopened"}:
                if (
                    event_scan_id is None
                    or event_actor is not None
                    or event_note is not None
                    or event_disposition is not None
                    or event_evidence_sha256 is None
                ):
                    fail(f"{event_type} event columns are inconsistent")
                if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != (
                    event_evidence_sha256
                ):
                    fail(f"{event_type} event observation hash mismatch")
                if any(
                    str(payload.get(payload_key)) != str(row[column])
                    for column, payload_key in {
                        "fingerprint": "fingerprint",
                        "rule_id": "rule_id",
                        "rule_version": "rule_version",
                        "scope": "scope",
                        "subject_type": "subject_type",
                        "subject_id": "subject_id",
                    }.items()
                ):
                    fail(f"{event_type} event identity mismatch")

                scan = connection.execute(
                    """
                    SELECT recorded_at, recorded_sequence, evidence_json
                    FROM anomaly_scans
                    WHERE scan_id = ?
                    """,
                    (event_scan_id,),
                ).fetchone()
                if scan is None or str(scan["recorded_at"]) != created_at:
                    fail(f"{event_type} event has no matching recorded scan")
                try:
                    scan_evidence = json.loads(str(scan["evidence_json"]))
                except json.JSONDecodeError as exc:  # pragma: no cover
                    raise RuntimeError(
                        f"anomaly case lifecycle integrity check failed: "
                        f"{case_id} scan evidence is invalid"
                    ) from exc
                manifest = (
                    scan_evidence.get("observation_manifest")
                    if isinstance(scan_evidence, dict)
                    else None
                )
                if not (
                    isinstance(manifest, list)
                    and any(
                        isinstance(item, dict)
                        and item.get("fingerprint") == str(row["fingerprint"])
                        and item.get("evidence_sha256")
                        == event_evidence_sha256
                        for item in manifest
                    )
                ):
                    fail(f"{event_type} event is absent from its scan manifest")

                if event_type == "opened":
                    if index != 0 or state is not None:
                        fail("opened event is not the initial lifecycle event")
                    state = "open"
                    first_seen_at = created_at
                elif event_type == "evidence_changed":
                    if state not in {"open", "acknowledged"}:
                        fail(
                            "evidence_changed event does not follow an active review"
                        )
                else:
                    if state not in {"resolved", "deferred"}:
                        fail("reopened event does not follow a disposition")
                    state = "open"
                    owner = None
                    acknowledged_at = None

                occurrence_count += 1
                evidence_sha256 = event_evidence_sha256
                observed_severity = str(payload.get("severity"))
                if observed_severity not in ANOMALY_SEVERITIES:
                    fail(f"{event_type} event severity is invalid")
                projected_severity = (
                    observed_severity
                    if projected_severity is None
                    else _higher_anomaly_severity(
                        projected_severity,
                        observed_severity,
                    )
                )
                current_observation_scan_sequence = int(scan["recorded_sequence"])
                disposition = None
                resolution_note = None
                resolution_actor = None
                resolved_at = None
                next_review_at = None
                verification_scan_id = None
                continue

            if evidence_sha256 is None or first_seen_at is None or state is None:
                fail(f"{event_type} event predates the opening observation")
            if event_evidence_sha256 != evidence_sha256:
                fail(f"{event_type} event reviewed stale evidence")

            if event_type == "acknowledged":
                if state != "open":
                    fail("acknowledged event does not follow an open case")
                expected_payload = {
                    "owner": event_actor,
                    "note": event_note,
                    "evidence_sha256": event_evidence_sha256,
                }
                if (
                    event_scan_id is not None
                    or event_actor is None
                    or event_note is None
                    or event_disposition is not None
                    or payload != expected_payload
                ):
                    fail("acknowledged event content is inconsistent")
                state = "acknowledged"
                owner = event_actor
                acknowledged_at = created_at
                continue

            if event_type not in {"deferred", "resolved"}:
                fail(f"contains unsupported event type {event_type}")
            if state not in {"open", "acknowledged", "deferred"}:
                fail(f"{event_type} event follows an invalid lifecycle state")
            expected_outcomes = (
                {"deferred"}
                if event_type == "deferred"
                else ANOMALY_RESOLUTION_OUTCOMES - {"deferred"}
            )
            if (
                event_actor is None
                or event_note is None
                or event_disposition not in expected_outcomes
            ):
                fail(f"{event_type} event disposition is inconsistent")
            payload_verification_scan_id = payload.get("verification_scan_id")
            expected_event_scan_id = (
                str(payload_verification_scan_id)
                if payload_verification_scan_id is not None
                else None
            )
            expected_payload = {
                "owner": event_actor,
                "note": event_note,
                "outcome": event_disposition,
                "next_review_at": payload.get("next_review_at"),
                "verification_scan_id": payload_verification_scan_id,
                "evidence_sha256": event_evidence_sha256,
            }
            if (
                event_type == "resolved"
                and event_disposition == "accepted"
                and _requires_sec_pre_periodic_acceptance(row)
            ):
                try:
                    expected_payload["acceptance_contract"] = (
                        _validate_sec_pre_periodic_acceptance(connection, row)
                    )
                except ValueError as exc:
                    fail(f"accepted-missingness contract is invalid: {exc}")
            if payload != expected_payload or event_scan_id != expected_event_scan_id:
                fail(f"{event_type} event content is inconsistent")
            if event_type == "deferred":
                review_at = payload.get("next_review_at")
                if (
                    not isinstance(review_at, str)
                    or _anomaly_moment(
                        review_at,
                        label="anomaly next_review_at",
                    )
                    <= created_moment
                    or event_scan_id is not None
                ):
                    fail("deferred event review time is invalid")
                state = "deferred"
                resolved_at = None
                next_review_at = review_at
                verification_scan_id = None
            else:
                if payload.get("next_review_at") is not None:
                    fail("resolved event unexpectedly schedules another review")
                if (
                    event_disposition
                    in {"source_corrected", "mapping_corrected"}
                    and event_scan_id is None
                ):
                    fail("corrected resolution has no verification scan")
                state = "resolved"
                resolved_at = created_at
                next_review_at = None
                verification_scan_id = event_scan_id
            owner = event_actor
            disposition = event_disposition
            resolution_note = event_note
            resolution_actor = event_actor

        if current_observation_scan_sequence is None:
            fail("has no current observation scan")
        matching_scans = connection.execute(
            """
            SELECT scan_id, recorded_at, recorded_sequence, evidence_json
            FROM anomaly_scans
            WHERE recorded_sequence >= ?
              AND (? IS NULL OR scan_id != ?)
            ORDER BY recorded_sequence DESC
            """,
            (
                current_observation_scan_sequence,
                pending_scan_id,
                pending_scan_id,
            ),
        ).fetchall()
        latest_scan: sqlite3.Row | None = None
        for scan in matching_scans:
            try:
                scan_evidence = json.loads(str(scan["evidence_json"]))
            except json.JSONDecodeError as exc:  # pragma: no cover
                raise RuntimeError(
                    f"anomaly case lifecycle integrity check failed: "
                    f"{case_id} scan evidence is invalid"
                ) from exc
            manifest = (
                scan_evidence.get("observation_manifest")
                if isinstance(scan_evidence, dict)
                else None
            )
            if isinstance(manifest, list) and any(
                isinstance(item, dict)
                and item.get("fingerprint") == str(row["fingerprint"])
                and item.get("evidence_sha256") == evidence_sha256
                for item in manifest
            ):
                latest_scan = scan
                break
        if latest_scan is None:
            fail("has no immutable scan for its current evidence")

        expected_projection: dict[str, Any] = {
            "state": state,
            "owner": owner,
            "first_seen_at": first_seen_at,
            "last_seen_at": str(latest_scan["recorded_at"]),
            "occurrence_count": occurrence_count,
            "current_evidence_sha256": evidence_sha256,
            "severity": projected_severity,
            "disposition": disposition,
            "resolution_note": resolution_note,
            "resolution_actor": resolution_actor,
            "resolved_at": resolved_at,
            "next_review_at": next_review_at,
            "last_scan_id": str(latest_scan["scan_id"]),
            "acknowledged_at": acknowledged_at,
            "verification_scan_id": verification_scan_id,
        }
        for column, expected in expected_projection.items():
            actual = row[column]
            if actual is not None:
                actual = int(actual) if column == "occurrence_count" else str(actual)
            if actual != expected:
                fail(
                    f"lifecycle projection {column} mismatch "
                    f"({column} projection mismatch)"
                )

    @staticmethod
    def _require_anomaly_transition_time(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        timestamp: str,
    ) -> None:
        latest = connection.execute(
            """
            SELECT created_at
            FROM anomaly_case_events
            WHERE case_id = ?
            ORDER BY event_sequence DESC
            LIMIT 1
            """,
            (row["case_id"],),
        ).fetchone()
        if latest is not None and _anomaly_moment(
            timestamp,
            label="anomaly transition timestamp",
        ) < _anomaly_moment(
            latest["created_at"],
            label="latest anomaly event created_at",
        ):
            raise ValueError(
                "anomaly review transition predates the current case lifecycle"
            )

    @staticmethod
    def _resolve_anomaly_case_id(
        connection: sqlite3.Connection,
        reference: str,
    ) -> str:
        value = str(reference).strip()
        if not value:
            raise ValueError("anomaly case reference is required")
        rows = connection.execute(
            """
            SELECT case_id
            FROM anomaly_cases
            WHERE case_id = ? OR substr(case_id, 1, length(?)) = ?
            ORDER BY CASE WHEN case_id = ? THEN 0 ELSE 1 END, case_id
            LIMIT 2
            """,
            (value, value, value, value),
        ).fetchall()
        if not rows:
            raise ValueError(f"unknown anomaly case: {reference}")
        if len(rows) > 1 and all(str(row["case_id"]) != value for row in rows):
            raise ValueError(f"anomaly case reference is ambiguous: {reference}")
        return str(rows[0]["case_id"])

    @staticmethod
    def _insert_anomaly_event(
        connection: sqlite3.Connection,
        *,
        case_id: str,
        scan_id: str | None,
        event_type: str,
        timestamp: str,
        payload_json: str,
        owner: str | None = None,
        note: str | None = None,
        outcome: str | None = None,
        evidence_sha256: str | None = None,
    ) -> None:
        if event_type not in ANOMALY_EVENT_TYPES:
            raise ValueError(f"unsupported anomaly event type: {event_type}")
        event_sequence = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(event_sequence), 0) + 1
                FROM anomaly_case_events
                """
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO anomaly_case_events (
                event_id, event_sequence, case_id, scan_id, event_type,
                created_at, actor, note, disposition, observation_sha256,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"case-event-{uuid4().hex}",
                event_sequence,
                case_id,
                scan_id,
                event_type,
                timestamp,
                owner,
                note,
                outcome,
                evidence_sha256,
                payload_json,
            ),
        )

    @staticmethod
    def _validate_anomaly_verification_scan(
        connection: sqlite3.Connection,
        case: sqlite3.Row,
        scan_id: str | None,
        *,
        required: bool,
        decision_at: str,
    ) -> str | None:
        if scan_id is None:
            if required:
                raise ValueError(
                    "corrected anomaly resolution requires a later verification scan"
                )
            return None
        normalized = _anomaly_text("verification scan id", scan_id)
        scan = connection.execute(
            "SELECT * FROM anomaly_scans WHERE scan_id = ?",
            (normalized,),
        ).fetchone()
        if scan is None:
            raise ValueError(f"unknown anomaly verification scan: {normalized}")
        if str(scan["scope"]) != str(case["scope"]):
            raise ValueError("anomaly verification scan scope does not match the case")
        finding_scan = connection.execute(
            """
            SELECT recorded_sequence, source_boundary_sha256,
                   source_boundary_at, recorded_at
            FROM anomaly_scans
            WHERE scan_id = ?
            """,
            (case["last_scan_id"],),
        ).fetchone()
        if finding_scan is None:
            raise RuntimeError("anomaly case has no finding scan evidence")
        if int(scan["recorded_sequence"]) <= int(
            finding_scan["recorded_sequence"]
        ):
            raise ValueError(
                "anomaly verification scan must be recorded after the finding scan"
            )
        if str(scan["source_boundary_sha256"]) == str(
            finding_scan["source_boundary_sha256"]
        ):
            raise ValueError(
                "anomaly verification scan must use a different source boundary"
            )
        if _anomaly_moment(
            scan["source_boundary_at"],
            label="verification source_boundary_at",
        ) < _anomaly_moment(
            finding_scan["source_boundary_at"],
            label="finding source_boundary_at",
        ):
            raise ValueError(
                "anomaly verification scan cannot predate the finding source boundary"
            )
        if _anomaly_moment(
            scan["recorded_at"],
            label="verification recorded_at",
        ) < _anomaly_moment(
            case["last_seen_at"],
            label="case last_seen_at",
        ):
            raise ValueError("anomaly verification scan must be later than the finding")
        if _anomaly_moment(
            decision_at,
            label="anomaly decision timestamp",
        ) < _anomaly_moment(
            scan["recorded_at"],
            label="verification recorded_at",
        ):
            raise ValueError(
                "anomaly disposition cannot predate its verification scan"
            )
        fingerprints = json.loads(str(scan["observed_fingerprints_json"]))
        if not isinstance(fingerprints, list):
            raise ValueError("anomaly verification scan fingerprint evidence is invalid")
        scan_evidence = json.loads(str(scan["evidence_json"]))
        executed_rules = (
            scan_evidence.get("executed_rules")
            if isinstance(scan_evidence, dict)
            else None
        )
        case_rule = f"{case['rule_id']}@{case['rule_version']}"
        if (
            not isinstance(executed_rules, list)
            or any(not isinstance(value, str) for value in executed_rules)
            or case_rule not in executed_rules
        ):
            raise ValueError(
                "anomaly verification scan did not execute the case rule: "
                f"{case_rule}"
            )
        if str(case["fingerprint"]) in {str(value) for value in fingerprints}:
            raise ValueError("anomaly verification scan still detects this fingerprint")
        if required:
            if case_rule != "sec_fundamentals_coverage_missing@1.0.0":
                raise ValueError(
                    "corrected anomaly resolution has no registered "
                    f"clearance-proof contract for rule: {case_rule}"
                )
            _validate_anomaly_clearance_proof(scan_evidence, case)
        return normalized

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


def verify_anomaly_case_evidence(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> None:
    """Verify a case projection against its append-only observation proof."""
    AlertStore._verify_current_anomaly_evidence(connection, row)


def incident_resolution_projection(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> dict[str, Any]:
    """Return the current incident hash and derived resolution gate."""
    assessment = classify_incident_resolution(connection, row)
    return {
        "evidence_sha256": _incident_evidence_sha256(row),
        "resolution_proof_status": assessment.resolution_proof_status,
        "operationally_blocking": assessment.operationally_blocking,
        "generation_event_id": assessment.generation_event_id,
    }


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
    """Retain the incident until a domain receipt can prove exact recovery."""
    _validate_managed_unit(unit)
    del store
    return None


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


def _incident_action_text(label: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} is required")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    if len(normalized) > 4000:
        raise ValueError(f"{label} is too long")
    return normalized


def _incident_sha256(label: str, value: Any) -> str:
    normalized = _incident_action_text(f"{label} SHA-256", value).lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{label} must be a 64-character SHA-256")
    return normalized


def _manual_incident_resolution_outcome(value: Any) -> str:
    normalized = _incident_action_text("incident resolution outcome", value)
    if normalized not in MANUAL_INCIDENT_RESOLUTION_OUTCOMES:
        raise ValueError(
            "unsupported incident resolution outcome: "
            f"{normalized}; choose verified_recovery or false_positive"
        )
    return normalized


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


def _validate_anomaly_source_boundary_policy(
    evidence: dict[str, Any],
    *,
    source_boundary_at: str,
) -> None:
    policy_fields = {
        "source_boundary_policy",
        "source_boundary_policy_transition",
        "source_boundary_proof",
    }
    present = policy_fields.intersection(evidence)
    if not present:
        return
    if present != policy_fields:
        raise ValueError(
            "versioned anomaly source-boundary policy evidence is incomplete"
        )
    if evidence["source_boundary_policy"] != SEC_SOURCE_BOUNDARY_POLICY_V2:
        raise ValueError("unsupported anomaly source-boundary policy")
    if (
        evidence["source_boundary_policy_transition"]
        != SEC_SOURCE_BOUNDARY_POLICY_V1_TO_V2
    ):
        raise ValueError("unsupported anomaly source-boundary policy transition")
    proof = evidence["source_boundary_proof"]
    if not isinstance(proof, dict) or set(proof) != {
        "used_snapshot_count",
        "used_snapshot_set_sha256",
        "maximum_received_at",
    }:
        raise ValueError("anomaly source-boundary proof is invalid")
    count = proof["used_snapshot_count"]
    digest = str(proof["used_snapshot_set_sha256"])
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count < 1
        or evidence.get("used_snapshot_count") != count
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("anomaly source-boundary proof is invalid")
    try:
        proof_moment = _anomaly_moment(
            proof["maximum_received_at"],
            label="source-boundary proof maximum_received_at",
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("anomaly source-boundary proof is invalid") from exc
    if proof_moment != _anomaly_moment(
        source_boundary_at,
        label="scan source_boundary_at",
    ):
        raise ValueError(
            "anomaly source-boundary proof does not match the scan boundary"
        )


def _prepare_anomaly_scan(scan: AnomalyScan) -> _PreparedAnomalyScan:
    if not isinstance(scan, AnomalyScan):
        raise TypeError("anomaly scan must be an AnomalyScan")
    scan_id = _anomaly_text("scan id", scan.scan_id)
    rule_bundle_version = _anomaly_text(
        "rule bundle version",
        scan.rule_bundle_version,
    )
    scope = _anomaly_text("scan scope", scan.scope)
    source_boundary_sha256 = _anomaly_sha256(
        "source boundary",
        scan.source_boundary_sha256,
    )
    source_boundary_at = _normalize_anomaly_time(
        scan.source_boundary_at,
        label="scan source_boundary_at",
    )
    if not isinstance(scan.executed_rules, tuple):
        raise ValueError("anomaly scan executed_rules must be a tuple")
    executed_rules = tuple(
        sorted(_anomaly_text("executed rule", value) for value in scan.executed_rules)
    )
    if not executed_rules:
        raise ValueError("anomaly scan must declare at least one executed rule")
    if len(executed_rules) != len(set(executed_rules)):
        raise ValueError("anomaly scan contains duplicate executed rules")
    if not isinstance(scan.observations, tuple):
        raise ValueError("anomaly scan observations must be a tuple")
    prepared = tuple(
        sorted(
            (_prepare_anomaly_observation(row) for row in scan.observations),
            key=lambda row: row.observation.fingerprint,
        )
    )
    fingerprints = tuple(row.observation.fingerprint for row in prepared)
    if len(fingerprints) != len(set(fingerprints)):
        raise ValueError("anomaly scan contains duplicate fingerprints")
    for row in prepared:
        if row.observation.scope != scope:
            raise ValueError(
                "anomaly observation scope does not match its scan: "
                f"{row.observation.fingerprint}"
            )
        observation_rule = f"{row.observation.rule_id}@{row.observation.rule_version}"
        if observation_rule not in executed_rules:
            raise ValueError(
                "anomaly observation rule was not executed by its scan: "
                f"{observation_rule}"
            )
    observation_manifest = [
        {
            "fingerprint": row.observation.fingerprint,
            "evidence_sha256": row.payload_sha256,
        }
        for row in prepared
    ]
    evidence = dict(scan.evidence)
    declared_rules = evidence.get("executed_rules")
    if declared_rules is not None and declared_rules != list(executed_rules):
        raise ValueError(
            "anomaly scan evidence conflicts with its executed rule declaration"
        )
    declared_manifest = evidence.get("observation_manifest")
    if declared_manifest is not None and declared_manifest != observation_manifest:
        raise ValueError(
            "anomaly scan evidence conflicts with its observation manifest"
        )
    _validate_anomaly_source_boundary_policy(
        evidence,
        source_boundary_at=source_boundary_at,
    )
    evidence["executed_rules"] = list(executed_rules)
    evidence["observation_manifest"] = observation_manifest
    evidence_json = _anomaly_object_json(evidence, label="anomaly scan evidence")
    observed_fingerprints_json = _anomaly_json(
        list(fingerprints),
        label="anomaly scan fingerprints",
    )
    payload_json = _anomaly_json(
        {
            "schema_version": 1,
            "scan_id": scan_id,
            "rule_bundle_version": rule_bundle_version,
            "scope": scope,
            "source_boundary_sha256": source_boundary_sha256,
            "source_boundary_at": source_boundary_at,
            "executed_rules": list(executed_rules),
            "evidence": json.loads(evidence_json),
            "observations": [
                {
                    "fingerprint": row.observation.fingerprint,
                    "payload_sha256": row.payload_sha256,
                }
                for row in prepared
            ],
        },
        label="anomaly scan payload",
    )
    normalized = AnomalyScan(
        scan_id=scan_id,
        rule_bundle_version=rule_bundle_version,
        scope=scope,
        source_boundary_sha256=source_boundary_sha256,
        source_boundary_at=source_boundary_at,
        executed_rules=executed_rules,
        observations=tuple(row.observation for row in prepared),
        evidence=json.loads(evidence_json),
    )
    return _PreparedAnomalyScan(
        scan=normalized,
        payload_sha256=hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        evidence_json=evidence_json,
        observed_fingerprints_json=observed_fingerprints_json,
        observations=prepared,
    )


def _prepare_anomaly_observation(
    observation: AnomalyObservation,
) -> _PreparedAnomalyObservation:
    if not isinstance(observation, AnomalyObservation):
        raise TypeError("anomaly observation must be an AnomalyObservation")
    severity = str(observation.severity).strip().lower()
    if severity not in ANOMALY_SEVERITIES:
        raise ValueError(f"unsupported anomaly severity: {observation.severity}")
    confidence = str(observation.confidence).strip().lower()
    if confidence not in ANOMALY_CONFIDENCES:
        raise ValueError(f"unsupported anomaly confidence: {observation.confidence}")
    if not isinstance(observation.old_value, dict):
        raise ValueError("anomaly old value must be an object")
    if not isinstance(observation.new_value, dict):
        raise ValueError("anomaly new value must be an object")
    if not isinstance(observation.evidence, dict):
        raise ValueError("anomaly evidence must be an object")
    if not isinstance(observation.suggested_checks, tuple):
        raise ValueError("anomaly suggested checks must be a tuple")
    checks = tuple(
        _anomaly_text("suggested check", value)
        for value in observation.suggested_checks
    )
    if not checks:
        raise ValueError("anomaly observation requires at least one suggested check")
    fingerprint = _anomaly_text(
        "anomaly fingerprint",
        observation.fingerprint,
    )
    rule_id = _anomaly_text("anomaly rule id", observation.rule_id)
    rule_version = _anomaly_text(
        "anomaly rule version",
        observation.rule_version,
    )
    scope = _anomaly_text("anomaly scope", observation.scope)
    subject_type = _anomaly_text(
        "anomaly subject type",
        observation.subject_type,
    )
    subject_id = _anomaly_text(
        "anomaly subject id",
        observation.subject_id,
    )
    expected_fingerprint = canonical_anomaly_fingerprint(
        rule_id=rule_id,
        rule_version=rule_version,
        scope=scope,
        subject_type=subject_type,
        subject_id=subject_id,
    )
    if fingerprint != expected_fingerprint:
        raise ValueError(
            "anomaly fingerprint is not canonical for its rule and subject: "
            f"expected {expected_fingerprint}"
        )
    payload_json = _anomaly_json(
        {
            "schema_version": 1,
            "fingerprint": fingerprint,
            "rule_id": rule_id,
            "rule_version": rule_version,
            "scope": scope,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "severity": severity,
            "confidence": confidence,
            "title": _anomaly_text("anomaly title", observation.title),
            "summary": _anomaly_text("anomaly summary", observation.summary),
            "old_value": observation.old_value,
            "new_value": observation.new_value,
            "evidence": observation.evidence,
            "suggested_checks": list(checks),
        },
        label="anomaly observation",
    )
    payload = json.loads(payload_json)
    normalized = AnomalyObservation(
        fingerprint=str(payload["fingerprint"]),
        rule_id=str(payload["rule_id"]),
        rule_version=str(payload["rule_version"]),
        scope=str(payload["scope"]),
        subject_type=str(payload["subject_type"]),
        subject_id=str(payload["subject_id"]),
        severity=str(payload["severity"]),
        confidence=str(payload["confidence"]),
        title=str(payload["title"]),
        summary=str(payload["summary"]),
        old_value=dict(payload["old_value"]),
        new_value=dict(payload["new_value"]),
        evidence=dict(payload["evidence"]),
        suggested_checks=tuple(str(value) for value in payload["suggested_checks"]),
    )
    return _PreparedAnomalyObservation(
        observation=normalized,
        payload_json=payload_json,
        payload_sha256=hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
    )


def _requires_sec_pre_periodic_acceptance(case: sqlite3.Row) -> bool:
    """Identify successor-issuer missingness that needs the stricter contract."""

    if f"{case['rule_id']}@{case['rule_version']}" != SEC_FUNDAMENTALS_COVERAGE_RULE:
        return False
    try:
        evidence = json.loads(str(case["evidence_json"]))
    except json.JSONDecodeError:
        return False
    filing_stage = evidence.get("filing_stage") if isinstance(evidence, dict) else None
    owner = (
        filing_stage.get("reviewed_security_owner")
        if isinstance(filing_stage, dict)
        else None
    )
    return isinstance(owner, dict) and isinstance(owner.get("predecessor_owner"), dict)


def _validate_sec_pre_periodic_acceptance(
    connection: sqlite3.Connection,
    case: sqlite3.Row,
) -> dict[str, Any]:
    """Prove that accepting SEC missingness cannot create analytical evidence."""

    def refuse(detail: str) -> None:
        raise ValueError(f"SEC pre-periodic acceptance refused: {detail}")

    def object_field(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            refuse(f"{label} must be an object")
        return value

    def integer_field(
        value: Any,
        label: str,
        *,
        minimum: int = 0,
    ) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            refuse(f"{label} must be an integer of at least {minimum}")
        return value

    def exact_zero(value: Any, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value != 0:
            refuse(f"{label} must remain zero")

    def verified_proof(value: Any, label: str) -> dict[str, Any]:
        proof = object_field(value, label)
        claimed = _anomaly_sha256(label, proof.get("proof_sha256"))
        body = dict(proof)
        body.pop("proof_sha256", None)
        canonical = _anomaly_json(body, label=label)
        actual = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if actual != claimed:
            raise ValueError(f"{label} checksum does not match")
        return proof

    case_rule = f"{case['rule_id']}@{case['rule_version']}"
    if case_rule != SEC_FUNDAMENTALS_COVERAGE_RULE:
        refuse(f"unsupported rule {case_rule}")
    if str(case["subject_type"]) != "issuer":
        refuse("the subject must be an issuer")

    try:
        new_value = json.loads(str(case["new_value_json"]))
        evidence = json.loads(str(case["evidence_json"]))
    except json.JSONDecodeError as exc:  # pragma: no cover - projection hash checks first
        raise ValueError("SEC pre-periodic evidence is not valid JSON") from exc
    new_value = object_field(new_value, "missing-coverage state")
    evidence = object_field(evidence, "case evidence")
    if new_value.get("coverage_state") != "missing":
        refuse("coverage state must remain missing")
    exact_zero(new_value.get("accepted_rows"), "accepted rows")
    exact_zero(
        new_value.get("latest_ingest_rows_inserted"),
        "latest ingest rows inserted",
    )

    decision_date = _anomaly_text(
        "decision evidence date",
        evidence.get("detected_as_of"),
    )
    try:
        date.fromisoformat(decision_date)
    except ValueError as exc:
        raise ValueError(
            "SEC pre-periodic acceptance refused: decision date is invalid"
        ) from exc

    issuer = object_field(evidence.get("issuer"), "issuer identity")
    subject_id = str(case["subject_id"])
    cik = _anomaly_text("issuer CIK", issuer.get("cik"))
    if len(cik) != 10 or not cik.isdigit():
        refuse("issuer CIK must be exactly ten digits")
    if (
        issuer.get("issuer_id") != subject_id
        or subject_id != f"aios:issuer:sec:{cik}"
    ):
        refuse("issuer identity does not match the anomaly subject")
    security_id = _anomaly_text("issuer security id", issuer.get("security_id"))
    if issuer.get("canonical_ticker") != issuer.get("ticker"):
        refuse("canonical and observed tickers do not match")
    _anomaly_text("issuer canonical name", issuer.get("canonical_name"))
    _anomaly_text("issuer verified date", issuer.get("verified_date"))

    ingest = object_field(evidence.get("ingest"), "ingest evidence")
    if (
        ingest.get("identity_binding") != "subject_tagged_and_payload_verified"
        or ingest.get("payload_cik") != cik
    ):
        refuse("ingest identity is not bound to the active issuer")
    exact_zero(ingest.get("rows_inserted"), "ingest rows inserted")
    exact_zero(ingest.get("rows_rejected"), "ingest rows rejected")
    ingest_id = integer_field(ingest.get("ingest_id"), "ingest id", minimum=1)
    ingest_run_id = _anomaly_text("ingest run id", ingest.get("run_id"))
    ingest_source = _anomaly_text("ingest source", ingest.get("source"))

    snapshots_raw = ingest.get("snapshots")
    if not isinstance(snapshots_raw, list) or len(snapshots_raw) != 2:
        refuse("exactly two SEC snapshots are required")
    snapshots: dict[str, dict[str, Any]] = {}
    for raw_snapshot in snapshots_raw:
        snapshot = object_field(raw_snapshot, "SEC snapshot")
        role = snapshot.get("role")
        if role not in {"companyfacts", "submissions"} or role in snapshots:
            refuse("snapshot roles must be unique Company Facts and Submissions")
        if (
            snapshot.get("provider") != "sec-edgar"
            or snapshot.get("dataset") != role
            or snapshot.get("artifact_kind") != "exact_response"
            or snapshot.get("http_status") != 200
        ):
            refuse(f"{role} snapshot is not an exact successful SEC response")
        _anomaly_text(f"{role} snapshot id", snapshot.get("snapshot_id"))
        _anomaly_sha256(f"{role} payload", snapshot.get("payload_sha256"))
        _anomaly_sha256(f"{role} parsed rows", snapshot.get("parsed_rows_sha256"))
        _anomaly_moment(snapshot.get("received_at"), label=f"{role} received_at")
        _anomaly_text(f"{role} relative path", snapshot.get("relative_path"))
        snapshots[str(role)] = snapshot
    facts = snapshots["companyfacts"]
    submissions = snapshots["submissions"]
    if facts.get("parser_version") != "sec-companyfacts-v2":
        refuse("Company Facts must use the frozen v2 parser")
    if submissions.get("parser_version") != "sec-submissions-v2":
        refuse("Submissions must use the frozen v2 parser")
    exact_zero(facts.get("parsed_row_count"), "Company Facts parsed row count")
    if facts.get("parsed_rows_sha256") != SEC_EMPTY_ROWSET_SHA256:
        refuse("Company Facts parsed rows are not the canonical empty rowset")
    integer_field(
        submissions.get("parsed_row_count"),
        "Submissions parsed row count",
        minimum=1,
    )

    replay = verified_proof(
        ingest.get("zero_row_replay_proof"),
        "Company Facts zero-row replay proof",
    )
    if (
        replay.get("parser_version") != "sec-companyfacts-v2"
        or replay.get("decision_evidence_as_of") != decision_date
        or replay.get("companyfacts_snapshot_id") != facts.get("snapshot_id")
        or replay.get("companyfacts_payload_sha256") != facts.get("payload_sha256")
    ):
        refuse("zero-row replay is not bound to the exact Company Facts evidence")
    for field_name in ("replayed_rows", "decision_visible_valid_rows"):
        exact_zero(replay.get(field_name), f"replay {field_name}")
    for field_name in ("replayed_rows_sha256", "decision_visible_rows_sha256"):
        if replay.get(field_name) != SEC_EMPTY_ROWSET_SHA256:
            refuse(f"replay {field_name} is not the canonical empty rowset")

    outcome = verified_proof(
        ingest.get("zero_row_outcome_proof"),
        "zero-row ingest outcome proof",
    )
    if (
        outcome.get("run_id") != ingest_run_id
        or outcome.get("source") != ingest_source
        or outcome.get("table_name") != "fundamentals"
        or outcome.get("status") != "warning"
        or outcome.get("ingest_id") != ingest_id
    ):
        refuse("zero-row outcome is not bound to the reviewed ingest")
    exact_zero(outcome.get("rows_inserted"), "outcome rows inserted")
    exact_zero(outcome.get("rows_rejected"), "outcome rows rejected")
    _anomaly_text("zero-row outcome error", outcome.get("error"))

    filing_stage = verified_proof(
        evidence.get("filing_stage"),
        "filing-stage proof",
    )
    if (
        filing_stage.get("context_version") != "sec-filing-stage-context.v1"
        or filing_stage.get("decision_evidence_as_of") != decision_date
    ):
        refuse("filing-stage proof uses a different evidence boundary")
    filing_index = verified_proof(
        filing_stage.get("submissions_filing_index"),
        "Submissions filing-index proof",
    )
    filing_source = object_field(filing_index.get("source"), "filing-index source")
    if (
        filing_index.get("availability") != "exact_submissions_filing_index"
        or filing_index.get("decision_evidence_as_of") != decision_date
        or filing_index.get("pit_filter") != "filingDate <= decision_evidence_as_of"
        or filing_source.get("snapshot_id") != submissions.get("snapshot_id")
        or filing_source.get("payload_sha256") != submissions.get("payload_sha256")
        or filing_source.get("received_at") != submissions.get("received_at")
    ):
        refuse("filing index is not bound to the exact Submissions snapshot")
    integer_field(
        filing_index.get("decision_visible_filing_count"),
        "decision-visible filing count",
    )
    integer_field(
        filing_index.get("excluded_after_decision_count"),
        "post-decision excluded filing count",
    )
    exact_zero(filing_index.get("periodic_form_count"), "periodic form count")
    if filing_index.get("periodic_forms") != []:
        refuse("periodic filing list must be empty")
    integer_field(
        filing_index.get("registration_form_count"),
        "registration form count",
        minimum=1,
    )
    registration_forms = filing_index.get("registration_forms")
    if not isinstance(registration_forms, list) or not registration_forms:
        refuse("at least one registration form is required")
    for item in registration_forms:
        form = object_field(item, "registration form summary")
        _anomaly_text("registration form", form.get("form"))
        integer_field(form.get("filing_count"), "registration filing count", minimum=1)
        for field_name in ("first_filing_date", "latest_filing_date"):
            filing_date = _anomaly_text(field_name, form.get(field_name))
            try:
                parsed_filing_date = date.fromisoformat(filing_date)
            except ValueError as exc:
                raise ValueError(
                    f"SEC pre-periodic acceptance refused: {field_name} is invalid"
                ) from exc
            if parsed_filing_date > date.fromisoformat(decision_date):
                refuse(f"{field_name} exceeds the decision evidence boundary")

    owner = verified_proof(
        filing_stage.get("reviewed_security_owner"),
        "reviewed security-owner proof",
    )
    active = object_field(owner.get("active_issuer"), "active issuer assignment")
    predecessor = object_field(
        owner.get("predecessor_owner"),
        "predecessor issuer assignment",
    )
    predecessor_coverage = verified_proof(
        owner.get("predecessor_fact_coverage"),
        "predecessor fact-coverage proof",
    )
    if (
        owner.get("availability") != "reviewed_assignment_history"
        or owner.get("security_id") != security_id
        or active.get("security_id") != security_id
        or active.get("issuer_id") != subject_id
        or active.get("canonical_name") != issuer.get("canonical_name")
        or active.get("canonical_ticker") != issuer.get("canonical_ticker")
        or owner.get("active_owner_start") != active.get("effective_start")
    ):
        refuse("active security-owner history does not match the issuer")
    if (
        predecessor.get("security_id") != security_id
        or predecessor.get("issuer_id") == subject_id
        or predecessor.get("effective_end") != active.get("effective_start")
        or owner.get("transition_gap_days") != 0
    ):
        refuse("predecessor ownership is not contiguous and distinct")
    if (
        predecessor_coverage.get("state")
        != "not_verified_from_exact_source_replay"
        or predecessor_coverage.get("accepted_rows") is not None
        or predecessor_coverage.get("facts_transfer_to_active_issuer") is not False
    ):
        refuse("predecessor facts are not strictly context-only")

    policy = object_field(filing_stage.get("policy"), "filing-stage policy")
    if (
        policy.get("future_filing_dates_excluded") is not True
        or policy.get("predecessor_facts_are_context_only") is not True
        or policy.get("predecessor_facts_transfer_to_active_issuer") is not False
    ):
        refuse("filing-stage policy permits unsafe evidence transfer")
    exact_zero(policy.get("data_repairs"), "policy data repairs")
    exact_zero(policy.get("readiness_overrides"), "policy readiness overrides")

    if evidence.get("provenance_quality") != "complete":
        refuse("evidence provenance is incomplete")
    scan = connection.execute(
        "SELECT evidence_json FROM anomaly_scans WHERE scan_id = ?",
        (case["last_scan_id"],),
    ).fetchone()
    if scan is None:
        refuse("the current scan manifest is unavailable")
    try:
        scan_evidence = json.loads(str(scan["evidence_json"]))
    except json.JSONDecodeError as exc:  # pragma: no cover - append-only storage
        raise ValueError("SEC pre-periodic scan evidence is invalid") from exc
    scan_evidence = object_field(scan_evidence, "scan evidence")
    if scan_evidence.get("as_of") != decision_date:
        refuse("scan evidence uses a different decision date")
    executed_rules = scan_evidence.get("executed_rules")
    if not isinstance(executed_rules, list) or case_rule not in executed_rules:
        refuse("the governing rule was not executed by the current scan")
    safety = object_field(scan_evidence.get("safety"), "scan safety evidence")
    for field_name in (
        "data_repairs",
        "readiness_overrides",
        "paper_actions",
        "broker_actions",
    ):
        exact_zero(safety.get(field_name), f"scan safety {field_name}")

    return {
        "contract_id": SEC_PRE_PERIODIC_ACCEPTANCE_CONTRACT,
        "case_evidence_sha256": str(case["current_evidence_sha256"]),
        "analytical_effect": {
            "coverage_changed": False,
            "readiness_changed": False,
            "score_created": False,
            "facts_transferred": False,
        },
    }


def _validate_anomaly_clearance_proof(
    scan_evidence: Any,
    case: sqlite3.Row,
) -> None:
    if not isinstance(scan_evidence, dict):
        raise ValueError("anomaly correction scan evidence must be an object")
    proofs = scan_evidence.get("clearance_proofs")
    proof = proofs.get(str(case["fingerprint"])) if isinstance(proofs, dict) else None
    if not isinstance(proof, dict):
        raise ValueError(
            "anomaly correction verification lacks a source-provenanced "
            "clearance proof for this case"
        )
    identity = {
        "rule_id": str(case["rule_id"]),
        "rule_version": str(case["rule_version"]),
        "scope": str(case["scope"]),
        "subject_type": str(case["subject_type"]),
        "subject_id": str(case["subject_id"]),
    }
    if any(proof.get(key) != value for key, value in identity.items()):
        raise ValueError("anomaly clearance proof identity does not match the case")
    if proof.get("coverage_state") != "covered_with_verified_ingest":
        raise ValueError("anomaly clearance proof does not certify covered state")
    for field_name in ("accepted_rows", "ingest_rows_inserted"):
        value = proof.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(
                f"anomaly clearance proof {field_name} must be positive"
            )
    _anomaly_text("clearance ingest run id", proof.get("ingest_run_id"))
    _anomaly_text("clearance warning run id", proof.get("prior_warning_run_id"))

    facts = proof.get("companyfacts_snapshot")
    submissions = proof.get("submissions_snapshot")
    if not isinstance(facts, dict) or not isinstance(submissions, dict):
        raise ValueError("anomaly clearance proof lacks exact SEC snapshots")
    if facts.get("role") != "companyfacts" or submissions.get("role") != "submissions":
        raise ValueError("anomaly clearance proof SEC snapshot roles are invalid")
    _anomaly_sha256("Company Facts payload", facts.get("payload_sha256"))
    _anomaly_sha256("Submissions payload", submissions.get("payload_sha256"))
    _anomaly_sha256("Company Facts parsed rows", facts.get("parsed_rows_sha256"))
    parsed_count = facts.get("parsed_row_count")
    if (
        isinstance(parsed_count, bool)
        or not isinstance(parsed_count, int)
        or parsed_count < 1
    ):
        raise ValueError("anomaly clearance proof parsed row count must be positive")

    claimed_sha256 = _anomaly_sha256(
        "clearance proof",
        proof.get("proof_sha256"),
    )
    proof_body = dict(proof)
    proof_body.pop("proof_sha256", None)
    canonical = _anomaly_json(proof_body, label="anomaly clearance proof")
    actual_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if actual_sha256 != claimed_sha256:
        raise ValueError("anomaly clearance proof checksum does not match")


def _anomaly_text(label: str, value: Any) -> str:
    normalized = str(value).strip() if value is not None else ""
    if not normalized:
        raise ValueError(f"{label} is required")
    if len(normalized) > 4000:
        raise ValueError(f"{label} is too long")
    return normalized


def _anomaly_sha256(label: str, value: Any) -> str:
    normalized = _anomaly_text(f"{label} SHA-256", value).lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{label} must be a 64-character SHA-256")
    return normalized


def _anomaly_json(value: Any, *, label: str) -> str:
    try:
        encoded = json.dumps(
            _redact(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
            default=_anomaly_json_default,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not canonical JSON: {exc}") from exc
    encoded_bytes = encoded.encode("utf-8")
    if len(encoded_bytes) > MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"{label} exceeds the {MAX_PAYLOAD_BYTES}-byte evidence limit"
        )
    return encoded


def _anomaly_object_json(value: Any, *, label: str) -> str:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return _anomaly_json(value, label=label)


def _anomaly_checks_json(checks: tuple[str, ...]) -> str:
    return _anomaly_json(list(checks), label="anomaly suggested checks")


def _anomaly_json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return _normalize_anomaly_time(value, label="anomaly evidence datetime")
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"unsupported anomaly evidence value: {type(value).__name__}")


def _normalize_anomaly_time(
    value: str | datetime,
    *,
    label: str,
    require_timezone: bool = True,
) -> str:
    if isinstance(value, datetime):
        if require_timezone and (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise ValueError(f"{label} must include an explicit timezone")
        moment = (
            value.replace(tzinfo=UTC)
            if value.tzinfo is None or value.utcoffset() is None
            else value.astimezone(UTC)
        )
        return moment.isoformat(timespec="auto").replace("+00:00", "Z")
    normalized = _anomaly_text(label, value)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 datetime") from exc
    if require_timezone and (
        parsed.tzinfo is None or parsed.utcoffset() is None
    ):
        raise ValueError(f"{label} must include an explicit timezone")
    moment = (
        parsed.replace(tzinfo=UTC)
        if parsed.tzinfo is None or parsed.utcoffset() is None
        else parsed.astimezone(UTC)
    )
    return moment.isoformat(timespec="auto").replace("+00:00", "Z")


def _anomaly_moment(value: str | datetime, *, label: str) -> datetime:
    """Parse one governed anomaly time before ordering it.

    ISO-8601 text cannot be ordered safely when otherwise equivalent values use
    different fractional-second precision, so lifecycle comparisons use
    timezone-aware UTC datetimes rather than lexical string order.
    """

    normalized = _normalize_anomaly_time(value, label=label)
    return datetime.fromisoformat(normalized.replace("Z", "+00:00"))


def _anomaly_timestamp(value: datetime | None) -> str:
    """Return an audit timestamp without discarding supplied subsecond evidence."""

    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("operation timestamp must be timezone-aware")
    return _normalize_anomaly_time(
        value or datetime.now(UTC),
        label="anomaly operation timestamp",
    )


def _future_anomaly_time(
    value: str | datetime | None,
    *,
    after: str,
) -> str:
    if value is None:
        raise ValueError("deferred anomaly resolution requires next_review_at")
    normalized = _normalize_anomaly_time(
        value,
        label="next_review_at",
        require_timezone=False,
    )
    future = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    boundary = datetime.fromisoformat(after.replace("Z", "+00:00"))
    if future.tzinfo is None:
        future = future.replace(tzinfo=UTC)
    if boundary.tzinfo is None:
        boundary = boundary.replace(tzinfo=UTC)
    if future <= boundary:
        raise ValueError("deferred anomaly next_review_at must be in the future")
    return normalized


def _higher_anomaly_severity(left: str, right: str) -> str:
    rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    return left if rank[left] >= rank[right] else right


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


def _incident_evidence_sha256(row: sqlite3.Row) -> str:
    """Hash the complete current incident projection without rewriting v6 data."""
    canonical = json.dumps(
        {
            "contract": "aios-incident-evidence.v1",
            "incident_id": str(row["incident_id"]),
            "fingerprint": str(row["fingerprint"]),
            "code": str(row["code"]),
            "severity": str(row["severity"]),
            "title": str(row["title"]),
            "body": str(row["body"]),
            "source_job": str(row["source_job"]),
            "state": str(row["state"]),
            "first_seen_at": str(row["first_seen_at"]),
            "last_seen_at": str(row["last_seen_at"]),
            "occurrence_count": int(row["occurrence_count"]),
            "payload_json": str(row["payload_json"]),
            "acknowledged_at": (
                str(row["acknowledged_at"])
                if row["acknowledged_at"] is not None
                else None
            ),
            "resolved_at": (
                str(row["resolved_at"]) if row["resolved_at"] is not None else None
            ),
            "notifications_enabled": bool(row["notifications_enabled"]),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _incident_action_payload_json(
    *,
    incident_id: str,
    event_type: str,
    timestamp: str,
    actor: str,
    note: str,
    resolution_outcome: str | None,
    expected_evidence_sha256: str,
    resulting_evidence_sha256: str,
    transitioned_state: bool | None = None,
) -> str:
    proof = {
        "contract": "aios-incident-action.v1",
        "incident_id": incident_id,
        "event_type": event_type,
        "created_at": timestamp,
        "actor": actor,
        "note": note,
        "resolution_outcome": resolution_outcome,
        "expected_evidence_sha256": expected_evidence_sha256,
        "resulting_evidence_sha256": resulting_evidence_sha256,
    }
    if transitioned_state is not None:
        proof["transitioned_state"] = transitioned_state
    canonical = json.dumps(
        proof,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    proof["proof_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return json.dumps(
        {INCIDENT_ACTION_AUDIT_KEY: proof},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _incident_json(value: Any, *, label: str) -> str:
    try:
        encoded = json.dumps(
            _redact(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not canonical JSON: {exc}") from exc
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"{label} exceeds the {MAX_PAYLOAD_BYTES}-byte limit")
    return encoded


def _incident_time(value: str | datetime, *, label: str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{label} must include an explicit timezone")
        moment = value.astimezone(UTC)
    else:
        normalized = _incident_action_text(label, value)
        try:
            moment = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} must be an ISO-8601 datetime") from exc
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError(f"{label} must include an explicit timezone")
        moment = moment.astimezone(UTC)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _incident_datetime(value: str, *, label: str) -> datetime:
    normalized = _incident_time(value, label=label)
    return datetime.fromisoformat(normalized.replace("Z", "+00:00"))


def _incident_events_for_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT rowid AS append_order, event_id, incident_id, event_type,
               created_at, payload_json
        FROM incident_events
        WHERE incident_id = ?
        ORDER BY rowid
        """,
        (row["incident_id"],),
    ).fetchall()


def _incident_recovery_context(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> IncidentRecoveryContext:
    events = _incident_events_for_row(connection, row)
    generations = [
        event for event in events if event["event_type"] in {"opened", "reopened"}
    ]
    if not generations:
        raise RuntimeError(
            f"incident {row['incident_id']} has no append-ordered generation event"
        )
    generation = generations[-1]
    resolutions = [
        event
        for event in events
        if event["event_type"] == "resolved"
        and int(event["append_order"]) > int(generation["append_order"])
    ]
    return IncidentRecoveryContext(
        incident_id=str(row["incident_id"]),
        fingerprint=str(row["fingerprint"]),
        generation_event_id=str(generation["event_id"]),
        generation_created_at=str(generation["created_at"]),
        latest_resolution_created_at=(
            str(resolutions[-1]["created_at"]) if resolutions else None
        ),
        evidence_sha256=_incident_evidence_sha256(row),
    )


def _fundamentals_review_observation(
    connection: sqlite3.Connection,
    incident: sqlite3.Row,
) -> dict[str, Any]:
    """Snapshot exact resolved anomaly proofs for one partial fundamentals incident."""

    if (
        str(incident["fingerprint"]) != "refresh:fundamentals:partial"
        or str(incident["code"]) != "current_refresh_partial"
    ):
        raise ValueError(
            "fundamentals reconciliation requires the partial fundamentals incident"
        )
    try:
        payload = json.loads(str(incident["payload_json"]))
    except json.JSONDecodeError as exc:  # pragma: no cover - incident write validates JSON
        raise ValueError("partial-refresh incident payload is invalid") from exc
    if not isinstance(payload, dict) or payload.get("areas") != ["fundamentals"]:
        raise ValueError("partial-refresh incident is not limited to fundamentals")
    identities = payload.get("identities")
    if (
        not isinstance(identities, list)
        or not identities
        or any(not isinstance(value, str) or not value.strip() for value in identities)
        or len(set(identities)) != len(identities)
    ):
        raise ValueError("partial-refresh incident identities are invalid")
    warning_count = payload.get("warning_count")
    if (
        isinstance(warning_count, bool)
        or not isinstance(warning_count, int)
        or warning_count != len(identities)
    ):
        raise ValueError(
            "partial-refresh warning count does not match its reviewed identities"
        )

    reviewed_cases: list[dict[str, Any]] = []
    for identity in sorted(identities):
        rows = connection.execute(
            """
            SELECT * FROM anomaly_cases
            WHERE subject_id = ?
              AND rule_id = 'sec_fundamentals_coverage_missing'
              AND rule_version = '1.0.0'
            ORDER BY case_id
            """,
            (identity,),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError(
                "fundamentals reconciliation requires one unambiguous anomaly "
                f"case for {identity}"
            )
        case = rows[0]
        AlertStore._verify_current_anomaly_evidence(connection, case)
        if str(case["state"]) != "resolved":
            raise ValueError(
                f"fundamentals review remains unresolved for {identity}"
            )
        outcome = str(case["disposition"] or "")
        if outcome not in ANOMALY_RESOLUTION_OUTCOMES - {"deferred"}:
            raise ValueError(
                f"fundamentals review has no final disposition for {identity}"
            )
        resolved_at = _anomaly_text(
            "anomaly resolved_at",
            case["resolved_at"],
        )
        resolution_event = connection.execute(
            """
            SELECT payload_json FROM anomaly_case_events
            WHERE case_id = ? AND event_type = 'resolved'
            ORDER BY event_sequence DESC
            LIMIT 1
            """,
            (case["case_id"],),
        ).fetchone()
        if resolution_event is None:  # pragma: no cover - lifecycle replay catches first
            raise RuntimeError("resolved anomaly case has no immutable resolution event")
        resolution_payload = str(resolution_event["payload_json"])
        reviewed_cases.append(
            {
                "subject_id": identity,
                "case_id": str(case["case_id"]),
                "fingerprint": str(case["fingerprint"]),
                "rule": SEC_FUNDAMENTALS_COVERAGE_RULE,
                "state": "resolved",
                "resolution_outcome": outcome,
                "resolved_at": resolved_at,
                "evidence_sha256": str(case["current_evidence_sha256"]),
                "resolution_event_sha256": hashlib.sha256(
                    resolution_payload.encode("utf-8")
                ).hexdigest(),
            }
        )
    return {
        "contract_id": "fundamentals-review-reconciliation.v1",
        "incident": {
            "areas": ["fundamentals"],
            "identities": sorted(identities),
            "warning_count": warning_count,
        },
        "reviewed_cases": reviewed_cases,
        "safety": {
            "research_data_changed": False,
            "readiness_overridden": False,
            "paper_state_changed": False,
            "broker_action": False,
        },
    }


def build_scheduler_recovery_evidence(
    context: IncidentRecoveryContext,
    status: Mapping[str, Mapping[str, Any]],
    *,
    observed_at: datetime | None = None,
) -> ProducerRecoveryEvidence:
    """Build proof only from a complete live systemd-user runtime response."""
    if context.fingerprint != "scheduler:runtime-unverified":
        raise ValueError("scheduler recovery context has the wrong fingerprint")
    missing = [timer for timer in TIMER_NAMES if timer not in status]
    if missing:
        raise ValueError(
            "scheduler recovery evidence is missing managed timers: "
            + ", ".join(missing)
        )
    timers: dict[str, dict[str, Any]] = {}
    allowed_fields = (
        "enabled",
        "active",
        "last_trigger",
        "last_run",
        "next_trigger",
        "service_result",
        "exit_status",
        "runtime_verified",
    )
    for timer in TIMER_NAMES:
        raw = status[timer]
        if not isinstance(raw, Mapping):
            raise ValueError(f"scheduler status for {timer} must be an object")
        if raw.get("runtime_verified") is not True:
            raise ValueError(
                "scheduler recovery requires live runtime verification for every "
                f"managed timer; {timer} is unverified"
            )
        normalized: dict[str, Any] = {}
        for field_name in allowed_fields:
            value = raw.get(field_name)
            if field_name in {"enabled", "active", "runtime_verified"}:
                if not isinstance(value, bool):
                    raise ValueError(
                        f"scheduler {timer} field {field_name} must be boolean"
                    )
            elif not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"scheduler {timer} field {field_name} is required"
                )
            normalized[field_name] = value
        timers[timer] = normalized
    moment = observed_at or datetime.now(UTC)
    return ProducerRecoveryEvidence(
        incident_id=context.incident_id,
        fingerprint=context.fingerprint,
        generation_event_id=context.generation_event_id,
        expected_evidence_sha256=context.evidence_sha256,
        producer="aios scheduler-status",
        proof_kind="scheduler_runtime_verified",
        observed_at=moment,
        observation={"timers": timers},
    )


def build_daily_cycle_recovery_evidence(
    context: IncidentRecoveryContext,
    job: JobRun,
    readiness: Mapping[str, Any] | Any,
    *,
    assessed_at: datetime | str | None = None,
) -> ProducerRecoveryEvidence:
    """Bind daily recovery to one job receipt and its exact readiness report."""

    if context.fingerprint != "daily:us-cycle:failure":
        raise ValueError("daily-cycle recovery context has the wrong fingerprint")
    receipt = _daily_cycle_v3_receipt(job)
    report = readiness.to_dict() if hasattr(readiness, "to_dict") else readiness
    report_json = _incident_json(report, label="daily-cycle readiness report")
    assessment_time = _incident_time(
        assessed_at or datetime.now(UTC),
        label="daily-cycle readiness assessment time",
    )
    observation = {
        "contract_id": "us-daily-cycle-recovery.v3",
        "job": receipt["job"],
        "result": receipt["result"],
        "safety": receipt["safety"],
        "readiness": {
            "assessed_at": assessment_time,
            "report": json.loads(report_json),
            "report_sha256": hashlib.sha256(report_json.encode("utf-8")).hexdigest(),
        },
    }
    _validate_producer_observation(
        proof_kind="daily_cycle_certified_v3",
        producer="aios refresh-us-daily",
        fingerprint="daily:us-cycle:failure",
        observation=observation,
    )
    return ProducerRecoveryEvidence(
        incident_id=context.incident_id,
        fingerprint=context.fingerprint,
        generation_event_id=context.generation_event_id,
        expected_evidence_sha256=context.evidence_sha256,
        producer="aios refresh-us-daily",
        proof_kind="daily_cycle_certified_v3",
        observed_at=assessment_time,
        observation=observation,
    )


def _prepare_producer_recovery(
    recovery: ProducerRecoveryEvidence,
    *,
    connection: sqlite3.Connection,
    context: IncidentRecoveryContext,
    row: sqlite3.Row,
    recorded_at: str,
) -> _PreparedProducerRecovery:
    if not isinstance(recovery, ProducerRecoveryEvidence):
        raise TypeError("producer recovery requires ProducerRecoveryEvidence")
    incident_id = _incident_action_text("recovery incident id", recovery.incident_id)
    fingerprint = _incident_action_text(
        "recovery incident fingerprint",
        recovery.fingerprint,
    )
    generation_event_id = _incident_action_text(
        "recovery generation event id",
        recovery.generation_event_id,
    )
    expected_evidence = _incident_sha256(
        "expected incident evidence",
        recovery.expected_evidence_sha256,
    )
    producer = _incident_action_text("recovery producer", recovery.producer)
    proof_kind = _incident_action_text("recovery proof kind", recovery.proof_kind)
    if proof_kind not in PRODUCER_RECOVERY_PROOF_KINDS:
        raise ValueError(f"unsupported producer recovery proof kind: {proof_kind}")
    _validate_producer_incident_domain(proof_kind, row)
    if (
        incident_id != context.incident_id
        or incident_id != str(row["incident_id"])
    ):
        raise ValueError("producer recovery incident id is stale or mismatched")
    if (
        fingerprint != context.fingerprint
        or fingerprint != str(row["fingerprint"])
    ):
        raise ValueError("producer recovery fingerprint is stale or mismatched")
    if generation_event_id != context.generation_event_id:
        raise ValueError("producer recovery generation is stale")
    if expected_evidence != context.evidence_sha256:
        raise ValueError("producer recovery incident evidence changed")
    observed = _incident_time(
        recovery.observed_at,
        label="producer recovery observation time",
    )
    observation_moment = _incident_datetime(
        observed,
        label="producer recovery observation time",
    )
    generation_moment = _incident_datetime(
        context.generation_created_at,
        label="incident generation time",
    )
    recorded_moment = _incident_datetime(
        recorded_at,
        label="producer recovery record time",
    )
    if observation_moment < generation_moment:
        raise ValueError("producer recovery observation predates the current generation")
    if observation_moment > recorded_moment:
        raise ValueError("producer recovery observation is later than its record time")
    if context.latest_resolution_created_at is not None:
        latest_resolution = _incident_datetime(
            context.latest_resolution_created_at,
            label="latest incident resolution time",
        )
        if observation_moment <= latest_resolution:
            raise ValueError(
                "producer recovery attestation must be later than the prior resolution"
            )
    if not isinstance(recovery.observation, dict):
        raise ValueError("producer recovery observation must be an object")
    observation_json = _incident_json(
        recovery.observation,
        label="producer recovery observation",
    )
    observation = json.loads(observation_json)
    _validate_producer_observation(
        proof_kind=proof_kind,
        producer=producer,
        fingerprint=fingerprint,
        observation=observation,
    )
    if proof_kind == "fundamentals_review_reconciled":
        current_observation = _fundamentals_review_observation(connection, row)
        if observation != current_observation:
            raise ValueError(
                "fundamentals review evidence changed after reconciliation preview"
            )
    if proof_kind == "daily_cycle_certified_v3":
        _validate_daily_cycle_proof_timing(
            observed_at=observed,
            observation=observation,
            generation_at=context.generation_created_at,
            recorded_at=recorded_at,
        )
        job_id = str(observation["job"]["run_id"])
        current_job = connection.execute(
            "SELECT * FROM job_runs WHERE run_id = ?",
            (job_id,),
        ).fetchone()
        if current_job is None:
            raise ValueError("daily-cycle recovery job receipt is missing")
        try:
            current_observation = _daily_cycle_v3_receipt(
                _job_run_from_row(current_job)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("daily-cycle recovery job evidence changed") from exc
        current_receipt = {
            key: current_observation[key] for key in ("job", "result", "safety")
        }
        recorded_receipt = {
            key: observation[key] for key in ("job", "result", "safety")
        }
        if recorded_receipt != current_receipt:
            raise ValueError("daily-cycle recovery job evidence changed")
        latest_job = connection.execute(
            """
            SELECT run_id FROM job_runs
            WHERE job_name = 'us-daily-refresh'
            ORDER BY started_at DESC, rowid DESC
            LIMIT 1
            """
        ).fetchone()
        if latest_job is None or str(latest_job["run_id"]) != job_id:
            raise ValueError("daily-cycle recovery job is no longer the latest receipt")
    return _PreparedProducerRecovery(
        incident_id=incident_id,
        fingerprint=fingerprint,
        generation_event_id=generation_event_id,
        expected_evidence_sha256=expected_evidence,
        producer=producer,
        proof_kind=proof_kind,
        observed_at=observed,
        observation=observation,
        observation_sha256=hashlib.sha256(
            observation_json.encode("utf-8")
        ).hexdigest(),
    )


def _validate_producer_observation(
    *,
    proof_kind: str,
    producer: str,
    fingerprint: str,
    observation: Mapping[str, Any],
) -> None:
    if proof_kind == "daily_cycle_certified_v3":
        if (
            producer != "aios refresh-us-daily"
            or fingerprint != "daily:us-cycle:failure"
        ):
            raise ValueError("daily-cycle recovery producer or fingerprint is invalid")
        if set(observation) != {
            "contract_id",
            "job",
            "readiness",
            "result",
            "safety",
        } or observation.get("contract_id") != "us-daily-cycle-recovery.v3":
            raise ValueError("daily-cycle recovery contract is invalid")
        job = observation.get("job")
        result = observation.get("result")
        if not isinstance(job, Mapping) or not isinstance(result, Mapping):
            raise ValueError("daily-cycle recovery receipt is invalid")
        if set(job) != {
            "detail",
            "finished_at",
            "job_name",
            "owner_boot_id",
            "owner_pid",
            "payload_sha256",
            "receipt_sha256",
            "run_id",
            "started_at",
            "state",
            "target_session",
        }:
            raise ValueError("daily-cycle recovery job identity is invalid")
        if (
            job.get("job_name") != "us-daily-refresh"
            or job.get("state") != "success"
        ):
            raise ValueError("daily-cycle recovery requires a successful daily job")
        _incident_action_text("daily-cycle run id", job.get("run_id"))
        _incident_action_text("daily-cycle job detail", job.get("detail"))
        _incident_action_text("daily-cycle owner boot id", job.get("owner_boot_id"))
        owner_pid = job.get("owner_pid")
        if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid < 1:
            raise ValueError("daily-cycle owner pid is invalid")
        started_at = _incident_time(
            job.get("started_at"),
            label="daily-cycle job started_at",
        )
        finished_at = _incident_time(
            job.get("finished_at"),
            label="daily-cycle job finished_at",
        )
        if started_at != job.get("started_at") or finished_at != job.get("finished_at"):
            raise ValueError("daily-cycle job timestamps must be canonical")
        if _incident_datetime(finished_at, label="daily-cycle job finished_at") < (
            _incident_datetime(started_at, label="daily-cycle job started_at")
        ):
            raise ValueError("daily-cycle job finished before it started")
        target_text = _incident_action_text(
            "daily-cycle target session",
            job.get("target_session"),
        )
        try:
            target = date.fromisoformat(target_text).isoformat()
        except ValueError as exc:
            raise ValueError(
                "daily-cycle target session must be an ISO date"
            ) from exc
        if target != target_text:
            raise ValueError("daily-cycle target session must be canonical")
        from aios.market_calendar import latest_completed_us_equity_session

        if latest_completed_us_equity_session(
            _incident_datetime(started_at, label="daily-cycle job started_at")
        ).isoformat() != target:
            raise ValueError("daily-cycle target does not match its job start time")
        if set(result) != {
            "benchmark_rows",
            "certified_research_through",
            "interrupted_run_ids",
            "macro_rows",
            "member_count",
            "member_price_rows",
            "run_id",
            "status",
            "target_session",
            "universe_coverage_through",
            "universe_status",
            "warning_count",
        }:
            raise ValueError("daily-cycle recovery result schema is invalid")
        _validate_daily_cycle_result(job, result, target)
        payload_json = _incident_json(result, label="daily-cycle job payload")
        if _incident_sha256(
            "daily-cycle job payload",
            job.get("payload_sha256"),
        ) != hashlib.sha256(payload_json.encode("utf-8")).hexdigest():
            raise ValueError("daily-cycle job payload hash is invalid")
        receipt = {key: value for key, value in job.items() if key != "receipt_sha256"}
        receipt["payload"] = result
        receipt_json = _incident_json(receipt, label="daily-cycle full job receipt")
        if _incident_sha256(
            "daily-cycle full job receipt",
            job.get("receipt_sha256"),
        ) != hashlib.sha256(receipt_json.encode("utf-8")).hexdigest():
            raise ValueError("daily-cycle full job receipt hash is invalid")
        if observation.get("safety") != {
            "broker_action": False,
            "fundamentals_refreshed": False,
            "paper_state_changed": False,
            "readiness_overridden": False,
        }:
            raise ValueError("daily-cycle recovery safety contract is invalid")
        _validate_daily_cycle_v3_readiness(observation.get("readiness"), target)
        return
    if proof_kind in {
        "daily_cycle_certified",
        "daily_cycle_readiness_certified",
    }:
        if (
            producer != "aios refresh-us-daily"
            or fingerprint != "daily:us-cycle:failure"
        ):
            raise ValueError("daily-cycle recovery producer or fingerprint is invalid")
        expected_contract = (
            "us-daily-cycle-recovery.v2"
            if proof_kind == "daily_cycle_readiness_certified"
            else "us-daily-cycle-recovery.v1"
        )
        expected_fields = {"contract_id", "job", "result", "safety"}
        if proof_kind == "daily_cycle_readiness_certified":
            expected_fields.add("readiness")
        if set(observation) != expected_fields or (
            observation.get("contract_id") != expected_contract
        ):
            raise ValueError("daily-cycle recovery contract is invalid")
        job = observation.get("job")
        result = observation.get("result")
        if not isinstance(job, Mapping) or not isinstance(result, Mapping):
            raise ValueError("daily-cycle recovery receipt is invalid")
        if set(job) != {
            "run_id",
            "job_name",
            "state",
            "target_session",
            "started_at",
            "finished_at",
            "payload_sha256",
        }:
            raise ValueError("daily-cycle recovery job identity is invalid")
        if (
            job.get("job_name") != "us-daily-refresh"
            or job.get("state") != "success"
        ):
            raise ValueError("daily-cycle recovery requires a successful daily job")
        _incident_action_text("daily-cycle run id", job.get("run_id"))
        _incident_time(job.get("started_at"), label="daily-cycle job started_at")
        _incident_time(job.get("finished_at"), label="daily-cycle job finished_at")
        target_text = _incident_action_text(
            "daily-cycle target session",
            job.get("target_session"),
        )
        try:
            target = date.fromisoformat(target_text).isoformat()
        except ValueError as exc:
            raise ValueError(
                "daily-cycle target session must be an ISO date"
            ) from exc
        _incident_sha256("daily-cycle job payload", job.get("payload_sha256"))
        if set(result) != {
            "benchmark_rows",
            "certified_research_through",
            "interrupted_run_ids",
            "macro_rows",
            "member_count",
            "member_price_rows",
            "run_id",
            "status",
            "target_session",
            "universe_coverage_through",
            "universe_status",
            "warning_count",
        }:
            raise ValueError("daily-cycle recovery result schema is invalid")
        _validate_daily_cycle_result(job, result, target)
        if observation.get("safety") != {
            "broker_action": False,
            "fundamentals_refreshed": False,
            "paper_state_changed": False,
            "readiness_overridden": False,
        }:
            raise ValueError("daily-cycle recovery safety contract is invalid")
        if proof_kind == "daily_cycle_readiness_certified":
            _validate_daily_cycle_readiness(observation.get("readiness"), target)
        return
    if proof_kind == "fundamentals_review_reconciled":
        if (
            producer != "aios alert-reconcile-fundamentals"
            or fingerprint != "refresh:fundamentals:partial"
        ):
            raise ValueError(
                "fundamentals recovery producer or fingerprint is invalid"
            )
        if set(observation) != {
            "contract_id",
            "incident",
            "reviewed_cases",
            "safety",
        } or observation.get("contract_id") != (
            "fundamentals-review-reconciliation.v1"
        ):
            raise ValueError("fundamentals recovery contract is invalid")
        incident = observation.get("incident")
        if not isinstance(incident, Mapping) or set(incident) != {
            "areas",
            "identities",
            "warning_count",
        }:
            raise ValueError("fundamentals recovery incident evidence is invalid")
        identities = incident.get("identities")
        if (
            incident.get("areas") != ["fundamentals"]
            or not isinstance(identities, list)
            or not identities
            or identities != sorted(identities)
            or len(set(identities)) != len(identities)
            or any(not isinstance(value, str) or not value for value in identities)
            or isinstance(incident.get("warning_count"), bool)
            or incident.get("warning_count") != len(identities)
        ):
            raise ValueError("fundamentals recovery identity set is invalid")
        cases = observation.get("reviewed_cases")
        if not isinstance(cases, list) or len(cases) != len(identities):
            raise ValueError("fundamentals recovery case set is incomplete")
        observed_identities: list[str] = []
        for case in cases:
            if not isinstance(case, Mapping) or set(case) != {
                "subject_id",
                "case_id",
                "fingerprint",
                "rule",
                "state",
                "resolution_outcome",
                "resolved_at",
                "evidence_sha256",
                "resolution_event_sha256",
            }:
                raise ValueError("fundamentals recovery case evidence is invalid")
            subject_id = _incident_action_text(
                "fundamentals recovery subject id",
                case.get("subject_id"),
            )
            observed_identities.append(subject_id)
            _incident_action_text(
                "fundamentals recovery case id",
                case.get("case_id"),
            )
            _incident_action_text(
                "fundamentals recovery fingerprint",
                case.get("fingerprint"),
            )
            if (
                case.get("rule") != SEC_FUNDAMENTALS_COVERAGE_RULE
                or case.get("state") != "resolved"
                or case.get("resolution_outcome")
                not in ANOMALY_RESOLUTION_OUTCOMES - {"deferred"}
            ):
                raise ValueError(
                    "fundamentals recovery case has no final governed disposition"
                )
            _incident_time(
                case.get("resolved_at"),
                label="fundamentals recovery case resolved_at",
            )
            _incident_sha256(
                "fundamentals recovery case evidence",
                case.get("evidence_sha256"),
            )
            _incident_sha256(
                "fundamentals recovery resolution event",
                case.get("resolution_event_sha256"),
            )
        if observed_identities != identities:
            raise ValueError(
                "fundamentals recovery cases do not match incident identities"
            )
        if observation.get("safety") != {
            "research_data_changed": False,
            "readiness_overridden": False,
            "paper_state_changed": False,
            "broker_action": False,
        }:
            raise ValueError("fundamentals recovery safety contract is invalid")
        return
    if proof_kind == "scheduler_runtime_verified":
        if (
            producer != "aios scheduler-status"
            or fingerprint != "scheduler:runtime-unverified"
        ):
            raise ValueError("scheduler recovery producer or fingerprint is invalid")
        timers = observation.get("timers")
        if not isinstance(timers, Mapping) or set(timers) != set(TIMER_NAMES):
            raise ValueError(
                "scheduler recovery observation must cover every managed timer"
            )
        for timer in TIMER_NAMES:
            state = timers.get(timer)
            if not isinstance(state, Mapping):
                raise ValueError(f"scheduler recovery status for {timer} is invalid")
            if state.get("runtime_verified") is not True:
                raise ValueError(
                    "scheduler recovery cannot use file-only or sandbox status"
                )
        return
    raise ValueError(f"unsupported producer recovery proof kind: {proof_kind}")


def _daily_cycle_recovery_observation(job: JobRun) -> dict[str, Any]:
    if not isinstance(job, JobRun):
        raise TypeError("daily-cycle recovery requires a JobRun receipt")
    payload_json = _incident_json(job.payload, label="daily-cycle job payload")
    observation = {
        "contract_id": "us-daily-cycle-recovery.v1",
        "job": {
            "run_id": job.run_id,
            "job_name": job.job_name,
            "state": job.state,
            "target_session": job.target_session,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "payload_sha256": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        },
        "result": job.payload,
        "safety": {
            "broker_action": False,
            "fundamentals_refreshed": False,
            "paper_state_changed": False,
            "readiness_overridden": False,
        },
    }
    _validate_producer_observation(
        proof_kind="daily_cycle_certified",
        producer="aios refresh-us-daily",
        fingerprint="daily:us-cycle:failure",
        observation=observation,
    )
    return observation


def _daily_cycle_v3_receipt(job: JobRun) -> dict[str, Any]:
    if not isinstance(job, JobRun):
        raise TypeError("daily-cycle recovery requires a JobRun receipt")
    payload_json = _incident_json(job.payload, label="daily-cycle job payload")
    job_identity: dict[str, Any] = {
        "detail": job.detail,
        "finished_at": job.finished_at,
        "job_name": job.job_name,
        "owner_boot_id": job.owner_boot_id,
        "owner_pid": job.owner_pid,
        "payload_sha256": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        "run_id": job.run_id,
        "started_at": job.started_at,
        "state": job.state,
        "target_session": job.target_session,
    }
    receipt_material = dict(job_identity)
    receipt_material["payload"] = job.payload
    receipt_json = _incident_json(
        receipt_material,
        label="daily-cycle full job receipt",
    )
    job_identity["receipt_sha256"] = hashlib.sha256(
        receipt_json.encode("utf-8")
    ).hexdigest()
    return {
        "job": job_identity,
        "result": job.payload,
        "safety": {
            "broker_action": False,
            "fundamentals_refreshed": False,
            "paper_state_changed": False,
            "readiness_overridden": False,
        },
    }


def _validate_daily_cycle_result(
    job: Mapping[str, Any],
    result: Mapping[str, Any],
    target: str,
) -> None:
    if (
        result.get("run_id") != job.get("run_id")
        or result.get("target_session") != target
        or result.get("certified_research_through") != target
        or result.get("universe_coverage_through") != target
    ):
        raise ValueError("daily-cycle recovery does not certify its exact target")
    status = result.get("status")
    if status not in {"completed", "already_current"}:
        raise ValueError("daily-cycle recovery status is invalid")
    member_count = _daily_cycle_nonnegative_int(result, "member_count")
    benchmark_rows = _daily_cycle_nonnegative_int(result, "benchmark_rows")
    member_price_rows = _daily_cycle_nonnegative_int(result, "member_price_rows")
    macro_rows = _daily_cycle_nonnegative_int(result, "macro_rows")
    _daily_cycle_nonnegative_int(result, "warning_count")
    if not 450 <= member_count <= 550:
        raise ValueError("daily-cycle recovery member count is invalid")
    if status == "completed" and (
        benchmark_rows < 1
        or member_price_rows < member_count
        or macro_rows < 1
        or result.get("universe_status") not in {"extended", "up_to_date"}
    ):
        raise ValueError("daily-cycle completed receipt is incomplete")
    if status == "already_current" and (
        benchmark_rows != 0
        or member_price_rows != 0
        or macro_rows != 0
        or result.get("universe_status") != "already_current"
    ):
        raise ValueError("daily-cycle already-current receipt is invalid")
    interrupted = result.get("interrupted_run_ids")
    if (
        not isinstance(interrupted, list)
        or len(interrupted) != len(set(interrupted))
        or any(not isinstance(value, str) or not value for value in interrupted)
    ):
        raise ValueError("daily-cycle interrupted-run evidence is invalid")


def _daily_cycle_nonnegative_int(
    result: Mapping[str, Any],
    field_name: str,
) -> int:
    value = result.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"daily-cycle recovery {field_name} is invalid")
    return value


DAILY_READINESS_CHECKS = (
    "decision_date",
    "data_integrity",
    "universe_membership",
    "stable_security_identity",
    "fundamental_coverage",
    "price_history_coverage",
    "reviewed_price_freshness",
    "benchmark_freshness",
    "macro_pit_readiness",
)


def _validate_daily_cycle_v3_readiness(
    readiness: Any,
    target: str,
) -> None:
    if not isinstance(readiness, Mapping) or set(readiness) != {
        "assessed_at",
        "report",
        "report_sha256",
    }:
        raise ValueError("daily-cycle readiness evidence is invalid")
    assessed_at = _incident_time(
        readiness.get("assessed_at"),
        label="daily-cycle readiness assessment time",
    )
    if assessed_at != readiness.get("assessed_at"):
        raise ValueError("daily-cycle readiness assessment time must be canonical")
    report = readiness.get("report")
    if not isinstance(report, Mapping) or set(report) != {
        "as_of",
        "benchmark_ticker",
        "certified_research_from",
        "certified_research_through",
        "checks",
        "fundamentals_through",
        "generated_on",
        "macro_releases_through",
        "purpose",
        "raw_prices_through",
        "ready",
        "universe_id",
    }:
        raise ValueError("daily-cycle readiness report schema is invalid")
    if (
        report.get("purpose") != "paper"
        or report.get("as_of") != target
        or report.get("generated_on") != target
        or report.get("certified_research_through") != target
        or report.get("universe_id") != "sp500"
        or report.get("benchmark_ticker") != "SPY"
    ):
        raise ValueError("daily-cycle readiness does not certify its exact target")
    try:
        target_date = date.fromisoformat(target)
    except ValueError as exc:  # pragma: no cover - target validated by its receipt
        raise ValueError("daily-cycle readiness target is invalid") from exc
    date_fields: dict[str, date | None] = {}
    for field_name in (
        "certified_research_from",
        "certified_research_through",
        "fundamentals_through",
        "macro_releases_through",
        "raw_prices_through",
    ):
        value = report.get(field_name)
        if value is None:
            date_fields[field_name] = None
            continue
        text = _incident_action_text(
            f"daily-cycle readiness {field_name}",
            value,
        )
        try:
            parsed = date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                f"daily-cycle readiness {field_name} must be an ISO date"
            ) from exc
        if parsed.isoformat() != text:
            raise ValueError(
                f"daily-cycle readiness {field_name} must be canonical"
            )
        date_fields[field_name] = parsed
    certified_from = date_fields["certified_research_from"]
    if certified_from is None or certified_from > target_date:
        raise ValueError("daily-cycle certified research start is invalid")
    for field_name in (
        "certified_research_through",
        "fundamentals_through",
        "macro_releases_through",
        "raw_prices_through",
    ):
        value = date_fields[field_name]
        if value is None or value > target_date:
            raise ValueError(
                f"daily-cycle readiness {field_name} exceeds its generation date"
            )
    checks = report.get("checks")
    if not isinstance(checks, list) or len(checks) != len(DAILY_READINESS_CHECKS):
        raise ValueError("daily-cycle readiness checks are incomplete")
    observed_names: list[str] = []
    statuses: list[str] = []
    for check in checks:
        if not isinstance(check, Mapping) or set(check) != {
            "check",
            "detail",
            "label",
            "observed",
            "required",
            "status",
        }:
            raise ValueError("daily-cycle readiness check schema is invalid")
        observed_names.append(
            _incident_action_text(
                "daily-cycle readiness check name",
                check.get("check"),
            )
        )
        status = check.get("status")
        if status not in {"pass", "warn", "fail"}:
            raise ValueError("daily-cycle readiness check status is invalid")
        statuses.append(str(status))
        for field_name in ("detail", "label", "observed", "required"):
            _incident_action_text(
                f"daily-cycle readiness check {field_name}",
                check.get(field_name),
            )
    if tuple(observed_names) != DAILY_READINESS_CHECKS:
        raise ValueError("daily-cycle readiness check set or order is invalid")
    computed_ready = all(status != "fail" for status in statuses)
    if report.get("ready") is not computed_ready or not computed_ready:
        raise ValueError("daily-cycle readiness report is not fully ready")
    report_json = _incident_json(report, label="daily-cycle readiness report")
    expected_sha = hashlib.sha256(report_json.encode("utf-8")).hexdigest()
    actual_sha = _incident_sha256(
        "daily-cycle readiness report",
        readiness.get("report_sha256"),
    )
    if actual_sha != expected_sha:
        raise ValueError("daily-cycle readiness report hash is invalid")


def _validate_daily_cycle_proof_timing(
    *,
    observed_at: str,
    observation: Mapping[str, Any],
    generation_at: str,
    recorded_at: str,
) -> None:
    job = observation["job"]
    readiness = observation["readiness"]
    assessed_at = _incident_time(
        readiness["assessed_at"],
        label="daily-cycle readiness assessment time",
    )
    if observed_at != assessed_at:
        raise ValueError("daily-cycle proof time must equal its readiness assessment")
    started = _incident_datetime(
        str(job["started_at"]),
        label="daily-cycle job started_at",
    )
    finished = _incident_datetime(
        str(job["finished_at"]),
        label="daily-cycle job finished_at",
    )
    assessed = _incident_datetime(
        assessed_at,
        label="daily-cycle readiness assessment time",
    )
    generation = _incident_datetime(generation_at, label="incident generation time")
    recorded = _incident_datetime(recorded_at, label="producer recovery record time")
    if started < generation:
        raise ValueError("daily-cycle job predates the current incident generation")
    if not started <= finished <= assessed <= recorded:
        raise ValueError("daily-cycle recovery timestamps are not causally ordered")


def _validate_producer_incident_domain(
    proof_kind: str,
    row: sqlite3.Row,
) -> None:
    if proof_kind not in {
        "daily_cycle_certified",
        "daily_cycle_readiness_certified",
        "daily_cycle_certified_v3",
    }:
        return
    if (
        str(row["code"]) != "daily_us_cycle_failed"
        or str(row["source_job"]) != "aios refresh-us-daily"
        or str(row["fingerprint"]) != "daily:us-cycle:failure"
        or str(row["severity"]) != "critical"
    ):
        raise ValueError("daily-cycle recovery incident domain is invalid")


def _validate_daily_cycle_readiness(
    readiness: Any,
    target: str,
) -> None:
    if not isinstance(readiness, Mapping) or set(readiness) != {
        "report",
        "report_sha256",
    }:
        raise ValueError("daily-cycle readiness evidence is invalid")
    report = readiness.get("report")
    if not isinstance(report, Mapping) or set(report) != {
        "as_of",
        "benchmark_ticker",
        "certified_research_from",
        "certified_research_through",
        "checks",
        "fundamentals_through",
        "generated_on",
        "macro_releases_through",
        "purpose",
        "raw_prices_through",
        "ready",
        "universe_id",
    }:
        raise ValueError("daily-cycle readiness report schema is invalid")
    if (
        report.get("ready") is not True
        or report.get("purpose") != "paper"
        or report.get("as_of") != target
        or report.get("certified_research_through") != target
        or report.get("universe_id") != "sp500"
        or report.get("benchmark_ticker") != "SPY"
    ):
        raise ValueError("daily-cycle readiness does not certify its exact target")
    generated_on = _incident_action_text(
        "daily-cycle readiness generated_on",
        report.get("generated_on"),
    )
    try:
        date.fromisoformat(generated_on)
    except ValueError as exc:
        raise ValueError(
            "daily-cycle readiness generated_on must be an ISO date"
        ) from exc
    checks = report.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("daily-cycle readiness checks are missing")
    check_names: set[str] = set()
    for check in checks:
        if not isinstance(check, Mapping) or set(check) != {
            "check",
            "detail",
            "label",
            "observed",
            "required",
            "status",
        }:
            raise ValueError("daily-cycle readiness check schema is invalid")
        name = _incident_action_text(
            "daily-cycle readiness check name",
            check.get("check"),
        )
        if name in check_names:
            raise ValueError("daily-cycle readiness checks must be unique")
        check_names.add(name)
        if check.get("status") not in {"pass", "warn"}:
            raise ValueError("daily-cycle readiness contains a failing check")
        for field_name in ("detail", "label", "observed", "required"):
            _incident_action_text(
                f"daily-cycle readiness check {field_name}",
                check.get(field_name),
            )
    report_json = _incident_json(report, label="daily-cycle readiness report")
    expected_sha = hashlib.sha256(report_json.encode("utf-8")).hexdigest()
    actual_sha = _incident_sha256(
        "daily-cycle readiness report",
        readiness.get("report_sha256"),
    )
    if actual_sha != expected_sha:
        raise ValueError("daily-cycle readiness report hash is invalid")


def _producer_recovery_payload_json(
    prepared: _PreparedProducerRecovery,
    *,
    resulting_evidence_sha256: str,
    transitioned_state: bool,
) -> str:
    proof = {
        "contract": "aios-incident-recovery.v1",
        "incident_id": prepared.incident_id,
        "fingerprint": prepared.fingerprint,
        "producer": prepared.producer,
        "proof_kind": prepared.proof_kind,
        "observed_at": prepared.observed_at,
        "current_generation_event_id": prepared.generation_event_id,
        "observation": prepared.observation,
        "observation_sha256": prepared.observation_sha256,
        "expected_evidence_sha256": prepared.expected_evidence_sha256,
        "resulting_evidence_sha256": _incident_sha256(
            "resulting incident evidence",
            resulting_evidence_sha256,
        ),
        "transitioned_state": bool(transitioned_state),
    }
    canonical = _incident_json(proof, label="producer recovery proof")
    proof["proof_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return _incident_json(
        {INCIDENT_PRODUCER_RECOVERY_KEY: proof},
        label="producer recovery event",
    )


def _manual_action_proof(row: sqlite3.Row, payload: Mapping[str, Any]) -> dict[str, Any]:
    action = payload.get(INCIDENT_ACTION_AUDIT_KEY)
    if not isinstance(action, dict):
        raise ValueError("incident action audit proof must be an object")
    claimed_sha256 = _incident_sha256(
        "incident action proof",
        action.get("proof_sha256"),
    )
    proof = dict(action)
    proof.pop("proof_sha256", None)
    canonical = _incident_json(proof, label="incident action proof")
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != claimed_sha256:
        raise ValueError("incident action audit proof does not match its SHA-256")
    if (
        proof.get("contract") != "aios-incident-action.v1"
        or proof.get("incident_id") != row["incident_id"]
        or proof.get("event_type") != row["event_type"]
        or proof.get("created_at") != row["created_at"]
    ):
        raise ValueError("incident action audit proof does not match its event")
    if _incident_action_text("incident action actor", proof.get("actor")) != proof.get(
        "actor"
    ):
        raise ValueError("incident action actor is not canonical")
    if _incident_action_text("incident action note", proof.get("note")) != proof.get(
        "note"
    ):
        raise ValueError("incident action note is not canonical")
    outcome = proof.get("resolution_outcome")
    if proof.get("event_type") == "resolved":
        if _manual_incident_resolution_outcome(outcome) != outcome:
            raise ValueError("incident resolution outcome is not canonical")
    elif outcome is not None:
        raise ValueError(
            "non-resolution incident action cannot have a resolution outcome"
        )
    if _incident_sha256(
        "expected incident evidence",
        proof.get("expected_evidence_sha256"),
    ) != proof.get("expected_evidence_sha256"):
        raise ValueError("expected incident evidence is not canonical")
    if _incident_sha256(
        "resulting incident evidence",
        proof.get("resulting_evidence_sha256"),
    ) != proof.get("resulting_evidence_sha256"):
        raise ValueError("resulting incident evidence is not canonical")
    if "transitioned_state" in proof and not isinstance(
        proof["transitioned_state"],
        bool,
    ):
        raise ValueError("incident action transitioned_state must be boolean")
    proof["proof_sha256"] = claimed_sha256
    return proof


def _producer_recovery_proof(
    row: sqlite3.Row,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    raw = payload.get(INCIDENT_PRODUCER_RECOVERY_KEY)
    if not isinstance(raw, dict):
        raise ValueError("producer recovery proof must be an object")
    claimed_sha256 = _incident_sha256(
        "producer recovery proof",
        raw.get("proof_sha256"),
    )
    proof = dict(raw)
    proof.pop("proof_sha256", None)
    canonical = _incident_json(proof, label="producer recovery proof")
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != claimed_sha256:
        raise ValueError("producer recovery proof does not match its SHA-256")
    if (
        proof.get("contract") != "aios-incident-recovery.v1"
        or proof.get("incident_id") != row["incident_id"]
        or row["event_type"] != "resolved"
    ):
        raise ValueError("producer recovery proof does not match its event")
    fingerprint = _incident_action_text(
        "producer recovery fingerprint",
        proof.get("fingerprint"),
    )
    producer = _incident_action_text(
        "producer recovery producer",
        proof.get("producer"),
    )
    proof_kind = _incident_action_text(
        "producer recovery proof kind",
        proof.get("proof_kind"),
    )
    _incident_time(
        proof.get("observed_at"),
        label="producer recovery observation time",
    )
    generation_event_id = _incident_action_text(
        "producer recovery generation event id",
        proof.get("current_generation_event_id"),
    )
    observation = proof.get("observation")
    if not isinstance(observation, dict):
        raise ValueError("producer recovery observation must be an object")
    observation_json = _incident_json(
        observation,
        label="producer recovery observation",
    )
    observation_sha256 = _incident_sha256(
        "producer recovery observation",
        proof.get("observation_sha256"),
    )
    if hashlib.sha256(observation_json.encode("utf-8")).hexdigest() != (
        observation_sha256
    ):
        raise ValueError("producer recovery observation does not match its SHA-256")
    _incident_sha256(
        "expected incident evidence",
        proof.get("expected_evidence_sha256"),
    )
    _incident_sha256(
        "resulting incident evidence",
        proof.get("resulting_evidence_sha256"),
    )
    if not isinstance(proof.get("transitioned_state"), bool):
        raise ValueError("producer recovery transitioned_state must be boolean")
    _validate_producer_observation(
        proof_kind=proof_kind,
        producer=producer,
        fingerprint=fingerprint,
        observation=observation,
    )
    proof["current_generation_event_id"] = generation_event_id
    proof["proof_sha256"] = claimed_sha256
    return proof


def _incident_event_from_row(
    row: sqlite3.Row,
    *,
    strict: bool = True,
) -> dict[str, Any]:
    event = {
        "event_id": str(row["event_id"]),
        "event_type": str(row["event_type"]),
        "created_at": str(row["created_at"]),
    }
    try:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise ValueError("incident event payload must be an object")
        event["payload"] = payload
        has_manual = INCIDENT_ACTION_AUDIT_KEY in payload
        has_producer = INCIDENT_PRODUCER_RECOVERY_KEY in payload
        if has_manual and has_producer:
            raise ValueError("incident event contains conflicting resolution proofs")
        if has_manual:
            proof = _manual_action_proof(row, payload)
            manual_fields = {
                "actor": str(proof["actor"]),
                "note": str(proof["note"]),
                "resolution_outcome": proof.get("resolution_outcome"),
                "proof_sha256": str(proof["proof_sha256"]),
                "expected_evidence_sha256": str(
                    proof["expected_evidence_sha256"]
                ),
                "resulting_evidence_sha256": str(
                    proof["resulting_evidence_sha256"]
                ),
            }
            if "transitioned_state" in proof:
                manual_fields["transitioned_state"] = bool(
                    proof["transitioned_state"]
                )
            event.update(manual_fields)
        elif has_producer:
            proof = _producer_recovery_proof(row, payload)
            event.update(
                {
                    "producer": str(proof["producer"]),
                    "proof_kind": str(proof["proof_kind"]),
                    "proof_sha256": str(proof["proof_sha256"]),
                    "expected_evidence_sha256": str(
                        proof["expected_evidence_sha256"]
                    ),
                    "resulting_evidence_sha256": str(
                        proof["resulting_evidence_sha256"]
                    ),
                    "transitioned_state": bool(proof["transitioned_state"]),
                }
            )
    except (TypeError, ValueError) as exc:
        if strict:
            raise ValueError(str(exc)) from exc
        event["payload"] = None
        event["proof_error"] = str(exc)
    return event


def classify_incident_resolution(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> IncidentResolutionAssessment:
    events = _incident_events_for_row(connection, row)
    generations = [
        event for event in events if event["event_type"] in {"opened", "reopened"}
    ]
    generation = generations[-1] if generations else None
    synthetic = (
        str(row["code"]),
        str(row["fingerprint"]),
    ) in SYNTHETIC_NON_GATING_INCIDENTS
    if str(row["state"]) != "resolved":
        return IncidentResolutionAssessment(
            resolution_proof_status="not_applicable",
            operationally_blocking=not synthetic,
            generation_event_id=(
                str(generation["event_id"]) if generation is not None else None
            ),
        )
    if generation is None:
        return IncidentResolutionAssessment(
            resolution_proof_status="invalid",
            operationally_blocking=not synthetic,
            generation_event_id=None,
        )
    resolutions = [
        event
        for event in events
        if event["event_type"] == "resolved"
        and int(event["append_order"]) > int(generation["append_order"])
    ]
    if not resolutions:
        return IncidentResolutionAssessment(
            resolution_proof_status="invalid",
            operationally_blocking=not synthetic,
            generation_event_id=str(generation["event_id"]),
        )
    resolution = resolutions[-1]
    try:
        payload = json.loads(str(resolution["payload_json"]))
        if not isinstance(payload, dict):
            raise ValueError("incident resolution payload must be an object")
        has_manual = INCIDENT_ACTION_AUDIT_KEY in payload
        has_producer = INCIDENT_PRODUCER_RECOVERY_KEY in payload
        if has_manual and has_producer:
            raise ValueError("incident resolution contains conflicting proofs")
        current_evidence = _incident_evidence_sha256(row)
        if has_manual:
            proof = _manual_action_proof(resolution, payload)
            if proof.get("event_type") != "resolved":
                raise ValueError("manual resolution proof is not a resolved event")
            if proof.get("resulting_evidence_sha256") != current_evidence:
                raise ValueError(
                    "manual resolution proof does not match current incident evidence"
                )
            generation_at = _incident_datetime(
                str(generation["created_at"]),
                label="incident generation time",
            )
            resolution_at = _incident_datetime(
                str(resolution["created_at"]),
                label="manual resolution event time",
            )
            if resolution_at < generation_at:
                raise ValueError("manual resolution event predates its generation")
            prior_resolutions = resolutions[:-1]
            transitioned_state = not prior_resolutions
            if "transitioned_state" in proof and (
                bool(proof["transitioned_state"]) != transitioned_state
            ):
                raise ValueError(
                    "manual resolution transition claim does not match lifecycle"
                )
            if prior_resolutions:
                prior_resolution_at = _incident_datetime(
                    str(prior_resolutions[-1]["created_at"]),
                    label="prior incident resolution time",
                )
                if resolution_at <= prior_resolution_at:
                    raise ValueError(
                        "manual resolution attestation is not later than the "
                        "prior resolution"
                    )
                if proof.get("expected_evidence_sha256") != current_evidence:
                    raise ValueError(
                        "manual resolution attestation does not match current "
                        "incident evidence"
                    )
            status = (
                "manual_verified_recovery"
                if proof.get("resolution_outcome") == "verified_recovery"
                else "manual_false_positive"
            )
        elif has_producer:
            proof = _producer_recovery_proof(resolution, payload)
            proof_kind = str(proof["proof_kind"])
            _validate_producer_incident_domain(proof_kind, row)
            if proof.get("fingerprint") != row["fingerprint"]:
                raise ValueError(
                    "producer recovery fingerprint does not match the incident"
                )
            if proof.get("current_generation_event_id") != generation["event_id"]:
                raise ValueError("producer recovery proof is for a stale generation")
            if proof.get("resulting_evidence_sha256") != current_evidence:
                raise ValueError(
                    "producer recovery proof does not match current incident evidence"
                )
            observed_at = _incident_datetime(
                str(proof["observed_at"]),
                label="producer recovery observation time",
            )
            generation_at = _incident_datetime(
                str(generation["created_at"]),
                label="incident generation time",
            )
            resolution_at = _incident_datetime(
                str(resolution["created_at"]),
                label="producer recovery event time",
            )
            if observed_at < generation_at:
                raise ValueError(
                    "producer recovery observation predates its generation"
                )
            if observed_at > resolution_at:
                raise ValueError(
                    "producer recovery observation is later than its event"
                )
            if proof_kind == "daily_cycle_certified_v3":
                _validate_daily_cycle_proof_timing(
                    observed_at=str(proof["observed_at"]),
                    observation=proof["observation"],
                    generation_at=str(generation["created_at"]),
                    recorded_at=str(resolution["created_at"]),
                )
            prior_resolutions = resolutions[:-1]
            if prior_resolutions:
                prior_resolution_at = _incident_datetime(
                    str(prior_resolutions[-1]["created_at"]),
                    label="prior incident resolution time",
                )
                if observed_at <= prior_resolution_at:
                    raise ValueError(
                        "producer recovery attestation is not later than the "
                        "prior resolution"
                    )
            if bool(proof.get("transitioned_state")) != (not prior_resolutions):
                raise ValueError(
                    "producer recovery transition claim does not match lifecycle"
                )
            status = "producer_verified_recovery"
        else:
            status = "legacy_unproven"
    except (TypeError, ValueError):
        status = "invalid"
    return IncidentResolutionAssessment(
        resolution_proof_status=status,
        operationally_blocking=(
            status in {"legacy_unproven", "invalid"} and not synthetic
        ),
        generation_event_id=str(generation["event_id"]),
    )


def verify_incident_event_evidence(row: sqlite3.Row) -> None:
    """Verify one v7 incident lifecycle event's optional action proof."""
    _incident_event_from_row(row)


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
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("operation timestamp must be timezone-aware")
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


def _incident_from_row(
    row: sqlite3.Row,
    *,
    assessment: IncidentResolutionAssessment | None = None,
    resolution_proof_status: str | None = None,
) -> Incident:
    status = (
        resolution_proof_status
        or (
            assessment.resolution_proof_status
            if assessment is not None
            else (
                "not_applicable"
                if str(row["state"]) != "resolved"
                else "legacy_unproven"
            )
        )
    )
    if status not in INCIDENT_RESOLUTION_PROOF_STATUSES:
        raise ValueError(f"unsupported incident resolution proof status: {status}")
    synthetic = (
        str(row["code"]),
        str(row["fingerprint"]),
    ) in SYNTHETIC_NON_GATING_INCIDENTS
    operationally_blocking = (
        assessment.operationally_blocking
        if assessment is not None
        else (
            str(row["state"]) != "resolved"
            or status in {"legacy_unproven", "invalid"}
        )
        and not synthetic
    )
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
        evidence_sha256=_incident_evidence_sha256(row),
        resolution_proof_status=status,
        operationally_blocking=operationally_blocking,
    )


def _anomaly_case_from_row(row: sqlite3.Row | None) -> AnomalyCase:
    if row is None:  # pragma: no cover - callers select inside a write transaction
        raise RuntimeError("anomaly case disappeared during read")
    old_value = json.loads(str(row["old_value_json"]))
    new_value = json.loads(str(row["new_value_json"]))
    evidence = json.loads(str(row["evidence_json"]))
    checks = json.loads(str(row["suggested_checks_json"]))
    if not isinstance(old_value, dict):
        raise ValueError("stored anomaly old value is not an object")
    if not isinstance(new_value, dict):
        raise ValueError("stored anomaly new value is not an object")
    if not isinstance(evidence, dict):
        raise ValueError("stored anomaly evidence is not an object")
    if not isinstance(checks, list) or any(
        not isinstance(value, str) for value in checks
    ):
        raise ValueError("stored anomaly suggested checks are invalid")
    return AnomalyCase(
        case_id=str(row["case_id"]),
        fingerprint=str(row["fingerprint"]),
        rule_id=str(row["rule_id"]),
        rule_version=str(row["rule_version"]),
        scope=str(row["scope"]),
        subject_type=str(row["subject_type"]),
        subject_id=str(row["subject_id"]),
        severity=str(row["severity"]),
        confidence=str(row["confidence"]),
        title=str(row["title"]),
        summary=str(row["summary"]),
        state=str(row["state"]),
        owner=str(row["owner"]) if row["owner"] is not None else None,
        first_seen_at=str(row["first_seen_at"]),
        last_seen_at=str(row["last_seen_at"]),
        occurrence_count=int(row["occurrence_count"]),
        old_value=old_value,
        new_value=new_value,
        evidence=evidence,
        suggested_checks=tuple(checks),
        evidence_sha256=str(row["current_evidence_sha256"]),
        last_scan_id=str(row["last_scan_id"]),
        acknowledged_at=(
            str(row["acknowledged_at"])
            if row["acknowledged_at"] is not None
            else None
        ),
        resolution_outcome=(
            str(row["disposition"]) if row["disposition"] is not None else None
        ),
        resolution_note=(
            str(row["resolution_note"])
            if row["resolution_note"] is not None
            else None
        ),
        resolved_at=str(row["resolved_at"]) if row["resolved_at"] is not None else None,
        next_review_at=(
            str(row["next_review_at"])
            if row["next_review_at"] is not None
            else None
        ),
        verification_scan_id=(
            str(row["verification_scan_id"])
            if row["verification_scan_id"] is not None
            else None
        ),
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

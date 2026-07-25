"""Durable local incidents that remain writable when the analytical DB is unavailable."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from aios.config import settings
from aios.scheduler import MANAGED_SERVICE_NAMES, STATUS_QUERY_TIMEOUT_SECONDS

ALERT_SCHEMA_VERSION = 2
MAX_PAYLOAD_BYTES = 64 * 1024
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


class AlertStore:
    """Independent SQLite incident ledger with bounded concurrent writes."""

    def __init__(self, path: Path) -> None:
        requested = Path(path).expanduser()
        if requested.is_symlink():
            raise ValueError(f"operations database cannot be a symbolic link: {requested}")
        self.path = requested.resolve()
        self._initialize()

    def _initialize(self) -> None:
        if self.path.is_symlink():
            raise ValueError(f"operations database cannot be a symbolic link: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
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
                    resolved_at TEXT
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
                """
            )
            connection.execute(f"PRAGMA user_version = {ALERT_SCHEMA_VERSION}")
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

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
                connection.execute(
                    """
                    INSERT INTO incidents (
                        incident_id, fingerprint, code, severity, title, body, source_job,
                        state, first_seen_at, last_seen_at, occurrence_count, payload_json,
                        acknowledged_at, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, 1, ?, NULL, NULL)
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
                    ),
                )
            else:
                incident_id = str(existing["incident_id"])
                event_type = "repeated" if existing["state"] == "open" else "reopened"
                severity = _higher_severity(str(existing["severity"]), alert.severity.value)
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
            self._insert_event(connection, incident_id, event_type, timestamp, payload_json)
            row = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
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

    def resolve(self, incident_id: str, *, now: datetime | None = None) -> Incident:
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
                self._insert_event(connection, incident_id, "resolved", timestamp, "{}")
            updated = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
        return _incident_from_row(updated)

    def resolve_fingerprint(
        self, fingerprint: str, *, now: datetime | None = None
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
                self._insert_event(connection, incident_id, "resolved", timestamp, "{}")
            updated = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
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
                         last_seen_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_incident_from_row(row) for row in rows]

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
    ) -> None:
        connection.execute(
            """
            INSERT INTO incident_events (
                event_id, incident_id, event_type, created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (f"evt-{uuid4().hex}", incident_id, event_type, timestamp, payload_json),
        )


def get_alert_store(path: Path | None = None) -> AlertStore:
    """Open the independent operations ledger at its configured local path."""
    configured = path or settings.operations_db_path
    resolved = configured if configured.is_absolute() else settings.project_root / configured
    return AlertStore(resolved)


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


def _validate_job_text(label: str, value: str) -> None:
    if not str(value).strip():
        raise ValueError(f"job {label} is required")
    if len(str(value)) > 4000:
        raise ValueError(f"job {label} is too long")


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

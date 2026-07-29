"""Strictly read-only evidence adapters for operator-facing status views."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from aios.alerts import ALERT_SCHEMA_VERSION, NOTIFICATION_STATES
from aios.daily import DAILY_JOB_NAME
from aios.forward import (
    DEFAULT_FORWARD_RELATIVE_PATH,
    assess_forward_trial,
    read_forward_trial,
)
from aios.paper import (
    ACCOUNT_DOCUMENT_KIND,
    DEFAULT_ACCOUNT_RELATIVE_PATH,
    PROPOSAL_DOCUMENT_KIND,
    paper_account_summary,
    paper_proposal_timing_status,
    read_paper_document,
)
from aios.storage.store import Store


def _project_relative(root: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return str(resolved)


def _record_path(root: Path, record: dict[str, Any]) -> Path:
    raw_value = record.get("path")
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ValueError("registered proposal path is missing")
    raw = Path(raw_value)
    candidate = raw if raw.is_absolute() else root / raw
    if candidate.is_symlink():
        raise ValueError(f"registered proposal path is a symbolic link: {candidate}")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("registered proposal path escapes the project root") from exc
    if not resolved.is_file():
        raise ValueError(f"registered proposal path is missing or unsafe: {resolved}")
    return resolved


def _latest_registered_record(
    payload: dict[str, Any],
    *,
    executed_ids: set[str],
) -> dict[str, Any] | None:
    records = [row for row in payload.get("proposals", []) if isinstance(row, dict)]
    if not records:
        return None
    pending = [
        row
        for row in records
        if str(row.get("proposal_id") or "") not in executed_ids
    ]
    candidates = pending or records
    latest_date = max(str(row.get("decision_date") or "") for row in candidates)
    latest = [
        row
        for row in candidates
        if str(row.get("decision_date") or "") == latest_date
    ]
    if len(latest) != 1:
        raise ValueError(
            "active forward trial is ambiguous: multiple registered proposals "
            f"share latest decision date {latest_date or 'unknown'}"
        )
    return latest[0]


def load_paper_monitor_evidence(
    project_root: Path,
    store: Store,
    *,
    now=None,
    proposal_path: Path | None = None,
) -> dict[str, Any]:
    """Read account, active-trial, and registered-proposal evidence without writes.

    The proposal is resolved from the active forward document rather than from
    the newest filename in ``data/paper/proposals``.  A stray or partially
    registered file therefore cannot silently replace the operator's active
    proposal view.
    """

    root = Path(project_root).resolve()
    account_path = root / DEFAULT_ACCOUNT_RELATIVE_PATH
    account_label = _project_relative(root, account_path)
    if not account_path.exists():
        return {
            "exists": False,
            "account_path": account_label,
            "account_payload_sha256": None,
            "summary": None,
            "proposal": None,
            "proposal_path": None,
            "proposal_payload_sha256": None,
            "forward": None,
            "trial_path": _project_relative(
                root, root / DEFAULT_FORWARD_RELATIVE_PATH
            ),
            "trial_payload_sha256": None,
        }

    account_document = read_paper_document(
        account_path,
        expected_kind=ACCOUNT_DOCUMENT_KIND,
    )
    summary = paper_account_summary(account_path, store)
    executed_ids = {
        str(row.get("proposal_id"))
        for row in account_document.payload.get("executions", [])
        if row.get("proposal_id")
    }

    requested_proposal_path = proposal_path
    proposal_payload: dict[str, Any] | None = None
    selected_proposal_path: Path | None = None
    proposal_sha256: str | None = None
    trial_path = root / DEFAULT_FORWARD_RELATIVE_PATH
    trial_sha256: str | None = None
    forward: dict[str, Any] | None = None

    if trial_path.exists():
        try:
            trial_document = read_forward_trial(trial_path)
            trial_sha256 = trial_document.payload_sha256
            records = [
                row
                for row in trial_document.payload.get("proposals", [])
                if isinstance(row, dict)
            ]
            # Validate every registered path before calling the frozen forward
            # assessor, which reads all registered proposal documents.
            for registered in records:
                _record_path(root, registered)
            status = assess_forward_trial(root, trial_path, account_path)
            if requested_proposal_path is None:
                record = _latest_registered_record(
                    trial_document.payload,
                    executed_ids=executed_ids,
                )
            else:
                requested = Path(requested_proposal_path)
                requested = (
                    requested.resolve()
                    if requested.is_absolute()
                    else (root / requested).resolve()
                )
                record = next(
                    (
                        candidate
                        for candidate in records
                        if _record_path(root, candidate) == requested
                    ),
                    None,
                )
                if record is None:
                    raise ValueError(
                        "requested proposal is not registered in the active forward trial"
                    )
            forward = {
                "ready": status.ready,
                "policy_unchanged": status.policy_unchanged,
                "trial_id": status.trial_id,
                "registered_proposals": status.registered_proposals,
                "issues": list(status.issues),
            }
            if record is not None:
                selected_proposal_path = _record_path(root, record)
                document = read_paper_document(
                    selected_proposal_path,
                    expected_kind=PROPOSAL_DOCUMENT_KIND,
                )
                proposal_sha256 = document.payload_sha256
                proposal_payload = dict(document.payload)
                proposal_payload["registered_in_forward"] = (
                    record.get("proposal_id") == proposal_payload.get("proposal_id")
                    and record.get("payload_sha256") == document.payload_sha256
                )
                proposal_payload["already_simulated"] = (
                    str(proposal_payload.get("proposal_id")) in executed_ids
                )
                proposal_payload["account_matches_proposal"] = (
                    proposal_payload.get("account_id")
                    == account_document.payload.get("account_id")
                    and proposal_payload.get("account_payload_sha256")
                    == account_document.payload_sha256
                )
                try:
                    proposal_payload["timing"] = paper_proposal_timing_status(
                        proposal_payload,
                        now=now,
                    )
                except ValueError as exc:
                    proposal_payload["timing"] = {
                        "status": "invalid",
                        "detail": f"The proposal timing evidence is invalid: {exc}",
                    }
                if (
                    read_paper_document(
                        selected_proposal_path,
                        expected_kind=PROPOSAL_DOCUMENT_KIND,
                    ).payload_sha256
                    != proposal_sha256
                ):
                    raise ValueError("registered proposal changed while evidence was read")
        except (OSError, ValueError) as exc:
            proposal_payload = None
            selected_proposal_path = None
            proposal_sha256 = None
            forward = {
                "ready": False,
                "policy_unchanged": False,
                "trial_id": "unavailable",
                "registered_proposals": 0,
                "issues": [str(exc)],
            }

    if (
        read_paper_document(
            account_path,
            expected_kind=ACCOUNT_DOCUMENT_KIND,
        ).payload_sha256
        != account_document.payload_sha256
    ):
        raise ValueError("paper account changed while evidence was read")
    if (
        trial_sha256 is not None
        and forward is not None
        and forward.get("ready")
        and read_forward_trial(trial_path).payload_sha256 != trial_sha256
    ):
        raise ValueError("forward trial changed while evidence was read")

    return {
        "exists": True,
        "account_path": account_label,
        "account_payload_sha256": account_document.payload_sha256,
        "summary": summary,
        "proposal": proposal_payload,
        "proposal_path": (
            _project_relative(root, selected_proposal_path)
            if selected_proposal_path is not None
            else None
        ),
        "proposal_payload_sha256": proposal_sha256,
        "forward": forward,
        "trial_path": _project_relative(root, trial_path),
        "trial_payload_sha256": trial_sha256,
    }


def _operations_unavailable(path: Path, error: str) -> dict[str, Any]:
    return {
        "ledger_path": str(path),
        "schema_version": None,
        "incidents": [],
        "incident_summary": {},
        "daily_cycle": None,
        "notifications": [],
        "notification_summary": {
            state: 0 for state in sorted(NOTIFICATION_STATES)
        },
        "notification_route": None,
        "error": error,
    }


def _read_json_object(raw: Any, *, field: str) -> dict[str, Any]:
    value = json.loads(str(raw))
    if not isinstance(value, dict):
        raise ValueError(f"{field} must contain a JSON object")
    return value


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    metadata = path.stat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def load_operations_evidence_read_only(
    path: Path,
    *,
    incident_limit: int = 100,
    notification_limit: int = 100,
    notification_channel: str = "smtp-email",
    notification_route_alias: str = "primary",
) -> dict[str, Any]:
    """Read the existing SQLite operations ledger without initialization.

    ``AlertStore`` is intentionally not used here because its constructor owns
    schema migration and WAL setup. An uncheckpointed WAL is refused before the
    immutable connection is opened, so the operator view never silently ignores
    recent incident or notification evidence.
    """

    source = Path(path).expanduser()
    if incident_limit < 1 or incident_limit > 1000:
        raise ValueError("incident limit must be between 1 and 1000")
    if notification_limit < 1 or notification_limit > 1000:
        raise ValueError("notification limit must be between 1 and 1000")
    if source.is_symlink():
        return _operations_unavailable(
            source,
            "operations ledger cannot be read through a symbolic link",
        )
    if not source.is_file():
        return _operations_unavailable(source, "operations ledger is not initialized")

    resolved = source.resolve()
    wal_path = Path(f"{resolved}-wal")
    if wal_path.exists() and wal_path.stat().st_size:
        return _operations_unavailable(
            resolved,
            "operations ledger has an uncheckpointed WAL; retry after the writer closes",
        )
    identity_before = _file_identity(resolved)
    uri = f"{resolved.as_uri()}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if schema_version != ALERT_SCHEMA_VERSION:
                return _operations_unavailable(
                    resolved,
                    (
                        f"operations schema {schema_version} does not match required "
                        f"{ALERT_SCHEMA_VERSION}; run a normal health command to migrate it"
                    ),
                )
            rows = connection.execute(
                """
                SELECT *
                FROM incidents
                ORDER BY
                    CASE state
                        WHEN 'open' THEN 0
                        WHEN 'acknowledged' THEN 1
                        ELSE 2
                    END,
                    CASE severity
                        WHEN 'critical' THEN 0
                        WHEN 'warning' THEN 1
                        ELSE 2
                    END,
                    last_seen_at DESC
                LIMIT ?
                """,
                (incident_limit,),
            ).fetchall()
            incidents = [
                {
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
                    "payload": _read_json_object(
                        row["payload_json"],
                        field="incident payload",
                    ),
                    "acknowledged_at": (
                        str(row["acknowledged_at"])
                        if row["acknowledged_at"] is not None
                        else None
                    ),
                    "resolved_at": (
                        str(row["resolved_at"])
                        if row["resolved_at"] is not None
                        else None
                    ),
                    "notifications_enabled": bool(row["notifications_enabled"]),
                }
                for row in rows
            ]
            summary = {
                "open": 0,
                "acknowledged": 0,
                "resolved": 0,
                "unresolved": 0,
                "critical_unresolved": 0,
                "total": 0,
            }
            for row in connection.execute(
                "SELECT state, severity, COUNT(*) AS count FROM incidents "
                "GROUP BY state, severity"
            ).fetchall():
                state = str(row["state"])
                count = int(row["count"])
                if state in summary:
                    summary[state] += count
                summary["total"] += count
                if state != "resolved":
                    summary["unresolved"] += count
                    if row["severity"] == "critical":
                        summary["critical_unresolved"] += count
            job = connection.execute(
                """
                SELECT *
                FROM job_runs
                WHERE job_name = ?
                ORDER BY started_at DESC, rowid DESC
                LIMIT 1
                """,
                (DAILY_JOB_NAME,),
            ).fetchone()
            daily_cycle = (
                {
                    "run_id": str(job["run_id"]),
                    "job_name": str(job["job_name"]),
                    "state": str(job["state"]),
                    "target_session": str(job["target_session"]),
                    "started_at": str(job["started_at"]),
                    "finished_at": (
                        str(job["finished_at"])
                        if job["finished_at"] is not None
                        else None
                    ),
                    "owner_pid": int(job["owner_pid"]),
                    "owner_boot_id": str(job["owner_boot_id"]),
                    "detail": str(job["detail"]),
                    "payload": _read_json_object(job["payload_json"], field="job payload"),
                }
                if job is not None
                else None
            )
            notification_rows = connection.execute(
                """
                SELECT *
                FROM notification_outbox
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
                (notification_limit,),
            ).fetchall()
            notifications = [
                {
                    "notification_id": str(row["notification_id"]),
                    "idempotency_key": str(row["idempotency_key"]),
                    "incident_id": (
                        str(row["incident_id"])
                        if row["incident_id"] is not None
                        else None
                    ),
                    "source_event_id": (
                        str(row["source_event_id"])
                        if row["source_event_id"] is not None
                        else None
                    ),
                    "route_activation_id": (
                        str(row["route_activation_id"])
                        if row["route_activation_id"] is not None
                        else None
                    ),
                    "depends_on_notification_id": (
                        str(row["depends_on_notification_id"])
                        if row["depends_on_notification_id"] is not None
                        else None
                    ),
                    "message_schema_version": int(row["message_schema_version"]),
                    "event_type": str(row["event_type"]),
                    "severity": str(row["severity"]),
                    "title": str(row["title"]),
                    "body": str(row["body"]),
                    "source_job": str(row["source_job"]),
                    "payload": _read_json_object(
                        row["payload_json"],
                        field="notification payload",
                    ),
                    "state": str(row["state"]),
                    "created_at": str(row["created_at"]),
                    "available_at": str(row["available_at"]),
                    "attempt_count": int(row["attempt_count"]),
                    "lease_token": (
                        str(row["lease_token"])
                        if row["lease_token"] is not None
                        else None
                    ),
                    "lease_expires_at": (
                        str(row["lease_expires_at"])
                        if row["lease_expires_at"] is not None
                        else None
                    ),
                    "delivered_at": (
                        str(row["delivered_at"])
                        if row["delivered_at"] is not None
                        else None
                    ),
                    "dead_lettered_at": (
                        str(row["dead_lettered_at"])
                        if row["dead_lettered_at"] is not None
                        else None
                    ),
                    "last_error_type": (
                        str(row["last_error_type"])
                        if row["last_error_type"] is not None
                        else None
                    ),
                }
                for row in notification_rows
            ]
            notification_summary = {
                state: 0 for state in sorted(NOTIFICATION_STATES)
            }
            for row in connection.execute(
                """
                SELECT state, COUNT(*) AS count
                FROM notification_outbox
                GROUP BY state
                """
            ).fetchall():
                notification_summary[str(row["state"])] = int(row["count"])
            route = connection.execute(
                """
                SELECT *
                FROM notification_routes
                WHERE channel = ? AND route_alias = ?
                """,
                (notification_channel, notification_route_alias),
            ).fetchone()
            notification_route = (
                {
                    "route_id": str(route["route_id"]),
                    "channel": str(route["channel"]),
                    "route_alias": str(route["route_alias"]),
                    "activation_id": str(route["activation_id"]),
                    "state": str(route["state"]),
                    "config_fingerprint": str(route["config_fingerprint"]),
                    "enabled_at": (
                        str(route["enabled_at"])
                        if route["enabled_at"] is not None
                        else None
                    ),
                    "disabled_at": (
                        str(route["disabled_at"])
                        if route["disabled_at"] is not None
                        else None
                    ),
                    "updated_at": str(route["updated_at"]),
                }
                if route is not None
                else None
            )
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        return _operations_unavailable(resolved, str(exc))
    try:
        wal_changed = wal_path.exists() and wal_path.stat().st_size > 0
        identity_changed = _file_identity(resolved) != identity_before
    except OSError as exc:
        return _operations_unavailable(resolved, str(exc))
    if wal_changed or identity_changed:
        return _operations_unavailable(
            resolved,
            "operations ledger changed while read-only evidence was collected; retry",
        )

    return {
        "ledger_path": str(resolved),
        "schema_version": ALERT_SCHEMA_VERSION,
        "incidents": incidents,
        "incident_summary": summary,
        "daily_cycle": daily_cycle,
        "notifications": notifications,
        "notification_summary": notification_summary,
        "notification_route": notification_route,
        "error": None,
    }

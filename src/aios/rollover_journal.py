"""Append-only evidence for governed forward-rollover transactions.

The journal is independent from the activation orchestrator so backup/restore
validation can understand transaction state without importing mutation code.
Every phase is write-once and checksum protected. A missing terminal phase
means paper state must be recovered before normal use.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aios.paper import canonical_payload_sha256

ROLLOVER_ATTEMPT_DOCUMENT_KIND = "aios.forward-rollover-attempt"
ROLLOVER_ATTEMPT_SCHEMA_VERSION = "forward-rollover-attempt.v1"

PHASE_SEQUENCE = {
    "prepared": 1,
    "outputs_published": 2,
    "active_swapped": 3,
    "verified": 4,
    "recovered_rolled_back": 4,
}
PHASE_FILENAMES = {
    phase: f"{sequence:02d}-{phase.replace('_', '-')}.json"
    for phase, sequence in PHASE_SEQUENCE.items()
}
TERMINAL_PHASES = frozenset({"verified", "recovered_rolled_back"})
_PAYLOAD_FIELDS = frozenset(
    {
        "attempt_schema_version",
        "attempt_id",
        "rollover_key",
        "plan_sha256",
        "phase",
        "sequence",
        "recorded_at",
        "prepared_payload_sha256",
        "plan",
        "backup",
        "state",
        "boundary",
    }
)
_BOUNDARY = {
    "paper_account_mutation": False,
    "paper_fill_recorded": False,
    "broker_order_sent": False,
    "retrospective_fill": False,
    "predecessor_rewrite": False,
}


@dataclass(frozen=True)
class RolloverAttemptDocument:
    """One checksum-verified phase in an append-only attempt journal."""

    path: Path
    payload: dict[str, Any]
    payload_sha256: str

    @property
    def phase(self) -> str:
        return str(self.payload["phase"])


@dataclass(frozen=True)
class RolloverAttemptState:
    """The validated sequence currently present in one attempt directory."""

    path: Path
    documents: tuple[RolloverAttemptDocument, ...]

    @property
    def prepared(self) -> RolloverAttemptDocument:
        return self.documents[0]

    @property
    def latest(self) -> RolloverAttemptDocument:
        return self.documents[-1]

    @property
    def terminal(self) -> bool:
        return self.latest.phase in TERMINAL_PHASES


def build_attempt_payload(
    *,
    attempt_id: str,
    rollover_key: str,
    plan_sha256: str,
    phase: str,
    recorded_at: datetime,
    state: dict[str, Any],
    prepared_payload_sha256: str | None = None,
    plan: dict[str, Any] | None = None,
    backup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the exact canonical payload accepted by the journal reader."""

    if phase not in PHASE_SEQUENCE:
        raise ValueError("rollover attempt phase is invalid")
    moment = _aware_utc(recorded_at)
    payload = {
        "attempt_schema_version": ROLLOVER_ATTEMPT_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "rollover_key": rollover_key,
        "plan_sha256": plan_sha256,
        "phase": phase,
        "sequence": PHASE_SEQUENCE[phase],
        "recorded_at": moment.isoformat().replace("+00:00", "Z"),
        "prepared_payload_sha256": prepared_payload_sha256,
        "plan": plan,
        "backup": backup,
        "state": state,
        "boundary": dict(_BOUNDARY),
    }
    _validate_attempt_payload(payload)
    return payload


def write_attempt_phase(
    attempt_directory: Path,
    payload: dict[str, Any],
) -> RolloverAttemptDocument:
    """Publish one phase exactly once and fsync both file and directory."""

    normalized = _canonical_mapping(payload, label="rollover attempt payload")
    _validate_attempt_payload(normalized)
    directory = Path(attempt_directory)
    _require_safe_directory(directory, create=True)
    destination = directory / PHASE_FILENAMES[str(normalized["phase"])]
    if destination.exists() or destination.is_symlink():
        existing = read_attempt_phase(destination)
        if existing.payload != normalized:
            raise ValueError(f"rollover attempt phase collision: {destination}")
        return existing

    envelope = {
        "document_kind": ROLLOVER_ATTEMPT_DOCUMENT_KIND,
        "schema_version": ROLLOVER_ATTEMPT_SCHEMA_VERSION,
        "payload_sha256": canonical_payload_sha256(normalized),
        "payload": normalized,
    }
    encoded = (_canonical_json(envelope) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("rollover attempt phase must be one unaliased regular file")
        written = 0
        while written < len(encoded):
            written += os.write(descriptor, encoded[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(directory)
    return read_attempt_phase(destination)


def read_attempt_phase(path: Path) -> RolloverAttemptDocument:
    """Read and strictly validate one checksum-protected journal phase."""

    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_nlink != 1:
        raise ValueError(f"rollover attempt phase is missing or unsafe: {source}")
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"rollover attempt phase is unreadable: {source}") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "document_kind",
        "schema_version",
        "payload_sha256",
        "payload",
    }:
        raise ValueError("unsupported rollover attempt envelope")
    payload = raw.get("payload")
    stored = raw.get("payload_sha256")
    if (
        raw.get("document_kind") != ROLLOVER_ATTEMPT_DOCUMENT_KIND
        or raw.get("schema_version") != ROLLOVER_ATTEMPT_SCHEMA_VERSION
        or not _lower_sha256(stored)
        or not isinstance(payload, dict)
    ):
        raise ValueError("unsupported rollover attempt envelope")
    normalized = _canonical_mapping(payload, label="rollover attempt payload")
    _validate_attempt_payload(normalized)
    actual = canonical_payload_sha256(normalized)
    if not hmac.compare_digest(str(stored), actual):
        raise ValueError("rollover attempt checksum mismatch")
    expected_name = PHASE_FILENAMES[str(normalized["phase"])]
    if source.name != expected_name:
        raise ValueError("rollover attempt phase filename is inconsistent")
    return RolloverAttemptDocument(source, normalized, actual)


def validate_attempt_directory(path: Path) -> RolloverAttemptState:
    """Validate one complete or recoverable append-only phase sequence."""

    directory = Path(path)
    _require_safe_directory(directory, create=False)
    children = sorted(directory.iterdir())
    if any(child.is_symlink() or not child.is_file() for child in children):
        raise ValueError(f"rollover attempt directory contains an unsafe entry: {directory}")
    allowed_names = set(PHASE_FILENAMES.values())
    unexpected = [child.name for child in children if child.name not in allowed_names]
    if unexpected:
        raise ValueError(
            f"rollover attempt directory contains an unsupported file: {unexpected[0]}"
        )
    documents = tuple(read_attempt_phase(child) for child in children)
    if not documents or documents[0].phase != "prepared":
        raise ValueError("rollover attempt has no prepared phase")

    prepared = documents[0]
    if canonical_payload_sha256(prepared.payload["plan"]) != prepared.payload["plan_sha256"]:
        raise ValueError("rollover prepared phase is bound to another plan")
    if directory.name != prepared.payload["attempt_id"]:
        raise ValueError("rollover attempt directory name is inconsistent")
    if (
        directory.parent.name != "attempts"
        or directory.parent.parent.name != prepared.payload["rollover_key"]
    ):
        raise ValueError("rollover journal path is inconsistent")

    common = {key: prepared.payload[key] for key in ("attempt_id", "rollover_key", "plan_sha256")}
    seen: set[str] = set()
    last_sequence = 0
    last_timestamp: datetime | None = None
    terminal_seen = False
    for index, document in enumerate(documents):
        payload = document.payload
        phase = document.phase
        if phase in seen:
            raise ValueError("rollover attempt repeats a phase")
        seen.add(phase)
        if terminal_seen:
            raise ValueError("rollover attempt has evidence after a terminal phase")
        if any(payload[key] != value for key, value in common.items()):
            raise ValueError("rollover attempt phases have inconsistent identity")
        sequence = int(payload["sequence"])
        if sequence <= last_sequence:
            raise ValueError("rollover attempt phases are out of order")
        recorded_at = _parse_timestamp(payload["recorded_at"])
        if last_timestamp is not None and recorded_at < last_timestamp:
            raise ValueError("rollover attempt timestamps are out of order")
        if index > 0 and payload["prepared_payload_sha256"] != prepared.payload_sha256:
            raise ValueError("rollover attempt phase is bound to another prepared record")
        if phase in TERMINAL_PHASES:
            terminal_seen = True
        last_sequence = sequence
        last_timestamp = recorded_at

    if "active_swapped" in seen and "outputs_published" not in seen:
        raise ValueError("rollover active swap has no output-publication evidence")
    if "verified" in seen and "active_swapped" not in seen:
        raise ValueError("verified rollover has no active-swap evidence")
    return RolloverAttemptState(directory, documents)


def scan_attempt_directories(project_root: Path) -> tuple[Path, ...]:
    """Return every safe attempt directory in deterministic order."""

    root = Path(project_root).resolve()
    namespace = root / "data" / "paper" / "rollovers"
    if not namespace.exists():
        return ()
    _require_safe_directory(namespace, create=False)
    attempts: list[Path] = []
    for rollover_directory in sorted(namespace.iterdir()):
        if rollover_directory.is_symlink() or not rollover_directory.is_dir():
            raise ValueError("rollover journal namespace contains an unsafe entry")
        attempts_root = rollover_directory / "attempts"
        if not attempts_root.exists():
            continue
        _require_safe_directory(attempts_root, create=False)
        for attempt in sorted(attempts_root.iterdir()):
            if attempt.is_symlink() or not attempt.is_dir():
                raise ValueError("rollover attempts namespace contains an unsafe entry")
            attempts.append(attempt)
    return tuple(attempts)


def attempt_directory(
    project_root: Path,
    rollover_key: str,
    attempt_id: str,
) -> Path:
    """Resolve the canonical directory for one attempt identity."""

    if not _lower_sha256(rollover_key):
        raise ValueError("rollover key is invalid")
    if not _lower_hex(attempt_id, length=32):
        raise ValueError("rollover attempt ID is invalid")
    root = Path(project_root).resolve()
    destination = root / "data" / "paper" / "rollovers" / rollover_key / "attempts" / attempt_id
    try:
        destination.relative_to(root)
    except ValueError as exc:  # pragma: no cover
        raise ValueError("rollover attempt directory escapes the project root") from exc
    return destination


def _validate_attempt_payload(payload: dict[str, Any]) -> None:
    if set(payload) != _PAYLOAD_FIELDS:
        raise ValueError("rollover attempt payload schema is invalid")
    phase = payload.get("phase")
    if (
        payload.get("attempt_schema_version") != ROLLOVER_ATTEMPT_SCHEMA_VERSION
        or phase not in PHASE_SEQUENCE
        or payload.get("sequence") != PHASE_SEQUENCE.get(str(phase))
        or not _lower_hex(payload.get("attempt_id"), length=32)
        or not _lower_sha256(payload.get("rollover_key"))
        or not _lower_sha256(payload.get("plan_sha256"))
        or payload.get("boundary") != _BOUNDARY
        or not isinstance(payload.get("state"), dict)
    ):
        raise ValueError("rollover attempt payload is invalid")
    try:
        _parse_timestamp(payload.get("recorded_at"))
    except ValueError as exc:
        raise ValueError("rollover attempt timestamp is invalid") from exc
    if phase == "prepared":
        if (
            payload.get("prepared_payload_sha256") is not None
            or not isinstance(payload.get("plan"), dict)
            or not isinstance(payload.get("backup"), dict)
            or canonical_payload_sha256(payload["plan"]) != payload["plan_sha256"]
        ):
            raise ValueError("rollover prepared phase is incomplete")
        _validate_backup(payload["backup"])
        _validate_prepared_state(payload["state"])
    elif (
        not _lower_sha256(payload.get("prepared_payload_sha256"))
        or payload.get("plan") is not None
        or payload.get("backup") is not None
    ):
        raise ValueError("rollover follow-up phase is invalid")
    else:
        _validate_followup_state(str(phase), payload["state"])


def _validate_backup(value: dict[str, Any]) -> None:
    if set(value) != {"path", "manifest_sha256", "files", "bytes"} or (
        not isinstance(value["path"], str)
        or not value["path"]
        or Path(value["path"]).is_absolute()
        or ".." in Path(value["path"]).parts
        or not _lower_sha256(value["manifest_sha256"])
        or not isinstance(value["files"], int)
        or isinstance(value["files"], bool)
        or value["files"] < 1
        or not isinstance(value["bytes"], int)
        or isinstance(value["bytes"], bool)
        or value["bytes"] < 0
    ):
        raise ValueError("rollover backup evidence is invalid")


def _validate_prepared_state(value: dict[str, Any]) -> None:
    required = {
        "paths",
        "output_file_sha256",
        "successor_proposal_payload_sha256",
        "successor_proposal_id",
        "successor_trial_file_sha256",
        "successor_trial_payload_sha256",
        "successor_trial_id",
        "lineage",
        "account_execution_registry_sha256",
    }
    if set(value) != required:
        raise ValueError("rollover prepared state schema is invalid")
    paths = value["paths"]
    expected_paths = {
        "account",
        "active_trial",
        "predecessor_proposal",
        "proposal_archive",
        "staged_trial",
        "successor_proposal",
        "trial_archive",
    }
    if not isinstance(paths, dict) or set(paths) != expected_paths:
        raise ValueError("rollover prepared path evidence is invalid")
    for path in paths.values():
        if (
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
        ):
            raise ValueError("rollover prepared path evidence is invalid")
    hashes = value["output_file_sha256"]
    if (
        not isinstance(hashes, dict)
        or set(hashes)
        != {"trial_archive", "proposal_archive", "successor_proposal", "staged_trial"}
        or any(not _lower_sha256(item) for item in hashes.values())
    ):
        raise ValueError("rollover prepared output evidence is invalid")
    for key in (
        "successor_proposal_payload_sha256",
        "successor_trial_file_sha256",
        "successor_trial_payload_sha256",
        "account_execution_registry_sha256",
    ):
        if not _lower_sha256(value[key]):
            raise ValueError("rollover prepared checksum evidence is invalid")
    for key in ("successor_proposal_id", "successor_trial_id"):
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError("rollover prepared identity evidence is invalid")
    if not isinstance(value["lineage"], dict):
        raise ValueError("rollover prepared lineage evidence is invalid")


def _validate_followup_state(phase: str, value: dict[str, Any]) -> None:
    if phase == "outputs_published":
        required = {
            "output_file_sha256",
            "active_trial_file_sha256",
            "recovered_after_process_interruption",
        }
        if (
            set(value) != required
            or not _lower_sha256(value["active_trial_file_sha256"])
            or not isinstance(value["recovered_after_process_interruption"], bool)
        ):
            raise ValueError("rollover output-publication evidence is invalid")
        _validate_output_hashes(value["output_file_sha256"])
    elif phase == "active_swapped":
        required = {
            "active_trial_file_sha256",
            "active_trial_payload_sha256",
            "recovered_after_process_interruption",
        }
        if set(value) != required or (
            any(
                not _lower_sha256(value[key])
                for key in (
                    "active_trial_file_sha256",
                    "active_trial_payload_sha256",
                )
            )
            or not isinstance(value["recovered_after_process_interruption"], bool)
        ):
            raise ValueError("rollover active-swap evidence is invalid")
    elif phase == "verified":
        required = {
            "authority",
            "active_trial_file_sha256",
            "active_trial_payload_sha256",
            "account_file_sha256",
            "account_execution_registry_sha256",
            "backup_manifest_sha256",
            "paper_fill_recorded",
            "broker_order_sent",
            "recovered_after_process_interruption",
        }
        if set(value) != required or (
            value.get("authority") != "successor"
            or any(
                not _lower_sha256(value.get(key))
                for key in (
                    "active_trial_file_sha256",
                    "active_trial_payload_sha256",
                    "account_file_sha256",
                    "account_execution_registry_sha256",
                    "backup_manifest_sha256",
                )
            )
            or value.get("paper_fill_recorded") is not False
            or value.get("broker_order_sent") is not False
            or not isinstance(value.get("recovered_after_process_interruption"), bool)
        ):
            raise ValueError("rollover verified evidence is invalid")
    elif phase == "recovered_rolled_back":
        required = {
            "authority",
            "active_trial_file_sha256",
            "error_type",
            "outputs_removed",
            "account_file_sha256",
            "account_execution_registry_sha256",
            "backup_manifest_sha256",
        }
        if set(value) != required or (
            value.get("authority") != "predecessor"
            or not _lower_sha256(value.get("active_trial_file_sha256"))
            or not _lower_sha256(value.get("account_file_sha256"))
            or not _lower_sha256(value.get("account_execution_registry_sha256"))
            or not _lower_sha256(value.get("backup_manifest_sha256"))
            or not isinstance(value.get("error_type"), str)
            or not value.get("error_type")
            or value.get("outputs_removed") is not True
        ):
            raise ValueError("rollover rollback evidence is invalid")


def _validate_output_hashes(value: Any) -> None:
    required = {
        "trial_archive",
        "proposal_archive",
        "successor_proposal",
        "staged_trial",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or any(not _lower_sha256(item) for item in value.values())
    ):
        raise ValueError("rollover output checksum evidence is invalid")


def _canonical_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    try:
        normalized = json.loads(_canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not canonical JSON") from exc
    if not isinstance(normalized, dict):  # pragma: no cover
        raise ValueError(f"{label} must be an object")
    return normalized


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _lower_hex(value: Any, *, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _lower_sha256(value: Any) -> bool:
    return _lower_hex(value, length=64)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("rollover attempt time must include a timezone")
    return value.astimezone(UTC)


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp has no timezone")
    return parsed.astimezone(UTC)


def _require_safe_directory(path: Path, *, create: bool) -> None:
    directory = Path(path)
    if directory.is_symlink():
        raise ValueError(f"rollover journal directory cannot be a symlink: {directory}")
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not directory.is_dir():
        raise ValueError(f"rollover journal directory is missing: {directory}")
    metadata = directory.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_nlink < 1:
        raise ValueError(f"rollover journal directory is unsafe: {directory}")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def file_sha256(path: Path) -> str:
    """Return a streaming checksum for journal-owned recovery evidence."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

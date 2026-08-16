"""Content-addressed immutable evidence for external provider responses."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from aios.config import settings
from aios.sec_rejections import canonical_rejection_codes
from aios.storage.store import (
    RAW_SNAPSHOT_REJECTION_EVIDENCE_MIGRATION,
    Store,
    get_store,
)

_SAFE_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_KINDS = {"exact_response", "normalized_provider_export"}
_MAX_ORIGINAL_BYTES = 256 * 1024 * 1024
_MAX_STORED_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class RawSnapshotResult:
    """One immutable fetch observation and its deduplicated payload path."""

    snapshot_id: str
    payload_sha256: str
    relative_path: str
    original_bytes: int
    stored_bytes: int
    parsed_rows_sha256: str | None


@dataclass(frozen=True)
class RawSnapshotVerification:
    """Summary of a complete immutable-payload verification pass."""

    payloads: int
    original_bytes: int
    stored_bytes: int
    replayed_snapshots: int


@dataclass(frozen=True)
class VerifiedRawSnapshot:
    """One checksum-verified immutable snapshot and its exact source bytes."""

    snapshot_id: str
    payload_sha256: str
    payload: bytes
    _metadata_json: str

    @property
    def metadata(self) -> dict[str, Any]:
        """Return a detached JSON-safe copy of the verified metadata."""

        return json.loads(self._metadata_json)


def replay_verified_raw_snapshot(
    snapshot: VerifiedRawSnapshot,
) -> list[dict[str, Any]]:
    """Return replayed rows from an already checksum-verified snapshot."""

    if not isinstance(snapshot, VerifiedRawSnapshot):
        raise TypeError("a verified raw snapshot is required")
    replayed = _require_snapshot_replay_evidence(snapshot.metadata, snapshot.payload)
    if replayed is None:
        raise ValueError("raw snapshot has no reviewed replay parser")
    return [dict(row) for row in replayed]


def capture_raw_snapshot(
    payload: bytes,
    *,
    provider: str,
    dataset: str,
    artifact_kind: str,
    requested_at: datetime,
    received_at: datetime,
    request_fingerprint: str,
    adapter_name: str,
    adapter_version: str,
    parser_version: str,
    http_status: int | None = None,
    content_type: str | None = None,
    parsed_rows: list[dict[str, Any]] | None = None,
    parsed_rows_rejected: int | None = None,
    parsed_rejection_codes: tuple[str, ...] | list[str] | None = None,
    ingest_run_id: str | None = None,
    role: str = "source",
    store: Store | None = None,
    project_root: Path | None = None,
) -> RawSnapshotResult:
    """Persist exact bytes atomically and record one fetch observation.

    Library adapters that cannot expose the provider's original HTTP body must
    pass ``normalized_provider_export``. This function never upgrades that
    weaker artifact into an exact-response claim.
    """
    if not isinstance(payload, bytes):
        raise TypeError("raw snapshot payload must be bytes")
    normalized_provider = _component(provider, "provider")
    normalized_dataset = _component(dataset, "dataset")
    if artifact_kind not in _ARTIFACT_KINDS:
        raise ValueError("unsupported raw snapshot artifact kind")
    if not _SHA256.fullmatch(request_fingerprint):
        raise ValueError("request fingerprint must be a lowercase 64-character secret-free SHA-256")
    if len(payload) > _MAX_ORIGINAL_BYTES:
        raise ValueError("raw snapshot payload exceeds the original-byte limit")
    if received_at < requested_at:
        raise ValueError("raw snapshot receive time cannot precede request time")
    if not adapter_name.strip() or not adapter_version.strip() or not parser_version.strip():
        raise ValueError("adapter and parser versions are required")
    if not role.strip():
        raise ValueError("raw snapshot ingest role is required")
    normalized_parser_version = parser_version.strip()
    encoded_rejection_codes = canonical_rejection_codes(parsed_rejection_codes)
    if parsed_rows is None:
        if parsed_rows_rejected is not None or encoded_rejection_codes is not None:
            raise ValueError("raw snapshot rejection evidence requires parsed row evidence")
    elif (
        normalized_provider == "sec-edgar"
        and normalized_dataset == "companyfacts"
        and normalized_parser_version in {
            "sec-companyfacts-v2",
            "sec-companyfacts-v2-storage-safe-v1",
            "sec-companyfacts-v2-storage-safe-v2",
            "sec-companyfacts-v3",
            "sec-companyfacts-v4",
        }
        and parsed_rows_rejected is None
    ):
        raise ValueError("parsed SEC Company Facts snapshots require rejection evidence")
    if parsed_rows_rejected is not None:
        if (
            isinstance(parsed_rows_rejected, bool)
            or not isinstance(parsed_rows_rejected, int)
            or parsed_rows_rejected < 0
        ):
            raise ValueError("raw snapshot rejected row count cannot be negative")
        if (parsed_rows_rejected == 0) != (encoded_rejection_codes is None):
            raise ValueError("raw snapshot rejection count and codes are inconsistent")

    requested_utc = _as_utc(requested_at)
    received_utc = _as_utc(received_at)
    root = (project_root or settings.project_root).resolve()
    raw_root = _resolve_raw_root(root)
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    db = store or get_store()
    existing_payload = db.raw_payload_record(payload_sha256)
    extension = _extension(content_type)
    captured_date = received_utc.date().isoformat()
    relative = (
        Path(existing_payload["relative_path"])
        if existing_payload
        else (
            Path("data")
            / "raw"
            / normalized_provider
            / normalized_dataset
            / captured_date
            / f"{payload_sha256}.{extension}.gz"
        )
    )
    _validate_payload_relative_path(relative, payload_sha256)
    target = root / relative
    if not target.is_relative_to(raw_root):
        raise ValueError("raw snapshot path escaped the configured raw-data root")
    _reject_symlink_ancestors(root, target)
    compressed = gzip.compress(payload, mtime=0)
    if len(compressed) > _MAX_STORED_BYTES:
        raise ValueError("raw snapshot payload exceeds the stored-byte limit")
    _write_once(
        root,
        relative,
        compressed,
        payload_sha256,
        original_size=len(payload),
    )

    parsed_hash = canonical_parsed_rows_sha256(parsed_rows) if parsed_rows is not None else None
    snapshot_id = f"raw-{uuid4().hex}"
    db.record_raw_snapshot(
        payload={
            "payload_sha256": payload_sha256,
            "relative_path": relative.as_posix(),
            "original_bytes": len(payload),
            "stored_bytes": len(compressed),
            "compression": "gzip",
        },
        snapshot={
            "snapshot_id": snapshot_id,
            "provider": normalized_provider,
            "dataset": normalized_dataset,
            "artifact_kind": artifact_kind,
            # DuckDB TIMESTAMP has no zone. Strip only after normalizing to UTC
            # so the persisted wall time retains the documented UTC basis.
            "requested_at": requested_utc.replace(tzinfo=None),
            "received_at": received_utc.replace(tzinfo=None),
            "http_status": http_status,
            "content_type": content_type,
            "request_fingerprint": request_fingerprint,
            "payload_sha256": payload_sha256,
            "adapter_name": adapter_name.strip(),
            "adapter_version": adapter_version.strip(),
            "parser_version": normalized_parser_version,
            "parsed_row_count": len(parsed_rows) if parsed_rows is not None else None,
            "parsed_rows_sha256": parsed_hash,
            "parsed_rows_rejected": parsed_rows_rejected,
            "parsed_rejection_codes": encoded_rejection_codes,
        },
        ingest_run_id=ingest_run_id,
        role=role.strip(),
    )
    return RawSnapshotResult(
        snapshot_id=snapshot_id,
        payload_sha256=payload_sha256,
        relative_path=relative.as_posix(),
        original_bytes=len(payload),
        stored_bytes=len(compressed),
        parsed_rows_sha256=parsed_hash,
    )


def verify_raw_snapshots(
    *,
    store: Store | None = None,
    project_root: Path | None = None,
) -> RawSnapshotVerification:
    """Recompute every registered payload size and uncompressed SHA-256."""
    root = (project_root or settings.project_root).resolve()
    raw_root = _resolve_raw_root(root)
    db = store or get_store()
    original_total = 0
    stored_total = 0
    records = db.raw_payload_records()
    records_by_hash: dict[str, dict[str, Any]] = {}
    for record in records:
        payload_hash = str(record["payload_sha256"])
        if payload_hash in records_by_hash:
            raise ValueError(f"raw snapshot payload hash is duplicated: {payload_hash}")
        original, stored_bytes = _read_verified_payload(
            root,
            raw_root,
            record,
        )
        records_by_hash[payload_hash] = record
        original_total += len(original)
        stored_total += stored_bytes

    replayed = 0
    snapshot_columns = {
        row["column_name"]
        for row in db.query(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'main' AND table_name = 'raw_snapshots'
            """
        )
    }
    rejection_columns = {
        "parsed_rows_rejected",
        "parsed_rejection_codes",
    } & snapshot_columns
    if rejection_columns != {
        "parsed_rows_rejected",
        "parsed_rejection_codes",
    }:
        raise ValueError("raw snapshot rejection evidence schema is incomplete")
    rejection_projection = "parsed_rows_rejected, parsed_rejection_codes"
    marker = db.query(
        """
        SELECT applied_at,
               applied_at <= CAST(now() AS TIMESTAMP)
                   + INTERVAL '5 seconds' AS is_not_future
        FROM schema_migrations
        WHERE name = ?
        """,
        (RAW_SNAPSHOT_REJECTION_EVIDENCE_MIGRATION,),
    )
    if len(marker) != 1:
        raise ValueError("raw snapshot rejection evidence migration marker is invalid")
    if marker and (
        not isinstance(marker[0]["applied_at"], datetime) or marker[0]["is_not_future"] is not True
    ):
        raise ValueError("raw snapshot rejection evidence migration timestamp is invalid")
    snapshots = db.query(
        f"""
        SELECT snapshot_id, provider, dataset, artifact_kind, payload_sha256,
               parser_version, parsed_row_count, parsed_rows_sha256,
               requested_at, received_at, created_at, {rejection_projection},
               requested_at <= received_at
                   AND received_at <= created_at AS timestamps_monotonic,
               requested_at <= CAST(now() AS TIMESTAMP) + INTERVAL '5 seconds'
                   AND received_at <= CAST(now() AS TIMESTAMP) + INTERVAL '5 seconds'
                   AND created_at <= CAST(now() AS TIMESTAMP)
                       + INTERVAL '5 seconds' AS timestamps_not_future
        FROM raw_snapshots
        ORDER BY payload_sha256, snapshot_id
        """
    )
    cached_payload_hash: str | None = None
    cached_payload: bytes | None = None
    for snapshot in snapshots:
        payload_hash = str(snapshot["payload_sha256"])
        payload_record = records_by_hash.get(payload_hash)
        if payload_record is None:
            raise ValueError(f"snapshot payload is unregistered: {snapshot['snapshot_id']}")
        if cached_payload_hash != payload_hash:
            cached_payload, _stored_bytes = _read_verified_payload(
                root,
                raw_root,
                payload_record,
            )
            cached_payload_hash = payload_hash
        if cached_payload is None:
            raise RuntimeError("raw snapshot payload cache is unavailable")
        payload = cached_payload
        if (
            snapshot["timestamps_monotonic"] is not True
            or snapshot["timestamps_not_future"] is not True
        ):
            raise ValueError(f"snapshot timestamps are invalid: {snapshot['snapshot_id']}")
        if _require_snapshot_replay_evidence(snapshot, payload) is not None:
            replayed += 1

    return RawSnapshotVerification(
        payloads=len(records),
        original_bytes=original_total,
        stored_bytes=stored_total,
        replayed_snapshots=replayed,
    )


def read_verified_raw_snapshot(
    *,
    store: Store,
    expected_run_id: str,
    expected_role: str,
    snapshot_id: str,
    expected_provider: str,
    expected_dataset: str,
    expected_artifact_kind: str,
    expected_parser_version: str,
    expected_request_fingerprint: str,
    expected_adapter_name: str,
    expected_adapter_version: str,
    require_parsed_evidence: bool = True,
    require_timestamps_not_future: bool = True,
    project_root: Path | None = None,
) -> VerifiedRawSnapshot:
    """Securely read and replay-check one exact staged evidence snapshot."""

    normalized_snapshot_id = str(snapshot_id).strip()
    run_id = str(expected_run_id).strip()
    role = str(expected_role).strip()
    provider = _component(expected_provider, "expected provider")
    dataset = _component(expected_dataset, "expected dataset")
    artifact_kind = str(expected_artifact_kind).strip()
    parser_version = str(expected_parser_version).strip()
    request_fingerprint = str(expected_request_fingerprint).strip()
    adapter_name = str(expected_adapter_name).strip()
    adapter_version = str(expected_adapter_version).strip()
    if not normalized_snapshot_id or not run_id or not role or not parser_version:
        raise ValueError(
            "verified raw snapshot requires run, role, snapshot, and parser version"
        )
    if not _SHA256.fullmatch(request_fingerprint):
        raise ValueError("expected request fingerprint must be a lowercase SHA-256")
    if not adapter_name or not adapter_version:
        raise ValueError("expected raw snapshot adapter name and version are required")
    if artifact_kind not in _ARTIFACT_KINDS:
        raise ValueError("unsupported expected raw snapshot artifact kind")

    rows = store.query(
        """
        WITH role_links AS (
            SELECT run_id, snapshot_id, role, linked_at,
                   COUNT(*) OVER () AS role_link_count
            FROM ingest_raw_snapshots
            WHERE run_id = ? AND role = ?
        )
        SELECT linked.run_id, linked.role, linked.linked_at,
               linked.role_link_count,
               snapshot.snapshot_id, snapshot.provider, snapshot.dataset,
               snapshot.artifact_kind, snapshot.requested_at,
               snapshot.received_at, snapshot.http_status,
               snapshot.content_type, snapshot.request_fingerprint,
               snapshot.payload_sha256, snapshot.adapter_name,
               snapshot.adapter_version, snapshot.parser_version,
               snapshot.parsed_row_count, snapshot.parsed_rows_sha256,
               snapshot.parsed_rows_rejected,
               snapshot.parsed_rejection_codes, snapshot.created_at,
               payload.relative_path, payload.original_bytes,
               payload.stored_bytes, payload.compression,
               snapshot.requested_at <= snapshot.received_at
                   AND snapshot.received_at <= snapshot.created_at
                   AND snapshot.created_at <= linked.linked_at
                   AS timestamps_monotonic,
               snapshot.requested_at <= CAST(now() AS TIMESTAMP) + INTERVAL '5 seconds'
                   AND snapshot.received_at <= CAST(now() AS TIMESTAMP) + INTERVAL '5 seconds'
                   AND snapshot.created_at <= CAST(now() AS TIMESTAMP) + INTERVAL '5 seconds'
                   AND linked.linked_at <= CAST(now() AS TIMESTAMP) + INTERVAL '5 seconds'
                   AS timestamps_not_future
        FROM role_links AS linked
        JOIN raw_snapshots AS snapshot USING (snapshot_id)
        JOIN raw_payloads AS payload USING (payload_sha256)
        WHERE snapshot.snapshot_id = ?
        """,
        (run_id, role, normalized_snapshot_id),
    )
    if len(rows) != 1 or int(rows[0]["role_link_count"]) != 1:
        raise ValueError(
            "raw snapshot must resolve to exactly one complete run/role observation: "
            f"{run_id}:{role}:{normalized_snapshot_id}"
        )
    snapshot = rows[0]
    expected = {
        "provider": provider,
        "dataset": dataset,
        "artifact_kind": artifact_kind,
        "parser_version": parser_version,
        "request_fingerprint": request_fingerprint,
        "adapter_name": adapter_name,
        "adapter_version": adapter_version,
    }
    for field, value in expected.items():
        if snapshot[field] != value:
            raise ValueError(
                f"raw snapshot {field} mismatch: expected {value!r}, "
                f"found {snapshot[field]!r}"
            )
    if snapshot["timestamps_monotonic"] is not True or (
        require_timestamps_not_future
        and snapshot["timestamps_not_future"] is not True
    ):
        raise ValueError(f"snapshot timestamps are invalid: {normalized_snapshot_id}")
    if artifact_kind == "exact_response" and not (
        isinstance(snapshot["http_status"], int)
        and 200 <= int(snapshot["http_status"]) <= 299
    ):
        raise ValueError("exact-response snapshot requires a successful HTTP status")
    if not _SHA256.fullmatch(str(snapshot["request_fingerprint"])):
        raise ValueError("raw snapshot request fingerprint is invalid")
    if not str(snapshot["adapter_name"]).strip() or not str(
        snapshot["adapter_version"]
    ).strip():
        raise ValueError("raw snapshot adapter metadata is invalid")

    root = (project_root or settings.project_root).resolve()
    raw_root = _resolve_raw_root(root)
    payload, _stored_bytes = _read_verified_payload(root, raw_root, snapshot)
    replayed_rows = _require_snapshot_replay_evidence(snapshot, payload)
    if require_parsed_evidence and replayed_rows is None:
        raise ValueError("raw snapshot lacks reviewed parsed evidence")
    metadata = {
        key: value
        for key, value in snapshot.items()
        if key
        not in {"timestamps_monotonic", "timestamps_not_future", "role_link_count"}
    }
    metadata_json = json.dumps(
        metadata,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_default,
    )
    return VerifiedRawSnapshot(
        snapshot_id=normalized_snapshot_id,
        payload_sha256=str(snapshot["payload_sha256"]),
        payload=payload,
        _metadata_json=metadata_json,
    )


def promote_legacy_sp500_change_snapshot(
    *,
    store: Store,
    run_id: str,
    role: str,
    snapshot_id: str,
    expected_request_fingerprint: str,
    expected_adapter_name: str,
    expected_adapter_version: str,
    project_root: Path | None = None,
) -> VerifiedRawSnapshot:
    """CAS-promote one legacy S&P change response to reviewed replay evidence."""

    from aios.universe_rollforward import CHANGE_ANNOUNCEMENT_PARSER_VERSION

    linked = store.raw_snapshot_for_run_role(run_id, role)
    if str(linked["snapshot_id"]) != str(snapshot_id).strip():
        raise ValueError("legacy S&P promotion snapshot does not match its run/role")
    common: dict[str, Any] = {
        "store": store,
        "expected_run_id": run_id,
        "expected_role": role,
        "snapshot_id": snapshot_id,
        "expected_provider": "spglobal",
        "expected_dataset": "sp500_change_announcement",
        "expected_artifact_kind": "exact_response",
        "expected_request_fingerprint": expected_request_fingerprint,
        "expected_adapter_name": expected_adapter_name,
        "expected_adapter_version": expected_adapter_version,
        "project_root": project_root,
    }
    current_parser = str(linked["parser_version"])
    if current_parser == CHANGE_ANNOUNCEMENT_PARSER_VERSION:
        return read_verified_raw_snapshot(
            **common,
            expected_parser_version=CHANGE_ANNOUNCEMENT_PARSER_VERSION,
        )
    if current_parser != "1":
        raise ValueError("legacy S&P promotion found an unsupported parser version")
    legacy = read_verified_raw_snapshot(
        **common,
        expected_parser_version="1",
        require_parsed_evidence=False,
    )
    from aios.universe_rollforward import parse_sp500_constituent_changes

    parsed_rows = [row.to_dict() for row in parse_sp500_constituent_changes(legacy.payload)]
    promoted = attach_parsed_rows_evidence(
        store=store,
        ingest_run_id=str(run_id),
        role=str(role),
        capture_parser_version="1",
        parser_version=CHANGE_ANNOUNCEMENT_PARSER_VERSION,
        parsed_rows=parsed_rows,
    )
    if promoted != str(snapshot_id).strip():
        raise RuntimeError("legacy S&P promotion changed the linked snapshot identity")
    return read_verified_raw_snapshot(
        **common,
        expected_parser_version=CHANGE_ANNOUNCEMENT_PARSER_VERSION,
    )


def _read_verified_payload(
    project_root: Path,
    raw_root: Path,
    record: dict[str, Any],
) -> tuple[bytes, int]:
    payload_hash = str(record["payload_sha256"])
    if not _SHA256.fullmatch(payload_hash):
        raise ValueError("raw snapshot payload hash is invalid")
    if record.get("compression") != "gzip":
        raise ValueError(f"raw snapshot compression is unsupported: {payload_hash}")
    relative = Path(str(record["relative_path"]))
    _validate_payload_relative_path(relative, payload_hash)
    target = project_root / relative
    if not target.is_relative_to(raw_root):
        raise ValueError(f"raw snapshot path escaped the raw root: {relative}")
    _reject_symlink_ancestors(project_root, target)
    expected_stored = int(record["stored_bytes"])
    expected_original = int(record["original_bytes"])
    if not 0 <= expected_stored <= _MAX_STORED_BYTES:
        raise ValueError(f"raw snapshot stored size is out of bounds: {relative}")
    if not 0 <= expected_original <= _MAX_ORIGINAL_BYTES:
        raise ValueError(f"raw snapshot original size is out of bounds: {relative}")
    compressed = _read_relative_file_exact(
        project_root,
        relative,
        expected_size=expected_stored,
        label=f"raw snapshot {relative}",
    )
    try:
        original = _decompress_gzip_exact(
            compressed,
            expected_size=expected_original,
        )
    except (OSError, EOFError) as exc:
        raise ValueError(f"raw snapshot compression is invalid: {relative}") from exc
    if hashlib.sha256(original).hexdigest() != payload_hash:
        raise ValueError(f"raw snapshot checksum mismatch: {relative}")
    return original, len(compressed)


def canonical_request_fingerprint(request: dict[str, Any]) -> str:
    """Hash an already redacted, JSON-safe request description."""
    encoded = json.dumps(
        request,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def attach_parsed_rows_evidence(
    *,
    store: Store,
    ingest_run_id: str,
    role: str,
    capture_parser_version: str,
    parser_version: str,
    parsed_rows: list[dict[str, Any]],
    rows_rejected: int = 0,
    rejection_codes: tuple[str, ...] | list[str] | None = None,
) -> str:
    """Bind one successful canonical parse to its exact linked response."""
    return store.attach_raw_snapshot_parse_evidence(
        ingest_run_id=ingest_run_id,
        role=role,
        expected_parser_version=capture_parser_version,
        parser_version=parser_version,
        parsed_row_count=len(parsed_rows),
        parsed_rows_sha256=canonical_parsed_rows_sha256(parsed_rows),
        parsed_rows_rejected=rows_rejected,
        parsed_rejection_codes=rejection_codes,
    )


def _write_once(
    project_root: Path,
    relative: Path,
    compressed: bytes,
    expected_hash: str,
    *,
    original_size: int,
) -> None:
    try:
        parent_descriptor = _open_directory_chain(
            project_root,
            relative.parent,
            create=True,
        )
    except OSError as exc:
        raise ValueError(f"raw snapshot write path is unsafe: {relative}") from exc
    filename = relative.name
    try:
        try:
            descriptor = _open_relative_leaf(parent_descriptor, filename)
        except FileNotFoundError:
            descriptor = None
        if descriptor is not None:
            try:
                existing_compressed = _read_open_file_exact(
                    descriptor,
                    expected_size=len(compressed),
                    label=f"raw snapshot target {relative}",
                )
            finally:
                os.close(descriptor)
            _validate_existing_payload(
                existing_compressed,
                expected_hash=expected_hash,
                original_size=original_size,
                label=str(relative),
            )
            return
        _publish_new_payload(
            parent_descriptor,
            filename,
            compressed,
            expected_hash=expected_hash,
            original_size=original_size,
            label=str(relative),
        )
    except (OSError, EOFError) as exc:
        raise ValueError(f"raw snapshot write is unsafe: {relative}") from exc
    finally:
        os.close(parent_descriptor)


def _publish_new_payload(
    parent_descriptor: int,
    filename: str,
    compressed: bytes,
    *,
    expected_hash: str,
    original_size: int,
    label: str,
) -> None:
    temporary = f".{filename}.{uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | _required_os_flag("O_NOFOLLOW")
        | _required_os_flag("O_CLOEXEC"),
        0o600,
        dir_fd=parent_descriptor,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(compressed)
            handle.flush()
            os.fsync(handle.fileno())
        written = os.fstat(descriptor)
        if (
            not stat.S_ISREG(written.st_mode)
            or written.st_nlink != 1
            or written.st_size != len(compressed)
        ):
            raise ValueError(f"raw snapshot temporary file is unsafe: {label}")
        try:
            os.link(
                temporary,
                filename,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing_descriptor = _open_relative_leaf(parent_descriptor, filename)
            try:
                existing_compressed = _read_open_file_exact(
                    existing_descriptor,
                    expected_size=len(compressed),
                    label=f"raw snapshot target {label}",
                )
            finally:
                os.close(existing_descriptor)
            _validate_existing_payload(
                existing_compressed,
                expected_hash=expected_hash,
                original_size=original_size,
                label=label,
            )
        else:
            os.unlink(temporary, dir_fd=parent_descriptor)
            existing_descriptor = _open_relative_leaf(parent_descriptor, filename)
            try:
                installed = os.fstat(existing_descriptor)
                if (installed.st_dev, installed.st_ino) != (
                    written.st_dev,
                    written.st_ino,
                ):
                    raise ValueError(f"raw snapshot publication inode changed: {label}")
                existing_compressed = _read_open_file_exact(
                    existing_descriptor,
                    expected_size=len(compressed),
                    label=f"raw snapshot target {label}",
                )
            finally:
                os.close(existing_descriptor)
            _validate_existing_payload(
                existing_compressed,
                expected_hash=expected_hash,
                original_size=original_size,
                label=label,
            )
            os.fsync(parent_descriptor)
    finally:
        os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent_descriptor)


def _validate_existing_payload(
    compressed: bytes,
    *,
    expected_hash: str,
    original_size: int,
    label: str,
) -> None:
    try:
        existing = _decompress_gzip_exact(
            compressed,
            expected_size=original_size,
        )
    except (OSError, EOFError, ValueError) as exc:
        raise ValueError(f"existing raw snapshot is corrupt: {label}") from exc
    if hashlib.sha256(existing).hexdigest() != expected_hash:
        raise ValueError(f"existing raw snapshot checksum mismatch: {label}")


def canonical_parsed_rows_sha256(rows: list[dict[str, Any]]) -> str:
    """Hash parsed rows using the stable representation stored with snapshots."""
    encoded = json.dumps(
        rows,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_snapshot_replay_evidence(
    snapshot: dict[str, Any],
    payload: bytes,
) -> list[dict[str, Any]] | None:
    """Re-run one reviewed parser and require its stored evidence to match."""

    parsed_rows = _replay_snapshot(snapshot, payload)
    if parsed_rows is None:
        parsed_evidence = (
            snapshot.get("parsed_row_count"),
            snapshot.get("parsed_rows_sha256"),
            snapshot.get("parsed_rows_rejected"),
            snapshot.get("parsed_rejection_codes"),
        )
        if any(value is not None for value in parsed_evidence):
            raise ValueError(
                "snapshot parsed evidence has no reviewed replay parser: "
                f"{snapshot['snapshot_id']}"
            )
        if snapshot["artifact_kind"] == "normalized_provider_export":
            raise ValueError(
                "normalized snapshot has no reviewed replay parser: "
                f"{snapshot['provider']}/{snapshot['dataset']}/"
                f"{snapshot['parser_version']}"
            )
        return None

    expected_count = snapshot.get("parsed_row_count")
    expected_hash = snapshot.get("parsed_rows_sha256")
    if expected_count is None or expected_hash is None:
        raise ValueError(f"snapshot lacks parsed evidence: {snapshot['snapshot_id']}")
    if len(parsed_rows) != int(expected_count):
        raise ValueError(f"snapshot replay row count mismatch: {snapshot['snapshot_id']}")
    if canonical_parsed_rows_sha256(parsed_rows) != expected_hash:
        raise ValueError(f"snapshot replay checksum mismatch: {snapshot['snapshot_id']}")
    replay_rejections = _replay_snapshot_rejections(snapshot, payload)
    if replay_rejections is not None:
        expected_rejected = snapshot.get("parsed_rows_rejected")
        expected_codes = snapshot.get("parsed_rejection_codes")
        if expected_rejected is None:
            if expected_codes is not None:
                raise ValueError(
                    f"snapshot has incomplete rejection evidence: {snapshot['snapshot_id']}"
                )
            raise ValueError(f"snapshot lacks rejection evidence: {snapshot['snapshot_id']}")
        actual_rejected, actual_codes = replay_rejections
        if (int(expected_rejected) == 0) != (expected_codes is None):
            raise ValueError(
                f"snapshot has inconsistent rejection evidence: {snapshot['snapshot_id']}"
            )
        if (
            int(expected_rejected) != actual_rejected
            or expected_codes != canonical_rejection_codes(actual_codes)
        ):
            raise ValueError(
                f"snapshot replay rejection evidence mismatch: {snapshot['snapshot_id']}"
            )
    else:
        expected_rejected = snapshot.get("parsed_rows_rejected")
        expected_codes = snapshot.get("parsed_rejection_codes")
        if not (
            (expected_rejected is None and expected_codes is None)
            or (
                expected_rejected is not None
                and int(expected_rejected) == 0
                and expected_codes is None
            )
        ):
            raise ValueError(
                "snapshot has unsupported parser rejection evidence: "
                f"{snapshot['snapshot_id']}"
            )
    return parsed_rows


def _replay_snapshot(
    snapshot: dict[str, Any],
    payload: bytes,
) -> list[dict[str, Any]] | None:
    key = (
        snapshot["provider"],
        snapshot["dataset"],
        snapshot["parser_version"],
    )
    if key == ("yfinance", "daily-prices", "yfinance-normalized-v1"):
        from aios.ingest.prices import parse_yfinance_normalized_export_v1

        return parse_yfinance_normalized_export_v1(payload)
    if key == ("yfinance", "daily-prices", "yfinance-normalized-v2"):
        from aios.ingest.prices import parse_yfinance_normalized_export_v2

        return parse_yfinance_normalized_export_v2(payload)
    if key == ("yfinance", "daily-prices", "yfinance-normalized-v3"):
        from aios.ingest.prices import parse_yfinance_normalized_export

        return parse_yfinance_normalized_export(payload)
    if key == ("yfinance", "daily-prices", "yfinance-normalized-v4"):
        from aios.ingest.prices import parse_yfinance_normalized_export_v4

        return parse_yfinance_normalized_export_v4(payload)
    if key == ("fred", "series-vintages", "fred-normalized-v1"):
        from aios.ingest.fred import parse_fred_normalized_export

        return parse_fred_normalized_export(payload)
    if key == ("us-treasury", "daily-yield-curve", "treasury-yield-csv-v2"):
        from aios.ingest.fred import parse_treasury_yield_curve_csv

        return parse_treasury_yield_curve_csv(payload)
    if key == ("stooq", "daily-prices", "stooq-daily-csv-v1"):
        from aios.ingest.prices import parse_stooq_daily_csv

        return parse_stooq_daily_csv(payload)
    if key == ("tiingo", "daily-prices", "tiingo-eod-json-v1"):
        from aios.ingest.prices import parse_tiingo_eod_response

        return parse_tiingo_eod_response(payload)
    if key == ("sec-edgar", "company-tickers", "sec-company-tickers-v2"):
        from aios.ingest.edgar import parse_sec_company_tickers_response

        return parse_sec_company_tickers_response(payload)
    if key == ("sec-edgar", "companyfacts", "sec-companyfacts-v2"):
        from aios.ingest.edgar import parse_sec_companyfacts_response_v2

        return parse_sec_companyfacts_response_v2(payload)
    if key == (
        "sec-edgar",
        "companyfacts",
        "sec-companyfacts-v2-storage-safe-v1",
    ):
        from aios.ingest.edgar import parse_sec_companyfacts_response_storage_safe_v1

        return parse_sec_companyfacts_response_storage_safe_v1(payload)
    if key == (
        "sec-edgar",
        "companyfacts",
        "sec-companyfacts-v2-storage-safe-v2",
    ):
        from aios.ingest.edgar import parse_sec_companyfacts_response_storage_safe

        return parse_sec_companyfacts_response_storage_safe(payload)
    if key == ("sec-edgar", "companyfacts", "sec-companyfacts-v3"):
        from aios.ingest.edgar import parse_sec_companyfacts_response_v3

        return parse_sec_companyfacts_response_v3(payload)
    if key == ("sec-edgar", "companyfacts", "sec-companyfacts-v4"):
        from aios.ingest.edgar import parse_sec_companyfacts_response_v4

        return parse_sec_companyfacts_response_v4(payload)
    if key == ("sec-edgar", "submissions", "sec-submissions-v2"):
        from aios.ingest.edgar import parse_sec_submissions_response

        return parse_sec_submissions_response(payload)
    if key == (
        "spglobal",
        "sp500_press_archive",
        "spglobal-press-archive-html-v1",
    ):
        from aios.universe_rollforward import parse_press_archive

        return [row.to_dict() for row in parse_press_archive(payload)]
    if key == (
        "spglobal",
        "sp500_change_announcement",
        "spglobal-constituent-change-html-v1",
    ):
        from aios.universe_rollforward import parse_sp500_constituent_changes

        return [row.to_dict() for row in parse_sp500_constituent_changes(payload)]
    if key == (
        "github",
        "sp500_current_components",
        "sp500-components-csv-v1",
    ):
        from dataclasses import asdict

        from aios.universe_rollforward import parse_component_snapshot

        return [asdict(row) for row in parse_component_snapshot(payload)]
    return None


def _replay_snapshot_rejections(
    snapshot: dict[str, Any],
    payload: bytes,
) -> tuple[int, tuple[str, ...]] | None:
    key = (
        snapshot["provider"],
        snapshot["dataset"],
        snapshot["parser_version"],
    )
    if key not in {
        ("sec-edgar", "companyfacts", "sec-companyfacts-v2"),
        (
            "sec-edgar",
            "companyfacts",
            "sec-companyfacts-v2-storage-safe-v1",
        ),
        (
            "sec-edgar",
            "companyfacts",
            "sec-companyfacts-v2-storage-safe-v2",
        ),
        ("sec-edgar", "companyfacts", "sec-companyfacts-v3"),
        ("sec-edgar", "companyfacts", "sec-companyfacts-v4"),
    }:
        return None
    from aios.ingest.edgar import replay_sec_companyfacts_response

    _rows, metadata = replay_sec_companyfacts_response(
        payload,
        parser_version=str(snapshot["parser_version"]),
    )
    return (
        int(metadata["rows_rejected"]),
        tuple(metadata["rejection_codes"]),
    )


def _json_default(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"unsupported parsed-row value: {type(value).__name__}")


def _component(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not _SAFE_COMPONENT.fullmatch(normalized):
        raise ValueError(f"raw snapshot {label} is not a safe path component")
    return normalized


def _validate_payload_relative_path(relative: Path, payload_sha256: str) -> None:
    """Require the one canonical content-addressed path shape."""

    parts = relative.parts
    if relative.is_absolute() or len(parts) != 6 or parts[:2] != ("data", "raw") or ".." in parts:
        raise ValueError(f"raw snapshot path is unsafe: {relative}")
    _component(parts[2], "stored provider")
    _component(parts[3], "stored dataset")
    try:
        captured_date = datetime.strptime(parts[4], "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"raw snapshot path has an invalid date: {relative}") from exc
    if captured_date != parts[4]:
        raise ValueError(f"raw snapshot path has a noncanonical date: {relative}")
    expected_name = re.compile(rf"{re.escape(payload_sha256)}\.(?:bin|csv|json|xml)\.gz")
    if not expected_name.fullmatch(parts[5]):
        raise ValueError(f"raw snapshot path does not match its content hash: {relative}")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("raw snapshot timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _resolve_raw_root(root: Path) -> Path:
    configured = settings.raw_data_dir
    raw_root = configured if configured.is_absolute() else root / configured
    expected = root / "data" / "raw"
    if raw_root.absolute() != expected.absolute():
        raise ValueError("raw snapshot storage must resolve to project data/raw")
    _reject_symlink_ancestors(root, raw_root)
    return raw_root


def _reject_symlink_ancestors(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"raw snapshot path escaped the project root: {target}") from exc
    current = root
    if current.is_symlink():
        raise ValueError(f"raw snapshot path contains a symlink: {current}")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"raw snapshot path contains a symlink: {current}")


def _required_os_flag(name: str) -> int:
    value = getattr(os, name, None)
    if not isinstance(value, int) or value == 0:
        raise RuntimeError(f"secure raw snapshot storage requires os.{name}")
    return value


def _require_secure_dirfd_support() -> None:
    required = (os.open, os.mkdir, os.unlink, os.link)
    if any(function not in os.supports_dir_fd for function in required):
        raise RuntimeError("secure raw snapshot storage requires dir_fd support")
    if os.link not in os.supports_follow_symlinks:
        raise RuntimeError("secure raw snapshot storage requires no-follow hard-link support")


def _open_directory_chain(
    project_root: Path,
    relative: Path,
    *,
    create: bool,
) -> int:
    _require_secure_dirfd_support()
    directory_flags = (
        os.O_RDONLY
        | _required_os_flag("O_DIRECTORY")
        | _required_os_flag("O_NOFOLLOW")
        | _required_os_flag("O_CLOEXEC")
    )
    descriptor = os.open(project_root, directory_flags)
    try:
        for component in relative.parts:
            if (
                component in {"", ".", ".."}
                or Path(component).name != component
                or not _SAFE_COMPONENT.fullmatch(component)
            ):
                raise ValueError(f"raw snapshot directory component is unsafe: {component!r}")
            if create:
                with suppress(FileExistsError):
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
            child = os.open(component, directory_flags, dir_fd=descriptor)
            child_status = os.fstat(child)
            if not stat.S_ISDIR(child_status.st_mode):
                os.close(child)
                raise ValueError(f"raw snapshot directory component is unsafe: {component!r}")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_relative_leaf(parent_descriptor: int, filename: str) -> int:
    if filename in {"", ".", ".."} or Path(filename).name != filename or "/" in filename:
        raise ValueError(f"raw snapshot filename is unsafe: {filename!r}")
    return os.open(
        filename,
        os.O_RDONLY
        | _required_os_flag("O_NONBLOCK")
        | _required_os_flag("O_NOFOLLOW")
        | _required_os_flag("O_CLOEXEC"),
        dir_fd=parent_descriptor,
    )


def _read_relative_file_exact(
    project_root: Path,
    relative: Path,
    *,
    expected_size: int,
    label: str,
) -> bytes:
    try:
        parent_descriptor = _open_directory_chain(
            project_root,
            relative.parent,
            create=False,
        )
        try:
            descriptor = _open_relative_leaf(parent_descriptor, relative.name)
        finally:
            os.close(parent_descriptor)
    except OSError as exc:
        raise ValueError(f"{label} file is missing or unsafe") from exc
    try:
        return _read_open_file_exact(
            descriptor,
            expected_size=expected_size,
            label=label,
        )
    finally:
        os.close(descriptor)


def _read_open_file_exact(
    descriptor: int,
    *,
    expected_size: int,
    label: str,
) -> bytes:
    if not 0 <= expected_size <= _MAX_STORED_BYTES:
        raise ValueError(f"{label} has an out-of-bounds declared size")
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} is not a regular file")
    if before.st_nlink != 1:
        raise ValueError(f"{label} link count is unsafe")
    if before.st_size != expected_size:
        raise ValueError(f"{label} stored size mismatch")
    with os.fdopen(descriptor, "rb", closefd=False) as handle:
        payload = handle.read(expected_size + 1)
    after = os.fstat(descriptor)
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if len(payload) != expected_size or before_identity != after_identity:
        raise ValueError(f"{label} changed while it was read")
    return payload


def _decompress_gzip_exact(payload: bytes, *, expected_size: int) -> bytes:
    if not 0 <= expected_size <= _MAX_ORIGINAL_BYTES:
        raise ValueError("raw snapshot has an out-of-bounds original size")
    with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as archive:
        original = archive.read(expected_size + 1)
    if len(original) != expected_size:
        raise ValueError("raw snapshot original size mismatch")
    return original


def _extension(content_type: str | None) -> str:
    normalized = (content_type or "").lower()
    if "json" in normalized:
        return "json"
    if "csv" in normalized:
        return "csv"
    if "xml" in normalized:
        return "xml"
    return "bin"

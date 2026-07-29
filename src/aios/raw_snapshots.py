"""Content-addressed immutable evidence for external provider responses."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from aios.config import settings
from aios.storage.store import Store, get_store

_SAFE_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ARTIFACT_KINDS = {"exact_response", "normalized_provider_export"}


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
    if not request_fingerprint or len(request_fingerprint) != 64:
        raise ValueError("request fingerprint must be a 64-character secret-free hash")
    if received_at < requested_at:
        raise ValueError("raw snapshot receive time cannot precede request time")
    if not adapter_name.strip() or not adapter_version.strip() or not parser_version.strip():
        raise ValueError("adapter and parser versions are required")
    if not role.strip():
        raise ValueError("raw snapshot ingest role is required")

    root = (project_root or settings.project_root).resolve()
    raw_root = _resolve_raw_root(root)
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    db = store or get_store()
    existing_payload = db.raw_payload_record(payload_sha256)
    extension = _extension(content_type)
    captured_date = _as_utc(received_at).date().isoformat()
    relative = Path(existing_payload["relative_path"]) if existing_payload else (
        Path("data")
        / "raw"
        / normalized_provider
        / normalized_dataset
        / captured_date
        / f"{payload_sha256}.{extension}.gz"
    )
    target = root / relative
    if not target.is_relative_to(raw_root):
        raise ValueError("raw snapshot path escaped the configured raw-data root")
    compressed = gzip.compress(payload, mtime=0)
    _write_once(target, compressed, payload_sha256)

    parsed_hash = (
        canonical_parsed_rows_sha256(parsed_rows) if parsed_rows is not None else None
    )
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
            "requested_at": _as_utc(requested_at),
            "received_at": _as_utc(received_at),
            "http_status": http_status,
            "content_type": content_type,
            "request_fingerprint": request_fingerprint,
            "payload_sha256": payload_sha256,
            "adapter_name": adapter_name.strip(),
            "adapter_version": adapter_version.strip(),
            "parser_version": parser_version.strip(),
            "parsed_row_count": len(parsed_rows) if parsed_rows is not None else None,
            "parsed_rows_sha256": parsed_hash,
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
    payload_bytes: dict[str, bytes] = {}
    records = db.raw_payload_records()
    for record in records:
        relative = Path(str(record["relative_path"]))
        target = (root / relative).resolve()
        if not target.is_relative_to(raw_root) or target.is_symlink() or not target.is_file():
            raise ValueError(f"raw snapshot file is missing or unsafe: {relative}")
        compressed = target.read_bytes()
        if len(compressed) != int(record["stored_bytes"]):
            raise ValueError(f"raw snapshot stored size mismatch: {relative}")
        try:
            original = gzip.decompress(compressed)
        except (OSError, EOFError) as exc:
            raise ValueError(f"raw snapshot compression is invalid: {relative}") from exc
        if len(original) != int(record["original_bytes"]):
            raise ValueError(f"raw snapshot original size mismatch: {relative}")
        if hashlib.sha256(original).hexdigest() != record["payload_sha256"]:
            raise ValueError(f"raw snapshot checksum mismatch: {relative}")
        payload_bytes[str(record["payload_sha256"])] = original
        original_total += len(original)
        stored_total += len(compressed)

    replayed = 0
    snapshots = db.query(
        """
        SELECT snapshot_id, provider, dataset, artifact_kind, payload_sha256,
               parser_version, parsed_row_count, parsed_rows_sha256
        FROM raw_snapshots
        ORDER BY snapshot_id
        """
    )
    for snapshot in snapshots:
        payload = payload_bytes.get(str(snapshot["payload_sha256"]))
        if payload is None:
            raise ValueError(
                f"snapshot payload is unregistered: {snapshot['snapshot_id']}"
            )
        parsed_rows = _replay_snapshot(snapshot, payload)
        if parsed_rows is None:
            if snapshot["artifact_kind"] == "normalized_provider_export":
                raise ValueError(
                    "normalized snapshot has no reviewed replay parser: "
                    f"{snapshot['provider']}/{snapshot['dataset']}/"
                    f"{snapshot['parser_version']}"
                )
            continue
        expected_count = snapshot.get("parsed_row_count")
        expected_hash = snapshot.get("parsed_rows_sha256")
        if expected_count is None or expected_hash is None:
            raise ValueError(
                f"snapshot lacks parsed evidence: {snapshot['snapshot_id']}"
            )
        if len(parsed_rows) != int(expected_count):
            raise ValueError(
                f"snapshot replay row count mismatch: {snapshot['snapshot_id']}"
            )
        if canonical_parsed_rows_sha256(parsed_rows) != expected_hash:
            raise ValueError(
                f"snapshot replay checksum mismatch: {snapshot['snapshot_id']}"
            )
        replayed += 1

    return RawSnapshotVerification(
        payloads=len(records),
        original_bytes=original_total,
        stored_bytes=stored_total,
        replayed_snapshots=replayed,
    )


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
) -> str:
    """Bind one successful canonical parse to its exact linked response."""
    return store.attach_raw_snapshot_parse_evidence(
        ingest_run_id=ingest_run_id,
        role=role,
        expected_parser_version=capture_parser_version,
        parser_version=parser_version,
        parsed_row_count=len(parsed_rows),
        parsed_rows_sha256=canonical_parsed_rows_sha256(parsed_rows),
    )


def _write_once(target: Path, compressed: bytes, expected_hash: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"raw snapshot target is unsafe: {target}")
        try:
            existing = gzip.decompress(target.read_bytes())
        except (OSError, EOFError) as exc:
            raise ValueError(f"existing raw snapshot is corrupt: {target}") from exc
        if hashlib.sha256(existing).hexdigest() != expected_hash:
            raise ValueError(f"existing raw snapshot checksum mismatch: {target}")
        return
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(compressed)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


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
        from aios.ingest.prices import parse_yfinance_normalized_export

        return parse_yfinance_normalized_export(payload)
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
        from aios.ingest.edgar import parse_sec_companyfacts_response

        return parse_sec_companyfacts_response(payload)
    if key == ("sec-edgar", "submissions", "sec-submissions-v2"):
        from aios.ingest.edgar import parse_sec_submissions_response

        return parse_sec_submissions_response(payload)
    return None


def _json_default(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"unsupported parsed-row value: {type(value).__name__}")


def _component(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not _SAFE_COMPONENT.fullmatch(normalized):
        raise ValueError(f"raw snapshot {label} is not a safe path component")
    return normalized


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("raw snapshot timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _resolve_raw_root(root: Path) -> Path:
    configured = settings.raw_data_dir
    raw_root = configured.resolve() if configured.is_absolute() else (root / configured).resolve()
    expected = (root / "data" / "raw").resolve()
    if raw_root != expected:
        raise ValueError("raw snapshot storage must resolve to project data/raw")
    return raw_root


def _extension(content_type: str | None) -> str:
    normalized = (content_type or "").lower()
    if "json" in normalized:
        return "json"
    if "csv" in normalized:
        return "csv"
    if "xml" in normalized:
        return "xml"
    return "bin"

"""Local operator workflows that hide storage implementation details."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

BACKUP_KIND = "aios.local-backup"
BACKUP_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class BackupResult:
    """One completed, manifest-verified local backup."""

    path: Path
    files: int
    bytes: int
    manifest_sha256: str


@dataclass(frozen=True)
class RestoreResult:
    """One completed restore and its automatic rollback snapshot."""

    source: Path
    safety_backup: Path
    files: int


def create_local_backup(
    project_root: Path,
    database_path: Path,
    *,
    operations_database_path: Path | None = None,
    output: Path | None = None,
    application_version: str,
    now: datetime | None = None,
) -> BackupResult:
    """Copy analytical, paper, and incident evidence into a verified snapshot."""
    root = Path(project_root).resolve()
    database = _resolve_under_root(root, database_path)
    if database.is_symlink() or not database.is_file():
        raise ValueError(f"database does not exist: {database}")
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    destination = (
        Path(output).resolve()
        if output is not None
        else root / "backups" / f"aios-{timestamp.strftime('%Y%m%dT%H%M%SZ')}"
    )
    if destination.exists():
        raise ValueError(f"backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    sources: list[tuple[Path, Path, bool]] = [
        (database, Path("database") / database.name, False)
    ]
    paper_root = root / "data" / "paper"
    if paper_root.exists():
        if paper_root.is_symlink() or not paper_root.is_dir():
            raise ValueError(f"paper backup source must be a regular directory: {paper_root}")
        for source in sorted(paper_root.rglob("*.json")):
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"paper backup source must be a regular file: {source}")
            sources.append((source, Path("paper") / source.relative_to(paper_root), False))
    if operations_database_path is not None:
        operations_database = _resolve_under_root(root, operations_database_path)
        if operations_database.exists():
            if operations_database.is_symlink() or not operations_database.is_file():
                raise ValueError(
                    f"operations backup source must be a regular file: {operations_database}"
                )
            sources.append(
                (
                    operations_database,
                    Path("operations") / operations_database.name,
                    True,
                )
            )
    raw_root = root / "data" / "raw"
    if raw_root.exists():
        if raw_root.is_symlink() or not raw_root.is_dir():
            raise ValueError(f"raw snapshot source must be a regular directory: {raw_root}")
        for source in sorted(raw_root.rglob("*")):
            if source.is_symlink():
                raise ValueError(f"raw snapshot source cannot be a symlink: {source}")
            if source.is_file():
                sources.append((source, Path("raw") / source.relative_to(raw_root), False))

    temporary = Path(
        tempfile.mkdtemp(prefix=".aios-backup-", dir=str(destination.parent))
    )
    try:
        manifest_files: list[dict[str, Any]] = []
        total_bytes = 0
        for source, relative, sqlite_snapshot in sources:
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if sqlite_snapshot:
                _copy_sqlite_snapshot(source, target)
            else:
                shutil.copy2(source, target)
            size = target.stat().st_size
            total_bytes += size
            manifest_files.append(
                {
                    "path": relative.as_posix(),
                    "bytes": size,
                    "sha256": _file_sha256(target),
                }
            )
        manifest = {
            "document_kind": BACKUP_KIND,
            "schema_version": BACKUP_SCHEMA_VERSION,
            "application_version": application_version,
            "created_at": timestamp.isoformat().replace("+00:00", "Z"),
            "database_file": (Path("database") / database.name).as_posix(),
            "files": manifest_files,
            "excludes": [".env", "logs", "backtest artifacts", "mutable provider caches"],
            "restore_policy": {
                "operations": "audit-only; never rolled backward during analytical restore",
                "raw": "immutable merge; newer content-addressed payloads are retained",
            },
        }
        encoded = _canonical_json(manifest)
        manifest_path = temporary / "manifest.json"
        manifest_path.write_bytes(encoded)
        manifest_sha256 = hashlib.sha256(encoded).hexdigest()
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    result = verify_local_backup(destination)
    if result.manifest_sha256 != manifest_sha256:
        raise RuntimeError("backup manifest changed during final verification")
    return result


def verify_local_backup(path: Path) -> BackupResult:
    """Verify backup shape, path safety, sizes, and all content hashes."""
    requested = Path(path)
    if requested.is_symlink():
        raise ValueError("backup directory cannot be a symbolic link")
    root = requested.resolve()
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"backup manifest does not exist: {manifest_path}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"backup manifest is unreadable: {manifest_path}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("document_kind") != BACKUP_KIND
        or manifest.get("schema_version") != BACKUP_SCHEMA_VERSION
        or not isinstance(manifest.get("files"), list)
    ):
        raise ValueError("unsupported backup manifest")

    total_bytes = 0
    seen: set[str] = set()
    database_file = str(manifest.get("database_file") or "")
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise ValueError("backup manifest has an invalid file entry")
        relative = _safe_relative_path(item.get("path"))
        label = relative.as_posix()
        if label in seen:
            raise ValueError(f"backup manifest repeats {label}")
        seen.add(label)
        target = root / relative
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"backup file is missing or unsafe: {label}")
        expected_size = item.get("bytes")
        expected_hash = item.get("sha256")
        if (
            not isinstance(expected_size, int)
            or expected_size < 0
            or not isinstance(expected_hash, str)
            or len(expected_hash) != 64
        ):
            raise ValueError(f"backup metadata is invalid for {label}")
        actual_size = target.stat().st_size
        if actual_size != expected_size or _file_sha256(target) != expected_hash:
            raise ValueError(f"backup checksum mismatch: {label}")
        total_bytes += actual_size
    if not database_file or database_file not in seen:
        raise ValueError("backup manifest does not identify its database file")
    actual_files: set[str] = set()
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            raise ValueError("backup contains a symbolic link")
        if candidate.is_file():
            actual_files.add(candidate.relative_to(root).as_posix())
    expected_files = seen | {"manifest.json"}
    if actual_files != expected_files:
        unexpected = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        detail = unexpected[0] if unexpected else missing[0]
        raise ValueError(f"backup contains an unmanifested or missing file: {detail}")

    encoded = _canonical_json(manifest)
    return BackupResult(
        path=root,
        files=len(seen),
        bytes=total_bytes,
        manifest_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def restore_local_backup(
    backup: Path,
    project_root: Path,
    database_path: Path,
    *,
    operations_database_path: Path | None = None,
    application_version: str,
    confirm: bool = False,
    now: datetime | None = None,
) -> RestoreResult:
    """Restore one verified snapshot after making an automatic safety backup."""
    if not confirm:
        raise ValueError("restore requires explicit confirmation")
    source_result = verify_local_backup(backup)
    source = source_result.path
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    database_relative = _safe_relative_path(manifest["database_file"])
    allowed_files: list[Path] = []
    for item in manifest["files"]:
        relative = _safe_relative_path(item["path"])
        if relative != database_relative and relative.parts[0] not in {
            "paper",
            "operations",
            "raw",
        }:
            raise ValueError(f"backup contains an unsupported restore path: {relative}")
        allowed_files.append(relative)

    root = Path(project_root).resolve()
    database_target = _resolve_under_root(root, database_path)
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    safety_destination = (
        root
        / "backups"
        / f"pre-restore-{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    )
    safety = create_local_backup(
        root,
        database_target,
        operations_database_path=operations_database_path,
        output=safety_destination,
        application_version=application_version,
        now=timestamp,
    )

    data_root = root / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".aios-restore-", dir=str(data_root)))
    old_paper = data_root / f".paper-before-restore-{uuid4().hex}"
    paper_target = data_root / "paper"
    paper_swapped = False
    try:
        staged_database = staging / "database" / database_target.name
        staged_database.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / database_relative, staged_database)

        paper_files = [relative for relative in allowed_files if relative.parts[0] == "paper"]
        staged_paper = staging / "paper"
        for relative in paper_files:
            target = staged_paper / Path(*relative.parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)

        raw_files = [relative for relative in allowed_files if relative.parts[0] == "raw"]
        for relative in raw_files:
            live_target = data_root / relative
            live_target.parent.mkdir(parents=True, exist_ok=True)
            source_file = source / relative
            if live_target.exists():
                if live_target.is_symlink() or not live_target.is_file():
                    raise ValueError(f"live raw snapshot path is unsafe: {live_target}")
                if _file_sha256(live_target) != _file_sha256(source_file):
                    raise ValueError(f"immutable raw snapshot conflicts during restore: {relative}")
            else:
                shutil.copy2(source_file, live_target)

        if paper_target.exists():
            os.replace(paper_target, old_paper)
            paper_swapped = True
        if paper_files:
            os.replace(staged_paper, paper_target)
            paper_swapped = True

        database_target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_database, database_target)
    except Exception:
        if paper_swapped:
            if paper_target.exists():
                failed_paper = data_root / f".paper-failed-restore-{uuid4().hex}"
                os.replace(paper_target, failed_paper)
                shutil.rmtree(failed_paper, ignore_errors=True)
            if old_paper.exists():
                os.replace(old_paper, paper_target)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if old_paper.exists():
        shutil.rmtree(old_paper)

    return RestoreResult(
        source=source,
        safety_backup=safety.path,
        files=source_result.files,
    )


def _resolve_under_root(project_root: Path, path: Path) -> Path:
    return Path(path).resolve() if Path(path).is_absolute() else (project_root / path).resolve()


def _safe_relative_path(value: Any) -> Path:
    candidate = Path(str(value or ""))
    if not candidate.parts or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("backup manifest contains an unsafe path")
    return candidate


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_sqlite_snapshot(source: Path, target: Path) -> None:
    """Use SQLite's online backup API so WAL-backed incident data is consistent."""
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with (
        sqlite3.connect(source_uri, uri=True, timeout=5.0) as source_connection,
        sqlite3.connect(target, timeout=5.0) as target_connection,
    ):
        source_connection.backup(target_connection)


def _canonical_json(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")

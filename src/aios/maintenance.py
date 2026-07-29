"""One cooperative project lock for high-risk local mutation workflows.

The lock is deliberately separate from DuckDB and paper-document locks. It
serializes CLI workflows that must not overlap across those storage domains,
such as backup/restore and forward-paper governance changes.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MAINTENANCE_LOCK_RELATIVE_PATH = Path("data/operations/maintenance.lock")
_ACTIVE_PATHS: set[Path] = set()
_ACTIVE_PATHS_GUARD = threading.Lock()


class MaintenanceLockError(RuntimeError):
    """Base error for an unsafe or unavailable project maintenance lock."""


class MaintenanceLockBusyError(MaintenanceLockError):
    """Another thread or process currently owns the project mutation lease."""


@dataclass(frozen=True)
class MaintenanceLease:
    """Metadata for one held cooperative project mutation lease."""

    path: Path
    operation: str
    acquired_at: str
    pid: int
    thread_id: int


def _validated_root(project_root: Path) -> Path:
    requested = Path(project_root).expanduser()
    if requested.is_symlink():
        raise MaintenanceLockError("project root cannot be a symbolic link")
    root = requested.resolve()
    if root == Path(root.anchor):
        raise MaintenanceLockError("project root cannot be a filesystem root")
    if not root.is_dir():
        raise MaintenanceLockError(f"project root does not exist: {root}")
    return root


def _validated_operation(operation: str) -> str:
    label = operation.strip() if isinstance(operation, str) else ""
    if not label or len(label) > 80:
        raise MaintenanceLockError("maintenance operation must contain 1-80 characters")
    if any(not (character.isalnum() or character in "-_.:") for character in label):
        raise MaintenanceLockError(
            "maintenance operation may contain only letters, numbers, -, _, ., and :"
        )
    return label


def _prepare_lock_parent(root: Path) -> Path:
    data_directory = root / "data"
    parent = data_directory / "operations"
    for candidate in (data_directory, parent):
        if candidate.is_symlink():
            raise MaintenanceLockError(
                f"maintenance lock directory cannot be a symbolic link: {candidate}"
            )
        if candidate.exists() and not candidate.is_dir():
            raise MaintenanceLockError(
                f"maintenance lock parent is not a directory: {candidate}"
            )
    data_directory.mkdir(mode=0o700, exist_ok=True)
    if data_directory.is_symlink() or not data_directory.is_dir():
        raise MaintenanceLockError("maintenance data directory became unsafe")
    parent.mkdir(mode=0o700, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise MaintenanceLockError("maintenance operations directory became unsafe")
    try:
        parent.resolve().relative_to(root)
    except ValueError as exc:
        raise MaintenanceLockError("maintenance lock directory escapes project root") from exc
    return parent


def _open_lock_file(root: Path) -> tuple[int, Path]:
    parent = _prepare_lock_parent(root)
    path = parent / MAINTENANCE_LOCK_RELATIVE_PATH.name
    if path.is_symlink():
        raise MaintenanceLockError("maintenance lock file cannot be a symbolic link")
    if path.exists() and not path.is_file():
        raise MaintenanceLockError("maintenance lock must be one regular file")

    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise MaintenanceLockError(f"maintenance lock file is unsafe: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise MaintenanceLockError(
                "maintenance lock must be one regular, non-hard-linked file"
            )
        current = path.lstat()
        if (
            not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            raise MaintenanceLockError(
                "maintenance lock path changed while it was being opened"
            )
        os.fchmod(descriptor, 0o600)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, path


def _holder_detail(descriptor: int) -> str:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        raw = os.read(descriptor, 4096).decode("utf-8", errors="replace").strip()
        payload = json.loads(raw) if raw else {}
    except (OSError, json.JSONDecodeError):
        return "holder metadata is unavailable"
    operation = str(payload.get("operation") or "unknown")
    pid = str(payload.get("pid") or "unknown")
    acquired_at = str(payload.get("acquired_at") or "unknown")
    return f"operation={operation}, pid={pid}, acquired_at={acquired_at}"


@contextmanager
def project_maintenance_lock(
    project_root: Path,
    *,
    operation: str,
    blocking: bool = False,
) -> Iterator[MaintenanceLease]:
    """Acquire the fixed project mutation lease and release it on every exit."""

    root = _validated_root(project_root)
    label = _validated_operation(operation)
    descriptor, path = _open_lock_file(root)
    registered = False
    try:
        with _ACTIVE_PATHS_GUARD:
            if path in _ACTIVE_PATHS:
                raise MaintenanceLockBusyError(
                    f"project mutation lease is already held in this process: {path}"
                )
            _ACTIVE_PATHS.add(path)
            registered = True

        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(descriptor, flags)
        except BlockingIOError as exc:
            detail = _holder_detail(descriptor)
            raise MaintenanceLockBusyError(
                f"project mutation lease is busy ({detail})"
            ) from exc

        acquired_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        lease = MaintenanceLease(
            path=path,
            operation=label,
            acquired_at=acquired_at,
            pid=os.getpid(),
            thread_id=threading.get_ident(),
        )
        encoded = json.dumps(
            {
                "schema_version": 1,
                "project_root": str(root),
                "operation": lease.operation,
                "acquired_at": lease.acquired_at,
                "pid": lease.pid,
                "thread_id": lease.thread_id,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        written = 0
        while written < len(encoded):
            written += os.write(descriptor, encoded[written:])
        os.fsync(descriptor)
        yield lease
    finally:
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        if registered:
            with _ACTIVE_PATHS_GUARD:
                _ACTIVE_PATHS.discard(path)

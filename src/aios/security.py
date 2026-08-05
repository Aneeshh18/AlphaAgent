"""Small, fail-closed primitives for private local runtime paths."""

from __future__ import annotations

import os
import stat
from pathlib import Path

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class PrivatePathError(RuntimeError):
    """A runtime path cannot be made private without following an alias."""


def _absolute_without_resolving(path: str | Path) -> Path:
    requested = Path(path).expanduser()
    return Path(os.path.abspath(requested))


def _reject_symlink_ancestors(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise PrivatePathError(f"private path cannot contain symbolic links: {path}")


def _verify_open_identity(path: Path, descriptor: int, *, kind: str) -> os.stat_result:
    opened = os.fstat(descriptor)
    current = path.lstat()
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise PrivatePathError(f"private {kind} changed while it was being secured: {path}")
    return opened


def ensure_private_directory(path: str | Path) -> Path:
    """Create or tighten one directory to owner-only access.

    Symbolic-link paths are rejected rather than followed. Existing directories
    are deliberately tightened, so callers should use this only for AIOS-owned
    runtime directories.
    """

    destination = _absolute_without_resolving(path)
    if destination == Path(destination.anchor):
        raise PrivatePathError("filesystem root cannot be an AIOS private directory")
    _reject_symlink_ancestors(destination)
    if destination.exists() and not destination.is_dir():
        raise PrivatePathError(f"private directory path is not a directory: {destination}")

    destination.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    _reject_symlink_ancestors(destination)

    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(destination, flags)
    except OSError as exc:
        raise PrivatePathError(f"private directory is unsafe: {destination}: {exc}") from exc
    try:
        opened = _verify_open_identity(destination, descriptor, kind="directory")
        if not stat.S_ISDIR(opened.st_mode):
            raise PrivatePathError(f"private directory path is not a directory: {destination}")
        os.fchmod(descriptor, PRIVATE_DIRECTORY_MODE)
    finally:
        os.close(descriptor)
    return destination


def ensure_private_file(path: str | Path) -> Path:
    """Tighten one existing, singly linked regular file to owner-only access."""

    destination = _absolute_without_resolving(path)
    _reject_symlink_ancestors(destination)
    if not destination.exists():
        raise PrivatePathError(f"private file does not exist: {destination}")

    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(destination, flags)
    except OSError as exc:
        raise PrivatePathError(f"private file is unsafe: {destination}: {exc}") from exc
    try:
        opened = _verify_open_identity(destination, descriptor, kind="file")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise PrivatePathError(
                f"private file must be one regular, non-hard-linked file: {destination}"
            )
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
    finally:
        os.close(descriptor)
    return destination

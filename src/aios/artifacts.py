"""Safe publication primitives for caller-visible generated artifacts."""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from uuid import uuid4


def publish_bytes_write_once(
    path: str | Path,
    payload: bytes,
    *,
    mode: int = 0o600,
) -> Path:
    """Atomically publish one file and refuse every overwrite or symlink target."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_ancestors(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"artifact already exists: {destination}")

    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            mode,
        )
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:  # pragma: no cover - defensive OS contract guard
                raise OSError("artifact write made no progress")
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None

        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            raise FileExistsError(f"artifact already exists: {destination}") from None

        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(destination.parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()
    return destination


def publish_text_write_once(
    path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int = 0o600,
) -> Path:
    """Encode and atomically publish one immutable text artifact."""

    return publish_bytes_write_once(path, text.encode(encoding), mode=mode)


def _reject_symlink_ancestors(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ValueError(f"artifact path cannot contain symlinks: {path}")

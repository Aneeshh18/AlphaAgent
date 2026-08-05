#!/usr/bin/env python3
"""Fail closed on secret-shaped paths without reading file payloads.

The verifier deliberately inspects only Git-index names/modes, filesystem
names/types, and archive member names/types. It is a release boundary, not a
content scanner and not proof that credentials were never committed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

POLICY_VERSION = "repository-secret-hygiene.v1"

_SAFE_ENV_FILENAMES = frozenset(
    {".env.example", ".env.sample", ".env.template", ".env.dist"}
)
_PRIVATE_KEY_SUFFIXES = frozenset(
    {".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk"}
)
_PRIVATE_KEY_FILENAMES = frozenset(
    {
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "private.pem",
        "private-key.pem",
        "private_key.pem",
    }
)
_CREDENTIAL_DIRECTORY_NAMES = frozenset(
    {
        ".aws",
        ".azure",
        ".gnupg",
        ".kube",
        ".password-store",
        ".ssh",
        ".terraform.d",
    }
)
_CREDENTIAL_FILENAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        "_netrc",
        "application_default_credentials.json",
        "credentials",
        "credentials.json",
        "credentials.tfrc.json",
        "kubeconfig",
        "pip.conf",
        "service-account.json",
        "service_account.json",
        "token.json",
    }
)
_RUNTIME_DIRECTORY_NAMES = frozenset(
    {"backups", "data", "logs", "runtime", "snapshots"}
)
_RUNTIME_SUFFIXES = (
    ".backup",
    ".bak",
    ".db",
    ".db-shm",
    ".db-wal",
    ".duckdb",
    ".duckdb.wal",
    ".dump",
    ".log",
    ".sqlite",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".wal",
)
_PUBLIC_FIXTURE_PREFIX = ("tests", "fixtures", "public")
_TAR_SUFFIXES = (".tar", ".tar.bz2", ".tar.gz", ".tar.xz", ".tbz2", ".tgz", ".txz")


class HygieneVerificationError(RuntimeError):
    """Raised when a metadata target cannot be inspected completely."""


@dataclass(frozen=True, order=True)
class Violation:
    """One deterministic, path-only policy violation."""

    source: str
    path: str
    code: str


@dataclass(frozen=True)
class VerificationReport:
    """Summary of all inspected metadata."""

    target_count: int
    path_count: int
    violations: tuple[Violation, ...]


def classify_path(path: str) -> str | None:
    """Return the first fail-closed policy code for a relative POSIX path."""

    unsafe = _unsafe_path_reason(path)
    if unsafe is not None:
        return unsafe

    canonical = path.rstrip("/")
    parts = tuple(part.casefold() for part in PurePosixPath(canonical).parts)
    basename = parts[-1]

    if ".zcode" in parts:
        return "local_ai_tooling"

    if basename == ".env" or (
        basename.startswith(".env.") and basename not in _SAFE_ENV_FILENAMES
    ):
        return "environment_secret_file"

    if _is_private_key_name(basename):
        return "private_key_material"

    if _is_local_credential_store(parts, basename):
        return "local_credential_store"

    if parts[: len(_PUBLIC_FIXTURE_PREFIX)] == _PUBLIC_FIXTURE_PREFIX:
        return None

    if any(part in _RUNTIME_DIRECTORY_NAMES for part in parts):
        return "sensitive_runtime_path"
    if basename.endswith(_RUNTIME_SUFFIXES):
        return "sensitive_runtime_path"
    return None


def verify_git_index(project_root: Path | str) -> VerificationReport:
    """Inspect cached Git path/mode metadata; never open an indexed payload."""

    root = _validated_target(
        project_root,
        expected="directory",
        reject_hardlinks=False,
        label="Git repository root",
    )
    command = ["git", "ls-files", "--cached", "--stage", "-z"]
    environment = _subprocess_environment()
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HygieneVerificationError(
            f"Git index metadata inspection could not run: {type(exc).__name__}"
        ) from exc
    if result.returncode != 0:
        raise HygieneVerificationError(
            "Git index metadata inspection failed with "
            f"exit code {result.returncode}"
        )

    violations: list[Violation] = []
    path_count = 0
    indexed_paths: list[str] = []
    for raw_entry in result.stdout.split(b"\0"):
        if not raw_entry:
            continue
        metadata, separator, raw_path = raw_entry.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise HygieneVerificationError("Git returned malformed index metadata")
        mode = os.fsdecode(fields[0])
        stage = os.fsdecode(fields[2])
        path = os.fsdecode(raw_path)
        path_count += 1
        indexed_paths.append(path)
        code = classify_path(path)
        if code is not None:
            violations.append(Violation("git-index", path, code))
        if mode == "120000":
            violations.append(Violation("git-index", path, "uninspected_symlink"))
        elif mode not in {"100644", "100755"}:
            violations.append(Violation("git-index", path, "unsupported_git_mode"))
        if stage != "0":
            violations.append(Violation("git-index", path, "unmerged_git_stage"))

    for path, count in sorted(Counter(indexed_paths).items()):
        if count > 1:
            violations.append(Violation("git-index", path, "duplicate_git_index_path"))

    return _report(1, path_count, violations)


def verify_tree(tree_root: Path | str) -> VerificationReport:
    """Inspect a publication-directory tree without opening file payloads."""

    root = _validated_target(
        tree_root,
        expected="directory",
        reject_hardlinks=False,
        label="publication tree root",
    )

    path_count = 0
    violations: list[Violation] = []
    pending: list[tuple[Path, str]] = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise HygieneVerificationError(
                f"publication tree metadata scan failed: {type(exc).__name__}"
            ) from exc
        for entry in entries:
            relative = f"{prefix}/{entry.name}".lstrip("/")
            path_count += 1
            code = classify_path(relative)
            if code is not None:
                violations.append(Violation(f"tree:{root.name}", relative, code))
            try:
                entry_mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise HygieneVerificationError(
                    f"publication tree entry metadata is unavailable: {type(exc).__name__}"
                ) from exc
            if stat.S_ISLNK(entry_mode):
                violations.append(
                    Violation(f"tree:{root.name}", relative, "uninspected_symlink")
                )
            elif stat.S_ISDIR(entry_mode):
                pending.append((Path(entry.path), relative))
            elif stat.S_ISREG(entry_mode):
                if entry.stat(follow_symlinks=False).st_nlink != 1:
                    violations.append(
                        Violation(
                            f"tree:{root.name}",
                            relative,
                            "hardlinked_publication_file",
                        )
                    )
            else:
                violations.append(
                    Violation(f"tree:{root.name}", relative, "unsupported_file_type")
                )

    return _report(1, path_count, violations)


def verify_archive(archive_path: Path | str) -> VerificationReport:
    """Inspect ZIP/wheel or TAR/sdist member metadata without reading payloads."""

    archive = _validated_target(
        archive_path,
        expected="file",
        reject_hardlinks=True,
        label="publication artifact",
    )

    name = archive.name.casefold()
    if name.endswith((".whl", ".zip")):
        return _verify_zip(archive)
    if name.endswith(_TAR_SUFFIXES):
        return _verify_tar(archive)
    raise HygieneVerificationError(
        "unsupported publication artifact type; expected wheel, ZIP, or TAR"
    )


def combine_reports(*reports: VerificationReport) -> VerificationReport:
    """Combine independent target reports in deterministic order."""

    return _report(
        sum(report.target_count for report in reports),
        sum(report.path_count for report in reports),
        [
            violation
            for report in reports
            for violation in report.violations
        ],
    )


def _unsafe_path_reason(path: str) -> str | None:
    if not path or any(ord(character) < 32 or ord(character) == 127 for character in path):
        return "unsafe_path"
    if "\\" in path:
        return "unsafe_path"
    pure = PurePosixPath(path)
    if pure.is_absolute() or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return "unsafe_path"
    raw_parts = path.rstrip("/").split("/")
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        return "unsafe_path"
    return None


def _validated_target(
    raw_path: Path | str,
    *,
    expected: str,
    reject_hardlinks: bool,
    label: str,
) -> Path:
    expanded = Path(raw_path).expanduser()
    absolute = Path(os.path.abspath(os.fspath(expanded)))
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise HygieneVerificationError(
            f"{label} metadata is unavailable: {type(exc).__name__}"
        ) from exc
    if resolved != absolute:
        raise HygieneVerificationError(f"{label} must not use a symbolic-link alias")

    for candidate in (absolute, *absolute.parents):
        try:
            mode = candidate.lstat().st_mode
        except OSError as exc:
            raise HygieneVerificationError(
                f"{label} ancestor metadata is unavailable: {type(exc).__name__}"
            ) from exc
        if stat.S_ISLNK(mode):
            raise HygieneVerificationError(
                f"{label} and its ancestors must not be symbolic links"
            )

    metadata = absolute.lstat()
    if expected == "directory" and not stat.S_ISDIR(metadata.st_mode):
        raise HygieneVerificationError(f"{label} must be a real directory")
    if expected == "file" and not stat.S_ISREG(metadata.st_mode):
        raise HygieneVerificationError(f"{label} must be a regular file")
    if reject_hardlinks and metadata.st_nlink != 1:
        raise HygieneVerificationError(f"{label} must not be hard linked")
    return absolute


def _is_private_key_name(basename: str) -> bool:
    if basename in _PRIVATE_KEY_FILENAMES or basename.endswith(
        tuple(_PRIVATE_KEY_SUFFIXES)
    ):
        return True
    if not basename.endswith(".pem"):
        return False
    stem = basename[:-4]
    return bool(
        re.search(r"(?:^|[-_.])private[-_.]?key(?:$|[-_.])", stem)
        or re.search(r"(?:^|[-_.])key(?:$|[-_.])", stem)
    )


def _is_local_credential_store(parts: tuple[str, ...], basename: str) -> bool:
    if any(part in _CREDENTIAL_DIRECTORY_NAMES for part in parts):
        return True
    if any(
        parts[index : index + 2] in {(".config", "gcloud"), (".config", "gh")}
        for index in range(len(parts) - 1)
    ):
        return True
    if ".docker" in parts and basename == "config.json":
        return True
    if ".streamlit" in parts and re.fullmatch(
        r"secrets(?:\.[a-z0-9_.-]+)?\.toml",
        basename,
    ):
        return True
    if basename in _CREDENTIAL_FILENAMES:
        return True
    return bool(
        re.fullmatch(r"service[-_]account(?:[-_][a-z0-9.-]+)?\.json", basename)
    )


def _verify_zip(archive: Path) -> VerificationReport:
    violations: list[Violation] = []
    try:
        with zipfile.ZipFile(archive) as handle:
            infos = handle.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise HygieneVerificationError(
            f"ZIP metadata inspection failed: {type(exc).__name__}"
        ) from exc

    names = [info.filename for info in infos]
    for duplicate, count in sorted(Counter(names).items()):
        if count > 1:
            violations.append(
                Violation(f"archive:{archive.name}", duplicate, "duplicate_archive_member")
            )
    for info in infos:
        path = info.filename.rstrip("/")
        if not path:
            violations.append(
                Violation(f"archive:{archive.name}", info.filename, "unsafe_path")
            )
            continue
        code = classify_path(path)
        if code is not None:
            violations.append(Violation(f"archive:{archive.name}", path, code))
        file_type = (info.external_attr >> 16) & 0o170000
        if file_type == stat.S_IFLNK:
            violations.append(
                Violation(f"archive:{archive.name}", path, "uninspected_symlink")
            )
        elif file_type not in {0, stat.S_IFDIR, stat.S_IFREG}:
            violations.append(
                Violation(f"archive:{archive.name}", path, "unsupported_file_type")
            )
    return _report(1, len(infos), violations)


def _verify_tar(archive: Path) -> VerificationReport:
    violations: list[Violation] = []
    try:
        with tarfile.open(archive, mode="r:*") as handle:
            members = handle.getmembers()
    except (OSError, tarfile.TarError) as exc:
        raise HygieneVerificationError(
            f"TAR metadata inspection failed: {type(exc).__name__}"
        ) from exc

    names = [member.name for member in members]
    for duplicate, count in sorted(Counter(names).items()):
        if count > 1:
            violations.append(
                Violation(f"archive:{archive.name}", duplicate, "duplicate_archive_member")
            )
    for member in members:
        path = member.name.rstrip("/")
        code = classify_path(path)
        if code is not None:
            violations.append(Violation(f"archive:{archive.name}", path, code))
        if member.issym() or member.islnk():
            violations.append(
                Violation(f"archive:{archive.name}", path, "uninspected_link")
            )
        elif not (member.isfile() or member.isdir()):
            violations.append(
                Violation(f"archive:{archive.name}", path, "unsupported_file_type")
            )
    return _report(1, len(members), violations)


def _subprocess_environment() -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name
        in {
            "COMSPEC",
            "LANG",
            "LC_ALL",
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TMPDIR",
            "WINDIR",
        }
    }
    environment["LC_ALL"] = "C"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    return environment


def _report(
    target_count: int,
    path_count: int,
    violations: list[Violation],
) -> VerificationReport:
    return VerificationReport(
        target_count=target_count,
        path_count=path_count,
        violations=tuple(sorted(set(violations))),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect Git and publication metadata for secret-shaped paths "
            "without reading payloads."
        )
    )
    parser.add_argument(
        "--git-index",
        action="store_true",
        help="inspect the cached Git index (default when no other target is supplied)",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="repository root for --git-index",
    )
    parser.add_argument(
        "--tree",
        type=Path,
        action="append",
        default=[],
        help="publication-directory tree to inspect; repeatable",
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        action="append",
        default=[],
        help="wheel, ZIP, or TAR publication artifact to inspect; repeatable",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic JSON metadata",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="emit output only when verification fails",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    inspect_git = arguments.git_index or (
        not arguments.tree and not arguments.artifact
    )

    try:
        reports: list[VerificationReport] = []
        if inspect_git:
            reports.append(verify_git_index(arguments.project_root))
        reports.extend(verify_tree(path) for path in arguments.tree)
        reports.extend(verify_archive(path) for path in arguments.artifact)
        report = combine_reports(*reports)
    except HygieneVerificationError as exc:
        print(
            f"repository secret hygiene could not be verified: {exc}",
            file=sys.stderr,
        )
        return 2

    payload = {
        "path_count": report.path_count,
        "policy": POLICY_VERSION,
        "target_count": report.target_count,
        "violations": [
            {
                "code": violation.code,
                "path": violation.path,
                "source": violation.source,
            }
            for violation in report.violations
        ],
    }
    if arguments.json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    elif report.violations:
        print(
            "repository secret hygiene failed: "
            f"policy={POLICY_VERSION} violations={len(report.violations)}",
            file=sys.stderr,
        )
        for violation in report.violations:
            safe_path = json.dumps(violation.path, ensure_ascii=True)
            print(
                f"- {violation.code}: {violation.source}:{safe_path}",
                file=sys.stderr,
            )
    elif not arguments.quiet:
        print(
            "repository secret hygiene verified: "
            f"policy={POLICY_VERSION} targets={report.target_count} "
            f"paths={report.path_count}"
        )
    return 1 if report.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())

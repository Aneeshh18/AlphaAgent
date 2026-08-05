#!/usr/bin/env python3
"""Verify that an AIOS release wheel exactly matches its reviewed source tree."""

from __future__ import annotations

import argparse
import ast
import base64
import configparser
import csv
import hashlib
import importlib.util
import io
import os
import stat
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
import venv
import zipfile
from collections import Counter
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name

ASSET_SUFFIXES = frozenset({".py", ".css", ".json"})
CACHE_DIRECTORIES = frozenset({"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"})
EXPECTED_CONSOLE_SCRIPT = "aios.cli:app"


class WheelVerificationError(RuntimeError):
    """Raised when a release wheel does not satisfy the source contract."""


@dataclass(frozen=True)
class VerificationReport:
    """Summary of a successful wheel verification."""

    distribution_name: str
    version: str
    asset_count: int
    install_smoke_ran: bool


@dataclass(frozen=True)
class SourceAssetSnapshot:
    """Immutable bytes and filesystem identity for one reviewed source asset."""

    path: Path
    payload: bytes
    stat_signature: tuple[int, int, int, int]


@dataclass(frozen=True)
class ProjectContract:
    """Release metadata that the wheel must reproduce from pyproject.toml."""

    name: str
    version: str
    requires_python: str
    dependencies: tuple[Requirement, ...]
    optional_extras: tuple[str, ...]


def verify_release_wheel(
    wheel_path: Path | str,
    project_root: Path | str,
    *,
    install_smoke: bool = True,
) -> VerificationReport:
    """Verify wheel contents, metadata, entry point, and optional installation."""

    wheel_input = Path(wheel_path).expanduser()
    wheel = wheel_input.resolve()
    root = Path(project_root).expanduser().resolve()
    source_root = root / "src" / "aios"
    pyproject_path = root / "pyproject.toml"

    if not wheel.is_file():
        raise WheelVerificationError(f"wheel does not exist: {wheel}")
    if wheel.suffix != ".whl":
        raise WheelVerificationError(f"expected a .whl archive: {wheel}")
    if not source_root.is_dir():
        raise WheelVerificationError(f"package source directory does not exist: {source_root}")
    if not pyproject_path.is_file():
        raise WheelVerificationError(f"pyproject.toml does not exist: {pyproject_path}")

    project = _read_project_contract(pyproject_path)
    distribution_name = project.name
    project_version = project.version
    source_version = _read_source_version(source_root / "__init__.py")
    if source_version != project_version:
        raise WheelVerificationError(
            "source and project versions disagree: "
            f"src/aios/__init__.py={source_version!r}, pyproject.toml={project_version!r}"
        )

    expected_assets = _source_assets(source_root)
    source_snapshot = _snapshot_source_assets(expected_assets)
    try:
        with zipfile.ZipFile(wheel) as archive:
            _verify_safe_unique_members(archive)
            _verify_member_manifest(archive, source_snapshot)
            _verify_asset_bytes(archive, source_root, source_snapshot)
            _verify_wheel_metadata(
                archive,
                project=project,
            )
            _verify_wheel_file_contract(archive)
            _verify_record(archive)
            _verify_console_entry_point(archive)
    except zipfile.BadZipFile as exc:
        raise WheelVerificationError(f"wheel is not a valid ZIP archive: {wheel}") from exc

    _verify_repository_hygiene_metadata(wheel_input)
    if install_smoke:
        _run_install_smoke(wheel)
    _verify_source_unchanged(source_root, source_snapshot)

    return VerificationReport(
        distribution_name=distribution_name,
        version=project_version,
        asset_count=len(source_snapshot),
        install_smoke_ran=install_smoke,
    )


def _verify_repository_hygiene_metadata(wheel_path: Path) -> None:
    script_path = Path(__file__).with_name("verify_repository_hygiene.py")
    if not script_path.is_file():
        raise WheelVerificationError(
            "repository secret-hygiene verifier is missing from scripts/"
        )
    module_name = "_aios_verify_repository_hygiene"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise WheelVerificationError(
            "repository secret-hygiene verifier could not be loaded"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        report = module.verify_archive(wheel_path)
    except Exception as exc:
        verification_error = getattr(module, "HygieneVerificationError", ())
        if verification_error and isinstance(exc, verification_error):
            raise WheelVerificationError(
                f"repository secret-hygiene metadata check failed: {exc}"
            ) from exc
        raise
    finally:
        sys.modules.pop(module_name, None)

    if report.violations:
        detail = ", ".join(
            f"{item.code}:{item.path}" for item in report.violations
        )
        raise WheelVerificationError(
            "wheel violates repository secret-hygiene metadata policy: " + detail
        )


def _read_project_contract(pyproject_path: Path) -> ProjectContract:
    try:
        with pyproject_path.open("rb") as handle:
            project = tomllib.load(handle).get("project", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WheelVerificationError(f"cannot read {pyproject_path}: {exc}") from exc

    name = project.get("name")
    version = project.get("version")
    requires_python = project.get("requires-python")
    if not isinstance(name, str) or not name.strip():
        raise WheelVerificationError("pyproject.toml must declare a nonblank project.name")
    if not isinstance(version, str) or not version.strip():
        raise WheelVerificationError("pyproject.toml must declare a nonblank project.version")
    if not isinstance(requires_python, str) or not requires_python.strip():
        raise WheelVerificationError(
            "pyproject.toml must declare a nonblank project.requires-python"
        )
    try:
        SpecifierSet(requires_python)
    except InvalidSpecifier as exc:
        raise WheelVerificationError(
            f"pyproject.toml project.requires-python is invalid: {exc}"
        ) from exc

    dependencies = _project_requirements(
        project.get("dependencies", []),
        label="project.dependencies",
    )
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise WheelVerificationError("pyproject.toml project.optional-dependencies must be a table")
    optional_requirements: list[Requirement] = []
    extras: list[str] = []
    for extra, values in sorted(optional.items()):
        if not isinstance(extra, str) or not extra.strip():
            raise WheelVerificationError(
                "pyproject.toml optional dependency extra must be nonblank"
            )
        normalized_extra = canonicalize_name(extra.strip())
        if normalized_extra in extras:
            raise WheelVerificationError(
                "pyproject.toml declares optional dependency extras that normalize to the same name"
            )
        extras.append(normalized_extra)
        for requirement in _project_requirements(
            values,
            label=f"project.optional-dependencies.{normalized_extra}",
        ):
            requirement_without_marker = str(requirement).split(";", 1)[0].strip()
            marker = (
                f"({requirement.marker}) and extra == '{normalized_extra}'"
                if requirement.marker is not None
                else f"extra == '{normalized_extra}'"
            )
            optional_requirements.append(Requirement(f"{requirement_without_marker}; {marker}"))

    all_requirements = tuple(dependencies + optional_requirements)
    if len(all_requirements) != len(set(all_requirements)):
        raise WheelVerificationError(
            "pyproject.toml declares duplicate project dependency metadata"
        )
    return ProjectContract(
        name=name.strip(),
        version=version.strip(),
        requires_python=requires_python.strip(),
        dependencies=all_requirements,
        optional_extras=tuple(extras),
    )


def _project_requirements(value: object, *, label: str) -> list[Requirement]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise WheelVerificationError(f"pyproject.toml {label} must be a list of strings")
    requirements: list[Requirement] = []
    for item in value:
        try:
            requirements.append(Requirement(item))
        except InvalidRequirement as exc:
            raise WheelVerificationError(
                f"pyproject.toml {label} contains an invalid requirement: {item!r}"
            ) from exc
    return requirements


def _read_source_version(init_path: Path) -> str:
    try:
        tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise WheelVerificationError(
            f"cannot parse package version from {init_path}: {exc}"
        ) from exc

    versions: list[str] = []
    for node in tree.body:
        value: ast.expr | None = None
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            )
        ) or (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__version__"
        ):
            value = node.value
        if value is not None:
            try:
                parsed = ast.literal_eval(value)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, str) and parsed.strip():
                versions.append(parsed.strip())

    if len(versions) != 1:
        raise WheelVerificationError(
            f"{init_path} must contain exactly one literal string __version__ assignment"
        )
    return versions[0]


def _source_assets(source_root: Path) -> dict[str, Path]:
    assets: dict[str, Path] = {}
    for candidate in source_root.rglob("*"):
        relative = candidate.relative_to(source_root)
        if _contains_cache(relative.parts) or candidate.suffix not in ASSET_SUFFIXES:
            continue
        try:
            mode = candidate.stat(follow_symlinks=False).st_mode
        except OSError as exc:
            raise WheelVerificationError(f"cannot inspect source asset {candidate}: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise WheelVerificationError(
                f"package source asset must not be a symbolic link: {candidate}"
            )
        if not stat.S_ISREG(mode):
            continue
        member = f"aios/{relative.as_posix()}"
        assets[member] = candidate
    if not assets:
        raise WheelVerificationError(f"no package assets found below {source_root}")
    return assets


def _contains_cache(parts: tuple[str, ...]) -> bool:
    return any(part in CACHE_DIRECTORIES for part in parts)


def _verify_safe_unique_members(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    names = [info.filename for info in infos]
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicates:
        raise WheelVerificationError(
            "wheel contains duplicate archive member(s): " + ", ".join(duplicates)
        )
    for info in infos:
        name = info.filename
        path = PurePosixPath(name)
        if "\\" in name or path.is_absolute() or ".." in path.parts:
            raise WheelVerificationError(f"wheel contains an unsafe archive path: {name}")
        file_type = (info.external_attr >> 16) & 0o170000
        if file_type == stat.S_IFLNK:
            raise WheelVerificationError(f"wheel member must not be a symbolic link: {name}")


def _verify_member_manifest(
    archive: zipfile.ZipFile,
    source_snapshot: dict[str, SourceAssetSnapshot],
) -> None:
    actual = {info.filename for info in archive.infolist() if not info.is_dir()}
    dist_info_roots = {
        path.parts[0]
        for name in actual
        if len((path := PurePosixPath(name)).parts) >= 2 and path.parts[0].endswith(".dist-info")
    }
    if len(dist_info_roots) != 1:
        raise WheelVerificationError(
            f"wheel must contain exactly one .dist-info directory; found {len(dist_info_roots)}"
        )
    dist_info_root = next(iter(dist_info_roots))
    expected = set(source_snapshot)
    expected.update(
        f"{dist_info_root}/{filename}"
        for filename in ("METADATA", "WHEEL", "entry_points.txt", "RECORD")
    )
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if not missing and not unexpected:
        return
    details: list[str] = []
    if missing:
        details.append("missing: " + ", ".join(missing))
    if unexpected:
        details.append("unexpected: " + ", ".join(unexpected))
    raise WheelVerificationError(
        "wheel member manifest differs from reviewed source; " + "; ".join(details)
    )


def _stat_signature(path: Path) -> tuple[int, int, int, int]:
    value = path.stat(follow_symlinks=False)
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _snapshot_source_assets(
    assets: dict[str, Path],
) -> dict[str, SourceAssetSnapshot]:
    snapshot: dict[str, SourceAssetSnapshot] = {}
    for member, source_path in sorted(assets.items()):
        try:
            before = _stat_signature(source_path)
            payload = source_path.read_bytes()
            after = _stat_signature(source_path)
        except OSError as exc:
            raise WheelVerificationError(
                f"cannot snapshot package asset {source_path}: {exc}"
            ) from exc
        if before != after:
            raise WheelVerificationError(
                f"package source changed while it was being read: {source_path}"
            )
        snapshot[member] = SourceAssetSnapshot(
            path=source_path,
            payload=payload,
            stat_signature=after,
        )
    return snapshot


def _verify_asset_bytes(
    archive: zipfile.ZipFile,
    source_root: Path,
    source_snapshot: dict[str, SourceAssetSnapshot],
) -> None:
    stale: list[str] = []
    for member, snapshot in sorted(source_snapshot.items()):
        try:
            wheel_bytes = archive.read(member)
        except OSError as exc:
            raise WheelVerificationError(
                f"cannot read package asset {snapshot.path}: {exc}"
            ) from exc
        if snapshot.payload != wheel_bytes:
            stale.append(str(snapshot.path.relative_to(source_root.parent)))
    if stale:
        raise WheelVerificationError("wheel contains stale package asset(s): " + ", ".join(stale))


def _verify_source_unchanged(
    source_root: Path,
    source_snapshot: dict[str, SourceAssetSnapshot],
) -> None:
    current_assets = _source_assets(source_root)
    if set(current_assets) != set(source_snapshot):
        raise WheelVerificationError(
            "package source asset manifest changed during wheel verification"
        )
    for member, snapshot in sorted(source_snapshot.items()):
        current_path = current_assets[member]
        try:
            signature = _stat_signature(current_path)
            payload = current_path.read_bytes()
            after = _stat_signature(current_path)
        except OSError as exc:
            raise WheelVerificationError(
                f"cannot recheck package asset {current_path}: {exc}"
            ) from exc
        if signature != after or after != snapshot.stat_signature or payload != snapshot.payload:
            raise WheelVerificationError(
                f"package source changed during wheel verification: {current_path}"
            )


def _dist_info_member(archive: zipfile.ZipFile, filename: str) -> str:
    matches = [
        info.filename
        for info in archive.infolist()
        if not info.is_dir()
        and len(PurePosixPath(info.filename).parts) == 2
        and PurePosixPath(info.filename).parts[0].endswith(".dist-info")
        and PurePosixPath(info.filename).name == filename
    ]
    if len(matches) != 1:
        raise WheelVerificationError(
            f"wheel must contain exactly one .dist-info/{filename}; found {len(matches)}"
        )
    return matches[0]


def _verify_wheel_metadata(
    archive: zipfile.ZipFile,
    *,
    project: ProjectContract,
) -> None:
    metadata_member = _dist_info_member(archive, "METADATA")
    try:
        metadata = BytesParser().parsebytes(archive.read(metadata_member))
    except (OSError, UnicodeError) as exc:
        raise WheelVerificationError(f"cannot parse wheel METADATA: {exc}") from exc
    if metadata.defects:
        raise WheelVerificationError(f"wheel METADATA has parser defects: {metadata.defects!r}")
    actual_name = _metadata_singleton(metadata, "Name", member="METADATA")
    actual_version = _metadata_singleton(metadata, "Version", member="METADATA")
    if actual_name != project.name:
        raise WheelVerificationError(
            f"wheel METADATA Name is {actual_name!r}; expected {project.name!r}"
        )
    if actual_version != project.version:
        raise WheelVerificationError(
            f"wheel METADATA Version is {actual_version!r}; expected {project.version!r}"
        )
    actual_requires_python = _metadata_singleton(
        metadata,
        "Requires-Python",
        member="METADATA",
    )
    try:
        python_contract_matches = SpecifierSet(actual_requires_python) == SpecifierSet(
            project.requires_python
        )
    except InvalidSpecifier as exc:
        raise WheelVerificationError(
            f"wheel METADATA Requires-Python is invalid: {actual_requires_python!r}"
        ) from exc
    if not python_contract_matches:
        raise WheelVerificationError(
            "wheel METADATA Requires-Python is "
            f"{actual_requires_python!r}; expected {project.requires_python!r}"
        )

    actual_requirements: list[Requirement] = []
    for value in metadata.get_all("Requires-Dist", []):
        try:
            actual_requirements.append(Requirement(value))
        except InvalidRequirement as exc:
            raise WheelVerificationError(
                f"wheel METADATA has invalid Requires-Dist: {value!r}"
            ) from exc
    actual_counter = Counter(actual_requirements)
    expected_counter = Counter(project.dependencies)
    if actual_counter != expected_counter:
        missing = sorted(str(value) for value in (expected_counter - actual_counter).elements())
        unexpected = sorted(str(value) for value in (actual_counter - expected_counter).elements())
        detail: list[str] = []
        if missing:
            detail.append("missing: " + ", ".join(missing))
        if unexpected:
            detail.append("unexpected: " + ", ".join(unexpected))
        raise WheelVerificationError(
            "wheel METADATA Requires-Dist differs from pyproject.toml; " + "; ".join(detail)
        )

    actual_extras = [canonicalize_name(value) for value in metadata.get_all("Provides-Extra", [])]
    if len(actual_extras) != len(set(actual_extras)):
        raise WheelVerificationError("wheel METADATA contains duplicate Provides-Extra fields")
    if set(actual_extras) != set(project.optional_extras):
        raise WheelVerificationError(
            "wheel METADATA Provides-Extra differs from pyproject.toml; "
            f"found {sorted(actual_extras)!r}, "
            f"expected {sorted(project.optional_extras)!r}"
        )


def _metadata_singleton(metadata, field: str, *, member: str) -> str:
    values = metadata.get_all(field, [])
    if len(values) != 1 or not values[0].strip():
        raise WheelVerificationError(
            f"wheel {member} must contain exactly one nonblank {field} field"
        )
    return values[0].strip()


def _verify_wheel_file_contract(archive: zipfile.ZipFile) -> None:
    wheel_member = _dist_info_member(archive, "WHEEL")
    try:
        metadata = BytesParser().parsebytes(archive.read(wheel_member))
    except (OSError, UnicodeError) as exc:
        raise WheelVerificationError(f"cannot parse wheel WHEEL metadata: {exc}") from exc
    if metadata.defects:
        raise WheelVerificationError(
            f"wheel WHEEL metadata has parser defects: {metadata.defects!r}"
        )
    wheel_version = _metadata_singleton(
        metadata,
        "Wheel-Version",
        member="WHEEL",
    )
    generator = _metadata_singleton(metadata, "Generator", member="WHEEL")
    purelib = _metadata_singleton(
        metadata,
        "Root-Is-Purelib",
        member="WHEEL",
    )
    tags = [value.strip() for value in metadata.get_all("Tag", [])]
    if wheel_version != "1.0":
        raise WheelVerificationError(
            f"wheel WHEEL Wheel-Version is {wheel_version!r}; expected '1.0'"
        )
    if not generator:
        raise WheelVerificationError("wheel WHEEL Generator must be nonblank")
    if purelib.lower() != "true":
        raise WheelVerificationError(f"wheel WHEEL Root-Is-Purelib is {purelib!r}; expected 'true'")
    if tags != ["py3-none-any"]:
        raise WheelVerificationError(
            f"wheel WHEEL Tag fields are {tags!r}; expected ['py3-none-any']"
        )


def _verify_record(archive: zipfile.ZipFile) -> None:
    record_member = _dist_info_member(archive, "RECORD")
    try:
        record_text = archive.read(record_member).decode("utf-8")
        rows = list(csv.reader(io.StringIO(record_text, newline="")))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise WheelVerificationError(f"cannot parse wheel RECORD: {exc}") from exc

    records: dict[str, tuple[str, str]] = {}
    for row_number, row in enumerate(rows, start=1):
        if len(row) != 3:
            raise WheelVerificationError(
                f"wheel RECORD row {row_number} must contain exactly 3 columns"
            )
        member, digest, size = row
        if not member:
            raise WheelVerificationError(f"wheel RECORD row {row_number} has a blank member path")
        if member in records:
            raise WheelVerificationError(f"wheel RECORD contains a duplicate member: {member}")
        records[member] = (digest, size)

    members = {info.filename for info in archive.infolist() if not info.is_dir()}
    missing = sorted(members - set(records))
    unexpected = sorted(set(records) - members)
    if missing or unexpected:
        detail: list[str] = []
        if missing:
            detail.append("missing: " + ", ".join(missing))
        if unexpected:
            detail.append("unexpected: " + ", ".join(unexpected))
        raise WheelVerificationError(
            "wheel RECORD coverage differs from archive members; " + "; ".join(detail)
        )

    for member in sorted(members):
        digest, size = records[member]
        if member == record_member:
            if digest or size:
                raise WheelVerificationError(
                    "wheel RECORD self-entry must have empty hash and size"
                )
            continue
        try:
            payload = archive.read(member)
        except OSError as exc:
            raise WheelVerificationError(
                f"cannot read wheel member for RECORD verification: {member}"
            ) from exc
        encoded_digest = (
            base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode("ascii")
        )
        expected_digest = f"sha256={encoded_digest}"
        if digest != expected_digest:
            raise WheelVerificationError(f"wheel RECORD hash mismatch for {member}")
        expected_size = str(len(payload))
        if size != expected_size:
            raise WheelVerificationError(
                f"wheel RECORD size mismatch for {member}: "
                f"found {size!r}, expected {expected_size!r}"
            )


def _verify_console_entry_point(archive: zipfile.ZipFile) -> None:
    entry_points_member = _dist_info_member(archive, "entry_points.txt")
    parser = configparser.RawConfigParser(strict=True)
    parser.optionxform = str
    try:
        parser.read_string(archive.read(entry_points_member).decode("utf-8"))
    except (configparser.Error, UnicodeError) as exc:
        raise WheelVerificationError(f"cannot parse wheel entry_points.txt: {exc}") from exc
    if not parser.has_section("console_scripts") or not parser.has_option(
        "console_scripts", "aios"
    ):
        raise WheelVerificationError("wheel is missing the 'aios' console entry point")
    actual = parser.get("console_scripts", "aios").strip()
    if actual != EXPECTED_CONSOLE_SCRIPT:
        raise WheelVerificationError(
            f"wheel console entry point is {actual!r}; expected {EXPECTED_CONSOLE_SCRIPT!r}"
        )


def _run_install_smoke(wheel: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="aios-wheel-smoke-") as temporary:
        smoke_root = Path(temporary)
        environment_root = smoke_root / "venv"
        project_root = smoke_root / "project"
        project_root.mkdir()
        try:
            venv.EnvBuilder(with_pip=True, system_site_packages=True).create(environment_root)
        except Exception as exc:
            raise WheelVerificationError(
                f"cannot create smoke-test virtual environment: {exc}"
            ) from exc

        python = _venv_executable(environment_root, "python")
        executable = _venv_executable(environment_root, "aios")
        _run_checked(
            [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
            cwd=smoke_root,
            stage="wheel installation",
        )

        site_packages = _capture_checked(
            [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
            cwd=smoke_root,
            stage="smoke environment inspection",
        ).strip()
        smoke_code = "\n".join(
            [
                "import json",
                "from importlib.resources import files",
                "from pathlib import Path",
                "import aios",
                "import aios.anomalies",
                "site = Path(__import__('sys').argv[1]).resolve()",
                "origin = Path(aios.__file__).resolve()",
                "assert origin.is_relative_to(site), (origin, site)",
                "css = files('aios').joinpath('dashboard.css').read_text(encoding='utf-8')",
                "assert css.strip()",
                "scenario = files('aios').joinpath(",
                "    'risk/scenarios/us_equity_reference_v1.json'",
                ").read_text(encoding='utf-8')",
                "assert isinstance(json.loads(scenario), dict)",
                "hosted_schema = files('aios').joinpath(",
                "    'schemas/hosted-research-snapshot.v1.json'",
                ").read_text(encoding='utf-8')",
                "parsed_schema = json.loads(hosted_schema)",
                "assert parsed_schema['$id'] == 'urn:aios:schema:hosted-research-snapshot:v1'",
            ]
        )
        smoke_env = _smoke_environment(project_root)
        _run_checked(
            [str(python), "-c", smoke_code, site_packages],
            cwd=smoke_root,
            env=smoke_env,
            stage="installed package and resource smoke test",
        )
        _run_checked(
            [str(executable), "--help"],
            cwd=smoke_root,
            env=smoke_env,
            stage="installed aios --help smoke test",
        )


def _smoke_environment(project_root: Path) -> dict[str, str]:
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
            "PYTHONUTF8",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TMPDIR",
            "WINDIR",
        }
    }
    environment["AIOS_PROJECT_ROOT"] = str(project_root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"

    caller_site = Path(sysconfig.get_path("purelib")).resolve()
    base_site = Path(
        sysconfig.get_path(
            "purelib",
            vars={
                "base": sys.base_prefix,
                "platbase": sys.base_prefix,
            },
        )
    ).resolve()
    if caller_site != base_site:
        environment["PYTHONPATH"] = str(caller_site)
    return environment


def _venv_executable(environment_root: Path, name: str) -> Path:
    if os.name == "nt":
        suffix = ".exe" if name in {"python", "aios"} else ""
        return environment_root / "Scripts" / f"{name}{suffix}"
    return environment_root / "bin" / name


def _run_checked(
    command: list[str],
    *,
    cwd: Path,
    stage: str,
    env: dict[str, str] | None = None,
) -> None:
    _capture_checked(command, cwd=cwd, stage=stage, env=env)


def _capture_checked(
    command: list[str],
    *,
    cwd: Path,
    stage: str,
    env: dict[str, str] | None = None,
) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WheelVerificationError(f"{stage} could not run: {exc}") from exc
    if result.returncode != 0:
        output = "\n".join(
            section.strip() for section in (result.stdout, result.stderr) if section.strip()
        )
        if len(output) > 4_000:
            output = output[-4_000:]
        raise WheelVerificationError(
            f"{stage} failed with exit code {result.returncode}"
            + (f":\n{output}" if output else "")
        )
    return result.stdout


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify that an AIOS wheel exactly matches the project source."
    )
    parser.add_argument("wheel", type=Path, help="path to the release .whl file")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="project root containing pyproject.toml and src/aios (default: cwd)",
    )
    parser.add_argument(
        "--skip-install-smoke",
        action="store_true",
        help="skip temporary-venv installation and executable smoke checks",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command-line verifier."""

    arguments = _build_parser().parse_args(argv)
    try:
        report = verify_release_wheel(
            arguments.wheel,
            arguments.project_root,
            install_smoke=not arguments.skip_install_smoke,
        )
    except WheelVerificationError as exc:
        print(f"release wheel verification failed: {exc}", file=sys.stderr)
        return 1

    smoke = "including install smoke" if report.install_smoke_ran else "archive checks only"
    print(
        "release wheel verified: "
        f"{report.distribution_name} {report.version}, "
        f"{report.asset_count} package assets ({smoke})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

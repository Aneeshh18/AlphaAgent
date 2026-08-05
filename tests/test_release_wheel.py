from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "verify_release_wheel.py"
SPEC = importlib.util.spec_from_file_location("verify_release_wheel", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

WheelVerificationError = MODULE.WheelVerificationError
verify_release_wheel = MODULE.verify_release_wheel
smoke_environment = MODULE._smoke_environment

DIST_INFO = "synthetic_aios-1.2.3.dist-info"
DIRECT_REQUIREMENTS = (
    "httpx>=0.27.0",
    "typer>=0.12.5",
)
OPTIONAL_REQUIREMENTS = (
    "streamlit<2.0,>=1.58.0; extra == 'dashboard'",
)
ALL_REQUIREMENTS = DIRECT_REQUIREMENTS + OPTIONAL_REQUIREMENTS


def _project(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    root = tmp_path / "project"
    package = root / "src" / "aios"
    scenario = package / "risk" / "scenarios" / "us_equity_reference_v1.json"
    scenario.parent.mkdir(parents=True)
    assets = {
        "aios/__init__.py": b'"""Synthetic package."""\n\n__version__ = "1.2.3"\n',
        "aios/anomalies.py": b'"""Synthetic anomaly module."""\n',
        "aios/dashboard.css": b":root { --accent: #123456; }\n",
        "aios/risk/__init__.py": b"",
        "aios/risk/scenarios/us_equity_reference_v1.json": b'{"name":"synthetic"}\n',
    }
    for member, payload in assets.items():
        source = root / "src" / member
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(payload)
    (root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "synthetic-aios"',
                'version = "1.2.3"',
                'requires-python = ">=3.12"',
                'dependencies = ["httpx>=0.27.0", "typer>=0.12.5"]',
                "",
                "[project.optional-dependencies]",
                'dashboard = ["streamlit>=1.58.0,<2.0"]',
                "",
                "[project.scripts]",
                'aios = "aios.cli:app"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return root, assets


def _wheel(
    tmp_path: Path,
    assets: dict[str, bytes],
    *,
    name: str = "synthetic-aios",
    version: str = "1.2.3",
    entry_point: str = "aios.cli:app",
    requires_python: str = ">=3.12",
    requires_dist: tuple[str, ...] = ALL_REQUIREMENTS,
    provides_extra: tuple[str, ...] = ("dashboard",),
    wheel_version: str = "1.0",
    generator: str = "synthetic-test",
    root_is_purelib: str = "true",
    tag: str = "py3-none-any",
    record_overrides: dict[str, tuple[str, str] | None] | None = None,
    record_extra_rows: tuple[tuple[str, str, str], ...] = (),
) -> Path:
    wheel = tmp_path / "synthetic_aios-1.2.3-py3-none-any.whl"
    metadata_lines = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        f"Version: {version}",
        f"Requires-Python: {requires_python}",
    ]
    metadata_lines.extend(f"Provides-Extra: {extra}" for extra in provides_extra)
    metadata_lines.extend(f"Requires-Dist: {value}" for value in requires_dist)
    members = dict(assets)
    members[f"{DIST_INFO}/METADATA"] = (
        "\n".join(metadata_lines) + "\n\n"
    ).encode()
    members[f"{DIST_INFO}/entry_points.txt"] = (
        f"[console_scripts]\naios = {entry_point}\n"
    ).encode()
    members[f"{DIST_INFO}/WHEEL"] = (
        f"Wheel-Version: {wheel_version}\n"
        f"Generator: {generator}\n"
        f"Root-Is-Purelib: {root_is_purelib}\n"
        f"Tag: {tag}\n"
    ).encode()
    record_member = f"{DIST_INFO}/RECORD"
    records: dict[str, tuple[str, str] | None] = {
        member: _record_value(payload)
        for member, payload in members.items()
    }
    records[record_member] = ("", "")
    records.update(record_overrides or {})
    record_buffer = io.StringIO(newline="")
    writer = csv.writer(record_buffer, lineterminator="\n")
    for member, value in sorted(records.items()):
        if value is not None:
            writer.writerow((member, *value))
    writer.writerows(record_extra_rows)
    members[record_member] = record_buffer.getvalue().encode()

    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member, payload in sorted(members.items()):
            archive.writestr(member, payload)
    return wheel


def _record_value(payload: bytes) -> tuple[str, str]:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
    return f"sha256={digest.rstrip(b'=').decode()}", str(len(payload))


def test_verify_release_wheel_accepts_exact_synthetic_archive(tmp_path: Path) -> None:
    root, assets = _project(tmp_path)
    wheel = _wheel(tmp_path, assets)

    report = verify_release_wheel(wheel, root, install_smoke=False)

    assert report.distribution_name == "synthetic-aios"
    assert report.version == "1.2.3"
    assert report.asset_count == len(assets)
    assert report.install_smoke_ran is False


def test_verify_release_wheel_rejects_stale_asset(tmp_path: Path) -> None:
    root, assets = _project(tmp_path)
    assets["aios/anomalies.py"] = b'"""Old anomaly module."""\n'
    wheel = _wheel(tmp_path, assets)

    with pytest.raises(WheelVerificationError, match="stale package asset"):
        verify_release_wheel(wheel, root, install_smoke=False)


def test_verify_release_wheel_rejects_missing_asset(tmp_path: Path) -> None:
    root, assets = _project(tmp_path)
    del assets["aios/dashboard.css"]
    wheel = _wheel(tmp_path, assets)

    with pytest.raises(WheelVerificationError, match=r"missing: aios/dashboard\.css"):
        verify_release_wheel(wheel, root, install_smoke=False)


def test_verify_release_wheel_rejects_unexpected_asset(tmp_path: Path) -> None:
    root, assets = _project(tmp_path)
    assets["aios/obsolete.py"] = b"OLD = True\n"
    wheel = _wheel(tmp_path, assets)

    with pytest.raises(WheelVerificationError, match=r"unexpected: aios/obsolete\.py"):
        verify_release_wheel(wheel, root, install_smoke=False)


def test_verify_release_wheel_rejects_secret_shaped_exact_source_asset(
    tmp_path: Path,
) -> None:
    root, assets = _project(tmp_path)
    member = "aios/credentials.json"
    assets[member] = b""
    source = root / "src" / member
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(assets[member])
    wheel = _wheel(tmp_path, assets)

    with pytest.raises(
        WheelVerificationError,
        match="repository secret-hygiene metadata policy",
    ):
        verify_release_wheel(wheel, root, install_smoke=False)


@pytest.mark.parametrize(
    "unexpected_member",
    [
        "aios/bootstrap.pth",
        "aios/native.so",
        "synthetic_aios-1.2.3.data/scripts/sidecar",
    ],
)
def test_verify_release_wheel_rejects_every_unexpected_payload(
    tmp_path: Path,
    unexpected_member: str,
) -> None:
    root, assets = _project(tmp_path)
    wheel = _wheel(tmp_path, assets)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(unexpected_member, b"unexpected")

    with pytest.raises(WheelVerificationError, match="unexpected:"):
        verify_release_wheel(wheel, root, install_smoke=False)


def test_verify_release_wheel_rejects_symlink_member(tmp_path: Path) -> None:
    root, assets = _project(tmp_path)
    wheel = _wheel(tmp_path, assets)
    link = zipfile.ZipInfo("aios/link.py")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(link, "anomalies.py")

    with pytest.raises(WheelVerificationError, match="symbolic link"):
        verify_release_wheel(wheel, root, install_smoke=False)


def test_verify_release_wheel_rejects_source_change_during_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, assets = _project(tmp_path)
    wheel = _wheel(tmp_path, assets)
    original = MODULE._verify_console_entry_point

    def mutate_source(archive: zipfile.ZipFile) -> None:
        original(archive)
        source = root / "src" / "aios" / "anomalies.py"
        source.write_bytes(source.read_bytes() + b"# changed\n")

    monkeypatch.setattr(MODULE, "_verify_console_entry_point", mutate_source)

    with pytest.raises(WheelVerificationError, match="source changed during"):
        verify_release_wheel(wheel, root, install_smoke=False)


def test_smoke_environment_is_secret_free_and_uses_temporary_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIOS_FRED_API_KEY", "secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("PYTHONPATH", "/unsafe/source")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

    environment = smoke_environment(tmp_path)

    assert environment["AIOS_PROJECT_ROOT"] == str(tmp_path)
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment.get("PYTHONPATH") != "/unsafe/source"
    assert "AIOS_FRED_API_KEY" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment


@pytest.mark.parametrize(
    ("wheel_kwargs", "message"),
    [
        ({"name": "wrong-name"}, "METADATA Name"),
        ({"version": "9.9.9"}, "METADATA Version"),
        ({"entry_point": "aios.cli:wrong"}, "console entry point"),
    ],
)
def test_verify_release_wheel_rejects_invalid_distribution_contract(
    tmp_path: Path,
    wheel_kwargs: dict[str, str],
    message: str,
) -> None:
    root, assets = _project(tmp_path)
    wheel = _wheel(tmp_path, assets, **wheel_kwargs)

    with pytest.raises(WheelVerificationError, match=message):
        verify_release_wheel(wheel, root, install_smoke=False)


@pytest.mark.parametrize(
    ("wheel_kwargs", "message"),
    [
        ({"requires_python": ">=3.11"}, "Requires-Python"),
        (
            {"requires_dist": ALL_REQUIREMENTS[:-1]},
            r"Requires-Dist differs.*missing: streamlit",
        ),
        (
            {"requires_dist": ALL_REQUIREMENTS + ("requests>=2.0",)},
            r"Requires-Dist differs.*unexpected: requests",
        ),
        (
            {
                "requires_dist": (
                    "httpx>=0.28.0",
                    "typer>=0.12.5",
                    *OPTIONAL_REQUIREMENTS,
                )
            },
            "Requires-Dist differs",
        ),
        ({"provides_extra": ()}, "Provides-Extra differs"),
    ],
)
def test_verify_release_wheel_rejects_metadata_dependency_drift(
    tmp_path: Path,
    wheel_kwargs: dict[str, object],
    message: str,
) -> None:
    root, assets = _project(tmp_path)
    wheel = _wheel(tmp_path, assets, **wheel_kwargs)

    with pytest.raises(WheelVerificationError, match=message):
        verify_release_wheel(wheel, root, install_smoke=False)


@pytest.mark.parametrize(
    ("wheel_kwargs", "message"),
    [
        ({"wheel_version": "2.0"}, "Wheel-Version"),
        ({"generator": ""}, "nonblank Generator"),
        ({"root_is_purelib": "false"}, "Root-Is-Purelib"),
        ({"tag": "cp312-none-any"}, "Tag fields"),
    ],
)
def test_verify_release_wheel_rejects_invalid_wheel_file_contract(
    tmp_path: Path,
    wheel_kwargs: dict[str, object],
    message: str,
) -> None:
    root, assets = _project(tmp_path)
    wheel = _wheel(tmp_path, assets, **wheel_kwargs)

    with pytest.raises(WheelVerificationError, match=message):
        verify_release_wheel(wheel, root, install_smoke=False)


def test_verify_release_wheel_rejects_record_coverage_drift(tmp_path: Path) -> None:
    root, assets = _project(tmp_path)
    wheel = _wheel(
        tmp_path,
        assets,
        record_overrides={
            "aios/anomalies.py": None,
            "ghost.py": _record_value(b"ghost"),
        },
    )

    with pytest.raises(
        WheelVerificationError,
        match=r"RECORD coverage differs.*missing: aios/anomalies.py.*unexpected: ghost.py",
    ):
        verify_release_wheel(wheel, root, install_smoke=False)


@pytest.mark.parametrize(
    ("record_overrides", "message"),
    [
        (
            {"aios/anomalies.py": ("sha256=invalid", "32")},
            r"RECORD hash mismatch for aios/anomalies.py",
        ),
        (
            {
                "aios/anomalies.py": (
                    _record_value(b'"""Synthetic anomaly module."""\n')[0],
                    "999",
                )
            },
            r"RECORD size mismatch for aios/anomalies.py",
        ),
        (
            {"aios/anomalies.py": ("", "")},
            r"RECORD hash mismatch for aios/anomalies.py",
        ),
        (
            {f"{DIST_INFO}/RECORD": _record_value(b"not-self-verifiable")},
            "RECORD self-entry must have empty hash and size",
        ),
    ],
)
def test_verify_release_wheel_rejects_invalid_record_hash_or_size(
    tmp_path: Path,
    record_overrides: dict[str, tuple[str, str] | None],
    message: str,
) -> None:
    root, assets = _project(tmp_path)
    wheel = _wheel(
        tmp_path,
        assets,
        record_overrides=record_overrides,
    )

    with pytest.raises(WheelVerificationError, match=message):
        verify_release_wheel(wheel, root, install_smoke=False)


def test_verify_release_wheel_rejects_duplicate_record_member(tmp_path: Path) -> None:
    root, assets = _project(tmp_path)
    digest, size = _record_value(assets["aios/anomalies.py"])
    wheel = _wheel(
        tmp_path,
        assets,
        record_extra_rows=(("aios/anomalies.py", digest, size),),
    )

    with pytest.raises(WheelVerificationError, match="duplicate member"):
        verify_release_wheel(wheel, root, install_smoke=False)

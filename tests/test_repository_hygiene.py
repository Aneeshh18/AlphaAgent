from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).parents[1] / "scripts" / "verify_repository_hygiene.py"
)
SPEC = importlib.util.spec_from_file_location(
    "verify_repository_hygiene",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

classify_path = MODULE.classify_path
verify_archive = MODULE.verify_archive
verify_git_index = MODULE.verify_git_index
verify_tree = MODULE.verify_tree
HygieneVerificationError = MODULE.HygieneVerificationError


@pytest.mark.parametrize(
    "path",
    (
        ".env.example",
        ".env.sample",
        ".streamlit/secrets.toml.example",
        "README.md",
        "docs/public-ca.pem",
        "examples/provider-symbols.csv",
        "src/aios/risk/scenarios/reference.json",
        "tests/fixtures/public/synthetic.duckdb",
        "tests/fixtures/public/synthetic.log",
    ),
)
def test_public_templates_certificates_docs_and_fixtures_are_allowed(path: str) -> None:
    assert classify_path(path) is None


@pytest.mark.parametrize(
    ("path", "code"),
    (
        (".zcode/v2/config.json", "local_ai_tooling"),
        ("nested/.zcode/certs/public-ca.pem", "local_ai_tooling"),
        (".env", "environment_secret_file"),
        (".env.production", "environment_secret_file"),
        ("src/aios/signing.key", "private_key_material"),
        ("src/aios/client-private-key.pem", "private_key_material"),
        ("id_ed25519", "private_key_material"),
        (".aws/credentials", "local_credential_store"),
        (
            ".config/gcloud/application_default_credentials.json",
            "local_credential_store",
        ),
        (".config/gh/hosts.yml", "local_credential_store"),
        (".kube/config", "local_credential_store"),
        (".docker/config.json", "local_credential_store"),
        (".terraform.d/credentials.tfrc.json", "local_credential_store"),
        (".streamlit/secrets.toml", "local_credential_store"),
        (".streamlit/secrets.production.toml", "local_credential_store"),
        (".netrc", "local_credential_store"),
        ("credentials.json", "local_credential_store"),
        ("service-account-release.json", "local_credential_store"),
        ("data/aios.duckdb", "sensitive_runtime_path"),
        ("build/logs/run.txt", "sensitive_runtime_path"),
        ("runtime/local-state.json", "sensitive_runtime_path"),
        ("state/backups/release.bin", "sensitive_runtime_path"),
        ("state.sqlite-wal", "sensitive_runtime_path"),
        ("application.log", "sensitive_runtime_path"),
    ),
)
def test_secret_shaped_paths_are_rejected(path: str, code: str) -> None:
    assert classify_path(path) == code


@pytest.mark.parametrize(
    "path",
    (
        "../credentials.json",
        "/absolute/file.txt",
        "folder\\credentials.json",
        "folder//file.txt",
        "folder/\nfile.txt",
        "C:/credentials.json",
    ),
)
def test_ambiguous_or_unsafe_paths_are_rejected(path: str) -> None:
    assert classify_path(path) == "unsafe_path"


def test_git_index_inspection_uses_only_cached_path_and_mode_metadata(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    (repository / "README.md").touch()
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)

    clean = verify_git_index(repository)

    assert clean.path_count == 1
    assert clean.violations == ()

    credential = repository / ".zcode" / "v2" / "credentials.json"
    credential.parent.mkdir(parents=True)
    credential.touch()
    subprocess.run(
        ["git", "add", "--force", ".zcode/v2/credentials.json"],
        cwd=repository,
        check=True,
    )

    rejected = verify_git_index(repository)

    assert [(item.path, item.code) for item in rejected.violations] == [
        (".zcode/v2/credentials.json", "local_ai_tooling")
    ]


def test_git_index_rejects_repository_symlink_alias(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    alias = tmp_path / "repository-alias"
    alias.symlink_to(repository, target_is_directory=True)

    with pytest.raises(HygieneVerificationError, match="symbolic-link alias"):
        verify_git_index(alias)


def test_git_index_rejects_gitlink_mode(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    object_id = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repository,
        input=b"",
        check=True,
        capture_output=True,
    ).stdout.decode("ascii").strip()
    subprocess.run(
        [
            "git",
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{object_id},vendor/module",
        ],
        cwd=repository,
        check=True,
    )

    report = verify_git_index(repository)

    assert [(item.path, item.code) for item in report.violations] == [
        ("vendor/module", "unsupported_git_mode")
    ]


def test_git_index_rejects_unmerged_duplicate_stages(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    object_id = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repository,
        input=b"",
        check=True,
        capture_output=True,
    ).stdout.decode("ascii").strip()
    index_entries = "".join(
        f"100644 {object_id} {stage}\tconflict.txt\n"
        for stage in (1, 2, 3)
    )
    subprocess.run(
        ["git", "update-index", "--index-info"],
        cwd=repository,
        input=index_entries,
        check=True,
        text=True,
    )

    report = verify_git_index(repository)

    assert [(item.path, item.code) for item in report.violations] == [
        ("conflict.txt", "duplicate_git_index_path"),
        ("conflict.txt", "unmerged_git_stage"),
    ]


def test_publication_tree_rejects_runtime_paths_without_opening_payloads(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "publication"
    (bundle / "docs").mkdir(parents=True)
    (bundle / "docs" / "public-ca.pem").touch()
    (bundle / "backups").mkdir()
    (bundle / "backups" / "state.bin").touch()

    report = verify_tree(bundle)

    assert [(item.path, item.code) for item in report.violations] == [
        ("backups", "sensitive_runtime_path"),
        ("backups/state.bin", "sensitive_runtime_path"),
    ]


def test_publication_tree_rejects_symlink_root_alias(tmp_path: Path) -> None:
    bundle = tmp_path / "publication"
    bundle.mkdir()
    alias = tmp_path / "publication-alias"
    alias.symlink_to(bundle, target_is_directory=True)

    with pytest.raises(HygieneVerificationError, match="symbolic-link alias"):
        verify_tree(alias)


def test_publication_tree_rejects_hardlinked_regular_files(tmp_path: Path) -> None:
    bundle = tmp_path / "publication"
    bundle.mkdir()
    first = bundle / "first.txt"
    first.touch()
    os.link(first, bundle / "second.txt")

    report = verify_tree(bundle)

    assert [(item.path, item.code) for item in report.violations] == [
        ("first.txt", "hardlinked_publication_file"),
        ("second.txt", "hardlinked_publication_file"),
    ]


def test_zip_publication_metadata_rejects_secret_paths_and_duplicates(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "publication.zip"
    with (
        pytest.warns(UserWarning, match="Duplicate name"),
        zipfile.ZipFile(bundle, "w") as archive,
    ):
        archive.writestr("package/docs/public-ca.pem", b"")
        archive.writestr("package/.env.production", b"")
        archive.writestr("package/.env.production", b"")

    report = verify_archive(bundle)

    assert [(item.path, item.code) for item in report.violations] == [
        ("package/.env.production", "duplicate_archive_member"),
        ("package/.env.production", "environment_secret_file"),
    ]


def test_archive_rejects_symlink_path_and_hardlinked_artifact(tmp_path: Path) -> None:
    bundle = tmp_path / "publication.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("package/README.md", b"")
    symlink = tmp_path / "publication-alias.zip"
    symlink.symlink_to(bundle)

    with pytest.raises(HygieneVerificationError, match="symbolic-link alias"):
        verify_archive(symlink)

    hardlink = tmp_path / "publication-hardlink.zip"
    os.link(bundle, hardlink)
    with pytest.raises(HygieneVerificationError, match="hard linked"):
        verify_archive(bundle)


def test_tar_sdist_metadata_rejects_private_key_path(tmp_path: Path) -> None:
    sdist = tmp_path / "publication.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        safe = tarfile.TarInfo("package/docs/public-ca.pem")
        safe.size = 0
        archive.addfile(safe, io.BytesIO())
        private = tarfile.TarInfo("package/release-private-key.pem")
        private.size = 0
        archive.addfile(private, io.BytesIO())

    report = verify_archive(sdist)

    assert [(item.path, item.code) for item in report.violations] == [
        ("package/release-private-key.pem", "private_key_material")
    ]


def test_cli_json_is_deterministic_and_contains_only_path_metadata(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "publication.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("package/data/state.duckdb", b"")
        archive.writestr("package/.zcode/config.json", b"")
    command = [
        sys.executable,
        str(SCRIPT_PATH),
        "--artifact",
        str(bundle),
        "--json",
    ]

    first = subprocess.run(command, check=False, capture_output=True, text=True)
    second = subprocess.run(command, check=False, capture_output=True, text=True)

    assert first.returncode == second.returncode == 1
    assert first.stdout == second.stdout
    assert first.stderr == second.stderr == ""
    payload = json.loads(first.stdout)
    assert payload == {
        "path_count": 2,
        "policy": "repository-secret-hygiene.v1",
        "target_count": 1,
        "violations": [
            {
                "code": "local_ai_tooling",
                "path": "package/.zcode/config.json",
                "source": "archive:publication.zip",
            },
            {
                "code": "sensitive_runtime_path",
                "path": "package/data/state.duckdb",
                "source": "archive:publication.zip",
            },
        ],
    }


def test_gitignore_and_hatch_exclusions_cover_release_boundary_paths() -> None:
    project = Path(__file__).resolve().parents[1]
    ignored = set(
        (project / ".gitignore").read_text(encoding="utf-8").splitlines()
    )
    assert {
        ".zcode/",
        ".env",
        ".env.*",
        "*.key",
        ".aws/",
        ".config/gcloud/",
        ".config/gh/",
        ".docker/config.json",
        ".streamlit/secrets.toml",
        "credentials.json",
        "application_default_credentials.json",
        "data/",
        "logs/",
        "runtime/",
        "backups/",
        "*.duckdb",
        "*.sqlite",
        "*.log",
    } <= ignored

    with (project / "pyproject.toml").open("rb") as handle:
        exclusions = set(tomllib.load(handle)["tool"]["hatch"]["build"]["exclude"])
    assert {
        "/.zcode/**",
        "/.env",
        "/.env.*",
        "/data/**",
        "/logs/**",
        "/backups/**",
        "**/*.key",
        "**/.aws/**",
        "**/.config/gcloud/**",
        "**/.config/gh/**",
        "**/.docker/config.json",
        "**/.streamlit/secrets.toml",
        "**/credentials.json",
        "**/application_default_credentials.json",
        "**/*.duckdb",
        "**/*.sqlite",
        "**/*.log",
        "/runtime/**",
    } <= exclusions


def test_ci_gate_uses_a_commit_pinned_checkout_and_path_metadata_only() -> None:
    project = Path(__file__).resolve().parents[1]
    workflow = (
        project / ".github" / "workflows" / "repository-secret-hygiene.yml"
    ).read_text(encoding="utf-8")

    assert (
        "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"
        in workflow
    )
    assert "persist-credentials: false" in workflow
    assert "python3 scripts/verify_repository_hygiene.py --git-index" in workflow
    assert "${{" not in workflow

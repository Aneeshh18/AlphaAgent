"""Fail-closed contracts for the backtest/factor experiment registry.

Every registration binds exact code, data and policy identity so a `frozen` or
`holdout` result cannot be silently cherry-picked or reproduced from the wrong
commit. `exploratory` runs are still write-once, but do not require a clean
worktree.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aios.backtest.engine import QVBacktestConfig, QVBacktestResult
from aios.canonical import canonical_sha256
from aios.experiments import (
    EXPERIMENT_SCHEMA_VERSION,
    compare_experiments,
    database_snapshot_sha256,
    git_fingerprint,
    list_experiments,
    load_experiment,
    register_experiment,
    retained_evidence_coverage,
)


class _EvidenceStore:
    """Serves only the raw_snapshots aggregate the registry reads."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def query(self, sql, params=None):
        assert "raw_snapshots" in sql
        return list(self._rows)


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)


def _result(**overrides) -> QVBacktestResult:
    config = QVBacktestConfig(
        start="2025-01-01", end="2025-04-01", universe_id="sp500"
    )
    return QVBacktestResult(config=config, tickers=("AAA", "BBB"), **overrides)


def _db_file(tmp_path: Path, content: bytes = b"duckdb-bytes") -> Path:
    path = tmp_path / "aios.duckdb"
    path.write_bytes(content)
    return path


def _evidence() -> _EvidenceStore:
    return _EvidenceStore(
        [
            {"dataset": "daily-prices", "row_count": 100, "latest": "2026-08-07T00:00:00+00:00"},
            {"dataset": "companyfacts", "row_count": 50, "latest": "2026-08-06T00:00:00+00:00"},
        ]
    )


# ----------------------------------------------------------------------
# git_fingerprint
# ----------------------------------------------------------------------
def test_git_fingerprint_reports_clean_worktree(tmp_path) -> None:
    _init_git_repo(tmp_path)
    fingerprint = git_fingerprint(project_root=tmp_path)
    assert fingerprint.dirty is False
    assert fingerprint.changed_files == ()
    assert len(fingerprint.commit_sha) == 40


def test_git_fingerprint_reports_dirty_paths_without_diff_content(tmp_path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "secret.env").write_text("API_KEY=super-secret-value\n")
    fingerprint = git_fingerprint(project_root=tmp_path)
    assert fingerprint.dirty is True
    assert fingerprint.changed_files == ("secret.env",)
    # Only the path is retained; the file's contents never enter the fingerprint.
    assert "super-secret-value" not in str(fingerprint.to_dict())


def test_git_fingerprint_fails_closed_outside_a_repo(tmp_path) -> None:
    with pytest.raises(ValueError):
        git_fingerprint(project_root=tmp_path)


# ----------------------------------------------------------------------
# database_snapshot_sha256
# ----------------------------------------------------------------------
def test_database_snapshot_sha256_is_exact_and_stable(tmp_path) -> None:
    db_path = _db_file(tmp_path)
    first = database_snapshot_sha256(db_path)
    second = database_snapshot_sha256(db_path)
    assert first == second
    assert len(first) == 64


def test_database_snapshot_sha256_requires_the_file_to_exist(tmp_path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        database_snapshot_sha256(tmp_path / "missing.duckdb")


# ----------------------------------------------------------------------
# retained_evidence_coverage
# ----------------------------------------------------------------------
def test_retained_evidence_coverage_is_deterministic_for_the_same_rows() -> None:
    store = _evidence()
    first = retained_evidence_coverage(store)
    second = retained_evidence_coverage(store)
    assert first == second
    assert len(first["coverage_sha256"]) == 64
    assert len(first["datasets"]) == 2


# ----------------------------------------------------------------------
# register_experiment
# ----------------------------------------------------------------------
def test_register_experiment_records_a_complete_document(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)
    db_path = _db_file(tmp_path)
    artifact_path = tmp_path / "backtest.json"
    artifact_path.write_text(json.dumps({"ok": True}))
    experiments_dir = tmp_path / "experiments"

    document = register_experiment(
        result=_result(),
        purpose="exploratory",
        artifact_path=artifact_path,
        store=_evidence(),
        db_path=db_path,
        project_root=repo_root,
        experiments_dir=experiments_dir,
    )

    assert document["schema_version"] == EXPERIMENT_SCHEMA_VERSION
    assert document["purpose"] == "exploratory"
    assert document["experiment_id"].startswith("exp-")
    assert document["git"]["dirty"] is False
    assert document["database_sha256"] == database_snapshot_sha256(db_path)
    assert document["parameters"]["universe_id"] == "sp500"
    assert document["parent_experiment_id"] is None

    reloaded = load_experiment(document["experiment_id"], experiments_dir=experiments_dir)
    assert reloaded == document


def test_register_experiment_is_write_once(tmp_path) -> None:
    """Two calls never collide; each experiment_id is unique and immutable."""
    _init_git_repo(tmp_path)
    db_path = _db_file(tmp_path)
    artifact_path = tmp_path / "backtest.json"
    artifact_path.write_text(json.dumps({"ok": True}))
    experiments_dir = tmp_path / "experiments"

    first = register_experiment(
        result=_result(),
        purpose="exploratory",
        artifact_path=artifact_path,
        store=_evidence(),
        db_path=db_path,
        project_root=tmp_path,
        experiments_dir=experiments_dir,
    )
    second = register_experiment(
        result=_result(),
        purpose="exploratory",
        artifact_path=artifact_path,
        store=_evidence(),
        db_path=db_path,
        project_root=tmp_path,
        experiments_dir=experiments_dir,
    )
    assert first["experiment_id"] != second["experiment_id"]
    assert len(list(experiments_dir.glob("exp-*.json"))) == 2


def test_frozen_experiment_refuses_a_dirty_worktree(tmp_path) -> None:
    """A frozen/holdout result must be reproducible from a named commit."""
    _init_git_repo(tmp_path)
    (tmp_path / "scratch.txt").write_text("wip\n")
    db_path = _db_file(tmp_path)
    artifact_path = tmp_path / "backtest.json"
    artifact_path.write_text(json.dumps({"ok": True}))

    with pytest.raises(ValueError, match="requires a clean worktree"):
        register_experiment(
            result=_result(),
            purpose="frozen",
            artifact_path=artifact_path,
            store=_evidence(),
            db_path=db_path,
            project_root=tmp_path,
            experiments_dir=tmp_path / "experiments",
        )


def test_holdout_experiment_refuses_a_dirty_worktree(tmp_path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "scratch.txt").write_text("wip\n")
    db_path = _db_file(tmp_path)
    artifact_path = tmp_path / "backtest.json"
    artifact_path.write_text(json.dumps({"ok": True}))

    with pytest.raises(ValueError, match="requires a clean worktree"):
        register_experiment(
            result=_result(),
            purpose="holdout",
            artifact_path=artifact_path,
            store=_evidence(),
            db_path=db_path,
            project_root=tmp_path,
            experiments_dir=tmp_path / "experiments",
        )


def test_frozen_experiment_accepts_a_clean_worktree(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)
    db_path = _db_file(tmp_path)
    artifact_path = tmp_path / "backtest.json"
    artifact_path.write_text(json.dumps({"ok": True}))

    document = register_experiment(
        result=_result(),
        purpose="frozen",
        artifact_path=artifact_path,
        store=_evidence(),
        db_path=db_path,
        project_root=repo_root,
        experiments_dir=tmp_path / "experiments",
    )
    assert document["purpose"] == "frozen"
    assert document["git"]["dirty"] is False


def test_register_experiment_rejects_an_unsupported_purpose(tmp_path) -> None:
    _init_git_repo(tmp_path)
    db_path = _db_file(tmp_path)
    artifact_path = tmp_path / "backtest.json"
    artifact_path.write_text(json.dumps({"ok": True}))

    with pytest.raises(ValueError, match="unsupported experiment purpose"):
        register_experiment(
            result=_result(),
            purpose="best_effort",
            artifact_path=artifact_path,
            store=_evidence(),
            db_path=db_path,
            project_root=tmp_path,
            experiments_dir=tmp_path / "experiments",
        )


def test_register_experiment_requires_the_artifact_to_exist(tmp_path) -> None:
    _init_git_repo(tmp_path)
    db_path = _db_file(tmp_path)

    with pytest.raises(ValueError, match="backtest artifact does not exist"):
        register_experiment(
            result=_result(),
            purpose="exploratory",
            artifact_path=tmp_path / "missing.json",
            store=_evidence(),
            db_path=db_path,
            project_root=tmp_path,
            experiments_dir=tmp_path / "experiments",
        )


def test_register_experiment_requires_matched_parent_and_reason(tmp_path) -> None:
    _init_git_repo(tmp_path)
    db_path = _db_file(tmp_path)
    artifact_path = tmp_path / "backtest.json"
    artifact_path.write_text(json.dumps({"ok": True}))

    with pytest.raises(ValueError, match="parent experiment"):
        register_experiment(
            result=_result(),
            purpose="exploratory",
            artifact_path=artifact_path,
            store=_evidence(),
            db_path=db_path,
            project_root=tmp_path,
            experiments_dir=tmp_path / "experiments",
            parent_experiment_id="exp-does-not-matter",
            comparison_reason=None,
        )


def test_register_experiment_accepts_a_parent_with_its_reason(tmp_path) -> None:
    _init_git_repo(tmp_path)
    db_path = _db_file(tmp_path)
    artifact_path = tmp_path / "backtest.json"
    artifact_path.write_text(json.dumps({"ok": True}))
    experiments_dir = tmp_path / "experiments"

    parent = register_experiment(
        result=_result(),
        purpose="exploratory",
        artifact_path=artifact_path,
        store=_evidence(),
        db_path=db_path,
        project_root=tmp_path,
        experiments_dir=experiments_dir,
    )
    child = register_experiment(
        result=_result(),
        purpose="exploratory",
        artifact_path=artifact_path,
        store=_evidence(),
        db_path=db_path,
        project_root=tmp_path,
        experiments_dir=experiments_dir,
        parent_experiment_id=parent["experiment_id"],
        comparison_reason="widened commission assumption",
    )
    assert child["parent_experiment_id"] == parent["experiment_id"]
    assert child["comparison_reason"] == "widened commission assumption"


# ----------------------------------------------------------------------
# Integrity and listing
# ----------------------------------------------------------------------
def test_load_experiment_detects_tampering(tmp_path) -> None:
    _init_git_repo(tmp_path)
    db_path = _db_file(tmp_path)
    artifact_path = tmp_path / "backtest.json"
    artifact_path.write_text(json.dumps({"ok": True}))
    experiments_dir = tmp_path / "experiments"

    document = register_experiment(
        result=_result(),
        purpose="exploratory",
        artifact_path=artifact_path,
        store=_evidence(),
        db_path=db_path,
        project_root=tmp_path,
        experiments_dir=experiments_dir,
    )
    stored_path = experiments_dir / f"{document['experiment_id']}.json"
    tampered = json.loads(stored_path.read_text())
    tampered["metrics"]["regime"]["cumulative_return"] = 999.0
    stored_path.write_text(json.dumps(tampered))

    with pytest.raises(ValueError, match="failed integrity check"):
        load_experiment(document["experiment_id"], experiments_dir=experiments_dir)


def test_load_experiment_fails_closed_when_missing(tmp_path) -> None:
    with pytest.raises(ValueError, match="no registered experiment"):
        load_experiment("exp-does-not-exist", experiments_dir=tmp_path)


def test_list_experiments_filters_by_purpose(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)
    db_path = _db_file(tmp_path)
    artifact_path = tmp_path / "backtest.json"
    artifact_path.write_text(json.dumps({"ok": True}))
    experiments_dir = tmp_path / "experiments"

    register_experiment(
        result=_result(), purpose="exploratory", artifact_path=artifact_path,
        store=_evidence(), db_path=db_path, project_root=repo_root,
        experiments_dir=experiments_dir,
    )
    register_experiment(
        result=_result(), purpose="frozen", artifact_path=artifact_path,
        store=_evidence(), db_path=db_path, project_root=repo_root,
        experiments_dir=experiments_dir,
    )

    assert len(list_experiments(experiments_dir=experiments_dir)) == 2
    frozen_only = list_experiments(purpose="frozen", experiments_dir=experiments_dir)
    assert len(frozen_only) == 1
    assert frozen_only[0]["purpose"] == "frozen"


def test_list_experiments_returns_empty_for_a_missing_directory(tmp_path) -> None:
    assert list_experiments(experiments_dir=tmp_path / "does-not-exist") == []


# ----------------------------------------------------------------------
# compare_experiments
# ----------------------------------------------------------------------
def test_compare_experiments_requires_at_least_two(tmp_path) -> None:
    with pytest.raises(ValueError, match="at least two"):
        compare_experiments(["exp-only-one"], experiments_dir=tmp_path)


def test_compare_experiments_rejects_a_duplicate_id(tmp_path) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        compare_experiments(["exp-a", "exp-a"], experiments_dir=tmp_path)


def test_compare_experiments_builds_a_row_per_experiment(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)
    db_path = _db_file(tmp_path)
    artifact_path = tmp_path / "backtest.json"
    artifact_path.write_text(json.dumps({"ok": True}))
    experiments_dir = tmp_path / "experiments"

    first = register_experiment(
        result=_result(), purpose="exploratory", artifact_path=artifact_path,
        store=_evidence(), db_path=db_path, project_root=repo_root,
        experiments_dir=experiments_dir,
    )
    config = QVBacktestConfig(
        start="2025-01-01", end="2025-04-01", universe_id="sp500", top_n=20
    )
    second = register_experiment(
        result=QVBacktestResult(config=config, tickers=("AAA", "BBB")),
        purpose="exploratory", artifact_path=artifact_path,
        store=_evidence(), db_path=db_path, project_root=repo_root,
        experiments_dir=experiments_dir,
    )

    comparison = compare_experiments(
        [first["experiment_id"], second["experiment_id"]],
        experiments_dir=experiments_dir,
    )
    assert comparison["experiment_ids"] == [
        first["experiment_id"],
        second["experiment_id"],
    ]
    assert comparison["comparable_window"] is True
    assert comparison["comparable_universe"] is True
    assert len(comparison["rows"]) == 2
    for row in comparison["rows"]:
        assert "regime_cumulative_return" in row
        assert "baseline_max_drawdown" in row
        assert row["universe_id"] == "sp500"


def test_compare_experiments_flags_an_incomparable_window(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)
    db_path = _db_file(tmp_path)
    artifact_path = tmp_path / "backtest.json"
    artifact_path.write_text(json.dumps({"ok": True}))
    experiments_dir = tmp_path / "experiments"

    first = register_experiment(
        result=_result(), purpose="exploratory", artifact_path=artifact_path,
        store=_evidence(), db_path=db_path, project_root=repo_root,
        experiments_dir=experiments_dir,
    )
    config = QVBacktestConfig(start="2020-01-01", end="2021-01-01", universe_id="sp500")
    second = register_experiment(
        result=QVBacktestResult(config=config, tickers=("AAA",)),
        purpose="exploratory", artifact_path=artifact_path,
        store=_evidence(), db_path=db_path, project_root=repo_root,
        experiments_dir=experiments_dir,
    )

    comparison = compare_experiments(
        [first["experiment_id"], second["experiment_id"]],
        experiments_dir=experiments_dir,
    )
    assert comparison["comparable_window"] is False
    assert comparison["rows"][0]["start"] != comparison["rows"][1]["start"]


def test_compare_experiments_fails_closed_on_a_missing_id(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)
    db_path = _db_file(tmp_path)
    artifact_path = tmp_path / "backtest.json"
    artifact_path.write_text(json.dumps({"ok": True}))
    experiments_dir = tmp_path / "experiments"

    first = register_experiment(
        result=_result(), purpose="exploratory", artifact_path=artifact_path,
        store=_evidence(), db_path=db_path, project_root=repo_root,
        experiments_dir=experiments_dir,
    )
    with pytest.raises(ValueError, match="no registered experiment"):
        compare_experiments(
            [first["experiment_id"], "exp-never-registered"],
            experiments_dir=experiments_dir,
        )


# ----------------------------------------------------------------------
# Policy identity wiring
# ----------------------------------------------------------------------
def test_register_experiment_embeds_a_policy_snapshot_by_default(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)
    db_path = _db_file(tmp_path)
    artifact_path = tmp_path / "backtest.json"
    artifact_path.write_text(json.dumps({"ok": True}))

    document = register_experiment(
        result=_result(),
        purpose="exploratory",
        artifact_path=artifact_path,
        store=_evidence(),
        db_path=db_path,
        project_root=repo_root,
        experiments_dir=tmp_path / "experiments",
    )
    assert document["policy"]["research_policy"]["name"] == "us-equity-qv-baseline"
    assert document["policy"]["market_profile"]["name"] == "us-equity-sp500-reference"
    assert document["policy"]["account_policy"]["name"] == "us-equity-paper-simulation"
    assert len(document["policy"]["combined_sha256"]) == 64


def test_register_experiment_accepts_an_explicit_policy_snapshot(tmp_path) -> None:
    from aios.policy_domains import policy_snapshot

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)
    db_path = _db_file(tmp_path)
    artifact_path = tmp_path / "backtest.json"
    artifact_path.write_text(json.dumps({"ok": True}))

    custom = policy_snapshot(research_name="custom-research-variant")
    document = register_experiment(
        result=_result(),
        purpose="exploratory",
        artifact_path=artifact_path,
        store=_evidence(),
        db_path=db_path,
        project_root=repo_root,
        experiments_dir=tmp_path / "experiments",
        policy=custom,
    )
    assert document["policy"]["research_policy"]["name"] == "custom-research-variant"


def test_compare_experiments_flags_a_shared_policy_identity(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)
    db_path = _db_file(tmp_path)
    artifact_path = tmp_path / "backtest.json"
    artifact_path.write_text(json.dumps({"ok": True}))
    experiments_dir = tmp_path / "experiments"

    first = register_experiment(
        result=_result(), purpose="exploratory", artifact_path=artifact_path,
        store=_evidence(), db_path=db_path, project_root=repo_root,
        experiments_dir=experiments_dir,
    )
    second = register_experiment(
        result=_result(), purpose="exploratory", artifact_path=artifact_path,
        store=_evidence(), db_path=db_path, project_root=repo_root,
        experiments_dir=experiments_dir,
    )
    comparison = compare_experiments(
        [first["experiment_id"], second["experiment_id"]],
        experiments_dir=experiments_dir,
    )
    assert comparison["comparable_policy"] is True
    assert comparison["rows"][0]["research_policy_name"] == "us-equity-qv-baseline"


def test_compare_experiments_flags_a_divergent_policy_identity(tmp_path) -> None:
    from aios.policy_domains import policy_snapshot

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)
    db_path = _db_file(tmp_path)
    artifact_path = tmp_path / "backtest.json"
    artifact_path.write_text(json.dumps({"ok": True}))
    experiments_dir = tmp_path / "experiments"

    first = register_experiment(
        result=_result(), purpose="exploratory", artifact_path=artifact_path,
        store=_evidence(), db_path=db_path, project_root=repo_root,
        experiments_dir=experiments_dir,
    )
    second = register_experiment(
        result=_result(), purpose="exploratory", artifact_path=artifact_path,
        store=_evidence(), db_path=db_path, project_root=repo_root,
        experiments_dir=experiments_dir,
        policy=policy_snapshot(research_version="v2-experimental"),
    )
    comparison = compare_experiments(
        [first["experiment_id"], second["experiment_id"]],
        experiments_dir=experiments_dir,
    )
    assert comparison["comparable_policy"] is False
    versions = {row["research_policy_version"] for row in comparison["rows"]}
    assert versions == {"v1", "v2-experimental"}


def test_compare_experiments_reports_incomparable_policy_for_legacy_documents(
    tmp_path,
) -> None:
    """A document registered before this field existed must not crash comparison."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)
    db_path = _db_file(tmp_path)
    artifact_path = tmp_path / "backtest.json"
    artifact_path.write_text(json.dumps({"ok": True}))
    experiments_dir = tmp_path / "experiments"

    first = register_experiment(
        result=_result(), purpose="exploratory", artifact_path=artifact_path,
        store=_evidence(), db_path=db_path, project_root=repo_root,
        experiments_dir=experiments_dir,
    )
    second = register_experiment(
        result=_result(), purpose="exploratory", artifact_path=artifact_path,
        store=_evidence(), db_path=db_path, project_root=repo_root,
        experiments_dir=experiments_dir,
    )
    # Simulate a legacy document by rewriting the stored file without `policy`.
    second_path = experiments_dir / f"{second['experiment_id']}.json"
    legacy = json.loads(second_path.read_text())
    del legacy["policy"]
    legacy["document_sha256"] = canonical_sha256(
        {key: value for key, value in legacy.items() if key != "document_sha256"}
    )
    second_path.write_text(json.dumps(legacy))

    comparison = compare_experiments(
        [first["experiment_id"], second["experiment_id"]],
        experiments_dir=experiments_dir,
    )
    assert comparison["comparable_policy"] is False
    assert comparison["rows"][1]["research_policy_name"] is None

"""Experiment registry for backtest and factor-research runs.

Prevents accidental cherry-picking and untraceable results by binding every
run to exact code, data and policy identity before its metrics are trusted:
Git commit, dirty-worktree fingerprint, database snapshot hash, retained
raw-evidence coverage, factor model, exclusions, cost/tax assumptions, and
resulting metrics. `frozen` and `holdout` purposes are append-only: the
registry refuses to overwrite an existing record rather than silently
replacing a result with a re-run.

No caller may pick the best of several runs by deleting or editing a losing
one; comparison is between compatible immutable records, never destructive.

CLI-wired via `backtest-qv --register-experiment`, `list-experiments`, and
`compare-experiments`; the dashboard's Operations workspace also lists
registered runs read-only, once the `forward-restart` narrowed the frozen
policy bundle and freed `cli.py`/`alerts.py` from it.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from aios.artifacts import publish_text_write_once
from aios.canonical import canonical_json, canonical_sha256, json_safe
from aios.policy_domains import policy_snapshot as _current_policy_snapshot

if TYPE_CHECKING:
    from aios.backtest.engine import QVBacktestResult

EXPERIMENT_PURPOSES = frozenset({"exploratory", "frozen", "holdout"})
EXPERIMENT_SCHEMA_VERSION = "aios-experiment.v1"
EXPERIMENT_ARTIFACT_DIR = Path("data/experiments")


@dataclass(frozen=True)
class GitFingerprint:
    """Exact code identity a run was produced under.

    A dirty worktree is not a defect — iterating on a backtest before a commit
    is normal — but a `frozen` or `holdout` run must be reproducible from a
    named commit, so registering one against a dirty tree fails closed.
    """

    commit_sha: str
    dirty: bool
    changed_files: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit_sha": self.commit_sha,
            "dirty": self.dirty,
            "changed_files": list(self.changed_files),
        }


def git_fingerprint(*, project_root: Path | None = None) -> GitFingerprint:
    """Return the exact commit and dirty-file set for the current worktree."""
    root = project_root or Path(__file__).resolve().parent.parent.parent
    commit_sha = _run_git(root, "rev-parse", "HEAD")
    status = _run_git(root, "status", "--porcelain")
    # Only paths are retained, never diff content: a dirty file may hold
    # unstaged secrets, and the registry must never become a place that leaks
    # them by way of a research artifact.
    changed_files = tuple(
        sorted({line[3:].strip() for line in status.splitlines() if line.strip()})
    )
    return GitFingerprint(
        commit_sha=commit_sha,
        dirty=bool(changed_files),
        changed_files=changed_files,
    )


def _run_git(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise ValueError("git is not available to fingerprint this run") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            f"git {' '.join(args)} failed: {exc.stderr.strip()}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"git {' '.join(args)} timed out") from exc
    return completed.stdout.strip()


def database_snapshot_sha256(db_path: Path) -> str:
    """Return a streaming SHA-256 of the exact DuckDB file bytes.

    DuckDB is single-process; run this only while no writer holds the
    database, the same discipline every other reader of this file follows.
    """
    if not db_path.is_file():
        raise ValueError(f"database file does not exist: {db_path}")
    digest = _sha256_file(db_path)
    return digest


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def retained_evidence_coverage(store: Any) -> dict[str, Any]:
    """Return the retained raw-snapshot coverage this run could have consumed.

    Hashing every payload's bytes into the experiment record would duplicate
    the immutable archive `verify-raw-snapshots` already covers. This binds
    the run to that archive's exact shape at record time instead: per-dataset
    row count and newest `received_at`, canonically hashed.
    """
    rows = store.query(
        """
        SELECT dataset, COUNT(*) AS row_count, MAX(received_at) AS latest
        FROM raw_snapshots
        GROUP BY dataset
        ORDER BY dataset
        """
    )
    datasets = [
        {
            "dataset": str(row["dataset"]),
            "row_count": int(row["row_count"]),
            "latest_received_at": str(row["latest"]),
        }
        for row in rows
    ]
    return {
        "datasets": datasets,
        "coverage_sha256": canonical_sha256(datasets),
    }


def register_experiment(
    *,
    result: QVBacktestResult,
    purpose: str,
    artifact_path: Path,
    store: Any,
    db_path: Path,
    parent_experiment_id: str | None = None,
    comparison_reason: str | None = None,
    notes: str | None = None,
    project_root: Path | None = None,
    experiments_dir: Path = EXPERIMENT_ARTIFACT_DIR,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and durably record one complete experiment identity.

    Returns the exact registered document. Raises rather than writing a
    partial record if any binding evidence is unavailable — an unregistered
    run is honest; a partially-registered one is not.

    ``policy`` binds the run to a named research/market/account policy
    identity (see `policy_domains.policy_snapshot`). It defaults to the
    current running code's snapshot, under the reviewed default names, so
    every registration carries this identity without the caller having to
    remember to ask for it.
    """
    if purpose not in EXPERIMENT_PURPOSES:
        raise ValueError(
            f"unsupported experiment purpose: {purpose!r}; "
            f"expected one of {sorted(EXPERIMENT_PURPOSES)}"
        )
    if not artifact_path.is_file():
        raise ValueError(f"backtest artifact does not exist: {artifact_path}")
    if (parent_experiment_id is None) != (comparison_reason is None):
        raise ValueError(
            "a parent experiment and its comparison reason must both be set "
            "or both be absent"
        )

    fingerprint = git_fingerprint(project_root=project_root)
    if purpose in ("frozen", "holdout") and fingerprint.dirty:
        raise ValueError(
            f"a {purpose} experiment requires a clean worktree; "
            f"{len(fingerprint.changed_files)} file(s) are uncommitted"
        )

    coverage = retained_evidence_coverage(store)
    policy = policy if policy is not None else _current_policy_snapshot()
    config = result.to_dict()["config"]
    # Benchmark metrics and the significance verdict are recorded alongside
    # the strategy's own numbers because without them a registered
    # experiment cannot answer the only question that matters — did this
    # beat buy-and-hold, and is the sample large enough to say so. A
    # registry that stores a strategy's return but not its benchmark's
    # invites the exact comparison-by-eye it exists to prevent.
    metrics = {
        "regime": result.regime_metrics.to_dict(),
        "baseline": result.baseline_metrics.to_dict(),
        "benchmark": {
            ticker: benchmark.to_dict()
            for ticker, benchmark in result.benchmark_metrics.items()
        },
        "significance": result.strategy_vs_benchmark_significance(),
        "comparison_periods": result.comparison_periods,
    }

    document: dict[str, Any] = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "experiment_id": f"exp-{uuid4().hex}",
        "purpose": purpose,
        "recorded_at": _now_iso(),
        "git": fingerprint.to_dict(),
        "database_sha256": database_snapshot_sha256(db_path),
        "retained_evidence_coverage": coverage,
        "policy": policy,
        "parameters": json_safe(config),
        "artifact_path": str(artifact_path),
        "artifact_sha256": _sha256_file(artifact_path),
        "metrics": json_safe(metrics),
        "warnings": list(result.warnings),
        "table_rowcounts": dict(result.table_rowcounts),
        "parent_experiment_id": parent_experiment_id,
        "comparison_reason": comparison_reason,
        "notes": notes,
    }
    document["document_sha256"] = canonical_sha256(document)

    destination = experiments_dir / f"{document['experiment_id']}.json"
    publish_text_write_once(destination, canonical_json(document))
    return document


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def load_experiment(
    experiment_id: str, *, experiments_dir: Path = EXPERIMENT_ARTIFACT_DIR
) -> dict[str, Any]:
    """Read back one registered experiment and verify it has not been altered."""
    path = experiments_dir / f"{experiment_id}.json"
    if not path.is_file():
        raise ValueError(f"no registered experiment: {experiment_id}")
    document = json.loads(path.read_text(encoding="utf-8"))
    stored_hash = document.get("document_sha256")
    recomputed = dict(document)
    recomputed.pop("document_sha256", None)
    if canonical_sha256(recomputed) != stored_hash:
        raise ValueError(
            f"registered experiment failed integrity check: {experiment_id}"
        )
    return document


def list_experiments(
    *,
    purpose: str | None = None,
    experiments_dir: Path = EXPERIMENT_ARTIFACT_DIR,
) -> list[dict[str, Any]]:
    """Return every registered experiment, optionally filtered by purpose."""
    if not experiments_dir.is_dir():
        return []
    documents = [
        load_experiment(path.stem, experiments_dir=experiments_dir)
        for path in sorted(experiments_dir.glob("exp-*.json"))
    ]
    if purpose is not None:
        documents = [doc for doc in documents if doc["purpose"] == purpose]
    return documents


_COMPARISON_METRIC_PATHS: tuple[tuple[str, str, str], ...] = (
    ("regime", "cumulative_return", "regime_cumulative_return"),
    ("regime", "annualized_return", "regime_annualized_return"),
    ("regime", "annualized_volatility", "regime_annualized_volatility"),
    ("regime", "max_drawdown", "regime_max_drawdown"),
    ("regime", "win_rate", "regime_win_rate"),
    ("baseline", "cumulative_return", "baseline_cumulative_return"),
    ("baseline", "max_drawdown", "baseline_max_drawdown"),
)


def compare_experiments(
    experiment_ids: list[str],
    *,
    experiments_dir: Path = EXPERIMENT_ARTIFACT_DIR,
) -> dict[str, Any]:
    """Build a side-by-side comparison of already-registered experiments.

    This never picks a winner. It surfaces compatible metrics and the exact
    identity each run was produced under so a human can decide, then activate
    the chosen configuration through the existing `forward-restart` gate —
    the same pattern `coverage_deterioration` and every other rule in this
    codebase uses: report the comparison, never silently act on it.

    Comparing across different `factor_model` or `universe_id` values is
    allowed here — unlike `scan_factor_percentile_jump`, which refuses it
    because a percentile *jump* would be a false alarm across a definition
    change. A comparison has no such failure mode: the difference is exactly
    what a human reviewing two candidate policies needs to see.
    """
    if len(experiment_ids) < 2:
        raise ValueError("compare_experiments needs at least two experiment IDs")
    if len(set(experiment_ids)) != len(experiment_ids):
        raise ValueError("compare_experiments received a duplicate experiment ID")

    documents = [
        load_experiment(experiment_id, experiments_dir=experiments_dir)
        for experiment_id in experiment_ids
    ]

    rows: list[dict[str, Any]] = []
    for document in documents:
        policy = document.get("policy")
        research_policy = policy.get("research_policy") if policy else None
        row: dict[str, Any] = {
            "experiment_id": document["experiment_id"],
            "purpose": document["purpose"],
            "recorded_at": document["recorded_at"],
            "commit_sha": document["git"]["commit_sha"],
            "dirty": document["git"]["dirty"],
            "factor_model": document["parameters"].get("factor_model"),
            "universe_id": document["parameters"].get("universe_id"),
            "start": document["parameters"].get("start"),
            "end": document["parameters"].get("end"),
            "excluded_tickers": document["parameters"].get("excluded_tickers"),
            "research_policy_name": (
                research_policy.get("name") if research_policy else None
            ),
            "research_policy_version": (
                research_policy.get("version") if research_policy else None
            ),
            "combined_policy_sha256": policy.get("combined_sha256") if policy else None,
        }
        for section, field_name, column in _COMPARISON_METRIC_PATHS:
            row[column] = document["metrics"].get(section, {}).get(field_name)
        significance = document["metrics"].get("significance") or {}
        row["significance_verdicts"] = {
            ticker: entry.get("verdict")
            for ticker, entry in significance.items()
            if isinstance(entry, dict)
        }
        row["beat_benchmark"] = _beat_benchmark(document)
        rows.append(row)

    same_window = len({(row["start"], row["end"]) for row in rows}) == 1
    same_universe = len({row["universe_id"] for row in rows}) == 1
    policy_hashes = {row["combined_policy_sha256"] for row in rows}
    same_policy = None not in policy_hashes and len(policy_hashes) == 1
    return {
        "experiment_ids": [document["experiment_id"] for document in documents],
        "comparable_window": same_window,
        "comparable_universe": same_universe,
        "comparable_policy": same_policy,
        "contradictions": _detect_contradictions(rows),
        "rows": rows,
    }


def _beat_benchmark(document: dict[str, Any]) -> bool | None:
    """Whether this run's strategy return exceeded every recorded benchmark.

    ``None`` when no benchmark was recorded, which is the case for every
    experiment registered before benchmarks were stored. An absent benchmark
    must never read as a win.
    """
    regime = (document["metrics"].get("regime") or {}).get("cumulative_return")
    benchmarks = document["metrics"].get("benchmark") or {}
    if regime is None or not benchmarks:
        return None
    returns = [
        entry.get("cumulative_return")
        for entry in benchmarks.values()
        if isinstance(entry, dict) and entry.get("cumulative_return") is not None
    ]
    if not returns:
        return None
    return all(regime > value for value in returns)


def _detect_contradictions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag any factor model that beats its benchmark in one window and loses in another.

    This is the specific failure this registry did not catch: a strategy was
    promoted to a live forward trial on the window where it won, while a
    window where it lost to the same benchmark by 14.5 points sat in the
    same directory. Reporting both runs side by side was not enough — the
    contradiction has to be named, because a reader comparing two rows of
    returns will reliably see the one that agrees with the decision already
    being made.
    """
    by_model: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("beat_benchmark") is None:
            continue
        by_model.setdefault(str(row.get("factor_model")), []).append(row)

    contradictions: list[dict[str, Any]] = []
    for factor_model, model_rows in sorted(by_model.items()):
        outcomes = {row["beat_benchmark"] for row in model_rows}
        if len(outcomes) > 1:
            contradictions.append(
                {
                    "factor_model": factor_model,
                    "detail": (
                        f"{factor_model} beat its benchmark in some registered "
                        "windows and lost in others. The ranking is not stable, "
                        "so neither window may be cited on its own as evidence "
                        "for or against this model."
                    ),
                    "beat_benchmark_in": [
                        row["experiment_id"] for row in model_rows if row["beat_benchmark"]
                    ],
                    "lost_to_benchmark_in": [
                        row["experiment_id"] for row in model_rows if not row["beat_benchmark"]
                    ],
                }
            )
    return contradictions

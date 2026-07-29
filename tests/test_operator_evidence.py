from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

from aios import operator_evidence
from aios.alerts import Alert, AlertSeverity, AlertStore
from aios.daily import DAILY_JOB_NAME
from aios.operator_evidence import (
    load_operations_evidence_read_only,
    load_paper_monitor_evidence,
)


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_paper_monitor_uses_the_latest_registered_custom_proposal_path(
    tmp_path,
    monkeypatch,
) -> None:
    account_path = tmp_path / "data" / "paper" / "us_qv_sandbox.json"
    trial_path = tmp_path / "data" / "paper" / "us_qv_forward_trial.json"
    custom_path = tmp_path / "data" / "paper" / "proposals" / "custom-plan.json"
    for path in (account_path, trial_path, custom_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    account = SimpleNamespace(
        path=account_path,
        payload={"executions": []},
        payload_sha256="a" * 64,
    )
    proposal = SimpleNamespace(
        path=custom_path,
        payload={
            "proposal_id": "proposal-custom",
            "status": "approved_for_supervised_simulation",
        },
        payload_sha256="b" * 64,
    )
    trial = SimpleNamespace(
        payload={
            "proposals": [
                {
                    "proposal_id": "proposal-custom",
                    "decision_date": "2026-07-28",
                    "generated_at": "2026-07-28T12:00:00Z",
                    "path": "data/paper/proposals/custom-plan.json",
                    "payload_sha256": "b" * 64,
                }
            ]
        },
        payload_sha256="c" * 64,
    )

    def read_document(path, *, expected_kind):
        if path == account_path:
            return account
        if path == custom_path:
            return proposal
        raise AssertionError(f"unexpected paper path: {path}")

    monkeypatch.setattr(operator_evidence, "read_paper_document", read_document)
    monkeypatch.setattr(
        operator_evidence,
        "paper_account_summary",
        lambda path, store: {"equity": 100_000.0, "holdings": []},
    )
    monkeypatch.setattr(
        operator_evidence,
        "assess_forward_trial",
        lambda *args: SimpleNamespace(
            ready=True,
            policy_unchanged=True,
            trial_id="trial-custom",
            registered_proposals=1,
            issues=(),
        ),
    )
    monkeypatch.setattr(operator_evidence, "read_forward_trial", lambda path: trial)
    monkeypatch.setattr(
        operator_evidence,
        "paper_proposal_timing_status",
        lambda payload, now=None: {"status": "execution_window_open"},
    )

    result = load_paper_monitor_evidence(tmp_path, object())

    assert result["proposal_path"] == "data/paper/proposals/custom-plan.json"
    assert result["proposal"]["proposal_id"] == "proposal-custom"
    assert result["proposal"]["registered_in_forward"] is True
    assert result["forward"]["ready"] is True


def test_operations_loader_is_read_only_and_preserves_database_identity(tmp_path) -> None:
    path = tmp_path / "operations.sqlite3"
    timestamp = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)
    store = AlertStore(path)
    route = store.enable_notification_route(
        "smtp-email",
        "a" * 64,
        route_alias="primary",
        now=timestamp,
    )
    incident = store.emit(
        Alert(
            code="review-source-warning",
            severity=AlertSeverity.WARNING,
            title="Review source warning",
            body="One source needs operator review.",
            dedup_key="review-source-warning",
            source_job="test",
        ),
        now=timestamp,
    )
    started = store.begin_job(
        DAILY_JOB_NAME,
        "2026-07-28",
        now=timestamp,
    )
    store.finish_job(
        started.run.run_id,
        state="success",
        detail="complete",
        now=timestamp,
    )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def directory_identity() -> dict[str, tuple[int, int, str]]:
        return {
            child.name: (
                child.stat().st_mtime_ns,
                child.stat().st_size,
                _sha256(child),
            )
            for child in sorted(tmp_path.iterdir())
            if child.is_file()
        }

    before = directory_identity()

    first = load_operations_evidence_read_only(path)
    second = load_operations_evidence_read_only(path)

    after = directory_identity()
    assert first == second
    assert after == before
    assert first["error"] is None
    assert first["incidents"][0]["incident_id"] == incident.incident_id
    assert first["daily_cycle"]["job_name"] == DAILY_JOB_NAME
    assert first["daily_cycle"]["state"] == "success"
    assert first["notification_summary"]["pending"] == 1
    assert first["notifications"][0]["incident_id"] == incident.incident_id
    assert first["notification_route"]["route_id"] == route.route_id
    assert first["notification_route"]["state"] == "enabled"


def test_operations_loader_fails_closed_when_ledger_changes_during_read(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "operations.sqlite3"
    AlertStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    identities = iter(
        [
            (1, 2, 3, 4, 5),
            (1, 2, 3, 4, 6),
        ]
    )
    monkeypatch.setattr(
        operator_evidence,
        "_file_identity",
        lambda _path: next(identities),
    )

    result = load_operations_evidence_read_only(path)

    assert result["incidents"] == []
    assert result["notifications"] == []
    assert "changed while read-only evidence was collected" in result["error"]


def test_paper_monitor_rejects_registered_path_outside_project(
    tmp_path,
    monkeypatch,
) -> None:
    account_path = tmp_path / "data" / "paper" / "us_qv_sandbox.json"
    trial_path = tmp_path / "data" / "paper" / "us_qv_forward_trial.json"
    outside_path = tmp_path.parent / "outside-proposal.json"
    for path in (account_path, trial_path, outside_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    monkeypatch.setattr(
        operator_evidence,
        "read_paper_document",
        lambda path, **kwargs: SimpleNamespace(
            path=path,
            payload={"executions": []},
            payload_sha256="a" * 64,
        ),
    )
    monkeypatch.setattr(
        operator_evidence,
        "paper_account_summary",
        lambda path, store: {"equity": 100_000.0, "holdings": []},
    )
    monkeypatch.setattr(
        operator_evidence,
        "read_forward_trial",
        lambda path: SimpleNamespace(
            payload={
                "proposals": [
                    {
                        "proposal_id": "outside",
                        "path": str(outside_path),
                    }
                ]
            },
            payload_sha256="c" * 64,
        )
    )

    result = load_paper_monitor_evidence(tmp_path, object())

    assert result["proposal"] is None
    assert result["forward"]["ready"] is False
    assert "escapes the project root" in result["forward"]["issues"][0]


def test_operations_loader_fails_closed_when_database_is_absent(tmp_path) -> None:
    result = load_operations_evidence_read_only(tmp_path / "missing.sqlite3")

    assert result["incidents"] == []
    assert result["daily_cycle"] is None
    assert "not initialized" in result["error"]


def test_paper_monitor_fails_closed_on_ambiguous_latest_registration(
    tmp_path,
    monkeypatch,
) -> None:
    account_path = tmp_path / "data" / "paper" / "us_qv_sandbox.json"
    trial_path = tmp_path / "data" / "paper" / "us_qv_forward_trial.json"
    proposal_paths = [
        tmp_path / "data" / "paper" / "proposals" / f"proposal-{suffix}.json"
        for suffix in ("a", "b")
    ]
    for path in (account_path, trial_path, *proposal_paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    account = SimpleNamespace(
        path=account_path,
        payload={"executions": []},
        payload_sha256="a" * 64,
    )
    trial = SimpleNamespace(
        payload={
            "proposals": [
                {
                    "proposal_id": f"proposal-{index}",
                    "decision_date": "2026-07-28",
                    "path": str(path.relative_to(tmp_path)),
                    "payload_sha256": str(index) * 64,
                }
                for index, path in enumerate(proposal_paths, start=1)
            ]
        },
        payload_sha256="c" * 64,
    )
    monkeypatch.setattr(
        operator_evidence,
        "read_paper_document",
        lambda path, **kwargs: account,
    )
    monkeypatch.setattr(
        operator_evidence,
        "paper_account_summary",
        lambda path, store: {"equity": 100_000.0, "holdings": []},
    )
    monkeypatch.setattr(operator_evidence, "read_forward_trial", lambda path: trial)
    monkeypatch.setattr(
        operator_evidence,
        "assess_forward_trial",
        lambda *args: SimpleNamespace(
            ready=True,
            policy_unchanged=True,
            trial_id="ambiguous",
            registered_proposals=2,
            issues=(),
        ),
    )

    result = load_paper_monitor_evidence(tmp_path, object())

    assert result["proposal"] is None
    assert result["forward"]["ready"] is False
    assert "ambiguous" in result["forward"]["issues"][0]


def test_paper_monitor_rejects_symlinked_registered_proposal(
    tmp_path,
    monkeypatch,
) -> None:
    account_path = tmp_path / "data" / "paper" / "us_qv_sandbox.json"
    trial_path = tmp_path / "data" / "paper" / "us_qv_forward_trial.json"
    real_path = tmp_path / "data" / "paper" / "proposals" / "real.json"
    linked_path = tmp_path / "data" / "paper" / "proposals" / "linked.json"
    for path in (account_path, trial_path, real_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    linked_path.symlink_to(real_path)

    account = SimpleNamespace(
        path=account_path,
        payload={"executions": []},
        payload_sha256="a" * 64,
    )
    monkeypatch.setattr(
        operator_evidence,
        "read_paper_document",
        lambda path, **kwargs: account,
    )
    monkeypatch.setattr(
        operator_evidence,
        "paper_account_summary",
        lambda path, store: {"equity": 100_000.0, "holdings": []},
    )
    monkeypatch.setattr(
        operator_evidence,
        "read_forward_trial",
        lambda path: SimpleNamespace(
            payload={
                "proposals": [
                    {
                        "proposal_id": "linked",
                        "decision_date": "2026-07-28",
                        "path": str(linked_path.relative_to(tmp_path)),
                    }
                ]
            },
            payload_sha256="c" * 64,
        ),
    )

    result = load_paper_monitor_evidence(tmp_path, object())

    assert result["proposal"] is None
    assert "symbolic link" in result["forward"]["issues"][0]


def test_operations_loader_refuses_uncheckpointed_wal_without_changes(tmp_path) -> None:
    path = tmp_path / "operations.sqlite3"
    path.write_bytes(b"not opened because WAL is present")
    wal = tmp_path / "operations.sqlite3-wal"
    wal.write_bytes(b"pending")
    before = {
        child.name: (child.stat().st_mtime_ns, child.read_bytes())
        for child in tmp_path.iterdir()
    }

    result = load_operations_evidence_read_only(path)

    after = {
        child.name: (child.stat().st_mtime_ns, child.read_bytes())
        for child in tmp_path.iterdir()
    }
    assert result["incidents"] == []
    assert "uncheckpointed WAL" in result["error"]
    assert after == before

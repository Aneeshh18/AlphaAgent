from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aios import operator_evidence
from aios.alerts import (
    Alert,
    AlertSeverity,
    AlertStore,
    AnomalyObservation,
    AnomalyScan,
    canonical_anomaly_fingerprint,
)
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
    anomaly_fingerprint = canonical_anomaly_fingerprint(
        rule_id="fundamentals_missing",
        rule_version="1",
        scope="us-equity",
        subject_type="issuer",
        subject_id="issuer-1",
    )
    anomaly_case = store.record_anomaly_scan(
        AnomalyScan(
            scan_id="scan-review-1",
            rule_bundle_version="dq-rules.v1",
            scope="us-equity",
            source_boundary_sha256="b" * 64,
            source_boundary_at=timestamp,
            executed_rules=("fundamentals_missing@1",),
            observations=(
                AnomalyObservation(
                    fingerprint=anomaly_fingerprint,
                    rule_id="fundamentals_missing",
                    rule_version="1",
                    scope="us-equity",
                    subject_type="issuer",
                    subject_id="issuer-1",
                    severity="high",
                    confidence="high",
                    title="Issuer fundamentals need review",
                    summary=(
                        "No accepted facts were available at the reviewed boundary."
                    ),
                    old_value={"minimum_accepted_fact_count": 1},
                    new_value={"accepted_fact_count": 0},
                    evidence={"snapshot_ids": ["snapshot-1"]},
                    suggested_checks=("Inspect the exact SEC response.",),
                ),
            ),
            evidence={"decision_date": "2026-07-28"},
        ),
        now=datetime(2026, 7, 29, 10, 5, tzinfo=UTC),
    )[0]
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
    assert first["incidents"][0]["resolution_proof_status"] == "not_applicable"
    assert first["incidents"][0]["operationally_blocking"] is True
    assert first["incident_page"] == {
        "limit": 100,
        "returned": 1,
        "total": 1,
        "truncated": False,
    }
    assert first["anomaly_case_page"]["truncated"] is False
    assert first["notification_page"]["truncated"] is False
    assert first["daily_cycle"]["job_name"] == DAILY_JOB_NAME
    assert first["daily_cycle"]["state"] == "success"
    assert first["notification_summary"]["pending"] == 1
    assert first["notifications"][0]["incident_id"] == incident.incident_id
    assert first["notification_route"]["route_id"] == route.route_id
    assert first["notification_route"]["state"] == "enabled"
    assert first["anomaly_cases"][0]["case_id"] == anomaly_case.case_id
    assert first["anomaly_cases"][0]["suggested_checks"] == [
        "Inspect the exact SEC response."
    ]
    assert first["anomaly_case_summary"] == {
        "open": 1,
        "acknowledged": 0,
        "deferred": 0,
        "resolved": 0,
        "unresolved": 1,
        "critical_unresolved": 0,
        "high_unresolved": 1,
        "total": 1,
        "affected_subjects": 1,
    }
    assert first["latest_anomaly_scan"]["scan_id"] == "scan-review-1"
    assert len(first["latest_anomaly_scan"]["payload_sha256"]) == 64
    assert first["latest_anomaly_scan"]["source_boundary_at"] == (
        "2026-07-29T10:00:00Z"
    )
    assert first["latest_anomaly_scan"]["recorded_at"] == (
        "2026-07-29T10:05:00Z"
    )
    assert first["latest_anomaly_scan"]["recorded_sequence"] == 1
    assert first["latest_anomaly_scan"]["observed_fingerprints"] == [
        anomaly_fingerprint
    ]


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
    assert result["anomaly_cases"] == []
    assert result["anomaly_case_summary"]["unresolved"] == 0
    assert result["latest_anomaly_scan"] is None
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


def test_operations_loader_rejects_missing_resolution_guard_without_changes(
    tmp_path,
) -> None:
    path = tmp_path / "operations.sqlite3"
    AlertStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DROP TRIGGER incident_events_resolution_proof_required"
        )
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = (path.stat().st_mtime_ns, path.read_bytes())

    result = load_operations_evidence_read_only(path)

    assert "incident proof schema is incomplete" in result["error"]
    assert (path.stat().st_mtime_ns, path.read_bytes()) == before


def test_operations_loader_prioritizes_critical_cases_before_page_limit(
    tmp_path,
) -> None:
    path = tmp_path / "operations.sqlite3"
    store = AlertStore(path)
    timestamp = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)

    def observation(index: int, *, severity: str) -> AnomalyObservation:
        subject = f"issuer-{index}"
        return AnomalyObservation(
            fingerprint=canonical_anomaly_fingerprint(
                rule_id="coverage",
                rule_version="1",
                scope="us-equity",
                subject_type="issuer",
                subject_id=subject,
            ),
            rule_id="coverage",
            rule_version="1",
            scope="us-equity",
            subject_type="issuer",
            subject_id=subject,
            severity=severity,
            confidence="high",
            title=f"Coverage review for {subject}",
            summary="One reviewed issuer needs evidence review.",
            old_value={"covered": True},
            new_value={"covered": False},
            evidence={"subject": subject},
            suggested_checks=("Inspect the exact source evidence.",),
        )

    observations = tuple(
        observation(index, severity="low") for index in range(100)
    ) + (observation(100, severity="critical"),)
    cases = store.record_anomaly_scan(
        AnomalyScan(
            scan_id="scan-priority",
            rule_bundle_version="coverage.v1",
            scope="us-equity",
            source_boundary_sha256="a" * 64,
            source_boundary_at=timestamp,
            executed_rules=("coverage@1",),
            observations=observations,
        ),
        now=timestamp,
    )
    critical = next(case for case in cases if case.severity == "critical")
    store.acknowledge_anomaly(
        critical.case_id,
        owner="data-ops",
        note="Critical case is under active review.",
        expected_evidence_sha256=critical.evidence_sha256,
        now=datetime(2026, 7, 29, 10, 0, 1, tzinfo=UTC),
    )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    result = load_operations_evidence_read_only(path, anomaly_case_limit=100)

    assert result["error"] is None
    assert len(result["anomaly_cases"]) == 100
    assert result["anomaly_cases"][0]["case_id"] == critical.case_id
    assert result["anomaly_cases"][0]["severity"] == "critical"
    assert result["anomaly_cases"][0]["state"] == "acknowledged"
    assert result["anomaly_case_page"] == {
        "limit": 100,
        "returned": 100,
        "total": 101,
        "truncated": True,
    }


def test_operations_loader_uses_exact_incident_summary_and_severity_priority(
    tmp_path,
) -> None:
    path = tmp_path / "operations.sqlite3"
    store = AlertStore(path)
    timestamp = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)
    for index in range(100):
        store.emit(
            Alert(
                code="warning",
                severity=AlertSeverity.WARNING,
                title=f"Warning {index}",
                body="One warning remains.",
                dedup_key=f"warning:{index}",
                source_job="test",
                notify=False,
            ),
            now=timestamp + timedelta(seconds=index),
        )
    critical = store.emit(
        Alert(
            code="critical",
            severity=AlertSeverity.CRITICAL,
            title="Critical acknowledged incident",
            body="This critical incident remains operationally blocking.",
            dedup_key="critical:acknowledged",
            source_job="test",
            notify=False,
        ),
        now=timestamp,
    )
    store.acknowledge(
        critical.incident_id,
        actor="ops@example.test",
        note="Critical evidence is under review.",
        expected_evidence_sha256=critical.evidence_sha256,
        now=datetime(2026, 7, 29, 10, 0, 1, tzinfo=UTC),
    )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    result = load_operations_evidence_read_only(path, incident_limit=100)

    assert result["error"] is None
    assert result["incident_summary"]["operational_blocking"] == 101
    assert result["incident_summary"]["critical_operational_blocking"] == 1
    assert result["incidents"][0]["incident_id"] == critical.incident_id
    assert result["incidents"][0]["state"] == "acknowledged"
    assert result["incidents"][1]["fingerprint"] == "warning:99"
    assert result["incident_page"] == {
        "limit": 100,
        "returned": 100,
        "total": 101,
        "truncated": True,
    }

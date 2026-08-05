from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

import aios.alerts as alerts_module
import aios.cli as cli
from aios.alerts import (
    Alert,
    AlertSeverity,
    AlertStore,
    AnomalyObservation,
    AnomalyScan,
    canonical_anomaly_fingerprint,
)

BASE_TIME = datetime(2026, 7, 30, 12, tzinfo=UTC)
RULE_ID = "sec_fundamentals_coverage_missing"
RULE_VERSION = "1.0.0"
SCOPE = "us-equity-reference:sp500"
IDENTITIES = (
    "aios:issuer:sec:0002082247",
    "aios:issuer:sec:0002089271",
    "aios:issuer:sec:0002115436",
)


def _observation(identity: str, *, revision: int = 1) -> AnomalyObservation:
    return AnomalyObservation(
        fingerprint=canonical_anomaly_fingerprint(
            rule_id=RULE_ID,
            rule_version=RULE_VERSION,
            scope=SCOPE,
            subject_type="issuer",
            subject_id=identity,
        ),
        rule_id=RULE_ID,
        rule_version=RULE_VERSION,
        scope=SCOPE,
        subject_type="issuer",
        subject_id=identity,
        severity="medium",
        confidence="high",
        title="SEC fundamentals coverage is missing",
        summary=f"{identity} has no accepted Company Facts rows.",
        old_value={"expected_state": "covered"},
        new_value={"coverage_state": "missing", "revision": revision},
        evidence={"run_id": f"run-{identity}-{revision}"},
        suggested_checks=("Review the exact source-bound evidence.",),
    )


def _record_cases(store: AlertStore) -> list:
    scan = AnomalyScan(
        scan_id="scan-fundamentals-review-1",
        rule_bundle_version="us-equity-data-quality.v1",
        scope=SCOPE,
        source_boundary_sha256="a" * 64,
        source_boundary_at=BASE_TIME,
        executed_rules=(f"{RULE_ID}@{RULE_VERSION}",),
        observations=tuple(_observation(identity) for identity in IDENTITIES),
        evidence={"as_of": "2026-07-29"},
    )
    return store.record_anomaly_scan(scan, now=BASE_TIME)


def _incident(store: AlertStore):
    return store.emit(
        Alert(
            code="current_refresh_partial",
            severity=AlertSeverity.WARNING,
            title="Current U.S. refresh completed with warnings",
            body="Some current fundamentals requests returned no accepted rows.",
            dedup_key="refresh:fundamentals:partial",
            source_job="aios refresh-us-current",
            payload={
                "areas": ["fundamentals"],
                "identities": list(IDENTITIES),
                "warning_count": len(IDENTITIES),
            },
        ),
        now=BASE_TIME - timedelta(days=1),
    )


def _resolve_cases(store: AlertStore, cases: list) -> None:
    for offset, case in enumerate(cases, start=1):
        store.resolve_anomaly(
            case.case_id,
            outcome="accepted",
            owner="research-ops",
            note="Reviewed exact evidence; score withholding remains unchanged.",
            expected_evidence_sha256=case.evidence_sha256,
            now=BASE_TIME + timedelta(minutes=offset),
        )


def test_partial_refresh_reconciles_only_after_every_named_case_is_resolved(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "operations.sqlite3")
    incident = _incident(store)
    cases = _record_cases(store)

    with pytest.raises(ValueError, match="remains unresolved"):
        store.fundamentals_review_recovery(
            incident.incident_id,
            expected_evidence_sha256=incident.evidence_sha256,
            observed_at=BASE_TIME + timedelta(minutes=10),
        )

    _resolve_cases(store, cases)
    recovery = store.fundamentals_review_recovery(
        incident.incident_id,
        expected_evidence_sha256=incident.evidence_sha256,
        observed_at=BASE_TIME + timedelta(minutes=10),
    )

    assert [row["subject_id"] for row in recovery.observation["reviewed_cases"]] == sorted(
        IDENTITIES
    )
    assert recovery.observation["safety"] == {
        "research_data_changed": False,
        "readiness_overridden": False,
        "paper_state_changed": False,
        "broker_action": False,
    }
    resolved = store.resolve_fingerprint(
        incident.fingerprint,
        recovery=recovery,
        now=BASE_TIME + timedelta(minutes=11),
    )

    assert resolved is not None
    assert resolved.state == "resolved"
    assert resolved.resolution_proof_status == "producer_verified_recovery"
    assert resolved.operationally_blocking is False
    event = store.events(incident.incident_id)[0]
    assert event["proof_kind"] == "fundamentals_review_reconciled"


def test_reconciliation_refuses_case_change_after_preview(tmp_path) -> None:
    store = AlertStore(tmp_path / "operations.sqlite3")
    incident = _incident(store)
    cases = _record_cases(store)
    _resolve_cases(store, cases)
    recovery = store.fundamentals_review_recovery(
        incident.incident_id,
        expected_evidence_sha256=incident.evidence_sha256,
        observed_at=BASE_TIME + timedelta(minutes=10),
    )

    changed = AnomalyScan(
        scan_id="scan-fundamentals-review-2",
        rule_bundle_version="us-equity-data-quality.v1",
        scope=SCOPE,
        source_boundary_sha256="b" * 64,
        source_boundary_at=BASE_TIME + timedelta(minutes=11),
        executed_rules=(f"{RULE_ID}@{RULE_VERSION}",),
        observations=(_observation(IDENTITIES[0], revision=2),),
        evidence={"as_of": "2026-07-29"},
    )
    store.record_anomaly_scan(changed, now=BASE_TIME + timedelta(minutes=11))

    with pytest.raises(ValueError, match="remains unresolved"):
        store.resolve_fingerprint(
            incident.fingerprint,
            recovery=recovery,
            now=BASE_TIME + timedelta(minutes=12),
        )
    assert store.get(incident.incident_id).state == "open"


def test_reconciliation_cli_is_preview_first_and_record_explicit(
    tmp_path,
    monkeypatch,
) -> None:
    store = AlertStore(tmp_path / "operations.sqlite3")
    incident = _incident(store)
    cases = _record_cases(store)
    _resolve_cases(store, cases)
    monkeypatch.setattr(alerts_module, "get_alert_store", lambda **_kwargs: store)

    preview = CliRunner().invoke(
        cli.app,
        [
            "alert-reconcile-fundamentals",
            incident.incident_id,
            "--evidence-sha256",
            incident.evidence_sha256,
            "--json",
        ],
    )
    assert preview.exit_code == 0, preview.output
    assert '"recorded": false' in preview.output.lower()
    assert store.get(incident.incident_id).state == "open"

    recorded = CliRunner().invoke(
        cli.app,
        [
            "alert-reconcile-fundamentals",
            incident.incident_id,
            "--evidence-sha256",
            incident.evidence_sha256,
            "--record",
        ],
    )
    assert recorded.exit_code == 0, recorded.output
    assert store.get(incident.incident_id).state == "resolved"

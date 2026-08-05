from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from aios.alerts import (
    SEC_SOURCE_BOUNDARY_POLICY_V1_TO_V2,
    SEC_SOURCE_BOUNDARY_POLICY_V2,
    AlertStore,
    AnomalyObservation,
    AnomalyScan,
    canonical_anomaly_fingerprint,
)

BASE_TIME = datetime(2026, 7, 28, 1, 0, tzinfo=UTC)
SCOPE = "sec-fundamental-coverage:2026-07-27"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _observation(
    *,
    fingerprint: str | None = None,
    rule_id: str = "sec_fundamentals_coverage_missing",
    rule_version: str = "1.0.0",
    subject_id: str = "issuer-1",
    marker: str = "first",
    severity: str = "medium",
    evidence: dict[str, Any] | None = None,
    scope: str = SCOPE,
) -> AnomalyObservation:
    return AnomalyObservation(
        fingerprint=fingerprint
        or canonical_anomaly_fingerprint(
            rule_id=rule_id,
            rule_version=rule_version,
            scope=scope,
            subject_type="issuer",
            subject_id=subject_id,
        ),
        rule_id=rule_id,
        rule_version=rule_version,
        scope=scope,
        subject_type="issuer",
        subject_id=subject_id,
        severity=severity,
        confidence="high",
        title=f"SEC coverage gap for {subject_id}",
        summary="No accepted CompanyFacts rows were found.",
        old_value={"accepted_rows": 1},
        new_value={"accepted_rows": 0, "marker": marker},
        evidence=evidence or {"run_id": f"run-{marker}"},
        suggested_checks=("Inspect the immutable SEC snapshot.",),
    )


def _scan(
    scan_id: str,
    *,
    observations: tuple[AnomalyObservation, ...],
    source_boundary_at: datetime = BASE_TIME,
    executed_rules: tuple[str, ...] = (
        "sec_fundamentals_coverage_missing@1.0.0",
    ),
    rule_bundle_version: str = "sec-coverage-v1",
    scope: str = SCOPE,
    include_clearance_proof: bool = True,
    extra_evidence: dict[str, Any] | None = None,
) -> AnomalyScan:
    evidence: dict[str, Any] = {"certified_close": "2026-07-27"}
    if extra_evidence is not None:
        evidence.update(extra_evidence)
    if not observations and include_clearance_proof:
        cleared = _observation()
        proof_body = {
            "rule_id": cleared.rule_id,
            "rule_version": cleared.rule_version,
            "scope": cleared.scope,
            "subject_type": cleared.subject_type,
            "subject_id": cleared.subject_id,
            "coverage_state": "covered_with_verified_ingest",
            "accepted_rows": 1,
            "ingest_id": 2,
            "ingest_run_id": "run-success",
            "ingest_rows_inserted": 1,
            "prior_warning_run_id": "run-warning",
            "companyfacts_snapshot": {
                "role": "companyfacts",
                "payload_sha256": "a" * 64,
                "parsed_rows_sha256": "b" * 64,
                "parsed_row_count": 1,
            },
            "submissions_snapshot": {
                "role": "submissions",
                "payload_sha256": "c" * 64,
            },
        }
        proof_sha256 = hashlib.sha256(
            json.dumps(
                proof_body,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        evidence["clearance_proofs"] = {
            cleared.fingerprint: {
                **proof_body,
                "proof_sha256": proof_sha256,
            }
        }
    return AnomalyScan(
        scan_id=scan_id,
        rule_bundle_version=rule_bundle_version,
        scope=scope,
        source_boundary_sha256=_sha(f"boundary:{scan_id}"),
        source_boundary_at=source_boundary_at,
        executed_rules=executed_rules,
        observations=observations,
        evidence=evidence,
    )


def _legacy_sec_boundary_evidence(boundary_at: datetime) -> dict[str, Any]:
    return {
        "evidence_observed_through": boundary_at.isoformat(),
        "temporal_mode": "retrospective_review_no_backfill",
        "ingest_log_timestamp_basis": "legacy_host_local_not_used_for_ordering",
        "source_boundary_basis": "utc_raw_snapshot_received_at",
        "safety": {
            "data_repairs": 0,
            "readiness_overrides": 0,
            "paper_actions": 0,
            "broker_actions": 0,
        },
    }


def _consumed_snapshot_boundary_evidence(
    boundary_at: datetime,
) -> dict[str, Any]:
    count = 2
    return {
        "evidence_observed_through": boundary_at.isoformat(),
        "temporal_mode": "retrospective_review_no_backfill",
        "ingest_log_timestamp_basis": "legacy_host_local_not_used_for_ordering",
        "source_boundary_basis": (
            "max_utc_received_at_of_snapshots_consumed_by_scan"
        ),
        "source_boundary_policy": SEC_SOURCE_BOUNDARY_POLICY_V2,
        "source_boundary_policy_transition": (
            SEC_SOURCE_BOUNDARY_POLICY_V1_TO_V2
        ),
        "source_boundary_proof": {
            "used_snapshot_count": count,
            "used_snapshot_set_sha256": _sha("consumed-snapshot-set"),
            "maximum_received_at": boundary_at.isoformat(),
        },
        "used_snapshot_count": count,
        "safety": {
            "data_repairs": 0,
            "readiness_overrides": 0,
            "paper_actions": 0,
            "broker_actions": 0,
        },
    }


def _record(
    store: AlertStore,
    scan: AnomalyScan,
):
    assert isinstance(scan.source_boundary_at, datetime)
    return store.record_anomaly_scan(scan, now=scan.source_boundary_at)


def _counts(path) -> tuple[int, int, int]:
    with sqlite3.connect(path) as connection:
        return tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "anomaly_scans",
                "anomaly_cases",
                "anomaly_case_events",
            )
        )


def test_v4_migration_does_not_backfill_historical_incidents_into_cases(
    tmp_path,
) -> None:
    path = tmp_path / "operations.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE incidents (
                incident_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL UNIQUE,
                code TEXT NOT NULL,
                severity TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                source_job TEXT NOT NULL,
                state TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                acknowledged_at TEXT,
                resolved_at TEXT,
                notifications_enabled INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO incidents VALUES (
                'inc-historical-sec', 'refresh:sec-zero-rows',
                'sec_fundamental_zero_rows', 'warning',
                'SEC returned no fundamental rows',
                'Thirteen historical ingests had zero rows.',
                'refresh-us-current', 'open',
                '2026-07-01T00:00:00Z', '2026-07-27T00:00:00Z', 13,
                '{"historical_zero_row_ingests":13}', NULL, NULL, 1
            );
            PRAGMA user_version = 4;
            """
        )

    store = AlertStore(path)

    assert store.get("inc-historical-sec").occurrence_count == 13
    assert store.anomaly_cases() == []
    assert _counts(path) == (0, 0, 0)


def test_verification_requires_a_newer_recorded_scan_and_distinct_source_boundary(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "operations.sqlite3")
    with pytest.raises(ValueError, match="later than its ledger record time"):
        store.record_anomaly_scan(
            _scan(
                "scan-future-source",
                observations=(),
                source_boundary_at=BASE_TIME + timedelta(minutes=1),
            ),
            now=BASE_TIME,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        store.record_anomaly_scan(
            _scan("scan-naive-record-time", observations=()),
            now=datetime(2026, 7, 28, 1, 0),
        )

    clean_before_finding = _scan(
        "scan-clean-before-finding",
        observations=(),
        source_boundary_at=BASE_TIME + timedelta(minutes=3),
    )
    store.record_anomaly_scan(
        clean_before_finding,
        now=BASE_TIME + timedelta(minutes=3),
    )
    finding = _scan(
        "scan-finding-after-clean",
        observations=(_observation(),),
        source_boundary_at=BASE_TIME + timedelta(minutes=2),
    )
    case = store.record_anomaly_scan(
        finding,
        now=BASE_TIME + timedelta(minutes=4),
    )[0]
    with pytest.raises(ValueError, match="recorded after the finding"):
        store.resolve_anomaly(
            case.case_id,
            outcome="source_corrected",
            owner="data-ops",
            note="A pre-existing scan cannot prove this correction.",
            expected_evidence_sha256=case.evidence_sha256,
            verification_scan_id=clean_before_finding.scan_id,
            now=BASE_TIME + timedelta(minutes=5),
        )

    older_boundary_clean = _scan(
        "scan-older-source-boundary",
        observations=(),
        source_boundary_at=BASE_TIME + timedelta(minutes=1),
    )
    store.record_anomaly_scan(
        older_boundary_clean,
        now=BASE_TIME + timedelta(minutes=5),
    )
    with pytest.raises(ValueError, match="cannot predate"):
        store.resolve_anomaly(
            case.case_id,
            outcome="source_corrected",
            owner="data-ops",
            note="A later review of older evidence cannot prove a correction.",
            expected_evidence_sha256=case.evidence_sha256,
            verification_scan_id=older_boundary_clean.scan_id,
            now=BASE_TIME + timedelta(minutes=5, seconds=30),
        )

    same_boundary_clean = replace(
        _scan(
            "scan-same-source-boundary",
            observations=(),
            source_boundary_at=BASE_TIME + timedelta(minutes=2),
        ),
        source_boundary_sha256=finding.source_boundary_sha256,
    )
    store.record_anomaly_scan(
        same_boundary_clean,
        now=BASE_TIME + timedelta(minutes=6),
    )
    with pytest.raises(ValueError, match="different source boundary"):
        store.resolve_anomaly(
            case.case_id,
            outcome="source_corrected",
            owner="data-ops",
            note="The identical source boundary cannot prove a correction.",
            expected_evidence_sha256=case.evidence_sha256,
            verification_scan_id=same_boundary_clean.scan_id,
            now=BASE_TIME + timedelta(minutes=7),
        )

    equal_time_clean = _scan(
        "scan-equal-source-boundary",
        observations=(),
        source_boundary_at=BASE_TIME + timedelta(minutes=2),
    )
    store.record_anomaly_scan(
        equal_time_clean,
        now=BASE_TIME + timedelta(minutes=8),
    )
    resolved = store.resolve_anomaly(
        case.case_id,
        outcome="source_corrected",
        owner="data-ops",
        note="A later scan evaluated a changed source boundary at the same time.",
        expected_evidence_sha256=case.evidence_sha256,
        verification_scan_id=equal_time_clean.scan_id,
        now=BASE_TIME + timedelta(minutes=9),
    )
    assert resolved.state == "resolved"


def test_correction_refuses_a_clean_scan_without_source_clearance_proof(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "operations.sqlite3")
    production_rule = "sec_fundamentals_coverage_missing@1.0.0"
    finding = _scan(
        "scan-finding",
        observations=(
            replace(
                _observation(),
                rule_id="sec_fundamentals_coverage_missing",
                rule_version="1.0.0",
            ),
        ),
        source_boundary_at=BASE_TIME,
        executed_rules=(production_rule,),
    )
    case = store.record_anomaly_scan(finding, now=BASE_TIME)[0]
    clean_without_proof = _scan(
        "scan-clean-without-proof",
        observations=(),
        source_boundary_at=BASE_TIME + timedelta(minutes=1),
        executed_rules=(production_rule,),
        include_clearance_proof=False,
    )
    store.record_anomaly_scan(
        clean_without_proof,
        now=BASE_TIME + timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="lacks a source-provenanced clearance proof"):
        store.resolve_anomaly(
            case.case_id,
            outcome="source_corrected",
            owner="data-ops",
            note="An empty scan cannot prove source correction.",
            expected_evidence_sha256=case.evidence_sha256,
            verification_scan_id=clean_without_proof.scan_id,
            now=BASE_TIME + timedelta(minutes=2),
        )


def test_correction_fails_closed_for_a_rule_without_a_clearance_contract(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "operations.sqlite3")
    unregistered = _observation(
        rule_id="unregistered_data_rule",
        rule_version="1.0.0",
    )
    finding = _scan(
        "scan-unregistered-finding",
        observations=(unregistered,),
        executed_rules=("unregistered_data_rule@1.0.0",),
    )
    case = store.record_anomaly_scan(finding, now=BASE_TIME)[0]
    clean = _scan(
        "scan-unregistered-clean",
        observations=(),
        source_boundary_at=BASE_TIME + timedelta(minutes=1),
        executed_rules=("unregistered_data_rule@1.0.0",),
        include_clearance_proof=False,
    )
    store.record_anomaly_scan(clean, now=BASE_TIME + timedelta(minutes=1))

    with pytest.raises(ValueError, match="no registered clearance-proof contract"):
        store.resolve_anomaly(
            case.case_id,
            outcome="source_corrected",
            owner="data-ops",
            note="An unregistered detector cannot self-certify a correction.",
            expected_evidence_sha256=case.evidence_sha256,
            verification_scan_id=clean.scan_id,
            now=BASE_TIME + timedelta(minutes=2),
        )


def test_anomaly_scan_refuses_a_naive_source_boundary(tmp_path) -> None:
    path = tmp_path / "operations.sqlite3"
    store = AlertStore(path)
    scan = _scan(
        "scan-naive-boundary",
        source_boundary_at=datetime(2026, 7, 28, 1, 0),
        observations=(_observation(),),
    )

    with pytest.raises(ValueError, match="must include an explicit timezone"):
        store.record_anomaly_scan(scan)

    assert _counts(path) == (0, 0, 0)


def test_anomaly_scan_orders_fractional_seconds_as_instants(tmp_path) -> None:
    path = tmp_path / "operations.sqlite3"
    store = AlertStore(path)

    with pytest.raises(ValueError, match="later than its ledger record time"):
        store.record_anomaly_scan(
            _scan(
                "scan-fractionally-future",
                source_boundary_at=BASE_TIME + timedelta(microseconds=500),
                observations=(),
            ),
            now=BASE_TIME + timedelta(microseconds=400),
        )

    assert _counts(path) == (0, 0, 0)
    assert store.record_anomaly_scan(
        _scan(
            "scan-fractionally-valid",
            source_boundary_at=BASE_TIME + timedelta(microseconds=400),
            observations=(),
        ),
        now=BASE_TIME + timedelta(microseconds=500),
    ) == ()
    assert _counts(path) == (1, 0, 0)


def test_legacy_sec_boundary_can_upgrade_once_to_consumed_snapshot_policy(
    tmp_path,
) -> None:
    path = tmp_path / "operations.sqlite3"
    store = AlertStore(path)
    policy_scope = "us-equity-reference:sp500"
    legacy_boundary = BASE_TIME + timedelta(minutes=3)
    corrected_boundary = BASE_TIME + timedelta(minutes=1)
    opened = store.record_anomaly_scan(
        _scan(
            "scan-legacy-global-watermark",
            source_boundary_at=legacy_boundary,
            observations=(_observation(marker="legacy", scope=policy_scope),),
            rule_bundle_version="us-equity-data-quality.v1",
            scope=policy_scope,
            extra_evidence=_legacy_sec_boundary_evidence(legacy_boundary),
        ),
        now=legacy_boundary,
    )[0]

    migrated = store.record_anomaly_scan(
        _scan(
            "scan-consumed-snapshot-watermark",
            source_boundary_at=corrected_boundary,
            observations=(_observation(marker="consumed", scope=policy_scope),),
            rule_bundle_version="us-equity-data-quality.v1",
            scope=policy_scope,
            extra_evidence=_consumed_snapshot_boundary_evidence(
                corrected_boundary
            ),
        ),
        now=BASE_TIME + timedelta(minutes=4),
    )[0]

    assert migrated.case_id == opened.case_id
    assert migrated.last_scan_id == "scan-consumed-snapshot-watermark"
    assert migrated.occurrence_count == 2
    assert store.anomaly_case_events(opened.case_id)[0]["scan_id"] == (
        "scan-consumed-snapshot-watermark"
    )
    with sqlite3.connect(path) as connection:
        evidence = json.loads(
            connection.execute(
                """
                SELECT evidence_json
                FROM anomaly_scans
                WHERE scan_id = 'scan-consumed-snapshot-watermark'
                """
            ).fetchone()[0]
        )
    assert evidence["source_boundary_policy"] == SEC_SOURCE_BOUNDARY_POLICY_V2
    assert evidence["source_boundary_policy_transition"] == (
        SEC_SOURCE_BOUNDARY_POLICY_V1_TO_V2
    )


def test_consumed_snapshot_boundary_policy_never_regresses(tmp_path) -> None:
    path = tmp_path / "operations.sqlite3"
    store = AlertStore(path)
    policy_scope = "us-equity-reference:sp500"
    first_boundary = BASE_TIME + timedelta(minutes=2)
    store.record_anomaly_scan(
        _scan(
            "scan-policy-v2-first",
            source_boundary_at=first_boundary,
            observations=(_observation(marker="first-v2", scope=policy_scope),),
            rule_bundle_version="us-equity-data-quality.v1",
            scope=policy_scope,
            extra_evidence=_consumed_snapshot_boundary_evidence(first_boundary),
        ),
        now=first_boundary,
    )

    regressed_boundary = BASE_TIME + timedelta(minutes=1)
    with pytest.raises(
        ValueError,
        match="predates the current case source boundary",
    ):
        store.record_anomaly_scan(
            _scan(
                "scan-policy-v2-regressed",
                source_boundary_at=regressed_boundary,
                observations=(
                    _observation(marker="regressed-v2", scope=policy_scope),
                ),
                rule_bundle_version="us-equity-data-quality.v1",
                scope=policy_scope,
                extra_evidence=_consumed_snapshot_boundary_evidence(
                    regressed_boundary
                ),
            ),
            now=BASE_TIME + timedelta(minutes=3),
        )

    assert _counts(path) == (1, 1, 1)


@pytest.mark.parametrize(
    "legacy_override",
    [
        {"source_boundary_basis": "unrecognized_legacy_watermark"},
        {"used_snapshot_count": 1},
        {
            "safety": {
                "data_repairs": 1,
                "readiness_overrides": 0,
                "paper_actions": 0,
                "broker_actions": 0,
            }
        },
    ],
)
def test_boundary_policy_upgrade_rejects_forged_legacy_contract(
    tmp_path,
    legacy_override,
) -> None:
    path = tmp_path / "operations.sqlite3"
    store = AlertStore(path)
    policy_scope = "us-equity-reference:sp500"
    legacy_boundary = BASE_TIME + timedelta(minutes=3)
    legacy_evidence = _legacy_sec_boundary_evidence(legacy_boundary)
    legacy_evidence.update(legacy_override)
    store.record_anomaly_scan(
        _scan(
            "scan-forged-legacy-contract",
            source_boundary_at=legacy_boundary,
            observations=(_observation(marker="legacy", scope=policy_scope),),
            rule_bundle_version="us-equity-data-quality.v1",
            scope=policy_scope,
            extra_evidence=legacy_evidence,
        ),
        now=legacy_boundary,
    )
    corrected_boundary = BASE_TIME + timedelta(minutes=1)

    with pytest.raises(
        ValueError,
        match="predates the current case source boundary",
    ):
        store.record_anomaly_scan(
            _scan(
                "scan-rejected-policy-upgrade",
                source_boundary_at=corrected_boundary,
                observations=(
                    _observation(marker="rejected", scope=policy_scope),
                ),
                rule_bundle_version="us-equity-data-quality.v1",
                scope=policy_scope,
                extra_evidence=_consumed_snapshot_boundary_evidence(
                    corrected_boundary
                ),
            ),
            now=BASE_TIME + timedelta(minutes=4),
        )

    assert _counts(path) == (1, 1, 1)


def test_versioned_boundary_policy_requires_exact_complete_proof(tmp_path) -> None:
    path = tmp_path / "operations.sqlite3"
    store = AlertStore(path)
    boundary = BASE_TIME
    evidence = _consumed_snapshot_boundary_evidence(boundary)
    evidence["source_boundary_proof"] = {
        **evidence["source_boundary_proof"],
        "maximum_received_at": (boundary - timedelta(minutes=1)).isoformat(),
    }

    with pytest.raises(
        ValueError,
        match="proof does not match the scan boundary",
    ):
        _record(
            store,
            _scan(
                "scan-forged-policy-proof",
                source_boundary_at=boundary,
                observations=(_observation(),),
                rule_bundle_version="us-equity-data-quality.v1",
                extra_evidence=evidence,
            ),
        )

    assert _counts(path) == (0, 0, 0)


def test_correction_refuses_clean_scan_that_omitted_the_case_rule(tmp_path) -> None:
    store = AlertStore(tmp_path / "operations.sqlite3")
    case = _record(
        store,
        _scan("scan-rule-finding", observations=(_observation(),)),
    )[0]
    clean_but_unrelated = _scan(
        "scan-unrelated-clean",
        source_boundary_at=BASE_TIME + timedelta(minutes=1),
        executed_rules=("different_rule@1",),
        observations=(),
    )
    _record(store, clean_but_unrelated)

    with pytest.raises(ValueError, match="did not execute the case rule"):
        store.resolve_anomaly(
            case.case_id,
            outcome="source_corrected",
            owner="data-ops",
            note="This unrelated scan cannot prove the correction.",
            expected_evidence_sha256=case.evidence_sha256,
            verification_scan_id=clean_but_unrelated.scan_id,
            now=BASE_TIME + timedelta(minutes=2),
        )

    assert store.anomaly_case(case.case_id).state == "open"


def test_failed_multi_observation_scan_rolls_back_earlier_new_case(tmp_path) -> None:
    path = tmp_path / "operations.sqlite3"
    store = AlertStore(path)
    existing = _observation(subject_id="issuer-original")
    original_case = _record(
        store,
        _scan("scan-seed", observations=(existing,))
    )[0]
    new_first = _observation(subject_id="issuer-new")
    conflicting_last = replace(existing, subject_id="issuer-different")

    with pytest.raises(ValueError, match="fingerprint is not canonical"):
        _record(
            store,
            _scan(
                "scan-atomic-failure",
                source_boundary_at=BASE_TIME + timedelta(minutes=1),
                observations=(conflicting_last, new_first),
            )
        )

    assert _counts(path) == (1, 1, 1)
    assert store.anomaly_case(original_case.case_id) == original_case


def test_material_change_never_downgrades_case_severity(tmp_path) -> None:
    store = AlertStore(tmp_path / "operations.sqlite3")
    opened = _record(
        store,
        _scan("scan-high", observations=(_observation(severity="high"),))
    )[0]

    changed = _record(
        store,
        _scan(
            "scan-low",
            source_boundary_at=BASE_TIME + timedelta(minutes=1),
            observations=(_observation(marker="changed", severity="low"),),
        )
    )[0]

    assert changed.case_id == opened.case_id
    assert changed.severity == "high"
    assert changed.occurrence_count == 2
    assert store.anomaly_case_events(opened.case_id)[0]["event_type"] == (
        "evidence_changed"
    )


def test_new_evidence_keeps_acknowledged_owner_but_invalidates_stale_review_hash(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "operations.sqlite3")
    opened = _record(
        store,
        _scan("scan-opened", observations=(_observation(),))
    )[0]
    acknowledged = store.acknowledge_anomaly(
        opened.case_id,
        owner="data-ops",
        note="Reviewing the exact source snapshot.",
        expected_evidence_sha256=opened.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=1),
    )

    changed = _record(
        store,
        _scan(
            "scan-changed",
            source_boundary_at=BASE_TIME + timedelta(minutes=2),
            observations=(_observation(marker="changed"),),
        )
    )[0]

    assert changed.state == "acknowledged"
    assert changed.owner == "data-ops"
    assert changed.acknowledged_at == acknowledged.acknowledged_at
    assert changed.evidence_sha256 != acknowledged.evidence_sha256
    assert store.anomaly_case_events(opened.case_id)[0]["event_type"] == (
        "evidence_changed"
    )
    with pytest.raises(ValueError, match="evidence changed after review"):
        store.resolve_anomaly(
            opened.case_id,
            outcome="accepted",
            note="This resolution used stale evidence.",
            expected_evidence_sha256=acknowledged.evidence_sha256,
        )


def test_acknowledgement_and_disposition_require_bounded_owner_and_note(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "operations.sqlite3")
    case = _record(
        store,
        _scan("scan-first", observations=(_observation(),))
    )[0]

    with pytest.raises(ValueError, match="owner is required"):
        store.acknowledge_anomaly(
            case.case_id,
            owner=" ",
            note="Reviewing.",
            expected_evidence_sha256=case.evidence_sha256,
        )
    with pytest.raises(ValueError, match="acknowledgement note is required"):
        store.acknowledge_anomaly(
            case.case_id,
            owner="data-ops",
            note=" ",
            expected_evidence_sha256=case.evidence_sha256,
        )
    with pytest.raises(ValueError, match="resolution note is required"):
        store.resolve_anomaly(
            case.case_id,
            outcome="accepted",
            owner="data-ops",
            note=" ",
            expected_evidence_sha256=case.evidence_sha256,
        )
    with pytest.raises(
        ValueError,
        match="requires a current or explicitly provided owner",
    ):
        store.resolve_anomaly(
            case.case_id,
            outcome="accepted",
            note="Reviewed.",
            expected_evidence_sha256=case.evidence_sha256,
        )

    current = store.anomaly_case(case.case_id)
    assert current.state == "open"
    assert [event["event_type"] for event in store.anomaly_case_events(case.case_id)] == [
        "opened"
    ]


@pytest.mark.parametrize(
    "invalid",
    [
        {"unsupported": {1, 2}},
        {"unsupported": object()},
    ],
)
def test_anomaly_evidence_refuses_non_json_values_atomically(
    tmp_path,
    invalid,
) -> None:
    path = tmp_path / "operations.sqlite3"
    store = AlertStore(path)

    with pytest.raises(ValueError, match="canonical JSON"):
        _record(
            store,
            _scan(
                "scan-invalid-json",
                observations=(_observation(evidence=invalid),),
            )
        )

    assert _counts(path) == (0, 0, 0)


def test_anomaly_comparison_values_must_be_json_objects(tmp_path) -> None:
    path = tmp_path / "operations.sqlite3"
    store = AlertStore(path)
    invalid = replace(
        _observation(),
        old_value=cast(Any, ["not", "an", "object"]),
    )

    with pytest.raises(ValueError, match="old value must be an object"):
        _record(
            store,
            _scan("scan-invalid-old-value", observations=(invalid,))
        )

    assert _counts(path) == (0, 0, 0)


def test_case_projection_tampering_is_refused_before_read_or_mutation(
    tmp_path,
) -> None:
    path = tmp_path / "operations.sqlite3"
    store = AlertStore(path)
    case = _record(
        store,
        _scan("scan-integrity", observations=(_observation(),)),
    )[0]
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE anomaly_cases SET evidence_json = ? WHERE case_id = ?",
            ('{"tampered":true}', case.case_id),
        )

    with pytest.raises(RuntimeError, match="evidence integrity check failed"):
        store.anomaly_case(case.case_id)
    with pytest.raises(RuntimeError, match="evidence integrity check failed"):
        store.acknowledge_anomaly(
            case.case_id,
            owner="data-ops",
            note="This mutation must not trust a stale stored hash.",
            expected_evidence_sha256=case.evidence_sha256,
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT state FROM anomaly_cases WHERE case_id = ?",
            (case.case_id,),
        ).fetchone() == ("open",)
        assert connection.execute(
            "SELECT COUNT(*) FROM anomaly_case_events WHERE case_id = ?",
            (case.case_id,),
        ).fetchone() == (1,)


def test_lifecycle_projection_tampering_cannot_hide_an_open_case(tmp_path) -> None:
    path = tmp_path / "operations.sqlite3"
    store = AlertStore(path)
    case = _record(
        store,
        _scan("scan-hidden-state", observations=(_observation(),)),
    )[0]
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE anomaly_cases
            SET state = 'resolved',
                owner = 'direct-sql',
                disposition = 'accepted',
                resolution_note = 'Projection-only edit.',
                resolution_actor = 'direct-sql',
                resolved_at = '2026-07-28T02:00:00Z'
            WHERE case_id = ?
            """,
            (case.case_id,),
        )

    with pytest.raises(RuntimeError, match="lifecycle projection state mismatch"):
        store.anomaly_cases(unresolved_only=True)
    with pytest.raises(RuntimeError, match="lifecycle projection state mismatch"):
        store.anomaly_summary()
    with pytest.raises(RuntimeError, match="lifecycle projection state mismatch"):
        store.anomaly_case(case.case_id)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            """
            SELECT event_type
            FROM anomaly_case_events
            WHERE case_id = ?
            ORDER BY rowid
            """,
            (case.case_id,),
        ).fetchall() == [("opened",)]


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM anomaly_scans WHERE scan_id = 'scan-history'",
        "UPDATE anomaly_case_events SET payload_json = '{}'",
    ],
)
def test_remaining_history_mutations_are_rejected_by_append_only_triggers(
    tmp_path,
    statement,
) -> None:
    path = tmp_path / "operations.sqlite3"
    store = AlertStore(path)
    _record(
        store,
        _scan("scan-history", observations=(_observation(),))
    )

    with (
        sqlite3.connect(path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
    ):
        connection.execute(statement)

    assert _counts(path) == (1, 1, 1)


def test_case_reference_must_be_exact_or_a_unique_prefix(tmp_path) -> None:
    store = AlertStore(tmp_path / "operations.sqlite3")
    cases = _record(
        store,
        _scan(
            "scan-references",
            observations=(
                _observation(subject_id="issuer-one"),
                _observation(subject_id="issuer-two"),
            ),
        )
    )
    first, second = cases
    unique_prefix = next(
        first.case_id[:length]
        for length in range(1, len(first.case_id) + 1)
        if not second.case_id.startswith(first.case_id[:length])
    )

    assert store.anomaly_case(first.case_id) == first
    assert store.anomaly_case(unique_prefix) == first
    with pytest.raises(ValueError, match="ambiguous"):
        store.anomaly_case("case-")
    with pytest.raises(ValueError, match="unknown"):
        store.anomaly_case("case-does-not-exist")
    with pytest.raises(ValueError, match="reference is required"):
        store.anomaly_case(" ")


def test_summary_counts_every_state_and_distinct_unresolved_subject(tmp_path) -> None:
    store = AlertStore(tmp_path / "operations.sqlite3")
    cases = _record(
        store,
        _scan(
            "scan-summary",
            observations=(
                _observation(
                    subject_id="issuer-shared",
                    severity="critical",
                ),
                _observation(
                    rule_id="sec_fundamentals_mapping_missing",
                    subject_id="issuer-shared",
                    severity="high",
                ),
                _observation(
                    subject_id="issuer-deferred",
                ),
                _observation(
                    subject_id="issuer-resolved",
                    severity="low",
                ),
            ),
            executed_rules=(
                "sec_fundamentals_coverage_missing@1.0.0",
                "sec_fundamentals_mapping_missing@1.0.0",
            ),
        )
    )
    by_subject = {case.subject_id: case for case in cases}
    acknowledged = next(
        case
        for case in cases
        if case.rule_id == "sec_fundamentals_mapping_missing"
    )
    deferred = by_subject["issuer-deferred"]
    resolved = by_subject["issuer-resolved"]
    store.acknowledge_anomaly(
        acknowledged.case_id,
        owner="ack-owner",
        note="Owned for review.",
        expected_evidence_sha256=acknowledged.evidence_sha256,
    )
    store.resolve_anomaly(
        deferred.case_id,
        outcome="deferred",
        owner="defer-owner",
        note="Review after the next governed refresh.",
        next_review_at=datetime.now(UTC) + timedelta(days=1),
        expected_evidence_sha256=deferred.evidence_sha256,
    )
    store.resolve_anomaly(
        resolved.case_id,
        outcome="false_positive",
        owner="resolve-owner",
        note="The exact evidence confirms a false positive.",
        expected_evidence_sha256=resolved.evidence_sha256,
    )

    assert store.anomaly_summary() == {
        "open": 1,
        "acknowledged": 1,
        "deferred": 1,
        "resolved": 1,
        "unresolved": 3,
        "critical_unresolved": 1,
        "high_unresolved": 1,
        "affected_subjects": 2,
        "total": 4,
    }

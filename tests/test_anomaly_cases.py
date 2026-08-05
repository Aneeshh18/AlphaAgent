from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from aios.alerts import (
    MAX_PAYLOAD_BYTES,
    AlertStore,
    AnomalyObservation,
    AnomalyScan,
    canonical_anomaly_fingerprint,
    verify_anomaly_case_evidence,
)

SCOPE = "sec-fundamental-coverage:2026-07-27"
BASE_TIME = datetime(2026, 7, 28, 1, 0, tzinfo=UTC)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _observation(
    *,
    fingerprint: str | None = None,
    subject_id: str = "issuer-fdx",
    severity: str = "medium",
    new_count: int = 0,
    evidence: dict | None = None,
    rule_id: str = "test_fundamentals_coverage_missing",
) -> AnomalyObservation:
    rule_version = "1.0.0"
    return AnomalyObservation(
        fingerprint=fingerprint
        or canonical_anomaly_fingerprint(
            rule_id=rule_id,
            rule_version=rule_version,
            scope=SCOPE,
            subject_type="issuer",
            subject_id=subject_id,
        ),
        rule_id=rule_id,
        rule_version=rule_version,
        scope=SCOPE,
        subject_type="issuer",
        subject_id=subject_id,
        severity=severity,
        confidence="medium",
        title="SEC fundamental coverage is missing",
        summary=f"{subject_id} has no accepted CompanyFacts rows.",
        old_value={"accepted_rows": 1},
        new_value={"accepted_rows": new_count},
        evidence=evidence or {"run_id": f"run-{subject_id}"},
        suggested_checks=(
            "Inspect the immutable CompanyFacts payload.",
            "Confirm the issuer mapping.",
        ),
    )


def _scan(
    scan_id: str,
    *,
    source_boundary_at: datetime = BASE_TIME,
    observations: tuple[AnomalyObservation, ...] | None = None,
    scope: str = SCOPE,
    evidence: dict | None = None,
    rule_id: str = "test_fundamentals_coverage_missing",
) -> AnomalyScan:
    normalized_observations = (
        observations
        if observations is not None
        else (_observation(rule_id=rule_id),)
    )
    normalized_evidence = evidence or {"certified_close": "2026-07-27"}
    if evidence is None and not normalized_observations:
        cleared = _observation(rule_id=rule_id)
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
        normalized_evidence = {
            **normalized_evidence,
            "clearance_proofs": {
                cleared.fingerprint: {
                    **proof_body,
                    "proof_sha256": proof_sha256,
                }
            },
        }
    return AnomalyScan(
        scan_id=scan_id,
        rule_bundle_version="sec-coverage-v1",
        scope=scope,
        source_boundary_sha256=_sha(f"boundary:{scan_id}"),
        source_boundary_at=source_boundary_at,
        executed_rules=(f"{rule_id}@1.0.0",),
        observations=normalized_observations,
        evidence=normalized_evidence,
    )


def _record(
    store: AlertStore,
    scan: AnomalyScan,
):
    assert isinstance(scan.source_boundary_at, datetime)
    return store.record_anomaly_scan(scan, now=scan.source_boundary_at)


def _downgrade_anomaly_events_to_schema_v5(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = OFF;
            BEGIN;
            DROP TRIGGER anomaly_case_events_no_update;
            DROP TRIGGER anomaly_case_events_no_delete;
            DROP TRIGGER anomaly_case_events_sequence_required;
            DROP INDEX anomaly_case_events_case_created_idx;
            DROP INDEX anomaly_case_events_sequence_unique;
            DROP INDEX anomaly_case_scan_observation_unique;
            ALTER TABLE anomaly_case_events RENAME TO anomaly_case_events_v6;
            CREATE TABLE anomaly_case_events (
                event_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL REFERENCES anomaly_cases(case_id),
                scan_id TEXT REFERENCES anomaly_scans(scan_id),
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                actor TEXT,
                note TEXT,
                disposition TEXT,
                observation_sha256 TEXT,
                payload_json TEXT NOT NULL
            );
            INSERT INTO anomaly_case_events (
                event_id, case_id, scan_id, event_type, created_at, actor, note,
                disposition, observation_sha256, payload_json
            )
            SELECT event_id, case_id, scan_id, event_type, created_at, actor, note,
                   disposition, observation_sha256, payload_json
            FROM anomaly_case_events_v6;
            DROP TABLE anomaly_case_events_v6;
            PRAGMA user_version = 5;
            COMMIT;
            PRAGMA foreign_keys = ON;
            """
        )


def test_anomaly_scan_creation_idempotence_and_evidence_change(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    opened = _record(store, _scan("scan-1"))

    assert len(opened) == 1
    case = opened[0]
    assert case.state == "open"
    assert case.occurrence_count == 1
    assert case.new_value == {"accepted_rows": 0}
    assert store.record_anomaly_scan(
        _scan("scan-1"),
        now=BASE_TIME - timedelta(hours=1),
    ) == opened
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT recorded_at FROM anomaly_scans WHERE scan_id = 'scan-1'"
        ).fetchone() == ("2026-07-28T01:00:00Z",)
    assert [event["event_type"] for event in store.anomaly_case_events(case.case_id)] == ["opened"]

    unchanged = _record(
        store,
        _scan("scan-2", source_boundary_at=BASE_TIME + timedelta(minutes=1))
    )[0]
    assert unchanged.case_id == case.case_id
    assert unchanged.occurrence_count == 1
    assert unchanged.last_scan_id == "scan-2"
    assert len(store.anomaly_case_events(case.case_id)) == 1

    changed_observation = _observation(severity="high", new_count=2)
    changed = _record(
        store,
        _scan(
            "scan-3",
            source_boundary_at=BASE_TIME + timedelta(minutes=2),
            observations=(changed_observation,),
        )
    )[0]
    assert changed.case_id == case.case_id
    assert changed.severity == "high"
    assert changed.occurrence_count == 2
    assert changed.new_value == {"accepted_rows": 2}
    assert [event["event_type"] for event in store.anomaly_case_events(case.case_id)] == [
        "evidence_changed",
        "opened",
    ]
    assert store.anomaly_summary() == {
        "open": 1,
        "acknowledged": 0,
        "deferred": 0,
        "resolved": 0,
        "unresolved": 1,
        "critical_unresolved": 0,
        "high_unresolved": 1,
        "affected_subjects": 1,
        "total": 1,
    }


def test_anomaly_scan_id_and_fingerprint_conflicts_are_atomic(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    case = _record(store, _scan("scan-1"))[0]

    with pytest.raises(ValueError, match="conflicts with different payload"):
        _record(store, _scan("scan-1", evidence={"certified_close": "2026-07-26"}))
    with pytest.raises(ValueError, match="fingerprint is not canonical"):
        _record(
            store,
            _scan(
                "scan-2",
                source_boundary_at=BASE_TIME + timedelta(minutes=1),
                observations=(
                    _observation(
                        fingerprint=case.fingerprint,
                        subject_id="issuer-other",
                    ),
                ),
            )
        )

    assert store.anomaly_case(case.case_id).occurrence_count == 1
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM anomaly_scans").fetchone() == (1,)


def test_anomaly_acknowledge_resolve_defer_and_reopen(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    case = _record(store, _scan("scan-1"))[0]

    with pytest.raises(ValueError, match="evidence changed after review"):
        store.acknowledge_anomaly(
            case.case_id,
            owner="research-ops",
            note="This review used stale evidence.",
            expected_evidence_sha256="0" * 64,
        )
    assert store.anomaly_case(case.case_id).state == "open"

    acknowledged = store.acknowledge_anomaly(
        case.case_id[:14],
        owner="research-ops",
        note="Reviewing the exact SEC payload.",
        expected_evidence_sha256=case.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=1),
    )
    assert acknowledged.state == "acknowledged"
    assert acknowledged.owner == "research-ops"

    resolved = store.resolve_anomaly(
        case.case_id,
        outcome="accepted",
        note="The absence is understood and explicitly accepted.",
        expected_evidence_sha256=acknowledged.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=2),
    )
    assert resolved.state == "resolved"
    assert resolved.resolution_outcome == "accepted"
    assert resolved.resolved_at == "2026-07-28T01:02:00Z"
    with pytest.raises(ValueError, match="resolved anomaly case cannot be acknowledged"):
        store.acknowledge_anomaly(
            case.case_id,
            owner="research-ops",
            note="Invalid second acknowledgement.",
            expected_evidence_sha256=resolved.evidence_sha256,
        )

    reopened = _record(
        store,
        _scan(
            "scan-2",
            source_boundary_at=BASE_TIME + timedelta(minutes=3),
            observations=(_observation(new_count=2),),
        )
    )[0]
    assert reopened.state == "open"
    assert reopened.owner is None
    assert reopened.resolution_outcome is None
    assert reopened.resolved_at is None

    with pytest.raises(ValueError, match="requires next_review_at"):
        store.resolve_anomaly(
            case.case_id,
            outcome="deferred",
            owner="research-ops",
            note="Waiting for the next filing.",
            expected_evidence_sha256=reopened.evidence_sha256,
            now=BASE_TIME + timedelta(minutes=4),
        )
    with pytest.raises(ValueError, match="must be in the future"):
        store.resolve_anomaly(
            case.case_id,
            outcome="deferred",
            owner="research-ops",
            note="Waiting for the next filing.",
            next_review_at=BASE_TIME,
            expected_evidence_sha256=reopened.evidence_sha256,
            now=BASE_TIME + timedelta(minutes=4),
        )

    deferred = store.resolve_anomaly(
        case.case_id,
        outcome="deferred",
        owner="research-ops",
        note="Waiting for the next filing.",
        next_review_at=BASE_TIME + timedelta(days=1),
        expected_evidence_sha256=reopened.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=4),
    )
    assert deferred.state == "deferred"
    assert deferred.resolved_at is None
    assert deferred.next_review_at == "2026-07-29T01:00:00Z"
    assert store.anomaly_cases(unresolved_only=True) == [deferred]
    assert [event["event_type"] for event in store.anomaly_case_events(case.case_id)] == [
        "deferred",
        "reopened",
        "resolved",
        "acknowledged",
        "opened",
    ]


def test_lifecycle_mutations_are_exact_retry_idempotent(tmp_path) -> None:
    store = AlertStore(tmp_path / "retry.sqlite3")
    case = _record(store, _scan("scan-retry"))[0]
    acknowledged = store.acknowledge_anomaly(
        case.case_id,
        owner="data-ops",
        note="Reviewing this exact evidence.",
        expected_evidence_sha256=case.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=1),
    )
    acknowledged_retry = store.acknowledge_anomaly(
        case.case_id,
        owner="data-ops",
        note="Reviewing this exact evidence.",
        expected_evidence_sha256=case.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=2),
    )
    assert acknowledged_retry == acknowledged
    assert len(store.anomaly_case_events(case.case_id)) == 2
    with pytest.raises(ValueError, match="different review content"):
        store.acknowledge_anomaly(
            case.case_id,
            owner="data-ops",
            note="A conflicting acknowledgement retry.",
            expected_evidence_sha256=case.evidence_sha256,
            now=BASE_TIME + timedelta(minutes=2),
        )

    resolved = store.resolve_anomaly(
        case.case_id,
        outcome="accepted",
        note="Accepted after reviewing this exact evidence.",
        expected_evidence_sha256=case.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=3),
    )
    resolved_retry = store.resolve_anomaly(
        case.case_id,
        outcome="accepted",
        note="Accepted after reviewing this exact evidence.",
        expected_evidence_sha256=case.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=4),
    )
    assert resolved_retry == resolved
    assert len(store.anomaly_case_events(case.case_id)) == 3
    with pytest.raises(ValueError, match="different disposition content"):
        store.resolve_anomaly(
            case.case_id,
            outcome="accepted",
            note="A conflicting resolution retry.",
            expected_evidence_sha256=case.evidence_sha256,
            now=BASE_TIME + timedelta(minutes=4),
        )


@pytest.mark.parametrize("outcome", ["accepted", "false_positive"])
def test_exact_evidence_suppression_reopens_only_after_material_change(
    tmp_path,
    outcome,
) -> None:
    store = AlertStore(tmp_path / f"{outcome}.sqlite3")
    case = _record(store, _scan("scan-finding"))[0]
    resolved = store.resolve_anomaly(
        case.case_id,
        outcome=outcome,
        owner="data-ops",
        note="This exact evidence was reviewed and dispositioned.",
        expected_evidence_sha256=case.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=1),
    )

    repeated = _record(
        store,
        _scan(
            "scan-same-evidence",
            source_boundary_at=BASE_TIME + timedelta(minutes=2),
        ),
    )[0]
    assert repeated.state == "resolved"
    assert repeated.resolution_outcome == outcome
    assert repeated.occurrence_count == resolved.occurrence_count

    changed = _record(
        store,
        _scan(
            "scan-changed-evidence",
            source_boundary_at=BASE_TIME + timedelta(minutes=3),
            observations=(_observation(new_count=2),),
        ),
    )[0]
    assert changed.state == "open"
    assert changed.resolution_outcome is None
    assert changed.occurrence_count == resolved.occurrence_count + 1
    assert store.anomaly_case_events(case.case_id)[0]["event_type"] == "reopened"


def test_corrected_outcome_requires_later_clean_scan_and_regression_reopens(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    rule_id = "sec_fundamentals_coverage_missing"
    original_scan = _scan("scan-finding", rule_id=rule_id)
    case = _record(store, original_scan)[0]
    case = store.acknowledge_anomaly(
        case.case_id,
        owner="data-steward",
        note="Tracing source lineage.",
        expected_evidence_sha256=case.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="requires a later verification scan"):
        store.resolve_anomaly(
            case.case_id,
            outcome="source_corrected",
            note="Source repaired.",
            expected_evidence_sha256=case.evidence_sha256,
        )

    wrong_scope = "other-scope:2026-07-27"
    _record(
        store,
        _scan(
            "scan-wrong-scope",
            source_boundary_at=BASE_TIME + timedelta(minutes=2),
            observations=(),
            scope=wrong_scope,
            rule_id=rule_id,
        )
    )
    with pytest.raises(ValueError, match="scope does not match"):
        store.resolve_anomaly(
            case.case_id,
            outcome="source_corrected",
            note="Source repaired.",
            expected_evidence_sha256=case.evidence_sha256,
            verification_scan_id="scan-wrong-scope",
        )

    _record(
        store,
        _scan(
            "scan-clean",
            source_boundary_at=BASE_TIME + timedelta(minutes=3),
            observations=(),
            rule_id=rule_id,
        )
    )
    resolved = store.resolve_anomaly(
        case.case_id,
        outcome="source_corrected",
        note="A later complete scan verifies the source repair.",
        expected_evidence_sha256=case.evidence_sha256,
        verification_scan_id="scan-clean",
        now=BASE_TIME + timedelta(minutes=4),
    )
    assert resolved.state == "resolved"
    assert resolved.verification_scan_id == "scan-clean"

    with pytest.raises(ValueError, match="predates the correction verification"):
        stale_source_scan = _scan(
            "scan-before-verification",
            source_boundary_at=BASE_TIME + timedelta(minutes=2),
            observations=original_scan.observations,
            rule_id=rule_id,
        )
        store.record_anomaly_scan(
            stale_source_scan,
            now=BASE_TIME + timedelta(minutes=5),
        )
    assert store.anomaly_case(case.case_id).state == "resolved"

    regression = _record(
        store,
        _scan(
            "scan-regression",
            source_boundary_at=BASE_TIME + timedelta(minutes=5),
            observations=original_scan.observations,
            rule_id=rule_id,
        )
    )[0]
    assert regression.state == "open"
    assert regression.owner is None
    assert regression.resolution_outcome is None
    assert regression.occurrence_count == 2
    assert store.anomaly_case_events(case.case_id)[0]["event_type"] == "reopened"


def test_anomaly_evidence_is_redacted_bounded_and_append_only(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    secret = "must-not-store"
    case = _record(
        store,
        _scan(
            "scan-redacted",
            observations=(
                _observation(
                    evidence={
                        "api_key": secret,
                        "nested": {
                            "Authorization": secret,
                            "safe": "retained",
                        },
                    }
                ),
            ),
            evidence={"session_token": secret, "safe": "retained"},
        )
    )[0]

    assert case.evidence == {
        "api_key": "[REDACTED]",
        "nested": {"Authorization": "[REDACTED]", "safe": "retained"},
    }
    with sqlite3.connect(store.path) as connection:
        stored = "\n".join(
            str(value)
            for row in connection.execute(
                """
                SELECT evidence_json FROM anomaly_scans
                UNION ALL
                SELECT evidence_json FROM anomaly_cases
                UNION ALL
                SELECT payload_json FROM anomaly_case_events
                """
            ).fetchall()
            for value in row
        )
        assert secret not in stored
        assert "[REDACTED]" in stored
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE anomaly_scans SET scope = 'tampered' WHERE scan_id = ?",
                ("scan-redacted",),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM anomaly_case_events WHERE case_id = ?",
                (case.case_id,),
            )

    with pytest.raises(ValueError, match="canonical JSON"):
        _record(
            store,
            _scan(
                "scan-nan",
                evidence={"not_a_number": float("nan")},
                source_boundary_at=BASE_TIME + timedelta(minutes=1),
            )
        )
    with pytest.raises(ValueError, match=f"{MAX_PAYLOAD_BYTES}-byte evidence limit"):
        _record(
            store,
            _scan(
                "scan-oversized",
                evidence={"payload": "x" * MAX_PAYLOAD_BYTES},
                source_boundary_at=BASE_TIME + timedelta(minutes=2),
            )
        )
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM anomaly_scans").fetchone() == (1,)


def test_anomaly_queue_never_paginates_away_an_unresolved_critical_case(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    observations = tuple(
        _observation(
            subject_id=f"issuer-low-{index}",
            severity="low",
        )
        for index in range(100)
    ) + (
        _observation(
            subject_id="issuer-critical",
            severity="critical",
        ),
    )
    cases = _record(
        store,
        _scan("scan-pagination", observations=observations),
    )
    critical = next(case for case in cases if case.severity == "critical")
    store.acknowledge_anomaly(
        critical.case_id,
        owner="data-ops",
        note="Critical evidence is under active review.",
        expected_evidence_sha256=critical.evidence_sha256,
        now=BASE_TIME + timedelta(seconds=1),
    )

    page = store.anomaly_cases(unresolved_only=True, limit=100)

    assert len(page) == 100
    assert page[0].case_id == critical.case_id
    assert page[0].state == "acknowledged"
    assert page[0].severity == "critical"


def test_schema_v4_migration_preserves_existing_incident_and_adds_case_ledger(
    tmp_path,
) -> None:
    path = tmp_path / "alerts.sqlite3"
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
                'inc-existing', 'refresh:partial', 'refresh_partial', 'warning',
                'Refresh warning', 'Three issuers need review.', 'refresh-us-current',
                'open', '2026-07-28T00:00:00Z', '2026-07-28T00:00:00Z', 1,
                '{"failed":3}', NULL, NULL, 1
            );
            PRAGMA user_version = 4;
            """
        )

    store = AlertStore(path)

    assert store.get("inc-existing").payload == {"failed": 3}
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (7,)
        assert {
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name LIKE 'anomaly_%'
                """
            ).fetchall()
        } == {"anomaly_scans", "anomaly_cases", "anomaly_case_events"}
        scan_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(anomaly_scans)"
            ).fetchall()
        }
        assert {
            "source_boundary_at",
            "recorded_at",
            "recorded_sequence",
        } <= scan_columns
        event_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(anomaly_case_events)"
            ).fetchall()
        }
        assert "event_sequence" in event_columns
        assert "completed_at" not in scan_columns
        assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone() == (1,)


def test_existing_schema_v5_without_event_sequence_remains_readable(tmp_path) -> None:
    path = tmp_path / "existing-v5.sqlite3"
    original_store = AlertStore(path)
    original = _record(original_store, _scan("scan-existing-v5"))[0]
    _downgrade_anomaly_events_to_schema_v5(path)

    reopened_store = AlertStore(path)

    assert reopened_store.anomaly_case(original.case_id) == original
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (7,)
        assert connection.execute(
            "SELECT event_sequence FROM anomaly_case_events"
        ).fetchall() == [(1,)]


def test_schema_v5_migration_refuses_a_preserved_noncanonical_case(tmp_path) -> None:
    path = tmp_path / "noncanonical-v5.sqlite3"
    store = AlertStore(path)
    case = _record(store, _scan("scan-noncanonical-v5"))[0]
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE anomaly_cases SET fingerprint = ? WHERE case_id = ?",
            ("dqf-" + "0" * 64, case.case_id),
        )
    _downgrade_anomaly_events_to_schema_v5(path)

    with pytest.raises(
        RuntimeError,
        match="fingerprint integrity check failed.*not canonical",
    ):
        AlertStore(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (5,)
        event_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(anomaly_case_events)"
            ).fetchall()
        }
        assert "event_sequence" not in event_columns


def test_anomaly_read_refuses_a_noncanonical_current_case(tmp_path) -> None:
    store = AlertStore(tmp_path / "noncanonical-current.sqlite3")
    case = _record(store, _scan("scan-noncanonical-current"))[0]
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE anomaly_cases SET fingerprint = ? WHERE case_id = ?",
            ("dqf-" + "f" * 64, case.case_id),
        )

    with pytest.raises(
        RuntimeError,
        match="fingerprint integrity check failed.*not canonical",
    ):
        store.anomaly_case(case.case_id)
    with pytest.raises(
        RuntimeError,
        match="fingerprint integrity check failed.*not canonical",
    ):
        store.anomaly_cases(unresolved_only=True)


@pytest.mark.parametrize(
    ("assignment", "parameters"),
    [
        ("first_seen_at = ?", ("2026-07-27T00:00:00Z",)),
        ("last_seen_at = ?", ("2026-07-29T00:00:00Z",)),
        ("occurrence_count = occurrence_count + 1", ()),
    ],
)
def test_anomaly_reads_reject_forged_projection_counters_and_times(
    tmp_path,
    assignment,
    parameters,
) -> None:
    store = AlertStore(tmp_path / "forged-times.sqlite3")
    case = _record(store, _scan("scan-forged-times"))[0]
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            f"UPDATE anomaly_cases SET {assignment} WHERE case_id = ?",
            (*parameters, case.case_id),
        )

    with pytest.raises(RuntimeError, match="lifecycle integrity check failed"):
        store.anomaly_case(case.case_id)
    with pytest.raises(RuntimeError, match="lifecycle integrity check failed"):
        store.anomaly_cases(unresolved_only=True)
    with pytest.raises(RuntimeError, match="lifecycle integrity check failed"):
        store.anomaly_summary()


def test_anomaly_read_rejects_forged_acknowledgement_without_event(tmp_path) -> None:
    store = AlertStore(tmp_path / "forged-ack.sqlite3")
    case = _record(store, _scan("scan-forged-ack"))[0]
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE anomaly_cases
            SET state = 'acknowledged', owner = 'attacker',
                acknowledged_at = '2026-07-28T01:01:00Z'
            WHERE case_id = ?
            """,
            (case.case_id,),
        )

    with pytest.raises(
        RuntimeError,
        match="state projection mismatch",
    ):
        store.anomaly_case(case.case_id)
    with pytest.raises(
        RuntimeError,
        match="state projection mismatch",
    ):
        store.anomaly_case_events(case.case_id)


def test_anomaly_read_rejects_forged_acknowledgement_owner(tmp_path) -> None:
    store = AlertStore(tmp_path / "forged-owner.sqlite3")
    case = _record(store, _scan("scan-forged-owner"))[0]
    acknowledged = store.acknowledge_anomaly(
        case.case_id,
        owner="data-ops",
        note="Reviewing the immutable observation.",
        expected_evidence_sha256=case.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=1),
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE anomaly_cases SET owner = 'attacker' WHERE case_id = ?",
            (acknowledged.case_id,),
        )

    with pytest.raises(
        RuntimeError,
        match="owner projection mismatch",
    ):
        store.anomaly_case(acknowledged.case_id)


def test_backup_verifier_rejects_forged_resolution_projection(tmp_path) -> None:
    store = AlertStore(tmp_path / "forged-resolution.sqlite3")
    case = _record(store, _scan("scan-forged-resolution"))[0]
    resolved = store.resolve_anomaly(
        case.case_id,
        outcome="accepted",
        owner="data-ops",
        note="Accepted after reviewing the immutable observation.",
        expected_evidence_sha256=case.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=1),
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE anomaly_cases
            SET disposition = 'false_positive',
                resolution_note = 'Forged disposition.',
                resolved_at = '2026-07-28T01:02:00Z'
            WHERE case_id = ?
            """,
            (resolved.case_id,),
        )
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM anomaly_cases WHERE case_id = ?",
            (resolved.case_id,),
        ).fetchone()
        assert row is not None
        with pytest.raises(
            RuntimeError,
            match="disposition projection mismatch",
        ):
            verify_anomaly_case_evidence(connection, row)


def test_read_only_anomaly_views_are_repeated_stable_zero_write_reads(
    tmp_path,
) -> None:
    path = tmp_path / "read-only-anomalies.sqlite3"
    writable = AlertStore(path)
    case = _record(writable, _scan("scan-read-only-stability"))[0]
    with sqlite3.connect(path) as checkpoint:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        checkpoint.execute("PRAGMA journal_mode = DELETE")
    before = {
        candidate.name: candidate.read_bytes()
        for candidate in tmp_path.iterdir()
        if candidate.is_file()
    }
    read_only = AlertStore(path, read_only=True)

    for _ in range(3):
        assert read_only.anomaly_cases(unresolved_only=True) == [case]
        assert read_only.anomaly_case(case.case_id) == case
        assert [
            event["event_type"]
            for event in read_only.anomaly_case_events(case.case_id)
        ] == ["opened"]
        assert read_only.anomaly_summary()["unresolved"] == 1

    assert {
        candidate.name: candidate.read_bytes()
        for candidate in tmp_path.iterdir()
        if candidate.is_file()
    } == before

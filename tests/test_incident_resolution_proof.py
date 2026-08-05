from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from typer.testing import CliRunner

import aios.alerts as alerts_module
import aios.cli as cli
from aios.alerts import (
    Alert,
    AlertSeverity,
    AlertStore,
    ProducerRecoveryEvidence,
    build_daily_cycle_recovery_evidence,
    build_scheduler_recovery_evidence,
)
from aios.scheduler import TIMER_NAMES

BASE_TIME = datetime(2026, 7, 31, 2, 0, tzinfo=UTC)
DAILY_ASSESSED_AT = BASE_TIME + timedelta(minutes=2, seconds=30)


def _alert(
    *,
    fingerprint: str = "scheduler:runtime-unverified",
    severity: AlertSeverity = AlertSeverity.WARNING,
) -> Alert:
    return Alert(
        code="scheduler_runtime_unverified",
        severity=severity,
        title="Scheduler runtime needs proof",
        body="The systemd user manager did not provide a complete live response.",
        dedup_key=fingerprint,
        source_job="aios scheduler-status",
        payload={"timers": list(TIMER_NAMES)},
    )


def _daily_alert() -> Alert:
    return Alert(
        code="daily_us_cycle_failed",
        severity=AlertSeverity.CRITICAL,
        title="Daily U.S. update did not complete",
        body="The exact completed U.S. session was not certified.",
        dedup_key="daily:us-cycle:failure",
        source_job="aios refresh-us-daily",
        payload={"error_type": "USDailyCycleError"},
    )


def _live_status() -> dict[str, dict[str, object]]:
    return {
        timer: {
            "enabled": True,
            "active": True,
            "last_trigger": "Wed 2026-07-29 13:00:00 UTC",
            "last_run": "Wed 2026-07-29 13:05:00 UTC",
            "next_trigger": "Thu 2026-07-30 13:00:00 UTC",
            "service_result": "success",
            "exit_status": "0",
            "runtime_verified": True,
        }
        for timer in TIMER_NAMES
    }


def _successful_daily_job(store: AlertStore):
    started = store.begin_job(
        "us-daily-refresh",
        "2026-07-30",
        now=BASE_TIME + timedelta(minutes=1),
        owner_pid=999_999_999,
        owner_boot_id="test-boot",
    )
    payload = {
        "benchmark_rows": 4,
        "certified_research_through": "2026-07-30",
        "interrupted_run_ids": [],
        "macro_rows": 98_462,
        "member_count": 503,
        "member_price_rows": 3_018,
        "run_id": started.run.run_id,
        "status": "completed",
        "target_session": "2026-07-30",
        "universe_coverage_through": "2026-07-30",
        "universe_status": "up_to_date",
        "warning_count": 0,
    }
    return store.finish_job(
        started.run.run_id,
        state="success",
        detail="The exact completed U.S. session is certified.",
        payload=payload,
        now=BASE_TIME + timedelta(minutes=2),
    )


def _ready_daily_report() -> dict[str, object]:
    check_names = (
        "decision_date",
        "data_integrity",
        "universe_membership",
        "stable_security_identity",
        "fundamental_coverage",
        "price_history_coverage",
        "reviewed_price_freshness",
        "benchmark_freshness",
        "macro_pit_readiness",
    )
    return {
        "as_of": "2026-07-30",
        "purpose": "paper",
        "generated_on": "2026-07-30",
        "universe_id": "sp500",
        "benchmark_ticker": "SPY",
        "certified_research_from": "2023-08-01",
        "certified_research_through": "2026-07-30",
        "raw_prices_through": "2026-07-30",
        "fundamentals_through": "2026-03-31",
        "macro_releases_through": "2026-07-30",
        "checks": [
            {
                "check": check_name,
                "label": check_name.replace("_", " ").title(),
                "status": "pass",
                "observed": "verified",
                "required": "pass or warning without a hard failure",
                "detail": "The exact governed gate passed.",
            }
            for check_name in check_names
        ],
        "ready": True,
    }


def test_daily_cycle_producer_proof_is_bound_to_exact_successful_job(tmp_path) -> None:
    store = AlertStore(tmp_path / "daily-alerts.sqlite3")
    opened = store.emit(
        _daily_alert(),
        now=BASE_TIME,
    )
    context = store.recovery_context(opened.fingerprint)
    assert context is not None
    job = _successful_daily_job(store)
    recovery = build_daily_cycle_recovery_evidence(
        context,
        job,
        _ready_daily_report(),
        assessed_at=DAILY_ASSESSED_AT,
    )

    resolved = store.resolve_fingerprint(
        opened.fingerprint,
        recovery=recovery,
        now=BASE_TIME + timedelta(minutes=3),
    )

    assert resolved is not None
    assert resolved.state == "resolved"
    assert resolved.resolution_proof_status == "producer_verified_recovery"
    assert resolved.operationally_blocking is False
    event = store.events(opened.incident_id)[0]
    assert event["proof_kind"] == "daily_cycle_certified_v3"
    proof = event["payload"]["_aios_incident_recovery_proof_v1"]
    assert proof["observation"]["job"]["run_id"] == job.run_id
    assert proof["observation"]["readiness"]["report"]["ready"] is True


def test_daily_cycle_producer_proof_rejects_failed_or_changed_job(tmp_path) -> None:
    store = AlertStore(tmp_path / "daily-invalid.sqlite3")
    opened = store.emit(
        _daily_alert(),
        now=BASE_TIME,
    )
    context = store.recovery_context(opened.fingerprint)
    assert context is not None
    failed = store.begin_job(
        "us-daily-refresh",
        "2026-07-30",
        now=BASE_TIME + timedelta(minutes=1),
        owner_pid=999_999_999,
        owner_boot_id="test-boot",
    )
    failed_job = store.finish_job(
        failed.run.run_id,
        state="failed",
        detail="Provider failed.",
        payload={"error_type": "RuntimeError"},
        now=BASE_TIME + timedelta(minutes=2),
    )
    with pytest.raises(ValueError, match="successful daily job"):
        build_daily_cycle_recovery_evidence(
            context,
            failed_job,
            _ready_daily_report(),
            assessed_at=DAILY_ASSESSED_AT,
        )

    valid = _successful_daily_job(store)
    recovery = build_daily_cycle_recovery_evidence(
        context,
        valid,
        _ready_daily_report(),
        assessed_at=DAILY_ASSESSED_AT,
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE job_runs SET payload_json = '{}' WHERE run_id = ?",
            (valid.run_id,),
        )
    with pytest.raises(ValueError, match="job evidence changed"):
        store.resolve_fingerprint(
            opened.fingerprint,
            recovery=recovery,
            now=BASE_TIME + timedelta(minutes=3),
        )


def test_daily_cycle_producer_proof_rejects_nonready_or_mismatched_report(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "daily-readiness-invalid.sqlite3")
    opened = store.emit(
        _daily_alert(),
        now=BASE_TIME,
    )
    context = store.recovery_context(opened.fingerprint)
    assert context is not None
    job = _successful_daily_job(store)

    nonready = _ready_daily_report()
    nonready["ready"] = False
    with pytest.raises(ValueError, match="not fully ready"):
        build_daily_cycle_recovery_evidence(
            context,
            job,
            nonready,
            assessed_at=DAILY_ASSESSED_AT,
        )

    stale = _ready_daily_report()
    stale["certified_research_through"] = "2026-07-29"
    with pytest.raises(ValueError, match="does not certify its exact target"):
        build_daily_cycle_recovery_evidence(
            context,
            job,
            stale,
            assessed_at=DAILY_ASSESSED_AT,
        )

    incomplete = _ready_daily_report()
    incomplete["checks"] = incomplete["checks"][:-1]  # type: ignore[index]
    with pytest.raises(ValueError, match="checks are incomplete"):
        build_daily_cycle_recovery_evidence(
            context,
            job,
            incomplete,
            assessed_at=DAILY_ASSESSED_AT,
        )


def test_daily_cycle_proof_rejects_wrong_incident_domain(tmp_path) -> None:
    store = AlertStore(tmp_path / "daily-wrong-domain.sqlite3")
    opened = store.emit(
        _alert(fingerprint="daily:us-cycle:failure"),
        now=BASE_TIME,
    )
    context = store.recovery_context(opened.fingerprint)
    assert context is not None
    recovery = build_daily_cycle_recovery_evidence(
        context,
        _successful_daily_job(store),
        _ready_daily_report(),
        assessed_at=DAILY_ASSESSED_AT,
    )

    with pytest.raises(ValueError, match="incident domain"):
        store.resolve_fingerprint(
            opened.fingerprint,
            recovery=recovery,
            now=BASE_TIME + timedelta(minutes=3),
        )


def test_daily_cycle_v1_is_historical_only_but_still_verifies(
    tmp_path,
    monkeypatch,
) -> None:
    store = AlertStore(tmp_path / "daily-v1.sqlite3")
    opened = store.emit(_daily_alert(), now=BASE_TIME)
    context = store.recovery_context(opened.fingerprint)
    assert context is not None
    job = _successful_daily_job(store)
    legacy = ProducerRecoveryEvidence(
        incident_id=context.incident_id,
        fingerprint=context.fingerprint,
        generation_event_id=context.generation_event_id,
        expected_evidence_sha256=context.evidence_sha256,
        producer="aios refresh-us-daily",
        proof_kind="daily_cycle_certified",
        observed_at=job.finished_at or "",
        observation=alerts_module._daily_cycle_recovery_observation(job),
    )

    with pytest.raises(ValueError, match="unsupported producer recovery proof kind"):
        store.resolve_fingerprint(
            opened.fingerprint,
            recovery=legacy,
            now=BASE_TIME + timedelta(minutes=3),
        )

    monkeypatch.setattr(
        alerts_module,
        "PRODUCER_RECOVERY_PROOF_KINDS",
        {*alerts_module.PRODUCER_RECOVERY_PROOF_KINDS, "daily_cycle_certified"},
    )
    resolved = store.resolve_fingerprint(
        opened.fingerprint,
        recovery=legacy,
        now=BASE_TIME + timedelta(minutes=3),
    )
    monkeypatch.undo()

    assert resolved is not None
    historical = AlertStore(store.path).get(opened.incident_id)
    assert historical.resolution_proof_status == "producer_verified_recovery"
    assert historical.operationally_blocking is False


def test_daily_cycle_proof_rejects_pre_generation_job_and_changed_hash(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "daily-causality.sqlite3")
    old_job = _successful_daily_job(store)
    opened = store.emit(_daily_alert(), now=BASE_TIME + timedelta(minutes=3))
    context = store.recovery_context(opened.fingerprint)
    assert context is not None
    recovery = build_daily_cycle_recovery_evidence(
        context,
        old_job,
        _ready_daily_report(),
        assessed_at=BASE_TIME + timedelta(minutes=4),
    )
    with pytest.raises(ValueError, match="predates the current incident generation"):
        store.resolve_fingerprint(
            opened.fingerprint,
            recovery=recovery,
            now=BASE_TIME + timedelta(minutes=5),
        )

    store = AlertStore(tmp_path / "daily-hash.sqlite3")
    opened = store.emit(_daily_alert(), now=BASE_TIME)
    context = store.recovery_context(opened.fingerprint)
    assert context is not None
    recovery = build_daily_cycle_recovery_evidence(
        context,
        _successful_daily_job(store),
        _ready_daily_report(),
        assessed_at=DAILY_ASSESSED_AT,
    )
    tampered_observation = deepcopy(recovery.observation)
    tampered_observation["job"]["payload_sha256"] = "0" * 64
    tampered = replace(recovery, observation=tampered_observation)
    with pytest.raises(ValueError, match="payload hash is invalid"):
        store.resolve_fingerprint(
            opened.fingerprint,
            recovery=tampered,
            now=BASE_TIME + timedelta(minutes=3),
        )


def test_daily_cycle_proof_requires_latest_job_receipt(tmp_path) -> None:
    store = AlertStore(tmp_path / "daily-latest.sqlite3")
    opened = store.emit(_daily_alert(), now=BASE_TIME)
    context = store.recovery_context(opened.fingerprint)
    assert context is not None
    recovery = build_daily_cycle_recovery_evidence(
        context,
        _successful_daily_job(store),
        _ready_daily_report(),
        assessed_at=DAILY_ASSESSED_AT,
    )
    store.begin_job(
        "us-daily-refresh",
        "2026-07-30",
        now=BASE_TIME + timedelta(minutes=2, seconds=40),
        owner_pid=999_999_998,
        owner_boot_id="test-boot",
    )

    with pytest.raises(ValueError, match="no longer the latest receipt"):
        store.resolve_fingerprint(
            opened.fingerprint,
            recovery=recovery,
            now=BASE_TIME + timedelta(minutes=3),
        )


def _legacy_resolve(
    store: AlertStore,
    incident_id: str,
    *,
    at: datetime,
) -> None:
    timestamp = at.isoformat(timespec="seconds").replace("+00:00", "Z")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "DROP TRIGGER incident_events_resolution_proof_required"
        )
        connection.execute(
            """
            UPDATE incidents
            SET state = 'resolved', resolved_at = ?
            WHERE incident_id = ?
            """,
            (timestamp, incident_id),
        )
        connection.execute(
            """
            INSERT INTO incident_events (
                event_id, incident_id, event_type, created_at, payload_json
            ) VALUES (?, ?, 'resolved', ?, '{}')
            """,
            (f"evt-legacy-{uuid4().hex}", incident_id, timestamp),
        )
    AlertStore(store.path)


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        ("verified_recovery", "manual_verified_recovery"),
        ("false_positive", "manual_false_positive"),
    ],
)
def test_manual_resolution_proof_is_non_blocking(
    tmp_path,
    outcome: str,
    expected_status: str,
) -> None:
    store = AlertStore(tmp_path / f"{outcome}.sqlite3")
    opened = store.emit(_alert(), now=BASE_TIME)

    resolved = store.resolve(
        opened.incident_id,
        actor="ops@example.test",
        note="Reviewed the exact current generation.",
        outcome=outcome,
        expected_evidence_sha256=opened.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=1),
    )

    current = store.get(opened.incident_id)
    assert resolved.resolution_proof_status == expected_status
    assert current.resolution_proof_status == expected_status
    assert current.operationally_blocking is False
    assert store.incident_summary()["operational_blocking"] == 0


def test_scheduler_producer_proof_is_typed_generation_bound_and_strict(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    opened = store.emit(_alert(), now=BASE_TIME)
    context = store.recovery_context(opened.fingerprint)
    assert context is not None
    observed_at = BASE_TIME + timedelta(minutes=1)
    recovery = build_scheduler_recovery_evidence(
        context,
        _live_status(),
        observed_at=observed_at,
    )

    resolved = store.resolve_fingerprint(
        opened.fingerprint,
        recovery=recovery,
        now=BASE_TIME + timedelta(minutes=2),
    )

    assert resolved is not None
    assert resolved.resolution_proof_status == "producer_verified_recovery"
    assert resolved.operationally_blocking is False
    event = store.events(opened.incident_id)[0]
    assert event["proof_kind"] == "scheduler_runtime_verified"
    assert event["transitioned_state"] is True
    assert len(event["proof_sha256"]) == 64

    with pytest.raises(TypeError, match="recovery"):
        store.resolve_fingerprint(opened.fingerprint)  # type: ignore[call-arg]


def test_scheduler_proof_rejects_file_only_missing_and_wrong_domain_evidence(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    opened = store.emit(_alert(), now=BASE_TIME)
    context = store.recovery_context(opened.fingerprint)
    assert context is not None
    file_only = _live_status()
    file_only[TIMER_NAMES[0]]["runtime_verified"] = False
    with pytest.raises(ValueError, match="live runtime verification"):
        build_scheduler_recovery_evidence(context, file_only)
    missing = _live_status()
    missing.pop(TIMER_NAMES[0])
    with pytest.raises(ValueError, match="missing managed timers"):
        build_scheduler_recovery_evidence(context, missing)

    valid = build_scheduler_recovery_evidence(
        context,
        _live_status(),
        observed_at=BASE_TIME + timedelta(minutes=1),
    )
    wrong_domain = replace(valid, fingerprint="readiness:paper")
    with pytest.raises(ValueError, match="fingerprint"):
        store.resolve_fingerprint(
            opened.fingerprint,
            recovery=wrong_domain,
            now=BASE_TIME + timedelta(minutes=2),
        )
    before_generation = replace(valid, observed_at=BASE_TIME - timedelta(seconds=1))
    with pytest.raises(ValueError, match="predates"):
        store.resolve_fingerprint(
            opened.fingerprint,
            recovery=before_generation,
            now=BASE_TIME + timedelta(minutes=2),
        )
    assert store.get(opened.incident_id).state == "open"
    assert len(store.events(opened.incident_id)) == 1


def test_scheduler_proof_refuses_changed_evidence_and_stale_generation(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    opened = store.emit(_alert(), now=BASE_TIME)
    context = store.recovery_context(opened.fingerprint)
    assert context is not None
    stale = build_scheduler_recovery_evidence(
        context,
        _live_status(),
        observed_at=BASE_TIME + timedelta(minutes=1),
    )
    store.emit(_alert(), now=BASE_TIME + timedelta(minutes=2))
    with pytest.raises(ValueError, match="evidence changed"):
        store.resolve_fingerprint(
            opened.fingerprint,
            recovery=stale,
            now=BASE_TIME + timedelta(minutes=3),
        )

    current = store.get(opened.incident_id)
    store.resolve(
        opened.incident_id,
        actor="ops@example.test",
        note="Manual resolution before a later generation.",
        outcome="verified_recovery",
        expected_evidence_sha256=current.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=4),
    )
    store.emit(_alert(), now=BASE_TIME + timedelta(minutes=5))
    with pytest.raises(ValueError, match="generation is stale"):
        store.resolve_fingerprint(
            opened.fingerprint,
            recovery=stale,
            now=BASE_TIME + timedelta(minutes=6),
        )
    reopened = store.get(opened.incident_id)
    assert reopened.state == "open"
    assert reopened.operationally_blocking is True


def test_later_attestation_upgrades_legacy_resolution_without_projection_rewrite(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    opened = store.emit(_alert(), now=BASE_TIME)
    _legacy_resolve(
        store,
        opened.incident_id,
        at=BASE_TIME + timedelta(minutes=1),
    )
    store = AlertStore(store.path)
    legacy = store.get(opened.incident_id)
    assert legacy.resolution_proof_status == "legacy_unproven"
    assert legacy.operationally_blocking is True
    context = store.recovery_context(opened.fingerprint)
    assert context is not None
    recovery = build_scheduler_recovery_evidence(
        context,
        _live_status(),
        observed_at=BASE_TIME + timedelta(minutes=2),
    )
    with sqlite3.connect(store.path) as connection:
        row_before = connection.execute(
            "SELECT * FROM incidents WHERE incident_id = ?",
            (opened.incident_id,),
        ).fetchone()
        notifications_before = connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0]
        events_before = connection.execute(
            "SELECT COUNT(*) FROM incident_events WHERE incident_id = ?",
            (opened.incident_id,),
        ).fetchone()[0]

    attested = store.resolve_fingerprint(
        opened.fingerprint,
        recovery=recovery,
        now=BASE_TIME + timedelta(minutes=3),
    )

    assert attested is not None
    assert attested.resolution_proof_status == "producer_verified_recovery"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT * FROM incidents WHERE incident_id = ?",
            (opened.incident_id,),
        ).fetchone() == row_before
        assert connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0] == notifications_before
        assert connection.execute(
            "SELECT COUNT(*) FROM incident_events WHERE incident_id = ?",
            (opened.incident_id,),
        ).fetchone()[0] == events_before + 1
    assert store.events(opened.incident_id)[0]["transitioned_state"] is False

    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER incident_events_no_update")
        event = connection.execute(
            """
            SELECT event_id, payload_json
            FROM incident_events
            WHERE incident_id = ? AND event_type = 'resolved'
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (opened.incident_id,),
        ).fetchone()
        payload = json.loads(event[1])
        proof = payload["_aios_incident_recovery_proof_v1"]
        proof["observed_at"] = (
            BASE_TIME + timedelta(minutes=1)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        proof.pop("proof_sha256")
        canonical = json.dumps(
            proof,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        proof["proof_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
        connection.execute(
            "UPDATE incident_events SET payload_json = ? WHERE event_id = ?",
            (
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                event[0],
            ),
        )
    current = store.get(opened.incident_id)
    assert current.resolution_proof_status == "invalid"
    assert current.operationally_blocking is True


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    (
        ("verified_recovery", "manual_verified_recovery"),
        ("false_positive", "manual_false_positive"),
    ),
)
def test_manual_attestation_upgrades_only_legacy_resolution_without_side_effects(
    tmp_path,
    outcome,
    expected_status,
) -> None:
    store = AlertStore(tmp_path / f"manual-{outcome}.sqlite3")
    opened = store.emit(_alert(), now=BASE_TIME)
    _legacy_resolve(
        store,
        opened.incident_id,
        at=BASE_TIME + timedelta(minutes=1),
    )
    store = AlertStore(store.path)
    legacy = store.get(opened.incident_id)
    assert legacy.resolution_proof_status == "legacy_unproven"
    with sqlite3.connect(store.path) as connection:
        projection_before = connection.execute(
            "SELECT * FROM incidents WHERE incident_id = ?",
            (opened.incident_id,),
        ).fetchone()
        notifications_before = connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0]
        events_before = connection.execute(
            "SELECT COUNT(*) FROM incident_events WHERE incident_id = ?",
            (opened.incident_id,),
        ).fetchone()[0]

    attested = store.attest_legacy_resolution(
        opened.incident_id,
        actor="ops@example.test",
        note="Reviewed the historical resolution against its exact evidence.",
        outcome=outcome,
        expected_evidence_sha256=legacy.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=2),
    )

    assert attested.state == "resolved"
    assert attested.resolution_proof_status == expected_status
    assert attested.operationally_blocking is False
    assert attested.occurrence_count == legacy.occurrence_count
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT * FROM incidents WHERE incident_id = ?",
            (opened.incident_id,),
        ).fetchone() == projection_before
        assert connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0] == notifications_before
        assert connection.execute(
            "SELECT COUNT(*) FROM incident_events WHERE incident_id = ?",
            (opened.incident_id,),
        ).fetchone()[0] == events_before + 1
    event = store.events(opened.incident_id)[0]
    assert event["event_type"] == "resolved"
    assert event["actor"] == "ops@example.test"
    assert event["resolution_outcome"] == outcome
    assert event["transitioned_state"] is False
    assert event["expected_evidence_sha256"] == legacy.evidence_sha256
    assert event["resulting_evidence_sha256"] == legacy.evidence_sha256


def test_manual_attestation_refuses_open_and_already_proven_incidents(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "state-refusals.sqlite3")
    opened = store.emit(_alert(), now=BASE_TIME)
    with pytest.raises(ValueError, match="only to an already-resolved incident"):
        store.attest_legacy_resolution(
            opened.incident_id,
            actor="ops@example.test",
            note="This must not close an open incident.",
            outcome="verified_recovery",
            expected_evidence_sha256=opened.evidence_sha256,
            now=BASE_TIME + timedelta(minutes=1),
        )
    assert store.get(opened.incident_id).state == "open"
    assert len(store.events(opened.incident_id)) == 1

    proven = store.resolve(
        opened.incident_id,
        actor="ops@example.test",
        note="Normal exact-generation resolution.",
        outcome="verified_recovery",
        expected_evidence_sha256=opened.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=2),
    )
    event_count = len(store.events(opened.incident_id))
    with pytest.raises(ValueError, match="already has a valid proof"):
        store.attest_legacy_resolution(
            opened.incident_id,
            actor="ops@example.test",
            note="A retry must not append a second proof.",
            outcome="verified_recovery",
            expected_evidence_sha256=proven.evidence_sha256,
            now=BASE_TIME + timedelta(minutes=3),
        )
    assert len(store.events(opened.incident_id)) == event_count


def test_manual_attestation_refuses_invalid_resolution_for_forensics(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "invalid.sqlite3")
    opened = store.emit(_alert(), now=BASE_TIME)
    resolved_at = (BASE_TIME + timedelta(minutes=1)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE incidents
            SET state = 'resolved', resolved_at = ?
            WHERE incident_id = ?
            """,
            (resolved_at, opened.incident_id),
        )
    invalid = store.get(opened.incident_id)
    assert invalid.resolution_proof_status == "invalid"
    event_count = len(store.events(opened.incident_id))

    with pytest.raises(ValueError, match="forensic review"):
        store.attest_legacy_resolution(
            opened.incident_id,
            actor="ops@example.test",
            note="A later manual event cannot mask invalid history.",
            outcome="false_positive",
            expected_evidence_sha256=invalid.evidence_sha256,
            now=BASE_TIME + timedelta(minutes=2),
        )

    current = store.get(opened.incident_id)
    assert current.resolution_proof_status == "invalid"
    assert current.operationally_blocking is True
    assert len(store.events(opened.incident_id)) == event_count


def test_manual_attestation_refuses_stale_evidence_backdating_and_exact_retry(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "cas-time-retry.sqlite3")
    opened = store.emit(_alert(), now=BASE_TIME)
    _legacy_resolve(
        store,
        opened.incident_id,
        at=BASE_TIME + timedelta(minutes=2),
    )
    store = AlertStore(store.path)
    legacy = store.get(opened.incident_id)
    event_count = len(store.events(opened.incident_id))

    with pytest.raises(ValueError, match="evidence changed"):
        store.attest_legacy_resolution(
            opened.incident_id,
            actor="ops@example.test",
            note="A stale CAS token must be refused.",
            outcome="verified_recovery",
            expected_evidence_sha256="0" * 64,
            now=BASE_TIME + timedelta(minutes=3),
        )
    with pytest.raises(ValueError, match="later than the legacy resolution"):
        store.attest_legacy_resolution(
            opened.incident_id,
            actor="ops@example.test",
            note="Append order must not permit a backdated proof.",
            outcome="verified_recovery",
            expected_evidence_sha256=legacy.evidence_sha256,
            now=BASE_TIME + timedelta(minutes=1),
        )
    assert len(store.events(opened.incident_id)) == event_count

    attested = store.attest_legacy_resolution(
        opened.incident_id,
        actor="ops@example.test",
        note="The exact reviewed historical resolution.",
        outcome="verified_recovery",
        expected_evidence_sha256=legacy.evidence_sha256,
        now=BASE_TIME + timedelta(minutes=3),
    )
    event_count = len(store.events(opened.incident_id))
    with pytest.raises(ValueError, match="attestation retry refused"):
        store.attest_legacy_resolution(
            opened.incident_id,
            actor="ops@example.test",
            note="The exact reviewed historical resolution.",
            outcome="verified_recovery",
            expected_evidence_sha256=attested.evidence_sha256,
            now=BASE_TIME + timedelta(minutes=4),
        )
    assert len(store.events(opened.incident_id)) == event_count


def test_manual_attestation_refuses_ambiguous_incident_reference(tmp_path) -> None:
    store = AlertStore(tmp_path / "ambiguous.sqlite3")
    first = store.emit(_alert(fingerprint="scheduler:first"), now=BASE_TIME)
    second = store.emit(_alert(fingerprint="scheduler:second"), now=BASE_TIME)
    _legacy_resolve(
        store,
        first.incident_id,
        at=BASE_TIME + timedelta(minutes=1),
    )
    _legacy_resolve(
        store,
        second.incident_id,
        at=BASE_TIME + timedelta(minutes=1),
    )
    store = AlertStore(store.path)
    event_counts = {
        incident_id: len(store.events(incident_id))
        for incident_id in (first.incident_id, second.incident_id)
    }

    with pytest.raises(ValueError, match="ambiguous"):
        store.attest_legacy_resolution(
            "inc-",
            actor="ops@example.test",
            note="An ambiguous reference must never choose an incident.",
            outcome="false_positive",
            expected_evidence_sha256=store.get(first.incident_id).evidence_sha256,
            now=BASE_TIME + timedelta(minutes=2),
        )

    assert {
        incident_id: len(store.events(incident_id))
        for incident_id in (first.incident_id, second.incident_id)
    } == event_counts


def test_alert_attest_resolution_cli_records_one_manual_legacy_proof(
    tmp_path,
    monkeypatch,
) -> None:
    store = AlertStore(tmp_path / "cli.sqlite3")
    opened = store.emit(_alert(), now=BASE_TIME - timedelta(days=1))
    _legacy_resolve(
        store,
        opened.incident_id,
        at=BASE_TIME - timedelta(days=1) + timedelta(minutes=1),
    )
    store = AlertStore(store.path)
    legacy = store.get(opened.incident_id)
    monkeypatch.setattr(alerts_module, "get_alert_store", lambda **kwargs: store)

    result = CliRunner().invoke(
        cli.app,
        [
            "alert-attest-resolution",
            opened.incident_id[:12],
            "--actor",
            "ops@example.test",
            "--note",
            "Reviewed the exact historical recovery record.",
            "--outcome",
            "verified_recovery",
            "--evidence-sha256",
            legacy.evidence_sha256,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Legacy incident resolution attested" in result.output
    current = store.get(opened.incident_id)
    assert current.state == "resolved"
    assert current.resolution_proof_status == "manual_verified_recovery"


def test_reopen_invalidates_prior_proof_and_tampering_is_blocking(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    opened = store.emit(_alert(), now=BASE_TIME)
    context = store.recovery_context(opened.fingerprint)
    assert context is not None
    store.resolve_fingerprint(
        opened.fingerprint,
        recovery=build_scheduler_recovery_evidence(
            context,
            _live_status(),
            observed_at=BASE_TIME + timedelta(minutes=1),
        ),
        now=BASE_TIME + timedelta(minutes=2),
    )
    store.emit(_alert(), now=BASE_TIME + timedelta(minutes=3))
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE incidents
            SET state = 'resolved', resolved_at = ?
            WHERE incident_id = ?
            """,
            (
                (BASE_TIME + timedelta(minutes=4))
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                opened.incident_id,
            ),
        )
    invalid = store.get(opened.incident_id)
    assert invalid.resolution_proof_status == "invalid"
    assert invalid.operationally_blocking is True

    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER incident_events_no_update")
        row = connection.execute(
            """
            SELECT event_id, payload_json
            FROM incident_events
            WHERE event_type = 'resolved'
            ORDER BY rowid
            LIMIT 1
            """
        ).fetchone()
        payload = json.loads(row[1])
        proof = payload["_aios_incident_recovery_proof_v1"]
        proof["observation"]["timers"][TIMER_NAMES[0]][
            "runtime_verified"
        ] = False
        connection.execute(
            "UPDATE incident_events SET payload_json = ? WHERE event_id = ?",
            (json.dumps(payload, sort_keys=True), row[0]),
        )
    assert store.get(opened.incident_id).resolution_proof_status == "invalid"
    assert store.events(opened.incident_id)[-2]["proof_error"]


def test_resolution_insert_guard_summary_and_read_only_reads(tmp_path) -> None:
    path = tmp_path / "alerts.sqlite3"
    store = AlertStore(path)
    warning = store.emit(_alert(), now=BASE_TIME)
    critical = store.emit(
        _alert(
            fingerprint="scheduler:critical-runtime-unverified",
            severity=AlertSeverity.CRITICAL,
        ),
        now=BASE_TIME,
    )
    _legacy_resolve(
        store,
        warning.incident_id,
        at=BASE_TIME + timedelta(minutes=1),
    )
    store = AlertStore(path)
    summary = store.incident_summary()
    assert summary["unproven_resolved"] == 1
    assert summary["operational_blocking"] == 2
    assert summary["critical_operational_blocking"] == 1

    with (
        sqlite3.connect(path) as connection,
        pytest.raises(
            sqlite3.IntegrityError,
            match="requires exactly one audit proof",
        ),
    ):
        connection.execute(
            """
            INSERT INTO incident_events (
                event_id, incident_id, event_type, created_at, payload_json
            ) VALUES (?, ?, 'resolved', ?, '{}')
            """,
            (
                f"evt-proofless-{uuid4().hex}",
                critical.incident_id,
                (BASE_TIME + timedelta(minutes=2))
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            ),
        )

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = path.read_bytes()
    read_only = AlertStore(path, read_only=True)
    assert read_only.get(warning.incident_id).resolution_proof_status == (
        "legacy_unproven"
    )
    assert len(read_only.list(blocking_only=True)) == 2
    assert read_only.incident_summary() == summary
    assert read_only.events(warning.incident_id)
    assert path.read_bytes() == before

def test_producer_recovery_requires_exact_dataclass(tmp_path) -> None:
    store = AlertStore(tmp_path / "alerts.sqlite3")
    opened = store.emit(_alert(), now=BASE_TIME)
    context = store.recovery_context(opened.fingerprint)
    assert context is not None
    recovery = ProducerRecoveryEvidence(
        incident_id=context.incident_id,
        fingerprint=context.fingerprint,
        generation_event_id=context.generation_event_id,
        expected_evidence_sha256=context.evidence_sha256,
        producer="aios scheduler-status",
        proof_kind="scheduler_runtime_verified",
        observed_at=BASE_TIME + timedelta(minutes=1),
        observation={"timers": {}},
    )
    with pytest.raises(ValueError, match="cover every managed timer"):
        store.resolve_fingerprint(
            opened.fingerprint,
            recovery=recovery,
            now=BASE_TIME + timedelta(minutes=2),
        )


def test_read_only_store_rejects_v7_without_resolution_insert_guard(
    tmp_path,
) -> None:
    path = tmp_path / "alerts.sqlite3"
    AlertStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DROP TRIGGER incident_events_resolution_proof_required"
        )
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = path.read_bytes()

    with pytest.raises(ValueError, match="proof schema is incomplete"):
        AlertStore(path, read_only=True)

    assert path.read_bytes() == before

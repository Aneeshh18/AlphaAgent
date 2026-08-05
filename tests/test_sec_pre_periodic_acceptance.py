from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime

import pytest

from aios.alerts import (
    SEC_PRE_PERIODIC_ACCEPTANCE_CONTRACT,
    AlertStore,
    AnomalyObservation,
    AnomalyScan,
    canonical_anomaly_fingerprint,
)

RULE_ID = "sec_fundamentals_coverage_missing"
RULE_VERSION = "1.0.0"
SCOPE = "us-equity-reference:sp500"
ISSUER_ID = "aios:issuer:sec:0002115436"
SECURITY_ID = "aios:bounded:sp500:xom"
DECISION_DATE = "2026-07-29"
RECORDED_AT = datetime(2026, 7, 30, 12, tzinfo=UTC)
EMPTY_ROWS_SHA256 = hashlib.sha256(b"[]").hexdigest()


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _proof(body: dict) -> dict:
    return {**body, "proof_sha256": _sha(body)}


def _bundle() -> dict:
    facts = {
        "snapshot_id": "raw-companyfacts-xom",
        "role": "companyfacts",
        "provider": "sec-edgar",
        "dataset": "companyfacts",
        "artifact_kind": "exact_response",
        "http_status": 200,
        "received_at": "2026-07-30T11:55:00Z",
        "payload_sha256": "a" * 64,
        "parser_version": "sec-companyfacts-v2",
        "parsed_row_count": 0,
        "parsed_rows_sha256": EMPTY_ROWS_SHA256,
        "relative_path": "data/raw/sec-edgar/companyfacts/a.json.gz",
    }
    submissions = {
        "snapshot_id": "raw-submissions-xom",
        "role": "submissions",
        "provider": "sec-edgar",
        "dataset": "submissions",
        "artifact_kind": "exact_response",
        "http_status": 200,
        "received_at": "2026-07-30T11:56:00Z",
        "payload_sha256": "b" * 64,
        "parser_version": "sec-submissions-v2",
        "parsed_row_count": 1,
        "parsed_rows_sha256": "c" * 64,
        "relative_path": "data/raw/sec-edgar/submissions/b.json.gz",
    }
    replay = _proof(
        {
            "parser_version": "sec-companyfacts-v2",
            "decision_evidence_as_of": DECISION_DATE,
            "companyfacts_snapshot_id": facts["snapshot_id"],
            "companyfacts_payload_sha256": facts["payload_sha256"],
            "replayed_rows": 0,
            "replayed_rows_sha256": EMPTY_ROWS_SHA256,
            "decision_visible_valid_rows": 0,
            "decision_visible_rows_sha256": EMPTY_ROWS_SHA256,
        }
    )
    outcome = _proof(
        {
            "run_id": "run-xom-zero",
            "source": "edgar:issuer-cik-history",
            "table_name": "fundamentals",
            "rows_inserted": 0,
            "rows_rejected": 0,
            "status": "warning",
            "error": "SEC returned no fundamental rows",
            "ingest_id": 12594,
        }
    )
    filing_index = _proof(
        {
            "availability": "exact_submissions_filing_index",
            "decision_evidence_as_of": DECISION_DATE,
            "source": {
                "snapshot_id": submissions["snapshot_id"],
                "payload_sha256": submissions["payload_sha256"],
                "received_at": submissions["received_at"],
            },
            "pit_filter": "filingDate <= decision_evidence_as_of",
            "decision_visible_filing_count": 2,
            "excluded_after_decision_count": 0,
            "periodic_form_count": 0,
            "periodic_forms": [],
            "registration_form_count": 2,
            "registration_forms": [
                {
                    "form": "S-8 POS",
                    "filing_count": 2,
                    "first_filing_date": "2026-07-01",
                    "latest_filing_date": "2026-07-01",
                }
            ],
        }
    )
    predecessor_coverage = _proof(
        {
            "state": "not_verified_from_exact_source_replay",
            "accepted_rows": None,
            "facts_transfer_to_active_issuer": False,
        }
    )
    owner = _proof(
        {
            "availability": "reviewed_assignment_history",
            "security_id": SECURITY_ID,
            "active_issuer": {
                "security_id": SECURITY_ID,
                "issuer_id": ISSUER_ID,
                "canonical_name": "ExxonMobil Holdings Corporation",
                "canonical_ticker": "XOM",
                "effective_start": "2026-07-02",
                "effective_end": "2026-08-01",
                "verified_date": "2026-07-21",
                "source": "reviewed-successor-filing",
            },
            "active_owner_start": "2026-07-02",
            "predecessor_owner": {
                "security_id": SECURITY_ID,
                "issuer_id": "aios:issuer:sec:0000034088",
                "canonical_name": "Exxon Mobil Corporation",
                "canonical_ticker": "XOM",
                "effective_start": "2025-01-01",
                "effective_end": "2026-07-02",
                "verified_date": "2026-07-21",
                "source": "reviewed-predecessor-filing",
                "cik": "0000034088",
            },
            "predecessor_fact_coverage": predecessor_coverage,
            "transition_gap_days": 0,
        }
    )
    policy = {
        "future_filing_dates_excluded": True,
        "predecessor_facts_are_context_only": True,
        "predecessor_facts_transfer_to_active_issuer": False,
        "data_repairs": 0,
        "readiness_overrides": 0,
    }
    filing_stage = _proof(
        {
            "context_version": "sec-filing-stage-context.v1",
            "decision_evidence_as_of": DECISION_DATE,
            "submissions_filing_index": filing_index,
            "reviewed_security_owner": owner,
            "policy": policy,
        }
    )
    evidence = {
        "detected_as_of": DECISION_DATE,
        "universe_id": "sp500",
        "reviewed_universe_members": 503,
        "reviewed_universe_issuers": 500,
        "covered_issuers": 497,
        "missing_issuers": 3,
        "coverage_rate": 0.994,
        "issuer": {
            "issuer_id": ISSUER_ID,
            "security_id": SECURITY_ID,
            "cik": "0002115436",
            "canonical_name": "ExxonMobil Holdings Corporation",
            "canonical_ticker": "XOM",
            "ticker": "XOM",
            "verified_date": "2026-07-21",
        },
        "ingest": {
            "ingest_id": 12594,
            "run_id": "run-xom-zero",
            "source": "edgar:issuer-cik-history",
            "identity_binding": "subject_tagged_and_payload_verified",
            "payload_cik": "0002115436",
            "rows_inserted": 0,
            "rows_rejected": 0,
            "snapshots": [facts, submissions],
            "zero_row_replay_proof": replay,
            "zero_row_outcome_proof": outcome,
        },
        "filing_stage": filing_stage,
        "provenance_quality": "complete",
    }
    return {
        "evidence": evidence,
        "new_value": {
            "coverage_state": "missing",
            "accepted_rows": 0,
            "latest_ingest_rows_inserted": 0,
        },
        "scan_evidence": {
            "as_of": DECISION_DATE,
            "executed_rules": [f"{RULE_ID}@{RULE_VERSION}"],
            "safety": {
                "data_repairs": 0,
                "readiness_overrides": 0,
                "paper_actions": 0,
                "broker_actions": 0,
            },
        },
    }


def _refresh_nested_proofs(bundle: dict) -> None:
    evidence = bundle["evidence"]
    ingest = evidence["ingest"]
    for key in ("zero_row_replay_proof", "zero_row_outcome_proof"):
        body = dict(ingest[key])
        body.pop("proof_sha256", None)
        ingest[key] = _proof(body)
    filing_stage = evidence["filing_stage"]
    filing_index = dict(filing_stage["submissions_filing_index"])
    filing_index.pop("proof_sha256", None)
    filing_stage["submissions_filing_index"] = _proof(filing_index)
    owner = filing_stage["reviewed_security_owner"]
    coverage = dict(owner["predecessor_fact_coverage"])
    coverage.pop("proof_sha256", None)
    owner["predecessor_fact_coverage"] = _proof(coverage)
    owner_body = dict(owner)
    owner_body.pop("proof_sha256", None)
    filing_stage["reviewed_security_owner"] = _proof(owner_body)
    stage_body = dict(filing_stage)
    stage_body.pop("proof_sha256", None)
    evidence["filing_stage"] = _proof(stage_body)


def _record(store: AlertStore, bundle: dict):
    fingerprint = canonical_anomaly_fingerprint(
        rule_id=RULE_ID,
        rule_version=RULE_VERSION,
        scope=SCOPE,
        subject_type="issuer",
        subject_id=ISSUER_ID,
    )
    observation = AnomalyObservation(
        fingerprint=fingerprint,
        rule_id=RULE_ID,
        rule_version=RULE_VERSION,
        scope=SCOPE,
        subject_type="issuer",
        subject_id=ISSUER_ID,
        severity="medium",
        confidence="high",
        title="SEC fundamentals pending for XOM",
        summary="The successor issuer has no accepted Company Facts rows.",
        old_value={"expected_state": "covered", "minimum_accepted_rows": 1},
        new_value=bundle["new_value"],
        evidence=bundle["evidence"],
        suggested_checks=("Inspect exact SEC evidence.",),
    )
    scan = AnomalyScan(
        scan_id="scan-xom-pre-periodic",
        rule_bundle_version="us-equity-data-quality.v1",
        scope=SCOPE,
        source_boundary_sha256="d" * 64,
        source_boundary_at=RECORDED_AT,
        executed_rules=(f"{RULE_ID}@{RULE_VERSION}",),
        observations=(observation,),
        evidence=bundle["scan_evidence"],
    )
    return store.record_anomaly_scan(scan, now=RECORDED_AT)[0]


def test_exact_pre_periodic_issuer_can_be_accepted_only_as_missingness(tmp_path) -> None:
    store = AlertStore(tmp_path / "operations.sqlite3")
    case = _record(store, _bundle())

    resolved = store.resolve_anomaly(
        case.case_id,
        outcome="accepted",
        owner="research-ops",
        note="Successor is pre-periodic; predecessor facts remain context only.",
        expected_evidence_sha256=case.evidence_sha256,
        now=datetime(2026, 7, 30, 12, 1, tzinfo=UTC),
    )

    assert resolved.state == "resolved"
    event = store.anomaly_case_events(case.case_id)[0]
    assert event["payload"]["acceptance_contract"] == {
        "contract_id": SEC_PRE_PERIODIC_ACCEPTANCE_CONTRACT,
        "case_evidence_sha256": case.evidence_sha256,
        "analytical_effect": {
            "coverage_changed": False,
            "readiness_changed": False,
            "score_created": False,
            "facts_transferred": False,
        },
    }


def _periodic_filing(bundle: dict) -> None:
    index = bundle["evidence"]["filing_stage"]["submissions_filing_index"]
    index["periodic_form_count"] = 1
    index["periodic_forms"] = [
        {
            "form": "10-Q",
            "filing_count": 1,
            "first_filing_date": "2026-07-20",
            "latest_filing_date": "2026-07-20",
        }
    ]


def _registration_removed(bundle: dict) -> None:
    index = bundle["evidence"]["filing_stage"]["submissions_filing_index"]
    index["registration_form_count"] = 0
    index["registration_forms"] = []


def _transfer_predecessor_facts(bundle: dict) -> None:
    stage = bundle["evidence"]["filing_stage"]
    stage["policy"]["predecessor_facts_transfer_to_active_issuer"] = True
    stage["reviewed_security_owner"]["predecessor_fact_coverage"][
        "facts_transfer_to_active_issuer"
    ] = True


def _visible_replay_row(bundle: dict) -> None:
    replay = bundle["evidence"]["ingest"]["zero_row_replay_proof"]
    replay["decision_visible_valid_rows"] = 1
    replay["decision_visible_rows_sha256"] = "e" * 64


def _identity_drift(bundle: dict) -> None:
    bundle["evidence"]["issuer"]["issuer_id"] = "aios:issuer:sec:0000034088"


def _analytical_action(bundle: dict) -> None:
    bundle["scan_evidence"]["safety"]["paper_actions"] = 1


def _coverage_created(bundle: dict) -> None:
    bundle["new_value"]["accepted_rows"] = 1


@pytest.mark.parametrize(
    "mutator",
    (
        _periodic_filing,
        _registration_removed,
        _transfer_predecessor_facts,
        _visible_replay_row,
        _identity_drift,
        _analytical_action,
        _coverage_created,
    ),
)
def test_semantically_unsafe_but_rehashed_sec_acceptance_is_refused(
    tmp_path,
    mutator,
) -> None:
    bundle = copy.deepcopy(_bundle())
    mutator(bundle)
    _refresh_nested_proofs(bundle)
    store = AlertStore(tmp_path / "operations.sqlite3")
    case = _record(store, bundle)

    with pytest.raises(ValueError, match="SEC pre-periodic"):
        store.resolve_anomaly(
            case.case_id,
            outcome="accepted",
            owner="research-ops",
            note="This evidence must remain blocked.",
            expected_evidence_sha256=case.evidence_sha256,
            now=datetime(2026, 7, 30, 12, 1, tzinfo=UTC),
        )

    assert store.anomaly_case(case.case_id).state == "open"


def test_nested_proof_tampering_is_refused(tmp_path) -> None:
    bundle = _bundle()
    bundle["evidence"]["filing_stage"]["proof_sha256"] = "0" * 64
    store = AlertStore(tmp_path / "operations.sqlite3")
    case = _record(store, bundle)

    with pytest.raises(ValueError, match="checksum does not match"):
        store.resolve_anomaly(
            case.case_id,
            outcome="accepted",
            owner="research-ops",
            note="Tampered nested evidence must fail.",
            expected_evidence_sha256=case.evidence_sha256,
            now=datetime(2026, 7, 30, 12, 1, tzinfo=UTC),
        )

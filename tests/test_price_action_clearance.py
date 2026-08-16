from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from aios.alerts import (
    AlertStore,
    AnomalyObservation,
    AnomalyScan,
    canonical_anomaly_fingerprint,
)
from aios.anomalies import (
    PRICE_RULE_ID,
    PRICE_RULE_VERSION,
    _price_action_clearance_proofs,
)
from aios.ingest.prices import (
    YFINANCE_PARSER_VERSION,
    parse_yfinance_normalized_export,
    relabel_provider_price_rows,
)
from aios.raw_snapshots import (
    canonical_request_fingerprint,
    capture_raw_snapshot,
)
from aios.storage.store import Store

SECURITY_ID = "aios:security:price-clearance-test"
SCOPE = "us-equity-prices:test"
SUBJECT_ID = f"{SECURITY_ID}@2026-08-14"
FINDING_TIME = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
SNAPSHOT_TIME = datetime(2026, 8, 14, 10, 0, tzinfo=UTC)
VERIFICATION_TIME = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _proof_digest(proof: dict) -> str:
    body = dict(proof)
    body.pop("proof_sha256", None)
    encoded = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _price_payload() -> bytes:
    return json.dumps(
        {
            "export_schema_version": 1,
            "provider": "yfinance",
            "symbol": "TEST",
            "requested_start": "2026-08-13",
            "requested_end_exclusive": "2026-08-15",
            "normalization_through": "2026-08-14",
            "provider_rows": [
                {
                    "date": "2026-08-13",
                    "open": 99.0,
                    "high": 101.0,
                    "low": 98.0,
                    "close": 100.0,
                    "adj_close": 100.0,
                    "volume": 1_000,
                    "dividends": 0.0,
                    "stock_splits": 0.0,
                },
                {
                    "date": "2026-08-14",
                    "open": 104.0,
                    "high": 106.0,
                    "low": 103.0,
                    "close": 105.0,
                    "adj_close": 105.0,
                    "volume": 1_100,
                    "dividends": 0.0,
                    "stock_splits": 0.0,
                },
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _seed_verified_prices(store: Store, project_root) -> None:
    payload = _price_payload()
    parsed = parse_yfinance_normalized_export(payload)
    capture_raw_snapshot(
        payload,
        provider="yfinance",
        dataset="daily-prices",
        artifact_kind="normalized_provider_export",
        requested_at=SNAPSHOT_TIME - timedelta(minutes=1),
        received_at=SNAPSHOT_TIME,
        request_fingerprint=canonical_request_fingerprint(
            {"provider": "yfinance", "symbol": "TEST", "purpose": "test"}
        ),
        adapter_name="aios-yfinance-library",
        adapter_version="1",
        parser_version=YFINANCE_PARSER_VERSION,
        content_type="application/vnd.aios.yfinance-normalized+json",
        parsed_rows=parsed,
        ingest_run_id="price-clearance-run",
        role="prices:TEST",
        store=store,
        project_root=project_root,
    )
    relabeled = relabel_provider_price_rows(
        parsed,
        {
            "mapping_status": "verified",
            "security_id": SECURITY_ID,
            "provider": "yfinance",
            "provider_symbol": "TEST",
            "data_start": "2026-08-13",
            "data_end": "2026-08-15",
        },
        [
            {
                "ticker": "TEST",
                "effective_start": "2026-08-13",
                "effective_end": "2026-08-15",
            }
        ],
    )
    assert store.upsert_prices(relabeled) == 2


def _finding() -> AnomalyObservation:
    fingerprint = canonical_anomaly_fingerprint(
        rule_id=PRICE_RULE_ID,
        rule_version=PRICE_RULE_VERSION,
        scope=SCOPE,
        subject_type="security_session",
        subject_id=SUBJECT_ID,
    )
    return AnomalyObservation(
        fingerprint=fingerprint,
        rule_id=PRICE_RULE_ID,
        rule_version=PRICE_RULE_VERSION,
        scope=SCOPE,
        subject_type="security_session",
        subject_id=SUBJECT_ID,
        severity="medium",
        confidence="high",
        title="TEST moved with no corporate action",
        summary="The originally observed source row crossed the review threshold.",
        old_value={"date": "2026-08-13", "close": 100.0},
        new_value={"date": "2026-08-14", "close": 50.0},
        evidence={"provider_symbol": "TEST"},
        suggested_checks=("Refetch and verify the immutable provider response.",),
    )


def _scan(
    scan_id: str,
    *,
    boundary: datetime,
    observations: tuple[AnomalyObservation, ...],
    clearance_proofs: dict | None = None,
) -> AnomalyScan:
    return AnomalyScan(
        scan_id=scan_id,
        rule_bundle_version="us-equity-data-quality.v1",
        scope=SCOPE,
        source_boundary_sha256=_digest(scan_id),
        source_boundary_at=boundary,
        executed_rules=(f"{PRICE_RULE_ID}@{PRICE_RULE_VERSION}",),
        observations=observations,
        evidence={"clearance_proofs": clearance_proofs or {}},
    )


def test_price_source_correction_requires_exact_snapshots_and_resolves(tmp_path) -> None:
    data_store = Store(tmp_path / "prices.duckdb")
    try:
        _seed_verified_prices(data_store, tmp_path)
        proofs = _price_action_clearance_proofs(
            store=data_store,
            scope=SCOPE,
            subject_ids=(SUBJECT_ID,),
            decision_date=datetime(2026, 8, 14, tzinfo=UTC).date(),
            move_threshold=0.25,
            project_root=tmp_path,
        )
    finally:
        data_store.close()

    fingerprint = _finding().fingerprint
    assert list(proofs) == [fingerprint]
    assert proofs[fingerprint]["current_source_snapshot"]["received_at"] == (
        SNAPSHOT_TIME.isoformat()
    )

    ledger = AlertStore(tmp_path / "operations.sqlite3")
    case = ledger.record_anomaly_scan(
        _scan(
            "price-finding",
            boundary=FINDING_TIME,
            observations=(_finding(),),
        ),
        now=FINDING_TIME,
    )[0]
    ledger.record_anomaly_scan(
        _scan(
            "price-verification",
            boundary=VERIFICATION_TIME,
            observations=(),
            clearance_proofs=proofs,
        ),
        now=VERIFICATION_TIME,
    )

    resolved = ledger.resolve_anomaly(
        case.case_id,
        outcome="source_corrected",
        owner="data-ops",
        note="A later retained provider snapshot proves the corrected pair.",
        expected_evidence_sha256=case.evidence_sha256,
        verification_scan_id="price-verification",
        now=VERIFICATION_TIME + timedelta(minutes=1),
    )

    assert resolved.state == "resolved"
    assert resolved.resolution_outcome == "source_corrected"


def test_direct_price_edit_cannot_create_source_clearance_proof(tmp_path) -> None:
    store = Store(tmp_path / "prices.duckdb")
    try:
        _seed_verified_prices(store, tmp_path)
        store.execute(
            "UPDATE prices SET close = 104.0 WHERE security_id = ? AND date = DATE '2026-08-14'",
            (SECURITY_ID,),
        )

        proofs = _price_action_clearance_proofs(
            store=store,
            scope=SCOPE,
            subject_ids=(SUBJECT_ID,),
            decision_date=datetime(2026, 8, 14, tzinfo=UTC).date(),
            move_threshold=0.25,
            project_root=tmp_path,
        )
    finally:
        store.close()

    assert proofs == {}


def test_price_correction_without_clearance_proof_is_refused(tmp_path) -> None:
    ledger = AlertStore(tmp_path / "operations.sqlite3")
    case = ledger.record_anomaly_scan(
        _scan(
            "price-finding",
            boundary=FINDING_TIME,
            observations=(_finding(),),
        ),
        now=FINDING_TIME,
    )[0]
    ledger.record_anomaly_scan(
        _scan(
            "price-verification",
            boundary=VERIFICATION_TIME,
            observations=(),
        ),
        now=VERIFICATION_TIME,
    )

    with pytest.raises(ValueError, match="lacks a source-provenanced clearance proof"):
        ledger.resolve_anomaly(
            case.case_id,
            outcome="source_corrected",
            owner="data-ops",
            note="A clean scan without exact source rows is insufficient.",
            expected_evidence_sha256=case.evidence_sha256,
            verification_scan_id="price-verification",
            now=VERIFICATION_TIME + timedelta(minutes=1),
        )


def test_price_correction_rejects_non_finite_string_economics(tmp_path) -> None:
    data_store = Store(tmp_path / "prices.duckdb")
    try:
        _seed_verified_prices(data_store, tmp_path)
        proofs = _price_action_clearance_proofs(
            store=data_store,
            scope=SCOPE,
            subject_ids=(SUBJECT_ID,),
            decision_date=datetime(2026, 8, 14, tzinfo=UTC).date(),
            move_threshold=0.25,
            project_root=tmp_path,
        )
    finally:
        data_store.close()

    fingerprint = _finding().fingerprint
    proofs[fingerprint]["close_change_fraction"] = "NaN"
    proofs[fingerprint]["proof_sha256"] = _proof_digest(proofs[fingerprint])
    ledger = AlertStore(tmp_path / "operations.sqlite3")
    case = ledger.record_anomaly_scan(
        _scan(
            "price-finding",
            boundary=FINDING_TIME,
            observations=(_finding(),),
        ),
        now=FINDING_TIME,
    )[0]
    ledger.record_anomaly_scan(
        _scan(
            "price-verification",
            boundary=VERIFICATION_TIME,
            observations=(),
            clearance_proofs=proofs,
        ),
        now=VERIFICATION_TIME,
    )

    with pytest.raises(ValueError, match="economics are out of range"):
        ledger.resolve_anomaly(
            case.case_id,
            outcome="source_corrected",
            owner="data-ops",
            note="A non-finite proof must not clear a price case.",
            expected_evidence_sha256=case.evidence_sha256,
            verification_scan_id="price-verification",
            now=VERIFICATION_TIME + timedelta(minutes=1),
        )

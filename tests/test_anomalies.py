from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import zlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from aios import anomalies as anomalies_module
from aios.alerts import (
    SEC_SOURCE_BOUNDARY_POLICY_V1_TO_V2,
    SEC_SOURCE_BOUNDARY_POLICY_V2,
    AlertStore,
    AnomalyObservation,
    AnomalyScan,
)
from aios.anomalies import (
    MAX_SEC_SNAPSHOT_ORIGINAL_BYTES,
    MAX_SEC_SNAPSHOT_STORED_BYTES,
    anomaly_fingerprint,
    scan_sec_fundamental_coverage,
)
from aios.ingest.edgar import (
    COMPANYFACTS_CAPTURE_PARSER_VERSION,
    COMPANYFACTS_NEXT_PARSER_VERSION,
    COMPANYFACTS_PARSER_VERSION,
    canonical_sec_fundamental_row_sha256,
    parse_sec_companyfacts_response,
    parse_sec_companyfacts_response_v3,
)
from aios.raw_snapshots import canonical_parsed_rows_sha256


class _EvidenceStore:
    def __init__(
        self,
        *,
        members: list[dict],
        references: dict[str, dict],
        coverage: list[dict],
        warnings: list[dict],
        evidence: dict[str, dict],
        successes: list[dict] | None = None,
        legacy_rows: dict[str, list[dict]] | None = None,
    ) -> None:
        self._members = members
        self._references = references
        self._coverage = coverage
        self._warnings = warnings
        self._evidence = evidence
        self._successes = successes
        self._legacy_rows = legacy_rows or {}
        self.coverage_query: str | None = None

    def universe_identity_labels(self, universe_id, as_of):
        assert universe_id == "sp500"
        assert as_of == date(2026, 7, 27)
        return self._members

    def issuer_reference(self, issuer_id, *, as_of=None):
        assert as_of == date(2026, 7, 27)
        return self._references.get(issuer_id)

    def issuer_id_for_security(self, security_id, as_of):
        assert as_of == date(2026, 7, 27)
        return next(
            (
                row["issuer_id"]
                for row in self._members
                if row["security_id"] == security_id
            ),
            None,
        )

    def ingest_evidence(self, run_id):
        return self._evidence.get(run_id)

    def sec_fundamental_lineage_rows(self, issuer_ids, as_of):
        assert set(issuer_ids) == set(self._references)
        assert as_of == date(2026, 7, 27)
        return [
            row for row in self._coverage if row.get("ingest_run_id") is not None
        ]

    def query(self, sql, params=None):
        if "anomaly_active_owner_context" in sql:
            return []
        if "anomaly_predecessor_owner_context" in sql:
            return []
        if "GROUP BY issuer_id" in sql:
            assert params == ("2026-07-27",)
            self.coverage_query = sql
            return self._coverage
        if "subject_type IS NULL" in sql:
            assert params is None
            return [
                row
                for row in (self._successes or [])
                if row.get("subject_type") is None and row.get("subject_id") is None
            ]
        if "subject_type = 'issuer'" in sql:
            assert params is not None
            issuer_ids = set(params)
            return [
                row
                for row in (self._successes or [])
                if row.get("subject_type") == "issuer"
                and row.get("subject_id") in issuer_ids
            ]
        if "FROM fundamentals" in sql and "source = 'edgar'" in sql:
            assert params is not None
            issuer_id = str(params[0])
            return self._legacy_rows.get(
                issuer_id,
                [
                    row
                    for row in self._coverage
                    if row.get("issuer_id") == issuer_id
                ],
            )
        if "FROM ingest_log" in sql and "NULL AS subject_type" in sql:
            assert params is None
            return [
                row
                for row in (self._successes or [])
                if row.get("subject_type") is None and row.get("subject_id") is None
            ]
        if "status = 'warning'" in sql:
            assert params in (None, ("SEC returned no fundamental rows",))
            return self._warnings
        if "information_schema.columns" in sql:
            return (
                [
                    {"column_name": "subject_type"},
                    {"column_name": "subject_id"},
                ]
                if self._successes is not None
                else []
            )
        if "status = 'success'" in sql:
            assert params is None
            return self._successes or []
        if "MAX(finished_at)" in sql:
            return [{"latest_finished_at": "2026-07-25 13:50:35.806262"}]
        if "MAX(received_at)" in sql:
            return [{"latest_received_at": "2026-07-25 08:20:35.806262"}]
        raise AssertionError(sql)


def test_sec_coverage_scan_builds_one_evidence_bound_case_per_missing_issuer(
    tmp_path,
) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[],
        warnings=warnings,
        evidence=evidence,
    )

    scan = scan_sec_fundamental_coverage(
        store=store,
        as_of="2026-07-27",
        project_root=tmp_path,
        minimum_members=1,
        maximum_members=10,
    )

    assert scan.scan_id.startswith("dqs-")
    assert len(scan.observations) == 3
    assert len({row.fingerprint for row in scan.observations}) == 3
    assert {row.subject_id for row in scan.observations} == set(references)
    assert {row.severity for row in scan.observations} == {"medium"}
    assert {row.confidence for row in scan.observations} == {"medium"}
    assert scan.evidence["reviewed_members"] == 3
    assert scan.evidence["reviewed_issuers"] == 3
    assert scan.evidence["covered_issuers"] == 0
    assert scan.evidence["missing_issuers"] == 3
    assert scan.evidence["coverage_rate"] == 0.0
    assert scan.evidence["safety"] == {
        "data_repairs": 0,
        "readiness_overrides": 0,
        "paper_actions": 0,
        "broker_actions": 0,
    }
    assert scan.evidence["temporal_mode"] == "retrospective_review_no_backfill"
    assert scan.evidence["source_boundary_policy"] == (
        SEC_SOURCE_BOUNDARY_POLICY_V2
    )
    assert scan.evidence["source_boundary_policy_transition"] == (
        SEC_SOURCE_BOUNDARY_POLICY_V1_TO_V2
    )
    assert scan.evidence["source_boundary_proof"][
        "used_snapshot_count"
    ] == scan.evidence["used_snapshot_count"]
    assert scan.evidence["source_boundary_proof"][
        "maximum_received_at"
    ] == scan.source_boundary_at.isoformat()
    assert len(
        scan.evidence["source_boundary_proof"]["used_snapshot_set_sha256"]
    ) == 64
    first = scan.observations[0]
    assert first.evidence["ingest"]["identity_binding"] in {
        "legacy_exact_payload_cik",
        "subject_tagged_and_payload_verified",
    }
    replay = first.evidence["ingest"]["zero_row_replay_proof"]
    assert replay["parser_version"] == COMPANYFACTS_PARSER_VERSION
    assert replay["decision_evidence_as_of"] == "2026-07-27"
    assert replay["replayed_rows"] == 0
    assert replay["decision_visible_valid_rows"] == 0
    assert len(replay["proof_sha256"]) == 64
    assert len(
        first.evidence["ingest"]["zero_row_outcome_proof"]["proof_sha256"]
    ) == 64
    assert first.new_value["accepted_rows"] == 0
    filing_stage = first.evidence["filing_stage"]
    assert filing_stage["context_version"] == "sec-filing-stage-context.v1"
    assert (
        filing_stage["submissions_filing_index"]["availability"]
        == "filing_index_not_present"
    )
    assert (
        filing_stage["reviewed_security_owner"]["availability"]
        == "assignment_detail_not_available"
    )
    assert filing_stage["policy"]["future_filing_dates_excluded"] is True
    assert (
        filing_stage["policy"]["predecessor_facts_transfer_to_active_issuer"]
        is False
    )
    assert len(filing_stage["proof_sha256"]) == 64


def test_sec_coverage_scan_rejects_positive_payload_disguised_as_zero_warning(
    tmp_path,
) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    warning = warnings[1]
    run_id = warning["run_id"]
    issuer_id = evidence[run_id]["subject_id"]
    cik = references[issuer_id]["cik"]
    positive_payload = json.dumps(
        {
            "cik": int(cik),
            "entityName": references[issuer_id]["canonical_name"],
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {
                            "USD": [
                                {
                                    "accn": "0001-26-000001",
                                    "start": "2026-01-01",
                                    "end": "2026-06-30",
                                    "filed": "2026-07-25",
                                    "fp": "Q2",
                                    "fy": 2026,
                                    "val": 100.0,
                                }
                            ]
                        }
                    }
                }
            },
        },
        sort_keys=True,
    ).encode()
    replacement = _raw_snapshot(
        tmp_path,
        run_id=run_id,
        role="companyfacts",
        dataset="companyfacts",
        payload=positive_payload,
    )
    evidence[run_id]["snapshots"] = [
        replacement
        if row["role"] == "companyfacts"
        else row
        for row in evidence[run_id]["snapshots"]
    ]
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[],
        warnings=warnings,
        evidence=evidence,
    )

    with pytest.raises(
        ValueError,
        match="Company Facts replay produced decision-visible rows",
    ):
        scan_sec_fundamental_coverage(
            store=store,
            as_of="2026-07-27",
            project_root=tmp_path,
            minimum_members=1,
            maximum_members=10,
        )


def test_sec_coverage_scan_rejects_rows_rejected_outcome_mismatch(tmp_path) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    run_id = warnings[1]["run_id"]
    evidence[run_id]["rows_rejected"] = 1
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[],
        warnings=warnings,
        evidence=evidence,
    )

    with pytest.raises(
        ValueError,
        match="does not match its warning.*rows_rejected",
    ):
        scan_sec_fundamental_coverage(
            store=store,
            as_of="2026-07-27",
            project_root=tmp_path,
            minimum_members=1,
            maximum_members=10,
        )


@pytest.mark.parametrize(
    "parser_version",
    [COMPANYFACTS_PARSER_VERSION, COMPANYFACTS_NEXT_PARSER_VERSION],
)
def test_sec_zero_row_warning_matches_structured_replay_rejections(
    tmp_path,
    parser_version,
) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    run_id = _replace_warning_with_future_only_companyfacts(
        tmp_path,
        references=references,
        warnings=warnings,
        evidence=evidence,
        parser_version=parser_version,
    )
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[],
        warnings=warnings,
        evidence=evidence,
    )

    scan = scan_sec_fundamental_coverage(
        store=store,
        as_of="2026-07-27",
        project_root=tmp_path,
        minimum_members=1,
        maximum_members=10,
    )

    observation = next(
        row
        for row in scan.observations
        if row.subject_id == evidence[run_id]["subject_id"]
    )
    proof = observation.evidence["ingest"]
    expected_code = (
        "future_period"
        if parser_version == COMPANYFACTS_PARSER_VERSION
        else "unsupported_context"
    )
    assert proof["zero_row_replay_proof"]["rows_rejected"] == 1
    assert proof["zero_row_replay_proof"]["rejection_codes"] == [
        expected_code
    ]
    assert proof["zero_row_outcome_proof"]["rejection_codes"] == (
        f'["{expected_code}"]'
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("rows_rejected", 2, "rows_rejected"),
        ("rejection_codes", '["storage_conflict"]', "rejection_codes"),
    ],
)
def test_v3_zero_row_warning_rejects_replay_outcome_mismatch(
    tmp_path,
    field,
    value,
    message,
) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    run_id = _replace_warning_with_future_only_companyfacts(
        tmp_path,
        references=references,
        warnings=warnings,
        evidence=evidence,
        parser_version=COMPANYFACTS_NEXT_PARSER_VERSION,
    )
    warning = next(row for row in warnings if row["run_id"] == run_id)
    warning[field] = value
    evidence[run_id][field] = value
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[],
        warnings=warnings,
        evidence=evidence,
    )

    with pytest.raises(ValueError, match=message):
        scan_sec_fundamental_coverage(
            store=store,
            as_of="2026-07-27",
            project_root=tmp_path,
            minimum_members=1,
            maximum_members=10,
        )


def test_sec_zero_row_warning_rejects_unknown_metadata_free_parser(tmp_path) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    run_id = warnings[1]["run_id"]
    facts = next(
        row
        for row in evidence[run_id]["snapshots"]
        if row["role"] == "companyfacts"
    )
    facts["parser_version"] = "sec-companyfacts-unknown"
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[],
        warnings=warnings,
        evidence=evidence,
    )

    with pytest.raises(ValueError, match="parser is unsupported"):
        scan_sec_fundamental_coverage(
            store=store,
            as_of="2026-07-27",
            project_root=tmp_path,
            minimum_members=1,
            maximum_members=10,
        )


def test_sec_coverage_scan_is_stable_for_the_same_source_boundary(tmp_path) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[],
        warnings=warnings,
        evidence=evidence,
    )

    first = scan_sec_fundamental_coverage(
        store=store,
        as_of="2026-07-27",
        project_root=tmp_path,
        minimum_members=1,
        maximum_members=10,
    )
    second = scan_sec_fundamental_coverage(
        store=store,
        as_of="2026-07-27",
        project_root=tmp_path,
        minimum_members=1,
        maximum_members=10,
    )

    assert first.scan_id == second.scan_id
    assert first.source_boundary_sha256 == second.source_boundary_sha256
    assert first.observations == second.observations


def test_sec_coverage_scan_refuses_missing_or_tampered_source_evidence(tmp_path) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    evidence.pop(warnings[0]["run_id"])
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[],
        warnings=warnings,
        evidence=evidence,
    )

    with pytest.raises(ValueError, match="no ingest evidence"):
        scan_sec_fundamental_coverage(
            store=store,
            as_of="2026-07-27",
            project_root=tmp_path,
            minimum_members=1,
            maximum_members=10,
        )

    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    snapshot = evidence[warnings[0]["run_id"]]["snapshots"][0]
    (tmp_path / snapshot["relative_path"]).write_bytes(gzip.compress(b"{}"))
    tampered = _EvidenceStore(
        members=members,
        references=references,
        coverage=[],
        warnings=warnings,
        evidence=evidence,
    )
    with pytest.raises(ValueError, match="stored size|checksum|original size"):
        scan_sec_fundamental_coverage(
            store=tampered,
            as_of="2026-07-27",
            project_root=tmp_path,
            minimum_members=1,
            maximum_members=10,
        )


def test_sec_coverage_scan_verifies_submissions_bytes_and_cik(tmp_path) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    first_run = warnings[0]["run_id"]
    submissions = next(
        row
        for row in evidence[first_run]["snapshots"]
        if row["role"] == "submissions"
    )
    (tmp_path / submissions["relative_path"]).write_bytes(gzip.compress(b"{}"))
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[],
        warnings=warnings,
        evidence=evidence,
    )

    with pytest.raises(ValueError, match="stored size|checksum|original size"):
        scan_sec_fundamental_coverage(
            store=store,
            as_of="2026-07-27",
            project_root=tmp_path,
            minimum_members=1,
            maximum_members=10,
        )

    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    first_run = warnings[0]["run_id"]
    submissions = next(
        row
        for row in evidence[first_run]["snapshots"]
        if row["role"] == "submissions"
    )
    mismatched = json.dumps(
        {"cik": "9999999999", "name": "Wrong issuer"},
        sort_keys=True,
    ).encode()
    compressed = gzip.compress(mismatched, mtime=0)
    target = tmp_path / submissions["relative_path"]
    target.write_bytes(compressed)
    submissions["payload_sha256"] = hashlib.sha256(mismatched).hexdigest()
    submissions["original_bytes"] = len(mismatched)
    submissions["stored_bytes"] = len(compressed)
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[],
        warnings=warnings,
        evidence=evidence,
    )

    with pytest.raises(ValueError, match="CIKs disagree"):
        scan_sec_fundamental_coverage(
            store=store,
            as_of="2026-07-27",
            project_root=tmp_path,
            minimum_members=1,
            maximum_members=10,
        )


def test_sec_coverage_scan_rejects_identity_disagreement(tmp_path) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    members[0]["issuer_id"] = "issuer-wrong"
    references["issuer-wrong"] = references.pop("issuer-fdx")
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[],
        warnings=warnings,
        evidence=evidence,
    )
    store.issuer_id_for_security = lambda security_id, as_of: (
        "issuer-fdx" if security_id == "security-fdxf" else next(
            row["issuer_id"]
            for row in members
            if row["security_id"] == security_id
        )
    )

    with pytest.raises(ValueError, match="active security owner"):
        scan_sec_fundamental_coverage(
            store=store,
            as_of="2026-07-27",
            project_root=tmp_path,
            minimum_members=1,
            maximum_members=10,
        )


def test_sec_coverage_scan_rejects_duplicate_active_cik_owners(tmp_path) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    references["issuer-hon"]["cik"] = references["issuer-fdx"]["cik"]
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[],
        warnings=warnings,
        evidence=evidence,
    )

    with pytest.raises(ValueError, match="share one active SEC CIK"):
        scan_sec_fundamental_coverage(
            store=store,
            as_of="2026-07-27",
            project_root=tmp_path,
            minimum_members=1,
            maximum_members=10,
        )


def test_sec_coverage_scan_rejects_symlinked_raw_parent(tmp_path) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    first_run = warnings[0]["run_id"]
    companyfacts = next(
        row
        for row in evidence[first_run]["snapshots"]
        if row["role"] == "companyfacts"
    )
    dataset_dir = tmp_path / "data" / "raw" / "sec-edgar" / "companyfacts"
    moved_dir = tmp_path / "outside-companyfacts"
    dataset_dir.rename(moved_dir)
    dataset_dir.symlink_to(moved_dir, target_is_directory=True)
    assert (tmp_path / companyfacts["relative_path"]).exists()
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[],
        warnings=warnings,
        evidence=evidence,
    )

    with pytest.raises(ValueError, match="unsafe parent"):
        scan_sec_fundamental_coverage(
            store=store,
            as_of="2026-07-27",
            project_root=tmp_path,
            minimum_members=1,
            maximum_members=10,
        )


@pytest.mark.parametrize(
    ("size_field", "unsafe_size"),
    [
        ("stored_bytes", MAX_SEC_SNAPSHOT_STORED_BYTES + 1),
        ("original_bytes", MAX_SEC_SNAPSHOT_ORIGINAL_BYTES + 1),
    ],
)
def test_sec_coverage_scan_rejects_unsafe_snapshot_size_metadata(
    tmp_path,
    size_field,
    unsafe_size,
) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    evidence[warnings[1]["run_id"]]["snapshots"][0][size_field] = unsafe_size
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[],
        warnings=warnings,
        evidence=evidence,
    )

    with pytest.raises(ValueError, match="size exceeds.*safety limit"):
        scan_sec_fundamental_coverage(
            store=store,
            as_of="2026-07-27",
            project_root=tmp_path,
            minimum_members=1,
            maximum_members=10,
        )


def test_sec_coverage_scan_converts_decoder_failures_to_safe_evidence_errors(
    tmp_path,
    monkeypatch,
) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[],
        warnings=warnings,
        evidence=evidence,
    )

    def corrupt_decoder(*_args, **_kwargs):
        raise zlib.error("invalid stored block lengths")

    monkeypatch.setattr(anomalies_module.gzip, "GzipFile", corrupt_decoder)

    with pytest.raises(ValueError, match="compression is invalid"):
        scan_sec_fundamental_coverage(
            store=store,
            as_of="2026-07-27",
            project_root=tmp_path,
            minimum_members=1,
            maximum_members=10,
        )


def test_sec_coverage_scan_rejects_unproven_aggregate_coverage(tmp_path) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    coverage = [
        {
            "issuer_id": issuer_id,
            "accepted_rows": 10,
            "first_as_of_date": "2026-01-01",
            "latest_as_of_date": "2026-07-20",
        }
        for issuer_id in references
    ]
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=coverage,
        warnings=warnings,
        evidence=evidence,
    )

    scan = scan_sec_fundamental_coverage(
        store=store,
        as_of="2026-07-27",
        project_root=tmp_path,
        minimum_members=1,
        maximum_members=10,
    )

    assert len(scan.observations) == 3
    assert scan.evidence["covered_issuers"] == 0
    assert scan.evidence["missing_issuers"] == 3
    assert scan.evidence["coverage_rate"] == 0.0


def test_sec_coverage_scan_emits_clearance_only_for_verified_success_lineage(
    tmp_path,
) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    issuer_id = "issuer-fdx"
    cik = references[issuer_id]["cik"]
    success_run_id = "run-success-fdx"
    companyfacts_payload = json.dumps(
        {
            "cik": int(cik),
            "entityName": references[issuer_id]["canonical_name"],
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {
                            "USD": [
                                {
                                    "accn": "0001-26-000001",
                                    "start": "2026-01-01",
                                    "end": "2026-06-30",
                                    "filed": "2026-07-25",
                                    "fp": "Q2",
                                    "fy": 2026,
                                    "val": 100.0,
                                }
                            ]
                        }
                    }
                }
            },
        },
        sort_keys=True,
    ).encode()
    provider_rows = parse_sec_companyfacts_response(companyfacts_payload)
    assert len(provider_rows) == 1
    companyfacts = _raw_snapshot(
        tmp_path,
        run_id=success_run_id,
        role="companyfacts",
        dataset="companyfacts",
        payload=companyfacts_payload,
    )
    companyfacts["parser_version"] = COMPANYFACTS_PARSER_VERSION
    companyfacts["parsed_row_count"] = 1
    companyfacts["parsed_rows_sha256"] = canonical_parsed_rows_sha256(
        provider_rows
    )
    submissions = _raw_snapshot(
        tmp_path,
        run_id=success_run_id,
        role="submissions",
        dataset="submissions",
        payload=json.dumps(
            {
                "cik": cik,
                "name": references[issuer_id]["canonical_name"],
                "tickers": ["FDXF"],
            },
            sort_keys=True,
        ).encode(),
    )
    evidence[success_run_id] = {
        "id": 10,
        "run_id": success_run_id,
        "source": "edgar:issuer-cik-history",
        "table_name": "fundamentals",
        "subject_type": "issuer",
        "subject_id": issuer_id,
        "rows_inserted": 1,
        "rows_rejected": 0,
        "status": "success",
        "error": None,
        "snapshots": [companyfacts, submissions],
    }
    successes = [
        {
            "id": 10,
            "run_id": success_run_id,
            "source": "edgar:issuer-cik-history",
            "subject_type": "issuer",
            "subject_id": issuer_id,
            "rows_inserted": 1,
            "rows_rejected": 0,
            "started_at": "2026-07-26 10:00:00",
            "finished_at": "2026-07-26 10:00:01",
            "status": "success",
            "error": None,
        }
    ]
    provider_row = provider_rows[0]
    coverage = [
        {
            "ticker": "FDXF",
            "issuer_id": issuer_id,
            "security_id": "security-fdxf",
            "period_end": provider_row["period_end"],
            "as_of_date": provider_row["as_of_date"],
            "fiscal_period": provider_row["fiscal_period"],
            "statement": provider_row["statement"],
            "metric": provider_row["metric"],
            "value": provider_row["value"],
            "quarter_value": provider_row["quarter_value"],
            "unit": provider_row["unit"],
            "source": "edgar",
            "ingest_run_id": success_run_id,
            "source_snapshot_id": companyfacts["snapshot_id"],
            "source_rowset_sha256": companyfacts["parsed_rows_sha256"],
            "source_row_sha256": canonical_sec_fundamental_row_sha256(
                provider_row
            ),
            "ingest_id": 10,
            "ingest_rows_inserted": 1,
            "ingest_finished_at": "2026-07-26 10:00:01",
            "ingest_status": "success",
            "ingest_error": None,
        }
    ]
    verified = _EvidenceStore(
        members=members,
        references=references,
        coverage=coverage,
        warnings=warnings,
        evidence=evidence,
        successes=successes,
    )

    scan = scan_sec_fundamental_coverage(
        store=verified,
        as_of="2026-07-27",
        project_root=tmp_path,
        minimum_members=1,
        maximum_members=10,
    )
    fingerprint = anomaly_fingerprint(
        rule_id="sec_fundamentals_coverage_missing",
        rule_version="1.0.0",
        scope="us-equity-reference:sp500",
        subject_type="issuer",
        subject_id=issuer_id,
    )
    proof = scan.evidence["clearance_proofs"][fingerprint]
    assert proof["coverage_state"] == "covered_with_verified_ingest"
    assert proof["accepted_rows"] == 1
    assert proof["ingest_run_id"] == success_run_id
    assert proof["companyfacts_snapshot"]["parsed_row_count"] == 1
    assert len(proof["proof_sha256"]) == 64

    unproven = _EvidenceStore(
        members=members,
        references=references,
        coverage=[],
        warnings=warnings,
        evidence=evidence,
    )
    scan_without_lineage = scan_sec_fundamental_coverage(
        store=unproven,
        as_of="2026-07-27",
        project_root=tmp_path,
        minimum_members=1,
        maximum_members=10,
    )
    assert fingerprint not in scan_without_lineage.evidence["clearance_proofs"]


def test_explicit_v3_lineage_replays_its_recorded_parser_and_locator(tmp_path) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    issuer_id = "issuer-fdx"
    run, ingest_evidence, lineaged_row = _explicit_success_lineage(
        tmp_path,
        references=references,
        issuer_id=issuer_id,
        run_id="run-lineaged-v3-fdx",
        ingest_id=10,
        parser_version=COMPANYFACTS_NEXT_PARSER_VERSION,
    )
    evidence[run["run_id"]] = ingest_evidence
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[lineaged_row],
        warnings=warnings,
        evidence=evidence,
        successes=[run],
    )

    scan = scan_sec_fundamental_coverage(
        store=store,
        as_of="2026-07-27",
        project_root=tmp_path,
        minimum_members=1,
        maximum_members=10,
    )

    fingerprint = anomaly_fingerprint(
        rule_id="sec_fundamentals_coverage_missing",
        rule_version="1.0.0",
        scope="us-equity-reference:sp500",
        subject_type="issuer",
        subject_id=issuer_id,
    )
    proof = scan.evidence["clearance_proofs"][fingerprint]
    assert proof["companyfacts_snapshot"]["parser_version"] == (
        COMPANYFACTS_NEXT_PARSER_VERSION
    )
    assert lineaged_row["source_fact_locator"]


def test_success_outcome_cannot_hide_replayed_future_period_rejection(
    tmp_path,
) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    issuer_id = "issuer-fdx"
    run, ingest_evidence, lineaged_row = _explicit_success_lineage(
        tmp_path,
        references=references,
        issuer_id=issuer_id,
        run_id="run-contradictory-success-fdx",
        ingest_id=10,
        future_period_rejection=True,
    )
    evidence[run["run_id"]] = ingest_evidence
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[lineaged_row],
        warnings=warnings,
        evidence=evidence,
        successes=[run],
    )

    with pytest.raises(
        ValueError,
        match="rejection codes do not match exact replay",
    ):
        scan_sec_fundamental_coverage(
            store=store,
            as_of="2026-07-27",
            project_root=tmp_path,
            minimum_members=1,
            maximum_members=10,
        )


def test_explicit_lineage_rejects_extra_unlineaged_decision_visible_row(
    tmp_path,
) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    issuer_id = "issuer-fdx"
    run, ingest_evidence, lineaged_row = _explicit_success_lineage(
        tmp_path,
        references=references,
        issuer_id=issuer_id,
        run_id="run-lineaged-fdx",
        ingest_id=10,
    )
    evidence[run["run_id"]] = ingest_evidence
    extra_unlineaged = {
        **lineaged_row,
        "metric": "Assets",
        "value": 200.0,
        "ingest_run_id": None,
        "source_snapshot_id": None,
        "source_rowset_sha256": None,
        "source_row_sha256": None,
        "ingest_id": None,
        "ingest_rows_inserted": None,
        "ingest_finished_at": None,
        "ingest_status": None,
        "ingest_error": None,
    }
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[lineaged_row, extra_unlineaged],
        warnings=warnings,
        evidence=evidence,
        successes=[run],
    )

    with pytest.raises(
        ValueError,
        match="full decision-visible EDGAR rowset is not wholly accounted",
    ):
        scan_sec_fundamental_coverage(
            store=store,
            as_of="2026-07-27",
            project_root=tmp_path,
            minimum_members=1,
            maximum_members=10,
        )


def test_newer_corrupt_subject_run_cannot_fall_back_to_older_valid_lineage(
    tmp_path,
) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    issuer_id = "issuer-fdx"
    older_run, older_evidence, older_row = _explicit_success_lineage(
        tmp_path,
        references=references,
        issuer_id=issuer_id,
        run_id="run-valid-older-fdx",
        ingest_id=10,
    )
    newer_run, newer_evidence, _newer_row = _explicit_success_lineage(
        tmp_path,
        references=references,
        issuer_id=issuer_id,
        run_id="run-corrupt-newer-fdx",
        ingest_id=11,
    )
    newer_facts = next(
        row
        for row in newer_evidence["snapshots"]
        if row["role"] == "companyfacts"
    )
    newer_facts["parsed_rows_sha256"] = "0" * 64
    evidence[older_run["run_id"]] = older_evidence
    evidence[newer_run["run_id"]] = newer_evidence
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[older_row],
        warnings=warnings,
        evidence=evidence,
        successes=[older_run, newer_run],
    )

    with pytest.raises(
        ValueError,
        match=(
            "newest relevant SEC fundamentals ingest candidate is invalid.*"
            "run-corrupt-newer-fdx.*parsed-row proof"
        ),
    ):
        scan_sec_fundamental_coverage(
            store=store,
            as_of="2026-07-27",
            project_root=tmp_path,
            minimum_members=1,
            maximum_members=10,
        )


def test_newer_invalid_rejection_evidence_cannot_fall_back_to_older_lineage(
    tmp_path,
) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    issuer_id = "issuer-fdx"
    older_run, older_evidence, older_row = _explicit_success_lineage(
        tmp_path,
        references=references,
        issuer_id=issuer_id,
        run_id="run-valid-older-rejections-fdx",
        ingest_id=10,
    )
    newer_run, newer_evidence, _newer_row = _explicit_success_lineage(
        tmp_path,
        references=references,
        issuer_id=issuer_id,
        run_id="run-invalid-newer-rejections-fdx",
        ingest_id=11,
        future_period_rejection=True,
    )
    for outcome in (newer_run, newer_evidence):
        outcome["status"] = "warning"
        outcome["error"] = "withheld one future-period row"
        outcome["rejection_codes"] = '["unsupported_context"]'
    evidence[older_run["run_id"]] = older_evidence
    evidence[newer_run["run_id"]] = newer_evidence
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[older_row],
        warnings=warnings,
        evidence=evidence,
        successes=[older_run, newer_run],
    )

    with pytest.raises(
        ValueError,
        match=(
            "newest relevant SEC fundamentals ingest candidate is invalid.*"
            "run-invalid-newer-rejections-fdx.*rejection codes"
        ),
    ):
        scan_sec_fundamental_coverage(
            store=store,
            as_of="2026-07-27",
            project_root=tmp_path,
            minimum_members=1,
            maximum_members=10,
        )


def test_sec_coverage_scan_accepts_exact_legacy_rowset_without_backfill(
    tmp_path,
) -> None:
    members, references, warnings, evidence = _three_missing_issuer_evidence(tmp_path)
    issuer_id = "issuer-fdx"
    cik = references[issuer_id]["cik"]
    run_id = "run-legacy-fdx"
    payload = json.dumps(
        {
            "cik": int(cik),
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {
                            "USD": [
                                {
                                    "accn": "0001-26-000001",
                                    "start": "2026-01-01",
                                    "end": "2026-06-30",
                                    "filed": "2026-07-25",
                                    "fp": "Q2",
                                    "fy": 2026,
                                    "val": 100.0,
                                }
                            ]
                        }
                    }
                }
            },
        },
        sort_keys=True,
    ).encode()
    provider_row = parse_sec_companyfacts_response(payload)[0]
    facts = _raw_snapshot(
        tmp_path,
        run_id=run_id,
        role="companyfacts",
        dataset="companyfacts",
        payload=payload,
    )
    facts["parser_version"] = "sec-companyfacts-v1"
    submissions = _raw_snapshot(
        tmp_path,
        run_id=run_id,
        role="submissions",
        dataset="submissions",
        payload=json.dumps({"cik": cik, "name": "FedEx"}, sort_keys=True).encode(),
    )
    evidence[run_id] = {
        "id": 9,
        "run_id": run_id,
        "source": "edgar:issuer-cik-history",
        "table_name": "fundamentals",
        "subject_type": None,
        "subject_id": None,
        "rows_inserted": 1,
        "status": "success",
        "error": None,
        "snapshots": [facts, submissions],
    }
    successes = [
        {
            "id": 9,
            "run_id": run_id,
            "source": "edgar:issuer-cik-history",
            "subject_type": None,
            "subject_id": None,
            "rows_inserted": 1,
            "status": "success",
            "error": None,
        }
    ]
    stored = {
        key: value
        for key, value in {
            **provider_row,
            "ticker": "FDXF",
            "issuer_id": issuer_id,
            "security_id": "security-fdxf",
        }.items()
        if key != "cik"
    }
    store = _EvidenceStore(
        members=members,
        references=references,
        coverage=[],
        warnings=warnings,
        evidence=evidence,
        successes=successes,
        legacy_rows={issuer_id: [stored]},
    )

    scan = scan_sec_fundamental_coverage(
        store=store,
        as_of="2026-07-27",
        project_root=tmp_path,
        minimum_members=1,
        maximum_members=10,
    )

    assert scan.evidence["covered_issuers"] == 1
    assert scan.evidence["missing_issuers"] == 2
    assert {row.subject_id for row in scan.observations} == {
        "issuer-hon",
        "issuer-xom",
    }
    assert scan.evidence["clearance_proofs"] == {}
    assert all("ingest_run_id" not in row for row in store._legacy_rows[issuer_id])


def test_anomaly_fingerprint_changes_only_with_case_identity() -> None:
    values = {
        "rule_id": "coverage",
        "rule_version": "1.0.0",
        "scope": "us-equity-reference:sp500",
        "subject_type": "issuer",
        "subject_id": "issuer-1",
    }

    first = anomaly_fingerprint(**values)
    assert first == anomaly_fingerprint(**values)
    assert first != anomaly_fingerprint(**{**values, "subject_id": "issuer-2"})


def test_anomaly_case_lifecycle_is_idempotent_audited_and_evidence_locked(
    tmp_path,
) -> None:
    store = AlertStore(tmp_path / "operations.sqlite3")
    first_time = datetime(2026, 7, 29, 1, 0, tzinfo=UTC)
    first_scan = _case_scan("scan-1", first_time, marker="first")

    opened = _record_case_scan(store, first_scan)[0]
    repeated = _record_case_scan(store, first_scan)[0]

    assert opened.case_id == repeated.case_id
    assert repeated.state == "open"
    assert repeated.occurrence_count == 1
    assert [event["event_type"] for event in store.anomaly_case_events(opened.case_id)] == [
        "opened"
    ]

    with pytest.raises(ValueError, match="evidence changed after review"):
        store.acknowledge_anomaly(
            opened.case_id,
            owner="operator@example.test",
            note="Review started.",
            expected_evidence_sha256="0" * 64,
            now=first_time + timedelta(minutes=5),
        )
    acknowledged = store.acknowledge_anomaly(
        opened.case_id,
        owner="operator@example.test",
        note="Reviewed the source-bound comparison.",
        expected_evidence_sha256=opened.evidence_sha256,
        now=first_time + timedelta(minutes=5),
    )
    assert acknowledged.state == "acknowledged"

    changed = _record_case_scan(
        store,
        _case_scan("scan-2", first_time + timedelta(hours=1), marker="second")
    )[0]
    assert changed.state == "acknowledged"
    assert changed.owner == "operator@example.test"
    assert changed.occurrence_count == 2
    assert changed.evidence_sha256 != opened.evidence_sha256

    with pytest.raises(ValueError, match="evidence changed after review"):
        store.resolve_anomaly(
            changed.case_id,
            outcome="accepted",
            note="This uses stale evidence and must fail.",
            owner="operator@example.test",
            expected_evidence_sha256=opened.evidence_sha256,
            now=first_time + timedelta(hours=1, minutes=5),
        )
    resolved = store.resolve_anomaly(
        changed.case_id,
        outcome="false_positive",
        note="Current evidence reviewed and accepted as a known gap.",
        expected_evidence_sha256=changed.evidence_sha256,
        now=first_time + timedelta(hours=1, minutes=5),
    )
    assert resolved.state == "resolved"

    same_evidence = _record_case_scan(
        store,
        _case_scan("scan-3", first_time + timedelta(hours=2), marker="second")
    )[0]
    assert same_evidence.state == "resolved"
    assert same_evidence.occurrence_count == 2

    reopened = _record_case_scan(
        store,
        _case_scan("scan-4", first_time + timedelta(hours=3), marker="third")
    )[0]
    assert reopened.state == "open"
    assert reopened.owner is None
    assert reopened.occurrence_count == 3

    verification = _case_scan(
        "scan-5",
        first_time + timedelta(hours=4),
        marker="absent",
        include_observation=False,
    )
    assert _record_case_scan(store, verification) == ()
    corrected = store.resolve_anomaly(
        reopened.case_id,
        outcome="source_corrected",
        note="A later complete scan no longer detects the issuer.",
        owner="operator@example.test",
        expected_evidence_sha256=reopened.evidence_sha256,
        verification_scan_id=verification.scan_id,
        now=first_time + timedelta(hours=4, minutes=5),
    )
    assert corrected.state == "resolved"
    assert corrected.verification_scan_id == verification.scan_id
    assert [event["event_type"] for event in store.anomaly_case_events(opened.case_id)] == [
        "resolved",
        "reopened",
        "resolved",
        "evidence_changed",
        "acknowledged",
        "opened",
    ]


def test_anomaly_scan_and_event_history_are_append_only(tmp_path) -> None:
    path = tmp_path / "operations.sqlite3"
    store = AlertStore(path)
    recorded = _record_case_scan(
        store,
        _case_scan("scan-append-only", datetime(2026, 7, 29, 1, tzinfo=UTC))
    )[0]
    event_id = store.anomaly_case_events(recorded.case_id)[0]["event_id"]

    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE anomaly_scans SET scope = scope WHERE scan_id = ?",
                ("scan-append-only",),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM anomaly_case_events WHERE event_id = ?",
                (event_id,),
            )
    finally:
        connection.close()


def _case_scan(
    scan_id: str,
    source_boundary_at: datetime,
    *,
    marker: str = "first",
    include_observation: bool = True,
) -> AnomalyScan:
    scope = "us-equity-reference:sp500"
    fingerprint = anomaly_fingerprint(
        rule_id="sec_fundamentals_coverage_missing",
        rule_version="1.0.0",
        scope=scope,
        subject_type="issuer",
        subject_id="issuer-test",
    )
    observations = (
        (
            AnomalyObservation(
                fingerprint=fingerprint,
                rule_id="sec_fundamentals_coverage_missing",
                rule_version="1.0.0",
                scope=scope,
                subject_type="issuer",
                subject_id="issuer-test",
                severity="medium",
                confidence="high",
                title="SEC fundamentals pending for TEST",
                summary="No accepted Company Facts rows are available.",
                old_value={"expected_state": "covered"},
                new_value={"coverage_state": "missing", "marker": marker},
                evidence={"marker": marker},
                suggested_checks=("Inspect the exact SEC snapshots.",),
            ),
        )
        if include_observation
        else ()
    )
    boundary = hashlib.sha256(f"{scan_id}:{marker}".encode()).hexdigest()
    evidence: dict[str, object] = {"marker": marker}
    if not include_observation:
        proof_body = {
            "rule_id": "sec_fundamentals_coverage_missing",
            "rule_version": "1.0.0",
            "scope": scope,
            "subject_type": "issuer",
            "subject_id": "issuer-test",
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
            fingerprint: {**proof_body, "proof_sha256": proof_sha256}
        }
    return AnomalyScan(
        scan_id=scan_id,
        rule_bundle_version="us-equity-data-quality.v1",
        scope=scope,
        source_boundary_sha256=boundary,
        source_boundary_at=source_boundary_at,
        executed_rules=("sec_fundamentals_coverage_missing@1.0.0",),
        observations=observations,
        evidence=evidence,
    )


def _record_case_scan(
    store: AlertStore,
    scan: AnomalyScan,
):
    assert isinstance(scan.source_boundary_at, datetime)
    return store.record_anomaly_scan(scan, now=scan.source_boundary_at)


def _three_missing_issuer_evidence(
    root: Path,
) -> tuple[list[dict], dict[str, dict], list[dict], dict[str, dict]]:
    identities = [
        ("issuer-fdx", "0002082247", "FDXF", "FedEx Freight Holding Company, Inc."),
        ("issuer-hon", "0002089271", "HONA", "Honeywell Aerospace Inc."),
        ("issuer-xom", "0002115436", "XOM", "ExxonMobil Holdings Corporation"),
    ]
    members = [
        {
            "ticker": ticker,
            "security_id": f"security-{ticker.lower()}",
            "issuer_id": issuer_id,
            "canonical_name": name,
        }
        for issuer_id, _cik, ticker, name in identities
    ]
    references = {
        issuer_id: {
            "issuer_id": issuer_id,
            "canonical_name": name,
            "canonical_ticker": ticker,
            "cik": cik,
            "cik_source": f"https://data.sec.gov/submissions/CIK{cik}.json",
            "verified_date": "2026-07-21",
        }
        for issuer_id, cik, ticker, name in identities
    }
    warnings: list[dict] = []
    evidence: dict[str, dict] = {}
    for index, (issuer_id, cik, ticker, name) in enumerate(identities, 1):
        run_id = f"run-{index}"
        finished_at = f"2026-07-25 13:50:3{index}.000000"
        warnings.append(
            {
                "id": index,
                "run_id": run_id,
                "source": "edgar:issuer-cik-history",
                "table_name": "fundamentals",
                "rows_inserted": 0,
                "rows_rejected": 0,
                "started_at": finished_at,
                "finished_at": finished_at,
                "status": "warning",
                "error": "SEC returned no fundamental rows",
            }
        )
        companyfacts = _raw_snapshot(
            root,
            run_id=run_id,
            role="companyfacts",
            dataset="companyfacts",
            payload=json.dumps(
                {"cik": int(cik), "entityName": name, "facts": {}},
                sort_keys=True,
            ).encode(),
        )
        submissions = _raw_snapshot(
            root,
            run_id=run_id,
            role="submissions",
            dataset="submissions",
            payload=json.dumps(
                {"cik": cik, "name": name, "tickers": [ticker]},
                sort_keys=True,
            ).encode(),
        )
        evidence[run_id] = {
            "id": index,
            "run_id": run_id,
            "source": "edgar:issuer-cik-history",
            "table_name": "fundamentals",
            "subject_type": "issuer" if index != 1 else None,
            "subject_id": issuer_id if index != 1 else None,
            "rows_inserted": 0,
            "rows_rejected": 0,
            "started_at": finished_at,
            "finished_at": finished_at,
            "status": "warning",
            "error": "SEC returned no fundamental rows",
            "snapshots": [companyfacts, submissions],
        }
    return members, references, warnings, evidence


def _replace_warning_with_future_only_companyfacts(
    root: Path,
    *,
    references: dict[str, dict],
    warnings: list[dict],
    evidence: dict[str, dict],
    parser_version: str,
) -> str:
    warning = warnings[1]
    run_id = warning["run_id"]
    cik = references[evidence[run_id]["subject_id"]]["cik"]
    future_payload = json.dumps(
        {
            "cik": int(cik),
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {
                            "USD": [
                                {
                                    "accn": "0001-26-000001",
                                    "start": "2026-07-01",
                                    "end": "2026-09-30",
                                    "filed": "2026-07-25",
                                    "fp": "Q3",
                                    "fy": 2026,
                                    "val": 100.0,
                                }
                            ]
                        }
                    }
                }
            },
        },
        sort_keys=True,
    ).encode()
    replacement = _raw_snapshot(
        root,
        run_id=run_id,
        role="companyfacts",
        dataset="companyfacts",
        payload=future_payload,
    )
    replacement["parser_version"] = parser_version
    replacement["parsed_row_count"] = 0
    replacement["parsed_rows_sha256"] = canonical_parsed_rows_sha256([])
    evidence[run_id]["snapshots"] = [
        replacement if row["role"] == "companyfacts" else row
        for row in evidence[run_id]["snapshots"]
    ]
    code = (
        "future_period"
        if parser_version == COMPANYFACTS_PARSER_VERSION
        else "unsupported_context"
    )
    detail = (
        "rejected 1 row(s) with period_end after filing date"
        if code == "future_period"
        else "withheld 1 unsupported SEC fact context row(s)"
    )
    for outcome in (warning, evidence[run_id]):
        outcome["rows_rejected"] = 1
        outcome["rejection_codes"] = f'["{code}"]'
        outcome["error"] = detail
    return run_id


def _explicit_success_lineage(
    root: Path,
    *,
    references: dict[str, dict],
    issuer_id: str,
    run_id: str,
    ingest_id: int,
    parser_version: str = COMPANYFACTS_PARSER_VERSION,
    future_period_rejection: bool = False,
) -> tuple[dict, dict, dict]:
    cik = references[issuer_id]["cik"]
    fact_rows = [
        {
            "accn": f"0001-26-{ingest_id:06d}",
            "start": "2026-01-01",
            "end": "2026-06-30",
            "filed": "2026-07-25",
            "form": "10-Q",
            "fp": "Q2",
            "fy": 2026,
            "val": 100.0,
        }
    ]
    if future_period_rejection:
        fact_rows.append(
            {
                "accn": f"0001-26-{ingest_id + 1:06d}",
                "start": "2026-07-01",
                "end": "2026-09-30",
                "filed": "2026-07-25",
                "form": "10-Q",
                "fp": "Q3",
                "fy": 2026,
                "val": 999.0,
            }
        )
    payload = json.dumps(
        {
            "cik": int(cik),
            "entityName": references[issuer_id]["canonical_name"],
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {
                            "USD": fact_rows
                        }
                    }
                }
            },
        },
        sort_keys=True,
    ).encode()
    parser = (
        parse_sec_companyfacts_response_v3
        if parser_version == COMPANYFACTS_NEXT_PARSER_VERSION
        else parse_sec_companyfacts_response
    )
    provider_row = parser(payload)[0]
    facts = _raw_snapshot(
        root,
        run_id=run_id,
        role="companyfacts",
        dataset="companyfacts",
        payload=payload,
    )
    facts["parser_version"] = parser_version
    facts["parsed_row_count"] = 1
    facts["parsed_rows_sha256"] = canonical_parsed_rows_sha256([provider_row])
    submissions = _raw_snapshot(
        root,
        run_id=run_id,
        role="submissions",
        dataset="submissions",
        payload=json.dumps(
            {
                "cik": cik,
                "name": references[issuer_id]["canonical_name"],
                "tickers": ["FDXF"],
            },
            sort_keys=True,
        ).encode(),
    )
    run = {
        "id": ingest_id,
        "run_id": run_id,
        "source": "edgar:issuer-cik-history",
        "subject_type": "issuer",
        "subject_id": issuer_id,
        "rows_inserted": 1,
        "rows_rejected": 1 if future_period_rejection else 0,
        "started_at": f"2026-07-26 10:00:{ingest_id:02d}",
        "finished_at": f"2026-07-26 10:00:{ingest_id:02d}",
        "status": "success",
        "error": None,
    }
    ingest_evidence = {
        **run,
        "table_name": "fundamentals",
        "snapshots": [facts, submissions],
    }
    lineaged_row = {
        "ticker": "FDXF",
        "issuer_id": issuer_id,
        "security_id": "security-fdxf",
        "period_end": provider_row["period_end"],
        "as_of_date": provider_row["as_of_date"],
        "fiscal_period": provider_row["fiscal_period"],
        "statement": provider_row["statement"],
        "metric": provider_row["metric"],
        "value": provider_row["value"],
        "quarter_value": provider_row["quarter_value"],
        "unit": provider_row["unit"],
        "source": "edgar",
        "ingest_run_id": run_id,
        "source_snapshot_id": facts["snapshot_id"],
        "source_rowset_sha256": facts["parsed_rows_sha256"],
        "source_row_sha256": canonical_sec_fundamental_row_sha256(provider_row),
        "source_fact_locator": provider_row.get("source_fact_locator"),
        "ingest_id": ingest_id,
        "ingest_rows_inserted": 1,
        "ingest_finished_at": run["finished_at"],
        "ingest_status": "success",
        "ingest_error": None,
    }
    return run, ingest_evidence, lineaged_row


def _raw_snapshot(
    root: Path,
    *,
    run_id: str,
    role: str,
    dataset: str,
    payload: bytes,
) -> dict:
    digest = hashlib.sha256(payload).hexdigest()
    compressed = gzip.compress(payload, mtime=0)
    relative = (
        Path("data")
        / "raw"
        / "sec-edgar"
        / dataset
        / "2026-07-25"
        / f"{digest}.json.gz"
    )
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(compressed)
    return {
        "run_id": run_id,
        "snapshot_id": f"raw-{run_id}-{role}",
        "role": role,
        "provider": "sec-edgar",
        "dataset": dataset,
        "artifact_kind": "exact_response",
        "http_status": 200,
        "requested_at": "2026-07-25 13:50:00",
        "received_at": "2026-07-25 13:50:01",
        "payload_sha256": digest,
        "relative_path": relative.as_posix(),
        "original_bytes": len(payload),
        "stored_bytes": len(compressed),
        "compression": "gzip",
        "adapter_name": "sec-edgar",
        "adapter_version": "1",
        "parser_version": (
            COMPANYFACTS_CAPTURE_PARSER_VERSION
            if dataset == "companyfacts"
            else "legacy"
        ),
        "parsed_row_count": None,
        "parsed_rows_sha256": None,
    }

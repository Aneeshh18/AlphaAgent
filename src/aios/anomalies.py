"""Evidence-bound data-quality detectors for the governed review ledger."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import stat
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from aios.alerts import (
    SEC_SOURCE_BOUNDARY_POLICY_V1_TO_V2,
    SEC_SOURCE_BOUNDARY_POLICY_V2,
    AnomalyObservation,
    AnomalyScan,
)
from aios.alerts import (
    canonical_anomaly_fingerprint as anomaly_fingerprint,
)
from aios.artifacts import publish_text_write_once
from aios.canonical import canonical_json
from aios.config import settings
from aios.ingest.edgar import (
    COMPANYFACTS_CAPTURE_PARSER_VERSION,
    COMPANYFACTS_LEGACY_PARSER_VERSION,
    COMPANYFACTS_NEXT_PARSER_VERSION,
    COMPANYFACTS_PARSER_VERSION,
    COMPANYFACTS_STORAGE_SAFE_V1_PARSER_VERSION,
    canonical_sec_fundamental_row_sha256,
    replay_sec_companyfacts_response,
)
from aios.raw_snapshots import canonical_parsed_rows_sha256
from aios.sec_rejections import (
    accepted_sec_fundamental_outcome,
    decode_rejection_codes,
)
from aios.storage.store import Store

RULE_BUNDLE_VERSION = "us-equity-data-quality.v1"
SEC_RULE_ID = "sec_fundamentals_coverage_missing"
SEC_RULE_VERSION = "1.0.0"
SEC_SCOPE_PREFIX = "us-equity-reference"

# Later rule families use their own scope namespace. The ledger enforces a
# monotonic source boundary per case, and ``rule_bundle_version`` plus the
# single-rule ``executed_rules`` tuple are pinned by the shipped ledger
# contract, so a separate scope is how a new family is added without changing
# either.
PRICE_RULE_ID = "price_action_mismatch"
PRICE_RULE_VERSION = "1.0.0"
PRICE_SCOPE_PREFIX = "us-equity-prices"
PRICE_LOOKBACK_SESSIONS = 30
PRICE_MOVE_THRESHOLD = 0.25

COVERAGE_RULE_ID = "coverage_deterioration"
COVERAGE_RULE_VERSION = "1.0.0"
COVERAGE_SCOPE_PREFIX = "us-equity-coverage"

MAPPING_RULE_ID = "mapping_drift"
MAPPING_RULE_VERSION = "1.0.0"
MAPPING_SCOPE_PREFIX = "us-equity-mappings"

SHARES_RULE_ID = "share_count_jump"
SHARES_RULE_VERSION = "1.0.0"
SHARES_SCOPE_PREFIX = "us-equity-shares"
SHARES_METRIC = "shares_out"
SHARES_JUMP_THRESHOLD = 0.30
# Bound the review window the way the price rule bounds its sessions. The
# retained filing history reaches back to 2008, and scanning all of it produced
# 750 findings for one decision date — mostly historical restatements that are
# known audit debt. That scan cannot be recorded at all: the ledger caps scan
# evidence at 64 KiB, which the per-observation manifest passes at roughly 500
# findings, and `alerts.py` is inside the frozen policy bundle so the cap
# cannot be raised. A recent window keeps the daily detector operable; pass a
# larger value deliberately for a one-off historical sweep.
SHARES_LOOKBACK_FILINGS = 8

# The operations ledger accepts exactly {"low", "medium", "high"} and rejects
# any other confidence, so a descriptive label cannot be stored directly. These
# constants keep WHY each rule is confident visible at the call site while
# recording the level the ledger actually understands.
#
# Both sides of the comparison are values this system recorded exactly, so the
# contradiction is certain even though its cause is not.
CONFIDENCE_EXACT_COMPARISON = "high"
# The finding depends on a value a provider reported, which may itself be the
# thing that is wrong.
CONFIDENCE_REPORTED_SOURCE = "medium"

FACTOR_RULE_ID = "factor_percentile_jump"
FACTOR_RULE_VERSION = "1.0.0"
FACTOR_SCOPE_PREFIX = "us-equity-factors"
# Composite scores are published on a 0-100 percentile scale, so the threshold
# is in percentile points, not a fraction.
FACTOR_JUMP_THRESHOLD = 40.0
FACTOR_SUPPORTED_MODELS = ("qv", "qvml")

FILINGS_RULE_ID = "conflicting_filings"
FILINGS_RULE_VERSION = "1.0.0"
FILINGS_SCOPE_PREFIX = "us-equity-filings"
SEC_ZERO_ROW_ERROR = "SEC returned no fundamental rows"
MAX_WARNING_RUNS = 5_000
MAX_SEC_SNAPSHOT_STORED_BYTES = 64 * 1024 * 1024
MAX_SEC_SNAPSHOT_ORIGINAL_BYTES = 256 * 1024 * 1024
SEC_FILING_STAGE_CONTEXT_VERSION = "sec-filing-stage-context.v1"
SEC_PERIODIC_FORM_BASES = frozenset(
    {
        "10-K",
        "10-Q",
        "20-F",
        "40-F",
    }
)
SEC_REGISTRATION_FORM_BASES = frozenset(
    {
        "10",
        "10-12B",
        "10-12G",
        "DRS",
        "F-1",
        "F-3",
        "F-4",
        "F-6",
        "F-10",
        "S-1",
        "S-3",
        "S-4",
        "S-8",
    }
)
SEC_REGISTRATION_POST_EFFECTIVE_FORMS = frozenset(
    {
        "POS AM",
        "POSASR",
        "S-8 POS",
    }
)


def scan_sec_fundamental_coverage(
    *,
    store: Store,
    as_of: date | str,
    project_root: Path | None = None,
    universe_id: str = "sp500",
    minimum_members: int = 450,
    maximum_members: int = 550,
) -> AnomalyScan:
    """Compare reviewed issuers with accepted SEC fundamentals.

    Detection is all-or-nothing. Every missing issuer must bind to one reviewed
    identity, one ingest run, and checksum-verified exact provider snapshots.
    The function is read-only and never repairs DuckDB or changes readiness.
    """

    decision_date = _as_date(as_of)
    if minimum_members < 1 or maximum_members < minimum_members:
        raise ValueError("invalid anomaly-scan universe bounds")
    root = (project_root or settings.project_root).resolve()
    scope = f"{SEC_SCOPE_PREFIX}:{universe_id}"

    members = store.universe_identity_labels(universe_id, decision_date)
    if not minimum_members <= len(members) <= maximum_members:
        raise ValueError(
            f"{universe_id} has {len(members)} reviewed members on {decision_date}; "
            f"expected {minimum_members}-{maximum_members}"
        )

    issuer_members: dict[str, dict[str, Any]] = {}
    issuer_by_cik: dict[str, str] = {}
    member_boundary: list[dict[str, str]] = []
    for member in members:
        issuer_id = _text(member, "issuer_id", "reviewed universe member")
        security_id = _text(member, "security_id", "reviewed universe member")
        ticker = _text(member, "ticker", "reviewed universe member").upper()
        active_issuer_id = store.issuer_id_for_security(
            security_id,
            decision_date,
        )
        if active_issuer_id != issuer_id:
            raise ValueError(
                "reviewed universe identity disagrees with the active "
                f"security owner: {security_id}@{decision_date}"
            )
        reference = store.issuer_reference(issuer_id, as_of=decision_date)
        if reference is None:
            raise ValueError(f"reviewed issuer reference is missing: {issuer_id}")
        cik = _cik(reference.get("cik"))
        normalized = {
            "issuer_id": issuer_id,
            "security_id": security_id,
            "ticker": ticker,
            "canonical_name": _text(reference, "canonical_name", issuer_id),
            "canonical_ticker": _text(
                reference,
                "canonical_ticker",
                issuer_id,
            ).upper(),
            "cik": cik,
            "cik_source": _text(reference, "cik_source", issuer_id),
            "verified_date": str(reference.get("verified_date") or ""),
        }
        prior = issuer_members.get(issuer_id)
        if prior is not None and prior["cik"] != cik:
            raise ValueError(f"active issuer has conflicting SEC CIKs: {issuer_id}")
        prior_cik_owner = issuer_by_cik.get(cik)
        if prior_cik_owner is not None and prior_cik_owner != issuer_id:
            raise ValueError(
                f"reviewed issuers share one active SEC CIK: {cik} ({prior_cik_owner}, {issuer_id})"
            )
        issuer_by_cik[cik] = issuer_id
        issuer_members[issuer_id] = normalized
        member_boundary.append(
            {
                "ticker": ticker,
                "security_id": security_id,
                "issuer_id": issuer_id,
                "cik": cik,
            }
        )

    accepted, coverage_snapshot_refs = _verified_sec_fundamental_coverage(
        store=store,
        root=root,
        issuer_members=issuer_members,
        decision_date=decision_date,
    )
    missing = sorted(
        issuer_id
        for issuer_id in issuer_members
        if int(accepted.get(issuer_id, {}).get("accepted_rows", 0)) == 0
    )

    ingest_columns = {
        row["column_name"]
        for row in store.query(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'main' AND table_name = 'ingest_log'
            """
        )
    }
    warning_rejection_projection = (
        "rejection_codes"
        if "rejection_codes" in ingest_columns
        else "NULL AS rejection_codes"
    )
    warning_runs = store.query(
        f"""
        SELECT id, run_id, source, table_name, rows_inserted, rows_rejected,
               started_at, finished_at, status, error,
               {warning_rejection_projection}
        FROM ingest_log
        WHERE table_name = 'fundamentals'
          AND status = 'warning'
          AND rows_inserted = 0
          AND source LIKE 'edgar:%'
        ORDER BY finished_at DESC, id DESC
        LIMIT {MAX_WARNING_RUNS}
        """
    )
    latest_ingest = store.query(
        """
        SELECT MAX(finished_at) AS latest_finished_at
        FROM ingest_log
        WHERE table_name = 'fundamentals' AND source LIKE 'edgar:%'
        """
    )[0].get("latest_finished_at")
    if latest_ingest is None:
        raise ValueError("no SEC fundamentals ingest boundary is available")
    cik_to_issuer = {
        str(reference["cik"]): issuer_id for issuer_id, reference in issuer_members.items()
    }
    latest_by_issuer: dict[str, dict[str, Any]] = {}
    filing_index_by_run: dict[str, dict[str, Any]] = {}
    legacy_evidence_errors: list[str] = []
    for warning in warning_runs:
        run_id = _text(warning, "run_id", "SEC ingest warning")
        ingest = store.ingest_evidence(run_id)
        if ingest is None:
            legacy_evidence_errors.append(f"{run_id}: no ingest evidence")
            continue
        subject_type = _optional(ingest.get("subject_type"))
        subject_id = _optional(ingest.get("subject_id"))
        if (subject_type is None) != (subject_id is None):
            raise ValueError(f"SEC ingest has incomplete subject identity: {run_id}")
        snapshots = ingest.get("snapshots")
        if not isinstance(snapshots, list):
            if subject_type == "issuer" and subject_id in missing:
                raise ValueError(f"current issuer has invalid snapshots: {run_id}")
            continue
        facts = [
            row for row in snapshots if isinstance(row, dict) and row.get("role") == "companyfacts"
        ]
        submissions = [
            row for row in snapshots if isinstance(row, dict) and row.get("role") == "submissions"
        ]
        if len(facts) != 1 or len(submissions) != 1:
            if subject_type == "issuer" and subject_id in missing:
                raise ValueError(
                    "current issuer requires exactly one Company Facts and one "
                    f"Submissions snapshot: {run_id}"
                )
            continue
        try:
            provider_payload = _read_sec_snapshot(
                facts[0],
                root,
                dataset="companyfacts",
                label="Company Facts",
            )
            submissions_payload = _read_sec_snapshot(
                submissions[0],
                root,
                dataset="submissions",
                label="Submissions",
            )
        except ValueError as exc:
            if subject_type == "issuer" and subject_id in missing:
                raise
            legacy_evidence_errors.append(f"{run_id}: {exc}")
            continue
        payload_cik = _cik(provider_payload.get("cik"))
        submissions_cik = _cik(submissions_payload.get("cik"))
        if submissions_cik != payload_cik:
            raise ValueError(f"SEC Company Facts and Submissions CIKs disagree: {run_id}")
        if subject_type is None:
            issuer_id = cik_to_issuer.get(payload_cik)
            binding = "legacy_exact_payload_cik"
        elif subject_type == "issuer":
            issuer_id = subject_id
            binding = "subject_tagged_and_payload_verified"
        else:
            continue
        if issuer_id not in issuer_members:
            continue
        if issuer_members[issuer_id]["cik"] != payload_cik:
            if issuer_id in missing:
                raise ValueError(
                    f"SEC payload CIK does not match reviewed issuer {issuer_id}: {run_id}"
                )
            legacy_evidence_errors.append(f"{run_id}: payload CIK does not match {issuer_id}")
            continue
        try:
            replay_proof = _sec_zero_row_replay_proof(
                payload=provider_payload,
                snapshot=_snapshot_reference(facts[0]),
                decision_date=decision_date,
            )
        except ValueError:
            if issuer_id in missing:
                raise
            continue
        try:
            outcome_proof = _sec_zero_row_outcome_proof(
                warning=warning,
                ingest=ingest,
                replay_proof=replay_proof,
            )
        except ValueError as exc:
            if issuer_id in missing:
                raise
            legacy_evidence_errors.append(f"{run_id}: {exc}")
            continue
        source_snapshots = [
            _snapshot_reference(facts[0]),
            _snapshot_reference(submissions[0]),
        ]
        if issuer_id in missing:
            filing_index_by_run[run_id] = _sec_submissions_filing_index(
                payload=submissions_payload,
                snapshot=source_snapshots[1],
                decision_date=decision_date,
            )
        candidate = {
            "ingest_id": int(warning["id"]),
            "run_id": run_id,
            "source": _text(warning, "source", "SEC ingest warning"),
            "started_at": str(warning.get("started_at") or ""),
            "finished_at": str(warning.get("finished_at") or ""),
            "rows_inserted": int(warning.get("rows_inserted") or 0),
            "rows_rejected": int(warning.get("rows_rejected") or 0),
            "error": _text(warning, "error", "SEC ingest warning"),
            "payload_cik": payload_cik,
            "payload_entity_name": _optional(provider_payload.get("entityName")),
            "submissions_entity_name": _optional(submissions_payload.get("name")),
            "identity_binding": binding,
            "zero_row_outcome_proof": outcome_proof,
            "zero_row_replay_proof": replay_proof,
            "snapshots": source_snapshots,
        }
        prior = latest_by_issuer.get(issuer_id)
        if prior is None or candidate["finished_at"] > prior["finished_at"]:
            latest_by_issuer[issuer_id] = candidate

    unsupported = [issuer_id for issuer_id in missing if issuer_id not in latest_by_issuer]
    if unsupported:
        if legacy_evidence_errors:
            raise ValueError(
                "exact SEC warning evidence could not be verified: " + legacy_evidence_errors[0]
            )
        raise ValueError(
            "missing issuers cannot be tied to exact SEC warning evidence: "
            + ", ".join(unsupported[:10])
        )

    filing_stage_by_issuer: dict[str, dict[str, Any]] = {}
    filing_stage_snapshot_refs: list[dict[str, Any]] = []
    for issuer_id in missing:
        warning = latest_by_issuer[issuer_id]
        filing_index = filing_index_by_run.get(warning["run_id"])
        if filing_index is None:
            raise ValueError(
                f"selected SEC warning lacks its verified filing-stage context: {warning['run_id']}"
            )
        owner_context, predecessor_snapshot_refs = _reviewed_owner_filing_context(
            store=store,
            root=root,
            security_id=issuer_members[issuer_id]["security_id"],
            active_issuer_id=issuer_id,
            decision_date=decision_date,
        )
        filing_stage_body = {
            "context_version": SEC_FILING_STAGE_CONTEXT_VERSION,
            "decision_evidence_as_of": decision_date.isoformat(),
            "submissions_filing_index": filing_index,
            "reviewed_security_owner": owner_context,
            "policy": {
                "future_filing_dates_excluded": True,
                "predecessor_facts_are_context_only": True,
                "predecessor_facts_transfer_to_active_issuer": False,
                "data_repairs": 0,
                "readiness_overrides": 0,
            },
        }
        filing_stage_by_issuer[issuer_id] = {
            **filing_stage_body,
            "proof_sha256": _sha(filing_stage_body),
        }
        filing_stage_snapshot_refs.extend(predecessor_snapshot_refs)

    warning_snapshot_refs = [
        snapshot for issuer_id in missing for snapshot in latest_by_issuer[issuer_id]["snapshots"]
    ]
    used_snapshot_refs = _deduplicate_snapshot_references(
        coverage_snapshot_refs + warning_snapshot_refs + filing_stage_snapshot_refs
    )
    if not used_snapshot_refs:
        raise ValueError("no exact SEC evidence was consumed by the coverage scan")
    source_boundary_at = max(
        _utc_raw_snapshot_time(snapshot["received_at"]) for snapshot in used_snapshot_refs
    )

    clearance_proofs = _sec_clearance_proofs(
        scope=scope,
        issuer_members=issuer_members,
        accepted=accepted,
        warning_by_issuer=latest_by_issuer,
    )
    reviewed_member_count = len(members)
    reviewed_issuer_count = len(issuer_members)
    covered_count = reviewed_issuer_count - len(missing)
    coverage_rate = covered_count / reviewed_issuer_count
    observations: list[AnomalyObservation] = []
    for issuer_id in missing:
        issuer = issuer_members[issuer_id]
        warning = latest_by_issuer[issuer_id]
        facts = next(row for row in warning["snapshots"] if row["role"] == "companyfacts")
        confidence = "high" if facts["parsed_rows_sha256"] else "medium"
        evidence = {
            "detected_as_of": decision_date.isoformat(),
            "universe_id": universe_id,
            "reviewed_universe_members": reviewed_member_count,
            "reviewed_universe_issuers": reviewed_issuer_count,
            "covered_issuers": covered_count,
            "missing_issuers": len(missing),
            "coverage_rate": coverage_rate,
            "issuer": issuer,
            "ingest": warning,
            "filing_stage": filing_stage_by_issuer[issuer_id],
            "provenance_quality": (
                "complete"
                if confidence == "high"
                else "legacy_snapshot_without_attached_parse_hash"
            ),
        }
        observations.append(
            AnomalyObservation(
                fingerprint=anomaly_fingerprint(
                    rule_id=SEC_RULE_ID,
                    rule_version=SEC_RULE_VERSION,
                    scope=scope,
                    subject_type="issuer",
                    subject_id=issuer_id,
                ),
                rule_id=SEC_RULE_ID,
                rule_version=SEC_RULE_VERSION,
                scope=scope,
                subject_type="issuer",
                subject_id=issuer_id,
                severity="medium",
                confidence=confidence,
                title=f"SEC fundamentals pending for {issuer['canonical_ticker']}",
                summary=(
                    "This reviewed issuer has no accepted Company Facts rows. "
                    "Its research score stays withheld until evidence is reviewed."
                ),
                old_value={
                    "expected_state": "covered",
                    "minimum_accepted_rows": 1,
                },
                new_value={
                    "coverage_state": "missing",
                    "accepted_rows": 0,
                    "latest_ingest_rows_inserted": warning["rows_inserted"],
                },
                evidence=evidence,
                suggested_checks=(
                    "Confirm the reviewed issuer-to-CIK mapping against the SEC filing.",
                    "Inspect the exact Company Facts and Submissions snapshots.",
                    "Check whether this is a pre-filing or newly public issuer.",
                    "Refresh only after new SEC evidence; never fabricate or backfill facts.",
                ),
            )
        )

    member_hash = _sha(sorted(member_boundary, key=lambda row: (row["ticker"], row["security_id"])))
    coverage_hash = _sha(
        [
            {
                "issuer_id": issuer_id,
                **accepted.get(
                    issuer_id,
                    {
                        "accepted_rows": 0,
                        "first_as_of_date": None,
                        "latest_as_of_date": None,
                    },
                ),
            }
            for issuer_id in sorted(issuer_members)
        ]
    )
    observation_boundary = [
        {
            "fingerprint": row.fingerprint,
            "evidence_sha256": _sha(
                {
                    "old_value": row.old_value,
                    "new_value": row.new_value,
                    "evidence": row.evidence,
                }
            ),
        }
        for row in observations
    ]
    used_snapshot_boundary = sorted(
        (
            {
                "snapshot_id": row["snapshot_id"],
                "payload_sha256": row["payload_sha256"],
                "received_at": row["received_at"],
            }
            for row in used_snapshot_refs
        ),
        key=lambda row: (row["snapshot_id"], row["payload_sha256"]),
    )
    used_snapshot_set_sha256 = _sha(used_snapshot_boundary)
    source_boundary_proof = {
        "used_snapshot_count": len(used_snapshot_boundary),
        "used_snapshot_set_sha256": used_snapshot_set_sha256,
        "maximum_received_at": source_boundary_at.isoformat(),
    }
    boundary = {
        "rule_bundle_version": RULE_BUNDLE_VERSION,
        "scope": scope,
        "decision_evidence_as_of": decision_date.isoformat(),
        "evidence_observed_through": source_boundary_at.isoformat(),
        "temporal_mode": "retrospective_review_no_backfill",
        "member_set_sha256": member_hash,
        "coverage_set_sha256": coverage_hash,
        "clearance_proofs_sha256": _sha(clearance_proofs),
        "used_snapshots": used_snapshot_boundary,
        "observations": observation_boundary,
    }
    boundary_hash = _sha(boundary)
    return AnomalyScan(
        scan_id=f"dqs-{boundary_hash[:32]}",
        rule_bundle_version=RULE_BUNDLE_VERSION,
        scope=scope,
        source_boundary_sha256=boundary_hash,
        source_boundary_at=source_boundary_at,
        executed_rules=(f"{SEC_RULE_ID}@{SEC_RULE_VERSION}",),
        observations=tuple(observations),
        evidence={
            "as_of": decision_date.isoformat(),
            "decision_evidence_as_of": decision_date.isoformat(),
            "evidence_observed_through": source_boundary_at.isoformat(),
            "temporal_mode": "retrospective_review_no_backfill",
            "universe_id": universe_id,
            "executed_rules": [f"{SEC_RULE_ID}@{SEC_RULE_VERSION}"],
            "reviewed_members": reviewed_member_count,
            "reviewed_issuers": reviewed_issuer_count,
            "covered_issuers": covered_count,
            "missing_issuers": len(missing),
            "coverage_rate": coverage_rate,
            "member_set_sha256": member_hash,
            "coverage_set_sha256": coverage_hash,
            "clearance_proofs": clearance_proofs,
            "clearance_proof_count": len(clearance_proofs),
            "used_snapshot_count": len(used_snapshot_refs),
            "latest_sec_ingest_finished_at_local_legacy": str(latest_ingest),
            "ingest_log_timestamp_basis": "legacy_host_local_not_used_for_ordering",
            "source_boundary_basis": ("max_utc_received_at_of_snapshots_consumed_by_scan"),
            "source_boundary_policy": SEC_SOURCE_BOUNDARY_POLICY_V2,
            "source_boundary_policy_transition": (SEC_SOURCE_BOUNDARY_POLICY_V1_TO_V2),
            "source_boundary_proof": source_boundary_proof,
            "safety": {
                "data_repairs": 0,
                "readiness_overrides": 0,
                "paper_actions": 0,
                "broker_actions": 0,
            },
        },
    )


def _sec_zero_row_outcome_proof(
    *,
    warning: dict[str, Any],
    ingest: dict[str, Any],
    replay_proof: dict[str, Any],
) -> dict[str, Any]:
    """Prove that the selected warning and its full ingest evidence agree."""
    run_id = _text(warning, "run_id", "SEC ingest warning")
    source = _text(warning, "source", "SEC ingest warning")
    expected_rows_rejected = int(replay_proof["rows_rejected"])
    warning_outcome = {
        "run_id": run_id,
        "source": source,
        "table_name": _text(warning, "table_name", "SEC ingest warning"),
        "rows_inserted": _exact_zero_count(
            warning.get("rows_inserted"),
            field="rows_inserted",
            run_id=run_id,
        ),
        "rows_rejected": warning.get("rows_rejected"),
        "status": _text(warning, "status", "SEC ingest warning"),
        "error": _text(warning, "error", "SEC ingest warning"),
        "rejection_codes": warning.get("rejection_codes"),
    }
    ingest_outcome = {
        "run_id": _text(ingest, "run_id", "SEC ingest evidence"),
        "source": _text(ingest, "source", "SEC ingest evidence"),
        "table_name": _text(ingest, "table_name", "SEC ingest evidence"),
        "rows_inserted": _exact_zero_count(
            ingest.get("rows_inserted"),
            field="rows_inserted",
            run_id=run_id,
        ),
        "rows_rejected": ingest.get("rows_rejected"),
        "status": _text(ingest, "status", "SEC ingest evidence"),
        "error": _text(ingest, "error", "SEC ingest evidence"),
        "rejection_codes": ingest.get("rejection_codes"),
    }
    expected_core = {
        "run_id": run_id,
        "source": source,
        "table_name": "fundamentals",
        "rows_inserted": 0,
        "status": "warning",
    }
    if any(warning_outcome.get(key) != value for key, value in expected_core.items()):
        field = next(
            key
            for key, value in expected_core.items()
            if warning_outcome.get(key) != value
        )
        raise ValueError(
            "SEC zero-row warning outcome is inconsistent "
            f"for run {_bounded_identifier(run_id)}: {field}"
        )
    if ingest_outcome != warning_outcome:
        field = next(
            key for key in warning_outcome if ingest_outcome.get(key) != warning_outcome[key]
        )
        raise ValueError(
            "SEC zero-row ingest evidence does not match its warning "
            f"for run {_bounded_identifier(run_id)}: {field}"
        )
    rows_rejected = warning_outcome["rows_rejected"]
    if (
        isinstance(rows_rejected, bool)
        or not isinstance(rows_rejected, int)
        or rows_rejected != expected_rows_rejected
    ):
        raise ValueError(
            "SEC zero-row warning outcome is inconsistent "
            f"for run {_bounded_identifier(run_id)}: rows_rejected"
        )
    expected_codes = tuple(replay_proof["rejection_codes"])
    try:
        recorded_codes = decode_rejection_codes(warning_outcome["rejection_codes"])
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "SEC zero-row warning outcome is inconsistent "
            f"for run {_bounded_identifier(run_id)}: rejection_codes"
        ) from exc
    legacy_future_warning = (
        replay_proof["parser_version"] == COMPANYFACTS_LEGACY_PARSER_VERSION
        and expected_codes == ("future_period",)
        and recorded_codes is None
        and accepted_sec_fundamental_outcome(
            status="warning",
            error=warning_outcome["error"],
            rejection_codes=None,
        )
    )
    if (
        expected_codes
        and recorded_codes != expected_codes
        and not legacy_future_warning
    ) or (not expected_codes and recorded_codes is not None):
        raise ValueError(
            "SEC zero-row warning outcome is inconsistent "
            f"for run {_bounded_identifier(run_id)}: rejection_codes"
        )
    if expected_rows_rejected == 0:
        if warning_outcome["error"] != SEC_ZERO_ROW_ERROR:
            raise ValueError(
                "SEC zero-row warning outcome is inconsistent "
                f"for run {_bounded_identifier(run_id)}: error"
            )
    elif not accepted_sec_fundamental_outcome(
        status="warning",
        error=warning_outcome["error"],
        rejection_codes=warning_outcome["rejection_codes"],
    ) and not legacy_future_warning:
        raise ValueError(
            "SEC zero-row warning outcome is inconsistent "
            f"for run {_bounded_identifier(run_id)}: error"
        )

    proof_body = {
        **warning_outcome,
        "ingest_id": _exact_positive_int(
            warning.get("id"),
            field="id",
            run_id=run_id,
        ),
    }
    ingest_id = _exact_positive_int(
        ingest.get("id"),
        field="id",
        run_id=run_id,
    )
    if ingest_id != proof_body["ingest_id"]:
        raise ValueError(
            "SEC zero-row ingest evidence does not match its warning "
            f"for run {_bounded_identifier(run_id)}: id"
        )
    return {
        **proof_body,
        "proof_sha256": _sha(proof_body),
    }


def _sec_zero_row_replay_proof(
    *,
    payload: dict[str, Any],
    snapshot: dict[str, Any],
    decision_date: date,
) -> dict[str, Any]:
    """Replay the recorded parser and prove zero decision-visible rows."""
    parsed_count = snapshot.get("parsed_row_count")
    parsed_hash = snapshot.get("parsed_rows_sha256")
    legacy_capture = parsed_count is None and parsed_hash is None
    if (parsed_count is None) != (parsed_hash is None):
        raise ValueError("SEC zero-row Company Facts parse metadata is incomplete")
    parser_version = snapshot.get("parser_version")
    if legacy_capture:
        if parser_version not in {
            COMPANYFACTS_CAPTURE_PARSER_VERSION,
            "sec-companyfacts-v1",
        }:
            raise ValueError("SEC zero-row Company Facts parser is unsupported")
        parser_version = COMPANYFACTS_LEGACY_PARSER_VERSION
    if parser_version not in {
        COMPANYFACTS_LEGACY_PARSER_VERSION,
        COMPANYFACTS_STORAGE_SAFE_V1_PARSER_VERSION,
        COMPANYFACTS_PARSER_VERSION,
        COMPANYFACTS_NEXT_PARSER_VERSION,
    }:
        raise ValueError("SEC zero-row Company Facts parser is unsupported")
    try:
        provider_rows, replay_metadata = replay_sec_companyfacts_response(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode(),
            parser_version=str(parser_version),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("SEC zero-row Company Facts replay failed") from exc

    replay_hash = canonical_parsed_rows_sha256(provider_rows)
    if not legacy_capture and (
        not isinstance(parsed_count, int)
        or isinstance(parsed_count, bool)
        or parsed_count != len(provider_rows)
        or parsed_hash != replay_hash
    ):
        raise ValueError("SEC zero-row Company Facts parsed-row proof does not match replay")
    decision_visible = _storage_canonical_provider_rows(
        [
            row
            for row in provider_rows
            if _as_date(row["as_of_date"]) <= decision_date
            and _as_date(row["period_end"]) <= _as_date(row["as_of_date"])
        ]
    )
    if decision_visible:
        raise ValueError(
            "SEC zero-row Company Facts replay produced decision-visible rows "
            f"for {decision_date.isoformat()}"
        )
    proof_body = {
        "parser_version": parser_version,
        "decision_evidence_as_of": decision_date.isoformat(),
        "companyfacts_snapshot_id": snapshot["snapshot_id"],
        "companyfacts_payload_sha256": snapshot["payload_sha256"],
        "replayed_rows": len(provider_rows),
        "replayed_rows_sha256": replay_hash,
        "rows_rejected": int(replay_metadata["rows_rejected"]),
        "rejection_codes": list(replay_metadata["rejection_codes"]),
        "decision_visible_valid_rows": 0,
        "decision_visible_rows_sha256": canonical_parsed_rows_sha256(decision_visible),
    }
    return {
        **proof_body,
        "proof_sha256": _sha(proof_body),
    }


def _sec_submissions_filing_index(
    *,
    payload: dict[str, Any],
    snapshot: dict[str, Any],
    decision_date: date,
) -> dict[str, Any]:
    """Summarize only decision-visible forms from one exact Submissions response."""
    source = {
        "snapshot_id": snapshot["snapshot_id"],
        "payload_sha256": snapshot["payload_sha256"],
        "received_at": snapshot["received_at"],
    }
    basis = {
        "decision_evidence_as_of": decision_date.isoformat(),
        "source": source,
        "pit_filter": "filingDate <= decision_evidence_as_of",
    }
    filings = payload.get("filings")
    if filings is None:
        unavailable = {
            **basis,
            "availability": "filing_index_not_present",
            "decision_visible_filing_count": None,
            "excluded_after_decision_count": None,
            "periodic_form_count": None,
            "periodic_forms": [],
            "registration_form_count": None,
            "registration_forms": [],
        }
        return {**unavailable, "proof_sha256": _sha(unavailable)}
    if not isinstance(filings, dict):
        raise ValueError("SEC Submissions filings field is not an object")
    recent = filings.get("recent")
    if recent is None:
        unavailable = {
            **basis,
            "availability": "recent_filing_index_not_present",
            "decision_visible_filing_count": None,
            "excluded_after_decision_count": None,
            "periodic_form_count": None,
            "periodic_forms": [],
            "registration_form_count": None,
            "registration_forms": [],
        }
        return {**unavailable, "proof_sha256": _sha(unavailable)}
    if not isinstance(recent, dict):
        raise ValueError("SEC Submissions recent filings field is not an object")

    forms = recent.get("form")
    filing_dates = recent.get("filingDate")
    if forms is None and filing_dates is None:
        unavailable = {
            **basis,
            "availability": "recent_form_index_not_present",
            "decision_visible_filing_count": None,
            "excluded_after_decision_count": None,
            "periodic_form_count": None,
            "periodic_forms": [],
            "registration_form_count": None,
            "registration_forms": [],
        }
        return {**unavailable, "proof_sha256": _sha(unavailable)}
    if not isinstance(forms, list) or not isinstance(filing_dates, list):
        raise ValueError("SEC Submissions recent form and filingDate fields must be lists")
    if len(forms) != len(filing_dates):
        raise ValueError("SEC Submissions recent form and filingDate arrays are not aligned")

    snapshot_date = _utc_raw_snapshot_time(snapshot["received_at"]).date()
    visible: list[tuple[str, date]] = []
    excluded_after_decision = 0
    for index, (raw_form, raw_filing_date) in enumerate(zip(forms, filing_dates, strict=True)):
        form = str(raw_form or "").strip().upper()
        if not form:
            raise ValueError(f"SEC Submissions recent filing {index} has no form")
        try:
            filing_date = _as_date(str(raw_filing_date or ""))
        except ValueError as exc:
            raise ValueError(
                f"SEC Submissions recent filing {index} has an invalid filingDate"
            ) from exc
        if filing_date > snapshot_date:
            raise ValueError(
                "SEC Submissions contains a filing dated after its exact "
                f"snapshot boundary: {filing_date.isoformat()}"
            )
        if filing_date > decision_date:
            excluded_after_decision += 1
            continue
        visible.append((form, filing_date))

    periodic = [row for row in visible if _sec_form_base(row[0]) in SEC_PERIODIC_FORM_BASES]
    registrations = [
        row
        for row in visible
        if _sec_form_base(row[0]) in SEC_REGISTRATION_FORM_BASES
        or row[0] in SEC_REGISTRATION_POST_EFFECTIVE_FORMS
    ]
    proof_body = {
        **basis,
        "availability": "exact_submissions_filing_index",
        "decision_visible_filing_count": len(visible),
        "excluded_after_decision_count": excluded_after_decision,
        "periodic_form_count": len(periodic),
        "periodic_forms": _filing_form_summary(periodic),
        "registration_form_count": len(registrations),
        "registration_forms": _filing_form_summary(registrations),
    }
    return {**proof_body, "proof_sha256": _sha(proof_body)}


def _sec_form_base(form: str) -> str:
    return form[:-2] if form.endswith("/A") else form


def _filing_form_summary(rows: list[tuple[str, date]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[date]] = {}
    for form, filing_date in rows:
        grouped.setdefault(form, []).append(filing_date)
    return [
        {
            "form": form,
            "filing_count": len(grouped[form]),
            "first_filing_date": min(grouped[form]).isoformat(),
            "latest_filing_date": max(grouped[form]).isoformat(),
        }
        for form in sorted(grouped)
    ]


def _reviewed_owner_filing_context(
    *,
    store: Store,
    root: Path,
    security_id: str,
    active_issuer_id: str,
    decision_date: date,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Bind owner timing and independently verified predecessor fact context."""
    active_rows = store.query(
        """
        /* anomaly_active_owner_context */
        SELECT owner.security_id, owner.issuer_id, owner.effective_start,
               owner.effective_end, owner.verified_date, owner.source,
               issuer.canonical_name, issuer.canonical_ticker
        FROM security_issuer_assignments AS owner
        JOIN issuer_master AS issuer USING (issuer_id)
        WHERE owner.security_id = ?
          AND owner.verified_date <= CAST(? AS DATE)
          AND owner.effective_start <= CAST(? AS DATE)
          AND (
              owner.effective_end IS NULL
              OR owner.effective_end > CAST(? AS DATE)
          )
        ORDER BY owner.effective_start DESC
        """,
        (
            security_id,
            decision_date.isoformat(),
            decision_date.isoformat(),
            decision_date.isoformat(),
        ),
    )
    if not active_rows:
        unavailable_body = {
            "availability": "assignment_detail_not_available",
            "security_id": security_id,
            "active_issuer_id": active_issuer_id,
            "active_owner_start": None,
            "predecessor_owner": None,
            "predecessor_fact_coverage": {
                "state": "not_evaluated_without_assignment_detail",
                "facts_transfer_to_active_issuer": False,
            },
        }
        return {
            **unavailable_body,
            "proof_sha256": _sha(unavailable_body),
        }, []
    if len(active_rows) != 1:
        raise ValueError(
            "reviewed security has ambiguous active owner assignment detail: "
            f"{security_id}@{decision_date.isoformat()}"
        )
    active_row = active_rows[0]
    if _text(active_row, "issuer_id", "active owner") != active_issuer_id:
        raise ValueError(
            "active owner assignment detail disagrees with the reviewed issuer: "
            f"{security_id}@{decision_date.isoformat()}"
        )
    active_start = _as_date(active_row["effective_start"])
    active_owner = _owner_assignment_evidence(active_row)

    predecessor_rows = store.query(
        """
        /* anomaly_predecessor_owner_context */
        SELECT owner.security_id, owner.issuer_id, owner.effective_start,
               owner.effective_end, owner.verified_date, owner.source,
               issuer.canonical_name, issuer.canonical_ticker,
               (
                   SELECT cik.cik
                   FROM issuer_cik_history AS cik
                   WHERE cik.issuer_id = owner.issuer_id
                     AND cik.verified_date <= CAST(? AS DATE)
                     AND cik.effective_start < owner.effective_end
                     AND (
                         cik.effective_end IS NULL
                         OR cik.effective_end >= owner.effective_end
                     )
                   ORDER BY cik.effective_start DESC
                   LIMIT 1
               ) AS cik
        FROM security_issuer_assignments AS owner
        JOIN issuer_master AS issuer USING (issuer_id)
        WHERE owner.security_id = ?
          AND owner.verified_date <= CAST(? AS DATE)
          AND owner.effective_end IS NOT NULL
          AND owner.effective_end <= CAST(? AS DATE)
          AND owner.issuer_id <> ?
        ORDER BY owner.effective_end DESC, owner.effective_start DESC
        """,
        (
            decision_date.isoformat(),
            security_id,
            decision_date.isoformat(),
            active_start.isoformat(),
            active_issuer_id,
        ),
    )
    if not predecessor_rows:
        no_predecessor_body = {
            "availability": "reviewed_assignment_history",
            "security_id": security_id,
            "active_issuer": active_owner,
            "active_owner_start": active_start.isoformat(),
            "predecessor_owner": None,
            "predecessor_fact_coverage": {
                "state": "not_applicable_no_reviewed_predecessor",
                "facts_transfer_to_active_issuer": False,
            },
        }
        return {
            **no_predecessor_body,
            "proof_sha256": _sha(no_predecessor_body),
        }, []

    nearest_end = _as_date(predecessor_rows[0]["effective_end"])
    equally_near = [
        row for row in predecessor_rows if _as_date(row["effective_end"]) == nearest_end
    ]
    if len({str(row.get("issuer_id")) for row in equally_near}) != 1:
        raise ValueError(
            "reviewed security has ambiguous predecessor owner assignments: "
            f"{security_id}@{active_start.isoformat()}"
        )
    predecessor_row = predecessor_rows[0]
    predecessor = _owner_assignment_evidence(predecessor_row)
    predecessor_cik = _optional(predecessor_row.get("cik"))
    snapshot_refs: list[dict[str, Any]] = []
    if predecessor_cik is None:
        coverage_body = {
            "state": "not_verified_without_reviewed_predecessor_cik",
            "accepted_rows": None,
            "facts_transfer_to_active_issuer": False,
        }
    else:
        predecessor_id = _text(
            predecessor_row,
            "issuer_id",
            "predecessor owner",
        )
        predecessor_coverage, snapshot_refs = _verified_sec_fundamental_coverage(
            store=store,
            root=root,
            issuer_members={
                predecessor_id: {
                    "issuer_id": predecessor_id,
                    "cik": _cik(predecessor_cik),
                }
            },
            decision_date=decision_date,
        )
        verified = predecessor_coverage.get(predecessor_id)
        if verified is None:
            coverage_body = {
                "state": "not_verified_from_exact_source_replay",
                "accepted_rows": None,
                "facts_transfer_to_active_issuer": False,
            }
        else:
            coverage_body = {
                "state": "covered_with_verified_source_replay",
                "accepted_rows": int(verified["accepted_rows"]),
                "first_as_of_date": verified.get("first_as_of_date"),
                "latest_as_of_date": verified.get("latest_as_of_date"),
                "lineage": verified.get("lineage"),
                "facts_transfer_to_active_issuer": False,
            }
    predecessor_coverage_evidence = {
        **coverage_body,
        "proof_sha256": _sha(coverage_body),
    }
    owner_body = {
        "availability": "reviewed_assignment_history",
        "security_id": security_id,
        "active_issuer": active_owner,
        "active_owner_start": active_start.isoformat(),
        "predecessor_owner": {
            **predecessor,
            "cik": _cik(predecessor_cik) if predecessor_cik else None,
        },
        "predecessor_fact_coverage": predecessor_coverage_evidence,
        "transition_gap_days": (active_start - nearest_end).days,
    }
    return {**owner_body, "proof_sha256": _sha(owner_body)}, snapshot_refs


def _owner_assignment_evidence(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "security_id": _text(row, "security_id", "security owner"),
        "issuer_id": _text(row, "issuer_id", "security owner"),
        "canonical_name": _text(row, "canonical_name", "security owner"),
        "canonical_ticker": _text(
            row,
            "canonical_ticker",
            "security owner",
        ).upper(),
        "effective_start": _as_date(row["effective_start"]).isoformat(),
        "effective_end": (
            _as_date(row["effective_end"]).isoformat()
            if row.get("effective_end") is not None
            else None
        ),
        "verified_date": _as_date(row["verified_date"]).isoformat(),
        "source": _text(row, "source", "security owner"),
    }


def _deduplicate_snapshot_references(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_snapshot_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        snapshot_id = _text(row, "snapshot_id", "raw snapshot")
        prior = by_snapshot_id.get(snapshot_id)
        if prior is not None and prior != row:
            raise ValueError(f"one SEC snapshot ID has conflicting references: {snapshot_id}")
        by_snapshot_id[snapshot_id] = row
    return [by_snapshot_id[key] for key in sorted(by_snapshot_id)]


def _exact_zero_count(value: Any, *, field: str, run_id: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value != 0:
        raise ValueError(
            "SEC zero-row outcome requires integer zero "
            f"for run {_bounded_identifier(run_id)}: {field}"
        )
    return value


def _exact_positive_int(value: Any, *, field: str, run_id: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            "SEC zero-row outcome has an invalid integer "
            f"for run {_bounded_identifier(run_id)}: {field}"
        )
    return value


def _bounded_identifier(value: str) -> str:
    return value if len(value) <= 96 else f"{value[:93]}..."


def _exact_count(value: Any, *, field: str, label: str) -> int:
    """Require a genuine non-negative integer count.

    A silently coerced ``"502"`` or ``None`` would let a malformed baseline
    manufacture or hide a deterioration finding, so the type is checked exactly.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} requires a non-negative integer {field}: {value!r}")
    return value


def _verified_sec_fundamental_coverage(
    *,
    store: Store,
    root: Path,
    issuer_members: dict[str, dict[str, Any]],
    decision_date: date,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Prove covered rows from explicit lineage or exact legacy equality."""
    explicit_rows: list[dict[str, Any]] = []
    lineage_reader = getattr(store, "sec_fundamental_lineage_rows", None)
    if callable(lineage_reader):
        explicit_rows = lineage_reader(
            list(issuer_members),
            decision_date,
        )
    explicit_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in explicit_rows:
        key = (
            _text(row, "issuer_id", "fundamental lineage"),
            _text(row, "ingest_run_id", "fundamental lineage"),
        )
        explicit_groups.setdefault(key, []).append(row)

    candidates: list[tuple[int, str, dict[str, Any], list[dict[str, Any]] | None]] = []
    placeholders = ",".join("?" for _ in issuer_members)
    ingest_columns = {
        row["column_name"]
        for row in store.query(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'main' AND table_name = 'ingest_log'
            """
        )
    }
    subject_present = {"subject_type", "subject_id"} & ingest_columns
    if subject_present and subject_present != {"subject_type", "subject_id"}:
        raise RuntimeError(
            "Ingest subject schema is incomplete: subject_type and subject_id "
            "must exist together."
        )
    subject_projection = (
        "subject_type, subject_id"
        if subject_present
        else "NULL AS subject_type, NULL AS subject_id"
    )
    rejection_projection = (
        "rejection_codes"
        if "rejection_codes" in ingest_columns
        else "NULL AS rejection_codes"
    )
    fundamental_columns = {
        row["column_name"]
        for row in store.query(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'main' AND table_name = 'fundamentals'
            """
        )
    }
    locator_projection = (
        "source_fact_locator"
        if "source_fact_locator" in fundamental_columns
        else "NULL AS source_fact_locator"
    )
    subject_runs = (
        store.query(
            f"""
            SELECT id, run_id, source, {subject_projection}, rows_inserted,
                   rows_rejected, started_at, finished_at, status, error,
                   {rejection_projection}
            FROM ingest_log
            WHERE table_name = 'fundamentals'
              AND status IN ('success', 'warning')
              AND rows_inserted > 0
              AND source LIKE 'edgar:%'
              AND subject_type = 'issuer'
              AND subject_id IN ({placeholders})
            ORDER BY id DESC
            """,
            tuple(sorted(issuer_members)),
        )
        if subject_present
        else []
    )
    candidate_keys: set[tuple[str, str]] = set()
    for row in subject_runs:
        issuer_id = _text(row, "subject_id", "SEC fundamentals ingest")
        run_id = _text(row, "run_id", "SEC fundamentals ingest")
        key = (issuer_id, run_id)
        candidate_keys.add(key)
        candidates.append(
            (
                int(row["id"]),
                issuer_id,
                row,
                explicit_groups.get(key, []),
            )
        )
    # Keep compatibility with stores that expose verified lineage rows but do
    # not yet expose subject-scoped candidate rows through the generic reader.
    for (issuer_id, run_id), rows in explicit_groups.items():
        if (issuer_id, run_id) in candidate_keys:
            continue
        candidates.append(
            (
                int(rows[0]["ingest_id"]),
                issuer_id,
                {
                    "run_id": run_id,
                    "status": rows[0].get("ingest_status"),
                    "error": rows[0].get("ingest_error"),
                },
                rows,
            )
        )

    legacy_subject_filter = (
        "AND subject_type IS NULL AND subject_id IS NULL"
        if subject_present
        else ""
    )
    legacy_runs = store.query(
        f"""
        SELECT id, run_id, source, {subject_projection}, rows_inserted,
               rows_rejected, started_at, finished_at, status, error,
               {rejection_projection}
        FROM ingest_log
        WHERE table_name = 'fundamentals'
          AND status IN ('success', 'warning')
          AND rows_inserted > 0
          AND source LIKE 'edgar:%'
          {legacy_subject_filter}
        ORDER BY id DESC
        """
    )
    for row in legacy_runs:
        if row.get("subject_type") is not None or row.get("subject_id") is not None:
            continue
        if not accepted_sec_fundamental_outcome(
            status=row.get("status"),
            error=row.get("error"),
            rejection_codes=row.get("rejection_codes"),
        ):
            continue
        candidates.append((int(row["id"]), "", row, None))

    accepted: dict[str, dict[str, Any]] = {}
    used: list[dict[str, Any]] = []
    for ingest_id, explicit_issuer_id, run, explicit in sorted(
        candidates,
        key=lambda item: item[0],
        reverse=True,
    ):
        run_id = _text(run, "run_id", "SEC fundamentals ingest")
        if explicit_issuer_id in accepted:
            continue
        evidence = store.ingest_evidence(run_id)
        if evidence is None:
            _reject_relevant_sec_candidate(
                issuer_id=explicit_issuer_id,
                run_id=run_id,
                reason="ingest evidence is missing",
            )
            continue
        evidence_status = evidence.get("status") or run.get("status")
        evidence_error = evidence.get("error") or run.get("error")
        evidence_rejection_codes = evidence.get(
            "rejection_codes",
            run.get("rejection_codes"),
        )
        if evidence_status is not None and not accepted_sec_fundamental_outcome(
            status=evidence_status,
            error=evidence_error,
            rejection_codes=evidence_rejection_codes,
        ):
            _reject_relevant_sec_candidate(
                issuer_id=explicit_issuer_id,
                run_id=run_id,
                reason="ingest outcome is not an accepted SEC result",
            )
            continue
        snapshots = evidence.get("snapshots")
        if not isinstance(snapshots, list):
            _reject_relevant_sec_candidate(
                issuer_id=explicit_issuer_id,
                run_id=run_id,
                reason="snapshot evidence is malformed",
            )
            continue
        facts = [
            row for row in snapshots if isinstance(row, dict) and row.get("role") == "companyfacts"
        ]
        submissions = [
            row for row in snapshots if isinstance(row, dict) and row.get("role") == "submissions"
        ]
        if len(facts) != 1 or len(submissions) != 1:
            _reject_relevant_sec_candidate(
                issuer_id=explicit_issuer_id,
                run_id=run_id,
                reason="exactly one Company Facts and one Submissions snapshot is required",
            )
            continue
        try:
            facts_reference = _snapshot_reference(facts[0])
            submissions_reference = _snapshot_reference(submissions[0])
            facts_payload = _read_sec_snapshot(
                facts[0],
                root,
                dataset="companyfacts",
                label="Company Facts",
            )
            submissions_payload = _read_sec_snapshot(
                submissions[0],
                root,
                dataset="submissions",
                label="Submissions",
            )
            payload_cik = _cik(facts_payload.get("cik"))
        except (TypeError, ValueError):
            _reject_relevant_sec_candidate(
                issuer_id=explicit_issuer_id,
                run_id=run_id,
                reason="snapshot metadata or bytes failed verification",
            )
            continue

        issuer_id = explicit_issuer_id
        if not issuer_id:
            issuer_id = next(
                (
                    candidate
                    for candidate, member in issuer_members.items()
                    if member["cik"] == payload_cik
                ),
                "",
            )
        if issuer_id in accepted:
            continue
        if (
            not issuer_id
            or issuer_id not in issuer_members
            or issuer_members[issuer_id]["cik"] != payload_cik
        ):
            _reject_relevant_sec_candidate(
                issuer_id=explicit_issuer_id,
                run_id=run_id,
                reason="payload CIK does not match the reviewed issuer",
            )
            continue
        try:
            submissions_cik = _cik(submissions_payload.get("cik"))
        except ValueError:
            _reject_relevant_sec_candidate(
                issuer_id=issuer_id,
                run_id=run_id,
                reason="Submissions snapshot has an invalid CIK",
            )
            continue
        if submissions_cik != payload_cik:
            _reject_relevant_sec_candidate(
                issuer_id=issuer_id,
                run_id=run_id,
                reason="Company Facts and Submissions CIKs disagree",
            )
            continue

        evidence_subject_type = evidence.get("subject_type") or run.get("subject_type")
        evidence_subject_id = evidence.get("subject_id") or run.get("subject_id")
        if explicit_issuer_id and (
            evidence_subject_type != "issuer" or evidence_subject_id != issuer_id
        ):
            _reject_relevant_sec_candidate(
                issuer_id=issuer_id,
                run_id=run_id,
                reason="ingest subject identity does not match the reviewed issuer",
            )
        parsed_count = facts_reference["parsed_row_count"]
        parsed_hash = facts_reference["parsed_rows_sha256"]
        legacy_capture = parsed_count is None and parsed_hash is None
        legacy_capture_versions = {
            COMPANYFACTS_CAPTURE_PARSER_VERSION,
            "sec-companyfacts-v1",
        }
        parser_version = (
            COMPANYFACTS_LEGACY_PARSER_VERSION
            if legacy_capture
            else facts_reference["parser_version"]
        )
        if (parsed_count is None) != (parsed_hash is None) or parser_version not in {
            COMPANYFACTS_LEGACY_PARSER_VERSION,
            COMPANYFACTS_STORAGE_SAFE_V1_PARSER_VERSION,
            COMPANYFACTS_PARSER_VERSION,
            COMPANYFACTS_NEXT_PARSER_VERSION,
        } or (
            legacy_capture
            and facts_reference["parser_version"] not in legacy_capture_versions
        ):
            _reject_relevant_sec_candidate(
                issuer_id=issuer_id,
                run_id=run_id,
                reason="Company Facts parse metadata is incomplete or unsupported",
            )
            continue
        try:
            provider_rows, replay_metadata = replay_sec_companyfacts_response(
                json.dumps(
                    facts_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode(),
                parser_version=parser_version,
            )
        except (TypeError, ValueError):
            _reject_relevant_sec_candidate(
                issuer_id=issuer_id,
                run_id=run_id,
                reason="Company Facts replay failed",
            )
            continue
        replay_hash = canonical_parsed_rows_sha256(provider_rows)
        if not legacy_capture and (
            int(parsed_count) != len(provider_rows) or replay_hash != parsed_hash
        ):
            _reject_relevant_sec_candidate(
                issuer_id=issuer_id,
                run_id=run_id,
                reason="Company Facts parsed-row proof does not match replay",
            )
            continue
        replay_rows_rejected = replay_metadata["rows_rejected"]
        outcome_rows_rejected = evidence.get(
            "rows_rejected",
            run.get("rows_rejected"),
        )
        if (
            outcome_rows_rejected is None
            and not explicit_issuer_id
            and evidence_status == "success"
            and replay_rows_rejected == 0
        ):
            # Historical unscoped outcomes could leave this nullable. Exact
            # replay plus a zero-rejection success is the sole compatibility
            # path; issuer-scoped evidence must always carry the integer.
            outcome_rows_rejected = 0
        if (
            isinstance(outcome_rows_rejected, bool)
            or not isinstance(outcome_rows_rejected, int)
            or outcome_rows_rejected != replay_rows_rejected
        ):
            _reject_relevant_sec_candidate(
                issuer_id=issuer_id,
                run_id=run_id,
                reason="ingest rejection count does not match exact replay",
            )
            continue
        expected_rejection_codes = tuple(replay_metadata["rejection_codes"])
        try:
            recorded_rejection_codes = decode_rejection_codes(
                evidence_rejection_codes
            )
        except (TypeError, ValueError):
            recorded_rejection_codes = None
            rejection_codes_invalid = evidence_rejection_codes is not None
        else:
            rejection_codes_invalid = False
        legacy_future_warning = (
            parser_version == COMPANYFACTS_LEGACY_PARSER_VERSION
            and expected_rejection_codes == ("future_period",)
            and evidence_rejection_codes is None
            and evidence_status == "warning"
            and accepted_sec_fundamental_outcome(
                status=evidence_status,
                error=evidence_error,
                rejection_codes=None,
            )
        )
        if rejection_codes_invalid or (
            expected_rejection_codes
            and recorded_rejection_codes != expected_rejection_codes
            and not legacy_future_warning
        ) or (not expected_rejection_codes and recorded_rejection_codes is not None):
            _reject_relevant_sec_candidate(
                issuer_id=issuer_id,
                run_id=run_id,
                reason="ingest rejection codes do not match exact replay",
            )
            continue
        effective_rowset_hash = replay_hash

        filtered_provider = _storage_canonical_provider_rows(
            [
                row
                for row in provider_rows
                if _as_date(row["as_of_date"]) <= decision_date
                and _as_date(row["period_end"]) <= _as_date(row["as_of_date"])
            ]
        )
        if not filtered_provider:
            # A valid newer response containing only future rows is not
            # relevant to this decision boundary. It must not displace an
            # older exact proof for rows that are visible on the decision date.
            continue

        if explicit is None:
            stored = store.query(
                """
                SELECT ticker, issuer_id, security_id, period_end, as_of_date,
                       fiscal_period, statement, metric, value, quarter_value,
                       unit, source
                FROM fundamentals
                WHERE issuer_id = ?
                  AND source = 'edgar'
                  AND as_of_date <= CAST(? AS DATE)
                  AND period_end <= as_of_date
                ORDER BY period_end, as_of_date, metric
                """,
                (issuer_id, decision_date.isoformat()),
            )
            identity_binding = "legacy_exact_rowset_equality"
        else:
            identity_binding = "explicit_row_lineage"
            stored = store.query(
                f"""
                SELECT ticker, issuer_id, security_id, period_end, as_of_date,
                       fiscal_period, statement, metric, value, quarter_value,
                       unit, source, ingest_run_id, source_snapshot_id,
                       source_rowset_sha256, source_row_sha256,
                       {locator_projection}
                FROM fundamentals
                WHERE issuer_id = ?
                  AND source = 'edgar'
                  AND as_of_date <= CAST(? AS DATE)
                  AND period_end <= as_of_date
                ORDER BY period_end, as_of_date, metric
                """,
                (issuer_id, decision_date.isoformat()),
            )
            if not explicit:
                _reject_relevant_sec_candidate(
                    issuer_id=issuer_id,
                    run_id=run_id,
                    reason="no decision-visible rows carry this ingest lineage",
                )
            if len(stored) != len(explicit):
                _reject_relevant_sec_candidate(
                    issuer_id=issuer_id,
                    run_id=run_id,
                    reason=(
                        "the full decision-visible EDGAR rowset is not wholly "
                        "accounted for by this lineage"
                    ),
                )
            if any(
                row.get("ingest_run_id") != run_id
                or row.get("source_snapshot_id") != facts_reference["snapshot_id"]
                or row["source_rowset_sha256"] != effective_rowset_hash
                or row["source_row_sha256"]
                != canonical_sec_fundamental_row_sha256(_provider_projection(row, payload_cik))
                for row in stored
            ):
                _reject_relevant_sec_candidate(
                    issuer_id=issuer_id,
                    run_id=run_id,
                    reason=(
                        "a decision-visible EDGAR row is unlineaged or carries "
                        "different source proof"
                    ),
                )
                continue
            explicit_provider = [
                _provider_projection(row, payload_cik)
                for row in explicit
                if str(row.get("source") or "") == "edgar"
            ]
            visible_provider = [
                _provider_projection(row, payload_cik)
                for row in stored
                if str(row.get("source") or "") == "edgar"
            ]
            if _canonical_rowset_hash(explicit_provider) != _canonical_rowset_hash(
                visible_provider
            ):
                _reject_relevant_sec_candidate(
                    issuer_id=issuer_id,
                    run_id=run_id,
                    reason=(
                        "verified lineage rows do not equal the full decision-visible EDGAR rowset"
                    ),
                )

        stored_provider = [
            _provider_projection(row, payload_cik)
            for row in stored
            if str(row.get("source") or "") == "edgar"
        ]
        if _canonical_rowset_hash(stored_provider) != _canonical_rowset_hash(filtered_provider):
            _reject_relevant_sec_candidate(
                issuer_id=issuer_id,
                run_id=run_id,
                reason="stored decision-visible rows do not match source replay",
            )
            continue
        dates = sorted(_as_date(row["as_of_date"]) for row in stored_provider)
        accepted[issuer_id] = {
            "accepted_rows": len(stored_provider),
            "first_as_of_date": dates[0].isoformat(),
            "latest_as_of_date": dates[-1].isoformat(),
            "lineage": {
                "ingest_id": ingest_id,
                "run_id": run_id,
                "rows_inserted": int(evidence.get("rows_inserted") or len(stored)),
                "identity_binding": identity_binding,
                "subject_type": evidence.get("subject_type"),
                "subject_id": evidence.get("subject_id"),
                "companyfacts_snapshot": facts_reference,
                "submissions_snapshot": submissions_reference,
                "decision_rowset_sha256": _canonical_rowset_hash(stored_provider),
                "replayed_rowset_sha256": effective_rowset_hash,
            },
        }
        used.extend((facts_reference, submissions_reference))
    return accepted, used


def _reject_relevant_sec_candidate(
    *,
    issuer_id: str,
    run_id: str,
    reason: str,
) -> None:
    """Prevent a known issuer's newer invalid result from falling back."""
    if issuer_id:
        raise ValueError(
            "newest relevant SEC fundamentals ingest candidate is invalid "
            f"for issuer {issuer_id} (run {run_id}): {reason}"
        )


def _provider_projection(row: dict[str, Any], cik: str) -> dict[str, Any]:
    projected = {
        "cik": cik,
        "period_end": str(row["period_end"]),
        "as_of_date": str(row["as_of_date"]),
        "fiscal_period": row.get("fiscal_period"),
        "statement": row.get("statement"),
        "metric": row["metric"],
        "value": row.get("value"),
        "quarter_value": row.get("quarter_value"),
        "unit": row.get("unit") or "USD",
        "source": row.get("source") or "edgar",
    }
    if row.get("source_fact_locator") is not None:
        projected["source_fact_locator"] = row["source_fact_locator"]
    return projected


def _storage_canonical_provider_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Mirror DuckDB's first source-row winner for one upsert batch."""
    canonical: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        canonical.setdefault(
            (
                str(row["period_end"]),
                str(row["as_of_date"]),
                str(row["metric"]),
            ),
            row,
        )
    return list(canonical.values())


def _canonical_rowset_hash(rows: list[dict[str, Any]]) -> str:
    return _sha(
        sorted(
            rows,
            key=lambda row: json.dumps(
                row,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )
    )


def _sec_clearance_proofs(
    *,
    scope: str,
    issuer_members: dict[str, dict[str, Any]],
    accepted: dict[str, dict[str, Any]],
    warning_by_issuer: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Bind a cleared prior gap to a later successful, exact SEC ingest.

    Legacy covered issuers do not need a proof merely to remain covered. A
    proof is emitted only when the issuer has a verified zero-row warning and a
    later subject-tagged successful ingest. This keeps the scan bounded while
    preventing direct table inserts from masquerading as source correction.
    """

    proofs: dict[str, dict[str, Any]] = {}
    for issuer_id in sorted(accepted):
        coverage = accepted[issuer_id]
        lineage = coverage.get("lineage")
        warning = warning_by_issuer.get(issuer_id)
        if (
            not isinstance(lineage, dict)
            or warning is None
            or lineage.get("identity_binding") != "explicit_row_lineage"
            or lineage.get("subject_type") != "issuer"
            or lineage.get("subject_id") != issuer_id
            or int(lineage.get("ingest_id") or 0) <= int(warning["ingest_id"])
        ):
            continue
        run_id = _text(lineage, "run_id", "successful SEC ingest")
        if issuer_id not in issuer_members or int(coverage.get("accepted_rows") or 0) < 1:
            continue
        facts_reference = lineage["companyfacts_snapshot"]
        submissions_reference = lineage["submissions_snapshot"]
        parsed_count = facts_reference["parsed_row_count"]
        parsed_sha256 = facts_reference["parsed_rows_sha256"]
        if parsed_count is None or int(parsed_count) < 1 or parsed_sha256 is None:
            raise ValueError(f"successful SEC clearance lacks parsed-row proof: {run_id}")

        fingerprint = anomaly_fingerprint(
            rule_id=SEC_RULE_ID,
            rule_version=SEC_RULE_VERSION,
            scope=scope,
            subject_type="issuer",
            subject_id=issuer_id,
        )
        proof_body = {
            "rule_id": SEC_RULE_ID,
            "rule_version": SEC_RULE_VERSION,
            "scope": scope,
            "subject_type": "issuer",
            "subject_id": issuer_id,
            "coverage_state": "covered_with_verified_ingest",
            "accepted_rows": int(coverage["accepted_rows"]),
            "first_as_of_date": coverage.get("first_as_of_date"),
            "latest_as_of_date": coverage.get("latest_as_of_date"),
            "ingest_id": int(lineage["ingest_id"]),
            "ingest_run_id": run_id,
            "ingest_rows_inserted": int(lineage["rows_inserted"]),
            "prior_warning_run_id": warning_by_issuer[issuer_id]["run_id"],
            "decision_rowset_sha256": lineage["decision_rowset_sha256"],
            "companyfacts_snapshot": facts_reference,
            "submissions_snapshot": submissions_reference,
        }
        proofs[fingerprint] = {
            **proof_body,
            "proof_sha256": _sha(proof_body),
        }
    return proofs


def _utc_raw_snapshot_time(value: Any) -> datetime:
    """Restore the UTC contract of raw snapshot timestamps from DuckDB TIMESTAMP."""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("exact SEC raw-snapshot boundary is not an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _snapshot_reference(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_id": _text(snapshot, "snapshot_id", "raw snapshot"),
        "role": _text(snapshot, "role", "raw snapshot"),
        "provider": _text(snapshot, "provider", "raw snapshot"),
        "dataset": _text(snapshot, "dataset", "raw snapshot"),
        "artifact_kind": _text(snapshot, "artifact_kind", "raw snapshot"),
        "http_status": (
            int(snapshot["http_status"]) if snapshot.get("http_status") is not None else None
        ),
        "received_at": str(snapshot.get("received_at") or ""),
        "payload_sha256": _digest(snapshot.get("payload_sha256"), "raw payload"),
        "parser_version": _text(snapshot, "parser_version", "raw snapshot"),
        "parsed_row_count": (
            int(snapshot["parsed_row_count"])
            if snapshot.get("parsed_row_count") is not None
            else None
        ),
        "parsed_rows_sha256": (
            _digest(snapshot["parsed_rows_sha256"], "parsed rows")
            if snapshot.get("parsed_rows_sha256") is not None
            else None
        ),
        "relative_path": _text(snapshot, "relative_path", "raw snapshot"),
    }


def _read_sec_snapshot(
    snapshot: dict[str, Any],
    root: Path,
    *,
    dataset: str,
    label: str,
) -> dict[str, Any]:
    if (
        snapshot.get("provider") != "sec-edgar"
        or snapshot.get("dataset") != dataset
        or snapshot.get("artifact_kind") != "exact_response"
    ):
        raise ValueError(f"{label} evidence is not an exact SEC response")
    status_code = snapshot.get("http_status")
    if status_code is None or not 200 <= int(status_code) <= 299:
        raise ValueError(f"{label} evidence lacks a successful HTTP status")
    if snapshot.get("compression") != "gzip":
        raise ValueError(f"{label} evidence has unsupported compression")
    stored_bytes = _bounded_snapshot_size(
        snapshot.get("stored_bytes"),
        label="stored",
        maximum=MAX_SEC_SNAPSHOT_STORED_BYTES,
    )
    original_bytes = _bounded_snapshot_size(
        snapshot.get("original_bytes"),
        label="original",
        maximum=MAX_SEC_SNAPSHOT_ORIGINAL_BYTES,
    )

    relative = Path(_text(snapshot, "relative_path", "raw snapshot"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("raw snapshot path is not safely project-relative")
    raw_root = root / "data" / "raw"
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise ValueError("raw snapshot root is missing or unsafe")
    target = root / relative
    try:
        beneath_raw = target.relative_to(raw_root)
    except ValueError as exc:
        raise ValueError("raw snapshot escaped the immutable raw-data root") from exc
    parent = raw_root
    for component in beneath_raw.parts[:-1]:
        parent /= component
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("raw snapshot has a missing or unsafe parent directory")
    if not target.resolve().is_relative_to(raw_root.resolve()):
        raise ValueError("raw snapshot escaped the immutable raw-data root")

    descriptor: int | None = None
    try:
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("raw snapshot must be one regular, non-hard-linked file")
        if metadata.st_size != stored_bytes:
            raise ValueError("raw snapshot stored size mismatch")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            compressed = handle.read(stored_bytes + 1)
    except OSError as exc:
        raise ValueError(f"raw snapshot file is missing or unsafe: {relative}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    if len(compressed) != stored_bytes:
        raise ValueError("raw snapshot stored size mismatch")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as archive:
            payload = archive.read(original_bytes + 1)
            has_trailing_output = bool(archive.read(1))
    except (OSError, EOFError, zlib.error) as exc:
        raise ValueError("raw snapshot compression is invalid") from exc
    if len(payload) != original_bytes or has_trailing_output:
        raise ValueError("raw snapshot original size mismatch")
    if hashlib.sha256(payload).hexdigest() != _digest(
        snapshot.get("payload_sha256"),
        "raw payload",
    ):
        raise ValueError("raw snapshot checksum mismatch")
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("raw SEC snapshot is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("raw SEC snapshot must contain one JSON object")
    return parsed


def _bounded_snapshot_size(value: Any, *, label: str, maximum: int) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"raw snapshot {label} size is invalid") from exc
    if size < 1 or size > maximum:
        raise ValueError(f"raw snapshot {label} size exceeds the {maximum}-byte safety limit")
    return size


def _sha(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _digest(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{label} SHA-256 is invalid")
    return normalized


def _text(row: dict[str, Any], key: str, label: str) -> str:
    normalized = str(row.get(key) or "").strip()
    if not normalized:
        raise ValueError(f"{label} lacks {key}")
    return normalized


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _cik(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized.isdigit() or len(normalized) > 10:
        raise ValueError(f"SEC CIK is missing or invalid: {value!r}")
    return normalized.zfill(10)


def _as_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"anomaly scan date must be YYYY-MM-DD: {value!r}") from exc


# ----------------------------------------------------------------------
# Rule: unexplained price move (price gaps and split/dividend mismatches)
# ----------------------------------------------------------------------
def scan_price_action_mismatch(
    *,
    store: Store,
    as_of: date | str,
    universe_id: str = "sp500",
    minimum_members: int = 450,
    maximum_members: int = 550,
    lookback_sessions: int = PRICE_LOOKBACK_SESSIONS,
    move_threshold: float = PRICE_MOVE_THRESHOLD,
) -> AnomalyScan:
    """Detect large close-to-close moves that no corporate action explains.

    A split or large distribution moves the quoted close. When a reviewed,
    action-complete row reports ``split_ratio = 1`` and ``dividends = 0`` yet the
    close still jumps beyond the threshold, either the action evidence is missing
    or the price is wrong. Both are review cases, never automatic repairs.

    Detection is all-or-nothing, exactly like the SEC coverage rule: if any
    examined row lacks the action fields required to make the judgement, the
    whole scan is withheld rather than reported with weaker evidence.
    """

    decision_date = _as_date(as_of)
    if minimum_members < 1 or maximum_members < minimum_members:
        raise ValueError("invalid anomaly-scan universe bounds")
    if lookback_sessions < 2:
        raise ValueError("price anomaly lookback requires at least two sessions")
    if not 0 < move_threshold < 1:
        raise ValueError("price move threshold must be a fraction in (0, 1)")
    scope = f"{PRICE_SCOPE_PREFIX}:{universe_id}"

    members = store.universe_identity_labels(universe_id, decision_date)
    if not minimum_members <= len(members) <= maximum_members:
        raise ValueError(
            f"{universe_id} has {len(members)} reviewed members on {decision_date}; "
            f"expected {minimum_members}-{maximum_members}"
        )

    observations: list[AnomalyObservation] = []
    examined_boundary: list[dict[str, Any]] = []
    examined_rows = 0
    examined_securities = 0
    latest_observed: date | None = None

    for member in sorted(members, key=lambda row: str(row.get("ticker") or "")):
        security_id = _text(member, "security_id", "reviewed universe member")
        ticker = _text(member, "ticker", "reviewed universe member").upper()
        window = store.query(
            """
            SELECT date, close, dividends, split_ratio, actions_complete,
                   close_split_adjusted, provider_symbol, source
            FROM prices
            WHERE security_id = ?
              AND date <= CAST(? AS DATE)
              AND close IS NOT NULL
            ORDER BY date DESC
            LIMIT ?
            """,
            (security_id, decision_date.isoformat(), lookback_sessions),
        )
        if len(window) < 2:
            # No comparable pair: nothing is claimed for this security.
            continue
        rows = list(reversed(window))
        examined_securities += 1
        examined_rows += len(rows)

        for row in rows:
            if not bool(row.get("actions_complete")):
                raise ValueError(
                    "price anomaly scan requires action-complete rows: "
                    f"{security_id}@{row.get('date')}"
                )
            if row.get("split_ratio") is None or row.get("dividends") is None:
                raise ValueError(
                    "price anomaly scan requires split and dividend evidence: "
                    f"{security_id}@{row.get('date')}"
                )

        window_start = _as_date(rows[0]["date"])
        window_end = _as_date(rows[-1]["date"])
        if latest_observed is None or window_end > latest_observed:
            latest_observed = window_end
        examined_boundary.append(
            {
                "security_id": security_id,
                "ticker": ticker,
                "sessions": len(rows),
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
            }
        )

        for previous, current in zip(rows, rows[1:], strict=False):
            previous_close = _positive_close(previous, security_id)
            current_close = _positive_close(current, security_id)
            change = (current_close - previous_close) / previous_close
            if abs(change) < move_threshold:
                continue
            split_ratio = float(current["split_ratio"])
            dividends = float(current["dividends"])
            if split_ratio != 1.0 or dividends != 0.0:
                # The move has declared corporate-action evidence behind it.
                continue

            current_date = _as_date(current["date"])
            previous_date = _as_date(previous["date"])
            implied_ratio = previous_close / current_close
            observations.append(
                AnomalyObservation(
                    fingerprint=anomaly_fingerprint(
                        rule_id=PRICE_RULE_ID,
                        rule_version=PRICE_RULE_VERSION,
                        scope=scope,
                        subject_type="security_session",
                        subject_id=f"{security_id}@{current_date.isoformat()}",
                    ),
                    rule_id=PRICE_RULE_ID,
                    rule_version=PRICE_RULE_VERSION,
                    scope=scope,
                    subject_type="security_session",
                    subject_id=f"{security_id}@{current_date.isoformat()}",
                    severity="high" if abs(change) >= 0.5 else "medium",
                    confidence=CONFIDENCE_REPORTED_SOURCE,
                    title=(
                        f"{ticker} moved {change:.1%} on {current_date.isoformat()} "
                        "with no corporate action"
                    ),
                    summary=(
                        "This action-complete session reports no split and no "
                        "dividend, yet the close moved beyond the review "
                        "threshold. Confirm the corporate-action evidence before "
                        "trusting the price; never adjust the stored row to make "
                        "the move look explained."
                    ),
                    old_value={
                        "date": previous_date.isoformat(),
                        "close": previous_close,
                    },
                    new_value={
                        "date": current_date.isoformat(),
                        "close": current_close,
                        "close_change_fraction": change,
                        "declared_split_ratio": split_ratio,
                        "declared_dividends": dividends,
                        "implied_split_ratio_if_unrecorded": implied_ratio,
                    },
                    evidence={
                        "security_id": security_id,
                        "ticker": ticker,
                        "provider_symbol": _optional(current.get("provider_symbol")),
                        "price_source": _optional(current.get("source")),
                        "actions_complete": True,
                        "close_split_adjusted": bool(
                            current.get("close_split_adjusted")
                        ),
                        "move_threshold": move_threshold,
                        "rule_bundle_version": RULE_BUNDLE_VERSION,
                        "decision_evidence_as_of": decision_date.isoformat(),
                    },
                    suggested_checks=(
                        "Check the issuer's official split/dividend announcement "
                        "for this exact session.",
                        "Compare the stored close against the exact retained "
                        "provider snapshot for both sessions.",
                        "If a split is real but unrecorded, correct it through a "
                        "reviewed corporate-action ingest, not a price edit.",
                        "If the provider row is wrong, withhold it; never "
                        "overwrite a valid stored close.",
                    ),
                )
            )

    if latest_observed is None:
        raise ValueError(
            "price anomaly scan found no comparable reviewed price sessions"
        )

    source_boundary_at = _price_source_boundary(store, decision_date)
    return _build_scan(
        scope=scope,
        rule_id=PRICE_RULE_ID,
        rule_version=PRICE_RULE_VERSION,
        decision_date=decision_date,
        source_boundary_at=source_boundary_at,
        observations=observations,
        boundary_extra={
            "examined_set_sha256": _sha(examined_boundary),
            "latest_examined_session": latest_observed.isoformat(),
        },
        evidence_extra={
            "universe_id": universe_id,
            "reviewed_members": len(members),
            "examined_securities": examined_securities,
            "examined_rows": examined_rows,
            "latest_examined_session": latest_observed.isoformat(),
            "lookback_sessions": lookback_sessions,
            "move_threshold": move_threshold,
            "unexplained_moves": len(observations),
            "examined_set_sha256": _sha(examined_boundary),
        },
    )


def _positive_close(row: dict[str, Any], security_id: str) -> float:
    value = row.get("close")
    if value is None:
        raise ValueError(f"price row lacks a close: {security_id}@{row.get('date')}")
    close = float(value)
    if not close > 0:
        raise ValueError(
            f"price row has a non-positive close: {security_id}@{row.get('date')}"
        )
    return close


def _price_source_boundary(store: Store, decision_date: date) -> datetime:
    """Bind the boundary to the newest price evidence the scan could consume."""
    return _dataset_source_boundary(
        store,
        decision_date,
        # The retained archive labels every price response `daily-prices`,
        # for yfinance and Tiingo alike. The storage table is `prices`; the
        # fetch dataset is not, and using the table name here silently found
        # no evidence and withheld every scan.
        dataset="daily-prices",
        label="price",
        evidence_label="price-fetch evidence",
    )


def _dataset_source_boundary(
    store: Store,
    decision_date: date,
    *,
    dataset: str,
    label: str,
    evidence_label: str,
) -> datetime:
    """Return the newest retained fetch observation this scan could have used.

    The SEC rule derives its boundary from the exact snapshots it consumed.
    Rules that read the storage projection rather than replaying payloads use
    the honest equivalent: the newest retained observation for their dataset at
    or before the decision date.

    Wall-clock time is deliberately NOT used. It would make an identical
    comparison produce a new ``scan_id`` on every run, breaking ledger
    idempotency, and it would claim evidence was observed later than it was.
    """
    row = store.query(
        """
        SELECT MAX(received_at) AS latest
        FROM raw_snapshots
        WHERE dataset = ?
          AND CAST(received_at AS DATE) <= CAST(? AS DATE)
        """,
        (dataset, decision_date.isoformat()),
    )[0]
    latest = row["latest"]
    if latest is None:
        raise ValueError(
            f"{label} anomaly scan has no retained {evidence_label} to bound it"
        )
    return _utc_raw_snapshot_time(latest)


def _reference_source_boundary(store: Store, decision_date: date) -> datetime:
    """Bound an identity/reference scan by the newest evidence it could consume.

    Mapping, share-count and filing rules all read reviewed reference and
    fundamental state, whose retained evidence is the SEC dataset.
    """
    return _dataset_source_boundary(
        store,
        decision_date,
        dataset="companyfacts",
        label="reference",
        evidence_label="companyfacts evidence",
    )


# ----------------------------------------------------------------------
# Rule: coverage deterioration versus the previous comparable run
# ----------------------------------------------------------------------
def measure_universe_coverage(
    *,
    store: Store,
    as_of: date | str,
    universe_id: str = "sp500",
) -> dict[str, Any]:
    """Return the exact current coverage counts for one decision date."""
    decision_date = _as_date(as_of)
    coverage = store.universe_data_coverage(universe_id, decision_date)
    members = len(coverage)
    if not members:
        raise ValueError(
            f"{universe_id} has no active members on {decision_date}"
        )
    priced = sum(bool(row.get("has_price_history")) for row in coverage)
    filed = sum(bool(row.get("has_pit_fundamentals")) for row in coverage)
    identified = sum(bool(row.get("security_id")) for row in coverage)
    return {
        "as_of": decision_date.isoformat(),
        "universe_id": universe_id,
        "members": members,
        "identified_members": identified,
        "priced_members": priced,
        "filed_members": filed,
        "price_coverage_rate": priced / members,
        "filing_coverage_rate": filed / members,
    }


def scan_coverage_deterioration(
    *,
    store: Store,
    as_of: date | str,
    baseline: dict[str, Any] | None,
    universe_id: str = "sp500",
    minimum_drop: int = 1,
) -> AnomalyScan:
    """Compare current coverage against the previous comparable measurement.

    ``baseline`` is the coverage payload recorded by the previous complete scan
    for this scope. When it is ``None`` the scan reports "no comparable
    baseline" and emits zero observations: a first run must never invent a
    deterioration finding.
    """

    decision_date = _as_date(as_of)
    if minimum_drop < 1:
        raise ValueError("coverage deterioration requires a positive minimum drop")
    scope = f"{COVERAGE_SCOPE_PREFIX}:{universe_id}"
    current = measure_universe_coverage(
        store=store,
        as_of=decision_date,
        universe_id=universe_id,
    )

    observations: list[AnomalyObservation] = []
    comparable = False
    if baseline is not None:
        comparable = True
        baseline_as_of = _as_date(_text(baseline, "as_of", "coverage baseline"))
        if baseline_as_of > decision_date:
            raise ValueError(
                "coverage baseline is later than the scanned decision date"
            )
        if str(baseline.get("universe_id") or "") != universe_id:
            raise ValueError("coverage baseline describes a different universe")

        for metric, label in (
            ("filed_members", "point-in-time filing"),
            ("priced_members", "identity-safe price"),
            ("identified_members", "stable identity"),
        ):
            previous_value = _exact_count(
                baseline.get(metric),
                field=metric,
                label="coverage baseline",
            )
            current_value = _exact_count(
                current.get(metric),
                field=metric,
                label="coverage observation",
            )
            drop = previous_value - current_value
            if drop < minimum_drop:
                continue
            observations.append(
                AnomalyObservation(
                    fingerprint=anomaly_fingerprint(
                        rule_id=COVERAGE_RULE_ID,
                        rule_version=COVERAGE_RULE_VERSION,
                        scope=scope,
                        subject_type="coverage_metric",
                        subject_id=f"{universe_id}:{metric}",
                    ),
                    rule_id=COVERAGE_RULE_ID,
                    rule_version=COVERAGE_RULE_VERSION,
                    scope=scope,
                    subject_type="coverage_metric",
                    subject_id=f"{universe_id}:{metric}",
                    severity="high" if drop >= 5 else "medium",
                    confidence=CONFIDENCE_EXACT_COMPARISON,
                    title=(
                        f"{label} coverage fell by {drop} member(s) since "
                        f"{baseline_as_of.isoformat()}"
                    ),
                    summary=(
                        "Coverage decreased versus the previous comparable run. "
                        "Investigate the source or identity change; scores stay "
                        "withheld for affected members until evidence returns."
                    ),
                    old_value={
                        "as_of": baseline_as_of.isoformat(),
                        metric: previous_value,
                        "members": _exact_count(
                            baseline.get("members"),
                            field="members",
                            label="coverage baseline",
                        ),
                    },
                    new_value={
                        "as_of": decision_date.isoformat(),
                        metric: current_value,
                        "members": _exact_count(
                            current.get("members"),
                            field="members",
                            label="coverage observation",
                        ),
                        "dropped": drop,
                    },
                    evidence={
                        "universe_id": universe_id,
                        "metric": metric,
                        "minimum_drop": minimum_drop,
                        "baseline_as_of": baseline_as_of.isoformat(),
                        "current_as_of": decision_date.isoformat(),
                        "rule_bundle_version": RULE_BUNDLE_VERSION,
                    },
                    suggested_checks=(
                        "Check whether a reviewed membership change explains the "
                        "difference.",
                        "Check the latest ingest and refresh outcomes for "
                        "failures or withheld rows.",
                        "Confirm no identity or provider mapping expired "
                        "unexpectedly.",
                        "Never backfill coverage to clear this case; restore the "
                        "source evidence instead.",
                    ),
                )
            )

    source_boundary_at = _reference_source_boundary(store, decision_date)
    return _build_scan(
        scope=scope,
        rule_id=COVERAGE_RULE_ID,
        rule_version=COVERAGE_RULE_VERSION,
        decision_date=decision_date,
        source_boundary_at=source_boundary_at,
        observations=observations,
        boundary_extra={"coverage_set_sha256": _sha(current)},
        evidence_extra={
            "universe_id": universe_id,
            "comparable_baseline": comparable,
            "baseline_as_of": (
                str(baseline.get("as_of")) if baseline is not None else None
            ),
            "current_coverage": current,
            "baseline_coverage": baseline,
            "deteriorated_metrics": len(observations),
            "minimum_drop": minimum_drop,
            "coverage_set_sha256": _sha(current),
        },
    )


# ----------------------------------------------------------------------
# Rule: changed ticker/provider mappings
# ----------------------------------------------------------------------
def scan_mapping_drift(
    *,
    store: Store,
    as_of: date | str,
    universe_id: str = "sp500",
    minimum_members: int = 450,
    maximum_members: int = 550,
) -> AnomalyScan:
    """Detect provider/ticker mappings that cannot all be true at once.

    Prices route by immutable ``security_id`` plus a dated provider symbol, so
    two contradictions matter and neither is self-correcting:

    1. one security holding two overlapping verified windows at the same
       provider — the router cannot know which symbol owns the date; and
    2. one provider symbol claimed by two different securities on the same date
       — the classic reused-ticker contamination this system exists to prevent.

    Both are reported, never repaired. A member whose identity evidence is
    missing withholds the whole scan rather than being skipped silently.
    """

    decision_date = _as_date(as_of)
    if minimum_members < 1 or maximum_members < minimum_members:
        raise ValueError("invalid anomaly-scan universe bounds")
    scope = f"{MAPPING_SCOPE_PREFIX}:{universe_id}"

    members = store.universe_identity_labels(universe_id, decision_date)
    if not minimum_members <= len(members) <= maximum_members:
        raise ValueError(
            f"{universe_id} has {len(members)} reviewed members on {decision_date}; "
            f"expected {minimum_members}-{maximum_members}"
        )

    observations: list[AnomalyObservation] = []
    examined_boundary: list[dict[str, Any]] = []
    # (provider, provider_symbol) -> the securities that claim it on this date.
    symbol_claims: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for member in sorted(members, key=lambda row: str(row.get("ticker") or "")):
        security_id = _text(member, "security_id", "reviewed universe member")
        ticker = _text(member, "ticker", "reviewed universe member").upper()
        mappings = store.provider_symbol_mappings(security_id)
        active = [
            row
            for row in mappings
            if _mapping_covers(row, decision_date)
        ]
        examined_boundary.append(
            {
                "security_id": security_id,
                "ticker": ticker,
                "active_mappings": len(active),
            }
        )

        by_provider: dict[str, list[dict[str, Any]]] = {}
        for row in active:
            provider = _text(row, "provider", "provider mapping").lower()
            symbol = _text(row, "provider_symbol", "provider mapping").upper()
            by_provider.setdefault(provider, []).append(row)
            symbol_claims.setdefault((provider, symbol), []).append(
                {"security_id": security_id, "ticker": ticker}
            )

        for provider, rows in sorted(by_provider.items()):
            if len(rows) < 2:
                continue
            symbols = sorted(
                {
                    _text(row, "provider_symbol", "provider mapping").upper()
                    for row in rows
                }
            )
            observations.append(
                AnomalyObservation(
                    fingerprint=anomaly_fingerprint(
                        rule_id=MAPPING_RULE_ID,
                        rule_version=MAPPING_RULE_VERSION,
                        scope=scope,
                        subject_type="security_provider",
                        subject_id=f"{security_id}:{provider}",
                    ),
                    rule_id=MAPPING_RULE_ID,
                    rule_version=MAPPING_RULE_VERSION,
                    scope=scope,
                    subject_type="security_provider",
                    subject_id=f"{security_id}:{provider}",
                    severity="high",
                    confidence=CONFIDENCE_EXACT_COMPARISON,
                    title=(
                        f"{ticker} has {len(rows)} overlapping {provider} mappings "
                        f"on {decision_date.isoformat()}"
                    ),
                    summary=(
                        "More than one verified provider window covers this "
                        "decision date, so price routing is ambiguous. Close or "
                        "correct the wrong window; never guess which symbol wins."
                    ),
                    old_value={"expected_active_mappings": 1},
                    new_value={
                        "active_mappings": len(rows),
                        "provider": provider,
                        "provider_symbols": symbols,
                    },
                    evidence={
                        "security_id": security_id,
                        "ticker": ticker,
                        "provider": provider,
                        "as_of": decision_date.isoformat(),
                        "windows": [_mapping_window(row) for row in rows],
                        "rule_bundle_version": RULE_BUNDLE_VERSION,
                    },
                    suggested_checks=(
                        "Confirm which provider symbol genuinely covers this date.",
                        "Close the superseded window with an exact data_end.",
                        "Re-verify any prices already routed through the wrong symbol.",
                        "Never delete history to resolve this; correct the window.",
                    ),
                )
            )

    for (provider, symbol), claimants in sorted(symbol_claims.items()):
        distinct = sorted({row["security_id"] for row in claimants})
        if len(distinct) < 2:
            continue
        tickers = sorted({row["ticker"] for row in claimants})
        observations.append(
            AnomalyObservation(
                fingerprint=anomaly_fingerprint(
                    rule_id=MAPPING_RULE_ID,
                    rule_version=MAPPING_RULE_VERSION,
                    scope=scope,
                    subject_type="provider_symbol",
                    subject_id=f"{provider}:{symbol}",
                ),
                rule_id=MAPPING_RULE_ID,
                rule_version=MAPPING_RULE_VERSION,
                scope=scope,
                subject_type="provider_symbol",
                subject_id=f"{provider}:{symbol}",
                severity="high",
                confidence=CONFIDENCE_EXACT_COMPARISON,
                title=(
                    f"{provider} symbol {symbol} is claimed by "
                    f"{len(distinct)} securities"
                ),
                summary=(
                    "One provider symbol maps to more than one listed security "
                    "on this date. This is how a reused ticker contaminates an "
                    "unrelated company's price history."
                ),
                old_value={"expected_securities": 1},
                new_value={
                    "securities": len(distinct),
                    "security_ids": distinct,
                    "tickers": tickers,
                },
                evidence={
                    "provider": provider,
                    "provider_symbol": symbol,
                    "as_of": decision_date.isoformat(),
                    "claimants": sorted(
                        claimants,
                        key=lambda row: (row["security_id"], row["ticker"]),
                    ),
                    "rule_bundle_version": RULE_BUNDLE_VERSION,
                },
                suggested_checks=(
                    "Determine which security genuinely owned this symbol on "
                    "this date.",
                    "Bound the losing mapping with an exact data_end.",
                    "Check whether prices were stored against the wrong security.",
                    "Never let a live provider result backfill a historical issuer.",
                ),
            )
        )

    source_boundary_at = _reference_source_boundary(store, decision_date)
    return _build_scan(
        scope=scope,
        rule_id=MAPPING_RULE_ID,
        rule_version=MAPPING_RULE_VERSION,
        decision_date=decision_date,
        source_boundary_at=source_boundary_at,
        observations=observations,
        boundary_extra={"examined_set_sha256": _sha(examined_boundary)},
        evidence_extra={
            "universe_id": universe_id,
            "reviewed_members": len(members),
            "examined_securities": len(examined_boundary),
            "distinct_provider_symbols": len(symbol_claims),
            "ambiguous_mappings": len(observations),
            "examined_set_sha256": _sha(examined_boundary),
        },
    )


def _mapping_covers(row: dict[str, Any], moment: date) -> bool:
    """Return whether one half-open provider window covers the decision date."""
    start = row.get("data_start")
    if start is None:
        raise ValueError("provider mapping lacks data_start")
    if _as_date(start) > moment:
        return False
    end = row.get("data_end")
    return end is None or _as_date(end) > moment


def _mapping_window(row: dict[str, Any]) -> dict[str, Any]:
    end = row.get("data_end")
    return {
        "provider_symbol": _text(row, "provider_symbol", "provider mapping").upper(),
        "data_start": _as_date(row["data_start"]).isoformat(),
        "data_end": _as_date(end).isoformat() if end is not None else None,
        "verified_date": _optional(row.get("verified_date")),
        "source": _optional(row.get("source")),
    }


def _issuer_members(members: list[dict[str, Any]]) -> list[tuple[str, tuple[str, ...]]]:
    """Group reviewed members by issuer, in deterministic ticker order.

    Fundamentals are issuer-level, but membership is security-level, and the
    S&P 500 holds three dual-class issuers (Alphabet, Fox, News Corp) whose two
    listed securities share one ``issuer_id``. Iterating members would query
    each of those issuers twice and emit the same finding twice, and the ledger
    rejects a scan containing duplicate fingerprints — so every issuer is
    visited exactly once, carrying all of its member tickers as evidence.
    """
    grouped: dict[str, set[str]] = {}
    for member in members:
        issuer_id = _text(member, "issuer_id", "reviewed universe member")
        ticker = _text(member, "ticker", "reviewed universe member").upper()
        grouped.setdefault(issuer_id, set()).add(ticker)
    return [
        (issuer_id, tuple(sorted(tickers)))
        for issuer_id, tickers in sorted(
            grouped.items(), key=lambda row: (sorted(row[1])[0], row[0])
        )
    ]


# ----------------------------------------------------------------------
# Rule: sudden share-count changes
# ----------------------------------------------------------------------
def scan_share_count_jump(
    *,
    store: Store,
    as_of: date | str,
    universe_id: str = "sp500",
    minimum_members: int = 450,
    maximum_members: int = 550,
    jump_threshold: float = SHARES_JUMP_THRESHOLD,
    lookback_filings: int = SHARES_LOOKBACK_FILINGS,
) -> AnomalyScan:
    """Detect implausible share-count movement between consecutive filings.

    Share count drives market cap, so a bad value silently corrupts every Value
    factor built on it. Genuine causes exist — splits, large issuance, buybacks
    — but a move past the threshold between two consecutive point-in-time
    filings is worth a human look. The rule does NOT look up the corresponding
    corporate action: confirming one is the operator's first suggested check,
    and co-reporting reviewed splits belongs to a later rule version.

    Two stored values need no threshold to be wrong. A non-positive share count
    is impossible for a listed company and is reported directly, and it is kept
    out of the ratio comparison so it cannot divide by zero or manufacture a
    fake jump in the surrounding series.

    The rule never repairs a value and never guesses a split from the ratio.
    Expect a large historical backlog on first run: the comparison spans the
    issuer's whole retained filing history, and a retroactive post-split
    restatement of an old period is a genuine, reviewable difference.
    """

    decision_date = _as_date(as_of)
    if minimum_members < 1 or maximum_members < minimum_members:
        raise ValueError("invalid anomaly-scan universe bounds")
    if not 0 < jump_threshold < 10:
        raise ValueError("share-count jump threshold must be a positive fraction")
    if lookback_filings < 2:
        raise ValueError("share-count lookback requires at least two filings")
    scope = f"{SHARES_SCOPE_PREFIX}:{universe_id}"

    members = store.universe_identity_labels(universe_id, decision_date)
    if not minimum_members <= len(members) <= maximum_members:
        raise ValueError(
            f"{universe_id} has {len(members)} reviewed members on {decision_date}; "
            f"expected {minimum_members}-{maximum_members}"
        )

    observations: list[AnomalyObservation] = []
    examined_boundary: list[dict[str, Any]] = []
    examined_issuers = 0

    for issuer_id, tickers in _issuer_members(members):
        ticker = tickers[0]
        window = store.query(
            """
            SELECT period_end, as_of_date, value
            FROM fundamentals
            WHERE issuer_id = ?
              AND metric = ?
              AND as_of_date <= CAST(? AS DATE)
              AND period_end <= as_of_date
              AND value IS NOT NULL
            ORDER BY period_end DESC, as_of_date DESC
            LIMIT ?
            """,
            (issuer_id, SHARES_METRIC, decision_date.isoformat(), lookback_filings),
        )
        history = list(reversed(window))
        if len(history) < 2:
            # Nothing comparable is claimed for this issuer.
            continue
        examined_issuers += 1
        examined_boundary.append(
            {
                "issuer_id": issuer_id,
                "ticker": ticker,
                "tickers": list(tickers),
                "observations": len(history),
            }
        )

        # A stored non-positive count is itself the anomaly. Report it, and
        # keep it out of the ratio comparison so one bad row cannot divide by
        # zero or manufacture a fake jump in the surrounding series.
        usable: list[dict[str, Any]] = []
        for row in history:
            if _share_value(row, issuer_id) > 0:
                usable.append(row)
                continue
            observations.append(
                _invalid_share_count_observation(
                    scope=scope,
                    issuer_id=issuer_id,
                    ticker=ticker,
                    tickers=tickers,
                    row=row,
                    decision_date=decision_date,
                )
            )

        for previous, current in zip(usable, usable[1:], strict=False):
            previous_shares = _share_value(previous, issuer_id)
            current_shares = _share_value(current, issuer_id)
            change = (current_shares - previous_shares) / previous_shares
            if abs(change) < jump_threshold:
                continue

            current_period = _as_date(current["period_end"])
            previous_period = _as_date(previous["period_end"])
            # One fiscal period is restated across several knowable dates, so
            # the period alone is not a unique case identity: two jumps inside
            # the same period would collide into one fingerprint and the ledger
            # would reject the whole scan.
            subject_id = (
                f"{issuer_id}@{current_period.isoformat()}"
                f"#{_as_date(current['as_of_date']).isoformat()}"
            )
            observations.append(
                AnomalyObservation(
                    fingerprint=anomaly_fingerprint(
                        rule_id=SHARES_RULE_ID,
                        rule_version=SHARES_RULE_VERSION,
                        scope=scope,
                        subject_type="issuer_period",
                        subject_id=subject_id,
                    ),
                    rule_id=SHARES_RULE_ID,
                    rule_version=SHARES_RULE_VERSION,
                    scope=scope,
                    subject_type="issuer_period",
                    subject_id=subject_id,
                    severity="high" if abs(change) >= 0.5 else "medium",
                    confidence=CONFIDENCE_REPORTED_SOURCE,
                    title=(
                        f"{ticker} share count moved {change:.1%} into "
                        f"{current_period.isoformat()}"
                    ),
                    summary=(
                        "Reported shares outstanding changed sharply between two "
                        "consecutive point-in-time filings. Confirm a split, "
                        "issuance or buyback explains it before trusting any "
                        "market-cap or per-share factor built on this value."
                    ),
                    old_value={
                        "period_end": previous_period.isoformat(),
                        "as_of_date": _as_date(previous["as_of_date"]).isoformat(),
                        "shares_out": previous_shares,
                    },
                    new_value={
                        "period_end": current_period.isoformat(),
                        "as_of_date": _as_date(current["as_of_date"]).isoformat(),
                        "shares_out": current_shares,
                        "change_fraction": change,
                        "implied_ratio": current_shares / previous_shares,
                    },
                    evidence={
                        "issuer_id": issuer_id,
                        "ticker": ticker,
                        "tickers": list(tickers),
                        "metric": SHARES_METRIC,
                        "jump_threshold": jump_threshold,
                        "decision_evidence_as_of": decision_date.isoformat(),
                        "rule_bundle_version": RULE_BUNDLE_VERSION,
                    },
                    suggested_checks=(
                        "Check the issuer's filing for a split, offering or "
                        "buyback covering this period.",
                        "Confirm the SEC cover-page share count was parsed with "
                        "the right unit and scale.",
                        "Re-check any market-cap or per-share factor computed "
                        "from the newer value.",
                        "Never rescale a stored share count to make the series "
                        "look smooth.",
                    ),
                )
            )

    source_boundary_at = _reference_source_boundary(store, decision_date)
    return _build_scan(
        scope=scope,
        rule_id=SHARES_RULE_ID,
        rule_version=SHARES_RULE_VERSION,
        decision_date=decision_date,
        source_boundary_at=source_boundary_at,
        observations=observations,
        boundary_extra={"examined_set_sha256": _sha(examined_boundary)},
        evidence_extra={
            "universe_id": universe_id,
            "reviewed_members": len(members),
            "examined_issuers": examined_issuers,
            "metric": SHARES_METRIC,
            "jump_threshold": jump_threshold,
            "lookback_filings": lookback_filings,
            "share_count_jumps": len(observations),
            "examined_set_sha256": _sha(examined_boundary),
        },
    )


def _invalid_share_count_observation(
    *,
    scope: str,
    issuer_id: str,
    ticker: str,
    tickers: tuple[str, ...],
    row: dict[str, Any],
    decision_date: date,
) -> AnomalyObservation:
    """Report one stored share count that cannot be true."""
    period_end = _as_date(row["period_end"])
    known_on = _as_date(row["as_of_date"])
    shares = float(row["value"])
    subject_id = f"{issuer_id}@{period_end.isoformat()}#{known_on.isoformat()}"
    return AnomalyObservation(
        fingerprint=anomaly_fingerprint(
            rule_id=SHARES_RULE_ID,
            rule_version=SHARES_RULE_VERSION,
            scope=scope,
            subject_type="issuer_share_count",
            subject_id=subject_id,
        ),
        rule_id=SHARES_RULE_ID,
        rule_version=SHARES_RULE_VERSION,
        scope=scope,
        subject_type="issuer_share_count",
        subject_id=subject_id,
        severity="high",
        confidence=CONFIDENCE_EXACT_COMPARISON,
        title=(
            f"{ticker} reports a non-positive share count "
            f"({shares:g}) for {period_end.isoformat()}"
        ),
        summary=(
            "A listed company cannot have zero or negative shares "
            "outstanding, so this stored value is wrong. Any market-cap or "
            "per-share factor built on it is meaningless. Correct the parse "
            "or withhold the row; never substitute a neighbouring period."
        ),
        old_value={"expected": "a positive share count"},
        new_value={
            "period_end": period_end.isoformat(),
            "as_of_date": known_on.isoformat(),
            "shares_out": shares,
        },
        evidence={
            "issuer_id": issuer_id,
            "ticker": ticker,
            "tickers": list(tickers),
            "metric": SHARES_METRIC,
            "decision_evidence_as_of": decision_date.isoformat(),
            "rule_bundle_version": RULE_BUNDLE_VERSION,
        },
        suggested_checks=(
            "Read the exact retained filing payload for this period and "
            "confirm the cover-page share count.",
            "Check whether the parser mapped an empty or placeholder tag to "
            "zero instead of withholding the row.",
            "Confirm no factor score for this issuer consumed this value.",
            "Withhold the row rather than guessing a replacement count.",
        ),
    )


def _share_value(row: dict[str, Any], issuer_id: str) -> float:
    """Return one stored share count without judging whether it is plausible.

    A missing value is a contract violation and still fails closed. A stored
    zero or negative count is NOT: it is precisely the implausible share count
    this rule exists to surface, and the live archive holds 80 such historical
    rows. Raising on them withheld every scan for the whole universe and hid
    the finding, so they are reported as observations instead.
    """
    value = row.get("value")
    if value is None:
        raise ValueError(f"share-count row lacks a value: {issuer_id}")
    return float(value)


# ----------------------------------------------------------------------
# Rule: duplicate or conflicting filings
# ----------------------------------------------------------------------
def scan_conflicting_filings(
    *,
    store: Store,
    as_of: date | str,
    universe_id: str = "sp500",
    minimum_members: int = 450,
    maximum_members: int = 550,
) -> AnomalyScan:
    """Detect one economic key carrying two different values.

    ``fundamentals`` is keyed by ``(ticker, period_end, as_of_date, metric)``, so
    the same issuer reporting one metric for one period, knowable on one date,
    must resolve to exactly one value. Two distinct values mean a restatement was
    stored as a duplicate, or two source rows disagree. Either way a factor read
    could silently pick the wrong one.

    The rule reports the conflict and never chooses a winner.
    """

    decision_date = _as_date(as_of)
    if minimum_members < 1 or maximum_members < minimum_members:
        raise ValueError("invalid anomaly-scan universe bounds")
    scope = f"{FILINGS_SCOPE_PREFIX}:{universe_id}"

    members = store.universe_identity_labels(universe_id, decision_date)
    if not minimum_members <= len(members) <= maximum_members:
        raise ValueError(
            f"{universe_id} has {len(members)} reviewed members on {decision_date}; "
            f"expected {minimum_members}-{maximum_members}"
        )

    observations: list[AnomalyObservation] = []
    examined_boundary: list[dict[str, Any]] = []

    for issuer_id, tickers in _issuer_members(members):
        ticker = tickers[0]
        conflicts = store.query(
            """
            SELECT period_end, as_of_date, metric,
                   COUNT(DISTINCT value) AS distinct_values,
                   MIN(value) AS minimum_value,
                   MAX(value) AS maximum_value
            FROM fundamentals
            WHERE issuer_id = ?
              AND as_of_date <= CAST(? AS DATE)
              AND period_end <= as_of_date
              AND value IS NOT NULL
            GROUP BY period_end, as_of_date, metric
            HAVING COUNT(DISTINCT value) > 1
            ORDER BY period_end, metric
            """,
            (issuer_id, decision_date.isoformat()),
        )
        examined_boundary.append(
            {
                "issuer_id": issuer_id,
                "ticker": ticker,
                "tickers": list(tickers),
                "conflicts": len(conflicts),
            }
        )
        for row in conflicts:
            metric = _text(row, "metric", "conflicting filing")
            period_end = _as_date(row["period_end"])
            known_on = _as_date(row["as_of_date"])
            distinct_values = _exact_count(
                row.get("distinct_values"),
                field="distinct_values",
                label="conflicting filing",
            )
            # The query groups by as_of_date, so one metric/period restated on
            # two knowable dates yields two rows. Omitting as_of_date here
            # collapsed them into one fingerprint and the ledger rejects a scan
            # that repeats a fingerprint.
            subject_id = (
                f"{issuer_id}:{metric}@{period_end.isoformat()}"
                f"#{known_on.isoformat()}"
            )
            observations.append(
                AnomalyObservation(
                    fingerprint=anomaly_fingerprint(
                        rule_id=FILINGS_RULE_ID,
                        rule_version=FILINGS_RULE_VERSION,
                        scope=scope,
                        subject_type="issuer_metric_period",
                        subject_id=subject_id,
                    ),
                    rule_id=FILINGS_RULE_ID,
                    rule_version=FILINGS_RULE_VERSION,
                    scope=scope,
                    subject_type="issuer_metric_period",
                    subject_id=subject_id,
                    severity="high",
                    confidence=CONFIDENCE_EXACT_COMPARISON,
                    title=(
                        f"{ticker} reports {distinct_values} values for {metric} "
                        f"in {period_end.isoformat()}"
                    ),
                    summary=(
                        "One issuer, metric and fiscal period knowable on one "
                        "date resolves to more than one stored value. A factor "
                        "read could pick either. Identify the correct source row "
                        "instead of averaging or deleting."
                    ),
                    old_value={"expected_distinct_values": 1},
                    new_value={
                        "distinct_values": distinct_values,
                        "minimum_value": row.get("minimum_value"),
                        "maximum_value": row.get("maximum_value"),
                        "period_end": period_end.isoformat(),
                        "as_of_date": known_on.isoformat(),
                        "metric": metric,
                    },
                    evidence={
                        "issuer_id": issuer_id,
                        "ticker": ticker,
                        "tickers": list(tickers),
                        "metric": metric,
                        "period_end": period_end.isoformat(),
                        "as_of_date": known_on.isoformat(),
                        "decision_evidence_as_of": decision_date.isoformat(),
                        "rule_bundle_version": RULE_BUNDLE_VERSION,
                    },
                    suggested_checks=(
                        "Compare both values against the exact retained filing "
                        "payload.",
                        "Check whether a restatement was stored as a duplicate "
                        "rather than a new version.",
                        "Confirm unit and scale parsing for this metric.",
                        "Never average conflicting values or delete one without "
                        "source evidence.",
                    ),
                )
            )

    source_boundary_at = _reference_source_boundary(store, decision_date)
    return _build_scan(
        scope=scope,
        rule_id=FILINGS_RULE_ID,
        rule_version=FILINGS_RULE_VERSION,
        decision_date=decision_date,
        source_boundary_at=source_boundary_at,
        observations=observations,
        boundary_extra={"examined_set_sha256": _sha(examined_boundary)},
        evidence_extra={
            "universe_id": universe_id,
            "reviewed_members": len(members),
            "examined_issuers": len(examined_boundary),
            "conflicting_keys": len(observations),
            "examined_set_sha256": _sha(examined_boundary),
        },
    )


# ----------------------------------------------------------------------
# Rule: factor percentile jumps
# ----------------------------------------------------------------------
def measure_universe_factor_percentiles(
    *,
    store: Store,
    as_of: date | str,
    universe_id: str = "sp500",
    factor_model: str = "qv",
) -> dict[str, Any]:
    """Return the exact composite percentile for every scored member.

    There is no stored factor-score history: scores are computed on demand from
    point-in-time evidence. So the caller keeps this snapshot and feeds it back
    as the next scan's baseline, exactly as the coverage rule works. The payload
    is deliberately separate from the scan evidence, which carries only a hash
    and counts — 503 embedded scores on both sides would not fit the ledger's
    64 KiB evidence limit.
    """
    from aios.factors.composite import compute_composite

    decision_date = _as_date(as_of)
    if factor_model not in FACTOR_SUPPORTED_MODELS:
        raise ValueError(
            f"unsupported factor model: {factor_model!r}; "
            f"expected one of {list(FACTOR_SUPPORTED_MODELS)}"
        )
    members = store.universe_identity_labels(universe_id, decision_date)
    if not members:
        raise ValueError(f"{universe_id} has no active members on {decision_date}")

    tickers = sorted(
        _text(member, "ticker", "reviewed universe member").upper()
        for member in members
    )
    rows = compute_composite(tickers, decision_date.isoformat(), store)

    attribute = "qv_score" if factor_model == "qv" else "qvml_score"
    scores: dict[str, float] = {}
    withheld: list[str] = []
    for row in rows:
        value = getattr(row, attribute)
        ticker = str(row.ticker).upper()
        if value is None:
            # A withheld score is a coverage question, not a factor jump. It is
            # counted, never treated as a zero.
            withheld.append(ticker)
            continue
        scores[ticker] = float(value)
    return {
        "as_of": decision_date.isoformat(),
        "universe_id": universe_id,
        "factor_model": factor_model,
        "members": len(members),
        "scored_members": len(scores),
        "withheld_members": sorted(withheld),
        "scores": scores,
    }


FACTOR_BASELINE_ARTIFACT_DIR = Path("data/anomaly_baselines/factor_percentiles")


def _factor_baseline_scope_slug(universe_id: str, factor_model: str) -> str:
    return f"{universe_id}-{factor_model}"


def record_factor_percentile_baseline(
    snapshot: dict[str, Any],
    *,
    baseline_dir: Path = FACTOR_BASELINE_ARTIFACT_DIR,
) -> Path:
    """Durably record one factor-percentile snapshot as the next scan's baseline.

    The scan ledger deliberately excludes the full score map from its own
    evidence — a 503-name mapping does not fit the 64 KiB evidence limit — so
    `scan_factor_percentile_jump`'s baseline cannot come from
    `latest_anomaly_scan_evidence` the way `coverage_deterioration`'s does.
    This is the separate, small write-once store that fills that gap: one
    immutable file per snapshot, content-addressed by decision date, entirely
    outside the operations ledger.
    """
    scope_slug = _factor_baseline_scope_slug(
        str(snapshot["universe_id"]), str(snapshot["factor_model"])
    )
    destination = baseline_dir / scope_slug / f"{snapshot['as_of']}.json"
    publish_text_write_once(destination, canonical_json(snapshot))
    return destination


def latest_factor_percentile_baseline(
    *,
    universe_id: str = "sp500",
    factor_model: str = "qv",
    before: date | str | None = None,
    baseline_dir: Path = FACTOR_BASELINE_ARTIFACT_DIR,
) -> dict[str, Any] | None:
    """Return the most recent recorded snapshot for one scope, if any.

    ``before`` restricts the search to snapshots dated strictly earlier than
    the given date — the caller comparing a new decision date against history
    should never pick a "baseline" that is actually later than the scan it is
    meant to explain. Returns ``None`` when nothing has been recorded yet: a
    legitimate first-run state, not an error.
    """
    scope_slug = _factor_baseline_scope_slug(universe_id, factor_model)
    directory = baseline_dir / scope_slug
    if not directory.is_dir():
        return None
    cutoff = _as_date(before) if before is not None else None
    candidates: list[tuple[date, Path]] = []
    for candidate in directory.glob("*.json"):
        try:
            snapshot_date = date.fromisoformat(candidate.stem)
        except ValueError:
            continue
        if cutoff is not None and snapshot_date >= cutoff:
            continue
        candidates.append((snapshot_date, candidate))
    if not candidates:
        return None
    _, latest_path = max(candidates, key=lambda row: row[0])
    return json.loads(latest_path.read_text(encoding="utf-8"))


def scan_factor_percentile_jump(
    *,
    store: Store,
    as_of: date | str,
    baseline: dict[str, Any] | None,
    universe_id: str = "sp500",
    factor_model: str = "qv",
    jump_threshold: float = FACTOR_JUMP_THRESHOLD,
    current: dict[str, Any] | None = None,
) -> AnomalyScan:
    """Detect research scores that moved further than evidence should allow.

    A composite percentile is a cross-sectional research score. It moves when
    peers move, so ordinary drift is expected and uninteresting. A jump of tens
    of percentile points between two comparable decision dates usually means the
    inputs changed underneath the score — a restated fundamental, a bad price, a
    lost mapping — not that the company transformed.

    The rule reports the jump and never adjusts a score or withholds a member.
    ``baseline`` is the snapshot from the previous comparable measurement; when
    it is ``None`` the scan emits zero observations rather than inventing a
    first-run finding, exactly like the coverage rule.

    ``current`` lets a caller pass a snapshot it already computed. Recomputing
    the full 503-member composite is the expensive part of this scan, so a
    caller that just measured it should not pay twice.
    """

    decision_date = _as_date(as_of)
    if not 0 < jump_threshold <= 100:
        raise ValueError("factor jump threshold must be in (0, 100] percentile points")
    scope = f"{FACTOR_SCOPE_PREFIX}:{universe_id}"

    if current is not None:
        measured = current
    else:
        measured = measure_universe_factor_percentiles(
            store=store,
            as_of=decision_date,
            universe_id=universe_id,
            factor_model=factor_model,
        )
    if str(measured.get("as_of") or "") != decision_date.isoformat():
        raise ValueError("factor snapshot describes a different decision date")
    if str(measured.get("factor_model") or "") != factor_model:
        raise ValueError("factor snapshot describes a different factor model")

    observations: list[AnomalyObservation] = []
    comparable = False
    compared_members = 0
    if baseline is not None:
        comparable = True
        baseline_as_of = _as_date(_text(baseline, "as_of", "factor baseline"))
        if baseline_as_of > decision_date:
            raise ValueError("factor baseline is later than the scanned decision date")
        if str(baseline.get("universe_id") or "") != universe_id:
            raise ValueError("factor baseline describes a different universe")
        if str(baseline.get("factor_model") or "") != factor_model:
            # Comparing a QV percentile against a QVML percentile would
            # manufacture jumps out of a definition change.
            raise ValueError("factor baseline describes a different factor model")

        previous_scores = baseline.get("scores")
        if not isinstance(previous_scores, dict):
            raise ValueError("factor baseline lacks a scores mapping")
        current_scores = measured["scores"]

        for ticker in sorted(set(previous_scores) & set(current_scores)):
            previous_value = _exact_percentile(previous_scores[ticker], ticker=ticker)
            current_value = _exact_percentile(current_scores[ticker], ticker=ticker)
            compared_members += 1
            move = current_value - previous_value
            if abs(move) < jump_threshold:
                continue
            subject_id = f"{universe_id}:{factor_model}:{ticker}"
            observations.append(
                AnomalyObservation(
                    fingerprint=anomaly_fingerprint(
                        rule_id=FACTOR_RULE_ID,
                        rule_version=FACTOR_RULE_VERSION,
                        scope=scope,
                        subject_type="member_factor_score",
                        subject_id=subject_id,
                    ),
                    rule_id=FACTOR_RULE_ID,
                    rule_version=FACTOR_RULE_VERSION,
                    scope=scope,
                    subject_type="member_factor_score",
                    subject_id=subject_id,
                    severity="high" if abs(move) >= 60 else "medium",
                    confidence=CONFIDENCE_EXACT_COMPARISON,
                    title=(
                        f"{ticker} {factor_model.upper()} percentile moved "
                        f"{move:+.1f} points since {baseline_as_of.isoformat()}"
                    ),
                    summary=(
                        "A cross-sectional research score moved further than "
                        "ordinary peer drift explains. Check whether an input "
                        "changed underneath it — a restated fundamental, a bad "
                        "price, or a lost mapping — before trusting the new "
                        "ranking. Never adjust a score to smooth this."
                    ),
                    old_value={
                        "as_of": baseline_as_of.isoformat(),
                        "percentile": previous_value,
                    },
                    new_value={
                        "as_of": decision_date.isoformat(),
                        "percentile": current_value,
                        "move_points": move,
                    },
                    evidence={
                        "universe_id": universe_id,
                        "ticker": ticker,
                        "factor_model": factor_model,
                        "jump_threshold": jump_threshold,
                        "baseline_as_of": baseline_as_of.isoformat(),
                        "decision_evidence_as_of": decision_date.isoformat(),
                        "rule_bundle_version": RULE_BUNDLE_VERSION,
                    },
                    suggested_checks=(
                        "Compare the point-in-time fundamentals behind both "
                        "scores for a restatement.",
                        "Check the price and share count this score consumed.",
                        "Confirm the provider mapping did not change between "
                        "the two decision dates.",
                        "Rankings are research scores, not expected returns; "
                        "never trade this signal to resolve the case.",
                    ),
                )
            )

    source_boundary_at = _reference_source_boundary(store, decision_date)
    return _build_scan(
        scope=scope,
        rule_id=FACTOR_RULE_ID,
        rule_version=FACTOR_RULE_VERSION,
        decision_date=decision_date,
        source_boundary_at=source_boundary_at,
        observations=observations,
        boundary_extra={"factor_set_sha256": _sha(measured)},
        evidence_extra={
            "universe_id": universe_id,
            "factor_model": factor_model,
            "comparable_baseline": comparable,
            "baseline_as_of": (
                str(baseline.get("as_of")) if baseline is not None else None
            ),
            "members": measured["members"],
            "scored_members": measured["scored_members"],
            "withheld_members": len(measured["withheld_members"]),
            "compared_members": compared_members,
            "jump_threshold": jump_threshold,
            "percentile_jumps": len(observations),
            # The full score maps stay out of the evidence deliberately: two
            # 503-entry mappings would not fit the ledger's 64 KiB limit.
            "factor_set_sha256": _sha(measured),
        },
    )


def _exact_percentile(value: Any, *, ticker: str) -> float:
    """Require a genuine 0-100 percentile.

    A silently coerced string or an out-of-range value would manufacture or hide
    a jump, so the range is checked exactly.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"factor percentile must be numeric for {ticker}: {value!r}")
    percentile = float(value)
    if not 0.0 <= percentile <= 100.0:
        raise ValueError(
            f"factor percentile out of range for {ticker}: {percentile}"
        )
    return percentile


# ----------------------------------------------------------------------
# Shared scan assembly and detector registry
# ----------------------------------------------------------------------
def _build_scan(
    *,
    scope: str,
    rule_id: str,
    rule_version: str,
    decision_date: date,
    source_boundary_at: datetime,
    observations: list[AnomalyObservation],
    boundary_extra: dict[str, Any],
    evidence_extra: dict[str, Any],
) -> AnomalyScan:
    """Assemble one complete scan with the shared safety and boundary contract.

    ``scan_id`` is derived from the boundary hash so an identical comparison is
    idempotent in the ledger, matching the SEC rule's behaviour.
    """
    executed = f"{rule_id}@{rule_version}"
    # The ledger refuses a scan that repeats a fingerprint, which would surface
    # far downstream as an opaque record-time failure. Catch it here, where the
    # rule and the colliding subject are still known.
    seen: set[str] = set()
    for row in observations:
        if row.fingerprint in seen:
            raise ValueError(
                f"{executed} produced a duplicate fingerprint for "
                f"{row.subject_type} {row.subject_id!r}; its subject identity "
                "is not unique within one scan"
            )
        seen.add(row.fingerprint)
    observation_boundary = [
        {
            "fingerprint": row.fingerprint,
            "evidence_sha256": _sha(
                {
                    "old_value": row.old_value,
                    "new_value": row.new_value,
                    "evidence": row.evidence,
                }
            ),
        }
        for row in observations
    ]
    boundary = {
        "rule_bundle_version": RULE_BUNDLE_VERSION,
        "scope": scope,
        "decision_evidence_as_of": decision_date.isoformat(),
        "evidence_observed_through": source_boundary_at.isoformat(),
        "temporal_mode": "retrospective_review_no_backfill",
        "executed_rules": [executed],
        "observations": observation_boundary,
        **boundary_extra,
    }
    boundary_hash = _sha(boundary)
    return AnomalyScan(
        scan_id=f"dqs-{boundary_hash[:32]}",
        rule_bundle_version=RULE_BUNDLE_VERSION,
        scope=scope,
        source_boundary_sha256=boundary_hash,
        source_boundary_at=source_boundary_at,
        executed_rules=(executed,),
        observations=tuple(observations),
        evidence={
            "as_of": decision_date.isoformat(),
            "decision_evidence_as_of": decision_date.isoformat(),
            "evidence_observed_through": source_boundary_at.isoformat(),
            "temporal_mode": "retrospective_review_no_backfill",
            "executed_rules": [executed],
            "safety": {
                "data_repairs": 0,
                "readiness_overrides": 0,
                "paper_actions": 0,
                "broker_actions": 0,
            },
            **evidence_extra,
        },
    )


@dataclass(frozen=True)
class DetectorSpec:
    """One registered detector and the ledger scope it owns.

    Each rule family writes into its own scope namespace. The ledger enforces a
    monotonic source boundary per case, so separate scopes let a price scan and
    a coverage scan advance independently without either blocking the other, and
    without changing the frozen ``rule_bundle_version`` contract.
    """

    rule_id: str
    rule_version: str
    scope_prefix: str
    subject_type: str
    requires_baseline: bool = False


SEC_DETECTOR = DetectorSpec(
    rule_id=SEC_RULE_ID,
    rule_version=SEC_RULE_VERSION,
    scope_prefix=SEC_SCOPE_PREFIX,
    subject_type="issuer",
)
PRICE_DETECTOR = DetectorSpec(
    rule_id=PRICE_RULE_ID,
    rule_version=PRICE_RULE_VERSION,
    scope_prefix=PRICE_SCOPE_PREFIX,
    subject_type="security_session",
)
COVERAGE_DETECTOR = DetectorSpec(
    rule_id=COVERAGE_RULE_ID,
    rule_version=COVERAGE_RULE_VERSION,
    scope_prefix=COVERAGE_SCOPE_PREFIX,
    subject_type="coverage_metric",
    requires_baseline=True,
)
MAPPING_DETECTOR = DetectorSpec(
    rule_id=MAPPING_RULE_ID,
    rule_version=MAPPING_RULE_VERSION,
    scope_prefix=MAPPING_SCOPE_PREFIX,
    subject_type="security_provider",
)
SHARES_DETECTOR = DetectorSpec(
    rule_id=SHARES_RULE_ID,
    rule_version=SHARES_RULE_VERSION,
    scope_prefix=SHARES_SCOPE_PREFIX,
    subject_type="issuer_period",
)
FILINGS_DETECTOR = DetectorSpec(
    rule_id=FILINGS_RULE_ID,
    rule_version=FILINGS_RULE_VERSION,
    scope_prefix=FILINGS_SCOPE_PREFIX,
    subject_type="issuer_metric_period",
)
FACTOR_DETECTOR = DetectorSpec(
    rule_id=FACTOR_RULE_ID,
    rule_version=FACTOR_RULE_VERSION,
    scope_prefix=FACTOR_SCOPE_PREFIX,
    subject_type="member_factor_score",
    requires_baseline=True,
)
RULE_REGISTRY: tuple[DetectorSpec, ...] = (
    SEC_DETECTOR,
    PRICE_DETECTOR,
    COVERAGE_DETECTOR,
    MAPPING_DETECTOR,
    SHARES_DETECTOR,
    FILINGS_DETECTOR,
    FACTOR_DETECTOR,
)


def registered_rule_ids() -> tuple[str, ...]:
    """Return every rule identifier this build can execute."""
    return tuple(spec.rule_id for spec in RULE_REGISTRY)


def rule_scope(rule_id: str, universe_id: str = "sp500") -> str:
    """Return the ledger scope one rule writes into."""
    for spec in RULE_REGISTRY:
        if spec.rule_id == rule_id:
            return f"{spec.scope_prefix}:{universe_id}"
    raise ValueError(f"unknown anomaly rule: {rule_id}")


def run_detectors(
    *,
    store: Store,
    as_of: date | str,
    rules: tuple[str, ...] | None = None,
    universe_id: str = "sp500",
    project_root: Path | None = None,
    coverage_baseline: dict[str, Any] | None = None,
    factor_baseline: dict[str, Any] | None = None,
    factor_model: str = "qv",
) -> tuple[AnomalyScan, ...]:
    """Run the selected detectors and return one independent scan per rule.

    Each scan owns its scope, so the caller may record them separately and one
    family's source boundary never constrains another's. A rule that cannot run
    honestly raises rather than returning a partial scan, and the exception
    names the rule so the operator can see exactly which evidence is missing.

    ``coverage_baseline`` is the coverage payload from the previous complete
    coverage scan. Passing ``None`` is valid and yields a baseline-free scan
    with zero observations.
    """
    selected = rules if rules is not None else registered_rule_ids()
    unknown = [rule_id for rule_id in selected if rule_id not in registered_rule_ids()]
    if unknown:
        raise ValueError(f"unknown anomaly rule(s): {sorted(unknown)}")

    scans: list[AnomalyScan] = []
    for rule_id in selected:
        if rule_id == SEC_RULE_ID:
            scans.append(
                scan_sec_fundamental_coverage(
                    store=store,
                    as_of=as_of,
                    project_root=project_root,
                    universe_id=universe_id,
                )
            )
        elif rule_id == PRICE_RULE_ID:
            scans.append(
                scan_price_action_mismatch(
                    store=store,
                    as_of=as_of,
                    universe_id=universe_id,
                )
            )
        elif rule_id == COVERAGE_RULE_ID:
            scans.append(
                scan_coverage_deterioration(
                    store=store,
                    as_of=as_of,
                    baseline=coverage_baseline,
                    universe_id=universe_id,
                )
            )
        elif rule_id == MAPPING_RULE_ID:
            scans.append(
                scan_mapping_drift(
                    store=store,
                    as_of=as_of,
                    universe_id=universe_id,
                )
            )
        elif rule_id == SHARES_RULE_ID:
            scans.append(
                scan_share_count_jump(
                    store=store,
                    as_of=as_of,
                    universe_id=universe_id,
                )
            )
        elif rule_id == FILINGS_RULE_ID:
            scans.append(
                scan_conflicting_filings(
                    store=store,
                    as_of=as_of,
                    universe_id=universe_id,
                )
            )
        elif rule_id == FACTOR_RULE_ID:
            scans.append(
                scan_factor_percentile_jump(
                    store=store,
                    as_of=as_of,
                    baseline=factor_baseline,
                    universe_id=universe_id,
                    factor_model=factor_model,
                )
            )
        else:  # pragma: no cover - registry and dispatch are kept in step
            raise ValueError(f"registered rule has no dispatch entry: {rule_id}")
    return tuple(scans)

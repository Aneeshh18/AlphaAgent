"""Read-only planning for a governed SEC Company Facts v3 replay.

This build can inspect exact, already-captured Company Facts responses and
produce a deterministic review plan.  It deliberately has no activation path:
no database row, raw snapshot, paper document, or provider is mutated or
contacted.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import io
import json
import zlib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aios.artifacts import publish_text_write_once
from aios.canonical import canonical_json
from aios.ingest.edgar import (
    COMPANYFACTS_CAPTURE_PARSER_VERSION,
    COMPANYFACTS_NEXT_PARSER_VERSION,
    COMPANYFACTS_PARSER_VERSION,
    canonical_sec_fundamental_row_sha256,
    parse_sec_companyfacts_response_v3,
    replay_sec_companyfacts_response,
)
from aios.raw_snapshots import canonical_parsed_rows_sha256
from aios.sec_rejections import (
    accepted_sec_fundamental_outcome,
    decode_rejection_codes,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aios.storage.store import Store


COMPANYFACTS_REPLAY_PLAN_DOCUMENT_KIND = "aios.companyfacts-replay-plan"
COMPANYFACTS_REPLAY_PLAN_SCHEMA_VERSION = "companyfacts-replay-plan.v1"
COMPANYFACTS_REPLAY_POLICY_CONTRACT = "companyfacts-v3-replay-planner.v1"
COMPANYFACTS_REPLAY_PLAN_REPORT_DIRECTORY = Path("data/reports/companyfacts_replays/plans")
MAX_COMPANYFACTS_SNAPSHOT_STORED_BYTES = 64 * 1024 * 1024
MAX_COMPANYFACTS_SNAPSHOT_ORIGINAL_BYTES = 256 * 1024 * 1024

CAPTURE_ONLY_PARSER_VERSIONS = frozenset(
    {
        COMPANYFACTS_CAPTURE_PARSER_VERSION,
        # Historical captures used this label before the explicit capture
        # suffix was introduced.
        "sec-companyfacts-v1",
    }
)

_LOWER_HEX = frozenset("0123456789abcdef")
_ACTIVATION_FORBIDDEN_ACTIONS = (
    "database_mutation",
    "raw_snapshot_mutation",
    "provider_fetch",
    "paper_account_mutation",
    "broker_order",
    "retrospective_fill",
)
_ISSUER_REASON_CODES = frozenset(
    {
        "ambiguous_identity",
        "capture_only",
        "current_relation_mismatch",
        "failed_ingest",
        "identity_cik_mismatch",
        "incomplete_ingest_evidence",
        "incomplete_parse_evidence",
        "invalid_observation_time",
        "missing_reviewed_identity",
        "no_scoped_source_evidence",
        "not_exact_response",
        "raw_payload_invalid",
        "raw_payload_mismatch",
        "raw_payload_size_invalid",
        "raw_payload_unavailable",
        "raw_payload_unsafe",
        "source_rejection_codes_mismatch",
        "source_rejection_count_mismatch",
        "source_replay_failed",
        "source_replay_mismatch",
        "target_replay_failed",
        "target_storage_key_conflict",
        "unlineaged_current_relation",
        "unsupported_compression",
        "unsupported_source_parser",
        "unsuccessful_response",
        "wrong_source_dataset",
        "zero_rows",
    }
)
_EXCLUDED_REASON_CODES = _ISSUER_REASON_CODES | {
    "received_after_as_of",
    "unscoped_ingest",
}


@dataclass(frozen=True)
class CompanyFactsReplayPreview:
    """One deterministic in-memory review plan over existing local evidence."""

    plan_sha256: str
    _canonical_plan_json: str

    @property
    def plan(self) -> dict[str, Any]:
        return json.loads(self._canonical_plan_json)

    @property
    def eligible_issuers(self) -> int:
        return int(self.plan["summary"]["eligible_issuers"])

    @property
    def activation_available(self) -> bool:
        return bool(self.plan["activation_contract"]["available_in_this_build"])

    def to_plan_envelope(self) -> dict[str, Any]:
        return {
            "document_kind": COMPANYFACTS_REPLAY_PLAN_DOCUMENT_KIND,
            "schema_version": COMPANYFACTS_REPLAY_PLAN_SCHEMA_VERSION,
            "read_only": True,
            "payload_sha256": self.plan_sha256,
            "payload": self.plan,
        }

    def canonical_plan_artifact_json(self) -> str:
        return _canonical_json(self.to_plan_envelope())


def preview_companyfacts_v3_replay(
    project_root: Path,
    *,
    store: Store,
    as_of: date | str,
    issuer_ids: Sequence[str] | None = None,
) -> CompanyFactsReplayPreview:
    """Classify locally captured v2 evidence without writing or fetching.

    An issuer is eligible only when its newest Company Facts observation is an
    exact, accepted, issuer-scoped v2 response; the archived v2 replay matches
    its stored parse evidence; its reviewed CIK is unambiguous; and its entire
    current issuer relation is exactly the lineaged storage projection of that
    response.
    """

    root = _safe_project_root(project_root)
    decision_as_of = _normalize_as_of(as_of)
    requested = _normalize_issuer_ids(issuer_ids)
    ingest_columns = _table_columns(store, "ingest_log")
    subject_columns = {"subject_type", "subject_id"} & ingest_columns
    if subject_columns and subject_columns != {"subject_type", "subject_id"}:
        raise ValueError("Company Facts planner found an incomplete ingest subject schema")
    evidence_rows = store.query(
        _SOURCE_EVIDENCE_SQL.format(
            subject_type=("outcome.subject_type" if subject_columns else "NULL AS subject_type"),
            subject_id=("outcome.subject_id" if subject_columns else "NULL AS subject_id"),
            rejection_codes=(
                "outcome.rejection_codes"
                if "rejection_codes" in ingest_columns
                else "NULL AS rejection_codes"
            ),
        )
    )
    evidence = [_normalize_evidence_row(row) for row in evidence_rows]
    fundamental_columns = _table_columns(store, "fundamentals")
    if "issuer_id" not in fundamental_columns:
        raise ValueError("Company Facts planner requires issuer-scoped fundamentals")
    current_relation_sql = _CURRENT_ISSUER_RELATION_SQL.format(
        ingest_run_id=_optional_column_projection(
            fundamental_columns,
            "fundamental.ingest_run_id",
            "ingest_run_id",
        ),
        source_snapshot_id=_optional_column_projection(
            fundamental_columns,
            "fundamental.source_snapshot_id",
            "source_snapshot_id",
        ),
        source_rowset_sha256=_optional_column_projection(
            fundamental_columns,
            "fundamental.source_rowset_sha256",
            "source_rowset_sha256",
        ),
        source_row_sha256=_optional_column_projection(
            fundamental_columns,
            "fundamental.source_row_sha256",
            "source_row_sha256",
        ),
    )

    scoped: dict[str, list[dict[str, Any]]] = {}
    excluded: list[dict[str, Any]] = []
    for row in evidence:
        issuer_id = _scoped_issuer_id(row)
        if requested is not None and issuer_id is not None and issuer_id not in requested:
            continue
        try:
            received_on = _observation_date(row.get("received_at"))
        except ValueError:
            excluded.append(
                _excluded_evidence(
                    row,
                    additional_reasons=("invalid_observation_time",),
                )
            )
            continue
        if received_on > decision_as_of:
            excluded.append(
                _excluded_evidence(
                    row,
                    additional_reasons=("received_after_as_of",),
                )
            )
            continue
        if issuer_id is None:
            excluded.append(_excluded_evidence(row))
            continue
        scoped.setdefault(issuer_id, []).append(row)

    issuer_scope = sorted(requested if requested is not None else scoped)
    issuers: list[dict[str, Any]] = []
    for issuer_id in issuer_scope:
        observations = scoped.get(issuer_id, [])
        if not observations:
            issuers.append(
                {
                    "issuer_id": issuer_id,
                    "classification": "ineligible",
                    "reasons": ["no_scoped_source_evidence"],
                    "source": None,
                    "current_relation": None,
                    "v2_replay": None,
                    "v3_candidate": None,
                    "delta": None,
                }
            )
            continue
        newest = max(observations, key=_evidence_order_key)
        result = _classify_issuer(
            root,
            store=store,
            issuer_id=issuer_id,
            evidence=newest,
            current_relation_sql=current_relation_sql,
            decision_as_of=decision_as_of,
        )
        issuers.append(result)

    excluded.sort(key=_excluded_evidence_sort_key)
    eligible = sum(row["classification"] == "eligible" for row in issuers)
    plan: dict[str, Any] = {
        "plan_schema_version": COMPANYFACTS_REPLAY_PLAN_SCHEMA_VERSION,
        "operation": "review_companyfacts_v3_replay",
        "read_only": True,
        "network_access": False,
        "source_parser_version": COMPANYFACTS_PARSER_VERSION,
        "target_parser_version": COMPANYFACTS_NEXT_PARSER_VERSION,
        "policy_contract": COMPANYFACTS_REPLAY_POLICY_CONTRACT,
        "scope": {
            "as_of": decision_as_of,
            "requested_issuer_ids": (list(requested) if requested is not None else None),
        },
        "issuers": issuers,
        "excluded_evidence": excluded,
        "summary": {
            "eligible_issuers": eligible,
            "ineligible_issuers": len(issuers) - eligible,
            "excluded_source_observations": len(excluded),
        },
        "activation_contract": _planner_activation_contract(),
    }
    _validate_plan_payload(plan)
    canonical = _canonical_json(plan)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return CompanyFactsReplayPreview(digest, canonical)


def persist_companyfacts_v3_plan(
    project_root: Path,
    preview: CompanyFactsReplayPreview,
) -> Path:
    """Publish one content-addressed plan without touching governed live state."""

    if not isinstance(preview, CompanyFactsReplayPreview):
        raise TypeError("a Company Facts replay preview is required")
    root = _safe_project_root(project_root)
    plan = preview.plan
    _validate_plan_payload(plan)
    actual_sha256 = _payload_sha256(plan)
    if not _constant_time_equal(actual_sha256, preview.plan_sha256):
        raise ValueError("Company Facts replay preview checksum mismatch")
    if plan.get("activation_contract", {}).get("available_in_this_build") is not False:
        raise ValueError("Company Facts replay plan unexpectedly permits activation")

    destination = root / COMPANYFACTS_REPLAY_PLAN_REPORT_DIRECTORY / f"{preview.plan_sha256}.json"
    _require_plan_destination(root, destination)
    encoded = preview.canonical_plan_artifact_json() + "\n"

    if destination.exists() or destination.is_symlink():
        return _require_matching_plan(destination, encoded, plan)

    _reject_symlink_ancestors(root, destination)
    try:
        publish_text_write_once(destination, encoded)
    except FileExistsError:
        return _require_matching_plan(destination, encoded, plan)

    if read_companyfacts_v3_plan(destination) != plan:
        raise RuntimeError("published Company Facts replay plan failed verification")
    return destination


def scoped_source_evidence(
    store: Store, *, issuer_id: str, as_of: date | str
) -> dict[str, Any] | None:
    """Return the newest accepted, decision-scoped Company Facts evidence for one issuer.

    Mirrors the per-issuer scoping `preview_companyfacts_v3_replay` performs
    internally, but returns the full evidence row (including its raw-payload
    location) that the persisted review plan's public `source` projection
    deliberately omits. The governed v3 activation module needs this to
    re-verify and re-parse the exact payload at activation time rather than
    trusting anything cached in a plan file.
    """
    decision_as_of = _normalize_as_of(as_of)
    ingest_columns = _table_columns(store, "ingest_log")
    subject_columns = {"subject_type", "subject_id"} & ingest_columns
    if subject_columns and subject_columns != {"subject_type", "subject_id"}:
        raise ValueError("Company Facts planner found an incomplete ingest subject schema")
    evidence_rows = store.query(
        _SOURCE_EVIDENCE_SQL.format(
            subject_type=("outcome.subject_type" if subject_columns else "NULL AS subject_type"),
            subject_id=("outcome.subject_id" if subject_columns else "NULL AS subject_id"),
            rejection_codes=(
                "outcome.rejection_codes"
                if "rejection_codes" in ingest_columns
                else "NULL AS rejection_codes"
            ),
        )
    )
    observations = []
    for row in evidence_rows:
        normalized = _normalize_evidence_row(row)
        if _scoped_issuer_id(normalized) != issuer_id:
            continue
        try:
            received_on = _observation_date(normalized.get("received_at"))
        except ValueError:
            continue
        if received_on > decision_as_of:
            continue
        observations.append(normalized)
    if not observations:
        return None
    return max(observations, key=_evidence_order_key)


def verified_companyfacts_payload_bytes(root: Path, evidence: dict[str, Any]) -> bytes:
    """Verify and return one exact captured Company Facts payload.

    Public wrapper so the governed v3 activation module reuses the same
    hardened evidence verification the read-only planner uses, rather than a
    second implementation that could silently diverge from it.
    """
    return _verified_payload_bytes(root, evidence)


def read_companyfacts_v3_plan(path: Path) -> dict[str, Any]:
    """Read and verify one strict, planner-only plan envelope."""

    source = Path(path)
    _reject_any_symlink_ancestor(source.absolute())
    if source.is_symlink() or not source.is_file() or source.stat().st_nlink != 1:
        raise ValueError(f"Company Facts replay plan is missing or unsafe: {source}")
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Company Facts replay plan is unreadable: {source}") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "document_kind",
        "schema_version",
        "read_only",
        "payload_sha256",
        "payload",
    }:
        raise ValueError("unsupported Company Facts replay plan artifact")
    payload = raw.get("payload")
    if (
        raw.get("document_kind") != COMPANYFACTS_REPLAY_PLAN_DOCUMENT_KIND
        or raw.get("schema_version") != COMPANYFACTS_REPLAY_PLAN_SCHEMA_VERSION
        or raw.get("read_only") is not True
        or not _is_lower_sha256(raw.get("payload_sha256"))
        or not isinstance(payload, dict)
        or payload.get("plan_schema_version") != COMPANYFACTS_REPLAY_PLAN_SCHEMA_VERSION
        or payload.get("operation") != "review_companyfacts_v3_replay"
        or payload.get("read_only") is not True
        or payload.get("network_access") is not False
    ):
        raise ValueError("unsupported Company Facts replay plan artifact")
    if not _constant_time_equal(str(raw["payload_sha256"]), _payload_sha256(payload)):
        raise ValueError("Company Facts replay plan checksum mismatch")
    _validate_plan_payload(payload)
    return dict(payload)


def _classify_issuer(
    root: Path,
    *,
    store: Store,
    issuer_id: str,
    evidence: dict[str, Any],
    current_relation_sql: str,
    decision_as_of: str,
) -> dict[str, Any]:
    reasons = _source_gate_reasons(evidence)
    source = _public_source_evidence(evidence)
    result: dict[str, Any] = {
        "issuer_id": issuer_id,
        "classification": "ineligible",
        "reasons": reasons,
        "source": source,
        "current_relation": None,
        "v2_replay": None,
        "v3_candidate": None,
        "delta": None,
    }
    if reasons:
        return result

    try:
        payload = _verified_payload_bytes(root, evidence)
    except _EvidenceFailure as exc:
        result["reasons"] = [exc.reason]
        return result

    try:
        v2_rows, v2_metadata = replay_sec_companyfacts_response(
            payload,
            parser_version=COMPANYFACTS_PARSER_VERSION,
        )
        v2_hash = canonical_parsed_rows_sha256(v2_rows)
    except (KeyError, TypeError, ValueError):
        result["reasons"] = ["source_replay_failed"]
        return result
    if (
        len(v2_rows) != int(evidence["parsed_row_count"])
        or v2_hash != evidence["parsed_rows_sha256"]
    ):
        result["reasons"] = ["source_replay_mismatch"]
        return result
    replay_rows_rejected = int(v2_metadata["rows_rejected"])
    if (
        isinstance(evidence.get("rows_rejected"), bool)
        or not isinstance(evidence.get("rows_rejected"), int)
        or int(evidence["rows_rejected"]) != replay_rows_rejected
    ):
        result["reasons"] = ["source_rejection_count_mismatch"]
        return result
    expected_rejection_codes = tuple(v2_metadata["rejection_codes"])
    try:
        recorded_rejection_codes = decode_rejection_codes(evidence.get("rejection_codes"))
    except (TypeError, ValueError):
        result["reasons"] = ["source_rejection_codes_mismatch"]
        return result
    legacy_future_warning = (
        expected_rejection_codes == ("future_period",)
        and recorded_rejection_codes is None
        and evidence.get("status") == "warning"
        and accepted_sec_fundamental_outcome(
            status=evidence.get("status"),
            error=evidence.get("error"),
            rejection_codes=None,
        )
    )
    if (
        expected_rejection_codes
        and recorded_rejection_codes != expected_rejection_codes
        and not legacy_future_warning
    ) or (not expected_rejection_codes and recorded_rejection_codes is not None):
        result["reasons"] = ["source_rejection_codes_mismatch"]
        return result

    try:
        reference = store.issuer_reference(issuer_id, as_of=decision_as_of)
    except ValueError:
        result["reasons"] = ["ambiguous_identity"]
        return result
    if not isinstance(reference, dict):
        result["reasons"] = ["missing_reviewed_identity"]
        return result
    try:
        reviewed_cik = int(reference["cik"])
        payload_cik = int(v2_rows[0]["cik"])
        canonical_ticker = str(reference["canonical_ticker"]).strip().upper()
    except (KeyError, TypeError, ValueError):
        result["reasons"] = ["ambiguous_identity"]
        return result
    if not canonical_ticker or reviewed_cik != payload_cik:
        result["reasons"] = ["identity_cik_mismatch"]
        return result

    expected_rows = _storage_projection(v2_rows)
    expected_relation = _canonical_provider_relation(expected_rows)
    expected_relation_hash = canonical_parsed_rows_sha256(expected_relation)
    result["v2_replay"] = {
        "provider_row_count": len(v2_rows),
        "provider_rows_sha256": v2_hash,
        "storage_row_count": len(expected_rows),
        "storage_rows_sha256": expected_relation_hash,
    }

    live_rows = store.query(
        current_relation_sql,
        (issuer_id,),
    )
    current_relation = _canonical_live_relation(live_rows, cik=reviewed_cik)
    current_hash = canonical_parsed_rows_sha256(current_relation)
    result["current_relation"] = {
        "row_count": len(live_rows),
        "rows_sha256": current_hash,
    }

    lineage_ok = _current_lineage_matches(
        live_rows,
        expected_rows=expected_rows,
        evidence=evidence,
    )
    relation_ok = (
        len(live_rows) == len(expected_rows)
        and current_hash == expected_relation_hash
        and all(
            str(row.get("ticker") or "").strip().upper() == canonical_ticker for row in live_rows
        )
    )
    if not lineage_ok:
        failure_reasons = ["unlineaged_current_relation"]
        if not relation_ok:
            failure_reasons.append("current_relation_mismatch")
        result["reasons"] = sorted(failure_reasons)
        return result
    if not relation_ok or int(evidence["rows_inserted"]) != len(expected_rows):
        result["reasons"] = ["current_relation_mismatch"]
        return result

    try:
        v3_rows = parse_sec_companyfacts_response_v3(payload)
        v3_hash = canonical_parsed_rows_sha256(v3_rows)
    except (KeyError, TypeError, ValueError):
        result["reasons"] = ["target_replay_failed"]
        return result
    v3_keys = [_storage_key(row) for row in v3_rows]
    if len(v3_keys) != len(set(v3_keys)):
        result["reasons"] = ["target_storage_key_conflict"]
        return result

    v3_relation = _canonical_provider_relation(v3_rows)
    result["v3_candidate"] = {
        "provider_row_count": len(v3_rows),
        "provider_rows_sha256": v3_hash,
        "storage_row_count": len(v3_relation),
        "storage_rows_sha256": canonical_parsed_rows_sha256(v3_relation),
    }
    result["delta"] = _relation_delta(expected_relation, v3_relation)
    result["classification"] = "eligible"
    result["reasons"] = []
    return result


def _source_gate_reasons(evidence: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if evidence.get("provider") != "sec-edgar" or evidence.get("dataset") != "companyfacts":
        reasons.append("wrong_source_dataset")
    parser = evidence.get("parser_version")
    if parser != COMPANYFACTS_PARSER_VERSION:
        reasons.append(
            "capture_only"
            if parser in CAPTURE_ONLY_PARSER_VERSIONS
            else "unsupported_source_parser"
        )
    if evidence.get("artifact_kind") != "exact_response":
        reasons.append("not_exact_response")
    rows_inserted = evidence.get("rows_inserted")
    rows_rejected = evidence.get("rows_rejected")
    accepted_outcome = accepted_sec_fundamental_outcome(
        status=evidence.get("status"),
        error=evidence.get("error"),
        rejection_codes=evidence.get("rejection_codes"),
    )
    if evidence.get("status") == "success":
        accepted_outcome = accepted_outcome and rows_rejected == 0
    elif evidence.get("status") == "warning":
        accepted_outcome = (
            accepted_outcome
            and isinstance(rows_rejected, int)
            and not isinstance(rows_rejected, bool)
            and rows_rejected > 0
        )
    if not accepted_outcome:
        reasons.append("failed_ingest")
    count = evidence.get("parsed_row_count")
    digest = evidence.get("parsed_rows_sha256")
    if (
        count is None
        or digest is None
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
    ):
        reasons.append("incomplete_parse_evidence")
    elif count == 0:
        reasons.append("zero_rows")
    if not _is_lower_sha256(digest) and "incomplete_parse_evidence" not in reasons:
        reasons.append("incomplete_parse_evidence")
    http_status = evidence.get("http_status")
    if not isinstance(http_status, int) or not 200 <= http_status <= 299:
        reasons.append("unsuccessful_response")
    rows_inserted = evidence.get("rows_inserted")
    if (
        not evidence.get("run_id")
        or evidence.get("role") != "companyfacts"
        or evidence.get("ingest_id") is None
        or not isinstance(rows_inserted, int)
        or isinstance(rows_inserted, bool)
        or rows_inserted < 0
        or not isinstance(rows_rejected, int)
        or isinstance(rows_rejected, bool)
        or rows_rejected < 0
    ):
        reasons.append("incomplete_ingest_evidence")
    if evidence.get("compression") != "gzip":
        reasons.append("unsupported_compression")
    return sorted(set(reasons))


def _excluded_evidence(
    evidence: dict[str, Any],
    *,
    additional_reasons: tuple[str, ...] = (),
) -> dict[str, Any]:
    reasons = _source_gate_reasons(evidence)
    if _scoped_issuer_id(evidence) is None and "unscoped_ingest" not in reasons:
        reasons.append("unscoped_ingest")
    reasons.extend(additional_reasons)
    return {
        **_public_source_evidence(evidence),
        "classification": "excluded",
        "reasons": sorted(set(reasons)),
    }


def _scoped_issuer_id(evidence: dict[str, Any]) -> str | None:
    if evidence.get("subject_type") != "issuer":
        return None
    issuer_id = str(evidence.get("subject_id") or "").strip()
    return issuer_id or None


def _verified_payload_bytes(root: Path, evidence: dict[str, Any]) -> bytes:
    relative = Path(str(evidence.get("relative_path") or ""))
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.parts[:2] != ("data", "raw")
        or ".." in relative.parts
    ):
        raise _EvidenceFailure("raw_payload_unsafe")
    stored_bytes = _bounded_snapshot_size(
        evidence.get("stored_bytes"),
        maximum=MAX_COMPANYFACTS_SNAPSHOT_STORED_BYTES,
    )
    original_bytes = _bounded_snapshot_size(
        evidence.get("original_bytes"),
        maximum=MAX_COMPANYFACTS_SNAPSHOT_ORIGINAL_BYTES,
    )
    raw_root = (root / "data" / "raw").resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(raw_root):
        raise _EvidenceFailure("raw_payload_unsafe")
    _reject_symlink_ancestors(root, root / relative)
    if target.is_symlink() or not target.is_file() or target.stat().st_nlink != 1:
        raise _EvidenceFailure("raw_payload_unavailable")
    if target.stat().st_size != stored_bytes:
        raise _EvidenceFailure("raw_payload_mismatch")
    try:
        with target.open("rb") as handle:
            compressed = handle.read(stored_bytes + 1)
    except OSError:
        raise _EvidenceFailure("raw_payload_unavailable") from None
    if len(compressed) != stored_bytes:
        raise _EvidenceFailure("raw_payload_mismatch")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as archive:
            payload = archive.read(original_bytes + 1)
            trailing_output = archive.read(1)
    except (EOFError, OSError, zlib.error):
        raise _EvidenceFailure("raw_payload_invalid") from None
    if (
        len(payload) != original_bytes
        or trailing_output
        or hashlib.sha256(payload).hexdigest() != evidence.get("payload_sha256")
    ):
        raise _EvidenceFailure("raw_payload_mismatch")
    return payload


def _bounded_snapshot_size(value: Any, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > maximum:
        raise _EvidenceFailure("raw_payload_size_invalid")
    return value


def _storage_projection(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mirror DuckDB v2 batch upsert semantics: the first row owns a key."""

    projected: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        projected.setdefault(_storage_key(row), dict(row))
    return [projected[key] for key in sorted(projected)]


def _canonical_provider_relation(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [_provider_economic_row(row) for row in rows],
        key=lambda row: (
            row["period_end"],
            row["as_of_date"],
            row["metric"],
        ),
    )


def _canonical_live_relation(
    rows: list[dict[str, Any]],
    *,
    cik: int,
) -> list[dict[str, Any]]:
    canonical = []
    for row in rows:
        canonical.append(
            {
                "cik": f"{cik:010d}",
                "period_end": _date_text(row.get("period_end")),
                "as_of_date": _date_text(row.get("as_of_date")),
                "fiscal_period": row.get("fiscal_period"),
                "statement": row.get("statement"),
                "metric": row.get("metric"),
                "value": row.get("value"),
                "quarter_value": row.get("quarter_value"),
                "unit": row.get("unit") or "USD",
                "source": row.get("source") or "edgar",
            }
        )
    return sorted(
        canonical,
        key=lambda row: (
            row["period_end"],
            row["as_of_date"],
            str(row["metric"]),
        ),
    )


def _provider_economic_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "cik": f"{int(row['cik']):010d}",
        "period_end": _date_text(row.get("period_end")),
        "as_of_date": _date_text(row.get("as_of_date")),
        "fiscal_period": row.get("fiscal_period"),
        "statement": row.get("statement"),
        "metric": row.get("metric"),
        "value": row.get("value"),
        "quarter_value": row.get("quarter_value"),
        "unit": row.get("unit") or "USD",
        "source": row.get("source") or "edgar",
    }


def _current_lineage_matches(
    live_rows: list[dict[str, Any]],
    *,
    expected_rows: list[dict[str, Any]],
    evidence: dict[str, Any],
) -> bool:
    expected = {_storage_key(row): row for row in expected_rows}
    for row in live_rows:
        try:
            key = (
                _date_text(row["period_end"]),
                _date_text(row["as_of_date"]),
                str(row["metric"]),
            )
        except (KeyError, TypeError, ValueError):
            return False
        source = expected.get(key)
        if source is None:
            return False
        if (
            row.get("ingest_run_id") != evidence["run_id"]
            or row.get("source_snapshot_id") != evidence["snapshot_id"]
            or row.get("source_rowset_sha256") != evidence["parsed_rows_sha256"]
            or row.get("source_row_sha256") != canonical_sec_fundamental_row_sha256(source)
        ):
            return False
    return len(live_rows) == len(expected_rows)


def _relation_delta(
    source: list[dict[str, Any]],
    target: list[dict[str, Any]],
) -> dict[str, int]:
    left = {_storage_key(row): row for row in source}
    right = {_storage_key(row): row for row in target}
    shared = left.keys() & right.keys()
    changed = sum(left[key] != right[key] for key in shared)
    return {
        "added_storage_keys": len(right.keys() - left.keys()),
        "removed_storage_keys": len(left.keys() - right.keys()),
        "changed_storage_keys": changed,
        "unchanged_storage_keys": len(shared) - changed,
    }


def _storage_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _date_text(row.get("period_end")),
        _date_text(row.get("as_of_date")),
        str(row.get("metric") or ""),
    )


def _normalize_evidence_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in _EVIDENCE_FIELDS}


def _table_columns(store: Store, table_name: str) -> set[str]:
    return {
        str(row["column_name"])
        for row in store.query(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'main' AND table_name = ?
            """,
            (table_name,),
        )
    }


def _optional_column_projection(
    columns: set[str],
    qualified_name: str,
    alias: str,
) -> str:
    return qualified_name if alias in columns else f"NULL AS {alias}"


def _public_source_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_id": evidence.get("snapshot_id"),
        "provider": evidence.get("provider"),
        "dataset": evidence.get("dataset"),
        "artifact_kind": evidence.get("artifact_kind"),
        "http_status": evidence.get("http_status"),
        "payload_sha256": evidence.get("payload_sha256"),
        "parser_version": evidence.get("parser_version"),
        "parsed_row_count": evidence.get("parsed_row_count"),
        "parsed_rows_sha256": evidence.get("parsed_rows_sha256"),
        "run_id": evidence.get("run_id"),
        "ingest_id": evidence.get("ingest_id"),
        "rows_inserted": evidence.get("rows_inserted"),
        "rows_rejected": evidence.get("rows_rejected"),
        "rejection_codes": evidence.get("rejection_codes"),
        "status": evidence.get("status"),
        "error": evidence.get("error"),
        "subject_type": evidence.get("subject_type"),
        "subject_id": evidence.get("subject_id"),
        "received_at": _timestamp_text(evidence.get("received_at")),
    }


def _evidence_order_key(row: dict[str, Any]) -> tuple[str, int, str, str]:
    ingest_id = row.get("ingest_id")
    return (
        _timestamp_text(row.get("received_at")),
        int(ingest_id) if isinstance(ingest_id, int) else -1,
        str(row.get("snapshot_id") or ""),
        str(row.get("run_id") or ""),
    )


def _excluded_evidence_identity(
    row: dict[str, Any],
) -> tuple[str, str, str]:
    return (
        str(row.get("snapshot_id") or ""),
        str(row.get("run_id") or ""),
        _canonical_json(row.get("ingest_id")),
    )


def _excluded_evidence_sort_key(
    row: dict[str, Any],
) -> tuple[str, str, str, str]:
    return (*_excluded_evidence_identity(row), _canonical_json(row))


def _planner_activation_contract() -> dict[str, Any]:
    return {
        "available_in_this_build": False,
        "reason": "planner_only_milestone",
        "explicit_confirmation_required_in_future_build": True,
        "verified_backup_required_in_future_build": True,
        "compare_and_set_required_in_future_build": True,
        "forbidden_actions": list(_ACTIVATION_FORBIDDEN_ACTIONS),
    }


def _validate_plan_payload(payload: Any) -> None:
    plan = _exact_mapping(
        payload,
        {
            "plan_schema_version",
            "operation",
            "read_only",
            "network_access",
            "source_parser_version",
            "target_parser_version",
            "policy_contract",
            "scope",
            "issuers",
            "excluded_evidence",
            "summary",
            "activation_contract",
        },
        label="payload",
    )
    if (
        plan["plan_schema_version"] != COMPANYFACTS_REPLAY_PLAN_SCHEMA_VERSION
        or plan["operation"] != "review_companyfacts_v3_replay"
        or plan["read_only"] is not True
        or plan["network_access"] is not False
        or plan["source_parser_version"] != COMPANYFACTS_PARSER_VERSION
        or plan["target_parser_version"] != COMPANYFACTS_NEXT_PARSER_VERSION
        or plan["policy_contract"] != COMPANYFACTS_REPLAY_POLICY_CONTRACT
    ):
        raise ValueError("Company Facts replay plan policy contract is invalid")

    scope = _exact_mapping(
        plan["scope"],
        {"as_of", "requested_issuer_ids"},
        label="scope",
    )
    decision_as_of = _normalize_as_of(scope["as_of"])
    if scope["as_of"] != decision_as_of:
        raise ValueError("Company Facts replay plan scope date is noncanonical")
    requested = scope["requested_issuer_ids"]
    if requested is not None and (
        not isinstance(requested, list)
        or not requested
        or any(not isinstance(value, str) or not value.strip() for value in requested)
        or requested != sorted(set(requested))
    ):
        raise ValueError("Company Facts replay plan issuer scope is invalid")

    issuers = plan["issuers"]
    if not isinstance(issuers, list):
        raise ValueError("Company Facts replay plan issuers must be a list")
    issuer_ids: list[str] = []
    eligible_count = 0
    for value in issuers:
        issuer_id, eligible = _validate_plan_issuer(value, as_of=decision_as_of)
        issuer_ids.append(issuer_id)
        eligible_count += int(eligible)
    if issuer_ids != sorted(set(issuer_ids)):
        raise ValueError("Company Facts replay plan issuers are not sorted and unique")
    if requested is not None and issuer_ids != requested:
        raise ValueError("Company Facts replay plan does not cover its requested scope")

    excluded = plan["excluded_evidence"]
    if not isinstance(excluded, list):
        raise ValueError("Company Facts replay excluded evidence must be a list")
    excluded_identities: list[tuple[str, str, str]] = []
    excluded_order: list[tuple[str, str, str, str]] = []
    for value in excluded:
        identity = _validate_excluded_evidence(value, as_of=decision_as_of)
        excluded_identities.append(identity)
        excluded_order.append(_excluded_evidence_sort_key(value))
    if len(excluded_identities) != len(set(excluded_identities)):
        raise ValueError("Company Facts replay excluded evidence identity is duplicated")
    if excluded_order != sorted(excluded_order):
        raise ValueError("Company Facts replay excluded evidence is not canonically sorted")

    summary = _exact_mapping(
        plan["summary"],
        {
            "eligible_issuers",
            "ineligible_issuers",
            "excluded_source_observations",
        },
        label="summary",
    )
    if summary != {
        "eligible_issuers": eligible_count,
        "ineligible_issuers": len(issuers) - eligible_count,
        "excluded_source_observations": len(excluded),
    }:
        raise ValueError("Company Facts replay plan summary is inconsistent")
    if plan["activation_contract"] != _planner_activation_contract():
        raise ValueError("Company Facts replay plan activation contract is invalid")


def _validate_plan_issuer(value: Any, *, as_of: str) -> tuple[str, bool]:
    row = _exact_mapping(
        value,
        {
            "issuer_id",
            "classification",
            "reasons",
            "source",
            "current_relation",
            "v2_replay",
            "v3_candidate",
            "delta",
        },
        label="issuer",
    )
    issuer_id = row["issuer_id"]
    if not isinstance(issuer_id, str) or not issuer_id.strip() or issuer_id != issuer_id.strip():
        raise ValueError("Company Facts replay plan issuer identity is invalid")
    reasons = _validate_reason_list(
        row["reasons"],
        allowed=_ISSUER_REASON_CODES,
        label="issuer",
    )
    source = row["source"]
    if source is not None:
        source = _validate_source_plan_evidence(source)
        try:
            if _observation_date(source["received_at"]) > as_of:
                raise ValueError("Company Facts replay issuer source is after the decision date")
        except ValueError as exc:
            raise ValueError("Company Facts replay issuer source date is invalid") from exc
    current = _validate_current_relation(row["current_relation"])
    v2 = _validate_replay_summary(row["v2_replay"], label="v2 replay")
    v3 = _validate_replay_summary(row["v3_candidate"], label="v3 candidate")
    delta = _validate_delta(row["delta"])

    classification = row["classification"]
    if classification == "eligible":
        if (
            reasons
            or source is None
            or current is None
            or v2 is None
            or v3 is None
            or delta is None
        ):
            raise ValueError("eligible Company Facts replay issuer is incomplete")
        if (
            source["subject_type"] != "issuer"
            or source["subject_id"] != issuer_id
            or source["provider"] != "sec-edgar"
            or source["dataset"] != "companyfacts"
            or source["artifact_kind"] != "exact_response"
            or source["parser_version"] != COMPANYFACTS_PARSER_VERSION
            or not isinstance(source["http_status"], int)
            or not 200 <= source["http_status"] <= 299
            or not _nonnegative_int(source["parsed_row_count"])
            or source["parsed_row_count"] < 1
            or not _nonnegative_int(source["rows_inserted"])
            or source["parsed_row_count"] != v2["provider_row_count"]
            or source["parsed_rows_sha256"] != v2["provider_rows_sha256"]
            or source["rows_inserted"] != v2["storage_row_count"]
            or current["row_count"] != v2["storage_row_count"]
            or current["rows_sha256"] != v2["storage_rows_sha256"]
        ):
            raise ValueError("eligible Company Facts replay source binding is invalid")
        if not _accepted_plan_source_outcome(source):
            raise ValueError("eligible Company Facts replay ingest outcome is invalid")
        _validate_delta_counts(delta, source=v2, target=v3)
        return issuer_id, True
    if classification != "ineligible" or not reasons:
        raise ValueError("Company Facts replay issuer classification is invalid")
    if current is not None and v2 is not None and current["row_count"] < 0:
        raise ValueError("Company Facts replay current relation is invalid")
    if delta is not None:
        if v2 is None or v3 is None:
            raise ValueError("Company Facts replay delta lacks replay summaries")
        _validate_delta_counts(delta, source=v2, target=v3)
    return issuer_id, False


def _validate_excluded_evidence(
    value: Any,
    *,
    as_of: str,
) -> tuple[str, str, str]:
    row = _exact_mapping(
        value,
        _source_plan_fields() | {"classification", "reasons"},
        label="excluded evidence",
    )
    if row["classification"] != "excluded":
        raise ValueError("Company Facts replay excluded classification is invalid")
    reasons = _validate_reason_list(
        row["reasons"],
        allowed=_EXCLUDED_REASON_CODES,
        label="excluded evidence",
    )
    source = _validate_source_plan_evidence({field: row[field] for field in _source_plan_fields()})
    if not reasons:
        raise ValueError("Company Facts replay excluded evidence has no reason")
    if "received_after_as_of" in reasons:
        try:
            if _observation_date(source["received_at"]) <= as_of:
                raise ValueError("Company Facts replay future evidence is not after scope")
        except ValueError as exc:
            raise ValueError("Company Facts replay future evidence date is invalid") from exc
    if "unscoped_ingest" in reasons and (
        source["subject_type"] == "issuer"
        and isinstance(source["subject_id"], str)
        and source["subject_id"].strip()
    ):
        raise ValueError("Company Facts replay unscoped evidence has an issuer")
    return _excluded_evidence_identity(source)


def _validate_source_plan_evidence(value: Any) -> dict[str, Any]:
    row = _exact_mapping(value, _source_plan_fields(), label="source evidence")
    if (
        not isinstance(row["snapshot_id"], str)
        or not row["snapshot_id"]
        or row["provider"] != "sec-edgar"
        or row["dataset"] != "companyfacts"
        or not _is_lower_sha256(row["payload_sha256"])
        or not isinstance(row["parser_version"], str)
        or not row["parser_version"]
    ):
        raise ValueError("Company Facts replay source evidence is invalid")
    return row


def _validate_current_relation(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    row = _exact_mapping(value, {"row_count", "rows_sha256"}, label="current relation")
    if not _nonnegative_int(row["row_count"]) or not _is_lower_sha256(row["rows_sha256"]):
        raise ValueError("Company Facts replay current relation is invalid")
    return row


def _validate_replay_summary(
    value: Any,
    *,
    label: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    row = _exact_mapping(
        value,
        {
            "provider_row_count",
            "provider_rows_sha256",
            "storage_row_count",
            "storage_rows_sha256",
        },
        label=label,
    )
    if (
        not _nonnegative_int(row["provider_row_count"])
        or not _nonnegative_int(row["storage_row_count"])
        or not _is_lower_sha256(row["provider_rows_sha256"])
        or not _is_lower_sha256(row["storage_rows_sha256"])
    ):
        raise ValueError(f"Company Facts replay {label} is invalid")
    return row


def _validate_delta(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    row = _exact_mapping(
        value,
        {
            "added_storage_keys",
            "removed_storage_keys",
            "changed_storage_keys",
            "unchanged_storage_keys",
        },
        label="delta",
    )
    if any(not _nonnegative_int(count) for count in row.values()):
        raise ValueError("Company Facts replay delta is invalid")
    return row


def _validate_delta_counts(
    delta: dict[str, Any],
    *,
    source: dict[str, Any],
    target: dict[str, Any],
) -> None:
    if (
        delta["removed_storage_keys"]
        + delta["changed_storage_keys"]
        + delta["unchanged_storage_keys"]
        != source["storage_row_count"]
        or delta["added_storage_keys"]
        + delta["changed_storage_keys"]
        + delta["unchanged_storage_keys"]
        != target["storage_row_count"]
    ):
        raise ValueError("Company Facts replay delta counts are inconsistent")


def _accepted_plan_source_outcome(source: dict[str, Any]) -> bool:
    if not accepted_sec_fundamental_outcome(
        status=source["status"],
        error=source["error"],
        rejection_codes=source["rejection_codes"],
    ):
        return False
    rejected = source["rows_rejected"]
    if not _nonnegative_int(rejected):
        return False
    if source["status"] == "success":
        return rejected == 0
    return source["status"] == "warning" and rejected > 0


def _validate_reason_list(
    value: Any,
    *,
    allowed: frozenset[str],
    label: str,
) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(reason, str) for reason in value)
        or value != sorted(set(value))
        or not set(value) <= allowed
    ):
        raise ValueError(f"Company Facts replay {label} reasons are invalid")
    return value


def _exact_mapping(
    value: Any,
    fields: set[str] | frozenset[str],
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f"Company Facts replay plan {label} schema is invalid")
    return value


def _source_plan_fields() -> set[str]:
    return {
        "snapshot_id",
        "provider",
        "dataset",
        "artifact_kind",
        "http_status",
        "payload_sha256",
        "parser_version",
        "parsed_row_count",
        "parsed_rows_sha256",
        "run_id",
        "ingest_id",
        "rows_inserted",
        "rows_rejected",
        "rejection_codes",
        "status",
        "error",
        "subject_type",
        "subject_id",
        "received_at",
    }


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _normalize_as_of(value: date | str) -> str:
    if isinstance(value, datetime):
        raise TypeError("Company Facts replay as_of must be a date, not a datetime")
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise TypeError("Company Facts replay as_of must be a date or ISO date string")
    normalized = value.strip()
    try:
        parsed = date.fromisoformat(normalized)
    except ValueError:
        raise ValueError("Company Facts replay as_of must be an ISO date") from None
    if normalized != parsed.isoformat():
        raise ValueError("Company Facts replay as_of must be a canonical ISO date")
    return normalized


def _normalize_issuer_ids(
    issuer_ids: Sequence[str] | None,
) -> tuple[str, ...] | None:
    if issuer_ids is None:
        return None
    normalized = tuple(
        sorted({str(issuer_id).strip() for issuer_id in issuer_ids if str(issuer_id).strip()})
    )
    if not normalized:
        raise ValueError("issuer scope cannot be empty")
    return normalized


def _safe_project_root(project_root: Path) -> Path:
    requested = Path(project_root)
    _reject_any_symlink_ancestor(requested.absolute())
    root = requested.resolve()
    if not root.is_dir():
        raise ValueError(f"project root must be an existing directory: {requested}")
    return root


def _require_plan_destination(root: Path, destination: Path) -> None:
    allowed = (root / COMPANYFACTS_REPLAY_PLAN_REPORT_DIRECTORY).resolve()
    parent = destination.parent.resolve()
    if parent != allowed or not destination.resolve().is_relative_to(root):
        raise ValueError("Company Facts replay plan path escapes its report namespace")
    if destination.name != f"{destination.stem}.json" or not _is_lower_sha256(destination.stem):
        raise ValueError("Company Facts replay plan destination is invalid")
    _reject_symlink_ancestors(root, destination)


def _require_matching_plan(
    destination: Path,
    expected_text: str,
    expected_plan: dict[str, Any],
) -> Path:
    _reject_any_symlink_ancestor(destination.absolute())
    if destination.is_symlink() or not destination.is_file() or destination.stat().st_nlink != 1:
        raise ValueError(f"Company Facts replay plan target is unsafe: {destination}")
    try:
        current = destination.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Company Facts replay plan target is unreadable: {destination}") from exc
    if current != expected_text:
        raise ValueError(f"Company Facts replay plan artifact collision: {destination}")
    if read_companyfacts_v3_plan(destination) != expected_plan:
        raise ValueError("existing Company Facts replay plan failed verification")
    return destination


def _reject_symlink_ancestors(root: Path, path: Path) -> None:
    candidate = Path(path)
    while True:
        if candidate.is_symlink():
            raise ValueError(f"path cannot contain symbolic links: {path}")
        if candidate == root:
            return
        if candidate.parent == candidate or not candidate.is_relative_to(root):
            raise ValueError(f"path escapes the project root: {path}")
        candidate = candidate.parent


def _reject_any_symlink_ancestor(path: Path) -> None:
    candidate = Path(path)
    while True:
        if candidate.is_symlink():
            raise ValueError(f"path cannot contain symbolic links: {path}")
        if candidate.parent == candidate:
            return
        candidate = candidate.parent


def _observation_date(value: Any) -> str:
    text = _timestamp_text(value)
    if len(text) < 10:
        raise ValueError("source observation has no valid date")
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        raise ValueError("source observation has no valid date") from None


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "")
    return date.fromisoformat(text[:10]).isoformat()


def _timestamp_text(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value or "")


def _payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


_canonical_json = canonical_json


def _is_lower_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _LOWER_HEX for character in value)
    )


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


class _EvidenceFailure(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_EVIDENCE_FIELDS = (
    "snapshot_id",
    "provider",
    "dataset",
    "artifact_kind",
    "received_at",
    "http_status",
    "payload_sha256",
    "adapter_name",
    "adapter_version",
    "parser_version",
    "parsed_row_count",
    "parsed_rows_sha256",
    "relative_path",
    "original_bytes",
    "stored_bytes",
    "compression",
    "run_id",
    "role",
    "ingest_id",
    "source",
    "table_name",
    "subject_type",
    "subject_id",
    "rows_inserted",
    "rows_rejected",
    "rejection_codes",
    "status",
    "error",
    "finished_at",
)

_SOURCE_EVIDENCE_SQL = """
SELECT snapshot.snapshot_id, snapshot.provider, snapshot.dataset,
       snapshot.artifact_kind, snapshot.received_at, snapshot.http_status,
       snapshot.payload_sha256, snapshot.adapter_name, snapshot.adapter_version,
       snapshot.parser_version, snapshot.parsed_row_count,
       snapshot.parsed_rows_sha256, payload.relative_path,
       payload.original_bytes, payload.stored_bytes, payload.compression,
       linked.run_id, linked.role, outcome.id AS ingest_id,
       outcome.source, outcome.table_name, {subject_type},
       {subject_id}, outcome.rows_inserted, outcome.rows_rejected,
       {rejection_codes}, outcome.status, outcome.error, outcome.finished_at
FROM raw_snapshots AS snapshot
JOIN raw_payloads AS payload USING (payload_sha256)
LEFT JOIN ingest_raw_snapshots AS linked
  ON linked.snapshot_id = snapshot.snapshot_id
 AND linked.role = 'companyfacts'
LEFT JOIN ingest_log AS outcome
  ON outcome.run_id = linked.run_id
 AND outcome.table_name = 'fundamentals'
WHERE snapshot.provider = 'sec-edgar'
  AND snapshot.dataset = 'companyfacts'
ORDER BY snapshot.received_at, snapshot.snapshot_id,
         linked.run_id, outcome.id
"""

_CURRENT_ISSUER_RELATION_SQL = """
SELECT fundamental.ticker, fundamental.issuer_id, fundamental.security_id,
       {ingest_run_id}, {source_snapshot_id},
       {source_rowset_sha256}, {source_row_sha256},
       fundamental.period_end, fundamental.as_of_date,
       fundamental.fiscal_period, fundamental.statement,
       fundamental.metric, fundamental.value, fundamental.quarter_value,
       fundamental.unit, fundamental.source
FROM fundamentals AS fundamental
WHERE fundamental.issuer_id = ?
ORDER BY fundamental.period_end, fundamental.as_of_date,
         fundamental.metric, fundamental.ticker
"""

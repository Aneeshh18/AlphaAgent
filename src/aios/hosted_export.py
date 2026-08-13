"""Isolated contract for an unsigned, tenant-neutral hosted research snapshot.

This module has no CLI, network, paper, operations, or mutation dependency.  It
accepts already-reviewed factor rows plus either explicit point-in-time universe
membership or a Store-like object that is provably open read-only.  The output
is a bounded, allowlisted local artifact. Python's JSON number serialization is
not an interoperable canonicalization standard, so every artifact carries an
explicit hosted-promotion blocker. Signing and publication are deliberately
outside this contract.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from aios.canonical import canonical_json, canonical_sha256

HOSTED_RESEARCH_DOCUMENT_KIND = "aios.hosted-research-snapshot"
HOSTED_RESEARCH_SCHEMA_VERSION = "hosted-research-snapshot.v1"
HOSTED_RESEARCH_SNAPSHOT_PREFIX = "hrs-v1-"
HOSTED_RESEARCH_CANONICALIZATION = "python-json-v1-local-only"
HOSTED_RESEARCH_PROMOTION_BLOCKER = "interoperable-canonicalization-and-signature-required"
HOSTED_READINESS_DOCUMENT_KIND = "aios.us-readiness-report"
HOSTED_READINESS_SCHEMA_VERSION = "us-readiness-report.v1"

MAX_HOSTED_SECURITIES = 600
MAX_MISSING_INPUTS_PER_SECURITY = 32
MAX_READINESS_BYTES = 32 * 1024
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_SERVING_TTL = timedelta(days=7)

_DOCUMENT_FIELDS = frozenset(
    {
        "document_kind",
        "schema_version",
        "unsigned",
        "canonicalization",
        "hosted_promotion_allowed",
        "promotion_blocker",
        "snapshot_id",
        "payload_sha256",
        "payload",
    }
)
_PAYLOAD_FIELDS = frozenset(
    {
        "certified_decision_close",
        "readiness_sha256",
        "universe_id",
        "universe_membership_sha256",
        "factor_policy_sha256",
        "source_policy_sha256",
        "created_at",
        "serving_expires_at",
        "coverage",
        "securities",
    }
)
_COVERAGE_FIELDS = frozenset({"total", "scored", "withheld"})
_MEMBERSHIP_INPUT_FIELDS = frozenset({"security_id", "ticker"})
_US_READINESS_REPORT_FIELDS = frozenset(
    {
        "as_of",
        "purpose",
        "generated_on",
        "universe_id",
        "benchmark_ticker",
        "certified_research_from",
        "certified_research_through",
        "raw_prices_through",
        "fundamentals_through",
        "macro_releases_through",
        "checks",
        "ready",
    }
)
_READINESS_FIELDS = frozenset(
    {
        "document_kind",
        "schema_version",
        *_US_READINESS_REPORT_FIELDS,
    }
)
_READINESS_CHECK_FIELDS = frozenset(
    {
        "check",
        "label",
        "status",
        "observed",
        "required",
        "detail",
    }
)
_READINESS_CHECK_ORDER = (
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
_SECURITY_FIELDS = frozenset(
    {
        "security_id",
        "ticker",
        "company_name",
        "rank",
        "grade",
        "qv_score",
        "quality_score",
        "value_score",
        "missing_inputs",
    }
)
_GRADES = frozenset({"A+", "A", "B", "C", "D", "N/A"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TICKER = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,15}$")
_MISSING_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEY = re.compile(
    r"(?:password|secret|token|authorization|cookie|api[_-]?key|"
    r"private[_-]?key|client[_-]?secret|path|directory|filename|command|broker)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(
        r"\b(?:password|secret|token|api[_-]?key|authorization)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:gh[pousr]_|sk-)[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"),
)
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_PROJECT_PATH = re.compile(
    r"^(?:\.{0,2}[\\/])?(?:data|backups?|logs|snapshots|\.zcode|\.env)(?:[\\/]|$)",
    re.IGNORECASE,
)


class ReadOnlyMembershipStore(Protocol):
    """The only Store surface used by the hosted exporter."""

    read_only: bool

    def universe_membership_on(
        self,
        universe_id: str,
        as_of: date | str,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class HostedResearchSnapshot:
    """Immutable canonical representation of one validated unsigned artifact."""

    _canonical_document: str = field(repr=False)

    @property
    def snapshot_id(self) -> str:
        return str(self.to_dict()["snapshot_id"])

    @property
    def payload_sha256(self) -> str:
        return str(self.to_dict()["payload_sha256"])

    def to_dict(self) -> dict[str, Any]:
        """Return a detached JSON object so callers cannot mutate this instance."""

        parsed = json.loads(self._canonical_document)
        if not isinstance(parsed, dict):  # pragma: no cover - construction invariant
            raise RuntimeError("hosted research snapshot is not a JSON object")
        return parsed

    def canonical_json(self) -> str:
        """Return stable UTF-8 JSON text without a trailing newline."""

        return self._canonical_document

    def canonical_bytes(self) -> bytes:
        return self._canonical_document.encode("utf-8")


def build_hosted_readiness_evidence(
    report: Mapping[str, Any],
) -> dict[str, Any]:
    """Version one exact ``USReadinessReport.to_dict()`` result for export.

    The readiness type itself intentionally remains outside this module's import
    graph because importing it also imports the mutable Store implementation.
    """

    normalized = _json_object(report, label="US readiness report")
    _require_exact_fields(
        normalized,
        _US_READINESS_REPORT_FIELDS,
        label="US readiness report",
    )
    return {
        "document_kind": HOSTED_READINESS_DOCUMENT_KIND,
        "schema_version": HOSTED_READINESS_SCHEMA_VERSION,
        **normalized,
    }


def build_hosted_research_snapshot(
    *,
    certified_decision_close: date | str,
    readiness_evidence: Mapping[str, Any],
    universe_id: str,
    security_rows: Sequence[Mapping[str, Any]],
    factor_policy_sha256: str,
    source_policy_sha256: str,
    created_at: datetime | str,
    serving_expires_at: datetime | str,
    memberships: Sequence[Mapping[str, Any]] | None = None,
    store: ReadOnlyMembershipStore | None = None,
) -> HostedResearchSnapshot:
    """Build one deterministic snapshot from reviewed, point-in-time evidence.

    Exactly one membership source is required.  Explicit membership rows have a
    strict two-field contract.  A Store-like source may expose its richer native
    membership rows, but must advertise ``read_only is True`` before it is
    queried.  Factor rows remain explicit reviewed inputs in both modes.
    """

    decision_close = _decision_date(certified_decision_close)
    normalized_universe = _identifier_text(universe_id, label="universe_id")
    if normalized_universe != "sp500":
        raise ValueError("hosted research snapshot v1 supports only the sp500 universe")
    if (memberships is None) == (store is None):
        raise ValueError("exactly one of memberships or store is required")
    if store is not None:
        if getattr(store, "read_only", None) is not True:
            raise ValueError("hosted export requires a Store opened read-only")
        native_memberships = store.universe_membership_on(
            normalized_universe,
            decision_close,
        )
        normalized_memberships = _normalize_memberships(
            native_memberships,
            strict_fields=False,
        )
    else:
        assert memberships is not None
        normalized_memberships = _normalize_memberships(
            memberships,
            strict_fields=True,
        )

    normalized_rows = _normalize_security_rows(security_rows)
    _require_membership_parity(normalized_memberships, normalized_rows)
    factor_identity = _sha256_text(
        factor_policy_sha256,
        label="factor_policy_sha256",
    )
    source_identity = _sha256_text(
        source_policy_sha256,
        label="source_policy_sha256",
    )
    created = _utc_timestamp(created_at, label="created_at")
    expires = _utc_timestamp(serving_expires_at, label="serving_expires_at")
    if _parse_utc_timestamp(created) >= _parse_utc_timestamp(expires):
        raise ValueError("serving_expires_at must be later than created_at")
    if _parse_utc_timestamp(expires) - _parse_utc_timestamp(created) > MAX_SERVING_TTL:
        raise ValueError("hosted research serving TTL cannot exceed 7 days")
    if _parse_utc_timestamp(created).date() < decision_close:
        raise ValueError("created_at cannot precede the certified decision close")
    readiness_sha256 = _readiness_sha256(
        readiness_evidence,
        decision_close=decision_close,
        universe_id=normalized_universe,
    )

    scored = sum(row["qv_score"] is not None for row in normalized_rows)
    total = len(normalized_rows)
    membership_sha256 = _membership_sha256(
        normalized_memberships,
        universe_id=normalized_universe,
        decision_close=decision_close,
    )
    payload = {
        "certified_decision_close": decision_close.isoformat(),
        "readiness_sha256": readiness_sha256,
        "universe_id": normalized_universe,
        "universe_membership_sha256": membership_sha256,
        "factor_policy_sha256": factor_identity,
        "source_policy_sha256": source_identity,
        "created_at": created,
        "serving_expires_at": expires,
        "coverage": {
            "total": total,
            "scored": scored,
            "withheld": total - scored,
        },
        "securities": normalized_rows,
    }
    payload_sha256 = _json_sha256(payload)
    document = {
        "document_kind": HOSTED_RESEARCH_DOCUMENT_KIND,
        "schema_version": HOSTED_RESEARCH_SCHEMA_VERSION,
        "unsigned": True,
        "canonicalization": HOSTED_RESEARCH_CANONICALIZATION,
        "hosted_promotion_allowed": False,
        "promotion_blocker": HOSTED_RESEARCH_PROMOTION_BLOCKER,
        "snapshot_id": f"{HOSTED_RESEARCH_SNAPSHOT_PREFIX}{payload_sha256}",
        "payload_sha256": payload_sha256,
        "payload": payload,
    }
    return validate_hosted_research_snapshot(document)


def validate_hosted_research_snapshot(
    artifact: Mapping[str, Any],
) -> HostedResearchSnapshot:
    """Validate an unsigned artifact and return its immutable canonical form."""

    document = _json_object(artifact, label="hosted research snapshot")
    _require_exact_fields(document, _DOCUMENT_FIELDS, label="hosted research snapshot")
    if document["document_kind"] != HOSTED_RESEARCH_DOCUMENT_KIND:
        raise ValueError("hosted research snapshot document kind is invalid")
    if document["schema_version"] != HOSTED_RESEARCH_SCHEMA_VERSION:
        raise ValueError("hosted research snapshot schema version is invalid")
    if document["unsigned"] is not True:
        raise ValueError("local hosted research snapshots must be explicitly unsigned")
    if document["canonicalization"] != HOSTED_RESEARCH_CANONICALIZATION:
        raise ValueError("hosted research canonicalization contract is invalid")
    if document["hosted_promotion_allowed"] is not False:
        raise ValueError("unsigned local snapshot cannot be enabled for hosted promotion")
    if document["promotion_blocker"] != HOSTED_RESEARCH_PROMOTION_BLOCKER:
        raise ValueError("hosted research promotion blocker is invalid")

    payload = _mapping(document["payload"], label="hosted research snapshot payload")
    _require_exact_fields(payload, _PAYLOAD_FIELDS, label="hosted research snapshot payload")
    decision_close = _decision_date(payload["certified_decision_close"])
    universe_id = _identifier_text(payload["universe_id"], label="universe_id")
    if universe_id != "sp500":
        raise ValueError("hosted research snapshot v1 supports only the sp500 universe")
    _sha256_text(payload["readiness_sha256"], label="readiness_sha256")
    expected_membership_sha256 = _sha256_text(
        payload["universe_membership_sha256"],
        label="universe_membership_sha256",
    )
    _sha256_text(payload["factor_policy_sha256"], label="factor_policy_sha256")
    _sha256_text(payload["source_policy_sha256"], label="source_policy_sha256")

    created = _utc_timestamp(payload["created_at"], label="created_at")
    expires = _utc_timestamp(payload["serving_expires_at"], label="serving_expires_at")
    if created != payload["created_at"] or expires != payload["serving_expires_at"]:
        raise ValueError("hosted research snapshot timestamps are not canonical UTC")
    if _parse_utc_timestamp(created) >= _parse_utc_timestamp(expires):
        raise ValueError("serving_expires_at must be later than created_at")
    if _parse_utc_timestamp(expires) - _parse_utc_timestamp(created) > MAX_SERVING_TTL:
        raise ValueError("hosted research serving TTL cannot exceed 7 days")
    if _parse_utc_timestamp(created).date() < decision_close:
        raise ValueError("created_at cannot precede the certified decision close")

    securities = _normalize_security_rows(_sequence(payload["securities"], label="securities"))
    if _canonical_json(securities) != _canonical_json(payload["securities"]):
        raise ValueError("hosted research security rows are not in canonical builder form")
    memberships = [
        {"security_id": row["security_id"], "ticker": row["ticker"]} for row in securities
    ]
    actual_membership_sha256 = _membership_sha256(
        memberships,
        universe_id=universe_id,
        decision_close=decision_close,
    )
    if actual_membership_sha256 != expected_membership_sha256:
        raise ValueError("hosted research universe membership checksum mismatch")

    coverage = _mapping(payload["coverage"], label="coverage")
    _require_exact_fields(coverage, _COVERAGE_FIELDS, label="coverage")
    expected_total = len(securities)
    expected_scored = sum(row["qv_score"] is not None for row in securities)
    expected_coverage = {
        "total": expected_total,
        "scored": expected_scored,
        "withheld": expected_total - expected_scored,
    }
    for key, expected in expected_coverage.items():
        value = coverage.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"coverage {key} must be an integer")
        if value != expected:
            raise ValueError("hosted research coverage does not reconcile to security rows")

    _reject_unsafe_tree(document, label="hosted research snapshot")
    expected_payload_sha256 = _json_sha256(payload)
    supplied_payload_sha256 = _sha256_text(
        document["payload_sha256"],
        label="payload_sha256",
    )
    if supplied_payload_sha256 != expected_payload_sha256:
        raise ValueError("hosted research snapshot payload checksum mismatch")
    expected_snapshot_id = f"{HOSTED_RESEARCH_SNAPSHOT_PREFIX}{expected_payload_sha256}"
    if document["snapshot_id"] != expected_snapshot_id:
        raise ValueError("hosted research snapshot id is invalid")

    canonical_document = _canonical_json(document)
    if len(canonical_document.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ValueError("hosted research snapshot exceeds the 2 MiB local contract")
    return HostedResearchSnapshot(canonical_document)


def validate_hosted_research_snapshot_for_serving(
    artifact: Mapping[str, Any],
    *,
    now: datetime | str,
) -> HostedResearchSnapshot:
    """Validate structure plus an explicit, deterministic serving instant."""

    snapshot = validate_hosted_research_snapshot(artifact)
    instant_text = _utc_timestamp(now, label="now")
    instant = _parse_utc_timestamp(instant_text)
    payload = snapshot.to_dict()["payload"]
    created = _parse_utc_timestamp(str(payload["created_at"]))
    expires = _parse_utc_timestamp(str(payload["serving_expires_at"]))
    if instant < created:
        raise ValueError("hosted research snapshot is not yet valid for serving")
    if instant >= expires:
        raise ValueError("hosted research snapshot has expired")
    return snapshot


def _normalize_memberships(
    rows: Sequence[Mapping[str, Any]],
    *,
    strict_fields: bool,
) -> list[dict[str, str]]:
    values = _sequence(rows, label="memberships")
    if not values:
        raise ValueError("hosted research membership cannot be empty")
    if len(values) > MAX_HOSTED_SECURITIES:
        raise ValueError("hosted research membership cannot exceed 600 securities")
    normalized: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_tickers: set[str] = set()
    for index, value in enumerate(values):
        row = _mapping(value, label=f"membership row {index}")
        if strict_fields:
            _require_exact_fields(
                row,
                _MEMBERSHIP_INPUT_FIELDS,
                label=f"membership row {index}",
            )
        elif not set(row) >= _MEMBERSHIP_INPUT_FIELDS:
            raise ValueError(f"membership row {index} is missing security_id or ticker")
        security_id = _identifier_text(row.get("security_id"), label="security_id")
        ticker = _ticker_text(row.get("ticker"))
        if security_id in seen_ids:
            raise ValueError(f"duplicate membership security_id: {security_id}")
        if ticker in seen_tickers:
            raise ValueError(f"duplicate membership ticker: {ticker}")
        seen_ids.add(security_id)
        seen_tickers.add(ticker)
        normalized.append({"security_id": security_id, "ticker": ticker})
    return sorted(normalized, key=lambda row: row["security_id"])


def _normalize_security_rows(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    values = _sequence(rows, label="security_rows")
    if not values:
        raise ValueError("hosted research security rows cannot be empty")
    if len(values) > MAX_HOSTED_SECURITIES:
        raise ValueError("hosted research security rows cannot exceed 600 securities")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_tickers: set[str] = set()
    for index, value in enumerate(values):
        row = _mapping(value, label=f"security row {index}")
        _require_exact_fields(row, _SECURITY_FIELDS, label=f"security row {index}")
        security_id = _identifier_text(row["security_id"], label="security_id")
        if security_id in seen_ids:
            raise ValueError(f"duplicate security row security_id: {security_id}")
        seen_ids.add(security_id)
        ticker = _ticker_text(row["ticker"])
        if ticker in seen_tickers:
            raise ValueError(f"duplicate security row ticker: {ticker}")
        seen_tickers.add(ticker)
        company_name = _optional_text(
            row["company_name"],
            label="company_name",
            maximum_length=256,
        )
        rank = _optional_rank(row["rank"])
        grade = row["grade"]
        if not isinstance(grade, str) or grade not in _GRADES:
            raise ValueError(f"security row {security_id} has an invalid grade")
        qv_score = _optional_score(row["qv_score"], label="qv_score")
        quality_score = _optional_score(row["quality_score"], label="quality_score")
        value_score = _optional_score(row["value_score"], label="value_score")
        missing_inputs = _missing_inputs(row["missing_inputs"])
        if qv_score is None:
            if rank is not None or grade != "N/A":
                raise ValueError(
                    f"withheld security row {security_id} must have no rank and grade N/A"
                )
            if not missing_inputs:
                raise ValueError(f"withheld security row {security_id} needs an evidence-gap code")
        else:
            if rank is None or grade == "N/A":
                raise ValueError(f"scored security row {security_id} needs a rank and scored grade")
            if quality_score is None or value_score is None:
                raise ValueError(
                    f"scored security row {security_id} needs Quality and Value scores"
                )
        normalized.append(
            {
                "security_id": security_id,
                "ticker": ticker,
                "company_name": company_name,
                "rank": rank,
                "grade": grade,
                "qv_score": qv_score,
                "quality_score": quality_score,
                "value_score": value_score,
                "missing_inputs": missing_inputs,
            }
        )
    normalized.sort(key=lambda row: row["security_id"])
    ranks = sorted(int(row["rank"]) for row in normalized if row["qv_score"] is not None)
    if ranks != list(range(1, len(ranks) + 1)):
        raise ValueError("scored security ranks must be unique and contiguous from 1")
    _reject_unsafe_tree(normalized, label="security rows")
    return normalized


def _require_membership_parity(
    memberships: Sequence[Mapping[str, str]],
    securities: Sequence[Mapping[str, Any]],
) -> None:
    member_routes = {str(row["security_id"]): str(row["ticker"]) for row in memberships}
    security_routes = {str(row["security_id"]): str(row["ticker"]) for row in securities}
    if member_routes != security_routes:
        missing = sorted(set(member_routes) - set(security_routes))
        extra = sorted(set(security_routes) - set(member_routes))
        mismatched = sorted(
            security_id
            for security_id in set(member_routes) & set(security_routes)
            if member_routes[security_id] != security_routes[security_id]
        )
        detail = f"missing={missing[:3]}, extra={extra[:3]}, ticker_mismatch={mismatched[:3]}"
        raise ValueError(f"security rows do not match certified membership: {detail}")


def _readiness_sha256(
    evidence: Mapping[str, Any],
    *,
    decision_close: date,
    universe_id: str,
) -> str:
    readiness = _json_object(evidence, label="readiness evidence")
    _reject_unsafe_tree(readiness, label="readiness evidence")
    _require_exact_fields(readiness, _READINESS_FIELDS, label="readiness evidence")
    if readiness["document_kind"] != HOSTED_READINESS_DOCUMENT_KIND:
        raise ValueError("readiness evidence document kind is invalid")
    if readiness["schema_version"] != HOSTED_READINESS_SCHEMA_VERSION:
        raise ValueError("readiness evidence schema version is invalid")
    try:
        evidence_date = _decision_date(readiness["as_of"])
    except ValueError as exc:
        raise ValueError("readiness evidence has no valid certified decision close") from exc
    if evidence_date != decision_close:
        raise ValueError("readiness evidence does not match certified decision close")
    evidence_universe = _identifier_text(readiness["universe_id"], label="readiness universe_id")
    if evidence_universe != universe_id:
        raise ValueError("readiness evidence does not match universe_id")
    if readiness["purpose"] != "historical_research":
        raise ValueError("hosted research requires historical_research readiness")
    generated_on = _readiness_date(readiness["generated_on"], label="generated_on")
    if generated_on < decision_close:
        raise ValueError("readiness generated_on cannot precede the decision close")
    benchmark_ticker = _ticker_text(readiness["benchmark_ticker"])
    if benchmark_ticker != "SPY":
        raise ValueError("hosted research readiness v1 requires the SPY benchmark")

    certified_from = _readiness_date(
        readiness["certified_research_from"],
        label="certified_research_from",
    )
    certified_through = _readiness_date(
        readiness["certified_research_through"],
        label="certified_research_through",
    )
    if certified_from > decision_close:
        raise ValueError("certified_research_from cannot follow the decision close")
    if certified_through != decision_close:
        raise ValueError("readiness must certify the exact decision close")
    for field_name in (
        "raw_prices_through",
        "fundamentals_through",
        "macro_releases_through",
    ):
        observed_date = _readiness_date(readiness[field_name], label=field_name)
        if observed_date > generated_on:
            raise ValueError(f"{field_name} cannot follow readiness generated_on")

    checks = _normalize_readiness_checks(readiness["checks"])
    if tuple(check["check"] for check in checks) != _READINESS_CHECK_ORDER:
        raise ValueError("readiness evidence must contain the exact ordered v1 check set")
    computed_ready = all(check["status"] != "fail" for check in checks)
    if readiness["ready"] is not computed_ready:
        raise ValueError("readiness ready flag does not reconcile to check statuses")
    if readiness["ready"] is not True:
        raise ValueError("hosted research snapshot requires ready evidence")
    if len(_canonical_json(readiness).encode("utf-8")) > MAX_READINESS_BYTES:
        raise ValueError("readiness evidence exceeds the 32 KiB local contract")
    return _json_sha256(readiness)


def _normalize_readiness_checks(value: Any) -> list[dict[str, str]]:
    values = _sequence(value, label="readiness checks")
    if len(values) != len(_READINESS_CHECK_ORDER):
        raise ValueError("readiness evidence must contain exactly 9 checks")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(values):
        check = _mapping(item, label=f"readiness check {index}")
        _require_exact_fields(
            check,
            _READINESS_CHECK_FIELDS,
            label=f"readiness check {index}",
        )
        check_id = _required_text(
            check["check"],
            label="readiness check id",
            maximum_length=64,
        )
        if check_id in seen:
            raise ValueError(f"duplicate readiness check: {check_id}")
        seen.add(check_id)
        status = check["status"]
        if status not in {"pass", "warn", "fail"}:
            raise ValueError(f"readiness check {check_id} has an invalid status")
        if status == "warn" and check_id != "data_integrity":
            raise ValueError(f"readiness check {check_id} cannot use warn status in v1")
        normalized.append(
            {
                "check": check_id,
                "label": _required_text(
                    check["label"],
                    label=f"readiness check {check_id} label",
                    maximum_length=128,
                ),
                "status": status,
                "observed": _required_text(
                    check["observed"],
                    label=f"readiness check {check_id} observed",
                    maximum_length=1_024,
                ),
                "required": _required_text(
                    check["required"],
                    label=f"readiness check {check_id} required",
                    maximum_length=1_024,
                ),
                "detail": _required_text(
                    check["detail"],
                    label=f"readiness check {check_id} detail",
                    maximum_length=2_048,
                ),
            }
        )
    return normalized


def _membership_sha256(
    memberships: Sequence[Mapping[str, str]],
    *,
    universe_id: str,
    decision_close: date,
) -> str:
    basis = {
        "certified_decision_close": decision_close.isoformat(),
        "universe_id": universe_id,
        "members": sorted(
            (
                {
                    "security_id": str(row["security_id"]),
                    "ticker": str(row["ticker"]),
                }
                for row in memberships
            ),
            key=lambda row: row["security_id"],
        ),
    }
    return _json_sha256(basis)


def _decision_date(value: Any) -> date:
    if isinstance(value, datetime):
        raise ValueError("certified decision close must be a date, not a timestamp")
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("certified decision close must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("certified decision close must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("certified decision close must use canonical YYYY-MM-DD")
    return parsed


def _readiness_date(value: Any, *, label: str) -> date:
    if value is None:
        raise ValueError(f"{label} must be present in ready evidence")
    try:
        return _decision_date(value)
    except ValueError as exc:
        raise ValueError(f"{label} must use canonical YYYY-MM-DD") from exc


def _utc_timestamp(value: Any, *, label: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value == value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} must be an ISO-8601 UTC timestamp") from exc
    else:
        raise ValueError(f"{label} must be an ISO-8601 UTC timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a UTC offset")
    parsed = parsed.astimezone(UTC)
    timespec = "microseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(timespec=timespec).replace("+00:00", "Z")


def _parse_utc_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _identifier_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} is missing or invalid")
    return value


def _ticker_text(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip() or not _TICKER.fullmatch(value):
        raise ValueError("ticker is missing or invalid")
    return value


def _optional_text(
    value: Any,
    *,
    label: str,
    maximum_length: int,
) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > maximum_length
    ):
        raise ValueError(f"{label} is invalid")
    _reject_unsafe_text(value, label=label)
    return value


def _required_text(
    value: Any,
    *,
    label: str,
    maximum_length: int,
) -> str:
    normalized = _optional_text(
        value,
        label=label,
        maximum_length=maximum_length,
    )
    if normalized is None:
        raise ValueError(f"{label} is required")
    return normalized


def _optional_rank(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("security rank must be a positive integer or null")
    return value


def _optional_score(value: Any, *, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number or null")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{label} must be finite")
    if normalized < 0 or normalized > 100:
        raise ValueError(f"{label} must be between 0 and 100")
    return 0.0 if normalized == 0 else normalized


def _missing_inputs(value: Any) -> list[str]:
    values = _sequence(value, label="missing_inputs")
    if len(values) > MAX_MISSING_INPUTS_PER_SECURITY:
        raise ValueError("missing_inputs cannot contain more than 32 evidence-gap codes")
    normalized: list[str] = []
    for item in values:
        if not isinstance(item, str) or item != item.strip() or not _MISSING_CODE.fullmatch(item):
            raise ValueError("missing_inputs contains an invalid evidence-gap code")
        _reject_unsafe_text(item, label="missing_inputs")
        normalized.append(item)
    if len(normalized) != len(set(normalized)):
        raise ValueError("missing_inputs cannot contain duplicate evidence-gap codes")
    return sorted(normalized)


def _sha256_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")
    return value


def _require_exact_fields(
    value: Mapping[str, Any],
    expected: frozenset[str],
    *,
    label: str,
) -> None:
    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    raise ValueError(f"{label} fields are invalid: missing={missing}, extra={extra}")


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} keys must be strings")
    return dict(value)


def _sequence(value: Any, *, label: str) -> list[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be an array")
    return list(value)


def _json_object(value: Any, *, label: str) -> dict[str, Any]:
    mapping = _mapping(value, label=label)
    try:
        encoded = _canonical_json(mapping)
        parsed = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must contain finite JSON values") from exc
    if not isinstance(parsed, dict):  # pragma: no cover - mapping invariant
        raise ValueError(f"{label} must be an object")
    return parsed


def _reject_unsafe_tree(value: Any, *, label: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} contains a non-string field")
            if _SENSITIVE_KEY.search(key):
                raise ValueError(f"{label} contains a forbidden field: {key}")
            _reject_unsafe_tree(item, label=label)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _reject_unsafe_tree(item, label=label)
        return
    if isinstance(value, str):
        _reject_unsafe_text(value, label=label)


def _reject_unsafe_text(value: str, *, label: str) -> None:
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{label} contains control characters")
    lowered = value.lower()
    if (
        value.startswith(("/", "~/", "./", "../", "file:"))
        or _WINDOWS_PATH.match(value)
        or "\\" in value
        or "://" in value
        or _PROJECT_PATH.match(value)
        or "/../" in lowered
        or "/./" in lowered
    ):
        raise ValueError(f"{label} contains a filesystem path or URL")
    if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
        raise ValueError(f"{label} contains secret-shaped text")


_canonical_json = canonical_json


def _json_sha256(value: Any) -> str:
    return canonical_sha256(value)

"""Governed Company Facts v3-to-v4 revenue-policy activation.

The v4 parser replays the same immutable SEC payload bytes already accepted
for v3. Planning is read-only and requires the live issuer relation to match
the exact v3 replay, including row lineage to the accepted v2 ingest or an
accepted v3 activation receipt. Preparation adds a verified local backup;
activation then performs two compare-and-set checks around a disposable
restore proof before committing the reviewed v4 delta.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

from aios.artifacts import publish_text_write_once
from aios.canonical import canonical_json, canonical_sha256, json_safe
from aios.companyfacts_replay import (
    _canonical_live_relation,
    _canonical_provider_relation,
    _normalize_as_of,
    _normalize_issuer_ids,
    _relation_delta,
    _safe_project_root,
    _source_gate_reasons,
    scoped_source_evidence,
    scoped_source_evidence_many,
    verified_companyfacts_payload_bytes,
)
from aios.companyfacts_v3_activation import (
    CompanyFactsV3ActivationResult,
    _apply_activation_transaction,
    _economics,
    _prove_disposable_activation,
    _resolve_under_root,
    _storage_key,
)
from aios.ingest.edgar import (
    COMPANYFACTS_NEXT_PARSER_VERSION,
    COMPANYFACTS_PARSER_VERSION,
    COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION,
    canonical_sec_fundamental_row_sha256,
    replay_sec_companyfacts_response,
)
from aios.operations import BackupResult, create_local_backup, verify_local_backup
from aios.raw_snapshots import canonical_parsed_rows_sha256
from aios.storage.store import Store

REVIEW_DOCUMENT_KIND = "aios.companyfacts-v4-review-plan"
REVIEW_SCHEMA_VERSION = 1
REVIEW_POLICY_VERSION = "companyfacts-v4-revenue-policy-review.v1"
REVIEW_REPORT_DIRECTORY = Path("data/reports/companyfacts_v4_replays/plans")
ACTIVATION_DOCUMENT_KIND = "aios.companyfacts-v4-activation-plan"
ACTIVATION_SCHEMA_VERSION = 1
ACTIVATION_POLICY_VERSION = "governed-companyfacts-v4-activation.v1"
ACTIVATION_REPORT_DIRECTORY = Path("data/reports/companyfacts_v4_activation_plans")


@dataclass(frozen=True)
class CompanyFactsV4Review:
    plan_sha256: str
    plan: dict[str, Any]

    @property
    def eligible_issuers(self) -> int:
        return int(self.plan["summary"]["eligible_issuers"])


@dataclass(frozen=True)
class CompanyFactsV4ActivationPreparation:
    plan_path: Path
    plan_sha256: str
    backup: BackupResult
    issuer_ids: tuple[str, ...]
    review_plan_sha256: str


@dataclass(frozen=True)
class CompanyFactsV4ActivationResult:
    activation_id: str
    plan_sha256: str
    issuer_ids: tuple[str, ...]
    counts: dict[str, int]
    generation_id: str
    version_sequence_boundary: int
    activated_at: str


def _canonical(value: Any) -> str:
    return canonical_json(json_safe(value))


def _content_sha256(value: Any) -> str:
    return canonical_sha256(json_safe(value))


def _required_actor(actor: str) -> str:
    normalized = str(actor).strip()
    if not normalized:
        raise ValueError("an actor identity is required")
    return normalized


def _accepted_v3_runs_many(
    store: Store,
    issuer_ids: Sequence[str],
) -> dict[str, set[str]]:
    requested = set(issuer_ids)
    runs: dict[str, set[str]] = {issuer_id: set() for issuer_id in requested}
    rows = store.query(
        """
        SELECT activation_run_id, issuer_ids_json
        FROM companyfacts_v3_activations
        WHERE status = 'accepted' AND target_parser_version = ?
        ORDER BY activated_at, activation_id
        """,
        (COMPANYFACTS_NEXT_PARSER_VERSION,),
    )
    for row in rows:
        try:
            receipt_issuer_ids = json.loads(str(row["issuer_ids_json"]))
        except json.JSONDecodeError:
            continue
        if not isinstance(receipt_issuer_ids, list):
            continue
        run_id = str(row["activation_run_id"])
        for issuer_id in requested.intersection(
            str(value) for value in receipt_issuer_ids
        ):
            runs[issuer_id].add(run_id)
    return runs


def _live_rows(store: Store, issuer_id: str) -> list[dict[str, Any]]:
    return store.query(
        """
        SELECT ticker, issuer_id, ingest_run_id, source_snapshot_id,
               source_rowset_sha256, source_row_sha256, source_fact_locator,
               period_end, as_of_date, fiscal_period, statement, metric,
               value, quarter_value, unit, source
        FROM fundamentals
        WHERE issuer_id = ?
        ORDER BY period_end, as_of_date, metric, ticker
        """,
        (issuer_id,),
    )


def _lineage_matches_v3(
    live_rows: list[dict[str, Any]],
    *,
    evidence: dict[str, Any],
    v2_rows: list[dict[str, Any]],
    v2_hash: str,
    v3_rows: list[dict[str, Any]],
    v3_hash: str,
    accepted_v3_runs: set[str],
) -> bool:
    v2_by_key = {_storage_key(row): row for row in v2_rows}
    v3_by_key = {_storage_key(row): row for row in v3_rows}
    for row in live_rows:
        key = _storage_key(row)
        rowset_hash = str(row.get("source_rowset_sha256") or "")
        row_hash = str(row.get("source_row_sha256") or "")
        run_id = str(row.get("ingest_run_id") or "")
        if str(row.get("source_snapshot_id") or "") != str(evidence["snapshot_id"]):
            return False
        if rowset_hash == v3_hash:
            expected = v3_by_key.get(key)
            if expected is None or run_id not in accepted_v3_runs:
                return False
            if row_hash != canonical_sec_fundamental_row_sha256(expected):
                return False
            continue
        if rowset_hash != v2_hash or run_id != str(evidence["run_id"]):
            return False
        v2_row = v2_by_key.get(key)
        v3_row = v3_by_key.get(key)
        if (
            v2_row is None
            or v3_row is None
            or _economics(v2_row) != _economics(v3_row)
            or row_hash != canonical_sec_fundamental_row_sha256(v2_row)
        ):
            return False
    return True


def _review_issuer(
    root: Path,
    *,
    store: Store,
    issuer_id: str,
    as_of: str,
    evidence: dict[str, Any] | None = None,
    accepted_v3_runs: set[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "issuer_id": issuer_id,
        "classification": "ineligible",
        "reasons": [],
        "source": None,
        "current_v3": None,
        "v4_candidate": None,
        "delta": None,
    }
    evidence = evidence or scoped_source_evidence(
        store,
        issuer_id=issuer_id,
        as_of=as_of,
    )
    if evidence is None:
        result["reasons"] = ["no_scoped_source_evidence"]
        return result
    source_reasons = _source_gate_reasons(evidence)
    if source_reasons:
        result["reasons"] = source_reasons
        return result
    result["source"] = {
        key: evidence.get(key)
        for key in (
            "snapshot_id",
            "payload_sha256",
            "parser_version",
            "parsed_row_count",
            "parsed_rows_sha256",
            "run_id",
            "received_at",
        )
    }
    try:
        payload = verified_companyfacts_payload_bytes(root, evidence)
        v2_rows, _v2_metadata = replay_sec_companyfacts_response(
            payload,
            parser_version=COMPANYFACTS_PARSER_VERSION,
        )
        v3_rows, _v3_metadata = replay_sec_companyfacts_response(
            payload,
            parser_version=COMPANYFACTS_NEXT_PARSER_VERSION,
        )
        v4_rows, v4_metadata = replay_sec_companyfacts_response(
            payload,
            parser_version=COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION,
        )
    except (KeyError, TypeError, ValueError):
        result["reasons"] = ["payload_replay_failed"]
        return result

    v2_hash = canonical_parsed_rows_sha256(v2_rows)
    v3_hash = canonical_parsed_rows_sha256(v3_rows)
    v4_hash = canonical_parsed_rows_sha256(v4_rows)
    if (
        len(v2_rows) != int(evidence["parsed_row_count"])
        or v2_hash != evidence["parsed_rows_sha256"]
    ):
        result["reasons"] = ["source_replay_mismatch"]
        return result

    try:
        reference = store.issuer_reference(issuer_id, as_of=as_of)
        if not isinstance(reference, dict):
            raise ValueError("missing issuer reference")
        reviewed_cik = int(reference["cik"])
        canonical_ticker = str(reference["canonical_ticker"]).strip().upper()
    except (KeyError, TypeError, ValueError):
        result["reasons"] = ["ambiguous_identity"]
        return result
    if not canonical_ticker or not v3_rows or int(v3_rows[0]["cik"]) != reviewed_cik:
        result["reasons"] = ["identity_cik_mismatch"]
        return result

    live_rows = _live_rows(store, issuer_id)
    current_relation = _canonical_live_relation(live_rows, cik=reviewed_cik)
    expected_v3_relation = _canonical_provider_relation(v3_rows)
    if current_relation != expected_v3_relation or any(
        str(row["ticker"]).strip().upper() != canonical_ticker for row in live_rows
    ):
        result["reasons"] = ["current_relation_mismatch"]
        return result
    accepted_runs = (
        set(accepted_v3_runs)
        if accepted_v3_runs is not None
        else _accepted_v3_runs_many(store, (issuer_id,))[issuer_id]
    )
    if not accepted_runs:
        result["reasons"] = ["missing_v3_activation_receipt"]
        return result
    if not _lineage_matches_v3(
        live_rows,
        evidence=evidence,
        v2_rows=v2_rows,
        v2_hash=v2_hash,
        v3_rows=v3_rows,
        v3_hash=v3_hash,
        accepted_v3_runs=accepted_runs,
    ):
        result["reasons"] = ["unlineaged_current_relation"]
        return result

    v4_keys = [_storage_key(row) for row in v4_rows]
    if len(v4_keys) != len(set(v4_keys)):
        result["reasons"] = ["target_storage_key_conflict"]
        return result
    v4_relation = _canonical_provider_relation(v4_rows)
    result["current_v3"] = {
        "provider_row_count": len(v3_rows),
        "provider_rows_sha256": v3_hash,
        "storage_rows_sha256": canonical_parsed_rows_sha256(expected_v3_relation),
        "accepted_activation_run_ids": sorted(accepted_runs),
    }
    result["v4_candidate"] = {
        "provider_row_count": len(v4_rows),
        "provider_rows_sha256": v4_hash,
        "storage_rows_sha256": canonical_parsed_rows_sha256(v4_relation),
        "revenue_policy": v4_metadata["revenue_policy"],
    }
    result["delta"] = _relation_delta(expected_v3_relation, v4_relation)
    result["classification"] = "eligible"
    return result


def preview_companyfacts_v4_replay(
    project_root: Path,
    *,
    store: Store,
    as_of: date | str,
    issuer_ids: Sequence[str],
) -> CompanyFactsV4Review:
    """Build a deterministic, read-only v3-to-v4 review plan."""

    root = _safe_project_root(project_root)
    decision_as_of = _normalize_as_of(as_of)
    requested = _normalize_issuer_ids(issuer_ids)
    if requested is None:
        raise ValueError("companyfacts v4 review requires an explicit issuer scope")
    evidence_by_issuer = scoped_source_evidence_many(
        store,
        issuer_ids=requested,
        as_of=decision_as_of,
    )
    accepted_runs_by_issuer = _accepted_v3_runs_many(store, requested)
    issuers = [
        _review_issuer(
            root,
            store=store,
            issuer_id=issuer_id,
            as_of=decision_as_of,
            evidence=evidence_by_issuer.get(issuer_id),
            accepted_v3_runs=accepted_runs_by_issuer[issuer_id],
        )
        for issuer_id in requested
    ]
    eligible = sum(row["classification"] == "eligible" for row in issuers)
    plan = {
        "document_kind": REVIEW_DOCUMENT_KIND,
        "schema_version": REVIEW_SCHEMA_VERSION,
        "policy_version": REVIEW_POLICY_VERSION,
        "read_only": True,
        "network_access": False,
        "as_of": decision_as_of,
        "issuer_ids": list(requested),
        "source_parser_version": COMPANYFACTS_NEXT_PARSER_VERSION,
        "target_parser_version": COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION,
        "issuers": issuers,
        "summary": {
            "eligible_issuers": eligible,
            "ineligible_issuers": len(issuers) - eligible,
        },
        "activation_available": False,
    }
    _validate_review_plan(plan)
    return CompanyFactsV4Review(_content_sha256(plan), plan)


def _validate_review_plan(plan: Any) -> None:
    if (
        not isinstance(plan, dict)
        or plan.get("document_kind") != REVIEW_DOCUMENT_KIND
        or plan.get("schema_version") != REVIEW_SCHEMA_VERSION
        or plan.get("policy_version") != REVIEW_POLICY_VERSION
        or plan.get("read_only") is not True
        or plan.get("network_access") is not False
        or plan.get("source_parser_version") != COMPANYFACTS_NEXT_PARSER_VERSION
        or plan.get("target_parser_version") != COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION
        or plan.get("activation_available") is not False
    ):
        raise ValueError("invalid Company Facts v4 review plan")
    issuer_ids = plan.get("issuer_ids")
    issuers = plan.get("issuers")
    if (
        not isinstance(issuer_ids, list)
        or issuer_ids != sorted(set(issuer_ids))
        or not issuer_ids
        or not isinstance(issuers, list)
        or [row.get("issuer_id") for row in issuers] != issuer_ids
    ):
        raise ValueError("invalid Company Facts v4 review scope")
    eligible = sum(row.get("classification") == "eligible" for row in issuers)
    if plan.get("summary") != {
        "eligible_issuers": eligible,
        "ineligible_issuers": len(issuers) - eligible,
    }:
        raise ValueError("invalid Company Facts v4 review summary")


def persist_companyfacts_v4_plan(
    project_root: Path,
    review: CompanyFactsV4Review,
) -> Path:
    root = _safe_project_root(project_root)
    _validate_review_plan(review.plan)
    if review.plan_sha256 != _content_sha256(review.plan):
        raise ValueError("Company Facts v4 review checksum mismatch")
    destination = root / REVIEW_REPORT_DIRECTORY / f"{review.plan_sha256}.json"
    encoded = (
        _canonical(
            {
                "payload_sha256": review.plan_sha256,
                "payload": review.plan,
            }
        )
        + "\n"
    )
    if destination.exists():
        if destination.is_symlink() or destination.read_text(encoding="utf-8") != encoded:
            raise ValueError("Company Facts v4 review artifact collision")
        return destination
    publish_text_write_once(destination, encoded)
    return destination


def read_companyfacts_v4_plan(
    path: Path,
    *,
    expected_sha256: str,
) -> dict[str, Any]:
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_nlink != 1:
        raise ValueError("Company Facts v4 review plan is missing or unsafe")
    try:
        envelope = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Company Facts v4 review plan is unreadable") from exc
    plan = envelope.get("payload") if isinstance(envelope, dict) else None
    if (
        not isinstance(plan, dict)
        or envelope.get("payload_sha256") != expected_sha256
        or _content_sha256(plan) != expected_sha256
    ):
        raise ValueError("Company Facts v4 review plan failed integrity check")
    _validate_review_plan(plan)
    return plan


def _verify_review_cas(
    root: Path,
    database: Path,
    *,
    review_plan_sha256: str,
    issuer_ids: Sequence[str],
    as_of: str,
) -> CompanyFactsV4Review:
    store = Store(database, read_only=True)
    try:
        current = preview_companyfacts_v4_replay(
            root,
            store=store,
            as_of=as_of,
            issuer_ids=issuer_ids,
        )
    finally:
        store.close()
    if current.plan_sha256 != review_plan_sha256:
        raise ValueError(
            "companyfacts v4 activation CAS rejected: live evidence or storage "
            "state changed since review"
        )
    return current


def prepare_companyfacts_v4_activation(
    *,
    project_root: Path,
    database_path: Path,
    application_version: str,
    review_plan_path: Path,
    review_plan_sha256: str,
    actor: str,
) -> CompanyFactsV4ActivationPreparation:
    """Bind an eligible read-only review to a fresh verified backup."""

    root = _safe_project_root(project_root)
    database = _resolve_under_root(root, Path(database_path))
    normalized_actor = _required_actor(actor)
    reviewed = read_companyfacts_v4_plan(
        review_plan_path,
        expected_sha256=review_plan_sha256,
    )
    ineligible = [
        row["issuer_id"] for row in reviewed["issuers"] if row["classification"] != "eligible"
    ]
    if ineligible:
        raise ValueError(f"companyfacts v4 activation refuses ineligible issuer(s): {ineligible}")
    _verify_review_cas(
        root,
        database,
        review_plan_sha256=review_plan_sha256,
        issuer_ids=reviewed["issuer_ids"],
        as_of=reviewed["as_of"],
    )
    backup = create_local_backup(
        root,
        database,
        application_version=application_version,
    )
    payload: dict[str, Any] = {
        "document_kind": ACTIVATION_DOCUMENT_KIND,
        "schema_version": ACTIVATION_SCHEMA_VERSION,
        "policy_version": ACTIVATION_POLICY_VERSION,
        "as_of": reviewed["as_of"],
        "issuer_ids": reviewed["issuer_ids"],
        "actor": normalized_actor,
        "activation_run_id": str(uuid4()),
        "source_parser_version": COMPANYFACTS_NEXT_PARSER_VERSION,
        "target_parser_version": COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION,
        "review_plan_sha256": review_plan_sha256,
        "backup": {
            "path": backup.path.relative_to(root).as_posix(),
            "manifest_sha256": backup.manifest_sha256,
        },
        "expected_after": {
            row["issuer_id"]: {
                "provider_row_count": row["v4_candidate"]["provider_row_count"],
                "provider_rows_sha256": row["v4_candidate"]["provider_rows_sha256"],
                "delta": row["delta"],
            }
            for row in reviewed["issuers"]
        },
    }
    payload["activation_plan_sha256"] = _content_sha256(payload)
    plan_sha256 = payload["activation_plan_sha256"]
    destination = root / ACTIVATION_REPORT_DIRECTORY / f"{plan_sha256}.json"
    publish_text_write_once(destination, _canonical(payload) + "\n")
    return CompanyFactsV4ActivationPreparation(
        plan_path=destination,
        plan_sha256=plan_sha256,
        backup=backup,
        issuer_ids=tuple(reviewed["issuer_ids"]),
        review_plan_sha256=review_plan_sha256,
    )


def _read_activation_plan(path: Path, expected_sha256: str) -> dict[str, Any]:
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_nlink != 1:
        raise ValueError("Company Facts v4 activation plan is missing or unsafe")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Company Facts v4 activation plan is unreadable") from exc
    stored = payload.get("activation_plan_sha256") if isinstance(payload, dict) else None
    unhashed = dict(payload) if isinstance(payload, dict) else {}
    unhashed.pop("activation_plan_sha256", None)
    if (
        payload.get("document_kind") != ACTIVATION_DOCUMENT_KIND
        or payload.get("schema_version") != ACTIVATION_SCHEMA_VERSION
        or payload.get("policy_version") != ACTIVATION_POLICY_VERSION
        or payload.get("source_parser_version") != COMPANYFACTS_NEXT_PARSER_VERSION
        or payload.get("target_parser_version")
        != COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION
        or stored != expected_sha256
        or _content_sha256(unhashed) != expected_sha256
    ):
        raise ValueError("Company Facts v4 activation plan failed integrity check")
    return payload


def _result(value: CompanyFactsV3ActivationResult) -> CompanyFactsV4ActivationResult:
    return CompanyFactsV4ActivationResult(
        activation_id=value.activation_id,
        plan_sha256=value.plan_sha256,
        issuer_ids=value.issuer_ids,
        counts=value.counts,
        generation_id=value.generation_id,
        version_sequence_boundary=value.version_sequence_boundary,
        activated_at=value.activated_at,
    )


def activate_companyfacts_v4(
    *,
    project_root: Path,
    database_path: Path,
    plan_path: Path,
    expected_plan_sha256: str,
    actor: str,
    confirm: bool,
) -> CompanyFactsV4ActivationResult:
    """Prove the reviewed v4 transition on a restore, then commit it live."""

    if not confirm:
        raise ValueError("explicit --confirm-activation approval is required")
    root = _safe_project_root(project_root)
    database = _resolve_under_root(root, Path(database_path))
    normalized_actor = _required_actor(actor)
    plan = _read_activation_plan(plan_path, expected_plan_sha256)
    if plan["actor"] != normalized_actor:
        raise ValueError("activation actor differs from the reviewed plan")
    backup = verify_local_backup(root / plan["backup"]["path"])
    if backup.manifest_sha256 != plan["backup"]["manifest_sha256"]:
        raise ValueError("activation backup no longer matches the plan")

    _verify_review_cas(
        root,
        database,
        review_plan_sha256=plan["review_plan_sha256"],
        issuer_ids=plan["issuer_ids"],
        as_of=plan["as_of"],
    )
    _prove_disposable_activation(root, backup, plan)
    _verify_review_cas(
        root,
        database,
        review_plan_sha256=plan["review_plan_sha256"],
        issuer_ids=plan["issuer_ids"],
        as_of=plan["as_of"],
    )

    store = Store(database)
    try:
        applied = _apply_activation_transaction(
            store=store,
            root=root,
            plan=plan,
            plan_sha256=expected_plan_sha256,
        )
    finally:
        store.close()
    reopened = Store(database, read_only=True)
    try:
        receipt = reopened.query(
            """
            SELECT status, target_parser_version
            FROM companyfacts_v3_activations
            WHERE activation_id = ?
            """,
            (applied.activation_id,),
        )
        if receipt != [
            {
                "status": "accepted",
                "target_parser_version": COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION,
            }
        ]:
            raise RuntimeError("Company Facts v4 activation receipt did not verify")
    finally:
        reopened.close()
    return _result(applied)

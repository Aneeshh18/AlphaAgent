"""Governed, content-addressed Company Facts v3 parser activation.

`companyfacts_replay.py`'s planner proves an issuer's v2 evidence and current
storage relation are fully lineaged, then classifies it eligible for the
reviewed v3 taxonomy-aware, conflict-withholding parser. That planner is
permanently read-only: it never fetches, mutates, or activates. This module
is the separate, explicit, human-confirmed path that actually commits the
migration for one reviewed issuer scope.

The pattern mirrors `universe_change_activation.py`: prepare captures a fresh
verified backup and publishes an immutable plan; activation re-verifies the
plan is still current (compare-and-set against freshly recomputed evidence),
proves the exact transaction on a disposable restore of that backup before
touching anything live, then commits in one DuckDB transaction and writes an
append-only receipt. It never fetches from a provider — v3 is a re-parse of
the same already-captured, already-verified payload bytes v2 used, so no
network access or new raw-snapshot capture is involved anywhere in this
module.

Mutation touches three tables. `fundamentals` (the current projection) gets
an upsert for every added/changed storage key via the frozen `Store`'s own
`upsert_fundamentals`, which already versions the merged projection into
`fundamental_versions`. A key v3 correctly withholds that v2 had silently
kept (a genuine multi-filing conflict) is tombstoned the same way: its
current row is versioned with `is_deleted=TRUE` before being removed from
`fundamentals`. One `fundamental_evidence_generations` row pins the
resulting `version_sequence` boundary so any future factor read can name
exactly this activation's evidence state.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from aios.artifacts import publish_text_write_once
from aios.canonical import canonical_json, canonical_sha256, json_safe
from aios.companyfacts_replay import (
    preview_companyfacts_v3_replay,
    scoped_source_evidence,
    verified_companyfacts_payload_bytes,
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

ACTIVATION_PLAN_DOCUMENT_KIND = "aios.companyfacts-v3-activation-plan"
ACTIVATION_PLAN_SCHEMA_VERSION = 1
ACTIVATION_POLICY_VERSION = "governed-companyfacts-v3-activation.v1"
ACTIVATION_PLAN_REPORT_DIRECTORY = Path("data/reports/companyfacts_v3_activation_plans")
ACTIVATION_GENERATION_PURPOSE = "companyfacts_v3_activation"


@dataclass(frozen=True)
class CompanyFactsV3ActivationPreparation:
    """One published activation plan and its recovery evidence."""

    plan_path: Path
    plan_sha256: str
    backup: BackupResult
    issuer_ids: tuple[str, ...]
    review_plan_sha256: str


@dataclass(frozen=True)
class CompanyFactsV3ActivationResult:
    """One accepted atomic activation."""

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


def _resolve_under_root(root: Path, path: Path) -> Path:
    candidate = Path(path)
    resolved = candidate if candidate.is_absolute() else root / candidate
    resolved = resolved.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"path escapes project root: {path}")
    return resolved


def _storage_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row["period_end"]), str(row["as_of_date"]), str(row["metric"]))


def _economics(row: dict[str, Any]) -> tuple[Any, ...]:
    """Mirror `companyfacts_replay._provider_economic_row`'s comparison fields.

    Deliberately excludes `source_fact_locator`: v3 always attaches it and v2
    never does, so raw dict equality would flag every row as changed. The
    plan's already-reported delta counts are economics-only for the same
    reason; matching that comparison keeps activation counts consistent with
    what was reviewed.
    """
    return (
        row.get("fiscal_period"),
        row.get("statement"),
        row.get("value"),
        row.get("quarter_value"),
        row.get("unit") or "USD",
        row.get("source") or "edgar",
    )


def prepare_companyfacts_v3_activation(
    *,
    project_root: Path,
    database_path: Path,
    application_version: str,
    as_of: date | str,
    issuer_ids: Sequence[str],
    actor: str,
) -> CompanyFactsV3ActivationPreparation:
    """Review one explicit issuer scope, back up, and publish an activation plan.

    Every requested issuer must currently classify eligible in the read-only
    planner; anything else refuses before a backup is even taken. No provider
    fetch happens here or anywhere in this module — v3 replays the same
    payload bytes v2 already captured and verified.
    """
    root = Path(project_root).resolve()
    database = _resolve_under_root(root, Path(database_path))
    normalized_actor = _required_actor(actor)
    if not issuer_ids:
        raise ValueError("companyfacts v3 activation requires an explicit issuer scope")
    requested = tuple(sorted({str(value).strip() for value in issuer_ids}))
    if any(not value for value in requested):
        raise ValueError("companyfacts v3 activation issuer scope contains an empty id")

    store = Store(database, read_only=True)
    try:
        preview = preview_companyfacts_v3_replay(
            root, store=store, as_of=as_of, issuer_ids=requested
        )
    finally:
        store.close()
    plan = preview.plan
    by_id = {row["issuer_id"]: row for row in plan["issuers"]}
    missing = [issuer_id for issuer_id in requested if issuer_id not in by_id]
    if missing:
        raise ValueError(f"companyfacts v3 activation found no evidence for: {missing}")
    ineligible = [
        issuer_id for issuer_id in requested if by_id[issuer_id]["classification"] != "eligible"
    ]
    if ineligible:
        raise ValueError(f"companyfacts v3 activation refuses ineligible issuer(s): {ineligible}")

    backup = create_local_backup(root, database, application_version=application_version)

    activation_payload: dict[str, Any] = {
        "document_kind": ACTIVATION_PLAN_DOCUMENT_KIND,
        "schema_version": ACTIVATION_PLAN_SCHEMA_VERSION,
        "policy_version": ACTIVATION_POLICY_VERSION,
        "as_of": plan["scope"]["as_of"],
        "issuer_ids": list(requested),
        "actor": normalized_actor,
        "activation_run_id": str(uuid4()),
        "source_parser_version": plan["source_parser_version"],
        "target_parser_version": plan["target_parser_version"],
        "review_plan_sha256": preview.plan_sha256,
        "review_plan": plan,
        "backup": {
            "path": backup.path.relative_to(root).as_posix(),
            "manifest_sha256": backup.manifest_sha256,
        },
        "expected_after": {
            row["issuer_id"]: {
                "provider_row_count": row["v3_candidate"]["provider_row_count"],
                "provider_rows_sha256": row["v3_candidate"]["provider_rows_sha256"],
                "delta": row["delta"],
            }
            for row in plan["issuers"]
            if row["issuer_id"] in requested
        },
    }
    activation_payload["activation_plan_sha256"] = _content_sha256(activation_payload)
    plan_sha256 = activation_payload["activation_plan_sha256"]
    destination = root / ACTIVATION_PLAN_REPORT_DIRECTORY / f"{plan_sha256}.json"
    publish_text_write_once(destination, _canonical(activation_payload) + "\n")
    return CompanyFactsV3ActivationPreparation(
        plan_path=destination,
        plan_sha256=plan_sha256,
        backup=backup,
        issuer_ids=requested,
        review_plan_sha256=preview.plan_sha256,
    )


def _read_activation_plan(path: Path, expected_plan_sha256: str) -> dict[str, Any]:
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_nlink != 1:
        raise ValueError(f"companyfacts v3 activation plan is missing or unsafe: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"companyfacts v3 activation plan is unreadable: {source}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("document_kind") != ACTIVATION_PLAN_DOCUMENT_KIND
        or payload.get("schema_version") != ACTIVATION_PLAN_SCHEMA_VERSION
    ):
        raise ValueError("unsupported companyfacts v3 activation plan artifact")
    stored_hash = payload.get("activation_plan_sha256")
    recomputed = dict(payload)
    recomputed.pop("activation_plan_sha256", None)
    if _content_sha256(recomputed) != stored_hash:
        raise ValueError("companyfacts v3 activation plan failed integrity check")
    if stored_hash != expected_plan_sha256:
        raise ValueError("companyfacts v3 activation plan does not match the expected hash")
    if (
        payload.get("policy_version") != ACTIVATION_POLICY_VERSION
        or payload.get("source_parser_version") != COMPANYFACTS_PARSER_VERSION
        or payload.get("target_parser_version") != COMPANYFACTS_NEXT_PARSER_VERSION
    ):
        raise ValueError("companyfacts v3 activation plan has an invalid policy transition")
    return payload


def _verify_live_cas(root: Path, database: Path, plan: dict[str, Any]) -> None:
    """Refuse activation if live evidence or storage state drifted since review.

    Re-runs the exact deterministic planner over the same scope; any change
    to the underlying evidence or current relation changes its hash.
    """
    store = Store(database, read_only=True)
    try:
        preview = preview_companyfacts_v3_replay(
            root, store=store, as_of=plan["as_of"], issuer_ids=plan["issuer_ids"]
        )
    finally:
        store.close()
    if preview.plan_sha256 != plan["review_plan_sha256"]:
        raise ValueError(
            "companyfacts v3 activation CAS rejected: live evidence or storage "
            "state changed since this plan was reviewed"
        )


def _apply_activation_transaction(
    *, store: Store, root: Path, plan: dict[str, Any], plan_sha256: str
) -> CompanyFactsV3ActivationResult:
    source_parser_version = str(plan["source_parser_version"])
    target_parser_version = str(plan["target_parser_version"])
    if (source_parser_version, target_parser_version) not in {
        (COMPANYFACTS_PARSER_VERSION, COMPANYFACTS_NEXT_PARSER_VERSION),
        (
            COMPANYFACTS_NEXT_PARSER_VERSION,
            COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION,
        ),
    }:
        raise ValueError("unsupported governed Company Facts parser transition")
    target_label = target_parser_version.removeprefix("sec-companyfacts-")
    activation_label = f"companyfacts {target_label}"
    if store.query(
        "SELECT 1 FROM companyfacts_v3_activations WHERE activation_plan_sha256 = ?",
        (plan_sha256,),
    ):
        raise ValueError(f"this {activation_label} activation plan is already activated")

    activation_run_id = plan["activation_run_id"]
    decision_as_of = plan["as_of"]
    counts = {"added": 0, "removed": 0, "changed": 0, "issuers": 0}

    store.execute("BEGIN TRANSACTION")
    try:
        for issuer_id in plan["issuer_ids"]:
            reference = store.issuer_reference(issuer_id, as_of=decision_as_of)
            if not isinstance(reference, dict):
                raise ValueError(f"activation lost reviewed identity for {issuer_id}")
            canonical_ticker = str(reference["canonical_ticker"]).strip().upper()

            evidence = scoped_source_evidence(store, issuer_id=issuer_id, as_of=decision_as_of)
            if evidence is None:
                raise ValueError(f"activation evidence vanished for {issuer_id}")
            payload = verified_companyfacts_payload_bytes(root, evidence)

            source_rows, _source_metadata = replay_sec_companyfacts_response(
                payload,
                parser_version=source_parser_version,
            )
            target_rows, _target_metadata = replay_sec_companyfacts_response(
                payload,
                parser_version=target_parser_version,
            )
            target_provider_hash = canonical_parsed_rows_sha256(target_rows)
            expected = plan["expected_after"][issuer_id]
            if (
                len(target_rows) != expected["provider_row_count"]
                or target_provider_hash != expected["provider_rows_sha256"]
            ):
                raise ValueError(
                    f"activation CAS rejected: {target_label} replay for {issuer_id} no longer "
                    "matches the reviewed plan"
                )

            source_by_key = {_storage_key(row): row for row in source_rows}
            target_by_key = {_storage_key(row): row for row in target_rows}
            removed_keys = sorted(set(source_by_key) - set(target_by_key))
            to_upsert = [
                row
                for key, row in target_by_key.items()
                if key not in source_by_key or _economics(source_by_key[key]) != _economics(row)
            ]
            expected_delta = expected["delta"]
            if (
                len(to_upsert) - len(target_by_key.keys() - source_by_key.keys())
                != expected_delta["changed_storage_keys"]
                or len(target_by_key.keys() - source_by_key.keys())
                != expected_delta["added_storage_keys"]
                or len(removed_keys) != expected_delta["removed_storage_keys"]
            ):
                raise ValueError(
                    f"activation delta for {issuer_id} no longer matches the reviewed plan"
                )

            # `fundamentals`' primary key is (ticker, period_end, as_of_date,
            # metric) — not issuer-scoped. A ticker can legitimately be
            # claimed by two issuer_ids (a CIK-successor event, e.g. a
            # reincorporation): BG, XOM, and BLK all have this shape live.
            # Refuse outright if any key this activation would touch already
            # belongs to a *different* issuer, rather than silently
            # reassigning or deleting that issuer's row.
            touched_keys = {_storage_key(row) for row in to_upsert} | set(removed_keys)
            if touched_keys:
                other_issuer_rows = store.query(
                    "SELECT DISTINCT period_end, as_of_date, metric, issuer_id "
                    "FROM fundamentals WHERE ticker = ? AND issuer_id IS NOT NULL "
                    "AND issuer_id <> ?",
                    (canonical_ticker, issuer_id),
                )
                collisions = [
                    row
                    for row in other_issuer_rows
                    if (str(row["period_end"]), str(row["as_of_date"]), str(row["metric"]))
                    in touched_keys
                ]
                if collisions:
                    raise ValueError(
                        f"activation for {issuer_id} refused: ticker {canonical_ticker!r} "
                        f"key(s) belong to a different issuer_id "
                        f"({collisions[0]['issuer_id']!r}) — shared-ticker CIK "
                        "successor, not a lineage bug"
                    )

            upsert_rows = [
                {
                    "ticker": canonical_ticker,
                    "issuer_id": issuer_id,
                    "security_id": None,
                    "ingest_run_id": activation_run_id,
                    "source_snapshot_id": evidence["snapshot_id"],
                    "source_rowset_sha256": target_provider_hash,
                    "source_row_sha256": canonical_sec_fundamental_row_sha256(row),
                    "source_fact_locator": row.get("source_fact_locator"),
                    "period_end": row["period_end"],
                    "as_of_date": row["as_of_date"],
                    "fiscal_period": row.get("fiscal_period"),
                    "statement": row.get("statement"),
                    "metric": row["metric"],
                    "value": row.get("value"),
                    "quarter_value": row.get("quarter_value"),
                    "unit": row.get("unit") or "USD",
                    "source": row.get("source") or "edgar",
                }
                for row in to_upsert
            ]
            store.upsert_fundamentals(upsert_rows, _manage_transaction=False)

            for period_end, as_of_value, metric in removed_keys:
                store.execute(
                    """
                    INSERT INTO fundamental_versions
                    (ticker, issuer_id, security_id, ingest_run_id,
                     source_snapshot_id, source_rowset_sha256,
                     source_row_sha256, source_fact_locator, period_end,
                     as_of_date, fiscal_period, statement, metric, value,
                     quarter_value, unit, source, recorded_at, is_deleted)
                    SELECT ticker, issuer_id, security_id, ingest_run_id,
                           source_snapshot_id, source_rowset_sha256,
                           source_row_sha256, source_fact_locator, period_end,
                           as_of_date, fiscal_period, statement, metric, value,
                           quarter_value, unit, source, fetched_at, TRUE
                    FROM fundamentals
                    WHERE ticker = ? AND issuer_id = ? AND period_end = CAST(? AS DATE)
                      AND as_of_date = CAST(? AS DATE) AND metric = ?
                    """,
                    (canonical_ticker, issuer_id, period_end, as_of_value, metric),
                )
                store.execute(
                    """
                    DELETE FROM fundamentals
                    WHERE ticker = ? AND issuer_id = ? AND period_end = CAST(? AS DATE)
                      AND as_of_date = CAST(? AS DATE) AND metric = ?
                    """,
                    (canonical_ticker, issuer_id, period_end, as_of_value, metric),
                )

            live_rows = store.query(
                """
                SELECT period_end, as_of_date, fiscal_period, statement, metric,
                       value, quarter_value, unit, source
                FROM fundamentals
                WHERE ticker = ? AND issuer_id = ?
                """,
                (canonical_ticker, issuer_id),
            )
            live_by_key = {
                (str(row["period_end"]), str(row["as_of_date"]), str(row["metric"])): row
                for row in live_rows
            }
            expected_by_key = {**source_by_key, **target_by_key}
            for key in removed_keys:
                expected_by_key.pop(key, None)
            if set(live_by_key) != set(expected_by_key) or any(
                _economics(live_by_key[key]) != _economics(expected_by_key[key])
                for key in live_by_key
            ):
                raise ValueError(
                    f"activation write for {issuer_id} does not match the "
                    f"intended {target_label} relation"
                )

            counts["added"] += len(target_by_key.keys() - source_by_key.keys())
            counts["removed"] += len(removed_keys)
            counts["changed"] += len(to_upsert) - len(target_by_key.keys() - source_by_key.keys())
            counts["issuers"] += 1

        generation_id = f"fundamental-generation-{uuid4().hex}"
        activated_at = datetime.now(UTC).replace(tzinfo=None)
        store.execute(
            """
            INSERT INTO fundamental_evidence_generations
            (generation_id, version_sequence, purpose, decision_date, captured_at)
            SELECT ?, COALESCE(MAX(version_sequence), 0), ?, CAST(? AS DATE), ?
            FROM fundamental_versions
            """,
            (
                generation_id,
                f"companyfacts_{target_label.replace('-', '_')}_activation",
                decision_as_of,
                activated_at,
            ),
        )
        version_sequence_boundary = int(
            store.query(
                "SELECT version_sequence FROM fundamental_evidence_generations "
                "WHERE generation_id = ?",
                (generation_id,),
            )[0]["version_sequence"]
        )

        store.record_ingest(
            source=(f"governed-companyfacts-{target_label}-activation:{plan_sha256[:16]}"),
            table_name="fundamentals",
            rows_inserted=counts["added"] + counts["changed"],
            rows_rejected=counts["removed"],
            started_at=activated_at,
            finished_at=activated_at,
            status="success",
            run_id=activation_run_id,
        )

        activation_id = f"cf{target_label}-activation-{uuid4().hex}"
        activation_payload = {
            "activation_id": activation_id,
            "plan_sha256": plan_sha256,
            "review_plan_sha256": plan["review_plan_sha256"],
            "activation_run_id": activation_run_id,
            "as_of": decision_as_of,
            "issuer_ids": list(plan["issuer_ids"]),
            "counts": counts,
            "generation_id": generation_id,
            "version_sequence_boundary": version_sequence_boundary,
            "backup_manifest_sha256": plan["backup"]["manifest_sha256"],
            "actor": plan["actor"],
            "policy_version": plan["policy_version"],
            "activated_at": activated_at.isoformat(),
            "safety": {
                "paper_mutated": False,
                "broker_used": False,
                "retrospective_fill": False,
            },
        }
        store.execute(
            """
            INSERT INTO companyfacts_v3_activations
            (activation_id, activation_plan_sha256, review_plan_sha256,
             activation_run_id, schema_version, as_of, issuer_ids_json,
             source_parser_version, target_parser_version,
             activation_payload_json, backup_manifest_sha256, actor,
             policy_version, counts_json, generation_id,
             version_sequence_boundary, activated_at, status)
            VALUES (?, ?, ?, ?, 1, CAST(? AS DATE), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'accepted')
            """,
            (
                activation_id,
                plan_sha256,
                plan["review_plan_sha256"],
                activation_run_id,
                decision_as_of,
                _canonical(plan["issuer_ids"]),
                source_parser_version,
                target_parser_version,
                _canonical(activation_payload),
                plan["backup"]["manifest_sha256"],
                plan["actor"],
                plan["policy_version"],
                _canonical(counts),
                generation_id,
                version_sequence_boundary,
                activated_at,
            ),
        )
        store.execute("COMMIT")
    except Exception:
        store.execute("ROLLBACK")
        raise

    return CompanyFactsV3ActivationResult(
        activation_id=activation_id,
        plan_sha256=plan_sha256,
        issuer_ids=tuple(plan["issuer_ids"]),
        counts=counts,
        generation_id=generation_id,
        version_sequence_boundary=version_sequence_boundary,
        activated_at=activated_at.isoformat(),
    )


def _prove_disposable_activation(root: Path, backup: BackupResult, plan: dict[str, Any]) -> None:
    manifest = json.loads((backup.path / "manifest.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="aios-companyfacts-v3-activation-") as temporary:
        scratch = Path(temporary)
        (scratch / "data").mkdir()
        shutil.copy2(
            backup.path / manifest["database_file"],
            scratch / "data" / "aios.duckdb",
        )
        raw = backup.path / "raw"
        if raw.is_dir():
            shutil.copytree(raw, scratch / "data" / "raw")
        store = Store(scratch / "data" / "aios.duckdb")
        try:
            _apply_activation_transaction(
                store=store,
                root=scratch,
                plan=plan,
                plan_sha256=plan["activation_plan_sha256"],
            )
        finally:
            store.close()
        verified = Store(scratch / "data" / "aios.duckdb", read_only=True)
        verified.close()


def activate_companyfacts_v3(
    *,
    project_root: Path,
    database_path: Path,
    plan_path: Path,
    expected_plan_sha256: str,
    actor: str,
    confirm: bool,
) -> CompanyFactsV3ActivationResult:
    """Recheck one published activation plan, prove disposable rollback, then commit live."""

    if not confirm:
        raise ValueError("explicit --confirm-activation approval is required")
    root = Path(project_root).resolve()
    database = _resolve_under_root(root, Path(database_path))
    normalized_actor = _required_actor(actor)
    plan = _read_activation_plan(plan_path, expected_plan_sha256)
    if plan["actor"] != normalized_actor:
        raise ValueError("activation actor differs from the reviewed plan")

    backup = verify_local_backup(root / plan["backup"]["path"])
    if backup.manifest_sha256 != plan["backup"]["manifest_sha256"]:
        raise ValueError("activation backup no longer matches the plan")

    _verify_live_cas(root, database, plan)
    _prove_disposable_activation(root, backup, plan)
    _verify_live_cas(root, database, plan)

    store = Store(database)
    try:
        result = _apply_activation_transaction(
            store=store,
            root=root,
            plan=plan,
            plan_sha256=expected_plan_sha256,
        )
    finally:
        store.close()

    reopened = Store(database, read_only=True)
    try:
        stored = reopened.query(
            "SELECT status FROM companyfacts_v3_activations WHERE activation_id = ?",
            (result.activation_id,),
        )
        if not stored or stored[0]["status"] != "accepted":
            raise RuntimeError("activation receipt did not verify after commit")
    finally:
        reopened.close()
    return result

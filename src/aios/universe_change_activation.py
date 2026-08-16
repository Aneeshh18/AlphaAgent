"""Governed, content-addressed S&P 500 constituent-change activation.

The automatic universe reviewer is intentionally allowed to certify only a
no-change interval.  This module is the narrower human-approved path for one
already-announced addition/deletion pair.  Preparation captures the entering
security's exact source evidence, binds reviewed manifests and a verified
backup, and publishes an immutable plan.  Activation replays every comparison
and commits references, prices, coverage, and an append-only receipt in one
DuckDB transaction.  It never mutates paper state or enables a broker route.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from aios.artifacts import publish_text_write_once
from aios.canonical import canonical_json, json_safe
from aios.ingest.edgar import COMPANYFACTS_LEGACY_PARSER_VERSION, extract_fundamentals
from aios.ingest.http_client import RawSnapshotContext, get_http
from aios.ingest.prices import fetch_provider_prices, relabel_provider_price_rows
from aios.ingest.reference_batch import merge_reference_batch_files
from aios.ingest.universe import load_universe_events_csv
from aios.market_calendar import us_equity_sessions
from aios.operations import (
    BackupResult,
    create_local_backup,
    drill_local_backup,
    verify_local_backup,
)
from aios.raw_snapshots import (
    canonical_request_fingerprint,
    read_verified_raw_snapshot,
)
from aios.storage.store import Store, checkpoint_database_for_backup
from aios.universe_change import (
    RawEvidenceExpectation,
    build_universe_change_plan,
    capture_paper_tree_state,
    capture_universe_change_state,
)
from aios.universe_rollforward import (
    COMPONENT_SNAPSHOT_URL,
    IVV_HOLDINGS_URL,
    sp500_archive_page_url,
)

ACTIVATION_PLAN_KIND = "aios.universe-change-activation-plan"
ACTIVATION_PLAN_SCHEMA_VERSION = 1
ACTIVATION_RECEIPT_SCHEMA_VERSION = "universe-change-activation-receipt.v1"
ACTIVATION_POLICY_VERSION = "governed-sp500-constituent-activation.v1"
IVV_CAPTURE_PARSER_VERSION = "ishares-ivv-holdings-csv-capture-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_PLAN_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class UniverseChangePreparation:
    """Published activation plan and its recovery evidence."""

    plan_path: Path
    plan_sha256: str
    backup: BackupResult
    restore_files: int
    restore_raw_payloads: int
    fundamental_status: str
    price_rows: int


@dataclass(frozen=True)
class UniverseChangeActivationResult:
    """One accepted atomic activation."""

    activation_id: str
    event_id: str
    plan_sha256: str
    prior_coverage_through: str
    target_coverage_through: str
    after_state_sha256: str
    counts: dict[str, int]
    disposable_rollback_proved: bool
    paper_mutated: bool = False
    broker_used: bool = False


def parse_ivv_holdings(payload: bytes) -> dict[str, Any]:
    """Strictly parse a dated official IVV holdings export."""

    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("IVV holdings response is not UTF-8 CSV") from exc
    rows = list(csv.reader(StringIO(text), strict=True))
    if len(rows) < 10 or rows[0] != ["iShares Core S&P 500 ETF"]:
        raise ValueError("IVV holdings response has an unsupported preamble")
    if len(rows[1]) != 2 or rows[1][0] != "Fund Holdings as of":
        raise ValueError("IVV holdings response has no exact as-of date")
    try:
        as_of = datetime.strptime(rows[1][1], "%b %d, %Y").date()
    except ValueError as exc:
        raise ValueError("IVV holdings as-of date is invalid") from exc
    expected_header = (
        "Ticker",
        "Name",
        "Sector",
        "Asset Class",
        "Market Value",
        "Weight (%)",
        "Notional Value",
        "Quantity",
        "Price",
        "Location",
        "Exchange",
        "Currency",
        "FX Rate",
        "Market Currency",
        "Accrual Date",
    )
    header_positions = [index for index, row in enumerate(rows) if tuple(row) == expected_header]
    if header_positions != [9] or rows[8] != []:
        raise ValueError("IVV holdings response has an unsupported column contract")
    holdings: list[dict[str, str]] = []
    for line_number, values in enumerate(rows[10:], start=11):
        if not values or not any(value.strip() for value in values):
            break
        if len(values) != len(expected_header):
            raise ValueError(f"IVV holdings row {line_number} has the wrong field count")
        row = dict(zip(expected_header, (value.strip() for value in values), strict=True))
        ticker = row["Ticker"].upper()
        if not ticker:
            raise ValueError(f"IVV holdings row {line_number} has no ticker")
        holdings.append({**row, "Ticker": ticker})
    equities = [row for row in holdings if row["Asset Class"] == "Equity"]
    tickers = [row["Ticker"] for row in equities]
    if len(equities) < 490 or len(tickers) != len(set(tickers)):
        raise ValueError("IVV holdings response has an implausible equity set")
    return {
        "fund": "IVV",
        "benchmark": "S&P 500 Index (USD)",
        "as_of": as_of.isoformat(),
        "equity_count": len(equities),
        "ticker_set_sha256": _sha256(sorted(tickers)),
        "tickers": sorted(tickers),
    }


def prepare_sp500_constituent_activation(
    *,
    project_root: Path,
    database_path: Path,
    operations_database_path: Path,
    application_version: str,
    actor: str,
    event_path: Path,
    reference_stem: Path,
    official_release_url: str,
    expected_effective_date: date,
    expected_member_count: int = 503,
) -> UniverseChangePreparation:
    """Capture entering evidence, drill recovery, and publish one exact plan."""

    root = Path(project_root).resolve()
    database = _under_root(root, database_path, label="analytical database")
    operations = _under_root(root, operations_database_path, label="operations database")
    normalized_actor = _required_actor(actor)
    source_files = _source_files(root, event_path=event_path, reference_stem=reference_stem)
    events = load_universe_events_csv(
        source_files["event"], universe_id="sp500", require_official_sources=True
    )
    event_projection = _validate_event_batch(
        events,
        official_release_url=official_release_url,
        expected_effective_date=expected_effective_date,
    )
    reference = _load_reference_projection(
        source_files,
        expected_effective_date=expected_effective_date,
    )
    source_hashes = {
        label: {
            "path": path.relative_to(root).as_posix(),
            "sha256": _file_sha256(path),
        }
        for label, path in sorted(source_files.items())
    }

    holdings_run_id = str(uuid4())
    price_run_id = str(uuid4())
    fundamental_run_id = str(uuid4())
    stage_started = datetime.now(UTC).replace(tzinfo=None)
    store = Store(database)
    try:
        holdings_payload = get_http().get_bytes(
            IVV_HOLDINGS_URL,
            raw_snapshot=RawSnapshotContext(
                provider="ishares",
                dataset="ivv_holdings",
                store=store,
                ingest_run_id=holdings_run_id,
                role="post_event_holdings_reconciliation",
                adapter_name="ishares_ivv_holdings_csv",
                adapter_version="1",
                parser_version=IVV_CAPTURE_PARSER_VERSION,
                project_root=root,
            ),
        )
        holdings = parse_ivv_holdings(holdings_payload)
        holdings_as_of = date.fromisoformat(holdings["as_of"])
        if not expected_effective_date <= holdings_as_of <= date.today():
            raise ValueError("IVV reconciliation is not post-effective and current")
        if "FERG" not in holdings["tickers"] or "EA" in holdings["tickers"]:
            raise ValueError("IVV reconciliation does not confirm the EA-to-FERG event")
        holdings_snapshot = _one_snapshot(
            store, holdings_run_id, "post_event_holdings_reconciliation"
        )
        store.record_ingest(
            run_id=holdings_run_id,
            source="ishares:ivv",
            table_name="universe_change_evidence",
            rows_inserted=1,
            status="success",
            started_at=stage_started,
            finished_at=datetime.now(UTC).replace(tzinfo=None),
        )

        mapping = reference["provider"]
        assignment = {
            "ticker": "FERG",
            "effective_start": expected_effective_date,
            "effective_end": date.fromisoformat(mapping["data_end"]),
        }
        raw_prices = fetch_provider_prices(
            mapping["provider"],
            mapping["provider_symbol"],
            mapping["data_start"],
            mapping["data_end"],
            store=store,
            ingest_run_id=price_run_id,
            project_root=root,
        )
        price_rows = relabel_provider_price_rows(raw_prices, mapping, [assignment])
        price_review = _validate_staged_prices(
            price_rows,
            start=date.fromisoformat(mapping["data_start"]),
            end=date.fromisoformat(mapping["data_end"]),
        )
        if price_review["sha256"] != reference["review"]["price_payload_sha256"]:
            raise ValueError("staged FERG price fingerprint changed from reviewed manifest")
        price_snapshot = _one_snapshot(store, price_run_id, "prices:FERG")
        store.record_ingest(
            run_id=price_run_id,
            source="yfinance:reviewed-identity",
            table_name="prices_staging",
            rows_inserted=0,
            rows_rejected=0,
            status="success",
            error=f"staged_for_atomic_activation:{len(price_rows)}",
            started_at=stage_started,
            finished_at=datetime.now(UTC).replace(tzinfo=None),
            subject_type="security",
            subject_id=reference["security_id"],
        )

        fundamental_rows, fundamental_meta = extract_fundamentals(
            "FERG",
            int(reference["cik"]),
            issuer_id=reference["issuer_id"],
            security_id=reference["security_id"],
            snapshot_store=store,
            ingest_run_id=fundamental_run_id,
            snapshot_project_root=root,
            companyfacts_parser_version=COMPANYFACTS_LEGACY_PARSER_VERSION,
        )
        duplicate_groups = _duplicate_fundamental_groups(fundamental_rows)
        if not duplicate_groups:
            raise ValueError(
                "FERG fundamentals no longer match the reviewed pending condition; "
                "a separate parser review is required"
            )
        submissions_snapshot = _one_snapshot(store, fundamental_run_id, "submissions")
        submissions_payload = _read_staged_snapshot(
            store=store,
            project_root=root,
            run_id=fundamental_run_id,
            role="submissions",
            snapshot=submissions_snapshot,
        ).payload
        try:
            submissions_document = json.loads(submissions_payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("staged FERG SEC submissions payload is invalid JSON") from exc
        if _sha256(submissions_document) != reference["review"]["sec_payload_sha256"]:
            raise ValueError("staged FERG SEC identity payload changed from reviewed manifest")
        companyfacts_snapshot = _one_snapshot(store, fundamental_run_id, "companyfacts")
        store.record_ingest(
            run_id=fundamental_run_id,
            source="edgar:constituent-staging",
            table_name="fundamentals_staging",
            rows_inserted=0,
            rows_rejected=len(fundamental_rows),
            status="warning",
            error=(
                "fundamentals_pending: active parser produced "
                f"{len(duplicate_groups)} ambiguous economic-key group(s)"
            ),
            started_at=stage_started,
            finished_at=datetime.now(UTC).replace(tzinfo=None),
            subject_type="issuer",
            subject_id=reference["issuer_id"],
        )
    finally:
        store.close()

    checkpoint_database_for_backup(database)
    backup_time = datetime.now(UTC)
    backup = create_local_backup(
        root,
        database,
        operations_database_path=operations,
        output=(
            root
            / "backups"
            / f"universe-change-{backup_time.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        ),
        application_version=application_version,
        now=backup_time,
    )
    drill = drill_local_backup(backup.path, application_version=application_version)

    readonly = Store(database, read_only=True)
    try:
        base_plan = _build_latest_base_plan(
            store=readonly,
            project_root=root,
            official_release_url=official_release_url,
            expected_effective_date=expected_effective_date,
            expected_member_count=expected_member_count,
        )
    finally:
        readonly.close()
    _compare_event_projections(base_plan.payload["change_rows"], event_projection)

    stage_evidence = {
        "holdings": {
            "run_id": holdings_run_id,
            "snapshot": holdings_snapshot,
            "source_url": IVV_HOLDINGS_URL,
            "review": holdings,
        },
        "prices": {
            "run_id": price_run_id,
            "snapshot": price_snapshot,
            "review": price_review,
            "rows": _json_safe(price_rows),
        },
        "fundamentals": {
            "run_id": fundamental_run_id,
            "companyfacts_snapshot": companyfacts_snapshot,
            "submissions_snapshot": submissions_snapshot,
            "status": "fundamentals_pending",
            "extracted_rows": len(fundamental_rows),
            "duplicate_economic_key_groups": len(duplicate_groups),
            "duplicate_economic_keys_sha256": _sha256(duplicate_groups),
            "parser_rejections": {
                "future_period": int(
                    fundamental_meta.get("rows_rejected_future_period") or 0
                ),
                "unsupported_context": int(
                    fundamental_meta.get("rows_rejected_context") or 0
                ),
                "storage_conflict": int(
                    fundamental_meta.get("rows_rejected_storage_conflict") or 0
                ),
            },
        },
    }
    prediction = _predict_after_state(
        backup=backup,
        base_plan=base_plan.payload,
        reference=reference,
        price_rows=_json_safe(price_rows),
        expected_member_count=expected_member_count,
    )
    activation_run_id = str(uuid4())
    core = {
        "schema_version": ACTIVATION_PLAN_SCHEMA_VERSION,
        "policy_version": ACTIVATION_POLICY_VERSION,
        "operation": "atomic_sp500_constituent_activation",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "actor": normalized_actor,
        "event_id": base_plan.event_id,
        "universe_id": "sp500",
        "activation_run_id": activation_run_id,
        "base_plan_sha256": base_plan.plan_sha256,
        "base_plan": base_plan.payload,
        "source_files": source_hashes,
        "event_rows": event_projection,
        "reference": reference,
        "stage_evidence": stage_evidence,
        "backup": {
            "path": backup.path.relative_to(root).as_posix(),
            "manifest_sha256": backup.manifest_sha256,
            "files": backup.files,
            "bytes": backup.bytes,
            "restore_drill": {
                "files": drill.files,
                "raw_payloads": drill.raw_payloads,
                "replayed_snapshots": drill.replayed_snapshots,
                "hard_failures": drill.hard_failures,
            },
        },
        "paper_state": {
            **base_plan.payload["paper_state"],
            "assurance": "semantic_restore_drill_passed_and_byte_identity_bound",
        },
        "expected_after": prediction,
        "safety": {
            "explicit_confirmation_required": True,
            "paper_mutation": False,
            "broker_used": False,
            "retrospective_fill": False,
            "fundamentals_status": "pending",
            "atomic_database_transaction": True,
        },
    }
    plan_sha256 = _sha256(core)
    envelope = {
        "document_kind": ACTIVATION_PLAN_KIND,
        "plan_sha256": plan_sha256,
        "plan": core,
    }
    plan_path = (
        root
        / "data/reports/universe_changes/plans"
        / f"{plan_sha256}.json"
    )
    publish_text_write_once(plan_path, _canonical_json(envelope), mode=0o600)
    return UniverseChangePreparation(
        plan_path=plan_path,
        plan_sha256=plan_sha256,
        backup=backup,
        restore_files=drill.files,
        restore_raw_payloads=drill.raw_payloads,
        fundamental_status="fundamentals_pending",
        price_rows=len(price_rows),
    )


def activate_sp500_constituent_change(
    *,
    project_root: Path,
    database_path: Path,
    plan_path: Path,
    expected_plan_sha256: str,
    actor: str,
    confirm: bool,
) -> UniverseChangeActivationResult:
    """Recheck one published plan, prove disposable rollback, then commit live."""

    if not confirm:
        raise ValueError("explicit --confirm-activation approval is required")
    _require_sha256(expected_plan_sha256, label="expected plan SHA-256")
    root = Path(project_root).resolve()
    database = _under_root(root, database_path, label="analytical database")
    normalized_actor = _required_actor(actor)
    envelope = _read_plan(root, plan_path, expected_plan_sha256)
    plan = envelope["plan"]
    if plan["actor"] != normalized_actor:
        raise ValueError("activation actor differs from the reviewed plan")
    _verify_source_files(root, plan["source_files"])
    backup = verify_local_backup(root / plan["backup"]["path"])
    if backup.manifest_sha256 != plan["backup"]["manifest_sha256"]:
        raise ValueError("activation backup no longer matches the plan")
    _rebuild_and_compare_base_plan(root, database, plan)
    _verify_live_cas(root, database, plan)
    _prove_disposable_activation(root, backup, plan, expected_plan_sha256)
    _verify_live_cas(root, database, plan)

    store = Store(database)
    try:
        result = _apply_activation_transaction(
            store=store,
            project_root=root,
            plan=plan,
            plan_sha256=expected_plan_sha256,
            actor=normalized_actor,
        )
    finally:
        store.close()

    reopened = Store(database, read_only=True)
    try:
        verify_universe_change_activation_receipts(reopened)
        current = capture_universe_change_state(
            store=reopened,
            universe_id="sp500",
            coverage_through=date.fromisoformat(result.target_coverage_through),
            expected_member_count=int(plan["expected_after"]["member_count"]),
        )
        if current.state_sha256 != result.after_state_sha256:
            raise RuntimeError("accepted activation changed after commit")
    finally:
        reopened.close()
    return result


def verify_universe_change_activation_receipts(store: Store) -> None:
    """Semantically verify every append-only activation receipt at Store startup."""

    rows = store.query(
        "SELECT * FROM universe_constituent_change_activations ORDER BY activated_at, activation_id"
    )
    seen_events: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(str(row["activation_payload_json"]))
        except json.JSONDecodeError as exc:
            raise RuntimeError("universe activation receipt is not JSON") from exc
        if not isinstance(payload, dict) or _canonical_json(payload) != row[
            "activation_payload_json"
        ]:
            raise RuntimeError("universe activation receipt is not canonical")
        if _sha256(payload) != row["activation_payload_sha256"]:
            raise RuntimeError("universe activation receipt payload hash is invalid")
        if payload.get("schema_version") != ACTIVATION_RECEIPT_SCHEMA_VERSION:
            raise RuntimeError("universe activation receipt schema is unsupported")
        event_id = str(row["event_id"])
        if event_id in seen_events:
            raise RuntimeError("universe activation receipt repeats an event")
        seen_events.add(event_id)
        exact = {
            "activation_id": row["activation_id"],
            "event_id": event_id,
            "plan_sha256": row["plan_sha256"],
            "activation_run_id": row["activation_run_id"],
            "fundamental_run_id": row["fundamental_run_id"],
            "price_run_id": row["price_run_id"],
            "source_attestation_id": row["source_attestation_id"],
            "universe_id": row["universe_id"],
            "announcement_date": str(row["announcement_date"]),
            "effective_date": str(row["effective_date"]),
            "prior_coverage_through": str(row["prior_coverage_through"]),
            "target_coverage_through": str(row["target_coverage_through"]),
            "official_detail_snapshot_id": row["official_detail_snapshot_id"],
            "component_snapshot_id": row["component_snapshot_id"],
            "before_member_set_sha256": row["before_member_set_sha256"],
            "after_member_set_sha256": row["after_member_set_sha256"],
            "before_state_sha256": row["before_state_sha256"],
            "after_state_sha256": row["after_state_sha256"],
            "change_rows_sha256": row["change_rows_sha256"],
            "backup_manifest_sha256": row["backup_manifest_sha256"],
            "actor": row["actor"],
            "policy_version": row["policy_version"],
            "status": row["status"],
        }
        if payload.get("receipt") != exact:
            raise RuntimeError("universe activation receipt columns disagree with payload")
        counts = payload.get("counts")
        if not isinstance(counts, dict) or _canonical_json(counts) != row["counts_json"]:
            raise RuntimeError("universe activation receipt counts are invalid")
        if row["schema_version"] != 1 or row["status"] != "accepted":
            raise RuntimeError("universe activation receipt status is unsupported")
        _require_receipt_lineage(store, payload)


def _require_receipt_lineage(store: Store, payload: dict[str, Any]) -> None:
    receipt = payload["receipt"]
    reference = payload.get("reference")
    changes = payload.get("change_rows")
    prices = payload.get("price_rows")
    if not isinstance(reference, dict) or not isinstance(changes, list) or not isinstance(
        prices, list
    ):
        raise RuntimeError("universe activation receipt evidence is incomplete")
    if not store.query(
        "SELECT 1 FROM universe_coverage_attestations WHERE attestation_id = ?",
        (receipt["source_attestation_id"],),
    ):
        raise RuntimeError("universe activation source attestation is missing")
    for run_id, table_name, status in (
        (receipt["fundamental_run_id"], "fundamentals_staging", "warning"),
        (receipt["price_run_id"], "prices_staging", "success"),
        (receipt["activation_run_id"], "universe_constituent_change_activations", "success"),
    ):
        outcomes = store.query(
            "SELECT * FROM ingest_log WHERE run_id = ? AND table_name = ?",
            (run_id, table_name),
        )
        if len(outcomes) != 1 or str(outcomes[0]["status"]) != status:
            raise RuntimeError("universe activation ingest lineage is missing")
    additions = [item for item in changes if item.get("action") == "addition"]
    deletions = [item for item in changes if item.get("action") == "deletion"]
    if len(additions) != 1 or len(deletions) != 1:
        raise RuntimeError("universe activation receipt is not one replacement")
    addition = additions[0]
    deletion = deletions[0]
    added = store.query(
        """
        SELECT security_id, effective_start, effective_end, known_date, source
        FROM universe_membership
        WHERE universe_id = ? AND ticker = ?
          AND effective_start = CAST(? AS DATE)
        """,
        (receipt["universe_id"], addition["ticker"], receipt["effective_date"]),
    )
    if (
        len(added) != 1
        or str(added[0]["security_id"]) != reference["security_id"]
        or str(added[0]["known_date"]) != receipt["announcement_date"]
        or added[0]["effective_end"]
        < date.fromisoformat(receipt["target_coverage_through"]) + timedelta(days=1)
        or receipt["event_id"] not in str(added[0]["source"])
    ):
        raise RuntimeError("universe activation addition lineage is invalid")
    removed = store.query(
        """
        SELECT effective_end, end_known_date, source
        FROM universe_membership
        WHERE universe_id = ? AND ticker = ?
          AND effective_end = CAST(? AS DATE)
        """,
        (receipt["universe_id"], deletion["ticker"], receipt["effective_date"]),
    )
    if (
        len(removed) != 1
        or str(removed[0]["end_known_date"]) != receipt["announcement_date"]
        or receipt["event_id"] not in str(removed[0]["source"])
    ):
        raise RuntimeError("universe activation deletion lineage is invalid")
    stored_prices = store.query(
        """
        SELECT ticker, security_id, provider_symbol, date, open, high, low, close,
               adj_close, volume, dividends, split_ratio, actions_complete,
               close_split_adjusted, split_normalization_factor,
               split_normalization_through, source
        FROM prices
        WHERE ticker = ? AND date >= CAST(? AS DATE) AND date < CAST(? AS DATE)
        ORDER BY date
        """,
        (
            addition["ticker"],
            receipt["effective_date"],
            (date.fromisoformat(receipt["target_coverage_through"]) + timedelta(days=1)),
        ),
    )
    expected_price_keys = {
        (
            str(row["ticker"]),
            str(row["security_id"]),
            str(row["provider_symbol"]),
            str(row["date"]),
        )
        for row in prices
    }
    observed_price_keys = {
        (
            str(row["ticker"]),
            str(row["security_id"]),
            str(row["provider_symbol"]),
            str(row["date"]),
        )
        for row in stored_prices
    }
    if (
        observed_price_keys != expected_price_keys
        or len(stored_prices) != len(expected_price_keys)
        or any(
            row.get("close") is None
            or float(row["close"]) <= 0
            or row.get("actions_complete") is not True
            or row.get("close_split_adjusted") is not True
            or row.get("split_normalization_factor") is None
            or float(row["split_normalization_factor"]) <= 0
            or str(row.get("source"))
            not in {"yfinance", "yfinance:ohlc-envelope-v1"}
            for row in stored_prices
        )
    ):
        raise RuntimeError("universe activation price identity coverage is invalid")


def _build_latest_base_plan(
    *,
    store: Store,
    project_root: Path,
    official_release_url: str,
    expected_effective_date: date,
    expected_member_count: int,
):
    rows = store.query(
        """
        SELECT * FROM universe_coverage_attestations
        WHERE universe_id = 'sp500' AND status = 'blocked_review_required'
          AND candidate_releases_json LIKE ?
        ORDER BY checked_at DESC LIMIT 1
        """,
        (f"%{official_release_url}%",),
    )
    if len(rows) != 1:
        raise ValueError("one current blocked universe attestation is required")
    attestation = rows[0]
    run_id = str(attestation["run_id"])
    linked = store.query(
        """
        SELECT linked.role, snapshot.*
        FROM ingest_raw_snapshots AS linked
        JOIN raw_snapshots AS snapshot USING (snapshot_id)
        WHERE linked.run_id = ? ORDER BY linked.role
        """,
        (run_id,),
    )
    by_role = {str(row["role"]): row for row in linked}
    archive_roles = sorted(
        role for role in by_role if role.startswith("official_release_archive_page_")
    )
    detail_roles = sorted(role for role in by_role if role.startswith("candidate_release_detail_"))
    if "independent_component_snapshot" not in by_role:
        raise ValueError("blocked attestation has no independent component snapshot")

    def expectation(role: str, source_url: str) -> RawEvidenceExpectation:
        row = by_role[role]
        return RawEvidenceExpectation(
            run_id=run_id,
            role=role,
            snapshot_id=str(row["snapshot_id"]),
            source_url=source_url,
            provider=str(row["provider"]),
            dataset=str(row["dataset"]),
            artifact_kind=str(row["artifact_kind"]),
            parser_version=str(row["parser_version"]),
            request_fingerprint=str(row["request_fingerprint"]),
            adapter_name=str(row["adapter_name"]),
            adapter_version=str(row["adapter_version"]),
        )

    archives = tuple(
        expectation(role, sp500_archive_page_url(position))
        for position, role in enumerate(archive_roles)
    )
    details = tuple(expectation(role, official_release_url) for role in detail_roles)
    component = expectation("independent_component_snapshot", COMPONENT_SNAPSHOT_URL)
    return build_universe_change_plan(
        store=store,
        project_root=project_root,
        universe_id="sp500",
        source_attestation_id=str(attestation["attestation_id"]),
        official_release_url=official_release_url,
        archive_evidence=archives,
        detail_evidence=details,
        component_evidence=component,
        expected_effective_date=expected_effective_date,
        expected_member_count=expected_member_count,
    )


def _apply_activation_transaction(
    *,
    store: Store,
    project_root: Path,
    plan: dict[str, Any],
    plan_sha256: str,
    actor: str,
) -> UniverseChangeActivationResult:
    del project_root
    base = plan["base_plan"]
    prior = date.fromisoformat(base["prior_coverage_through"])
    target = date.fromisoformat(base["requested_coverage_through"])
    before = capture_universe_change_state(
        store=store,
        universe_id="sp500",
        coverage_through=prior,
        expected_member_count=int(plan["expected_after"]["member_count"]),
    )
    if before.state_sha256 != base["before_state_sha256"]:
        raise ValueError("universe activation CAS rejected changed reference state")
    if store.query(
        "SELECT 1 FROM universe_constituent_change_activations WHERE event_id = ?",
        (plan["event_id"],),
    ):
        raise ValueError("universe constituent event is already activated")

    store.execute("BEGIN TRANSACTION")
    try:
        counts = _mutate_reference_state(
            store=store,
            base_plan=base,
            reference=plan["reference"],
            price_rows=plan["stage_evidence"]["prices"]["rows"],
        )
        after = capture_universe_change_state(
            store=store,
            universe_id="sp500",
            coverage_through=target,
            expected_member_count=int(plan["expected_after"]["member_count"]),
        )
        expected_after = plan["expected_after"]
        if (
            after.state_sha256 != expected_after["state_sha256"]
            or after.member_set_sha256 != expected_after["member_set_sha256"]
            or counts != expected_after["counts"]
        ):
            raise ValueError("atomic activation result differs from disposable prediction")
        activated_at = datetime.now(UTC).replace(tzinfo=None)
        activation_id = f"uca-event-{uuid4().hex}"
        evidence_by_role = {item["role"]: item for item in base["evidence"]}
        receipt_projection = {
            "activation_id": activation_id,
            "event_id": plan["event_id"],
            "plan_sha256": plan_sha256,
            "activation_run_id": plan["activation_run_id"],
            "fundamental_run_id": plan["stage_evidence"]["fundamentals"]["run_id"],
            "price_run_id": plan["stage_evidence"]["prices"]["run_id"],
            "source_attestation_id": base["source_attestation_id"],
            "universe_id": "sp500",
            "announcement_date": base["announcement_date"],
            "effective_date": base["effective_date"],
            "prior_coverage_through": base["prior_coverage_through"],
            "target_coverage_through": base["requested_coverage_through"],
            "official_detail_snapshot_id": evidence_by_role[
                "candidate_release_detail_000"
            ]["snapshot_id"],
            "component_snapshot_id": evidence_by_role[
                "independent_component_snapshot"
            ]["snapshot_id"],
            "before_member_set_sha256": base["before_member_set_sha256"],
            "after_member_set_sha256": after.member_set_sha256,
            "before_state_sha256": base["before_state_sha256"],
            "after_state_sha256": after.state_sha256,
            "change_rows_sha256": base["change_rows_sha256"],
            "backup_manifest_sha256": plan["backup"]["manifest_sha256"],
            "actor": actor,
            "policy_version": ACTIVATION_POLICY_VERSION,
            "status": "accepted",
        }
        activation_payload = {
            "schema_version": ACTIVATION_RECEIPT_SCHEMA_VERSION,
            "receipt": receipt_projection,
            "change_rows": base["change_rows"],
            "reference": plan["reference"],
            "price_rows": plan["stage_evidence"]["prices"]["rows"],
            "fundamentals": plan["stage_evidence"]["fundamentals"],
            "post_event_reconciliation": plan["stage_evidence"]["holdings"],
            "counts": counts,
            "safety": {
                "paper_mutated": False,
                "broker_used": False,
                "retrospective_fill": False,
                "fundamentals_status": "pending",
            },
        }
        encoded_payload = _canonical_json(activation_payload)
        encoded_counts = _canonical_json(counts)
        store.execute(
            """
            INSERT INTO ingest_log
            (run_id, source, table_name, rows_inserted, rows_rejected,
             started_at, finished_at, status, error)
            VALUES (?, ?, 'universe_constituent_change_activations', 1, 0,
                    ?, ?, 'success', NULL)
            """,
            (
                plan["activation_run_id"],
                f"governed-event:{plan['event_id']}",
                activated_at,
                activated_at,
            ),
        )
        store.execute(
            """
            INSERT INTO universe_constituent_change_activations
            (activation_id, event_id, plan_sha256, activation_payload_sha256,
             activation_run_id, fundamental_run_id, price_run_id,
             source_attestation_id, schema_version, universe_id,
             announcement_date, effective_date, prior_coverage_through,
             target_coverage_through, official_detail_snapshot_id,
             component_snapshot_id, before_member_set_sha256,
             after_member_set_sha256, before_state_sha256, after_state_sha256,
             change_rows_sha256, activation_payload_json,
             backup_manifest_sha256, actor, policy_version, counts_json,
             activated_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, CAST(? AS DATE),
                    CAST(? AS DATE), CAST(? AS DATE), CAST(? AS DATE), ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted')
            """,
            (
                activation_id,
                plan["event_id"],
                plan_sha256,
                _sha256(activation_payload),
                plan["activation_run_id"],
                plan["stage_evidence"]["fundamentals"]["run_id"],
                plan["stage_evidence"]["prices"]["run_id"],
                base["source_attestation_id"],
                "sp500",
                base["announcement_date"],
                base["effective_date"],
                base["prior_coverage_through"],
                base["requested_coverage_through"],
                receipt_projection["official_detail_snapshot_id"],
                receipt_projection["component_snapshot_id"],
                base["before_member_set_sha256"],
                after.member_set_sha256,
                base["before_state_sha256"],
                after.state_sha256,
                base["change_rows_sha256"],
                encoded_payload,
                plan["backup"]["manifest_sha256"],
                actor,
                ACTIVATION_POLICY_VERSION,
                encoded_counts,
                activated_at,
            ),
        )
        store.execute("COMMIT")
    except Exception:
        store.execute("ROLLBACK")
        raise
    return UniverseChangeActivationResult(
        activation_id=activation_id,
        event_id=plan["event_id"],
        plan_sha256=plan_sha256,
        prior_coverage_through=prior.isoformat(),
        target_coverage_through=target.isoformat(),
        after_state_sha256=after.state_sha256,
        counts=counts,
        disposable_rollback_proved=True,
    )


def _mutate_reference_state(
    *,
    store: Store,
    base_plan: dict[str, Any],
    reference: dict[str, Any],
    price_rows: list[dict[str, Any]],
) -> dict[str, int]:
    changes = base_plan["change_rows"]
    additions = [row for row in changes if row["action"] == "addition"]
    deletions = [row for row in changes if row["action"] == "deletion"]
    if len(additions) != 1 or len(deletions) != 1:
        raise ValueError("atomic activation supports one addition and one deletion")
    addition, deletion = additions[0], deletions[0]
    prior = date.fromisoformat(base_plan["prior_coverage_through"])
    target = date.fromisoformat(base_plan["requested_coverage_through"])
    effective = date.fromisoformat(base_plan["effective_date"])
    announcement = date.fromisoformat(base_plan["announcement_date"])
    old_boundary = prior + timedelta(days=1)
    new_boundary = target + timedelta(days=1)
    if date.fromisoformat(reference["provider"]["data_end"]) != new_boundary:
        raise ValueError("entering reference interval does not reach target coverage")
    before = capture_universe_change_state(
        store=store,
        universe_id="sp500",
        coverage_through=prior,
        expected_member_count=int(base_plan["before_state"]["member_count"]),
    )
    members = before.payload["members"]
    deleted = [row for row in members if row["ticker"] == deletion["ticker"]]
    if len(deleted) != 1:
        raise ValueError("official deletion no longer identifies one current member")
    deleted_security = deleted[0]["security_id"]
    unchanged_security = sorted(
        row["security_id"] for row in members if row["ticker"] != deletion["ticker"]
    )
    owners = {row["security_id"]: row["issuer_id"] for row in before.payload["security_issuers"]}
    unchanged_issuers = sorted({owners[security] for security in unchanged_security})
    deleted_issuer = owners[deleted_security]
    marker = f"|constituent-event:{base_plan['event_id']}"
    if store.query(
        """
        SELECT security_id AS id FROM security_master WHERE security_id = ?
        UNION ALL SELECT issuer_id FROM issuer_master WHERE issuer_id = ?
        UNION ALL SELECT ticker FROM universe_membership
          WHERE universe_id = 'sp500' AND ticker = ? AND effective_start >= CAST(? AS DATE)
        """,
        (
            reference["security_id"],
            reference["issuer_id"],
            addition["ticker"],
            effective.isoformat(),
        ),
    ):
        raise ValueError("entering reference already exists or activation is partial")

    counts: dict[str, int] = {}

    def update_count(name: str, sql: str, params: tuple[Any, ...]) -> None:
        counts[name] = int(store.execute(sql, params).fetchone()[0])

    update_count(
        "membership_extended",
        """
        UPDATE universe_membership
        SET effective_end = CAST(? AS DATE), end_known_date = CAST(? AS DATE),
            source = source || ?, fetched_at = now()
        WHERE universe_id = 'sp500' AND ticker <> ?
          AND effective_end = CAST(? AS DATE) AND end_known_date = CAST(? AS DATE)
        """,
        (
            new_boundary.isoformat(),
            target.isoformat(),
            marker,
            deletion["ticker"],
            old_boundary.isoformat(),
            prior.isoformat(),
        ),
    )
    update_count(
        "membership_deleted",
        """
        UPDATE universe_membership
        SET effective_end = CAST(? AS DATE), end_known_date = CAST(? AS DATE),
            source = source || ?, fetched_at = now()
        WHERE universe_id = 'sp500' AND ticker = ? AND security_id = ?
          AND effective_end = CAST(? AS DATE) AND end_known_date = CAST(? AS DATE)
        """,
        (
            effective.isoformat(),
            announcement.isoformat(),
            marker,
            deletion["ticker"],
            deleted_security,
            old_boundary.isoformat(),
            prior.isoformat(),
        ),
    )
    update_count(
        "identity_extended",
        """
        UPDATE security_identity_assignments
        SET effective_end = CAST(? AS DATE), source = source || ?, fetched_at = now()
        WHERE universe_id = 'sp500' AND ticker <> ?
          AND effective_end = CAST(? AS DATE)
        """,
        (new_boundary.isoformat(), marker, deletion["ticker"], old_boundary.isoformat()),
    )
    update_count(
        "identity_deleted",
        """
        UPDATE security_identity_assignments
        SET effective_end = CAST(? AS DATE), source = source || ?, fetched_at = now()
        WHERE universe_id = 'sp500' AND ticker = ? AND security_id = ?
          AND effective_end = CAST(? AS DATE)
        """,
        (
            effective.isoformat(),
            marker,
            deletion["ticker"],
            deleted_security,
            old_boundary.isoformat(),
        ),
    )
    counts["owner_extended"] = _extend_ids(
        store,
        table="security_issuer_assignments",
        id_column="security_id",
        ids=unchanged_security,
        end_column="effective_end",
        old_end=old_boundary,
        new_end=new_boundary,
    )
    counts["owner_deleted"] = _extend_ids(
        store,
        table="security_issuer_assignments",
        id_column="security_id",
        ids=[deleted_security],
        end_column="effective_end",
        old_end=old_boundary,
        new_end=effective,
    )
    counts["cik_extended"] = _extend_ids(
        store,
        table="issuer_cik_history",
        id_column="issuer_id",
        ids=unchanged_issuers,
        end_column="effective_end",
        old_end=old_boundary,
        new_end=new_boundary,
    )
    counts["cik_deleted"] = (
        0
        if deleted_issuer in unchanged_issuers
        else _extend_ids(
            store,
            table="issuer_cik_history",
            id_column="issuer_id",
            ids=[deleted_issuer],
            end_column="effective_end",
            old_end=old_boundary,
            new_end=effective,
        )
    )
    counts["provider_extended"] = _extend_ids(
        store,
        table="provider_symbol_history",
        id_column="security_id",
        ids=unchanged_security,
        end_column="data_end",
        old_end=old_boundary,
        new_end=new_boundary,
    )
    counts["provider_deleted"] = _extend_ids(
        store,
        table="provider_symbol_history",
        id_column="security_id",
        ids=[deleted_security],
        end_column="data_end",
        old_end=old_boundary,
        new_end=effective,
    )
    expected_counts = {
        "membership_extended": len(members) - 1,
        "membership_deleted": 1,
        "identity_extended": len(members) - 1,
        "identity_deleted": 1,
        "owner_extended": len(unchanged_security),
        "owner_deleted": 1,
        "cik_extended": len(unchanged_issuers),
        "cik_deleted": 0 if deleted_issuer in unchanged_issuers else 1,
        "provider_extended": sum(
            row["security_id"] in unchanged_security
            for row in before.payload["provider_symbols"]
        ),
        "provider_deleted": sum(
            row["security_id"] == deleted_security
            for row in before.payload["provider_symbols"]
        ),
    }
    if counts != expected_counts:
        raise ValueError(f"constituent interval update count mismatch: {counts}")

    issuer = reference["issuer"]
    provider = reference["provider"]
    security_id = reference["security_id"]
    issuer_id = reference["issuer_id"]
    event_source = base_plan["official_release_url"] + marker
    store.execute(
        """
        INSERT INTO universe_membership
        (universe_id, ticker, security_id, effective_start, effective_end,
         known_date, end_known_date, source, fetched_at)
        VALUES ('sp500', ?, ?, CAST(? AS DATE), CAST(? AS DATE),
                CAST(? AS DATE), CAST(? AS DATE), ?, now())
        """,
        (
            addition["ticker"],
            security_id,
            effective.isoformat(),
            new_boundary.isoformat(),
            announcement.isoformat(),
            target.isoformat(),
            event_source,
        ),
    )
    store.execute(
        """
        INSERT INTO security_master
        (security_id, canonical_ticker, security_type, identity_status, source,
         created_at, last_updated)
        VALUES (?, ?, 'common_stock', 'bounded_ticker', ?, now(), now())
        """,
        (security_id, addition["ticker"], event_source),
    )
    store.execute(
        """
        INSERT INTO security_identity_assignments
        (universe_id, ticker, effective_start, effective_end, security_id,
         known_date, identity_status, source, fetched_at)
        VALUES ('sp500', ?, CAST(? AS DATE), CAST(? AS DATE), ?,
                CAST(? AS DATE), 'bounded_ticker', ?, now())
        """,
        (
            addition["ticker"],
            effective.isoformat(),
            new_boundary.isoformat(),
            security_id,
            announcement.isoformat(),
            event_source,
        ),
    )
    store.execute(
        """
        INSERT INTO issuer_master
        (issuer_id, canonical_name, canonical_ticker, source, created_at, last_updated)
        VALUES (?, ?, ?, ?, now(), now())
        """,
        (
            issuer_id,
            issuer["canonical_name"],
            issuer["canonical_ticker"],
            issuer["source"],
        ),
    )
    store.execute(
        """
        INSERT INTO issuer_cik_history
        (issuer_id, cik, effective_start, effective_end, verified_date, source, fetched_at)
        VALUES (?, ?, CAST(? AS DATE), CAST(? AS DATE), CAST(? AS DATE), ?, now())
        """,
        (
            issuer_id,
            reference["cik"],
            issuer["effective_start"],
            issuer["effective_end"],
            issuer["verified_date"],
            issuer["source"],
        ),
    )
    store.execute(
        """
        INSERT INTO security_issuer_assignments
        (security_id, issuer_id, effective_start, effective_end, verified_date,
         source, fetched_at)
        VALUES (?, ?, CAST(? AS DATE), CAST(? AS DATE), CAST(? AS DATE), ?, now())
        """,
        (
            security_id,
            issuer_id,
            reference["owner"]["effective_start"],
            reference["owner"]["effective_end"],
            reference["owner"]["verified_date"],
            reference["owner"]["source"],
        ),
    )
    store.execute(
        """
        INSERT INTO provider_symbol_history
        (provider, provider_symbol, security_id, data_start, data_end,
         mapping_status, verified_date, source, fetched_at)
        VALUES (?, ?, ?, CAST(? AS DATE), CAST(? AS DATE), 'verified',
                CAST(? AS DATE), ?, now())
        """,
        (
            provider["provider"],
            provider["provider_symbol"],
            security_id,
            provider["data_start"],
            provider["data_end"],
            provider["verified_date"],
            provider["source"],
        ),
    )
    store.execute(
        """
        INSERT INTO securities
        (ticker, cik, name, is_active, first_seen, last_updated)
        VALUES (?, ?, ?, TRUE, now(), now())
        ON CONFLICT (ticker) DO NOTHING
        """,
        (addition["ticker"], int(reference["cik"]), issuer["canonical_name"]),
    )
    for row in price_rows:
        store.execute(
            """
            INSERT INTO prices
            (ticker, security_id, provider_symbol, date, open, high, low, close,
             adj_close, volume, dividends, split_ratio, actions_complete,
             close_split_adjusted, split_normalization_factor,
             split_normalization_through, source, fetched_at)
            VALUES (?, ?, ?, CAST(? AS DATE), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    CAST(? AS DATE), ?, now())
            """,
            tuple(row.get(field) for field in (
                "ticker", "security_id", "provider_symbol", "date", "open", "high",
                "low", "close", "adj_close", "volume", "dividends", "split_ratio",
                "actions_complete", "close_split_adjusted", "split_normalization_factor",
                "split_normalization_through", "source",
            )),
        )
    counts.update(
        {
            "membership_added": 1,
            "identity_added": 1,
            "security_master_added": 1,
            "issuer_added": 1,
            "cik_added": 1,
            "owner_added": 1,
            "provider_added": 1,
            "price_rows_added": len(price_rows),
            "fundamental_rows_added": 0,
        }
    )
    return counts


def _extend_ids(
    store: Store,
    *,
    table: str,
    id_column: str,
    ids: list[str],
    end_column: str,
    old_end: date,
    new_end: date,
) -> int:
    allowed = {
        ("security_issuer_assignments", "security_id", "effective_end"),
        ("issuer_cik_history", "issuer_id", "effective_end"),
        ("provider_symbol_history", "security_id", "data_end"),
    }
    if (table, id_column, end_column) not in allowed or not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    return int(
        store.execute(
            f"""
            UPDATE {table} SET {end_column} = CAST(? AS DATE), fetched_at = now()
            WHERE {id_column} IN ({placeholders}) AND {end_column} = CAST(? AS DATE)
            """,
            (new_end.isoformat(), *ids, old_end.isoformat()),
        ).fetchone()[0]
    )


def _predict_after_state(
    *,
    backup: BackupResult,
    base_plan: dict[str, Any],
    reference: dict[str, Any],
    price_rows: list[dict[str, Any]],
    expected_member_count: int,
) -> dict[str, Any]:
    manifest = json.loads((backup.path / "manifest.json").read_text(encoding="utf-8"))
    source_db = backup.path / manifest["database_file"]
    with tempfile.TemporaryDirectory(prefix="aios-universe-predict-") as temporary:
        database = Path(temporary) / "aios.duckdb"
        shutil.copy2(source_db, database)
        store = Store(database)
        try:
            prior = date.fromisoformat(base_plan["prior_coverage_through"])
            before = capture_universe_change_state(
                store=store,
                universe_id="sp500",
                coverage_through=prior,
                expected_member_count=expected_member_count,
            )
            store.execute("BEGIN TRANSACTION")
            try:
                counts = _mutate_reference_state(
                    store=store,
                    base_plan=base_plan,
                    reference=reference,
                    price_rows=price_rows,
                )
                after = capture_universe_change_state(
                    store=store,
                    universe_id="sp500",
                    coverage_through=date.fromisoformat(
                        base_plan["requested_coverage_through"]
                    ),
                    expected_member_count=expected_member_count,
                )
            finally:
                store.execute("ROLLBACK")
            restored = capture_universe_change_state(
                store=store,
                universe_id="sp500",
                coverage_through=prior,
                expected_member_count=expected_member_count,
            )
            if restored.state_sha256 != before.state_sha256:
                raise RuntimeError("disposable activation rollback did not restore state")
        finally:
            store.close()
    return {
        "member_count": after.member_count,
        "member_set_sha256": after.member_set_sha256,
        "security_set_sha256": after.security_set_sha256,
        "state_sha256": after.state_sha256,
        "counts": counts,
        "rollback_proved": True,
    }


def _prove_disposable_activation(
    root: Path,
    backup: BackupResult,
    plan: dict[str, Any],
    plan_sha256: str,
) -> None:
    manifest = json.loads((backup.path / "manifest.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="aios-universe-activation-") as temporary:
        scratch = Path(temporary)
        (scratch / "data").mkdir()
        shutil.copy2(
            backup.path / manifest["database_file"],
            scratch / "data/aios.duckdb",
        )
        raw = backup.path / "raw"
        if raw.is_dir():
            shutil.copytree(raw, scratch / "data/raw")
        paper = backup.path / "paper"
        if paper.is_dir():
            shutil.copytree(paper, scratch / "data/paper")
        store = Store(scratch / "data/aios.duckdb")
        try:
            result = _apply_activation_transaction(
                store=store,
                project_root=scratch,
                plan=plan,
                plan_sha256=plan_sha256,
                actor=plan["actor"],
            )
            if result.after_state_sha256 != plan["expected_after"]["state_sha256"]:
                raise RuntimeError("disposable activation produced another state")
        finally:
            store.close()
        verified = Store(scratch / "data/aios.duckdb", read_only=True)
        verified.close()


def _verify_live_cas(root: Path, database: Path, plan: dict[str, Any]) -> None:
    store = Store(database, read_only=True)
    try:
        base = plan["base_plan"]
        current = capture_universe_change_state(
            store=store,
            universe_id="sp500",
            coverage_through=date.fromisoformat(base["prior_coverage_through"]),
            expected_member_count=int(plan["expected_after"]["member_count"]),
        )
        if current.state_sha256 != base["before_state_sha256"]:
            raise ValueError("activation CAS rejected changed universe references")
        if capture_paper_tree_state(root)["tree_sha256"] != plan["paper_state"][
            "tree_sha256"
        ]:
            raise ValueError("activation CAS rejected changed paper state")
        _verify_stage_evidence(store, root, plan)
    finally:
        store.close()


def _verify_stage_evidence(store: Store, root: Path, plan: dict[str, Any]) -> None:
    stage = plan["stage_evidence"]
    holdings = stage["holdings"]
    snapshot = holdings["snapshot"]
    verified = read_verified_raw_snapshot(
        store=store,
        expected_run_id=holdings["run_id"],
        expected_role="post_event_holdings_reconciliation",
        snapshot_id=snapshot["snapshot_id"],
        expected_provider="ishares",
        expected_dataset="ivv_holdings",
        expected_artifact_kind="exact_response",
        expected_parser_version=IVV_CAPTURE_PARSER_VERSION,
        expected_request_fingerprint=canonical_request_fingerprint(
            {"method": "GET", "url": IVV_HOLDINGS_URL}
        ),
        expected_adapter_name="ishares_ivv_holdings_csv",
        expected_adapter_version="1",
        require_parsed_evidence=False,
        project_root=root,
    )
    if parse_ivv_holdings(verified.payload) != holdings["review"]:
        raise ValueError("post-event IVV evidence changed from the plan")
    for kind, run_id, table_name, status in (
        ("prices", stage["prices"]["run_id"], "prices_staging", "success"),
        (
            "fundamentals",
            stage["fundamentals"]["run_id"],
            "fundamentals_staging",
            "warning",
        ),
    ):
        rows = store.query(
            "SELECT * FROM ingest_log WHERE run_id = ? AND table_name = ?",
            (run_id, table_name),
        )
        if len(rows) != 1 or rows[0]["status"] != status:
            raise ValueError(f"{kind} staging outcome no longer matches the plan")
    for evidence in (
        stage["prices"]["snapshot"],
        stage["fundamentals"]["companyfacts_snapshot"],
        stage["fundamentals"]["submissions_snapshot"],
    ):
        rows = store.query(
            "SELECT payload_sha256 FROM raw_snapshots WHERE snapshot_id = ?",
            (evidence["snapshot_id"],),
        )
        if len(rows) != 1 or rows[0]["payload_sha256"] != evidence["payload_sha256"]:
            raise ValueError("staged provider evidence no longer matches the plan")


def _rebuild_and_compare_base_plan(root: Path, database: Path, plan: dict[str, Any]) -> None:
    base = plan["base_plan"]
    store = Store(database, read_only=True)
    try:
        rebuilt = _build_latest_base_plan(
            store=store,
            project_root=root,
            official_release_url=base["official_release_url"],
            expected_effective_date=date.fromisoformat(base["effective_date"]),
            expected_member_count=int(plan["expected_after"]["member_count"]),
        )
    finally:
        store.close()
    if rebuilt.plan_sha256 != plan["base_plan_sha256"] or rebuilt.payload != base:
        raise ValueError("official/source plan changed after operator review")


def _source_files(root: Path, *, event_path: Path, reference_stem: Path) -> dict[str, Path]:
    stem = _under_root(root, reference_stem, label="reference stem", require_file=False)
    files = {
        "event": _under_root(root, event_path, label="event manifest"),
        "issuer_ciks": Path(f"{stem}_issuer_ciks.csv"),
        "security_issuers": Path(f"{stem}_security_issuers.csv"),
        "provider_symbols": Path(f"{stem}_provider_symbols.csv"),
        "review": Path(f"{stem}_review.csv"),
    }
    for label, path in files.items():
        _under_root(root, path, label=label)
    return files


def _load_reference_projection(
    files: dict[str, Path], *, expected_effective_date: date
) -> dict[str, Any]:
    review_path = files["review"]
    suffix = "_review.csv"
    if not review_path.name.endswith(suffix):
        raise ValueError("reference review filename is not canonical")
    stem = review_path.with_name(review_path.name[: -len(suffix)])
    batch = merge_reference_batch_files([stem])
    if (
        batch["accepted"] != 1
        or batch["rejected"] != 0
        or len(batch["issuer_rows"]) != 1
        or len(batch["owner_rows"]) != 1
        or len(batch["provider_rows"]) != 1
        or len(batch["review_rows"]) != 1
    ):
        raise ValueError("FERG activation requires one complete accepted reference")
    issuer = _json_safe(batch["issuer_rows"][0])
    owner = _json_safe(batch["owner_rows"][0])
    provider = _json_safe(batch["provider_rows"][0])
    review = _json_safe(batch["review_rows"][0])
    if (
        review["ticker"] != "FERG"
        or issuer["canonical_ticker"] != "FERG"
        or provider["provider_symbol"] != "FERG"
        or date.fromisoformat(issuer["effective_start"]) != expected_effective_date
        or date.fromisoformat(owner["effective_start"]) != expected_effective_date
        or date.fromisoformat(provider["data_start"]) != expected_effective_date
        or review["review_status"] != "accepted"
    ):
        raise ValueError("reviewed entering reference does not describe FERG at the event")
    if not _SHA256.fullmatch(review["sec_payload_sha256"]) or not _SHA256.fullmatch(
        review["price_payload_sha256"]
    ):
        raise ValueError("reviewed entering reference lacks source fingerprints")
    return {
        "ticker": "FERG",
        "security_id": owner["security_id"],
        "issuer_id": owner["issuer_id"],
        "cik": issuer["cik"],
        "issuer": issuer,
        "owner": owner,
        "provider": provider,
        "review": review,
    }


def _validate_event_batch(
    events: list[Any], *, official_release_url: str, expected_effective_date: date
) -> list[dict[str, str]]:
    projection = [
        {
            "effective_date": event.effective_date.isoformat(),
            "action": event.action,
            "ticker": event.ticker,
            "known_date": event.known_date.isoformat(),
            "source": event.source,
        }
        for event in events
    ]
    expected = {("EA", "deletion"), ("FERG", "addition")}
    if (
        {(row["ticker"], row["action"]) for row in projection} != expected
        or len(projection) != 2
        or any(row["effective_date"] != expected_effective_date.isoformat() for row in projection)
        or any(row["source"] != official_release_url for row in projection)
    ):
        raise ValueError("event manifest must be the exact official EA-to-FERG replacement")
    return sorted(projection, key=lambda row: (row["action"], row["ticker"]))


def _compare_event_projections(
    official_rows: list[dict[str, str]], reviewed_rows: list[dict[str, str]]
) -> None:
    official = {
        (row["ticker"], row["action"], row["effective_date"]) for row in official_rows
    }
    reviewed = {
        (row["ticker"], row["action"], row["effective_date"]) for row in reviewed_rows
    }
    if official != reviewed:
        raise ValueError("reviewed event CSV disagrees with exact official announcement")


def _validate_staged_prices(
    rows: list[dict[str, Any]], *, start: date, end: date
) -> dict[str, Any]:
    dates = [date.fromisoformat(str(row["date"])) for row in rows]
    expected = us_equity_sessions(start, min(end, date.today()))
    if dates != expected or not rows:
        raise ValueError("staged FERG prices do not cover every completed event session")
    if any(
        row.get("ticker") != "FERG"
        or not row.get("security_id")
        or row.get("source") != "yfinance"
        or row.get("close") is None
        or float(row["close"]) <= 0
        for row in rows
    ):
        raise ValueError("staged FERG prices violate identity or close requirements")
    fingerprint = [
        {
            "date": day.isoformat(),
            "close": row.get("close"),
            "adj_close": row.get("adj_close"),
            "volume": row.get("volume"),
        }
        for day, row in zip(dates, rows, strict=True)
    ]
    return {
        "rows": len(rows),
        "first_date": dates[0].isoformat(),
        "last_date": dates[-1].isoformat(),
        "sha256": _sha256(fingerprint),
    }


def _duplicate_fundamental_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], int] = {}
    for row in rows:
        key = (
            str(row["ticker"]),
            str(row["period_end"]),
            str(row["as_of_date"]),
            str(row["metric"]),
        )
        grouped[key] = grouped.get(key, 0) + 1
    return [
        {
            "ticker": key[0],
            "period_end": key[1],
            "as_of_date": key[2],
            "metric": key[3],
            "rows": count,
        }
        for key, count in sorted(grouped.items())
        if count > 1
    ]


def _one_snapshot(store: Store, run_id: str, role: str) -> dict[str, Any]:
    rows = store.query(
        """
        SELECT snapshot.snapshot_id, snapshot.payload_sha256,
               snapshot.request_fingerprint, snapshot.provider, snapshot.dataset,
               snapshot.artifact_kind, snapshot.parser_version,
               snapshot.adapter_name, snapshot.adapter_version
        FROM ingest_raw_snapshots AS linked
        JOIN raw_snapshots AS snapshot USING (snapshot_id)
        WHERE linked.run_id = ? AND linked.role = ?
        """,
        (run_id, role),
    )
    if len(rows) != 1:
        raise ValueError(f"staging run requires one {role!r} raw snapshot")
    return _json_safe(rows[0])


def _read_staged_snapshot(
    *,
    store: Store,
    project_root: Path,
    run_id: str,
    role: str,
    snapshot: dict[str, Any],
):
    return read_verified_raw_snapshot(
        store=store,
        expected_run_id=run_id,
        expected_role=role,
        snapshot_id=snapshot["snapshot_id"],
        expected_provider=snapshot["provider"],
        expected_dataset=snapshot["dataset"],
        expected_artifact_kind=snapshot["artifact_kind"],
        expected_parser_version=snapshot["parser_version"],
        expected_request_fingerprint=snapshot["request_fingerprint"],
        expected_adapter_name=snapshot["adapter_name"],
        expected_adapter_version=snapshot["adapter_version"],
        project_root=project_root,
    )


def _verify_source_files(root: Path, sources: dict[str, dict[str, str]]) -> None:
    for label, evidence in sources.items():
        path = _under_root(root, Path(evidence["path"]), label=label)
        if _file_sha256(path) != evidence["sha256"]:
            raise ValueError(f"reviewed {label} file changed after plan publication")


def _read_plan(root: Path, path: Path, expected_sha256: str) -> dict[str, Any]:
    candidate = _under_root(root, path, label="activation plan")
    if candidate.is_symlink() or candidate.stat().st_nlink != 1:
        raise ValueError("activation plan must be one regular unaliased file")
    if candidate.stat().st_size > _MAX_PLAN_BYTES:
        raise ValueError("activation plan exceeds the governed byte limit")
    raw = candidate.read_text(encoding="utf-8")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("activation plan is invalid JSON") from exc
    if not isinstance(envelope, dict) or _canonical_json(envelope) != raw:
        raise ValueError("activation plan is not canonical JSON")
    if set(envelope) != {"document_kind", "plan_sha256", "plan"}:
        raise ValueError("activation plan envelope has an unsupported shape")
    plan = envelope["plan"]
    if (
        envelope["document_kind"] != ACTIVATION_PLAN_KIND
        or envelope["plan_sha256"] != expected_sha256
        or _sha256(plan) != expected_sha256
        or plan.get("schema_version") != ACTIVATION_PLAN_SCHEMA_VERSION
        or plan.get("policy_version") != ACTIVATION_POLICY_VERSION
        or plan.get("operation") != "atomic_sp500_constituent_activation"
        or plan.get("safety", {}).get("paper_mutation") is not False
        or plan.get("safety", {}).get("broker_used") is not False
        or plan.get("safety", {}).get("retrospective_fill") is not False
        or plan.get("safety", {}).get("fundamentals_status") != "pending"
    ):
        raise ValueError("activation plan policy or content hash is invalid")
    return envelope


def _under_root(
    root: Path, path: Path, *, label: str, require_file: bool = True
) -> Path:
    candidate = Path(path)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the project root") from exc
    for ancestor in (resolved, *resolved.parents):
        if ancestor == root.parent:
            break
        if ancestor.is_symlink():
            raise ValueError(f"{label} path cannot contain symbolic links")
    if require_file and (not resolved.is_file() or resolved.stat().st_nlink != 1):
        raise ValueError(f"{label} must be one regular unaliased file")
    return resolved


def _required_actor(actor: str) -> str:
    value = str(actor).strip()
    if not value or len(value) > 80 or any(
        not (character.isalnum() or character in "-_.:@") for character in value
    ):
        raise ValueError("actor must contain 1-80 safe identity characters")
    return value


def _require_sha256(value: str, *, label: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_json_safe = json_safe


def _canonical_json(value: Any) -> str:
    return canonical_json(json_safe(value))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()

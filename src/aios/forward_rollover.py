"""Governed planning, activation, and recovery for forward-trial rollover.

Preview remains read-only. Activation consumes one independently persisted,
content-addressed plan and requires explicit confirmation, fresh readiness and
operations gates, a verified backup, fixed-order document locks, append-only
crash evidence, and a final compare-and-set. It never mutates the paper
account, records a fill, or sends a broker order.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from string import hexdigits
from tempfile import TemporaryDirectory
from typing import Any

from aios.artifacts import publish_text_write_once
from aios.forward import assess_forward_trial, read_forward_trial
from aios.market_calendar import us_equity_sessions
from aios.operations import BackupResult
from aios.paper import (
    ACCOUNT_DOCUMENT_KIND,
    ACCOUNT_SCHEMA_VERSION,
    PROPOSAL_DOCUMENT_KIND,
    PROPOSAL_SCHEMA_VERSION,
    SIMULATION_MODE,
    canonical_payload_sha256,
    create_paper_proposal,
    paper_proposal_timing_status,
    read_paper_document,
)
from aios.risk.policy import PortfolioRiskPolicy
from aios.rollover_journal import ROLLOVER_ATTEMPT_DOCUMENT_KIND
from aios.storage.store import Store

ROLLOVER_PREVIEW_DOCUMENT_KIND = "aios.forward-rollover-preview"
ROLLOVER_PLAN_DOCUMENT_KIND = "aios.forward-rollover-plan"
ROLLOVER_PREVIEW_SCHEMA_VERSION = "forward-rollover-preview.v3"
ROLLOVER_PLAN_SCHEMA_VERSION = "forward-rollover-plan.v4"
ROLLOVER_POLICY_CONTRACT = "forward-rollover.v4.1"
ROLLOVER_ACTIVATION_ENABLED = True
ROLLOVER_LINEAGE_SCHEMA_VERSION = "forward-rollover-lineage.v1"
ROLLOVER_POLICY_FILE = "src/aios/forward_rollover.py"
ROLLOVER_PLAN_REPORT_DIRECTORY = Path("data/reports/forward_rollovers/plans")
MINIMUM_ACTIVATION_MARGIN_SECONDS = 300
MAXIMUM_PREFLIGHT_AGE_SECONDS = 120
ROLLOVER_ADDITIONAL_POLICY_FILES = (
    ROLLOVER_POLICY_FILE,
    "src/aios/forward_rollover_activation.py",
    "src/aios/alerts.py",
    "src/aios/artifacts.py",
    "src/aios/cli.py",
    "src/aios/config.py",
    "src/aios/ingest/edgar.py",
    "src/aios/ingest/prices.py",
    "src/aios/local_state_upgrade.py",
    "src/aios/maintenance.py",
    "src/aios/operations.py",
    "src/aios/operator_evidence.py",
    "src/aios/operator_preflight.py",
    "src/aios/raw_snapshots.py",
    "src/aios/rollover_journal.py",
    "src/aios/security.py",
    "src/aios/storage/schema.py",
    "src/aios/storage/store.py",
)
REQUIRED_READINESS_CHECKS = (
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
READINESS_FIELDS = frozenset(
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
READINESS_CHECK_FIELDS = frozenset({"check", "label", "status", "observed", "required", "detail"})


@dataclass(frozen=True)
class ForwardRolloverPreview:
    """One immutable in-memory preview over checksum-bound live evidence."""

    checked_at: str
    plan_sha256: str
    _canonical_plan_json: str
    _canonical_observation_json: str

    @property
    def plan(self) -> dict[str, Any]:
        return json.loads(self._canonical_plan_json)

    @property
    def observation(self) -> dict[str, Any]:
        return json.loads(self._canonical_observation_json)

    @property
    def plan_complete(self) -> bool:
        return bool(self.observation["plan_complete"])

    @property
    def source_eligible(self) -> bool:
        return bool(self.observation["source_eligible"])

    @property
    def activation_available(self) -> bool:
        return bool(self.plan["activation_contract"]["available_in_this_build"])

    def to_plan_envelope(self) -> dict[str, Any]:
        """Return the stable review artifact; volatile observations stay outside it."""

        return {
            "document_kind": ROLLOVER_PLAN_DOCUMENT_KIND,
            "schema_version": ROLLOVER_PLAN_SCHEMA_VERSION,
            "read_only": True,
            "payload_sha256": self.plan_sha256,
            "payload": self.plan,
        }

    def canonical_plan_artifact_json(self) -> str:
        return _canonical_json(self.to_plan_envelope())

    def to_envelope(self) -> dict[str, Any]:
        return {
            "document_kind": ROLLOVER_PREVIEW_DOCUMENT_KIND,
            "schema_version": ROLLOVER_PREVIEW_SCHEMA_VERSION,
            "read_only": True,
            "checked_at": self.checked_at,
            "plan_sha256": self.plan_sha256,
            "plan": self.plan,
            "observation": self.observation,
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.to_envelope())


@dataclass(frozen=True)
class ForwardRolloverResult:
    """Terminal evidence returned after one verified successor activation."""

    plan_sha256: str
    attempt_id: str
    predecessor_trial_id: str
    successor_trial_id: str
    active_trial: Path
    archived_trial: Path
    archived_proposal: Path
    successor_proposal: Path
    backup: BackupResult
    journal_directory: Path
    verified_receipt: Path


@dataclass(frozen=True)
class ForwardRolloverRecovery:
    """The terminal authority selected for one recovered transaction."""

    plan_sha256: str
    attempt_id: str
    terminal_phase: str
    active_trial: Path
    journal_directory: Path
    terminal_receipt: Path


def execute_forward_rollover_from_plan(
    project_root: Path,
    plan_path: Path,
    expected_plan_sha256: str,
    *,
    database_path: Path,
    operations_database_path: Path,
    application_version: str,
    confirm: bool,
) -> ForwardRolloverResult:
    """Activate an exact persisted plan through the isolated transaction engine."""

    if not ROLLOVER_ACTIVATION_ENABLED:
        raise ValueError(
            "forward rollover activation is disabled in this build pending "
            "owner-approved policy constants and a prospective market window"
        )

    from aios.forward_rollover_activation import execute_forward_rollover_from_plan

    return execute_forward_rollover_from_plan(
        project_root,
        plan_path,
        expected_plan_sha256,
        database_path=database_path,
        operations_database_path=operations_database_path,
        application_version=application_version,
        confirm=confirm,
    )


def recover_forward_rollover_from_plan(
    project_root: Path,
    plan_path: Path,
    expected_plan_sha256: str,
    *,
    confirm: bool,
) -> tuple[ForwardRolloverRecovery, ...]:
    """Recover one exact plan without creating a new successor."""

    from aios.forward_rollover_activation import recover_forward_rollover_from_plan

    return recover_forward_rollover_from_plan(
        project_root,
        plan_path,
        expected_plan_sha256,
        confirm=confirm,
    )


def read_forward_rollover_plan(
    path: Path,
    *,
    expected_activation_available: bool | None = None,
) -> dict[str, Any]:
    """Read one strict checksum-protected plan artifact without changing state.

    Normal review binds the artifact to this build's activation gate. Recovery
    may instead require a historical ``True`` artifact produced by an enabled
    build after activation has subsequently been disabled.
    """

    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"rollover plan does not exist: {source}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"rollover plan is unreadable: {source}") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "document_kind",
        "schema_version",
        "read_only",
        "payload_sha256",
        "payload",
    }:
        raise ValueError("unsupported rollover plan artifact")
    if (
        raw.get("document_kind") != ROLLOVER_PLAN_DOCUMENT_KIND
        or raw.get("schema_version") != ROLLOVER_PLAN_SCHEMA_VERSION
        or raw.get("read_only") is not True
        or not _is_lower_sha256(raw.get("payload_sha256"))
        or not isinstance(raw.get("payload"), dict)
    ):
        raise ValueError("unsupported rollover plan artifact")
    payload = _json_mapping(raw["payload"], label="rollover plan payload")
    activation_contract = payload.get("activation_contract")
    expected_availability = (
        ROLLOVER_ACTIVATION_ENABLED
        if expected_activation_available is None
        else expected_activation_available
    )
    if (
        payload.get("plan_schema_version") != ROLLOVER_PLAN_SCHEMA_VERSION
        or payload.get("operation") != "prospective_forward_rollover"
        or not isinstance(activation_contract, dict)
        or activation_contract.get("available_in_this_build")
        is not expected_availability
        or activation_contract.get("persisted_plan_artifact_required") is not True
    ):
        raise ValueError("rollover plan payload is invalid")
    actual = canonical_payload_sha256(payload)
    if not _constant_time_equal(str(raw["payload_sha256"]), actual):
        raise ValueError("rollover plan checksum mismatch")
    return payload


def persist_forward_rollover_plan(
    project_root: Path,
    preview: ForwardRolloverPreview,
) -> Path:
    """Publish one content-addressed review plan outside governed paper state."""

    if not isinstance(preview, ForwardRolloverPreview):
        raise TypeError("a forward rollover preview is required")
    if not preview.plan_complete:
        blockers = ", ".join(preview.observation["plan_blockers"])
        raise ValueError(f"rollover plan is incomplete: {blockers}")

    root = Path(project_root).resolve()
    plan = preview.plan
    if not _constant_time_equal(
        preview.plan_sha256,
        canonical_payload_sha256(plan),
    ):
        raise ValueError("rollover preview plan checksum mismatch")
    _require_plan_sources_unchanged(root, plan)
    destination = _rollover_plan_artifact_path(root, preview.plan_sha256)
    encoded = preview.canonical_plan_artifact_json() + "\n"

    if destination.exists() or destination.is_symlink():
        current = _require_matching_plan_artifact(root, destination, encoded, plan)
        _require_plan_sources_unchanged(root, plan)
        return current

    _reject_symlink_ancestors(root, destination, label="rollover plan")
    try:
        publish_text_write_once(destination, encoded)
    except FileExistsError:
        current = _require_matching_plan_artifact(root, destination, encoded, plan)
        _require_plan_sources_unchanged(root, plan)
        return current
    published = _governed_project_file(root, destination, label="rollover plan")
    if read_forward_rollover_plan(published) != plan:
        raise RuntimeError("published rollover plan failed checksum verification")
    _require_plan_sources_unchanged(root, plan)
    return published


def _require_matching_plan_artifact(
    root: Path,
    destination: Path,
    expected_text: str,
    expected_plan: dict[str, Any],
) -> Path:
    current = _governed_project_file(root, destination, label="rollover plan")
    if current.read_text(encoding="utf-8") != expected_text:
        raise ValueError(f"rollover plan artifact collision: {current}")
    if read_forward_rollover_plan(current) != expected_plan:
        raise ValueError("existing rollover plan does not match the reviewed plan")
    return current


def preview_forward_rollover(
    project_root: Path,
    trial_path: Path,
    account_path: Path,
    *,
    store: Store,
    successor_decision_date: date,
    readiness_evidence: Mapping[str, Any],
    operator_preflight_evidence: Mapping[str, Any],
    now: datetime | None = None,
) -> ForwardRolloverPreview:
    """Plan an expired-proposal rollover without changing governed state.

    The plan hash excludes the wall clock so an operator can review a stable
    authorization artifact.  Eligibility still fails when less than five
    minutes remain before the successor proposal-generation deadline.
    """

    checked = _aware_utc(now)
    checked_at = _timestamp(checked)
    root = Path(project_root).resolve()
    trial_file = _governed_project_file(root, trial_path, label="forward trial")
    account_file = _governed_project_file(root, account_path, label="paper account")
    trial_file_sha256 = _file_sha256(trial_file)
    account_file_sha256 = _file_sha256(account_file)

    trial = read_forward_trial(trial_file)
    account = read_paper_document(account_file, expected_kind=ACCOUNT_DOCUMENT_KIND)
    status = assess_forward_trial(root, trial_file, account_file)
    plan_blockers: set[str] = set()
    observation_blockers: set[str] = set()
    if not status.ready:
        plan_blockers.add("forward_trial_not_unchanged")
    _validate_account_payload(account.payload)
    for key in ("account_id", "market", "universe_id", "strategy"):
        if trial.payload.get(key) != account.payload.get(key):
            plan_blockers.add(f"forward_trial_{key}_mismatch")

    records = _registered_proposal_records(trial.payload)
    global_records = _global_registered_proposal_records(root, trial_file, records)
    registered_proposal_files = _registered_proposal_files(
        root,
        global_records,
        plan_blockers,
    )
    _apply_proposal_namespace_gate(root, registered_proposal_files, plan_blockers)
    executions, executed_keys = _execution_records(account.payload)
    executed_by_id = {
        str(row["proposal_id"]): str(row["proposal_payload_sha256"]) for row in executions
    }
    unresolved = [
        record
        for record in records
        if (str(record["proposal_id"]), str(record["payload_sha256"])) not in executed_keys
    ]
    for record in unresolved:
        recorded_hash = executed_by_id.get(str(record["proposal_id"]))
        if recorded_hash is not None and not _constant_time_equal(
            recorded_hash,
            str(record["payload_sha256"]),
        ):
            plan_blockers.add("proposal_execution_checksum_mismatch")
    if not unresolved:
        plan_blockers.add("unresolved_registered_proposal_missing")
    elif len(unresolved) > 1:
        plan_blockers.add("multiple_unresolved_registered_proposals")

    source_record = unresolved[0] if len(unresolved) == 1 else None
    source_proposal: dict[str, Any] | None = None
    source_proposal_file: Path | None = None
    source_proposal_file_sha256: str | None = None
    source_timing: dict[str, str] | None = None
    if source_record is not None:
        source_proposal_file = registered_proposal_files[str(source_record["proposal_id"])]
        source_proposal_file_sha256 = _file_sha256(source_proposal_file)
        proposal_document = read_paper_document(
            source_proposal_file,
            expected_kind=PROPOSAL_DOCUMENT_KIND,
        )
        source_proposal = proposal_document.payload
        _validate_source_proposal(
            trial.payload,
            account.payload,
            account.payload_sha256,
            source_record,
            source_proposal,
            proposal_document.payload_sha256,
            plan_blockers,
        )
        if source_record != max(records, key=_record_order_key):
            plan_blockers.add("unresolved_proposal_is_not_latest")
        try:
            source_timing = paper_proposal_timing_status(
                source_proposal,
                now=checked,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("registered proposal timing evidence is invalid") from exc
        if source_timing["status"] != "expired":
            plan_blockers.add("registered_proposal_not_expired")

    registered_dates = _registered_decision_dates(records)
    if registered_dates and successor_decision_date <= max(registered_dates):
        plan_blockers.add("successor_decision_date_not_later")
    successor_session = _next_session(successor_decision_date)
    generation_deadline = _proposal_generation_deadline(
        successor_decision_date,
        successor_session,
    )
    if checked + timedelta(seconds=MINIMUM_ACTIVATION_MARGIN_SECONDS) >= generation_deadline:
        observation_blockers.add("successor_generation_margin_insufficient")

    readiness = _json_mapping(readiness_evidence, label="readiness evidence")
    _apply_readiness_gates(
        readiness,
        successor_decision_date,
        expected_universe=str(trial.payload.get("universe_id") or ""),
        blockers=plan_blockers,
    )
    readiness_sha256 = canonical_payload_sha256(readiness)

    preflight = _verified_preflight(operator_preflight_evidence)
    _apply_preflight_gates(
        preflight,
        checked=checked,
        root=root,
        trial=trial.payload,
        trial_file=trial_file,
        account=account.payload,
        account_payload_sha256=account.payload_sha256,
        account_file=account_file,
        source_record=source_record,
        source_proposal_file=source_proposal_file,
        successor_decision_date=successor_decision_date,
        blockers=observation_blockers,
    )

    frozen_configuration = trial.payload.get("frozen_configuration")
    if not isinstance(frozen_configuration, dict):
        raise ValueError("forward trial frozen configuration is invalid")
    top_n = frozen_configuration.get("top_n")
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n < 1:
        raise ValueError("forward trial target count is invalid")
    policy_files = _successor_policy_files(root, trial.payload)
    successor_policy_bundle_sha256 = canonical_payload_sha256({"files": policy_files})
    risk_policy_payload = _json_mapping(
        frozen_configuration.get("risk_policy"),
        label="forward trial risk policy",
    )
    try:
        risk_policy = PortfolioRiskPolicy(**risk_policy_payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("forward trial risk policy is invalid") from exc
    normalized_proposal_intent = (
        {"status": "withheld", "reason": "source_ineligible"}
        if plan_blockers
        else _prove_successor_proposal(
            root=root,
            account_file=account_file,
            account=account.payload,
            account_payload_sha256=account.payload_sha256,
            successor_decision_date=successor_decision_date,
            successor_session=successor_session,
            store=store,
            top_n=top_n,
            risk_policy=risk_policy,
            readiness=readiness,
            readiness_sha256=readiness_sha256,
            checked=checked,
        )
    )

    rollover_seed = {
        "plan_schema_version": ROLLOVER_PLAN_SCHEMA_VERSION,
        "predecessor_trial_payload_sha256": trial.payload_sha256,
        "expired_proposal_payload_sha256": (
            str(source_record["payload_sha256"]) if source_record is not None else None
        ),
        "account_payload_sha256": account.payload_sha256,
        "successor_decision_date": successor_decision_date.isoformat(),
        "successor_policy_bundle_sha256": successor_policy_bundle_sha256,
    }
    rollover_key = canonical_payload_sha256(rollover_seed)
    trial_id = str(trial.payload.get("trial_id") or "invalid-trial")
    proposal_id = (
        str(source_record["proposal_id"]) if source_record is not None else "invalid-proposal"
    )
    trial_archive = trial_file.parent / "forward_trials" / f"{trial_id}.json"
    proposal_archive = trial_file.parent / "proposal_archives" / f"{proposal_id}.json"
    generation_directory = root / "data" / "paper" / "proposals" / "rollovers" / rollover_key
    successor_proposal_path = generation_directory / "proposal.json"
    staged_trial_path = root / "data" / "paper" / ".rollovers" / f"{rollover_key}.trial.tmp"
    for label, candidate in (
        ("predecessor trial archive", trial_archive),
        ("predecessor proposal archive", proposal_archive),
        ("successor proposal", successor_proposal_path),
        ("staged successor trial", staged_trial_path),
    ):
        _governed_project_destination(root, candidate, label=label)
        if candidate.exists() or candidate.is_symlink():
            plan_blockers.add(f"{label.replace(' ', '_')}_already_exists")

    plan: dict[str, Any] = {
        "plan_schema_version": ROLLOVER_PLAN_SCHEMA_VERSION,
        "operation": "prospective_forward_rollover",
        "rollover_key": rollover_key,
        "predecessor": {
            "trial_id": trial_id,
            "trial_path": _relative_path(root, trial_file),
            "trial_payload_sha256": trial.payload_sha256,
            "trial_file_sha256": trial_file_sha256,
            "policy_bundle_sha256": str(trial.payload.get("policy_bundle_sha256") or ""),
            "trial_archive_path": _relative_path(root, trial_archive),
            "proposal_id": (
                str(source_record["proposal_id"]) if source_record is not None else None
            ),
            "proposal_path": _relative_path(root, source_proposal_file),
            "proposal_payload_sha256": (
                str(source_record["payload_sha256"]) if source_record is not None else None
            ),
            "proposal_file_sha256": source_proposal_file_sha256,
            "proposal_archive_path": _relative_path(root, proposal_archive),
            "decision_date": (
                str(source_record["decision_date"]) if source_record is not None else None
            ),
            "scheduled_simulation_date": (
                str(source_proposal.get("scheduled_simulation_date") or "")
                if source_proposal is not None
                else None
            ),
            "generated_at": (
                str(source_proposal.get("generated_at") or "")
                if source_proposal is not None
                else None
            ),
            "expires_at": (source_timing["expires_at"] if source_timing is not None else None),
            "timing_status": (source_timing["status"] if source_timing is not None else None),
            "execution_recorded": False,
            "disposition": {
                "kind": "no_fill",
                "reason": "expired_without_execution",
                "predecessor_rewrite": False,
                "paper_account_mutation": False,
                "broker_order": False,
            },
        },
        "account": {
            "account_id": str(account.payload.get("account_id") or ""),
            "account_path": _relative_path(root, account_file),
            "account_payload_sha256": account.payload_sha256,
            "account_file_sha256": account_file_sha256,
            "execution_identities_sha256": canonical_payload_sha256(
                {
                    "proposal_identities": [
                        {"proposal_id": proposal_id, "payload_sha256": payload_sha256}
                        for proposal_id, payload_sha256 in sorted(executed_keys)
                    ]
                }
            ),
            "execution_registry_sha256": canonical_payload_sha256({"executions": executions}),
            "execution_count": len(executions),
            "mode": account.payload.get("mode"),
            "broker_connected": account.payload.get("broker_connected"),
        },
        "successor": {
            "decision_date": successor_decision_date.isoformat(),
            "scheduled_simulation_date": successor_session.isoformat(),
            "must_be_generated_before": _timestamp(generation_deadline),
            "minimum_activation_margin_seconds": MINIMUM_ACTIVATION_MARGIN_SECONDS,
            "top_n": top_n,
            "proposal_path": _relative_path(root, successor_proposal_path),
            "normalized_proposal_blueprint": normalized_proposal_intent,
            "normalized_proposal_blueprint_sha256": canonical_payload_sha256(
                normalized_proposal_intent
            ),
            "readiness_evidence": readiness,
            "readiness_payload_sha256": readiness_sha256,
            "policy_files": policy_files,
            "policy_bundle_sha256": successor_policy_bundle_sha256,
        },
        "transaction": {
            "active_trial_path": _relative_path(root, trial_file),
            "staged_trial_path": _relative_path(root, staged_trial_path),
            "future_journal_path": (
                Path("data/paper/rollovers") / rollover_key
            ).as_posix(),
            "journal_document_kind": ROLLOVER_ATTEMPT_DOCUMENT_KIND,
            "verified_backup_required": True,
            "archive_before_activation": True,
            "single_active_swap_required": True,
        },
        "activation_contract": {
            "available_in_this_build": ROLLOVER_ACTIVATION_ENABLED,
            "required_policy_contract": ROLLOVER_POLICY_CONTRACT,
            "explicit_confirmation_required": True,
            "required_plan_hash": True,
            "persisted_plan_artifact_required": True,
            "operations_gate": {
                "predicate": "canonical_operator_preflight_operations_available",
                "fresh_recheck_required": True,
                "evidence_stored_in_plan": False,
            },
            "required_gates": [
                "verified_backup_bound_to_plan",
                "exclusive_project_lock",
                "account_trial_proposal_compare_and_set",
                "successor_readiness_and_operations_recheck",
                "successor_generation_deadline_recheck",
                "byte_identical_predecessor_archives",
                "append_only_crash_recovery_journal",
                "single_atomic_active_trial_swap",
                "post_activation_verification_or_atomic_recovery",
            ],
            "forbidden_actions": [
                "retrospective_fill",
                "paper_account_mutation",
                "broker_order",
                "predecessor_rewrite",
                "proposal_backfill",
            ],
        },
    }
    canonical_plan = _canonical_json(plan)
    plan_sha256 = hashlib.sha256(canonical_plan.encode("utf-8")).hexdigest()
    all_blockers = plan_blockers | observation_blockers
    observation = {
        "plan_complete": not plan_blockers,
        "source_eligible": not all_blockers,
        "plan_blockers": sorted(plan_blockers),
        "live_blockers": sorted(observation_blockers),
        "blockers": sorted(all_blockers),
        "operator_preflight": _preflight_observation(preflight),
    }
    canonical_observation = _canonical_json(observation)

    _require_file_unchanged(
        root,
        trial_file,
        trial_file_sha256,
        label="forward trial",
    )
    _require_file_unchanged(
        root,
        account_file,
        account_file_sha256,
        label="paper account",
    )
    if source_proposal_file is not None and source_proposal_file_sha256 is not None:
        _require_file_unchanged(
            root,
            source_proposal_file,
            source_proposal_file_sha256,
            label="registered proposal",
        )
    _require_policy_files_unchanged(root, policy_files)
    return ForwardRolloverPreview(
        checked_at=checked_at,
        plan_sha256=plan_sha256,
        _canonical_plan_json=canonical_plan,
        _canonical_observation_json=canonical_observation,
    )


def _validate_account_payload(payload: dict[str, Any]) -> None:
    if payload.get("account_schema_version") != ACCOUNT_SCHEMA_VERSION:
        raise ValueError("unsupported paper account schema")
    if payload.get("mode") != SIMULATION_MODE:
        raise ValueError("paper account is not simulation-only")
    if payload.get("broker_connected") is not False:
        raise ValueError("paper account must not have a broker connection")
    for field in ("account_id", "market", "universe_id", "strategy"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ValueError(f"paper account {field.replace('_', ' ')} is invalid")
    if not isinstance(payload.get("portfolio"), dict):
        raise ValueError("paper account is missing portfolio state")
    if not isinstance(payload.get("audit_events"), list):
        raise ValueError("paper account audit state is invalid")
    _execution_records(payload)


def _validate_source_proposal(
    trial: dict[str, Any],
    account: dict[str, Any],
    account_payload_sha256: str,
    record: dict[str, Any],
    proposal: dict[str, Any],
    proposal_payload_sha256: str,
    blockers: set[str],
) -> None:
    if proposal.get("proposal_schema_version") != PROPOSAL_SCHEMA_VERSION:
        raise ValueError("unsupported paper proposal schema")
    targets = proposal.get("targets")
    if not isinstance(targets, list):
        raise ValueError("paper proposal targets are invalid")
    if proposal.get("mode") != SIMULATION_MODE:
        blockers.add("registered_proposal_not_simulation_only")
    if proposal.get("status") != "approved_for_supervised_simulation":
        blockers.add("registered_proposal_not_approved")
    for field in ("proposal_id", "decision_date", "generated_at", "status"):
        if record.get(field) != proposal.get(field):
            blockers.add(f"registered_proposal_{field}_mismatch")
    if not _constant_time_equal(
        str(record["payload_sha256"]),
        proposal_payload_sha256,
    ):
        blockers.add("registered_proposal_checksum_mismatch")
    for key in ("account_id", "market", "universe_id", "strategy"):
        if proposal.get(key) != trial.get(key):
            blockers.add(f"registered_proposal_{key}_mismatch")
    if proposal.get("account_id") != account.get("account_id"):
        blockers.add("registered_proposal_account_mismatch")
    if not _constant_time_equal(
        str(proposal.get("account_payload_sha256") or ""),
        account_payload_sha256,
    ):
        blockers.add("paper_account_changed_after_registered_proposal")
    frozen = trial.get("frozen_configuration")
    if not isinstance(frozen, dict):
        raise ValueError("forward frozen configuration is invalid")
    if proposal.get("risk_policy") != frozen.get("risk_policy"):
        blockers.add("registered_proposal_risk_policy_mismatch")
    try:
        expected_targets = int(frozen["top_n"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("forward frozen target count is invalid") from exc
    if len(targets) != expected_targets:
        blockers.add("registered_proposal_target_count_mismatch")


def _registered_proposal_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records = payload.get("proposals")
    if not isinstance(records, list):
        raise ValueError("forward proposal registry is invalid")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("forward proposal registry is invalid")
        proposal_id = record.get("proposal_id")
        proposal_path = record.get("path")
        payload_sha256 = record.get("payload_sha256")
        decision_date = record.get("decision_date")
        generated_at = record.get("generated_at")
        status = record.get("status")
        if (
            not isinstance(proposal_id, str)
            or not proposal_id.strip()
            or not isinstance(proposal_path, str)
            or not proposal_path.strip()
            or not _is_sha256(payload_sha256)
            or not isinstance(decision_date, str)
            or not isinstance(generated_at, str)
            or not generated_at.strip()
            or not isinstance(status, str)
            or not status.strip()
        ):
            raise ValueError("forward proposal registry is invalid")
        if proposal_id in seen:
            raise ValueError("forward proposal registry repeats a proposal ID")
        try:
            date.fromisoformat(decision_date)
            _parse_timestamp(generated_at)
        except ValueError as exc:
            raise ValueError("forward proposal registry is invalid") from exc
        seen.add(proposal_id)
        output.append(record)
    return output


def _registered_proposal_files(
    root: Path,
    records: list[dict[str, Any]],
    blockers: set[str],
) -> dict[str, Path]:
    """Resolve every registered proposal and bind its exact document identity."""

    proposal_root = _absolute_path(root, root / "data/paper/proposals")
    output: dict[str, Path] = {}
    claimed_paths: set[Path] = set()
    for record in records:
        proposal_id = str(record["proposal_id"])
        if proposal_id in output:
            raise ValueError("proposal ID is registered by more than one forward trial")
        proposal_file = _governed_project_file(
            root,
            Path(str(record["path"])),
            label=f"registered proposal {proposal_id}",
        )
        try:
            proposal_file.relative_to(proposal_root)
        except ValueError:
            blockers.add("registered_proposal_outside_governed_namespace")
        if proposal_file in claimed_paths:
            raise ValueError("proposal path is registered by more than one forward trial")
        claimed_paths.add(proposal_file)
        document = read_paper_document(
            proposal_file,
            expected_kind=PROPOSAL_DOCUMENT_KIND,
        )
        if not _constant_time_equal(
            str(record["payload_sha256"]),
            document.payload_sha256,
        ):
            blockers.add("registered_proposal_checksum_mismatch")
        for field in ("proposal_id", "decision_date", "generated_at", "status"):
            if record.get(field) != document.payload.get(field):
                blockers.add(f"registered_proposal_{field}_mismatch")
        output[proposal_id] = proposal_file
    return output


def _global_registered_proposal_records(
    root: Path,
    active_trial_file: Path,
    active_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collect the unique proposal registry across active and archived trials."""

    records = list(active_records)
    archive_root = _absolute_path(
        root,
        active_trial_file.parent / "forward_trials",
    )
    _require_within(root, archive_root, label="forward trial archive namespace")
    _reject_symlink_ancestors(root, archive_root, label="forward trial archive namespace")
    if not archive_root.exists():
        return records
    if not archive_root.is_dir():
        raise ValueError("forward trial archive namespace must be a directory")
    for candidate in sorted(archive_root.rglob("*")):
        if candidate.is_symlink():
            raise ValueError("forward trial archive namespace cannot contain symlinks")
        if candidate.is_dir() or candidate.suffix.lower() != ".json":
            continue
        archive_file = _governed_project_file(
            root,
            candidate,
            label="archived forward trial",
        )
        archived_trial = read_forward_trial(archive_file)
        records.extend(_registered_proposal_records(archived_trial.payload))
    return records


def _apply_proposal_namespace_gate(
    root: Path,
    registered: Mapping[str, Path],
    blockers: set[str],
) -> None:
    """Reject every governed proposal JSON that is not exactly registered."""

    proposal_root = _absolute_path(root, root / "data/paper/proposals")
    _require_within(root, proposal_root, label="governed proposal namespace")
    _reject_symlink_ancestors(root, proposal_root, label="governed proposal namespace")
    if not proposal_root.exists():
        if registered:
            raise ValueError("governed proposal namespace is missing")
        return
    if not proposal_root.is_dir():
        raise ValueError("governed proposal namespace must be a directory")

    registered_paths = set(registered.values())
    for candidate in sorted(proposal_root.rglob("*")):
        if candidate.is_symlink():
            raise ValueError("governed proposal namespace cannot contain symlinks")
        if candidate.is_dir() or candidate.suffix.lower() != ".json":
            continue
        proposal_file = _governed_project_file(
            root,
            candidate,
            label="governed proposal",
        )
        if proposal_file not in registered_paths:
            relative = proposal_file.relative_to(root).as_posix()
            blockers.add(f"unregistered_governed_proposal:{relative}")


def _execution_records(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    executions = payload.get("executions")
    if not isinstance(executions, list):
        raise ValueError("paper execution registry is invalid")
    normalized: list[dict[str, Any]] = []
    proposal_ids: set[str] = set()
    proposal_keys: set[tuple[str, str]] = set()
    for execution in executions:
        if not isinstance(execution, dict):
            raise ValueError("paper execution registry is invalid")
        proposal_id = execution.get("proposal_id")
        proposal_sha256 = execution.get("proposal_payload_sha256")
        if (
            not isinstance(proposal_id, str)
            or not proposal_id.strip()
            or not _is_sha256(proposal_sha256)
        ):
            raise ValueError("paper execution registry is invalid")
        if proposal_id in proposal_ids:
            raise ValueError("paper execution registry repeats a proposal ID")
        proposal_ids.add(proposal_id)
        proposal_keys.add((proposal_id, str(proposal_sha256)))
        normalized.append(_json_mapping(execution, label="paper execution"))
    return normalized, proposal_keys


def _registered_decision_dates(records: list[dict[str, Any]]) -> tuple[date, ...]:
    try:
        return tuple(date.fromisoformat(str(record["decision_date"])) for record in records)
    except (TypeError, ValueError) as exc:
        raise ValueError("forward proposal decision date is invalid") from exc


def _record_order_key(record: dict[str, Any]) -> tuple[date, datetime, str, str]:
    return (
        date.fromisoformat(str(record["decision_date"])),
        _parse_timestamp(record["generated_at"]),
        str(record["proposal_id"]),
        str(record["payload_sha256"]),
    )


def _apply_readiness_gates(
    readiness: dict[str, Any],
    successor_decision_date: date,
    *,
    expected_universe: str,
    blockers: set[str],
) -> None:
    if set(readiness) != READINESS_FIELDS:
        raise ValueError("readiness evidence schema is invalid")
    if readiness.get("purpose") != "paper":
        blockers.add("successor_readiness_purpose_invalid")
    if readiness.get("as_of") != successor_decision_date.isoformat():
        blockers.add("successor_readiness_date_mismatch")
    if readiness.get("universe_id") != expected_universe:
        blockers.add("successor_readiness_universe_mismatch")
    if readiness.get("benchmark_ticker") != "SPY":
        blockers.add("successor_readiness_benchmark_mismatch")
    certified_through = readiness.get("certified_research_through")
    try:
        boundary = date.fromisoformat(str(certified_through))
    except (TypeError, ValueError):
        blockers.add("successor_readiness_certification_missing")
    else:
        if boundary != successor_decision_date:
            blockers.add("successor_readiness_not_certified_for_exact_decision")
    certified_from = readiness.get("certified_research_from")
    try:
        lower_boundary = date.fromisoformat(str(certified_from))
    except (TypeError, ValueError):
        blockers.add("successor_readiness_certification_start_missing")
    else:
        if lower_boundary > successor_decision_date:
            blockers.add("successor_readiness_certification_range_invalid")
    try:
        date.fromisoformat(str(readiness.get("generated_on")))
    except (TypeError, ValueError) as exc:
        raise ValueError("readiness evidence generation date is invalid") from exc
    for field in (
        "raw_prices_through",
        "fundamentals_through",
        "macro_releases_through",
    ):
        value = readiness.get(field)
        if value is not None:
            try:
                date.fromisoformat(str(value))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"readiness evidence {field} is invalid") from exc
    checks = readiness.get("checks")
    if not isinstance(checks, list):
        raise ValueError("readiness evidence checks are invalid")
    normalized_checks: list[dict[str, Any]] = []
    for row in checks:
        if not isinstance(row, dict) or set(row) != READINESS_CHECK_FIELDS:
            raise ValueError("readiness evidence check schema is invalid")
        if row.get("status") not in {"pass", "warn", "fail"}:
            raise ValueError("readiness evidence check status is invalid")
        if any(not isinstance(row.get(field), str) for field in READINESS_CHECK_FIELDS):
            raise ValueError("readiness evidence check values are invalid")
        normalized_checks.append(row)
    names = tuple(str(row["check"]) for row in normalized_checks)
    if names != REQUIRED_READINESS_CHECKS:
        raise ValueError("readiness evidence check set is invalid")
    derived_ready = all(row["status"] != "fail" for row in normalized_checks)
    if readiness.get("ready") is not derived_ready:
        raise ValueError("readiness evidence ready flag is inconsistent")
    if not derived_ready:
        blockers.add("successor_research_not_ready")


def _apply_preflight_gates(
    preflight: dict[str, Any],
    *,
    checked: datetime,
    root: Path,
    trial: dict[str, Any],
    trial_file: Path,
    account: dict[str, Any],
    account_payload_sha256: str,
    account_file: Path,
    source_record: dict[str, Any] | None,
    source_proposal_file: Path | None,
    successor_decision_date: date,
    blockers: set[str],
) -> None:
    try:
        preflight_checked = _parse_timestamp(preflight.get("checked_at"))
    except ValueError as exc:
        raise ValueError("operator preflight checked_at is invalid") from exc
    if preflight_checked > checked:
        blockers.add("operator_preflight_from_future")
    elif (checked - preflight_checked).total_seconds() > MAXIMUM_PREFLIGHT_AGE_SECONDS:
        blockers.add("operator_preflight_stale")
    boundary = preflight.get("execution_boundary")
    if not isinstance(boundary, dict):
        raise ValueError("operator preflight execution boundary is invalid")
    if (
        boundary.get("simulation_only") is not True
        or boundary.get("broker_connected") is not False
        or boundary.get("broker_orders_enabled") is not False
    ):
        blockers.add("operator_preflight_broker_boundary_invalid")
    source_dates = preflight.get("source_dates")
    if not isinstance(source_dates, dict):
        raise ValueError("operator preflight source dates are invalid")
    if source_dates.get("certified_decision_close") != successor_decision_date.isoformat():
        blockers.add("operator_preflight_decision_date_mismatch")
    capabilities = preflight.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ValueError("operator preflight capabilities are invalid")
    for capability, blocker in (
        ("research", "operator_research_not_available"),
        ("operations", "operator_operations_not_available"),
    ):
        state = capabilities.get(capability)
        if not isinstance(state, dict) or state.get("available") is not True:
            blockers.add(blocker)

    identity = preflight.get("evidence_identity")
    if not isinstance(identity, dict):
        raise ValueError("operator preflight evidence identity is invalid")
    expected = {
        "account_id": account.get("account_id"),
        "trial_id": trial.get("trial_id"),
        "account_payload_sha256": account_payload_sha256,
        "trial_payload_sha256": canonical_payload_sha256(trial),
        "proposal_id": (source_record.get("proposal_id") if source_record is not None else None),
        "proposal_payload_sha256": (
            source_record.get("payload_sha256") if source_record is not None else None
        ),
    }
    if any(identity.get(key) != value for key, value in expected.items()):
        blockers.add("operator_preflight_evidence_identity_mismatch")
    for key, path in (
        ("account_path", account_file),
        ("trial_path", trial_file),
        ("proposal_path", source_proposal_file),
    ):
        if not _preflight_path_matches(root, identity.get(key), path):
            blockers.add("operator_preflight_evidence_path_mismatch")


def _verified_preflight(evidence: Mapping[str, Any]) -> dict[str, Any]:
    envelope = _json_mapping(evidence, label="operator preflight evidence")
    if (
        envelope.get("document_kind") != "aios.operator_preflight"
        or envelope.get("schema_version") != "operator-preflight.v1"
        or envelope.get("read_only") is not True
    ):
        raise ValueError("unsupported operator preflight evidence")
    stored = envelope.get("payload_sha256")
    if not _is_sha256(stored):
        raise ValueError("operator preflight checksum is invalid")
    payload = {key: value for key, value in envelope.items() if key != "payload_sha256"}
    actual = _json_sha256(payload, ensure_ascii=True)
    if not _constant_time_equal(str(stored), actual):
        raise ValueError("operator preflight checksum mismatch")
    return envelope


def _preflight_observation(preflight: dict[str, Any]) -> dict[str, Any]:
    """Keep volatile gate evidence visible without making it part of the plan hash."""

    capabilities = preflight.get("capabilities")
    if not isinstance(capabilities, dict):  # guarded by _apply_preflight_gates
        raise ValueError("operator preflight capabilities are invalid")
    projected: dict[str, dict[str, Any]] = {}
    for key in ("research", "operations"):
        state = capabilities.get(key)
        if not isinstance(state, dict):
            raise ValueError(f"operator preflight {key} capability is invalid")
        blockers = state.get("blockers", [])
        if not isinstance(blockers, list) or any(
            not isinstance(value, str) or not value.strip() for value in blockers
        ):
            raise ValueError(f"operator preflight {key} blockers are invalid")
        projected[key] = {
            "available": state.get("available") is True,
            "blockers": sorted(set(blockers)),
        }
    return {
        "checked_at": preflight["checked_at"],
        "payload_sha256": preflight["payload_sha256"],
        "capabilities": projected,
    }


def _prove_successor_proposal(
    *,
    root: Path,
    account_file: Path,
    account: dict[str, Any],
    account_payload_sha256: str,
    successor_decision_date: date,
    successor_session: date,
    store: Store,
    top_n: int,
    risk_policy: PortfolioRiskPolicy,
    readiness: dict[str, Any],
    readiness_sha256: str,
    checked: datetime,
) -> dict[str, Any]:
    """Run the production constructor outside governed state and normalize its intent."""

    if store is None:
        raise ValueError("a read-only store is required for successor proposal proof")
    with TemporaryDirectory(prefix="aios-rollover-preview-") as temporary:
        staging_root = Path(temporary).resolve()
        try:
            staging_root.relative_to(root)
        except ValueError:
            pass
        else:
            raise ValueError("successor proposal proof must stay outside the project root")
        staged_proposal = staging_root / "proposal.json"
        document = create_paper_proposal(
            account_file,
            staged_proposal,
            successor_decision_date,
            store,
            top_n=top_n,
            risk_policy=risk_policy,
            now=checked,
        )
        if Path(document.path) != staged_proposal:
            raise RuntimeError("production proposal constructor returned an unexpected path")
        verified = read_paper_document(
            staged_proposal,
            expected_kind=PROPOSAL_DOCUMENT_KIND,
        )
        if not _constant_time_equal(document.payload_sha256, verified.payload_sha256):
            raise RuntimeError("production proposal proof checksum changed after construction")
        payload = _json_mapping(verified.payload, label="successor proposal proof")

    expected = {
        "proposal_schema_version": PROPOSAL_SCHEMA_VERSION,
        "account_id": account.get("account_id"),
        "account_payload_sha256": account_payload_sha256,
        "market": account.get("market"),
        "universe_id": account.get("universe_id"),
        "strategy": account.get("strategy"),
        "mode": SIMULATION_MODE,
        "decision_date": successor_decision_date.isoformat(),
        "scheduled_simulation_date": successor_session.isoformat(),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("production successor proposal identity is invalid")
    proposal_id = payload.get("proposal_id")
    generated_at = payload.get("generated_at")
    if not isinstance(proposal_id, str) or not proposal_id.strip():
        raise ValueError("production successor proposal ID is invalid")
    try:
        generated_moment = _parse_timestamp(generated_at)
    except ValueError as exc:
        raise ValueError("production successor proposal timestamp is invalid") from exc
    if generated_moment != checked:
        raise ValueError("production successor proposal timestamp is not plan-bound")
    if payload.get("status") != "approved_for_supervised_simulation":
        raise ValueError("production successor proposal is not approved")
    if payload.get("risk_policy") != risk_policy.__dict__:
        raise ValueError("production successor proposal risk policy changed")
    targets = payload.get("targets")
    if not isinstance(targets, list) or len(targets) != top_n:
        raise ValueError("production successor proposal target evidence is incomplete")
    risk_assessment = payload.get("risk_assessment")
    if not isinstance(risk_assessment, dict) or risk_assessment.get("approved") is not True:
        raise ValueError("production successor proposal risk evidence is not approved")
    for field in ("factor_evidence_sha256", "decision_evidence_sha256"):
        if not _is_sha256(payload.get(field)):
            raise ValueError(f"production successor proposal {field} is invalid")
    proposal_readiness = payload.get("readiness")
    if not isinstance(proposal_readiness, dict):
        raise ValueError("production successor proposal readiness is missing")
    if not _constant_time_equal(
        canonical_payload_sha256(proposal_readiness),
        readiness_sha256,
    ):
        raise ValueError("production successor proposal readiness changed")
    if proposal_readiness != readiness:
        raise ValueError("production successor proposal readiness is not the reviewed evidence")

    payload["proposal_id"] = "<generated-at-activation>"
    payload["generated_at"] = "<generated-at-activation>"
    return payload


def _successor_policy_files(root: Path, trial: dict[str, Any]) -> dict[str, str]:
    stored = trial.get("policy_files")
    if not isinstance(stored, dict) or not stored:
        raise ValueError("forward policy file evidence is invalid")
    files: dict[str, str] = {}
    for relative, digest in stored.items():
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not _is_sha256(digest)
        ):
            raise ValueError("forward policy file evidence is invalid")
        files[relative] = str(digest)
    for relative in ROLLOVER_ADDITIONAL_POLICY_FILES:
        source = _governed_project_file(
            root,
            root / relative,
            label=f"successor policy module {relative}",
        )
        files[relative] = _file_sha256(source)
    return dict(sorted(files.items()))


def _require_policy_files_unchanged(root: Path, files: dict[str, str]) -> None:
    for relative, expected in files.items():
        source = _governed_project_file(root, root / relative, label="policy file")
        _require_file_unchanged(
            root,
            source,
            expected,
            label=f"policy file {relative}",
        )


def _rollover_plan_artifact_path(root: Path, plan_sha256: str) -> Path:
    if not _is_lower_sha256(plan_sha256):
        raise ValueError("rollover plan checksum is invalid")
    destination = _absolute_path(
        root,
        root / ROLLOVER_PLAN_REPORT_DIRECTORY / f"{plan_sha256}.json",
    )
    _require_within(root, destination, label="rollover plan")
    allowed = _absolute_path(root, root / ROLLOVER_PLAN_REPORT_DIRECTORY)
    if destination.parent != allowed:
        raise ValueError("rollover plan path escapes its report namespace")
    return destination


def _require_plan_sources_unchanged(root: Path, plan: dict[str, Any]) -> None:
    if plan.get("plan_schema_version") != ROLLOVER_PLAN_SCHEMA_VERSION:
        raise ValueError("rollover plan schema is invalid")
    predecessor = plan.get("predecessor")
    account = plan.get("account")
    successor = plan.get("successor")
    if not all(isinstance(value, dict) for value in (predecessor, account, successor)):
        raise ValueError("rollover plan source evidence is invalid")
    assert isinstance(predecessor, dict)
    assert isinstance(account, dict)
    assert isinstance(successor, dict)
    for label, path_value, digest in (
        (
            "forward trial",
            predecessor.get("trial_path"),
            predecessor.get("trial_file_sha256"),
        ),
        (
            "paper account",
            account.get("account_path"),
            account.get("account_file_sha256"),
        ),
        (
            "registered proposal",
            predecessor.get("proposal_path"),
            predecessor.get("proposal_file_sha256"),
        ),
    ):
        if (
            not isinstance(path_value, str)
            or not path_value
            or not _is_sha256(digest)
        ):
            raise ValueError("rollover plan source evidence is invalid")
        source = _governed_project_file(root, Path(path_value), label=label)
        _require_file_unchanged(root, source, str(digest), label=label)
    policy_files = successor.get("policy_files")
    if not isinstance(policy_files, dict):
        raise ValueError("rollover plan policy evidence is invalid")
    _require_policy_files_unchanged(root, policy_files)


def _next_session(decision_date: date) -> date:
    sessions = us_equity_sessions(
        decision_date + timedelta(days=1),
        decision_date + timedelta(days=15),
    )
    if not sessions:
        raise ValueError("no U.S. session follows the successor decision date")
    return sessions[0]


def _proposal_generation_deadline(
    decision_date: date,
    entry_date: date,
) -> datetime:
    timing = paper_proposal_timing_status(
        {
            "decision_date": decision_date.isoformat(),
            "scheduled_simulation_date": entry_date.isoformat(),
            "generated_at": f"{decision_date.isoformat()}T00:00:00Z",
        },
        now=datetime.combine(decision_date, datetime.min.time(), tzinfo=UTC),
    )
    return _parse_timestamp(timing["must_be_generated_before"])


def _governed_project_file(root: Path, value: Path, *, label: str) -> Path:
    candidate = _absolute_path(root, value)
    _require_within(root, candidate, label=label)
    _reject_symlink_ancestors(root, candidate, label=label)
    if not candidate.is_file() or candidate.stat().st_nlink != 1:
        raise ValueError(f"{label} must be one regular unaliased file")
    return candidate


def _governed_project_destination(root: Path, value: Path, *, label: str) -> Path:
    candidate = _absolute_path(root, value)
    _require_within(root, candidate, label=label)
    _reject_symlink_ancestors(root, candidate, label=label)
    if candidate.exists() and (not candidate.is_file() or candidate.stat().st_nlink != 1):
        raise ValueError(f"{label} must be one regular unaliased file")
    return candidate


def _absolute_path(root: Path, value: Path) -> Path:
    requested = Path(value).expanduser()
    return Path(os.path.abspath(requested if requested.is_absolute() else root / requested))


def _require_within(root: Path, candidate: Path, *, label: str) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the project root") from exc


def _reject_symlink_ancestors(root: Path, candidate: Path, *, label: str) -> None:
    for component in (candidate, *candidate.parents):
        if component.is_symlink():
            raise ValueError(f"{label} path cannot contain symlinks")
        if component == root:
            return
    raise ValueError(f"{label} escapes the project root")


def _relative_path(root: Path, value: Path | None) -> str | None:
    if value is None:
        return None
    try:
        return value.relative_to(root).as_posix()
    except ValueError as exc:  # pragma: no cover - callers resolve under root
        raise ValueError("rollover evidence path escapes the project root") from exc


def _preflight_path_matches(root: Path, value: Any, expected: Path | None) -> bool:
    if expected is None:
        return value is None
    if not isinstance(value, str) or not value.strip():
        return False
    return _absolute_path(root, Path(value)) == expected


def _json_mapping(value: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    try:
        normalized = json.loads(_canonical_json(dict(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not canonical JSON") from exc
    if not isinstance(normalized, dict):  # pragma: no cover - guarded above
        raise ValueError(f"{label} must be a JSON object")
    return normalized


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _json_sha256(value: Any, *, ensure_ascii: bool) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=ensure_ascii,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in hexdigits for character in value)
    )


def _is_lower_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _constant_time_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


def _aware_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("rollover preview time must include a timezone")
    return current.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_file_unchanged(
    root: Path,
    path: Path,
    expected: str,
    *,
    label: str,
) -> None:
    current = _governed_project_file(root, path, label=label)
    if not _constant_time_equal(_file_sha256(current), expected):
        raise RuntimeError(f"{label} changed while the rollover preview was built")

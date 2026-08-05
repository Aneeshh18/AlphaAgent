"""Crash-recoverable activation engine for persisted forward-rollover plans.

This module is imported lazily by :mod:`aios.forward_rollover`. Keeping the
mutation engine separate lets preview remain small and read-only while the
successor freezes both planning and transaction policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import ExitStack, suppress
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from aios.forward import (
    _write_forward_document,
    assess_forward_trial,
    create_forward_trial,
    read_forward_trial,
)
from aios.forward_rollover import (
    MINIMUM_ACTIVATION_MARGIN_SECONDS,
    ROLLOVER_ATTEMPT_DOCUMENT_KIND,
    ROLLOVER_LINEAGE_SCHEMA_VERSION,
    ROLLOVER_POLICY_CONTRACT,
    ForwardRolloverPreview,
    ForwardRolloverRecovery,
    ForwardRolloverResult,
    _absolute_path,
    _apply_preflight_gates,
    _apply_readiness_gates,
    _file_sha256,
    _global_registered_proposal_records,
    _governed_project_destination,
    _governed_project_file,
    _is_lower_sha256,
    _json_mapping,
    _parse_timestamp,
    _registered_proposal_files,
    _registered_proposal_records,
    _reject_symlink_ancestors,
    _relative_path,
    _require_plan_sources_unchanged,
    _rollover_plan_artifact_path,
    _timestamp,
    _verified_preflight,
    preview_forward_rollover,
    read_forward_rollover_plan,
)
from aios.maintenance import project_maintenance_lock
from aios.operations import BackupResult, create_local_backup, verify_local_backup
from aios.paper import (
    ACCOUNT_DOCUMENT_KIND,
    PROPOSAL_DOCUMENT_KIND,
    _paper_document_write_lock,
    canonical_payload_sha256,
    create_paper_proposal,
    paper_proposal_timing_status,
    read_paper_document,
)
from aios.risk.policy import PortfolioRiskPolicy
from aios.rollover_journal import (
    RolloverAttemptDocument,
    RolloverAttemptState,
    attempt_directory,
    build_attempt_payload,
    scan_attempt_directories,
    validate_attempt_directory,
    write_attempt_phase,
)
from aios.storage.store import Store, store_scope


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
    """Activate one exact persisted plan through a recoverable transaction."""

    from aios import forward_rollover as rollover_policy

    if not rollover_policy.ROLLOVER_ACTIVATION_ENABLED:
        raise ValueError(
            "forward rollover activation is disabled in this build pending "
            "owner-approved policy constants and a prospective market window"
        )

    if not confirm:
        raise ValueError("explicit --confirm-rollover approval is required")
    _require_sha256(expected_plan_sha256)
    if not isinstance(application_version, str) or not application_version.strip():
        raise ValueError("application version is required")

    root = Path(project_root).resolve()
    plan_file, plan = _load_plan_artifact(
        root,
        plan_path,
        expected_plan_sha256,
        require_live_sources=True,
    )
    _validate_activation_plan(plan)
    database_file = _governed_project_file(
        root,
        database_path,
        label="analytical database",
    )
    operations_file = _governed_project_file(
        root,
        operations_database_path,
        label="operations database",
    )
    paths = _activation_paths(root, plan)
    attempt_id = uuid4().hex
    journal = attempt_directory(root, str(plan["rollover_key"]), attempt_id)
    _governed_project_destination(root, journal, label="rollover attempt journal")
    prepared: RolloverAttemptDocument | None = None
    candidate: dict[str, Any] | None = None
    backup: BackupResult | None = None

    previous_umask = os.umask(0o077)
    try:
        with project_maintenance_lock(root, operation="forward-rollover"):
            _require_no_incomplete_attempts(root)
            with _document_locks(paths):
                _require_reviewed_preview(
                    _capture_fresh_preview(root, plan, database_file),
                    plan,
                    expected_plan_sha256,
                )
                _checkpoint_database(database_file)
                backup_time = _utc_now()
                backup = create_local_backup(
                    root,
                    database_file,
                    operations_database_path=operations_file,
                    output=(
                        root
                        / "backups"
                        / (
                            f"forward-rollover-{backup_time.strftime('%Y%m%dT%H%M%SZ')}-"
                            f"{expected_plan_sha256[:12]}-{attempt_id[:8]}"
                        )
                    ),
                    application_version=application_version,
                    now=backup_time,
                )
                if verify_local_backup(backup.path) != backup:
                    raise RuntimeError("rollover backup changed during final verification")
                _require_backup_bound_to_plan(root, backup, plan)
                _require_reviewed_preview(
                    _capture_fresh_preview(root, plan, database_file),
                    plan,
                    expected_plan_sha256,
                )

                candidate = _build_candidate(
                    root,
                    plan,
                    database_file,
                    backup,
                    paths,
                    attempt_id=attempt_id,
                    journal=journal,
                )
                prepared = write_attempt_phase(
                    journal,
                    build_attempt_payload(
                        attempt_id=attempt_id,
                        rollover_key=str(plan["rollover_key"]),
                        plan_sha256=expected_plan_sha256,
                        phase="prepared",
                        recorded_at=_utc_now(),
                        plan=plan,
                        backup=_backup_evidence(root, backup),
                        state=_prepared_state(root, plan, paths, candidate),
                    ),
                )
                _publish_rollover_outputs(root, paths, candidate)
                _write_followup_phase(
                    journal,
                    prepared,
                    phase="outputs_published",
                    state={
                        "output_file_sha256": dict(candidate["output_file_sha256"]),
                        "active_trial_file_sha256": _file_sha256(paths["active_trial"]),
                        "recovered_after_process_interruption": False,
                    },
                )
                _final_activation_recheck(
                    root,
                    plan_file,
                    plan,
                    expected_plan_sha256,
                    database_file,
                    paths,
                    candidate,
                )

                os.replace(paths["staged_trial"], paths["active_trial"])
                _fsync_directory(paths["active_trial"].parent)
                _write_followup_phase(
                    journal,
                    prepared,
                    phase="active_swapped",
                    state={
                        "active_trial_file_sha256": candidate["successor_trial_file_sha256"],
                        "active_trial_payload_sha256": (
                            candidate["successor_trial_payload_sha256"]
                        ),
                        "recovered_after_process_interruption": False,
                    },
                )
                _verify_successor(root, plan, paths, candidate)
                verified = _write_followup_phase(
                    journal,
                    prepared,
                    phase="verified",
                    state=_terminal_success_state(plan, backup, candidate),
                )
                active = read_forward_trial(paths["active_trial"])
                return ForwardRolloverResult(
                    plan_sha256=expected_plan_sha256,
                    attempt_id=attempt_id,
                    predecessor_trial_id=str(plan["predecessor"]["trial_id"]),
                    successor_trial_id=str(active.payload["trial_id"]),
                    active_trial=paths["active_trial"],
                    archived_trial=paths["trial_archive"],
                    archived_proposal=paths["proposal_archive"],
                    successor_proposal=paths["successor_proposal"],
                    backup=backup,
                    journal_directory=journal,
                    verified_receipt=verified.path,
                )
    except Exception as exc:
        if prepared is not None and candidate is not None:
            try:
                _rollback_started_attempt(
                    root,
                    plan,
                    paths,
                    candidate,
                    journal,
                    prepared,
                    error_type=type(exc).__name__,
                )
            except Exception as recovery_error:
                raise RuntimeError(
                    "rollover failed and durable recovery is required; "
                    f"journal={journal}; recovery_error={type(recovery_error).__name__}: "
                    f"{recovery_error}"
                ) from exc
        raise
    finally:
        os.umask(previous_umask)


def recover_forward_rollover_from_plan(
    project_root: Path,
    plan_path: Path,
    expected_plan_sha256: str,
    *,
    confirm: bool,
) -> tuple[ForwardRolloverRecovery, ...]:
    """Reconcile attempts for one plan without constructing a new successor."""

    if not confirm:
        raise ValueError("explicit --confirm-recovery approval is required")
    _require_sha256(expected_plan_sha256)
    root = Path(project_root).resolve()
    _plan_file, plan = _load_plan_artifact(
        root,
        plan_path,
        expected_plan_sha256,
        require_live_sources=False,
    )
    _validate_activation_plan(plan)
    states = [validate_attempt_directory(path) for path in scan_attempt_directories(root)]
    matches = [
        state for state in states if state.prepared.payload["plan_sha256"] == expected_plan_sha256
    ]
    if not matches:
        raise ValueError("no rollover attempt exists for the reviewed plan")
    if sum(not state.terminal for state in matches) > 1:
        raise ValueError("multiple incomplete rollover attempts require investigation")
    paths = _activation_paths(root, plan)
    output: list[ForwardRolloverRecovery] = []

    previous_umask = os.umask(0o077)
    try:
        with (
            project_maintenance_lock(root, operation="forward-rollover-recovery"),
            _document_locks(paths),
        ):
            for state in matches:
                if state.prepared.payload["plan"] != plan:
                    raise ValueError("rollover attempt is bound to another plan payload")
                if state.terminal:
                    _validate_terminal_state(root, plan, paths, state)
                    terminal = state
                else:
                    terminal = _recover_incomplete(root, plan, paths, state)
                output.append(_recovery_result(paths, terminal))
    finally:
        os.umask(previous_umask)
    return tuple(output)


def _load_plan_artifact(
    root: Path,
    supplied_path: Path,
    expected_sha256: str,
    *,
    require_live_sources: bool,
) -> tuple[Path, dict[str, Any]]:
    expected_path = _rollover_plan_artifact_path(root, expected_sha256)
    supplied = _absolute_path(root, supplied_path)
    if supplied != expected_path:
        raise ValueError("rollover activation requires the canonical content-addressed plan path")
    plan_file = _governed_project_file(root, supplied, label="rollover plan")
    plan = read_forward_rollover_plan(
        plan_file,
        expected_activation_available=True,
    )
    if canonical_payload_sha256(plan) != expected_sha256:
        raise ValueError("persisted rollover plan does not match the reviewed SHA-256")
    if require_live_sources:
        _require_plan_sources_unchanged(root, plan)
    return plan_file, plan


def _validate_activation_plan(plan: dict[str, Any]) -> None:
    predecessor = plan.get("predecessor")
    account = plan.get("account")
    successor = plan.get("successor")
    transaction = plan.get("transaction")
    contract = plan.get("activation_contract")
    if not all(
        isinstance(value, dict)
        for value in (predecessor, account, successor, transaction, contract)
    ):
        raise ValueError("rollover activation plan is incomplete")
    expected_journal = (Path("data/paper/rollovers") / str(plan.get("rollover_key"))).as_posix()
    if (
        not _is_lower_sha256(plan.get("rollover_key"))
        or transaction.get("future_journal_path") != expected_journal
        or transaction.get("journal_document_kind") != ROLLOVER_ATTEMPT_DOCUMENT_KIND
        or transaction.get("verified_backup_required") is not True
        or transaction.get("single_active_swap_required") is not True
        or contract.get("available_in_this_build") is not True
        or contract.get("required_policy_contract") != ROLLOVER_POLICY_CONTRACT
        or contract.get("persisted_plan_artifact_required") is not True
        or account.get("mode") != "simulation_only"
        or account.get("broker_connected") is not False
    ):
        raise ValueError("rollover activation plan violates the governed boundary")
    if predecessor.get("disposition") != {
        "kind": "no_fill",
        "reason": "expired_without_execution",
        "predecessor_rewrite": False,
        "paper_account_mutation": False,
        "broker_order": False,
    }:
        raise ValueError("rollover predecessor disposition is invalid")
    _parse_timestamp(successor.get("must_be_generated_before"))
    date.fromisoformat(str(successor.get("decision_date")))


def _activation_paths(root: Path, plan: dict[str, Any]) -> dict[str, Path]:
    predecessor = plan["predecessor"]
    account = plan["account"]
    successor = plan["successor"]
    transaction = plan["transaction"]
    raw = {
        "active_trial": transaction["active_trial_path"],
        "account": account["account_path"],
        "predecessor_proposal": predecessor["proposal_path"],
        "trial_archive": predecessor["trial_archive_path"],
        "proposal_archive": predecessor["proposal_archive_path"],
        "successor_proposal": successor["proposal_path"],
        "staged_trial": transaction["staged_trial_path"],
    }
    paths: dict[str, Path] = {}
    for label, value in raw.items():
        if not isinstance(value, str) or not value:
            raise ValueError("rollover activation path evidence is invalid")
        paths[label] = _governed_project_destination(
            root,
            Path(value),
            label=label.replace("_", " "),
        )
    for label in ("active_trial", "account", "predecessor_proposal"):
        _governed_project_file(root, paths[label], label=label.replace("_", " "))
    return paths


def _document_locks(paths: dict[str, Path]) -> ExitStack:
    stack = ExitStack()
    try:
        for path in sorted(set(paths.values()), key=str):
            stack.enter_context(_paper_document_write_lock(path))
    except Exception:
        stack.close()
        raise
    return stack


def _capture_fresh_preview(
    root: Path,
    plan: dict[str, Any],
    database_file: Path,
) -> ForwardRolloverPreview:
    checked = _utc_now()
    decision_date = date.fromisoformat(str(plan["successor"]["decision_date"]))
    with store_scope(database_file, read_only=True) as store:
        readiness = _assess_fresh_readiness(decision_date, store)
        preflight = _assess_fresh_operator_preflight(checked)
        return preview_forward_rollover(
            root,
            root / str(plan["predecessor"]["trial_path"]),
            root / str(plan["account"]["account_path"]),
            store=store,
            successor_decision_date=decision_date,
            readiness_evidence=readiness,
            operator_preflight_evidence=preflight,
            now=checked,
        )


def _require_reviewed_preview(
    preview: ForwardRolloverPreview,
    plan: dict[str, Any],
    expected_sha256: str,
) -> None:
    if preview.plan != plan or preview.plan_sha256 != expected_sha256:
        raise ValueError("fresh rollover evidence no longer matches the reviewed plan")
    if not preview.plan_complete or not preview.source_eligible:
        blockers = ", ".join(preview.observation["blockers"])
        raise ValueError(f"fresh rollover gates refused activation: {blockers}")
    if not preview.activation_available:
        raise ValueError("rollover activation is unavailable in this build")


def _checkpoint_database(database_file: Path) -> None:
    store = Store(database_file)
    try:
        store.execute("CHECKPOINT")
    finally:
        store.close()


def _require_backup_bound_to_plan(
    root: Path,
    backup: BackupResult,
    plan: dict[str, Any],
) -> None:
    manifest = json.loads((backup.path / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        raise ValueError("rollover backup manifest has no file evidence")
    by_path = {
        str(item.get("path")): str(item.get("sha256")) for item in entries if isinstance(item, dict)
    }
    for label, source_value, expected_hash in (
        (
            "forward trial",
            plan["predecessor"]["trial_path"],
            plan["predecessor"]["trial_file_sha256"],
        ),
        (
            "paper account",
            plan["account"]["account_path"],
            plan["account"]["account_file_sha256"],
        ),
        (
            "predecessor proposal",
            plan["predecessor"]["proposal_path"],
            plan["predecessor"]["proposal_file_sha256"],
        ),
    ):
        source = _absolute_path(root, Path(str(source_value)))
        try:
            relative = source.relative_to(root / "data" / "paper")
        except ValueError as exc:
            raise ValueError(f"{label} is outside the backup paper namespace") from exc
        if by_path.get((Path("paper") / relative).as_posix()) != expected_hash:
            raise ValueError(f"verified backup is not bound to the reviewed {label}")


def _backup_evidence(root: Path, backup: BackupResult) -> dict[str, Any]:
    try:
        relative = backup.path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("rollover backup must be inside the project root") from exc
    return {
        "path": relative,
        "manifest_sha256": backup.manifest_sha256,
        "files": backup.files,
        "bytes": backup.bytes,
    }


def _build_candidate(
    root: Path,
    plan: dict[str, Any],
    database_file: Path,
    backup: BackupResult,
    paths: dict[str, Path],
    *,
    attempt_id: str,
    journal: Path,
) -> dict[str, Any]:
    successor = plan["successor"]
    predecessor = plan["predecessor"]
    generated_at = _utc_now()
    _require_activation_margin(successor["must_be_generated_before"], generated_at)
    blueprint = _json_mapping(
        successor["normalized_proposal_blueprint"],
        label="successor proposal blueprint",
    )
    risk_policy = PortfolioRiskPolicy(
        **_json_mapping(blueprint.get("risk_policy"), label="successor risk policy")
    )
    with TemporaryDirectory(prefix="aios-rollover-activation-") as temporary:
        temporary_root = Path(temporary)
        proposal_path = temporary_root / "proposal.json"
        trial_path = temporary_root / "trial.json"
        with store_scope(database_file, read_only=True) as store:
            proposal = create_paper_proposal(
                paths["account"],
                proposal_path,
                date.fromisoformat(str(successor["decision_date"])),
                store,
                top_n=int(successor["top_n"]),
                risk_policy=risk_policy,
                now=generated_at,
            )
        normalized = _json_mapping(proposal.payload, label="successor proposal")
        normalized["proposal_id"] = "<generated-at-activation>"
        normalized["generated_at"] = "<generated-at-activation>"
        if normalized != blueprint:
            raise ValueError("successor proposal changed after the reviewed plan")
        if (
            paper_proposal_timing_status(proposal.payload, now=_utc_now())["status"]
            != "waiting_for_scheduled_close"
        ):
            raise ValueError("successor proposal is no longer prospective")

        trial = create_forward_trial(
            root,
            trial_path,
            paths["account"],
            proposal_path,
            confirm=True,
            now=generated_at,
            policy_files=tuple(successor["policy_files"]),
        )
        payload = _json_mapping(trial.payload, label="successor forward trial")
        records = payload.get("proposals")
        if not isinstance(records, list) or len(records) != 1:
            raise ValueError("successor forward trial registry is invalid")
        records[0]["path"] = _relative_path(root, paths["successor_proposal"])
        lineage = {
            "schema_version": ROLLOVER_LINEAGE_SCHEMA_VERSION,
            "plan_sha256": canonical_payload_sha256(plan),
            "rollover_key": plan["rollover_key"],
            "attempt_id": attempt_id,
            "journal_path": _relative_path(root, journal),
            "predecessor_trial_id": predecessor["trial_id"],
            "predecessor_trial_payload_sha256": predecessor["trial_payload_sha256"],
            "predecessor_trial_file_sha256": predecessor["trial_file_sha256"],
            "predecessor_proposal_id": predecessor["proposal_id"],
            "predecessor_proposal_payload_sha256": (predecessor["proposal_payload_sha256"]),
            "trial_archive_path": predecessor["trial_archive_path"],
            "proposal_archive_path": predecessor["proposal_archive_path"],
            "backup_manifest_sha256": backup.manifest_sha256,
            "disposition": dict(predecessor["disposition"]),
        }
        payload["rollover_lineage"] = lineage
        payload["audit_events"] = [
            *payload.get("audit_events", []),
            {
                "event": "forward_rollover_prepared",
                "at": _timestamp(generated_at),
                "plan_sha256": canonical_payload_sha256(plan),
                "predecessor_trial_id": predecessor["trial_id"],
            },
        ]
        rewritten = _write_forward_document(trial_path, payload, replace=True)
        proposal_bytes = proposal_path.read_bytes()
        trial_bytes = trial_path.read_bytes()
        proposal_file_sha = hashlib.sha256(proposal_bytes).hexdigest()
        trial_file_sha = hashlib.sha256(trial_bytes).hexdigest()
        return {
            "proposal_bytes": proposal_bytes,
            "trial_bytes": trial_bytes,
            "successor_proposal_file_sha256": proposal_file_sha,
            "successor_proposal_payload_sha256": proposal.payload_sha256,
            "successor_proposal_id": proposal.payload["proposal_id"],
            "successor_trial_file_sha256": trial_file_sha,
            "successor_trial_payload_sha256": rewritten.payload_sha256,
            "successor_trial_id": rewritten.payload["trial_id"],
            "lineage": lineage,
            "output_file_sha256": {
                "trial_archive": predecessor["trial_file_sha256"],
                "proposal_archive": predecessor["proposal_file_sha256"],
                "successor_proposal": proposal_file_sha,
                "staged_trial": trial_file_sha,
            },
        }


def _prepared_state(
    root: Path,
    plan: dict[str, Any],
    paths: dict[str, Path],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    return {
        "paths": {key: _relative_path(root, value) for key, value in sorted(paths.items())},
        "output_file_sha256": dict(candidate["output_file_sha256"]),
        "successor_proposal_payload_sha256": (candidate["successor_proposal_payload_sha256"]),
        "successor_proposal_id": candidate["successor_proposal_id"],
        "successor_trial_file_sha256": candidate["successor_trial_file_sha256"],
        "successor_trial_payload_sha256": candidate["successor_trial_payload_sha256"],
        "successor_trial_id": candidate["successor_trial_id"],
        "lineage": candidate["lineage"],
        "account_execution_registry_sha256": (plan["account"]["execution_registry_sha256"]),
    }


def _publish_rollover_outputs(
    root: Path,
    paths: dict[str, Path],
    candidate: dict[str, Any],
) -> None:
    _publish_bytes_once(
        root,
        paths["trial_archive"],
        paths["active_trial"].read_bytes(),
        label="predecessor trial archive",
    )
    _publish_bytes_once(
        root,
        paths["proposal_archive"],
        paths["predecessor_proposal"].read_bytes(),
        label="predecessor proposal archive",
    )
    _publish_bytes_once(
        root,
        paths["successor_proposal"],
        candidate["proposal_bytes"],
        label="successor proposal",
    )
    _publish_bytes_once(
        root,
        paths["staged_trial"],
        candidate["trial_bytes"],
        label="staged successor trial",
    )
    _verify_output_hashes(root, paths, candidate)


def _write_followup_phase(
    journal: Path,
    prepared: RolloverAttemptDocument,
    *,
    phase: str,
    state: dict[str, Any],
) -> RolloverAttemptDocument:
    return write_attempt_phase(
        journal,
        build_attempt_payload(
            attempt_id=str(prepared.payload["attempt_id"]),
            rollover_key=str(prepared.payload["rollover_key"]),
            plan_sha256=str(prepared.payload["plan_sha256"]),
            phase=phase,
            recorded_at=_utc_now(),
            prepared_payload_sha256=prepared.payload_sha256,
            state=state,
        ),
    )


def _final_activation_recheck(
    root: Path,
    plan_file: Path,
    plan: dict[str, Any],
    expected_sha256: str,
    database_file: Path,
    paths: dict[str, Path],
    candidate: dict[str, Any],
) -> None:
    loaded = read_forward_rollover_plan(plan_file)
    if loaded != plan or canonical_payload_sha256(loaded) != expected_sha256:
        raise ValueError("reviewed rollover plan changed before activation")
    _require_plan_sources_unchanged(root, plan)
    _require_activation_margin(plan["successor"]["must_be_generated_before"], _utc_now())
    _require_fresh_final_gates(root, plan, database_file, paths)
    _require_only_planned_orphan(root, plan, paths["successor_proposal"])
    _verify_output_hashes(root, paths, candidate)


def _require_fresh_final_gates(
    root: Path,
    plan: dict[str, Any],
    database_file: Path,
    paths: dict[str, Path],
) -> None:
    checked = _utc_now()
    successor_date = date.fromisoformat(str(plan["successor"]["decision_date"]))
    trial = read_forward_trial(paths["active_trial"])
    with store_scope(database_file, read_only=True) as store:
        readiness = _json_mapping(
            _assess_fresh_readiness(successor_date, store),
            label="fresh readiness evidence",
        )
    blockers: set[str] = set()
    _apply_readiness_gates(
        readiness,
        successor_date,
        expected_universe=str(trial.payload.get("universe_id") or ""),
        blockers=blockers,
    )
    if readiness != plan["successor"]["readiness_evidence"]:
        blockers.add("successor_readiness_changed_after_review")
    account = read_paper_document(paths["account"], expected_kind=ACCOUNT_DOCUMENT_KIND)
    source = read_paper_document(
        paths["predecessor_proposal"],
        expected_kind=PROPOSAL_DOCUMENT_KIND,
    )
    source_record = next(
        (
            row
            for row in trial.payload.get("proposals", [])
            if isinstance(row, dict)
            and row.get("proposal_id") == plan["predecessor"]["proposal_id"]
            and row.get("payload_sha256") == plan["predecessor"]["proposal_payload_sha256"]
        ),
        None,
    )
    if source_record is None:
        raise ValueError("predecessor proposal registry changed before activation")
    _apply_preflight_gates(
        _verified_preflight(_assess_fresh_operator_preflight(checked)),
        checked=checked,
        root=root,
        trial=trial.payload,
        trial_file=paths["active_trial"],
        account=account.payload,
        account_payload_sha256=account.payload_sha256,
        account_file=paths["account"],
        source_record=source_record,
        source_proposal_file=paths["predecessor_proposal"],
        successor_decision_date=successor_date,
        blockers=blockers,
    )
    if source.payload_sha256 != plan["predecessor"]["proposal_payload_sha256"]:
        blockers.add("predecessor_proposal_changed_before_activation")
    if blockers:
        raise ValueError("final rollover gates refused activation: " + ", ".join(sorted(blockers)))


def _require_only_planned_orphan(
    root: Path,
    plan: dict[str, Any],
    successor_proposal: Path,
) -> None:
    trial_file = root / str(plan["predecessor"]["trial_path"])
    trial = read_forward_trial(trial_file)
    records = _registered_proposal_records(trial.payload)
    global_records = _global_registered_proposal_records(root, trial_file, records)
    unique_records: dict[str, dict[str, Any]] = {}
    for record in global_records:
        proposal_id = str(record["proposal_id"])
        existing = unique_records.get(proposal_id)
        if existing is not None and existing != record:
            raise ValueError("proposal ID has conflicting global registration evidence")
        unique_records[proposal_id] = record
    blockers: set[str] = set()
    registered = _registered_proposal_files(
        root,
        list(unique_records.values()),
        blockers,
    )
    if blockers:
        raise ValueError("registered proposal evidence changed before activation")
    allowed = set(registered.values()) | {successor_proposal}
    proposal_root = root / "data" / "paper" / "proposals"
    for candidate in sorted(proposal_root.rglob("*.json")):
        current = _governed_project_file(root, candidate, label="governed proposal")
        if current not in allowed:
            raise ValueError(
                "unexpected governed proposal appeared before activation: "
                f"{current.relative_to(root)}"
            )


def _verify_output_hashes(
    root: Path,
    paths: dict[str, Path],
    candidate: dict[str, Any],
) -> None:
    for key, expected in candidate["output_file_sha256"].items():
        current = _governed_project_file(root, paths[key], label=key.replace("_", " "))
        if _file_sha256(current) != expected:
            raise RuntimeError(f"rollover output changed: {key}")


def _verify_successor(
    root: Path,
    plan: dict[str, Any],
    paths: dict[str, Path],
    candidate: dict[str, Any],
) -> None:
    active = _governed_project_file(root, paths["active_trial"], label="active successor")
    if _file_sha256(active) != candidate["successor_trial_file_sha256"]:
        raise RuntimeError("active successor bytes are not the prepared bytes")
    document = read_forward_trial(active)
    if (
        document.payload_sha256 != candidate["successor_trial_payload_sha256"]
        or document.payload.get("trial_id") != candidate["successor_trial_id"]
        or document.payload.get("rollover_lineage") != candidate["lineage"]
    ):
        raise RuntimeError("active successor identity is inconsistent")
    status = assess_forward_trial(
        root,
        active,
        paths["account"],
        policy_files=tuple(plan["successor"]["policy_files"]),
    )
    if not status.ready:
        raise RuntimeError("activated successor failed verification: " + "; ".join(status.issues))
    expected = {
        "account": plan["account"]["account_file_sha256"],
        "trial_archive": plan["predecessor"]["trial_file_sha256"],
        "proposal_archive": plan["predecessor"]["proposal_file_sha256"],
        "successor_proposal": candidate["successor_proposal_file_sha256"],
    }
    for key, digest in expected.items():
        if _file_sha256(paths[key]) != digest:
            raise RuntimeError(f"{key.replace('_', ' ')} changed during rollover")


def _terminal_success_state(
    plan: dict[str, Any],
    backup: BackupResult,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    return {
        "authority": "successor",
        "active_trial_file_sha256": candidate["successor_trial_file_sha256"],
        "active_trial_payload_sha256": candidate["successor_trial_payload_sha256"],
        "account_file_sha256": plan["account"]["account_file_sha256"],
        "account_execution_registry_sha256": (plan["account"]["execution_registry_sha256"]),
        "backup_manifest_sha256": backup.manifest_sha256,
        "paper_fill_recorded": False,
        "broker_order_sent": False,
        "recovered_after_process_interruption": False,
    }


def _rollback_started_attempt(
    root: Path,
    plan: dict[str, Any],
    paths: dict[str, Path],
    candidate: dict[str, Any],
    journal: Path,
    prepared: RolloverAttemptDocument,
    *,
    error_type: str,
) -> RolloverAttemptState:
    active_hash = _file_sha256(paths["active_trial"])
    predecessor_hash = str(plan["predecessor"]["trial_file_sha256"])
    successor_hash = str(candidate["successor_trial_file_sha256"])
    if active_hash == successor_hash:
        archive = _governed_project_file(
            root,
            paths["trial_archive"],
            label="predecessor trial archive",
        )
        if _file_sha256(archive) != predecessor_hash:
            raise RuntimeError("predecessor archive cannot support exact rollback")
        _atomic_replace_bytes(root, paths["active_trial"], archive.read_bytes())
    elif active_hash != predecessor_hash:
        raise RuntimeError("active trial is neither predecessor nor prepared successor")
    _remove_exact_outputs(root, paths, candidate)
    terminal = _write_followup_phase(
        journal,
        prepared,
        phase="recovered_rolled_back",
        state=_terminal_rollback_state(plan, prepared, error_type),
    )
    state = validate_attempt_directory(journal)
    if state.latest.path != terminal.path:
        raise RuntimeError("rollover rollback terminal evidence is inconsistent")
    return state


def _terminal_rollback_state(
    plan: dict[str, Any],
    prepared: RolloverAttemptDocument,
    error_type: str,
) -> dict[str, Any]:
    return {
        "authority": "predecessor",
        "active_trial_file_sha256": plan["predecessor"]["trial_file_sha256"],
        "error_type": error_type,
        "outputs_removed": True,
        "account_file_sha256": plan["account"]["account_file_sha256"],
        "account_execution_registry_sha256": (plan["account"]["execution_registry_sha256"]),
        "backup_manifest_sha256": prepared.payload["backup"]["manifest_sha256"],
    }


def _recover_incomplete(
    root: Path,
    plan: dict[str, Any],
    paths: dict[str, Path],
    state: RolloverAttemptState,
) -> RolloverAttemptState:
    prepared = state.prepared
    candidate = _candidate_from_prepared(prepared)
    active_hash = _file_sha256(paths["active_trial"])
    predecessor_hash = str(plan["predecessor"]["trial_file_sha256"])
    successor_hash = str(candidate["successor_trial_file_sha256"])
    if active_hash == predecessor_hash:
        _remove_exact_outputs(root, paths, candidate)
        _write_followup_phase(
            state.path,
            prepared,
            phase="recovered_rolled_back",
            state=_terminal_rollback_state(
                plan,
                prepared,
                "process_interruption",
            ),
        )
        return validate_attempt_directory(state.path)
    if active_hash != successor_hash:
        raise RuntimeError("recovery cannot identify an authoritative active trial")

    _verify_recovery_successor(root, plan, paths, candidate)
    phases = {document.phase for document in state.documents}
    if "outputs_published" not in phases:
        _write_followup_phase(
            state.path,
            prepared,
            phase="outputs_published",
            state={
                "output_file_sha256": dict(candidate["output_file_sha256"]),
                "active_trial_file_sha256": successor_hash,
                "recovered_after_process_interruption": True,
            },
        )
    if "active_swapped" not in phases:
        _write_followup_phase(
            state.path,
            prepared,
            phase="active_swapped",
            state={
                "active_trial_file_sha256": successor_hash,
                "active_trial_payload_sha256": (candidate["successor_trial_payload_sha256"]),
                "recovered_after_process_interruption": True,
            },
        )
    success = _terminal_success_state_from_prepared(plan, prepared, candidate)
    success["recovered_after_process_interruption"] = True
    _write_followup_phase(
        state.path,
        prepared,
        phase="verified",
        state=success,
    )
    return validate_attempt_directory(state.path)


def _candidate_from_prepared(
    prepared: RolloverAttemptDocument,
) -> dict[str, Any]:
    state = prepared.payload["state"]
    return {
        key: state[key]
        for key in (
            "output_file_sha256",
            "successor_proposal_payload_sha256",
            "successor_proposal_id",
            "successor_trial_file_sha256",
            "successor_trial_payload_sha256",
            "successor_trial_id",
            "lineage",
        )
    }


def _terminal_success_state_from_prepared(
    plan: dict[str, Any],
    prepared: RolloverAttemptDocument,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    return {
        "authority": "successor",
        "active_trial_file_sha256": candidate["successor_trial_file_sha256"],
        "active_trial_payload_sha256": candidate["successor_trial_payload_sha256"],
        "account_file_sha256": plan["account"]["account_file_sha256"],
        "account_execution_registry_sha256": (plan["account"]["execution_registry_sha256"]),
        "backup_manifest_sha256": prepared.payload["backup"]["manifest_sha256"],
        "paper_fill_recorded": False,
        "broker_order_sent": False,
    }


def _verify_recovery_successor(
    root: Path,
    plan: dict[str, Any],
    paths: dict[str, Path],
    candidate: dict[str, Any],
) -> None:
    for key in ("trial_archive", "proposal_archive", "successor_proposal"):
        current = _governed_project_file(root, paths[key], label=key.replace("_", " "))
        if _file_sha256(current) != candidate["output_file_sha256"][key]:
            raise RuntimeError(f"recovery found changed rollover output: {key}")
    if _file_sha256(paths["account"]) != plan["account"]["account_file_sha256"]:
        raise RuntimeError("paper account changed during incomplete rollover")
    active = read_forward_trial(paths["active_trial"])
    if (
        active.payload_sha256 != candidate["successor_trial_payload_sha256"]
        or active.payload.get("rollover_lineage") != candidate["lineage"]
    ):
        raise RuntimeError("recovered successor lineage is inconsistent")
    proposal = read_paper_document(
        paths["successor_proposal"],
        expected_kind=PROPOSAL_DOCUMENT_KIND,
    )
    if proposal.payload_sha256 != candidate["successor_proposal_payload_sha256"]:
        raise RuntimeError("recovered successor proposal identity is inconsistent")


def _validate_terminal_state(
    root: Path,
    plan: dict[str, Any],
    paths: dict[str, Path],
    state: RolloverAttemptState,
) -> None:
    candidate = _candidate_from_prepared(state.prepared)
    if _file_sha256(paths["account"]) != plan["account"]["account_file_sha256"]:
        raise RuntimeError("terminal rollover evidence does not match the paper account")
    if state.latest.phase == "verified":
        if _file_sha256(paths["active_trial"]) != candidate["successor_trial_file_sha256"]:
            raise RuntimeError("verified rollover does not match the active successor")
        _verify_recovery_successor(root, plan, paths, candidate)
        return
    if _file_sha256(paths["active_trial"]) != plan["predecessor"]["trial_file_sha256"]:
        raise RuntimeError("rolled-back rollover does not match the active predecessor")
    for key in ("trial_archive", "proposal_archive", "successor_proposal", "staged_trial"):
        if paths[key].exists() or paths[key].is_symlink():
            raise RuntimeError(f"rolled-back rollover retained an output: {key}")


def _remove_exact_outputs(
    root: Path,
    paths: dict[str, Path],
    candidate: dict[str, Any],
) -> None:
    for key in ("staged_trial", "successor_proposal", "proposal_archive", "trial_archive"):
        path = paths[key]
        if not path.exists() and not path.is_symlink():
            continue
        current = _governed_project_file(root, path, label=key.replace("_", " "))
        if _file_sha256(current) != candidate["output_file_sha256"][key]:
            raise RuntimeError(f"refusing to remove changed rollover output: {key}")
        current.unlink()
        _fsync_directory(current.parent)


def _recovery_result(
    paths: dict[str, Path],
    state: RolloverAttemptState,
) -> ForwardRolloverRecovery:
    return ForwardRolloverRecovery(
        plan_sha256=str(state.prepared.payload["plan_sha256"]),
        attempt_id=str(state.prepared.payload["attempt_id"]),
        terminal_phase=state.latest.phase,
        active_trial=paths["active_trial"],
        journal_directory=state.path,
        terminal_receipt=state.latest.path,
    )


def _require_no_incomplete_attempts(root: Path) -> None:
    for path in scan_attempt_directories(root):
        state = validate_attempt_directory(path)
        if not state.terminal:
            raise ValueError(
                f"an incomplete rollover attempt requires explicit recovery first: {path}"
            )


def _publish_bytes_once(
    root: Path,
    path: Path,
    payload: bytes,
    *,
    label: str,
) -> None:
    destination = _governed_project_destination(root, path, label=label)
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"{label} already exists: {destination}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_symlink_ancestors(root, destination, label=label)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(f"{label} must be one unaliased regular file")
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(destination.parent)


def _atomic_replace_bytes(root: Path, destination: Path, payload: bytes) -> None:
    target = _governed_project_file(root, destination, label="active trial")
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.recovery")
    try:
        _publish_bytes_once(
            root,
            temporary,
            payload,
            label="active trial recovery",
        )
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _require_activation_margin(value: Any, now: datetime) -> None:
    if now + timedelta(seconds=MINIMUM_ACTIVATION_MARGIN_SECONDS) >= _parse_timestamp(value):
        raise ValueError("successor proposal-generation margin is no longer sufficient")


def _require_sha256(value: str) -> None:
    if not _is_lower_sha256(value):
        raise ValueError("rollover plan SHA-256 must be 64 lowercase hexadecimal characters")


def _assess_fresh_readiness(decision_date: date, store: Store) -> dict[str, Any]:
    from aios.readiness import assess_us_readiness

    return assess_us_readiness(
        decision_date,
        purpose="paper",
        store=store,
    ).to_dict()


def _assess_fresh_operator_preflight(now: datetime) -> dict[str, Any]:
    from aios.operator_preflight import assess_operator_preflight

    return assess_operator_preflight(now=now).to_envelope()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

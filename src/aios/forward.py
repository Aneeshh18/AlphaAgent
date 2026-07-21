"""Checksum-protected policy freeze for untouched U.S. forward monitoring.

The market database is expected to change as new public information arrives, so
the forward trial deliberately does not hash DuckDB.  It freezes the strategy,
risk, cost, tax, calendar, readiness, and paper-workflow source files plus the
reviewed operating configuration.  Registered proposals remain separate,
checksum-validated evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from aios.paper import (
    ACCOUNT_DOCUMENT_KIND,
    PROPOSAL_DOCUMENT_KIND,
    canonical_payload_sha256,
    read_paper_document,
)

FORWARD_DOCUMENT_KIND = "aios.forward-trial"
FORWARD_SCHEMA_VERSION = 1
DEFAULT_FORWARD_RELATIVE_PATH = Path("data/paper/us_qv_forward_trial.json")

# Keep this list explicit.  Adding/removing a policy dependency is itself a
# reviewed policy change and therefore requires a new forward trial.
FORWARD_POLICY_FILES = (
    "src/aios/backtest/costs.py",
    "src/aios/backtest/portfolio.py",
    "src/aios/factors/common.py",
    "src/aios/factors/composite.py",
    "src/aios/factors/policy.py",
    "src/aios/factors/quality.py",
    "src/aios/factors/value.py",
    "src/aios/forward.py",
    "src/aios/macro/regime.py",
    "src/aios/market_calendar.py",
    "src/aios/paper.py",
    "src/aios/readiness.py",
    "src/aios/risk/policy.py",
)


@dataclass(frozen=True)
class ForwardTrialDocument:
    """One verified local forward-trial envelope."""

    path: Path
    payload: dict[str, Any]
    payload_sha256: str


@dataclass(frozen=True)
class ForwardTrialStatus:
    """Current policy/configuration comparison against the frozen baseline."""

    trial_id: str
    active: bool
    policy_unchanged: bool
    registered_proposals: int
    issues: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.active and not self.issues


def create_forward_trial(
    project_root: Path,
    path: Path,
    account_path: Path,
    proposal_path: Path,
    *,
    confirm: bool,
    now: datetime | None = None,
    policy_files: tuple[str, ...] = FORWARD_POLICY_FILES,
) -> ForwardTrialDocument:
    """Freeze reviewed policy/configuration without freezing changing market data."""
    if not confirm:
        raise ValueError("explicit --confirm-freeze approval is required")
    destination = Path(path)
    if destination.exists():
        raise ValueError(f"forward trial already exists: {destination}")

    root = Path(project_root).resolve()
    account = read_paper_document(account_path, expected_kind=ACCOUNT_DOCUMENT_KIND)
    proposal = read_paper_document(proposal_path, expected_kind=PROPOSAL_DOCUMENT_KIND)
    if proposal.payload.get("account_id") != account.payload.get("account_id"):
        raise ValueError("proposal belongs to a different paper account")
    if proposal.payload.get("account_payload_sha256") != account.payload_sha256:
        raise ValueError("paper account changed after the baseline proposal")
    if proposal.payload.get("mode") != "simulation_only":
        raise ValueError("only simulation-only proposals can start a forward trial")
    if proposal.payload.get("status") != "approved_for_supervised_simulation":
        raise ValueError("baseline proposal is not approved for supervised simulation")

    files = policy_file_hashes(root, policy_files=policy_files)
    frozen_at = _timestamp(now)
    configuration = _proposal_configuration(account.payload, proposal.payload)
    payload: dict[str, Any] = {
        "forward_schema_version": FORWARD_SCHEMA_VERSION,
        "trial_id": f"us-qv-forward-{uuid4().hex[:12]}",
        "status": "active",
        "frozen_at": frozen_at,
        "observation_start_decision_date": proposal.payload["decision_date"],
        "market": proposal.payload["market"],
        "universe_id": proposal.payload["universe_id"],
        "strategy": proposal.payload["strategy"],
        "account_id": account.payload["account_id"],
        "account_baseline_payload_sha256": account.payload_sha256,
        "policy_files": files,
        "policy_bundle_sha256": canonical_payload_sha256({"files": files}),
        "frozen_configuration": configuration,
        "proposals": [_proposal_record(root, proposal.path, proposal)],
        "audit_events": [
            {
                "event": "forward_trial_frozen",
                "at": frozen_at,
                "proposal_id": proposal.payload["proposal_id"],
            }
        ],
        "notice": (
            "Simulation-only forward evidence. Market data may advance, but changing a "
            "frozen policy or configuration invalidates this trial baseline."
        ),
    }
    return _write_forward_document(destination, payload)


def read_forward_trial(path: Path) -> ForwardTrialDocument:
    """Read and checksum-validate one forward-trial document."""
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"forward trial does not exist: {source}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"forward trial is unreadable: {source}") from exc
    if not isinstance(raw, dict) or raw.get("document_kind") != FORWARD_DOCUMENT_KIND:
        raise ValueError("unsupported forward-trial document")
    payload = raw.get("payload")
    stored_digest = raw.get("payload_sha256")
    if not isinstance(payload, dict) or not isinstance(stored_digest, str):
        raise ValueError("invalid forward-trial envelope")
    if payload.get("forward_schema_version") != FORWARD_SCHEMA_VERSION:
        raise ValueError("unsupported forward-trial schema")
    actual_digest = canonical_payload_sha256(payload)
    if stored_digest != actual_digest:
        raise ValueError("forward-trial checksum mismatch; restore or recreate it")
    return ForwardTrialDocument(source, payload, actual_digest)


def assess_forward_trial(
    project_root: Path,
    path: Path,
    account_path: Path,
    *,
    policy_files: tuple[str, ...] | None = None,
) -> ForwardTrialStatus:
    """Compare current policy/configuration and proposal evidence to the freeze."""
    root = Path(project_root).resolve()
    document = read_forward_trial(path)
    files = tuple(document.payload["policy_files"]) if policy_files is None else policy_files
    issues = _base_issues(root, document.payload, account_path, files)
    issues.extend(_unregistered_proposal_issues(root, document.payload))
    current_files = policy_file_hashes(root, policy_files=files)
    policy_unchanged = current_files == document.payload["policy_files"]
    return ForwardTrialStatus(
        trial_id=str(document.payload["trial_id"]),
        active=document.payload.get("status") == "active",
        policy_unchanged=policy_unchanged,
        registered_proposals=len(document.payload.get("proposals", [])),
        issues=tuple(issues),
    )


def register_forward_proposal(
    project_root: Path,
    trial_path: Path,
    account_path: Path,
    proposal_path: Path,
    *,
    now: datetime | None = None,
) -> ForwardTrialDocument:
    """Append one checksum-verified proposal to an unchanged active trial."""
    root = Path(project_root).resolve()
    trial = read_forward_trial(trial_path)
    files = tuple(trial.payload["policy_files"])
    issues = _base_issues(root, trial.payload, account_path, files)
    if issues:
        raise ValueError("forward trial is not unchanged: " + "; ".join(issues))

    proposal = read_paper_document(proposal_path, expected_kind=PROPOSAL_DOCUMENT_KIND)
    _validate_proposal_configuration(trial.payload, proposal.payload)
    records = list(trial.payload.get("proposals", []))
    if any(row.get("payload_sha256") == proposal.payload_sha256 for row in records):
        return trial
    if any(row.get("proposal_id") == proposal.payload.get("proposal_id") for row in records):
        raise ValueError("registered proposal ID has different contents")

    timestamp = _timestamp(now)
    updated = dict(trial.payload)
    updated["proposals"] = [*records, _proposal_record(root, proposal.path, proposal)]
    updated["audit_events"] = [
        *trial.payload.get("audit_events", []),
        {
            "event": "proposal_registered",
            "at": timestamp,
            "proposal_id": proposal.payload["proposal_id"],
        },
    ]
    updated["updated_at"] = timestamp
    return _write_forward_document(trial.path, updated, replace=True)


def require_registered_forward_proposal(
    project_root: Path,
    trial_path: Path,
    account_path: Path,
    proposal_path: Path,
) -> ForwardTrialStatus:
    """Fail before simulation if policy drifted or proposal evidence is unregistered."""
    status = assess_forward_trial(project_root, trial_path, account_path)
    if not status.ready:
        raise ValueError("forward trial is not unchanged: " + "; ".join(status.issues))
    proposal = read_paper_document(proposal_path, expected_kind=PROPOSAL_DOCUMENT_KIND)
    trial = read_forward_trial(trial_path)
    if not any(
        row.get("proposal_id") == proposal.payload.get("proposal_id")
        and row.get("payload_sha256") == proposal.payload_sha256
        for row in trial.payload.get("proposals", [])
    ):
        raise ValueError("proposal is not registered in the active forward trial")
    return status


def policy_file_hashes(
    project_root: Path,
    *,
    policy_files: tuple[str, ...] = FORWARD_POLICY_FILES,
) -> dict[str, str]:
    """Return deterministic SHA-256 evidence for every frozen source file."""
    root = Path(project_root).resolve()
    hashes: dict[str, str] = {}
    for relative in sorted(set(policy_files)):
        source = (root / relative).resolve()
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"policy file escapes project root: {relative}") from exc
        if not source.is_file():
            raise ValueError(f"frozen policy file is missing: {relative}")
        hashes[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
    if not hashes:
        raise ValueError("forward trial needs at least one policy file")
    return hashes


def _base_issues(
    root: Path,
    payload: dict[str, Any],
    account_path: Path,
    policy_files: tuple[str, ...],
) -> list[str]:
    issues: list[str] = []
    if payload.get("status") != "active":
        issues.append("trial is not active")
    try:
        current_files = policy_file_hashes(root, policy_files=policy_files)
    except ValueError as exc:
        issues.append(str(exc))
    else:
        if current_files != payload.get("policy_files"):
            issues.append("frozen policy files changed")
        if canonical_payload_sha256({"files": current_files}) != payload.get(
            "policy_bundle_sha256"
        ):
            issues.append("frozen policy bundle checksum changed")

    try:
        account = read_paper_document(account_path, expected_kind=ACCOUNT_DOCUMENT_KIND)
    except ValueError as exc:
        issues.append(str(exc))
    else:
        expected = payload.get("frozen_configuration", {})
        current = {
            "transaction_costs": account.payload.get("portfolio", {}).get(
                "transaction_costs"
            ),
            "tax_policy": account.payload.get("portfolio", {}).get("tax_policy"),
        }
        for key, value in current.items():
            if value != expected.get(key):
                issues.append(f"frozen {key.replace('_', ' ')} changed")

    for record in payload.get("proposals", []):
        proposal_path = root / str(record.get("path", ""))
        try:
            proposal = read_paper_document(
                proposal_path, expected_kind=PROPOSAL_DOCUMENT_KIND
            )
        except ValueError as exc:
            issues.append(str(exc))
            continue
        if proposal.payload_sha256 != record.get("payload_sha256"):
            issues.append(f"registered proposal changed: {record.get('proposal_id')}")
    return issues


def _unregistered_proposal_issues(root: Path, payload: dict[str, Any]) -> list[str]:
    registered = {
        (row.get("proposal_id"), row.get("payload_sha256"))
        for row in payload.get("proposals", [])
    }
    frozen_at = datetime.fromisoformat(str(payload["frozen_at"]).replace("Z", "+00:00"))
    issues: list[str] = []
    observation_start = date.fromisoformat(str(payload["observation_start_decision_date"]))
    for path in sorted((root / "data/paper/proposals").glob("us-qv-*.json")):
        try:
            proposal = read_paper_document(path, expected_kind=PROPOSAL_DOCUMENT_KIND)
            generated = datetime.fromisoformat(
                str(proposal.payload["generated_at"]).replace("Z", "+00:00")
            )
        except (KeyError, ValueError) as exc:
            try:
                filename_date = date.fromisoformat(path.stem.removeprefix("us-qv-"))
            except ValueError:
                continue
            if filename_date >= observation_start:
                issues.append(f"proposal cannot be verified: {path.name}: {exc}")
            continue
        identity = (proposal.payload.get("proposal_id"), proposal.payload_sha256)
        if (
            generated >= frozen_at
            and proposal.payload.get("account_id") == payload.get("account_id")
            and identity not in registered
        ):
            issues.append(f"unregistered proposal exists: {path.name}")
    return issues


def _validate_proposal_configuration(
    trial: dict[str, Any], proposal: dict[str, Any]
) -> None:
    for key in ("account_id", "market", "universe_id", "strategy"):
        if proposal.get(key) != trial.get(key):
            raise ValueError(f"proposal changed frozen {key.replace('_', ' ')}")
    if date.fromisoformat(str(proposal["decision_date"])) < date.fromisoformat(
        str(trial["observation_start_decision_date"])
    ):
        raise ValueError("proposal predates the forward observation window")
    expected = trial["frozen_configuration"]
    if proposal.get("risk_policy") != expected.get("risk_policy"):
        raise ValueError("proposal changed frozen risk policy")
    target_count = len(proposal.get("targets", []))
    top_n = int(expected["top_n"])
    if proposal.get("status") == "approved_for_supervised_simulation":
        if target_count != top_n:
            raise ValueError("approved proposal changed frozen target count")
    elif target_count > top_n:
        raise ValueError("blocked proposal exceeds frozen target count")


def _proposal_configuration(account: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    portfolio = account.get("portfolio", {})
    return {
        "top_n": len(proposal.get("targets", [])),
        "risk_policy": proposal.get("risk_policy"),
        "transaction_costs": portfolio.get("transaction_costs"),
        "tax_policy": portfolio.get("tax_policy"),
    }


def _proposal_record(
    root: Path, path: Path, proposal: Any
) -> dict[str, Any]:
    resolved = Path(path).resolve()
    try:
        stored_path = resolved.relative_to(root).as_posix()
    except ValueError:
        stored_path = str(resolved)
    return {
        "proposal_id": proposal.payload["proposal_id"],
        "decision_date": proposal.payload["decision_date"],
        "generated_at": proposal.payload["generated_at"],
        "path": stored_path,
        "payload_sha256": proposal.payload_sha256,
        "status": proposal.payload["status"],
    }


def _write_forward_document(
    path: Path,
    payload: dict[str, Any],
    *,
    replace: bool = False,
) -> ForwardTrialDocument:
    destination = Path(path)
    if destination.exists() and not replace:
        raise ValueError(f"forward trial already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = canonical_payload_sha256(payload)
    envelope = {
        "document_kind": FORWARD_DOCUMENT_KIND,
        "payload_sha256": digest,
        "payload": payload,
    }
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(envelope, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return ForwardTrialDocument(destination, payload, digest)


def _timestamp(value: datetime | None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")

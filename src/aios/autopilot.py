"""Scheduled paper-trial cycle: report the due proposal, then stage the next one.

The paper trial had never recorded a single simulated fill. Every proposal
reached `Registered`, its execution window opened while nothing was running to
act on it, and it expired at the following session's open — after which a new
proposal is refused because the expired one is still unresolved. Cash sat at
its initial balance indefinitely, so the forward trial produced no evidence at
all.

The window arithmetic in `paper.py` is what makes a single daily run
sufficient. For a proposal whose entry session is E:

- it must be *created* before E's regular open, so targets are frozen before
  any of that session's movement is observable; and
- it may be *executed* only between E's conservative close and the next
  session's regular open.

A run positioned after a close and before the next open therefore sits inside
both windows at once, for two different proposals: it can report the one whose
entry session just closed for manual confirmation, and create the one whose
entry session is about to open. The installed timer fires at 02:02
America/New_York, which satisfies this. It deliberately does not confirm or
record the due fill.

This module orchestrates existing governed operations and implements none of
them. Readiness, prospective-timing, registered-proposal, checksum and
frozen-policy gates all continue to run inside `paper.py` and `forward.py`,
which are part of the frozen policy bundle and are not modified here. Every
step is skipped rather than forced when its own precondition fails, so an
unattended run can decline to act but can never widen what is permitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from aios.config import settings
from aios.forward import (
    DEFAULT_FORWARD_RELATIVE_PATH,
    assess_forward_trial,
    read_forward_trial,
    register_forward_proposal,
)
from aios.paper import (
    ACCOUNT_DOCUMENT_KIND,
    PROPOSAL_DOCUMENT_KIND,
    create_paper_proposal,
    default_proposal_path,
    execute_paper_proposal,
    latest_paper_decision_date,
    paper_proposal_timing_status,
    read_paper_document,
)
from aios.storage.store import Store

DEFAULT_ACCOUNT_RELATIVE_PATH = Path("data/paper/us_qv_sandbox.json")


@dataclass
class AutopilotResult:
    """What one unattended cycle did, and why it declined anything it skipped."""

    executed_proposal_id: str | None = None
    execution_detail: str | None = None
    created_proposal_path: Path | None = None
    creation_detail: str | None = None
    skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "executed_proposal_id": self.executed_proposal_id,
            "execution_detail": self.execution_detail,
            "created_proposal_path": (
                str(self.created_proposal_path) if self.created_proposal_path else None
            ),
            "creation_detail": self.creation_detail,
            "skipped": list(self.skipped),
        }


def _project_path(path: Path) -> Path:
    return path if path.is_absolute() else settings.project_root / path


def _registered_proposal_paths(trial_payload: dict[str, Any], root: Path) -> list[Path]:
    paths: list[Path] = []
    for record in trial_payload.get("proposals", []):
        relative = record.get("path")
        if relative:
            paths.append(_project_path(Path(str(relative))))
    return paths


def has_unresolved_registered_proposal(
    trial_payload: dict[str, Any],
    account_payload: dict[str, Any],
) -> bool:
    """Fail closed unless every registered proposal has an exact execution record.

    Mirrors the identical guard the interactive `paper-propose` command
    applies. An unattended cycle must not be able to create a proposal in a
    situation where a human running the equivalent command would be refused —
    otherwise automation quietly becomes the weakest path into governed state.
    """
    proposals = trial_payload.get("proposals")
    executions = account_payload.get("executions")
    if not isinstance(proposals, list):
        raise ValueError("forward proposal lifecycle evidence is invalid")
    if not isinstance(executions, list):
        raise ValueError("paper execution lifecycle evidence is invalid")

    executed_ids: set[str] = set()
    for execution in executions:
        if not isinstance(execution, dict):
            raise ValueError("paper execution lifecycle evidence is invalid")
        proposal_id = execution.get("proposal_id")
        if not isinstance(proposal_id, str) or not proposal_id.strip():
            raise ValueError("paper execution lifecycle evidence is invalid")
        executed_ids.add(proposal_id)

    for proposal in proposals:
        if not isinstance(proposal, dict):
            raise ValueError("forward proposal lifecycle evidence is invalid")
        proposal_id = proposal.get("proposal_id")
        if not isinstance(proposal_id, str) or not proposal_id.strip():
            raise ValueError("forward proposal lifecycle evidence is invalid")
        if proposal_id not in executed_ids:
            return True
    return False


def run_autopilot_cycle(
    *,
    account_path: Path | None = None,
    trial_path: Path | None = None,
    store: Store,
    now: datetime | None = None,
    top_n: int = 10,
    confirm_simulated: bool = False,
) -> AutopilotResult:
    """Execute any due registered proposal, then stage the next one.

    ``confirm_simulated`` is passed straight through to the frozen
    `execute_paper_proposal`, which refuses without it, and defaults to False
    so importing or dry-running this module can never record a fill by
    accident.

    The scheduled entry point deliberately does **not** set it. Recording a
    fill is the one always-manual confirmation point in this product, so an
    unattended run stages the next proposal and reports what is due while the
    fill itself waits for a person. The parameter exists for an operator who
    invokes the cycle directly and confirms in the same breath.
    """
    root = settings.project_root
    account = _project_path(account_path or DEFAULT_ACCOUNT_RELATIVE_PATH)
    trial = _project_path(trial_path or DEFAULT_FORWARD_RELATIVE_PATH)
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    result = AutopilotResult()

    if not account.is_file():
        result.skipped.append(f"no paper account at {account}")
        return result
    if not trial.is_file():
        result.skipped.append(f"no forward trial at {trial}")
        return result

    trial_document = read_forward_trial(trial)
    status = assess_forward_trial(root, trial, account)
    if not status.active:
        result.skipped.append(f"forward trial {status.trial_id} is not active")
        return result

    # --- 1. Execute a registered proposal whose window is open ------------
    for proposal_path in _registered_proposal_paths(trial_document.payload, root):
        if not proposal_path.is_file():
            result.skipped.append(f"registered proposal missing: {proposal_path.name}")
            continue
        proposal = read_paper_document(proposal_path, expected_kind=PROPOSAL_DOCUMENT_KIND)
        if proposal.payload.get("already_simulated"):
            continue
        try:
            timing = paper_proposal_timing_status(proposal.payload, now=moment)
        except ValueError as exc:
            # A proposal that was not created prospectively can never be
            # executed. Leave it for the governed rollover path rather than
            # attempting any repair here.
            result.skipped.append(f"{proposal_path.name}: {exc}")
            continue
        if timing["status"] != "execution_window_open":
            result.skipped.append(f"{proposal_path.name}: {timing['status']}")
            continue
        try:
            execution = execute_paper_proposal(
                account,
                proposal_path,
                store,
                confirm_simulated=confirm_simulated,
                now=moment,
            )
        except (ValueError, RuntimeError) as exc:
            # Fail-closed by design: readiness, evidence, risk and checksum
            # gates all surface here and must not be retried or bypassed.
            result.skipped.append(f"{proposal_path.name}: execution refused: {exc}")
        else:
            result.executed_proposal_id = str(proposal.payload.get("proposal_id"))
            result.execution_detail = (
                f"recorded a simulated fill for {result.executed_proposal_id}; "
                f"ending equity {execution.get('ending_equity')}"
            )
        break

    # --- 2. Stage the next proposal --------------------------------------
    # These are the same preconditions `paper-propose` enforces. They are
    # re-read here rather than reused from step 1 because a fill recorded
    # above changes both the account and the trial.
    trial_document = read_forward_trial(trial)
    status = assess_forward_trial(root, trial, account)
    if not status.ready:
        result.creation_detail = (
            "forward trial is not unchanged: " + "; ".join(status.issues)
        )
        return result

    account_document = read_paper_document(account, expected_kind=ACCOUNT_DOCUMENT_KIND)
    if has_unresolved_registered_proposal(trial_document.payload, account_document.payload):
        result.creation_detail = (
            "a registered forward proposal remains unresolved; the governed "
            "rollover path must close it before another can be created"
        )
        return result

    frozen_top_n = int(trial_document.payload["frozen_configuration"]["top_n"])
    if top_n != frozen_top_n:
        result.creation_detail = (
            f"top_n {top_n} differs from the active forward trial's frozen {frozen_top_n}"
        )
        return result

    decision_date = latest_paper_decision_date(store)
    destination = default_proposal_path(root, decision_date)
    if destination.exists():
        result.creation_detail = f"proposal already exists for {decision_date.isoformat()}"
        return result

    try:
        proposal = create_paper_proposal(
            account,
            destination,
            decision_date,
            store,
            top_n=top_n,
        )
    except (ValueError, RuntimeError) as exc:
        result.creation_detail = f"proposal not created: {exc}"
        return result

    try:
        register_forward_proposal(root, trial, account, proposal.path)
    except (ValueError, RuntimeError) as exc:
        result.creation_detail = (
            f"created {proposal.path.name} but registration refused: {exc}"
        )
        return result

    result.created_proposal_path = proposal.path
    result.creation_detail = (
        f"created and registered {proposal.path.name} for the "
        f"{decision_date.isoformat()} decision close"
    )
    return result


def latest_decision_date(store: Store) -> date:
    """Expose the reviewed decision close the next proposal would use."""
    return latest_paper_decision_date(store)

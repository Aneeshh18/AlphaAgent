"""One governed, side-effect-free operator snapshot for AIOS."""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

PREFLIGHT_DOCUMENT_KIND = "aios.operator_preflight"
PREFLIGHT_SCHEMA_VERSION = "operator-preflight.v1"
CAPABILITY_KEYS = (
    "research",
    "proposal_creation",
    "stress_review",
    "paper_recording",
    "operations",
    "real_capital",
)
CapabilityKey = Literal[
    "research",
    "proposal_creation",
    "stress_review",
    "paper_recording",
    "operations",
    "real_capital",
]
ActionKind = Literal["command", "wait", "human_decision"]
Tone = Literal["success", "warning", "danger", "neutral"]


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _blockers(values: Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(
        sorted({value.strip() for value in values if isinstance(value, str) and value.strip()})
    )


def _timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CapabilityState:
    """One scoped answer; AIOS never exposes an ambiguous global READY."""

    key: CapabilityKey
    label: str
    state: str
    available: bool
    detail: str
    blockers: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        """Compatibility alias for reader surfaces that use the older term."""

        return self.available

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "state": self.state,
            "available": self.available,
            "detail": self.detail,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class OperatorAction:
    """Exactly one safe command, explicit wait, or human-only decision."""

    kind: ActionKind
    title: str
    detail: str
    destination: Literal["research", "paper", "system"]
    tone: Tone
    command: str | None = None
    cta_label: str = "Review"

    def __post_init__(self) -> None:
        if self.kind == "command" and not _text(self.command):
            raise ValueError("a command action requires one non-empty command")
        if self.kind != "command" and self.command is not None:
            raise ValueError(f"a {self.kind} action cannot contain a command")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "destination": self.destination,
            "tone": self.tone,
            "command": self.command,
            "cta_label": self.cta_label,
        }


@dataclass(frozen=True)
class OperatorPreflight:
    """Versioned, checksum-protected snapshot for CLI and dashboard consumers."""

    checked_at: str
    decision_date: str | None
    research: CapabilityState
    proposal_creation: CapabilityState
    stress_review: CapabilityState
    paper_recording: CapabilityState
    operations: CapabilityState
    real_capital: CapabilityState
    next_action: OperatorAction
    raw_prices_through: str | None = None
    fundamentals_through: str | None = None
    macro_releases_through: str | None = None
    account_id: str | None = None
    proposal_id: str | None = None
    trial_id: str | None = None
    account_path: str | None = None
    proposal_path: str | None = None
    trial_path: str | None = None
    account_payload_sha256: str | None = None
    proposal_payload_sha256: str | None = None
    trial_payload_sha256: str | None = None

    @property
    def proposal(self) -> CapabilityState:
        """Compatibility view of the active registered-proposal gate."""

        available = self.stress_review.state == "available"
        return CapabilityState(
            key="proposal_creation",
            label="Registered Proposal",
            state="approved_and_registered" if available else "blocked",
            available=available,
            detail=(
                "The active proposal matches its account and unchanged forward trial."
                if available
                else "The proposal identity or forward-policy evidence requires review."
            ),
            blockers=() if available else self.paper_recording.blockers,
        )

    @property
    def stress(self) -> CapabilityState:
        return self.stress_review

    @property
    def paper(self) -> CapabilityState:
        return self.paper_recording

    @property
    def broker(self) -> CapabilityState:
        return self.real_capital

    def capability(self, key: str) -> CapabilityState:
        if key not in CAPABILITY_KEYS:
            raise KeyError(key)
        return getattr(self, key)

    def payload(self) -> dict[str, Any]:
        return {
            "document_kind": PREFLIGHT_DOCUMENT_KIND,
            "schema_version": PREFLIGHT_SCHEMA_VERSION,
            "read_only": True,
            "execution_boundary": {
                "simulation_only": True,
                "broker_connected": False,
                "broker_orders_enabled": False,
            },
            "checked_at": self.checked_at,
            "source_dates": {
                "certified_decision_close": self.decision_date,
                "raw_prices_through": self.raw_prices_through,
                "fundamentals_through": self.fundamentals_through,
                "macro_releases_through": self.macro_releases_through,
            },
            "evidence_identity": {
                "account_id": self.account_id,
                "proposal_id": self.proposal_id,
                "trial_id": self.trial_id,
                "account_path": self.account_path,
                "proposal_path": self.proposal_path,
                "trial_path": self.trial_path,
                "account_payload_sha256": self.account_payload_sha256,
                "proposal_payload_sha256": self.proposal_payload_sha256,
                "trial_payload_sha256": self.trial_payload_sha256,
            },
            "capabilities": {key: self.capability(key).to_dict() for key in CAPABILITY_KEYS},
            "next_action": self.next_action.to_dict(),
        }

    def to_envelope(self) -> dict[str, Any]:
        payload = self.payload()
        canonical = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            **payload,
            "payload_sha256": hashlib.sha256(canonical).hexdigest(),
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_envelope()

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_envelope(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def _research_state(readiness: Mapping[str, Any]) -> CapabilityState:
    failed = [
        row
        for row in readiness.get("checks", ())
        if isinstance(row, Mapping) and row.get("status") == "fail"
    ]
    blockers = tuple(
        sorted({str(row.get("check") or row.get("label") or "readiness_gate") for row in failed})
    )
    available = bool(readiness.get("ready")) and not failed
    return CapabilityState(
        key="research",
        label="Supervised Research",
        state="available" if available else "blocked",
        available=available,
        detail=(
            "The certified decision close passed every fail-closed research gate."
            if available
            else "One or more certified research gates require review."
        ),
        blockers=blockers,
    )


def _proposal_mapping(
    monitor: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    value = monitor.get("proposal") if isinstance(monitor, Mapping) else None
    return value if isinstance(value, Mapping) else None


def _forward_mapping(
    monitor: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    value = monitor.get("forward") if isinstance(monitor, Mapping) else None
    return value if isinstance(value, Mapping) else None


def _registered_proposal_gate(
    monitor: Mapping[str, Any] | None,
) -> tuple[bool, tuple[str, ...]]:
    proposal = _proposal_mapping(monitor)
    forward = _forward_mapping(monitor)
    blockers: set[str] = set()
    if not isinstance(monitor, Mapping) or not monitor.get("exists"):
        blockers.add("paper_account_not_initialized")
    if proposal is None:
        blockers.add("registered_proposal_missing")
    else:
        if proposal.get("status") != "approved_for_supervised_simulation":
            blockers.add("proposal_not_approved")
        if not proposal.get("registered_in_forward"):
            blockers.add("proposal_not_registered")
        if proposal.get("account_matches_proposal") is False:
            blockers.add("paper_account_changed_after_proposal")
    if not isinstance(forward, Mapping) or not forward.get("ready"):
        blockers.update(_blockers(forward.get("issues")) if forward else ())
        if not blockers:
            blockers.add("forward_trial_not_ready")
    return not blockers, tuple(sorted(blockers))


def _proposal_creation_state(
    monitor: Mapping[str, Any] | None,
    *,
    registered_proposal_ready: bool,
    proposal_blockers: tuple[str, ...],
) -> CapabilityState:
    proposal = _proposal_mapping(monitor)
    if registered_proposal_ready:
        return CapabilityState(
            key="proposal_creation",
            label="Proposal Creation",
            state="active_proposal_exists",
            available=False,
            detail=(
                "An approved registered proposal already exists. Do not replace it "
                "while its prospective paper decision remains unresolved."
            ),
            blockers=("active_registered_proposal_exists",),
        )
    account_exists = isinstance(monitor, Mapping) and bool(monitor.get("exists"))
    if account_exists and proposal is None:
        forward = _forward_mapping(monitor)
        forward_ready = isinstance(forward, Mapping) and bool(forward.get("ready"))
        return CapabilityState(
            key="proposal_creation",
            label="Proposal Creation",
            state="available" if forward_ready else "blocked",
            available=forward_ready,
            detail=(
                "No active proposal exists; a new governed proposal may be reviewed."
                if forward_ready
                else "The paper account or forward-policy baseline requires review."
            ),
            blockers=() if forward_ready else proposal_blockers,
        )
    return CapabilityState(
        key="proposal_creation",
        label="Proposal Creation",
        state="blocked",
        available=False,
        detail="Existing proposal evidence or the paper-account baseline requires review.",
        blockers=proposal_blockers,
    )


def _stress_state(
    *,
    registered_proposal_ready: bool,
    proposal_blockers: tuple[str, ...],
    monitor: Mapping[str, Any] | None,
) -> CapabilityState:
    proposal = _proposal_mapping(monitor)
    blockers = set(proposal_blockers)
    if proposal is not None and proposal.get("already_simulated"):
        blockers.add("proposal_already_simulated")
    available = registered_proposal_ready and not blockers
    return CapabilityState(
        key="stress_review",
        label="Registered-Proposal Stress Review",
        state="available" if available else "blocked",
        available=available,
        detail=(
            "The registered proposal can be stressed without changing governed state."
            if available
            else "Stress output is withheld until proposal and forward gates pass."
        ),
        blockers=tuple(sorted(blockers)),
    )


def _paper_state(
    *,
    registered_proposal_ready: bool,
    proposal_blockers: tuple[str, ...],
    monitor: Mapping[str, Any] | None,
    paper_review: Mapping[str, Any] | None,
) -> CapabilityState:
    proposal = _proposal_mapping(monitor)
    if not registered_proposal_ready:
        return CapabilityState(
            key="paper_recording",
            label="Paper Simulation Recording",
            state="blocked",
            available=False,
            detail="Paper recording is blocked; no account change is permitted.",
            blockers=proposal_blockers,
        )
    assert proposal is not None
    if proposal.get("already_simulated"):
        return CapabilityState(
            key="paper_recording",
            label="Paper Simulation Recording",
            state="recorded",
            available=False,
            detail="This proposal is already recorded and cannot be simulated twice.",
            blockers=("proposal_already_simulated",),
        )

    if isinstance(paper_review, Mapping):
        status = _text(paper_review.get("status")) or "blocked"
        detail = _text(paper_review.get("detail"))
        if paper_review.get("ready"):
            return CapabilityState(
                key="paper_recording",
                label="Paper Simulation Recording",
                state="ready_for_explicit_confirmation",
                available=True,
                detail=(
                    detail
                    or "Every governed review gate passed; a human decision is still required."
                ),
            )
        if status == "waiting_for_execution_data":
            missing = _blockers(paper_review.get("missing"))
            return CapabilityState(
                key="paper_recording",
                label="Paper Simulation Recording",
                state="waiting_for_evidence",
                available=False,
                detail=detail or "Reviewed close-price evidence is incomplete.",
                blockers=missing or ("reviewed_execution_evidence_missing",),
            )
        if status == "waiting_for_scheduled_close":
            return CapabilityState(
                key="paper_recording",
                label="Paper Simulation Recording",
                state="waiting_for_close",
                available=False,
                detail=detail or "The scheduled U.S. close has not passed.",
            )
        if status == "expired":
            return CapabilityState(
                key="paper_recording",
                label="Paper Simulation Recording",
                state="expired",
                available=False,
                detail=detail or "The prospective simulation window expired.",
                blockers=("proposal_timing_expired",),
            )
        return CapabilityState(
            key="paper_recording",
            label="Paper Simulation Recording",
            state="blocked",
            available=False,
            detail=detail or "The governed paper review failed safely.",
            blockers=("governed_paper_review_failed",),
        )

    timing = proposal.get("timing")
    timing_status = timing.get("status") if isinstance(timing, Mapping) else None
    detail = _text(timing.get("detail")) if isinstance(timing, Mapping) else None
    if timing_status == "execution_window_open":
        return CapabilityState(
            key="paper_recording",
            label="Paper Simulation Recording",
            state="review_required",
            available=False,
            detail=detail or "Run the read-only paper review before a human decision.",
            blockers=("governed_paper_review_required",),
        )
    if timing_status == "waiting_for_scheduled_close":
        return CapabilityState(
            key="paper_recording",
            label="Paper Simulation Recording",
            state="waiting_for_close",
            available=False,
            detail=detail or "The scheduled U.S. close has not passed.",
        )
    if timing_status == "expired":
        return CapabilityState(
            key="paper_recording",
            label="Paper Simulation Recording",
            state="expired",
            available=False,
            detail=detail or "The prospective simulation window expired.",
            blockers=("proposal_timing_expired",),
        )
    return CapabilityState(
        key="paper_recording",
        label="Paper Simulation Recording",
        state="blocked",
        available=False,
        detail=detail or "Proposal timing evidence is missing or invalid.",
        blockers=("proposal_timing_invalid",),
    )


def _unresolved(
    operations: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    if not isinstance(operations, Mapping):
        return []
    incidents = [
        row
        for row in operations.get("incidents", ())
        if isinstance(row, Mapping)
        and (
            bool(row.get("operationally_blocking"))
            if "operationally_blocking" in row
            else row.get("state") != "resolved"
        )
    ]
    return sorted(
        incidents,
        key=lambda row: (
            row.get("severity") != "critical",
            str(row.get("incident_id") or ""),
        ),
    )


def _unresolved_anomaly_cases(
    operations: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    if not isinstance(operations, Mapping):
        return []
    cases = [
        row
        for row in operations.get("anomaly_cases", ())
        if isinstance(row, Mapping) and row.get("state") != "resolved"
    ]
    severity_rank = {
        "critical": 0,
        "high": 1,
        "warning": 2,
        "medium": 3,
        "info": 4,
        "low": 5,
    }
    return sorted(
        cases,
        key=lambda row: (
            severity_rank.get(str(row.get("severity") or "").lower(), 6),
            str(row.get("case_id") or ""),
        ),
    )


def _exact_summary_count(
    operations: Mapping[str, Any],
    summary_name: str,
    count_name: str,
    *,
    fallback: int,
) -> int:
    summary = operations.get(summary_name)
    if not isinstance(summary, Mapping):
        return fallback
    value = summary.get(count_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return fallback
    return value


def _operations_state(
    operations: Mapping[str, Any] | None,
) -> CapabilityState:
    if not isinstance(operations, Mapping) or operations.get("error"):
        return CapabilityState(
            key="operations",
            label="Unattended Operations",
            state="unavailable",
            available=False,
            detail="The independent operations ledger could not be verified read-only.",
            blockers=("operations_ledger_unavailable",),
        )
    incidents = _unresolved(operations)
    anomaly_cases = _unresolved_anomaly_cases(operations)
    critical = [row for row in incidents if row.get("severity") == "critical"]
    critical_cases = [row for row in anomaly_cases if row.get("severity") == "critical"]
    incident_count = _exact_summary_count(
        operations,
        "incident_summary",
        "operational_blocking",
        fallback=len(incidents),
    )
    critical_incident_count = _exact_summary_count(
        operations,
        "incident_summary",
        "critical_operational_blocking",
        fallback=len(critical),
    )
    anomaly_count = _exact_summary_count(
        operations,
        "anomaly_case_summary",
        "unresolved",
        fallback=len(anomaly_cases),
    )
    critical_anomaly_count = _exact_summary_count(
        operations,
        "anomaly_case_summary",
        "critical_unresolved",
        fallback=len(critical_cases),
    )
    notification_route = operations.get("notification_route")
    route_enabled = (
        isinstance(notification_route, Mapping)
        and notification_route.get("state") == "enabled"
    )
    dead_letters = (
        _exact_summary_count(
            operations,
            "notification_summary",
            "dead_letter",
            fallback=0,
        )
        if route_enabled
        else 0
    )
    daily = operations.get("daily_cycle")
    daily_state = daily.get("state") if isinstance(daily, Mapping) else None
    incident_blockers = tuple(
        str(row.get("incident_id") or "unresolved_operating_incident") for row in incidents
    )
    anomaly_blockers = tuple(
        str(row.get("case_id") or "unresolved_data_quality_case") for row in anomaly_cases
    )
    truncated_blockers: set[str] = set()
    if incident_count > len(incidents):
        truncated_blockers.add("operational_incidents_truncated")
    if critical_incident_count > len(critical):
        truncated_blockers.add("critical_operational_incident_not_displayed")
    if anomaly_count > len(anomaly_cases):
        truncated_blockers.add("data_quality_cases_truncated")
    if critical_anomaly_count > len(critical_cases):
        truncated_blockers.add("critical_data_quality_case_not_displayed")
    if (
        critical_anomaly_count
        or critical_incident_count
        or daily_state in {"failed", "interrupted"}
    ):
        blockers = {*incident_blockers, *anomaly_blockers}
        blockers.update(truncated_blockers)
        if daily_state in {"failed", "interrupted"}:
            blockers.add("daily_workflow_not_completed")
        return CapabilityState(
            key="operations",
            label="Unattended Operations",
            state="critical",
            available=False,
            detail=(
                f"{critical_anomaly_count} critical data-review case(s), "
                f"{critical_incident_count} critical operating incident(s), "
                "or a failed guarded workflow requires review."
            ),
            blockers=tuple(sorted(blockers)),
        )
    if anomaly_count or incident_count or dead_letters:
        blockers = {
            *anomaly_blockers,
            *incident_blockers,
            *truncated_blockers,
        }
        if dead_letters:
            blockers.add("notification_delivery_dead_letter")
        return CapabilityState(
            key="operations",
            label="Unattended Operations",
            state="needs_review",
            available=False,
            detail=(
                f"{anomaly_count} data-review case(s), {incident_count} "
                f"operating incident(s), and {dead_letters} enabled-route "
                "notification dead letter(s) require review."
            ),
            blockers=tuple(sorted(blockers)),
        )
    if daily_state == "running":
        return CapabilityState(
            key="operations",
            label="Unattended Operations",
            state="updating",
            available=False,
            detail="The guarded daily workflow is currently running.",
        )
    if daily_state != "success":
        return CapabilityState(
            key="operations",
            label="Unattended Operations",
            state="not_verified",
            available=False,
            detail="No successful guarded daily workflow is recorded.",
            blockers=("daily_workflow_not_verified",),
        )
    return CapabilityState(
        key="operations",
        label="Unattended Operations",
        state="verified",
        available=True,
        detail=(
            "The latest guarded workflow succeeded with no unresolved incident or data-review case."
        ),
    )


def _real_capital_state() -> CapabilityState:
    return CapabilityState(
        key="real_capital",
        label="Real-Capital Execution",
        state="disabled",
        available=False,
        detail=(
            "This installation has no broker credential, order route, or live account. "
            "Only checksum-protected local simulation is supported."
        ),
        blockers=("broker_connection_not_implemented",),
    )


def _proposal_command(
    operation: str,
    monitor: Mapping[str, Any] | None,
) -> str | None:
    if not isinstance(monitor, Mapping):
        return None
    proposal_path = _text(monitor.get("proposal_path"))
    if proposal_path is None:
        return None
    parts = ["aios", operation, "--proposal", proposal_path]
    account_path = _text(monitor.get("account_path"))
    if account_path:
        parts.extend(("--account", account_path))
    return shlex.join(parts)


def _incident_action(
    incidents: list[Mapping[str, Any]],
    *,
    critical_only: bool,
) -> OperatorAction | None:
    candidates = [
        row for row in incidents if not critical_only or row.get("severity") == "critical"
    ]
    if not candidates:
        return None
    incident = candidates[0]
    incident_id = _text(incident.get("incident_id"))
    title = _text(incident.get("title")) or "An operating incident needs review"
    command = f"aios alert-show {shlex.quote(incident_id)}" if incident_id else None
    return OperatorAction(
        kind="command" if command else "wait",
        title="Review the operating issue first",
        detail=title,
        destination="system",
        tone="danger" if incident.get("severity") == "critical" else "warning",
        command=command,
        cta_label="Open System Health",
    )


def _anomaly_case_action(
    anomaly_cases: list[Mapping[str, Any]],
    *,
    critical_only: bool,
) -> OperatorAction | None:
    candidates = [
        row for row in anomaly_cases if not critical_only or row.get("severity") == "critical"
    ]
    if not candidates:
        return None
    case = candidates[0]
    case_id = _text(case.get("case_id"))
    title = _text(case.get("title")) or "A data-quality case needs review"
    command = f"aios anomaly-show {shlex.quote(case_id)}" if case_id else None
    severity = str(case.get("severity") or "").lower()
    return OperatorAction(
        kind="command" if command else "wait",
        title="Review the data-quality case first",
        detail=title,
        destination="system",
        tone="danger" if severity == "critical" else "warning",
        command=command,
        cta_label="Open Operations",
    )


def _next_action(
    readiness: Mapping[str, Any],
    monitor: Mapping[str, Any] | None,
    operations_evidence: Mapping[str, Any] | None,
    *,
    research: CapabilityState,
    registered_proposal_ready: bool,
    paper_recording: CapabilityState,
    operations: CapabilityState,
) -> OperatorAction:
    incidents = _unresolved(operations_evidence)
    anomaly_cases = _unresolved_anomaly_cases(operations_evidence)
    exact_critical_incidents = (
        _exact_summary_count(
            operations_evidence,
            "incident_summary",
            "critical_operational_blocking",
            fallback=sum(
                row.get("severity") == "critical" for row in incidents
            ),
        )
        if isinstance(operations_evidence, Mapping)
        else 0
    )
    exact_critical_anomalies = (
        _exact_summary_count(
            operations_evidence,
            "anomaly_case_summary",
            "critical_unresolved",
            fallback=sum(
                row.get("severity") == "critical" for row in anomaly_cases
            ),
        )
        if isinstance(operations_evidence, Mapping)
        else 0
    )
    exact_incidents = (
        _exact_summary_count(
            operations_evidence,
            "incident_summary",
            "operational_blocking",
            fallback=len(incidents),
        )
        if isinstance(operations_evidence, Mapping)
        else 0
    )
    exact_anomalies = (
        _exact_summary_count(
            operations_evidence,
            "anomaly_case_summary",
            "unresolved",
            fallback=len(anomaly_cases),
        )
        if isinstance(operations_evidence, Mapping)
        else 0
    )
    critical_anomaly_action = _anomaly_case_action(
        anomaly_cases,
        critical_only=True,
    )
    if critical_anomaly_action is not None:
        return critical_anomaly_action
    if exact_critical_anomalies:
        return OperatorAction(
            kind="command",
            title="Review the critical data-quality evidence first",
            detail=(
                f"{exact_critical_anomalies} critical data-quality case(s) exist "
                "outside the bounded preflight list."
            ),
            destination="system",
            tone="danger",
            command="aios anomalies --unresolved --limit 1000",
            cta_label="Open Operations",
        )
    critical_action = _incident_action(incidents, critical_only=True)
    if critical_action is not None:
        return critical_action
    if exact_critical_incidents:
        return OperatorAction(
            kind="command",
            title="Review the critical operating evidence first",
            detail=(
                f"{exact_critical_incidents} critical operational blocker(s) "
                "exist outside the bounded preflight list."
            ),
            destination="system",
            tone="danger",
            command="aios alerts --blocking --limit 1000",
            cta_label="Open System Health",
        )
    daily = (
        operations_evidence.get("daily_cycle") if isinstance(operations_evidence, Mapping) else None
    )
    if isinstance(daily, Mapping) and daily.get("state") in {"failed", "interrupted"}:
        return OperatorAction(
            kind="command",
            title="Verify local operations",
            detail="The latest daily workflow did not finish safely.",
            destination="system",
            tone="danger",
            command="aios health --report-only",
            cta_label="Open System Health",
        )
    if not research.available:
        failed = [
            row
            for row in readiness.get("checks", ())
            if isinstance(row, Mapping) and row.get("status") == "fail"
        ]
        label = str(failed[0].get("label")) if failed else "the readiness gate"
        decision_date = _text(readiness.get("as_of") or readiness.get("certified_research_through"))
        command = "aios readiness --purpose paper --report-only"
        if decision_date:
            command = (
                f"aios readiness --as-of {shlex.quote(decision_date)} --purpose paper --report-only"
            )
        return OperatorAction(
            kind="command",
            title="Restore research readiness",
            detail=f"Resolve {label}, then rerun the date-pinned read-only check.",
            destination="system",
            tone="danger",
            command=command,
            cta_label="Open System Health",
        )
    if operations.state == "unavailable":
        return OperatorAction(
            kind="command",
            title="Verify local operations",
            detail=operations.detail,
            destination="system",
            tone="warning",
            command="aios health --report-only",
            cta_label="Open System Health",
        )
    if not registered_proposal_ready:
        forward = _forward_mapping(monitor)
        proposal = _proposal_mapping(monitor)
        if proposal is not None and (not isinstance(forward, Mapping) or not forward.get("ready")):
            title = "Keep the paper trial blocked"
            detail = "Review the missing or changed forward-policy evidence first."
            command = "aios forward-status"
        elif (
            proposal is not None and proposal.get("status") != "approved_for_supervised_simulation"
        ):
            title = "Resolve the proposal blocker"
            detail = "The registered proposal did not pass its governed approval gates."
            command = "aios paper-status"
        elif proposal is not None and not proposal.get("registered_in_forward"):
            title = "Do not use the unregistered proposal"
            detail = "It is not checksum-registered in the active forward trial."
            command = "aios forward-status"
        else:
            title = "Keep the paper trial blocked"
            detail = "The registered proposal or account evidence is blocked."
            command = "aios paper-status"
        return OperatorAction(
            kind="command",
            title=title,
            detail=detail,
            destination="paper",
            tone="danger",
            command=command,
            cta_label="Open Paper Trial",
        )
    if paper_recording.state == "ready_for_explicit_confirmation":
        return OperatorAction(
            kind="human_decision",
            title="Review and make the explicit human decision",
            detail=(
                f"{paper_recording.detail} Preflight deliberately generates no "
                "state-changing command."
            ),
            destination="paper",
            tone="warning",
            cta_label="Open Paper Trial",
        )
    if paper_recording.state == "review_required":
        command = _proposal_command("paper-review", monitor)
        if command:
            return OperatorAction(
                kind="command",
                title="Run the read-only paper review",
                detail=(
                    "The prospective window is open. Recheck current evidence before "
                    "any separately confirmed local simulation."
                ),
                destination="paper",
                tone="warning",
                command=command,
                cta_label="Open Paper Trial",
            )
    if paper_recording.state == "waiting_for_evidence":
        return OperatorAction(
            kind="wait",
            title="Wait for reviewed execution evidence",
            detail=paper_recording.detail,
            destination="paper",
            tone="warning",
            cta_label="Open Paper Trial",
        )
    if paper_recording.state == "expired":
        return OperatorAction(
            kind="command",
            title="Preview the governed prospective rollover",
            detail=(
                f"{paper_recording.detail} The recommended bare command only builds a "
                "checksum-bound later-cycle plan; exact targets are exposed only after "
                "every source gate passes. It never fills the expired proposal or "
                "activates a successor."
            ),
            destination="paper",
            tone="warning",
            command="aios forward-rollover",
            cta_label="Open Paper Trial",
        )
    warning_anomaly_action = _anomaly_case_action(
        anomaly_cases,
        critical_only=False,
    )
    if warning_anomaly_action is not None:
        return warning_anomaly_action
    warning_action = _incident_action(incidents, critical_only=False)
    if warning_action is not None:
        return warning_action
    if exact_anomalies:
        return OperatorAction(
            kind="command",
            title="Review the data-quality evidence",
            detail=(
                f"{exact_anomalies} unresolved data-quality case(s) exist "
                "outside the bounded preflight list."
            ),
            destination="system",
            tone="warning",
            command="aios anomalies --unresolved --limit 1000",
            cta_label="Open Operations",
        )
    if exact_incidents:
        return OperatorAction(
            kind="command",
            title="Review the operating evidence",
            detail=(
                f"{exact_incidents} operational blocker(s) exist outside the "
                "bounded preflight list."
            ),
            destination="system",
            tone="warning",
            command="aios alerts --blocking --limit 1000",
            cta_label="Open System Health",
        )
    route = (
        operations_evidence.get("notification_route")
        if isinstance(operations_evidence, Mapping)
        else None
    )
    enabled_dead_letters = (
        _exact_summary_count(
            operations_evidence,
            "notification_summary",
            "dead_letter",
            fallback=0,
        )
        if isinstance(operations_evidence, Mapping)
        and isinstance(route, Mapping)
        and route.get("state") == "enabled"
        else 0
    )
    if enabled_dead_letters:
        return OperatorAction(
            kind="command",
            title="Review failed notification delivery",
            detail=(
                f"{enabled_dead_letters} enabled-route notification(s) "
                "exhausted retry policy."
            ),
            destination="system",
            tone="warning",
            command="aios notifications --needs-review --limit 1000",
            cta_label="Open System Health",
        )
    if not operations.available:
        return OperatorAction(
            kind="command",
            title="Verify local operations",
            detail=operations.detail,
            destination="system",
            tone="warning",
            command="aios health --report-only",
            cta_label="Open System Health",
        )
    if paper_recording.state == "waiting_for_close":
        proposal = _proposal_mapping(monitor)
        scheduled = (
            _text(proposal.get("scheduled_simulation_date")) if proposal is not None else None
        )
        detail = (
            f"The proposal targets {scheduled}. Wait for the reviewed close."
            if scheduled
            else paper_recording.detail
        )
        return OperatorAction(
            kind="wait",
            title="Wait for the scheduled close",
            detail=detail,
            destination="paper",
            tone="neutral",
            cta_label="Open Paper Trial",
        )
    return OperatorAction(
        kind="command",
        title="Explore the reviewed research",
        detail="Open the certified universe to compare scores and evidence.",
        destination="research",
        tone="success",
        command="aios dashboard",
        cta_label="Open Research",
    )


def build_operator_preflight(
    readiness: Mapping[str, Any],
    monitor: Mapping[str, Any] | None,
    operations_evidence: Mapping[str, Any] | None,
    *,
    paper_review: Mapping[str, Any] | None = None,
    checked_at: str | None = None,
) -> OperatorPreflight:
    """Compose one deterministic snapshot from already-loaded governed evidence."""

    research = _research_state(readiness)
    registered_proposal_ready, proposal_blockers = _registered_proposal_gate(monitor)
    proposal_creation = _proposal_creation_state(
        monitor,
        registered_proposal_ready=registered_proposal_ready,
        proposal_blockers=proposal_blockers,
    )
    stress_review = _stress_state(
        registered_proposal_ready=registered_proposal_ready,
        proposal_blockers=proposal_blockers,
        monitor=monitor,
    )
    paper_recording = _paper_state(
        registered_proposal_ready=registered_proposal_ready,
        proposal_blockers=proposal_blockers,
        monitor=monitor,
        paper_review=paper_review,
    )
    operations = _operations_state(operations_evidence)
    real_capital = _real_capital_state()
    proposal_payload = _proposal_mapping(monitor)
    forward_payload = _forward_mapping(monitor)
    return OperatorPreflight(
        checked_at=checked_at or "not-recorded",
        decision_date=_text(readiness.get("as_of") or readiness.get("certified_research_through")),
        raw_prices_through=_text(readiness.get("raw_prices_through")),
        fundamentals_through=_text(readiness.get("fundamentals_through")),
        macro_releases_through=_text(readiness.get("macro_releases_through")),
        research=research,
        proposal_creation=proposal_creation,
        stress_review=stress_review,
        paper_recording=paper_recording,
        operations=operations,
        real_capital=real_capital,
        next_action=_next_action(
            readiness,
            monitor,
            operations_evidence,
            research=research,
            registered_proposal_ready=registered_proposal_ready,
            paper_recording=paper_recording,
            operations=operations,
        ),
        account_id=(
            _text(proposal_payload.get("account_id")) if proposal_payload is not None else None
        ),
        proposal_id=(
            _text(proposal_payload.get("proposal_id")) if proposal_payload is not None else None
        ),
        trial_id=(_text(forward_payload.get("trial_id")) if forward_payload is not None else None),
        account_path=(_text(monitor.get("account_path")) if isinstance(monitor, Mapping) else None),
        proposal_path=(
            _text(monitor.get("proposal_path")) if isinstance(monitor, Mapping) else None
        ),
        trial_path=(_text(monitor.get("trial_path")) if isinstance(monitor, Mapping) else None),
        account_payload_sha256=(
            _text(monitor.get("account_payload_sha256")) if isinstance(monitor, Mapping) else None
        ),
        proposal_payload_sha256=(
            _text(monitor.get("proposal_payload_sha256")) if isinstance(monitor, Mapping) else None
        ),
        trial_payload_sha256=(
            _text(monitor.get("trial_payload_sha256")) if isinstance(monitor, Mapping) else None
        ),
    )


def _project_path(root: Path, value: str | Path | None) -> Path | None:
    if value is None:
        return None
    requested = Path(value)
    resolved = requested.resolve() if requested.is_absolute() else (root / requested).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"operator evidence path escapes project root: {requested}") from exc
    return resolved


def assess_operator_preflight(
    *,
    proposal_path: Path | None = None,
    review_paper: bool = False,
    now: datetime | None = None,
) -> OperatorPreflight:
    """Load one live snapshot through read-only governed adapters."""

    from aios.config import settings
    from aios.forward import require_registered_forward_proposal
    from aios.operator_evidence import (
        load_operations_evidence_read_only,
        load_paper_monitor_evidence,
    )
    from aios.paper import (
        latest_paper_decision_date,
        review_paper_proposal_execution,
    )
    from aios.readiness import assess_us_readiness
    from aios.storage.store import store_scope

    root = settings.project_root.resolve()
    requested_proposal = _project_path(root, proposal_path)
    paper_review: Mapping[str, Any] | None = None
    with store_scope(read_only=True) as store:
        decision_date = latest_paper_decision_date(store)
        readiness = assess_us_readiness(
            decision_date,
            purpose="paper",
            store=store,
        ).to_dict()
        monitor = load_paper_monitor_evidence(
            root,
            store,
            now=now,
            proposal_path=requested_proposal,
        )
        proposal = _proposal_mapping(monitor)
        timing = proposal.get("timing") if proposal is not None else None
        if review_paper and isinstance(timing, Mapping):
            timing_status = timing.get("status")
            if timing_status == "execution_window_open":
                account = _project_path(root, _text(monitor.get("account_path")))
                registered = _project_path(
                    root,
                    _text(monitor.get("proposal_path")),
                )
                trial = _project_path(root, _text(monitor.get("trial_path")))
                if account is None or registered is None or trial is None:
                    raise ValueError("registered paper evidence is incomplete")
                require_registered_forward_proposal(
                    root,
                    trial,
                    account,
                    registered,
                )
                paper_review = review_paper_proposal_execution(
                    account,
                    registered,
                    store,
                    now=now,
                )
            else:
                paper_review = {
                    "ready": False,
                    "status": str(timing_status or "invalid"),
                    "detail": str(
                        timing.get("detail") or "The proposal timing evidence is unavailable."
                    ),
                    "missing": [],
                }

    operations_path = settings.operations_db_path
    if not operations_path.is_absolute():
        operations_path = root / operations_path
    operations = load_operations_evidence_read_only(operations_path)
    return build_operator_preflight(
        readiness,
        monitor,
        operations,
        paper_review=paper_review,
        checked_at=_timestamp(now),
    )

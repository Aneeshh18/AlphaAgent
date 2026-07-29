"""Pure presentation models for the AIOS dashboard.

This module intentionally has no Streamlit dependency. It translates governed
research, paper, and operations evidence into a small set of user-facing states
that can be tested without rendering the application.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any

from aios.dashboard_copy import display_date

STRESS_REVIEW_ADVISORY = (
    "Proposal-target sensitivities only — these targets are not holdings, fills, or "
    "orders; the scenarios are hypothetical and are not forecasts or investment advice."
)


def notification_route_matches(
    route: Mapping[str, Any] | None,
    config_fingerprint: str,
) -> bool:
    """Compare a read-only notification-route mapping with local configuration."""

    return (
        isinstance(route, Mapping)
        and route.get("config_fingerprint") == config_fingerprint
    )


@dataclass(frozen=True)
class StatusCard:
    """One scoped answer on the dashboard home screen."""

    label: str
    value: str
    detail: str
    tone: str


@dataclass(frozen=True)
class NextAction:
    """The single highest-priority safe operator action."""

    title: str
    detail: str
    tone: str
    command: str | None = None
    cta_label: str = "Open Details"
    destination: str = "today"


@dataclass(frozen=True)
class HomeViewModel:
    """Decision-first state for a first-time dashboard user."""

    research: StatusCard
    paper: StatusCard
    operations: StatusCard
    next_action: NextAction


@dataclass(frozen=True)
class PaperViewModel:
    """Governed paper workflow expressed as one status and four fixed stages."""

    status: StatusCard
    stages: tuple[StatusCard, StatusCard, StatusCard, StatusCard]


@dataclass(frozen=True)
class StressScenarioView:
    """One stress result, kept separate by calculation kind."""

    scenario_id: str
    label: str
    result_kind: str
    status: str
    loss: float | None
    loss_pct: float | None
    drawdown: float | None
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class StressLossContribution:
    """One position or sector contribution to the largest fixed mark loss."""

    kind: str
    label: str
    loss: float
    loss_pct_of_starting_equity: float
    sector: str | None = None


@dataclass(frozen=True)
class StressReferenceFinding:
    """A hypothetical comparison with a sandbox reference limit."""

    scenario_id: str
    finding_id: str
    message: str
    observed: float | None
    limit: float | None
    unit: str | None


@dataclass(frozen=True)
class StressReviewViewModel:
    """Read-only paper-proposal stress evidence for one advisory panel.

    ``state`` is intentionally not a paper-workflow stage. It describes only
    whether this optional advisory evidence is complete, partial, withheld,
    unavailable, or no longer applicable because the proposal was recorded.
    """

    state: str
    status: StatusCard
    availability_reason: str | None
    advisory: str
    report_id: str | None
    calculated_count: int
    withheld_count: int
    generated_count: int
    fixed_marks: tuple[StressScenarioView, ...]
    statistical_proxies: tuple[StressScenarioView, ...]
    largest_fixed_scenario_id: str | None
    largest_fixed_loss: float | None
    largest_fixed_loss_pct: float | None
    largest_fixed_drawdown: float | None
    reference_findings: tuple[StressReferenceFinding, ...]
    top_position_contributions: tuple[StressLossContribution, ...]
    top_sector_contributions: tuple[StressLossContribution, ...]
    blockers: tuple[str, ...]


def _checks_by_key(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(row.get("check")): row for row in report.get("checks", []) if isinstance(row, Mapping)
    }


def _research_status(report: Mapping[str, Any]) -> StatusCard:
    certified = display_date(report.get("certified_research_through"))
    if report.get("ready"):
        return StatusCard(
            label="Research",
            value="Ready",
            detail=f"Reviewed evidence is certified through {certified}.",
            tone="success",
        )

    failed = [
        row
        for row in report.get("checks", [])
        if isinstance(row, Mapping) and row.get("status") == "fail"
    ]
    blocker = str(failed[0].get("label")) if failed else "Required evidence is incomplete"
    return StatusCard(
        label="Research",
        value="Blocked",
        detail=f"New decisions are paused: {blocker}.",
        tone="danger",
    )


def _paper_status(monitor: Mapping[str, Any] | None) -> StatusCard:
    if not monitor or monitor.get("error"):
        return StatusCard(
            label="Paper Trial",
            value="Unavailable",
            detail="The checksum-verified paper state could not be read.",
            tone="warning",
        )
    if not monitor.get("exists"):
        return StatusCard(
            label="Paper Trial",
            value="Not Set Up",
            detail="No checksum-verified local paper account is available.",
            tone="warning",
        )

    forward = monitor.get("forward")
    if not isinstance(forward, Mapping):
        return StatusCard(
            label="Paper Trial",
            value="Blocked",
            detail="Forward-policy evidence is unavailable.",
            tone="danger",
        )
    if not forward.get("ready"):
        return StatusCard(
            label="Paper Trial",
            value="Blocked",
            detail="Forward-policy evidence changed or needs review.",
            tone="danger",
        )

    proposal = monitor.get("proposal")
    if not isinstance(proposal, Mapping):
        return StatusCard(
            label="Paper Trial",
            value="No Proposal",
            detail="The account is unchanged; create a proposal only after research is ready.",
            tone="neutral",
        )
    if proposal.get("already_simulated"):
        return StatusCard(
            label="Paper Trial",
            value="Recorded",
            detail="The latest proposal was recorded in the local simulation.",
            tone="success",
        )
    if proposal.get("status") != "approved_for_supervised_simulation":
        return StatusCard(
            label="Paper Trial",
            value="Blocked",
            detail="The latest proposal did not clear its data and risk checks.",
            tone="danger",
        )
    if not proposal.get("registered_in_forward"):
        return StatusCard(
            label="Paper Trial",
            value="Blocked",
            detail="The proposal is not registered in the active forward trial.",
            tone="danger",
        )

    timing = proposal.get("timing")
    timing_status = timing.get("status") if isinstance(timing, Mapping) else None
    scheduled = display_date(proposal.get("scheduled_simulation_date"))
    if timing_status == "waiting_for_scheduled_close":
        return StatusCard(
            label="Paper Trial",
            value="Waiting for Close",
            detail=f"The {scheduled} proposal is locked and is not a holding.",
            tone="warning",
        )
    if timing_status == "execution_window_open":
        return StatusCard(
            label="Paper Trial",
            value="Review Required",
            detail="The simulation window is open; run the read-only review first.",
            tone="warning",
        )
    if timing_status == "expired":
        return StatusCard(
            label="Paper Trial",
            value="Expired",
            detail="The safe window closed; do not create a retrospective fill.",
            tone="danger",
        )
    if timing_status == "invalid":
        return StatusCard(
            label="Paper Trial",
            value="Blocked",
            detail="Proposal timing evidence is invalid.",
            tone="danger",
        )
    return StatusCard(
        label="Paper Trial",
        value="Needs Review",
        detail="Check the proposal timing evidence before any simulation.",
        tone="warning",
    )


def build_paper_view_model(monitor: Mapping[str, Any] | None) -> PaperViewModel:
    """Build a stable four-stage paper workflow without changing paper state."""
    proposal = monitor.get("proposal") if isinstance(monitor, Mapping) else None
    forward = monitor.get("forward") if isinstance(monitor, Mapping) else None
    proposal_exists = isinstance(proposal, Mapping)
    forward_exists = isinstance(forward, Mapping)
    already_simulated = bool(proposal_exists and proposal.get("already_simulated"))

    if not proposal_exists:
        proposal_stage = StatusCard(
            label="1 · Proposal",
            value="Not Created",
            detail="No target portfolio has been proposed.",
            tone="neutral",
        )
    elif proposal.get("status") == "approved_for_supervised_simulation":
        proposal_stage = StatusCard(
            label="1 · Proposal",
            value="Approved",
            detail="Data and portfolio-risk checks passed.",
            tone="success",
        )
    else:
        proposal_stage = StatusCard(
            label="1 · Proposal",
            value="Blocked",
            detail="The latest proposal failed a required gate.",
            tone="danger",
        )

    if not forward_exists:
        forward_stage = StatusCard(
            label="2 · Forward Trial",
            value="Not Frozen",
            detail="Prospective policy evidence is unavailable.",
            tone="danger",
        )
    elif not forward.get("ready"):
        forward_stage = StatusCard(
            label="2 · Forward Trial",
            value="Policy Drift",
            detail="The frozen policy changed and must be reviewed.",
            tone="danger",
        )
    elif proposal_exists and proposal.get("registered_in_forward"):
        forward_stage = StatusCard(
            label="2 · Forward Trial",
            value="Registered",
            detail="The proposal belongs to the active trial.",
            tone="success",
        )
    else:
        forward_stage = StatusCard(
            label="2 · Forward Trial",
            value="Ready",
            detail="Policy is stable; no proposal is registered.",
            tone="warning",
        )

    timing = proposal.get("timing") if proposal_exists else None
    timing_status = timing.get("status") if isinstance(timing, Mapping) else None
    if already_simulated:
        timing_stage = StatusCard(
            label="3 · Timing Review",
            value="Completed",
            detail="Timing was reviewed before the local record.",
            tone="success",
        )
    elif timing_status == "waiting_for_scheduled_close":
        timing_stage = StatusCard(
            label="3 · Timing Review",
            value="Waiting",
            detail="The scheduled decision close has not passed.",
            tone="warning",
        )
    elif timing_status == "execution_window_open":
        timing_stage = StatusCard(
            label="3 · Timing Review",
            value="Review Now",
            detail="Run the read-only review before any confirmation.",
            tone="warning",
        )
    elif timing_status in {"expired", "invalid"}:
        timing_stage = StatusCard(
            label="3 · Timing Review",
            value="Blocked",
            detail="The safe prospective timing evidence is unusable.",
            tone="danger",
        )
    else:
        timing_stage = StatusCard(
            label="3 · Timing Review",
            value="Not Scheduled",
            detail="There is no eligible review window yet.",
            tone="neutral",
        )

    record_stage = StatusCard(
        label="4 · Local Record",
        value="Recorded" if already_simulated else "No Fill",
        detail=(
            "The checksum-verified simulation contains the proposal."
            if already_simulated
            else "The simulated account remains unchanged."
        ),
        tone="success" if already_simulated else "neutral",
    )
    return PaperViewModel(
        status=_paper_status(monitor),
        stages=(proposal_stage, forward_stage, timing_stage, record_stage),
    )


def _empty_stress_review(
    *,
    state: str,
    value: str,
    detail: str,
    tone: str,
    availability_reason: str,
    blockers: tuple[str, ...] = (),
) -> StressReviewViewModel:
    return StressReviewViewModel(
        state=state,
        status=StatusCard(
            label="Stress Review",
            value=value,
            detail=detail,
            tone=tone,
        ),
        availability_reason=availability_reason,
        advisory=STRESS_REVIEW_ADVISORY,
        report_id=None,
        calculated_count=0,
        withheld_count=0,
        generated_count=0,
        fixed_marks=(),
        statistical_proxies=(),
        largest_fixed_scenario_id=None,
        largest_fixed_loss=None,
        largest_fixed_loss_pct=None,
        largest_fixed_drawdown=None,
        reference_findings=(),
        top_position_contributions=(),
        top_sector_contributions=(),
        blockers=blockers,
    )


def _stress_review_availability(
    monitor: Mapping[str, Any] | None,
) -> StressReviewViewModel | None:
    """Refuse to present a stale report when paper governance is not eligible."""
    if monitor is None:
        return None
    if monitor.get("error") or not monitor.get("exists"):
        return _empty_stress_review(
            state="unavailable",
            value="Unavailable",
            detail="The checksum-verified paper state is unavailable.",
            tone="warning",
            availability_reason="paper_state_unavailable",
            blockers=("paper:state_unavailable",),
        )

    proposal = monitor.get("proposal")
    if not isinstance(proposal, Mapping):
        return _empty_stress_review(
            state="unavailable",
            value="No Proposal",
            detail="There are no proposal targets to stress.",
            tone="neutral",
            availability_reason="no_proposal",
        )
    if proposal.get("already_simulated"):
        return _empty_stress_review(
            state="already_recorded",
            value="Already Recorded",
            detail=(
                "This proposal is already in the local simulation; the panel does not "
                "recast recorded holdings as pending proposal targets."
            ),
            tone="neutral",
            availability_reason="proposal_already_recorded",
        )
    if proposal.get("status") != "approved_for_supervised_simulation":
        return _empty_stress_review(
            state="unavailable",
            value="Unavailable",
            detail="The proposal did not clear its required data and risk gates.",
            tone="danger",
            availability_reason="proposal_blocked",
            blockers=("proposal:not_approved_for_supervised_simulation",),
        )

    forward = monitor.get("forward")
    if not isinstance(forward, Mapping) or not forward.get("ready"):
        return _empty_stress_review(
            state="unavailable",
            value="Policy Drift",
            detail="The frozen forward policy is unavailable or changed.",
            tone="danger",
            availability_reason="forward_policy_drift",
            blockers=("forward:policy_not_unchanged",),
        )
    if not proposal.get("registered_in_forward"):
        return _empty_stress_review(
            state="unavailable",
            value="Unregistered",
            detail="The proposal is not registered in the active forward trial.",
            tone="danger",
            availability_reason="proposal_unregistered",
            blockers=("proposal:not_registered_in_forward",),
        )
    return None


def _stress_payload(report: Any) -> Mapping[str, Any] | None:
    """Accept a report object, a payload, or a checksum envelope."""
    if isinstance(report, Mapping):
        candidate: Any = report
        if "analysis" not in report and isinstance(report.get("payload"), Mapping):
            candidate = report["payload"]
    else:
        candidate = getattr(report, "payload", None)
    return candidate if isinstance(candidate, Mapping) else None


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _stress_blockers(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(sorted({item.strip() for item in value if isinstance(item, str) and item.strip()}))


def _stress_scenario_view(row: Mapping[str, Any]) -> StressScenarioView:
    scenario_id = row.get("scenario_id")
    label = row.get("label")
    result_kind = row.get("result_kind")
    status = row.get("status")
    if not isinstance(scenario_id, str) or not scenario_id.strip():
        raise ValueError("stress scenario is missing its ID")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("stress scenario is missing its label")
    if result_kind not in {"deterministic_mark_shock", "statistical_loss_proxy"}:
        raise ValueError("stress scenario has an unsupported result kind")
    if status not in {"calculated", "withheld_evidence"}:
        raise ValueError("stress scenario has an unsupported result status")

    loss = _finite_float(row.get("portfolio_loss"))
    loss_pct = _finite_float(row.get("portfolio_loss_pct"))
    drawdown = _finite_float(row.get("stressed_drawdown"))
    if status == "calculated" and (loss is None or loss_pct is None):
        raise ValueError("calculated stress scenario is missing its loss")
    if result_kind == "deterministic_mark_shock" and status == "calculated" and drawdown is None:
        raise ValueError("calculated fixed mark scenario is missing its drawdown")
    if status == "withheld_evidence" and any(
        value is not None for value in (loss, loss_pct, drawdown)
    ):
        raise ValueError("withheld stress scenario contains a numerical result")

    return StressScenarioView(
        scenario_id=scenario_id.strip(),
        label=label.strip(),
        result_kind=result_kind,
        status=status,
        loss=loss,
        loss_pct=loss_pct,
        drawdown=drawdown,
        blockers=_stress_blockers(row.get("blockers")),
    )


def _stress_contributions(
    rows: Any,
    *,
    kind: str,
) -> tuple[StressLossContribution, ...]:
    if not isinstance(rows, (list, tuple)):
        return ()
    contributions: list[StressLossContribution] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        label_key = "ticker" if kind == "position" else "sector"
        label = row.get(label_key)
        loss = _finite_float(row.get("loss"))
        loss_pct = _finite_float(row.get("loss_pct_of_starting_equity"))
        if (
            not isinstance(label, str)
            or not label.strip()
            or loss is None
            or loss < 0
            or loss_pct is None
            or loss_pct < 0
        ):
            continue
        sector = row.get("sector") if kind == "position" else None
        contributions.append(
            StressLossContribution(
                kind=kind,
                label=label.strip(),
                loss=loss,
                loss_pct_of_starting_equity=loss_pct,
                sector=sector.strip() if isinstance(sector, str) and sector.strip() else None,
            )
        )
    return tuple(
        sorted(
            contributions,
            key=lambda item: (-item.loss, -item.loss_pct_of_starting_equity, item.label),
        )[:5]
    )


def _stress_reference_findings(
    scenarios: list[tuple[StressScenarioView, Mapping[str, Any]]],
) -> tuple[StressReferenceFinding, ...]:
    findings: list[StressReferenceFinding] = []
    for scenario, raw in scenarios:
        rows = raw.get("reference_limit_findings")
        if not isinstance(rows, (list, tuple)):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            finding_id = row.get("finding_id")
            message = row.get("message")
            if not isinstance(finding_id, str) or not finding_id.strip():
                continue
            if not isinstance(message, str) or not message.strip():
                continue
            unit = row.get("unit")
            findings.append(
                StressReferenceFinding(
                    scenario_id=scenario.scenario_id,
                    finding_id=finding_id.strip(),
                    message=message.strip(),
                    observed=_finite_float(row.get("observed")),
                    limit=_finite_float(row.get("limit")),
                    unit=unit.strip() if isinstance(unit, str) and unit.strip() else None,
                )
            )
    def downside_priority(item: StressReferenceFinding) -> tuple[float, float, str, str]:
        observed = item.observed or 0.0
        limit = item.limit or 0.0
        relative_breach = observed / limit if limit > 0 else observed
        return (-relative_breach, -observed, item.scenario_id, item.finding_id)

    return tuple(sorted(findings, key=downside_priority))


def build_stress_review_view_model(
    report: Any | None,
    monitor: Mapping[str, Any] | None = None,
    *,
    review_error: str | None = None,
) -> StressReviewViewModel:
    """Build a defensive, read-only stress-review panel model.

    The function does not calculate stress, mutate paper state, introduce a
    workflow stage, or offer an action. The caller may pass the governed
    ``StressReviewReport`` object directly, its payload mapping, or a checksum
    envelope containing that payload.
    """
    gated = _stress_review_availability(monitor)
    if gated is not None:
        return gated
    if review_error:
        return _empty_stress_review(
            state="unavailable",
            value="Unavailable",
            detail="The read-only stress review could not be produced safely.",
            tone="warning",
            availability_reason="review_failed",
            blockers=("stress_review:calculation_unavailable",),
        )
    if report is None:
        return _empty_stress_review(
            state="unavailable",
            value="Not Calculated",
            detail="No read-only stress report is available for these proposal targets.",
            tone="neutral",
            availability_reason="report_absent",
        )

    payload = _stress_payload(report)
    try:
        if payload is None:
            raise ValueError("stress report payload is unavailable")
        analysis = payload.get("analysis")
        if not isinstance(analysis, Mapping):
            raise ValueError("stress report analysis is unavailable")
        raw_scenarios = analysis.get("scenarios")
        if not isinstance(raw_scenarios, (list, tuple)):
            raise ValueError("stress report scenarios are unavailable")
        report_status = payload.get(
            "report_generation_status",
            analysis.get("report_generation_status"),
        )
        if report_status not in {"complete", "partial", "blocked"}:
            raise ValueError("stress report has an unsupported generation status")

        scenarios: list[tuple[StressScenarioView, Mapping[str, Any]]] = []
        scenario_ids: set[str] = set()
        for raw in raw_scenarios:
            if not isinstance(raw, Mapping):
                raise ValueError("stress report contains an invalid scenario")
            view = _stress_scenario_view(raw)
            if view.scenario_id in scenario_ids:
                raise ValueError("stress report contains a duplicate scenario ID")
            scenario_ids.add(view.scenario_id)
            scenarios.append((view, raw))
    except (TypeError, ValueError):
        return _empty_stress_review(
            state="unavailable",
            value="Unavailable",
            detail="The stress report payload is incomplete or malformed.",
            tone="warning",
            availability_reason="invalid_report",
            blockers=("stress_report:invalid_payload",),
        )

    fixed_pairs = [pair for pair in scenarios if pair[0].result_kind == "deterministic_mark_shock"]
    proxy_pairs = [pair for pair in scenarios if pair[0].result_kind == "statistical_loss_proxy"]
    fixed_pairs.sort(
        key=lambda pair: (
            pair[0].status != "calculated",
            -(pair[0].loss_pct or 0.0),
            pair[0].scenario_id,
        )
    )
    proxy_pairs.sort(
        key=lambda pair: (
            pair[0].status != "calculated",
            -(pair[0].loss_pct or 0.0),
            pair[0].scenario_id,
        )
    )
    calculated_count = sum(view.status == "calculated" for view, _raw in scenarios)
    withheld_count = sum(view.status == "withheld_evidence" for view, _raw in scenarios)
    blockers = {blocker for view, _raw in scenarios for blocker in view.blockers}
    evidence = payload.get("evidence")
    if isinstance(evidence, Mapping):
        blockers.update(_stress_blockers(evidence.get("blockers")))

    calculated_fixed = [
        pair
        for pair in fixed_pairs
        if pair[0].status == "calculated" and pair[0].loss_pct is not None
    ]
    largest_fixed_pair = calculated_fixed[0] if calculated_fixed else None
    largest_fixed = largest_fixed_pair[0] if largest_fixed_pair else None
    largest_fixed_raw = largest_fixed_pair[1] if largest_fixed_pair else None

    if report_status == "blocked" or (withheld_count and not calculated_count):
        state = "withheld"
        status = StatusCard(
            label="Stress Review",
            value="Withheld",
            detail=("Required evidence failed closed, so no numerical stress result is shown."),
            tone="danger",
        )
        availability_reason = "required_evidence_withheld"
        if not blockers:
            blockers.add("stress_review:required_evidence_withheld")
    elif report_status == "partial" or withheld_count or blockers:
        state = "partial"
        status = StatusCard(
            label="Stress Review",
            value="Partial",
            detail=(
                f"{calculated_count} scenario result(s) were calculated and "
                f"{withheld_count} were withheld."
            ),
            tone="warning",
        )
        availability_reason = None
    else:
        state = "complete"
        status = StatusCard(
            label="Stress Review",
            value="Calculated",
            detail=(
                f"{len(calculated_fixed)} deterministic mark result(s) and "
                f"{sum(pair[0].status == 'calculated' for pair in proxy_pairs)} "
                "statistical proxy result(s) are available."
            ),
            tone="neutral",
        )
        availability_reason = None

    report_id = payload.get("report_id")
    return StressReviewViewModel(
        state=state,
        status=status,
        availability_reason=availability_reason,
        advisory=STRESS_REVIEW_ADVISORY,
        report_id=report_id.strip() if isinstance(report_id, str) and report_id.strip() else None,
        calculated_count=calculated_count,
        withheld_count=withheld_count,
        generated_count=len(scenarios),
        fixed_marks=tuple(view for view, _raw in fixed_pairs),
        statistical_proxies=tuple(view for view, _raw in proxy_pairs),
        largest_fixed_scenario_id=largest_fixed.scenario_id if largest_fixed else None,
        largest_fixed_loss=largest_fixed.loss if largest_fixed else None,
        largest_fixed_loss_pct=largest_fixed.loss_pct if largest_fixed else None,
        largest_fixed_drawdown=largest_fixed.drawdown if largest_fixed else None,
        reference_findings=_stress_reference_findings(scenarios),
        top_position_contributions=(
            _stress_contributions(largest_fixed_raw.get("positions"), kind="position")
            if largest_fixed_raw is not None
            else ()
        ),
        top_sector_contributions=(
            _stress_contributions(
                largest_fixed_raw.get("sector_contributions"),
                kind="sector",
            )
            if largest_fixed_raw is not None
            else ()
        ),
        blockers=tuple(sorted(blockers)),
    )


def _unresolved_incidents(operations: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not operations:
        return []
    return [
        row
        for row in operations.get("incidents", [])
        if isinstance(row, Mapping) and row.get("state") != "resolved"
    ]


def _operations_status(operations: Mapping[str, Any] | None) -> StatusCard:
    if not operations or operations.get("error"):
        return StatusCard(
            label="Operations",
            value="Unavailable",
            detail="The independent operations ledger could not be read.",
            tone="warning",
        )

    unresolved = _unresolved_incidents(operations)
    critical = [row for row in unresolved if row.get("severity") == "critical"]
    if critical:
        return StatusCard(
            label="Operations",
            value="Critical Attention",
            detail=f"{len(critical)} critical of {len(unresolved)} unresolved incidents.",
            tone="danger",
        )

    daily_cycle = operations.get("daily_cycle")
    daily_state = daily_cycle.get("state") if isinstance(daily_cycle, Mapping) else None
    if daily_state in {"failed", "interrupted"}:
        return StatusCard(
            label="Operations",
            value="Needs Attention",
            detail="The latest guarded daily workflow did not finish safely.",
            tone="danger",
        )
    if unresolved:
        return StatusCard(
            label="Operations",
            value="Needs Review",
            detail=f"{len(unresolved)} unresolved operating incident(s).",
            tone="warning",
        )
    if daily_state == "running":
        return StatusCard(
            label="Operations",
            value="Updating",
            detail="The guarded daily workflow is currently running.",
            tone="neutral",
        )
    if daily_state is None:
        return StatusCard(
            label="Operations",
            value="Not Verified",
            detail="No guarded daily workflow result is recorded in the operations ledger.",
            tone="warning",
        )
    if daily_state != "success":
        return StatusCard(
            label="Operations",
            value="Needs Review",
            detail=f"The latest guarded daily workflow state is {daily_state}.",
            tone="warning",
        )
    return StatusCard(
        label="Operations",
        value="No Open Incidents",
        detail=(
            "The latest recorded daily workflow succeeded and no unresolved incident "
            "is recorded. Verify scheduler and backup detail in System Health."
        ),
        tone="success",
    )


def _next_action(
    report: Mapping[str, Any],
    monitor: Mapping[str, Any] | None,
    operations: Mapping[str, Any] | None,
) -> NextAction:
    # Import locally so the pure operator contract stays below both UI surfaces.
    from aios.operator_preflight import build_operator_preflight

    action = build_operator_preflight(report, monitor, operations).next_action
    return NextAction(
        title=action.title,
        detail=action.detail,
        tone=action.tone,
        command=action.command,
        cta_label=action.cta_label,
        destination=action.destination,
    )


def build_home_view_model(
    report: Mapping[str, Any],
    monitor: Mapping[str, Any] | None,
    operations: Mapping[str, Any] | None,
) -> HomeViewModel:
    """Build a scoped, decision-first dashboard model from read-only evidence."""

    return HomeViewModel(
        research=_research_status(report),
        paper=_paper_status(monitor),
        operations=_operations_status(operations),
        next_action=_next_action(report, monitor, operations),
    )

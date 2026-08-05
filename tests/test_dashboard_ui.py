from __future__ import annotations

from copy import deepcopy

from aios.dashboard_ui import (
    STRESS_REVIEW_ADVISORY,
    build_home_view_model,
    build_paper_view_model,
    build_stress_review_view_model,
    notification_route_matches,
)
from aios.risk.stress import StressReviewReport


def _ready_report() -> dict:
    return {
        "ready": True,
        "certified_research_through": "2026-07-27",
        "checks": [
            {
                "check": "universe_membership",
                "label": "Point-in-time universe membership",
                "status": "pass",
                "observed": "503/503",
            }
        ],
    }


def _waiting_monitor() -> dict:
    return {
        "exists": True,
        "proposal_path": "data/paper/proposals/us-qv-2026-07-27.json",
        "summary": {
            "cash": 100_000.0,
            "equity": 100_000.0,
            "holdings": [],
            "execution_count": 0,
        },
        "forward": {"ready": True},
        "proposal": {
            "status": "approved_for_supervised_simulation",
            "registered_in_forward": True,
            "already_simulated": False,
            "scheduled_simulation_date": "2026-07-28",
            "timing": {"status": "waiting_for_scheduled_close"},
        },
    }


def _healthy_operations() -> dict:
    return {
        "error": None,
        "incidents": [],
        "anomaly_cases": [],
        "daily_cycle": {"state": "success"},
    }


def test_notification_route_match_uses_read_only_mapping_contract() -> None:
    assert notification_route_matches(
        {"config_fingerprint": "a" * 64},
        "a" * 64,
    )
    assert not notification_route_matches(
        {"config_fingerprint": "b" * 64},
        "a" * 64,
    )
    assert not notification_route_matches(None, "a" * 64)


def _complete_stress_payload() -> dict:
    fixed = {
        "scenario_id": "broad_mark_decline",
        "label": "Broad mark decline",
        "result_kind": "deterministic_mark_shock",
        "status": "calculated",
        "portfolio_loss": 40_000.0,
        "portfolio_loss_pct": 0.4,
        "stressed_drawdown": -0.4,
        "blockers": [],
        "positions": [
            {
                "ticker": "AAA",
                "sector": "Services",
                "loss": 24_000.0,
                "loss_pct_of_starting_equity": 0.24,
            },
            {
                "ticker": "BBB",
                "sector": "Manufacturing",
                "loss": 16_000.0,
                "loss_pct_of_starting_equity": 0.16,
            },
        ],
        "sector_contributions": [
            {
                "sector": "Services",
                "loss": 24_000.0,
                "loss_pct_of_starting_equity": 0.24,
            },
            {
                "sector": "Manufacturing",
                "loss": 16_000.0,
                "loss_pct_of_starting_equity": 0.16,
            },
        ],
        "reference_limit_findings": [
            {
                "finding_id": "drawdown_above_reference",
                "observed": 0.4,
                "limit": 0.2,
                "unit": "fraction_of_peak_equity",
                "message": "Modeled drawdown exceeds the sandbox reference.",
            }
        ],
    }
    proxy = {
        "scenario_id": "volatility_proxy",
        "label": "Volatility and correlation proxy",
        "result_kind": "statistical_loss_proxy",
        "status": "calculated",
        "portfolio_loss": 25_000.0,
        "portfolio_loss_pct": 0.25,
        "stressed_drawdown": None,
        "blockers": [],
        "positions": [],
        "sector_contributions": [],
        "reference_limit_findings": [],
    }
    return {
        "report_id": "stress-proposal-abc123",
        "report_generation_status": "complete",
        "evidence": {"blockers": []},
        "analysis": {
            "report_generation_status": "complete",
            "scenarios": [proxy, fixed],
        },
    }


def test_home_model_separates_research_paper_and_operations_scope() -> None:
    model = build_home_view_model(
        _ready_report(),
        _waiting_monitor(),
        _healthy_operations(),
    )

    assert model.research.value == "Ready"
    assert "Jul 27, 2026" in model.research.detail
    assert model.paper.value == "Waiting for Close"
    assert "not a holding" in model.paper.detail
    assert model.operations.value == "No Open Incidents"
    assert model.next_action.title == "Wait for the scheduled close"
    assert model.next_action.command is None
    assert model.next_action.cta_label == "Open Paper Trial"
    assert model.next_action.destination == "paper"


def test_home_model_surfaces_critical_operations_over_green_research() -> None:
    operations = {
        "error": None,
        "daily_cycle": {"state": "failed"},
        "incidents": [
            {
                "incident_id": "incident-critical-1",
                "state": "open",
                "severity": "critical",
                "title": "The guarded daily workflow failed",
            },
            {
                "incident_id": "incident-warning-1",
                "state": "open",
                "severity": "warning",
                "title": "A historical ingest needs review",
            },
        ],
    }

    model = build_home_view_model(_ready_report(), _waiting_monitor(), operations)

    assert model.research.value == "Ready"
    assert model.operations.value == "Critical Attention"
    assert model.operations.detail == "1 critical of 2 unresolved incidents."
    assert model.next_action.title == "Review the operating issue first"
    assert "guarded daily workflow failed" in model.next_action.detail
    assert model.next_action.command == "aios alert-show incident-critical-1"
    assert model.next_action.cta_label == "Open System Health"
    assert model.next_action.destination == "system"


def test_home_model_surfaces_critical_data_case_without_blocking_research() -> None:
    operations = _healthy_operations()
    operations["anomaly_cases"] = [
        {
            "case_id": "case-critical-1",
            "state": "open",
            "severity": "critical",
            "title": "Conflicting issuer filing evidence",
        },
        {
            "case_id": "case-warning-1",
            "state": "acknowledged",
            "severity": "warning",
            "title": "Coverage deterioration needs review",
        },
    ]

    model = build_home_view_model(_ready_report(), _waiting_monitor(), operations)

    assert model.research.value == "Ready"
    assert model.operations.value == "Critical Attention"
    assert model.operations.detail == (
        "1 critical of 2 unresolved data-review case(s); 0 operating incident(s)."
    )
    assert model.next_action.command == "aios anomaly-show case-critical-1"
    assert model.next_action.cta_label == "Open Operations"


def test_home_model_routes_open_window_to_read_only_review_only() -> None:
    monitor = _waiting_monitor()
    monitor["proposal"]["timing"] = {"status": "execution_window_open"}

    model = build_home_view_model(_ready_report(), monitor, _healthy_operations())

    assert model.paper.value == "Review Required"
    assert model.next_action.title == "Run the read-only paper review"
    assert model.next_action.command == (
        "aios paper-review --proposal data/paper/proposals/us-qv-2026-07-27.json"
    )
    assert "paper-execute" not in model.next_action.command
    assert model.next_action.destination == "paper"


def test_home_model_keeps_open_paper_review_ahead_of_noncritical_warning() -> None:
    monitor = _waiting_monitor()
    monitor["proposal"]["timing"] = {"status": "execution_window_open"}
    operations = _healthy_operations()
    operations["incidents"] = [
        {
            "incident_id": "incident-warning-1",
            "state": "open",
            "severity": "warning",
            "title": "Scheduler runtime needs review",
        }
    ]

    model = build_home_view_model(_ready_report(), monitor, operations)

    assert model.operations.value == "Needs Review"
    assert model.next_action.title == "Run the read-only paper review"
    assert model.next_action.command.startswith("aios paper-review ")


def test_home_model_keeps_open_paper_review_ahead_of_noncritical_data_case() -> None:
    monitor = _waiting_monitor()
    monitor["proposal"]["timing"] = {"status": "execution_window_open"}
    operations = _healthy_operations()
    operations["anomaly_cases"] = [
        {
            "case_id": "case-high-1",
            "state": "open",
            "severity": "warning",
            "title": "Issuer coverage needs review",
        }
    ]

    model = build_home_view_model(_ready_report(), monitor, operations)

    assert model.operations.value == "Needs Review"
    assert model.operations.detail == (
        "1 data-review case(s) and 0 operating incident(s) remain unresolved."
    )
    assert model.next_action.command.startswith("aios paper-review ")


def test_home_model_routes_expired_proposal_to_read_only_rollover_preview() -> None:
    monitor = _waiting_monitor()
    monitor["proposal"]["timing"] = {"status": "expired"}

    model = build_home_view_model(_ready_report(), monitor, _healthy_operations())

    assert model.paper.value == "Expired"
    assert model.next_action.title == "Preview the governed prospective rollover"
    assert "never fills the expired proposal or activates a successor" in (
        model.next_action.detail
    )
    assert model.next_action.command == "aios forward-rollover"
    assert "--confirm-rollover" not in model.next_action.command


def test_home_model_does_not_offer_review_when_forward_policy_is_blocked() -> None:
    monitor = _waiting_monitor()
    monitor["proposal"]["timing"] = {"status": "execution_window_open"}
    monitor["forward"] = {"ready": False}

    model = build_home_view_model(_ready_report(), monitor, _healthy_operations())

    assert model.paper.value == "Blocked"
    assert model.next_action.title == "Keep the paper trial blocked"
    assert model.next_action.command == "aios forward-status"


def test_home_model_does_not_offer_review_for_blocked_risk_proposal() -> None:
    monitor = _waiting_monitor()
    monitor["proposal"]["timing"] = {"status": "execution_window_open"}
    monitor["proposal"]["status"] = "blocked_risk"

    model = build_home_view_model(_ready_report(), monitor, _healthy_operations())

    assert model.paper.value == "Blocked"
    assert model.next_action.title == "Resolve the proposal blocker"
    assert model.next_action.command == "aios paper-status"


def test_home_model_does_not_offer_review_for_unregistered_proposal() -> None:
    monitor = _waiting_monitor()
    monitor["proposal"]["timing"] = {"status": "execution_window_open"}
    monitor["proposal"]["registered_in_forward"] = False

    model = build_home_view_model(_ready_report(), monitor, _healthy_operations())

    assert model.paper.value == "Blocked"
    assert model.next_action.title == "Do not use the unregistered proposal"
    assert model.next_action.command == "aios forward-status"


def test_home_model_explains_failed_research_gate() -> None:
    report = deepcopy(_ready_report())
    report["ready"] = False
    report["checks"][0].update(status="fail", label="Reviewed price freshness")

    model = build_home_view_model(report, _waiting_monitor(), _healthy_operations())

    assert model.research.value == "Blocked"
    assert "Reviewed price freshness" in model.research.detail
    assert model.next_action.title == "Restore research readiness"
    assert model.next_action.command == (
        "aios readiness --as-of 2026-07-27 --purpose paper --report-only"
    )


def test_home_model_never_hides_an_unavailable_operations_ledger() -> None:
    model = build_home_view_model(
        _ready_report(),
        _waiting_monitor(),
        {"error": "database unavailable", "incidents": [], "daily_cycle": None},
    )

    assert model.operations.value == "Unavailable"
    assert model.operations.tone == "warning"
    assert model.next_action.title == "Verify local operations"
    assert model.next_action.command == "aios health --report-only"


def test_home_model_prioritizes_failed_daily_workflow_over_waiting_proposal() -> None:
    model = build_home_view_model(
        _ready_report(),
        _waiting_monitor(),
        {"error": None, "incidents": [], "daily_cycle": {"state": "failed"}},
    )

    assert model.operations.value == "Needs Attention"
    assert model.next_action.title == "Verify local operations"
    assert model.next_action.command == "aios health --report-only"


def test_home_model_does_not_claim_operations_health_without_a_daily_result() -> None:
    model = build_home_view_model(
        _ready_report(),
        _waiting_monitor(),
        {"error": None, "incidents": [], "daily_cycle": None},
    )

    assert model.operations.value == "Not Verified"
    assert model.next_action.title == "Verify local operations"


def test_paper_view_model_keeps_all_governance_stages_visible() -> None:
    model = build_paper_view_model(_waiting_monitor())

    assert model.status.value == "Waiting for Close"
    assert [stage.label for stage in model.stages] == [
        "1 · Proposal",
        "2 · Forward Trial",
        "3 · Timing Review",
        "4 · Local Record",
    ]
    assert [stage.value for stage in model.stages] == [
        "Approved",
        "Registered",
        "Waiting",
        "No Fill",
    ]


def test_paper_view_model_marks_policy_drift_and_timing_failure_as_blocked() -> None:
    monitor = _waiting_monitor()
    monitor["forward"] = {"ready": False}
    monitor["proposal"]["timing"] = {"status": "expired"}

    model = build_paper_view_model(monitor)

    assert model.status.value == "Blocked"
    assert model.stages[1].value == "Policy Drift"
    assert model.stages[1].tone == "danger"
    assert model.stages[2].value == "Blocked"
    assert model.stages[2].tone == "danger"


def test_stress_view_model_separates_fixed_marks_from_statistical_proxies() -> None:
    report = StressReviewReport(
        payload=_complete_stress_payload(),
        payload_sha256="a" * 64,
    )

    model = build_stress_review_view_model(report, _waiting_monitor())

    assert model.state == "complete"
    assert model.status.value == "Calculated"
    assert model.status.tone == "neutral"
    assert model.calculated_count == 2
    assert model.withheld_count == 0
    assert model.generated_count == 2
    assert [row.scenario_id for row in model.fixed_marks] == ["broad_mark_decline"]
    assert [row.scenario_id for row in model.statistical_proxies] == ["volatility_proxy"]
    assert model.largest_fixed_scenario_id == "broad_mark_decline"
    assert model.largest_fixed_loss == 40_000.0
    assert model.largest_fixed_loss_pct == 0.4
    assert model.largest_fixed_drawdown == -0.4
    assert [row.label for row in model.top_position_contributions] == ["AAA", "BBB"]
    assert [row.label for row in model.top_sector_contributions] == [
        "Services",
        "Manufacturing",
    ]
    assert model.reference_findings[0].finding_id == "drawdown_above_reference"
    assert "not holdings" in model.advisory
    assert "not forecasts" in model.advisory
    assert model.advisory == STRESS_REVIEW_ADVISORY
    assert not hasattr(model, "cta")


def test_stress_view_model_orders_reference_findings_by_downside_severity() -> None:
    payload = _complete_stress_payload()
    payload["analysis"]["scenarios"][0]["reference_limit_findings"] = [
        {
            "finding_id": "larger_relative_breach",
            "observed": 0.6,
            "limit": 0.15,
            "unit": "fraction_of_starting_equity",
            "message": "The larger relative breach must be shown first.",
        }
    ]

    model = build_stress_review_view_model(payload, _waiting_monitor())

    assert [finding.finding_id for finding in model.reference_findings] == [
        "larger_relative_breach",
        "drawdown_above_reference",
    ]


def test_stress_view_model_marks_mixed_calculation_as_partial() -> None:
    payload = _complete_stress_payload()
    payload["report_generation_status"] = "partial"
    payload["analysis"]["report_generation_status"] = "partial"
    payload["analysis"]["scenarios"].append(
        {
            "scenario_id": "sector_mark_decline",
            "label": "Sector mark decline",
            "result_kind": "deterministic_mark_shock",
            "status": "withheld_evidence",
            "portfolio_loss": None,
            "portfolio_loss_pct": None,
            "stressed_drawdown": None,
            "blockers": ["AAA:liquidity:proposal_evidence_mismatch"],
            "positions": [],
            "sector_contributions": [],
            "reference_limit_findings": [],
        }
    )
    payload["evidence"]["blockers"] = ["AAA:liquidity:proposal_evidence_mismatch"]

    model = build_stress_review_view_model(payload, _waiting_monitor())

    assert model.state == "partial"
    assert model.status.value == "Partial"
    assert model.calculated_count == 2
    assert model.withheld_count == 1
    assert len(model.fixed_marks) == 2
    assert model.fixed_marks[-1].status == "withheld_evidence"
    assert model.blockers == ("AAA:liquidity:proposal_evidence_mismatch",)


def test_stress_view_model_withholds_all_numbers_when_evidence_is_blocked() -> None:
    payload = _complete_stress_payload()
    payload["report_generation_status"] = "blocked"
    payload["analysis"]["report_generation_status"] = "blocked"
    payload["analysis"]["scenarios"] = [
        {
            "scenario_id": "broad_mark_decline",
            "label": "Broad mark decline",
            "result_kind": "deterministic_mark_shock",
            "status": "withheld_evidence",
            "portfolio_loss": None,
            "portfolio_loss_pct": None,
            "stressed_drawdown": None,
            "blockers": ["AAA:liquidity:missing_evidence"],
            "positions": [],
            "sector_contributions": [],
            "reference_limit_findings": [],
        }
    ]

    model = build_stress_review_view_model({"payload": payload}, _waiting_monitor())

    assert model.state == "withheld"
    assert model.status.value == "Withheld"
    assert model.calculated_count == 0
    assert model.withheld_count == 1
    assert model.largest_fixed_loss is None
    assert model.blockers == ("AAA:liquidity:missing_evidence",)


def test_stress_view_model_refuses_blocked_and_unregistered_proposals() -> None:
    blocked = _waiting_monitor()
    blocked["proposal"]["status"] = "blocked_risk"
    blocked_model = build_stress_review_view_model(_complete_stress_payload(), blocked)

    unregistered = _waiting_monitor()
    unregistered["proposal"]["registered_in_forward"] = False
    unregistered_model = build_stress_review_view_model(
        _complete_stress_payload(),
        unregistered,
    )

    assert blocked_model.state == "unavailable"
    assert blocked_model.availability_reason == "proposal_blocked"
    assert blocked_model.fixed_marks == ()
    assert unregistered_model.state == "unavailable"
    assert unregistered_model.status.value == "Unregistered"
    assert unregistered_model.availability_reason == "proposal_unregistered"


def test_stress_view_model_refuses_policy_drift_and_review_failure() -> None:
    drifted = _waiting_monitor()
    drifted["forward"] = {"ready": False}

    drift_model = build_stress_review_view_model(_complete_stress_payload(), drifted)
    failure_model = build_stress_review_view_model(
        None,
        _waiting_monitor(),
        review_error="source identity changed",
    )

    assert drift_model.state == "unavailable"
    assert drift_model.status.value == "Policy Drift"
    assert drift_model.availability_reason == "forward_policy_drift"
    assert failure_model.state == "unavailable"
    assert failure_model.availability_reason == "review_failed"
    assert failure_model.blockers == ("stress_review:calculation_unavailable",)


def test_stress_view_model_handles_absent_and_malformed_reports_defensively() -> None:
    absent = build_stress_review_view_model(None, _waiting_monitor())
    malformed = build_stress_review_view_model(
        {"report_generation_status": "complete", "analysis": {"scenarios": [{}]}},
        _waiting_monitor(),
    )

    no_proposal = _waiting_monitor()
    no_proposal.pop("proposal")
    no_proposal_model = build_stress_review_view_model(None, no_proposal)

    assert absent.state == "unavailable"
    assert absent.status.value == "Not Calculated"
    assert absent.availability_reason == "report_absent"
    assert malformed.state == "unavailable"
    assert malformed.availability_reason == "invalid_report"
    assert malformed.blockers == ("stress_report:invalid_payload",)
    assert no_proposal_model.status.value == "No Proposal"
    assert no_proposal_model.availability_reason == "no_proposal"


def test_stress_view_model_does_not_recast_already_simulated_positions() -> None:
    monitor = _waiting_monitor()
    monitor["proposal"]["already_simulated"] = True

    model = build_stress_review_view_model(_complete_stress_payload(), monitor)

    assert model.state == "already_recorded"
    assert model.status.value == "Already Recorded"
    assert model.availability_reason == "proposal_already_recorded"
    assert model.calculated_count == 0
    assert model.fixed_marks == ()
    assert "not holdings" in model.advisory

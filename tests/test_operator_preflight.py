from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date

import pytest

from aios.operator_preflight import (
    CAPABILITY_KEYS,
    OperatorAction,
    _load_universe_evidence,
    build_operator_preflight,
)


def _readiness() -> dict:
    return {
        "ready": True,
        "as_of": "2026-07-28",
        "certified_research_through": "2026-07-28",
        "raw_prices_through": "2026-07-28",
        "fundamentals_through": "2026-07-24",
        "macro_releases_through": "2026-07-28",
        "checks": [
            {
                "check": "universe_membership",
                "label": "Dated investable universe",
                "status": "pass",
            }
        ],
    }


def _monitor(*, timing: str = "execution_window_open") -> dict:
    return {
        "exists": True,
        "account_path": "data/paper/account.json",
        "account_payload_sha256": "a" * 64,
        "proposal_path": "data/paper/proposals/custom plan.json",
        "proposal_payload_sha256": "b" * 64,
        "trial_path": "data/paper/trial.json",
        "trial_payload_sha256": "c" * 64,
        "summary": {
            "cash": 100_000.0,
            "equity": 100_000.0,
            "holdings": [],
            "execution_count": 0,
        },
        "forward": {
            "ready": True,
            "registered_proposals": 1,
            "issues": [],
        },
        "proposal": {
            "status": "approved_for_supervised_simulation",
            "registered_in_forward": True,
            "already_simulated": False,
            "scheduled_simulation_date": "2026-07-28",
            "timing": {
                "status": timing,
                "detail": "Governed timing detail.",
            },
        },
    }


def _operations() -> dict:
    return {
        "error": None,
        "incidents": [],
        "incident_summary": {
            "operational_blocking": 0,
            "critical_operational_blocking": 0,
        },
        "anomaly_cases": [],
        "anomaly_case_summary": {
            "unresolved": 0,
            "critical_unresolved": 0,
        },
        "notification_summary": {"dead_letter": 0},
        "notification_route": None,
        "daily_cycle": {"state": "success"},
    }


def test_preflight_is_deterministic_scoped_and_checksum_protected() -> None:
    first = build_operator_preflight(_readiness(), _monitor(), _operations())
    second = build_operator_preflight(_readiness(), _monitor(), _operations())

    assert first.to_envelope() == second.to_envelope()
    assert tuple(first.to_envelope()["capabilities"]) == CAPABILITY_KEYS
    assert first.research.available is True
    assert first.proposal_creation.state == "active_proposal_exists"
    assert first.stress_review.available is True
    assert first.paper_recording.state == "review_required"
    assert first.operations.available is True
    assert first.real_capital.state == "disabled"

    envelope = first.to_envelope()
    observed = envelope.pop("payload_sha256")
    canonical = json.dumps(
        envelope,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert observed == hashlib.sha256(canonical).hexdigest()


def test_preflight_surfaces_receipt_bound_component_divergence() -> None:
    reconciliation = {
        "mode": "receipt_bound_component_divergence",
        "reconciliation_basis": "accepted_activation_receipt+dated_ivv_holdings",
        "activation_id": "uca-event-test",
        "effective_date": "2026-07-25",
        "holdings_as_of": "2026-07-25",
        "additions": ["GAM"],
        "deletions": ["BBB"],
        "lag_days": 20,
    }

    class AttestationStore:
        def query(self, _sql, _params):
            return [
                {
                    "attestation_id": "uca-review-test",
                    "requested_coverage_through": date(2026, 8, 14),
                    "component_source_url": "https://example.test/components.csv",
                    "mismatch_detail_json": json.dumps(
                        {"accepted_activation_component_lag": reconciliation}
                    ),
                }
            ]

    universe_evidence = _load_universe_evidence(
        AttestationStore(),
        date(2026, 8, 14),
    )
    result = build_operator_preflight(
        _readiness(),
        _monitor(),
        _operations(),
        universe_evidence=universe_evidence,
    )

    assert result.universe_evidence == universe_evidence
    assert result.to_envelope()["universe_evidence"] == {
        "attestation_id": "uca-review-test",
        "coverage_through": "2026-08-14",
        "component_source_url": "https://example.test/components.csv",
        "component_source_mode": "reconciled_divergence",
        "accepted_activation_component_lag": reconciliation,
    }


def test_open_window_routes_to_one_exact_read_only_review_command() -> None:
    result = build_operator_preflight(_readiness(), _monitor(), _operations())

    assert result.next_action.kind == "command"
    assert result.next_action.command == (
        "aios paper-review --proposal 'data/paper/proposals/custom plan.json' "
        "--account data/paper/account.json"
    )
    assert "paper-execute" not in result.canonical_json()


def test_critical_operations_preempt_an_available_paper_review() -> None:
    operations = _operations()
    operations["incidents"] = [
        {
            "incident_id": "inc-critical",
            "severity": "critical",
            "state": "open",
            "title": "The daily workflow failed",
        }
    ]
    operations["incident_summary"] = {
        "operational_blocking": 1,
        "critical_operational_blocking": 1,
    }

    result = build_operator_preflight(_readiness(), _monitor(), operations)

    assert result.operations.state == "critical"
    assert result.next_action.command == "aios alert-show inc-critical"


def test_critical_data_case_preempts_incidents_and_keeps_research_separate() -> None:
    operations = _operations()
    operations["incidents"] = [
        {
            "incident_id": "inc-critical",
            "severity": "critical",
            "state": "open",
            "title": "The daily workflow failed",
        }
    ]
    operations["anomaly_cases"] = [
        {
            "case_id": "case-critical",
            "severity": "critical",
            "state": "open",
            "title": "Conflicting filing evidence",
        }
    ]
    operations["incident_summary"] = {
        "operational_blocking": 1,
        "critical_operational_blocking": 1,
    }
    operations["anomaly_case_summary"] = {
        "unresolved": 1,
        "critical_unresolved": 1,
    }

    result = build_operator_preflight(_readiness(), _monitor(), operations)

    assert result.research.available is True
    assert result.operations.state == "critical"
    assert result.next_action.command == "aios anomaly-show case-critical"
    assert result.next_action.cta_label == "Open Operations"
    assert "paper-execute" not in result.canonical_json()


def test_noncritical_warning_does_not_hide_time_bounded_paper_review() -> None:
    operations = _operations()
    operations["incidents"] = [
        {
            "incident_id": "inc-warning",
            "severity": "warning",
            "state": "open",
            "title": "Scheduler runtime was not verified",
        }
    ]
    operations["incident_summary"]["operational_blocking"] = 1

    result = build_operator_preflight(_readiness(), _monitor(), operations)

    assert result.operations.state == "needs_review"
    assert result.next_action.command.startswith("aios paper-review ")


def test_noncritical_data_case_waits_behind_time_bounded_paper_review() -> None:
    operations = _operations()
    operations["anomaly_cases"] = [
        {
            "case_id": "case-warning",
            "severity": "warning",
            "state": "open",
            "title": "Coverage deterioration needs review",
        }
    ]
    operations["anomaly_case_summary"]["unresolved"] = 1

    result = build_operator_preflight(_readiness(), _monitor(), operations)

    assert result.operations.state == "needs_review"
    assert result.operations.available is False
    assert result.next_action.command.startswith("aios paper-review ")


def test_expired_registered_proposal_suggests_only_the_read_only_rollover_preview() -> None:
    monitor = _monitor(timing="expired")

    result = build_operator_preflight(_readiness(), monitor, _operations())

    assert result.proposal_creation.state == "active_proposal_exists"
    assert result.proposal_creation.available is False
    assert result.paper_recording.state == "expired"
    assert result.next_action.title == "Preview the governed prospective rollover"
    assert "never fills the expired proposal or activates a successor" in (
        result.next_action.detail
    )
    assert result.next_action.command == "aios forward-rollover"
    assert "--confirm-rollover" not in result.next_action.command


def test_research_failure_returns_a_date_pinned_readiness_command() -> None:
    readiness = deepcopy(_readiness())
    readiness["ready"] = False
    readiness["checks"][0].update(status="fail", label="Reviewed price freshness")

    result = build_operator_preflight(readiness, _monitor(), _operations())

    assert result.research.available is False
    assert result.research.blockers == ("universe_membership",)
    assert result.next_action.command == (
        "aios readiness --as-of 2026-07-28 --purpose paper --report-only"
    )


def test_forward_drift_blocks_paper_without_hiding_research() -> None:
    monitor = _monitor()
    monitor["forward"] = {
        "ready": False,
        "issues": ["frozen policy files changed"],
    }

    result = build_operator_preflight(_readiness(), monitor, _operations())

    assert result.research.available is True
    assert result.paper_recording.state == "blocked"
    assert result.paper_recording.blockers == ("frozen policy files changed",)
    assert result.next_action.command == "aios forward-status"


def test_waiting_proposal_surfaces_noncritical_operations_before_waiting() -> None:
    operations = _operations()
    operations["incidents"] = [
        {
            "incident_id": "inc-warning",
            "severity": "warning",
            "state": "open",
            "title": "A warning remains.",
        }
    ]
    operations["incident_summary"]["operational_blocking"] = 1

    result = build_operator_preflight(
        _readiness(),
        _monitor(timing="waiting_for_scheduled_close"),
        operations,
    )

    assert result.next_action.command == "aios alert-show inc-warning"


def test_waiting_proposal_surfaces_data_case_before_waiting() -> None:
    operations = _operations()
    operations["anomaly_cases"] = [
        {
            "case_id": "case-warning",
            "severity": "warning",
            "state": "acknowledged",
            "title": "Issuer facts need review",
        }
    ]
    operations["anomaly_case_summary"]["unresolved"] = 1

    result = build_operator_preflight(
        _readiness(),
        _monitor(timing="waiting_for_scheduled_close"),
        operations,
    )

    assert result.next_action.command == "aios anomaly-show case-warning"


def test_exact_summary_blocks_when_critical_incident_is_outside_bounded_rows() -> None:
    operations = _operations()
    operations["incident_summary"] = {
        "operational_blocking": 101,
        "critical_operational_blocking": 1,
    }

    result = build_operator_preflight(_readiness(), _monitor(), operations)

    assert result.operations.state == "critical"
    assert "1 critical operating incident" in result.operations.detail
    assert "critical_operational_incident_not_displayed" in result.operations.blockers
    assert result.next_action.command == "aios alerts --blocking --limit 1000"


def test_legacy_resolved_blocker_remains_visible_to_preflight() -> None:
    operations = _operations()
    operations["incidents"] = [
        {
            "incident_id": "inc-legacy",
            "severity": "warning",
            "state": "resolved",
            "resolution_proof_status": "legacy_unproven",
            "operationally_blocking": True,
            "title": "Legacy resolution needs later proof",
        }
    ]
    operations["incident_summary"]["operational_blocking"] = 1

    result = build_operator_preflight(
        _readiness(),
        _monitor(timing="waiting_for_scheduled_close"),
        operations,
    )

    assert result.operations.state == "needs_review"
    assert result.next_action.command == "aios alert-show inc-legacy"


def test_enabled_route_dead_letter_requires_operations_review() -> None:
    operations = _operations()
    operations["notification_route"] = {"state": "enabled"}
    operations["notification_summary"]["dead_letter"] = 2

    result = build_operator_preflight(
        _readiness(),
        _monitor(timing="waiting_for_scheduled_close"),
        operations,
    )

    assert result.operations.state == "needs_review"
    assert "notification_delivery_dead_letter" in result.operations.blockers
    assert result.next_action.command == (
        "aios notifications --needs-review --limit 1000"
    )


def test_disabled_route_dead_letters_do_not_conflate_optional_delivery() -> None:
    operations = _operations()
    operations["notification_route"] = {"state": "disabled"}
    operations["notification_summary"]["dead_letter"] = 2

    result = build_operator_preflight(_readiness(), _monitor(), operations)

    assert result.operations.state == "verified"


def test_unknown_capability_and_ambiguous_actions_are_rejected() -> None:
    result = build_operator_preflight(_readiness(), _monitor(), _operations())
    with pytest.raises(KeyError):
        result.capability("everything")
    with pytest.raises(ValueError, match="requires one non-empty command"):
        OperatorAction(
            kind="command",
            title="Invalid",
            detail="Missing command.",
            destination="paper",
            tone="warning",
        )
    with pytest.raises(ValueError, match="wait action cannot contain"):
        OperatorAction(
            kind="wait",
            title="Invalid",
            detail="Wait cannot execute.",
            destination="paper",
            tone="neutral",
            command="aios paper-execute",
        )

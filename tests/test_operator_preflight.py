from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from aios.operator_preflight import (
    CAPABILITY_KEYS,
    OperatorAction,
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

    result = build_operator_preflight(_readiness(), _monitor(), operations)

    assert result.operations.state == "critical"
    assert result.next_action.command == "aios alert-show inc-critical"


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

    result = build_operator_preflight(_readiness(), _monitor(), operations)

    assert result.operations.state == "needs_review"
    assert result.next_action.command.startswith("aios paper-review ")


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

    result = build_operator_preflight(
        _readiness(),
        _monitor(timing="waiting_for_scheduled_close"),
        operations,
    )

    assert result.next_action.command == "aios alert-show inc-warning"


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

"""Guards for the unattended paper cycle.

The autopilot exists because the paper trial never recorded a fill: proposals
expired unexecuted and cash never moved. The risk in fixing that is the
opposite failure — an unattended path that records fills a human running the
equivalent command would have been refused. These tests pin that it refuses in
the same places, and that it cannot record anything without explicit
confirmation.
"""

from __future__ import annotations

from typing import Any

import pytest

from aios.autopilot import has_unresolved_registered_proposal


def _trial(*proposal_ids: str) -> dict[str, Any]:
    return {"proposals": [{"proposal_id": pid} for pid in proposal_ids]}


def _account(*executed_ids: str) -> dict[str, Any]:
    return {"executions": [{"proposal_id": pid} for pid in executed_ids]}


def test_registered_proposal_without_an_execution_is_unresolved() -> None:
    """The exact state that stalled the live trial for weeks."""
    assert has_unresolved_registered_proposal(_trial("p-1"), _account()) is True


def test_every_registered_proposal_executed_is_resolved() -> None:
    assert has_unresolved_registered_proposal(_trial("p-1"), _account("p-1")) is False


def test_one_unexecuted_proposal_among_several_still_blocks() -> None:
    assert (
        has_unresolved_registered_proposal(
            _trial("p-1", "p-2", "p-3"), _account("p-1", "p-3")
        )
        is True
    )


def test_no_registered_proposals_is_resolved() -> None:
    assert has_unresolved_registered_proposal(_trial(), _account()) is False


def test_unrelated_executions_do_not_resolve_a_proposal() -> None:
    """An execution of a different proposal must never satisfy this check."""
    assert has_unresolved_registered_proposal(_trial("p-1"), _account("p-other")) is True


@pytest.mark.parametrize(
    "trial_payload, account_payload",
    [
        ({"proposals": "not-a-list"}, {"executions": []}),
        ({"proposals": []}, {"executions": "not-a-list"}),
        ({"proposals": [{"proposal_id": ""}]}, {"executions": []}),
        ({"proposals": [{}]}, {"executions": []}),
        ({"proposals": []}, {"executions": [{"proposal_id": 5}]}),
        ({"proposals": ["not-a-mapping"]}, {"executions": []}),
    ],
)
def test_malformed_lifecycle_evidence_raises_instead_of_passing(
    trial_payload: dict[str, Any],
    account_payload: dict[str, Any],
) -> None:
    """Unreadable lifecycle evidence must fail closed, never read as 'resolved'.

    Returning False here would let an unattended run create a proposal on top
    of an unknown state.
    """
    with pytest.raises(ValueError):
        has_unresolved_registered_proposal(trial_payload, account_payload)


def test_confirm_simulated_defaults_to_false() -> None:
    """Importing or dry-running the cycle must not be able to record a fill.

    `execute_paper_proposal` refuses without confirmation, so the default here
    is the difference between a safe dry run and an accidental governed write.
    """
    import inspect

    from aios.autopilot import run_autopilot_cycle

    signature = inspect.signature(run_autopilot_cycle)
    assert signature.parameters["confirm_simulated"].default is False

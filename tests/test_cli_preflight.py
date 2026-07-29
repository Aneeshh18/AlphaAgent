from __future__ import annotations

import json

from typer.testing import CliRunner

from aios import cli, operator_preflight
from aios.operator_preflight import build_operator_preflight


def _readiness() -> dict:
    return {
        "ready": True,
        "as_of": "2026-07-28",
        "certified_research_through": "2026-07-28",
        "raw_prices_through": "2026-07-28",
        "fundamentals_through": "2026-07-24",
        "macro_releases_through": "2026-07-28",
        "checks": [{"check": "universe_membership", "status": "pass"}],
    }


def _monitor() -> dict:
    return {
        "exists": True,
        "account_path": "data/paper/account.json",
        "account_payload_sha256": "a" * 64,
        "proposal_path": "data/paper/proposals/proposal.json",
        "proposal_payload_sha256": "b" * 64,
        "trial_path": "data/paper/trial.json",
        "trial_payload_sha256": "c" * 64,
        "forward": {"ready": True, "registered_proposals": 1, "issues": []},
        "proposal": {
            "status": "approved_for_supervised_simulation",
            "registered_in_forward": True,
            "already_simulated": False,
            "timing": {
                "status": "execution_window_open",
                "detail": "The simulation window is open.",
            },
        },
    }


def _operations(*, warning: bool = False) -> dict:
    incidents = []
    if warning:
        incidents.append(
            {
                "incident_id": "warning-1",
                "severity": "warning",
                "state": "open",
                "title": "Review scheduler evidence.",
            }
        )
    return {
        "error": None,
        "incidents": incidents,
        "daily_cycle": {"state": "success"},
    }


def _configure(monkeypatch, *, warning: bool = False) -> None:
    def assess(*, proposal_path=None, review_paper=False):
        review = (
            {
                "ready": True,
                "status": "ready_for_confirmed_simulation",
                "detail": "Every governed review gate passed.",
            }
            if review_paper
            else None
        )
        return build_operator_preflight(
            _readiness(),
            _monitor(),
            _operations(warning=warning),
            paper_review=review,
            checked_at="2026-07-29T10:00:00Z",
        )

    monkeypatch.setattr(operator_preflight, "assess_operator_preflight", assess)


def test_preflight_json_is_scoped_read_only_and_never_emits_execution(
    monkeypatch,
) -> None:
    _configure(monkeypatch, warning=True)

    result = CliRunner().invoke(cli.app, ["preflight", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "operator-preflight.v1"
    assert payload["read_only"] is True
    assert set(payload["capabilities"]) == {
        "research",
        "proposal_creation",
        "stress_review",
        "paper_recording",
        "operations",
        "real_capital",
    }
    assert payload["capabilities"]["research"]["available"] is True
    assert payload["capabilities"]["operations"]["state"] == "needs_review"
    assert payload["capabilities"]["real_capital"]["state"] == "disabled"
    assert payload["next_action"]["command"] == (
        "aios paper-review --proposal data/paper/proposals/proposal.json "
        "--account data/paper/account.json"
    )
    assert "paper-execute" not in result.output
    assert len(payload["payload_sha256"]) == 64


def test_preflight_require_and_unknown_scope_have_meaningful_exit_codes(
    monkeypatch,
) -> None:
    _configure(monkeypatch, warning=True)

    unmet = CliRunner().invoke(
        cli.app,
        ["preflight", "--require", "operations", "--json"],
    )
    unknown = CliRunner().invoke(
        cli.app,
        ["preflight", "--require", "everything"],
    )

    assert unmet.exit_code == 1
    assert json.loads(unmet.output)["capabilities"]["operations"]["available"] is False
    assert unknown.exit_code == 2
    assert "unknown capability" in unknown.output


def test_preflight_failure_is_structured_and_does_not_claim_success(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        operator_preflight,
        "assess_operator_preflight",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("database locked")),
    )

    result = CliRunner().invoke(cli.app, ["preflight", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["read_only"] is True
    assert payload["error"] == {
        "type": "RuntimeError",
        "detail": "database locked",
    }


def test_review_ready_is_a_human_decision_with_no_generated_command(
    monkeypatch,
) -> None:
    _configure(monkeypatch)

    result = CliRunner().invoke(
        cli.app,
        ["preflight", "--review-paper", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["capabilities"]["paper_recording"]["state"] == (
        "ready_for_explicit_confirmation"
    )
    assert payload["next_action"]["kind"] == "human_decision"
    assert payload["next_action"]["command"] is None
    assert "paper-execute" not in result.output

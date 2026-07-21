from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aios.forward import (
    assess_forward_trial,
    create_forward_trial,
    register_forward_proposal,
    require_registered_forward_proposal,
)
from aios.paper import (
    ACCOUNT_DOCUMENT_KIND,
    PROPOSAL_DOCUMENT_KIND,
    canonical_payload_sha256,
)


def _write_document(path: Path, kind: str, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "document_schema_version": 1,
                "document_kind": kind,
                "payload_sha256": canonical_payload_sha256(payload),
                "payload": payload,
            }
        ),
        encoding="utf-8",
    )


def _account_payload() -> dict:
    return {
        "account_id": "paper-test",
        "mode": "simulation_only",
        "portfolio": {
            "transaction_costs": {
                "commission_bps": 5.0,
                "slippage_bps": 5.0,
                "fixed_fee": 0.0,
            },
            "tax_policy": {
                "short_term_rate": 0.0,
                "long_term_rate": 0.0,
                "dividend_rate": 0.0,
                "long_term_days": 365,
            },
        },
    }


def _proposal_payload(
    account_sha: str,
    *,
    proposal_id: str,
    decision_date: str,
    generated_at: str,
    risk_limit: float = 0.1,
) -> dict:
    return {
        "proposal_id": proposal_id,
        "account_id": "paper-test",
        "account_payload_sha256": account_sha,
        "market": "US",
        "universe_id": "sp500",
        "strategy": "qv",
        "mode": "simulation_only",
        "decision_date": decision_date,
        "generated_at": generated_at,
        "status": "approved_for_supervised_simulation",
        "risk_policy": {"maximum_position_weight": risk_limit},
        "targets": [{"ticker": "AAA"}, {"ticker": "BBB"}],
    }


def _baseline(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    policy = tmp_path / "policy.py"
    policy.write_text("WEIGHT = 1\n", encoding="utf-8")
    account = tmp_path / "data/paper/account.json"
    account_payload = _account_payload()
    _write_document(account, ACCOUNT_DOCUMENT_KIND, account_payload)
    proposal = tmp_path / "data/paper/proposals/us-qv-2026-07-20.json"
    _write_document(
        proposal,
        PROPOSAL_DOCUMENT_KIND,
        _proposal_payload(
            canonical_payload_sha256(account_payload),
            proposal_id="baseline",
            decision_date="2026-07-20",
            generated_at="2026-07-20T22:00:00Z",
        ),
    )
    trial = tmp_path / "data/paper/trial.json"
    return policy, account, proposal, trial


def test_forward_trial_detects_policy_drift_without_hashing_market_data(tmp_path) -> None:
    policy, account, proposal, trial = _baseline(tmp_path)
    create_forward_trial(
        tmp_path,
        trial,
        account,
        proposal,
        confirm=True,
        now=datetime(2026, 7, 21, tzinfo=UTC),
        policy_files=("policy.py",),
    )

    status = assess_forward_trial(
        tmp_path,
        trial,
        account,
        policy_files=("policy.py",),
    )
    assert status.ready is True
    assert status.registered_proposals == 1

    policy.write_text("WEIGHT = 2\n", encoding="utf-8")
    changed = assess_forward_trial(
        tmp_path,
        trial,
        account,
        policy_files=("policy.py",),
    )
    assert changed.ready is False
    assert "frozen policy files changed" in changed.issues


def test_forward_trial_registers_every_new_matching_proposal(tmp_path) -> None:
    _policy, account, proposal, trial = _baseline(tmp_path)
    create_forward_trial(
        tmp_path,
        trial,
        account,
        proposal,
        confirm=True,
        now=datetime(2026, 7, 21, tzinfo=UTC),
        policy_files=("policy.py",),
    )
    account_sha = canonical_payload_sha256(_account_payload())
    next_proposal = tmp_path / "data/paper/proposals/us-qv-2026-10-01.json"
    _write_document(
        next_proposal,
        PROPOSAL_DOCUMENT_KIND,
        _proposal_payload(
            account_sha,
            proposal_id="next",
            decision_date="2026-10-01",
            generated_at="2026-10-01T22:00:00Z",
        ),
    )

    before = assess_forward_trial(
        tmp_path,
        trial,
        account,
        policy_files=("policy.py",),
    )
    assert before.ready is False
    assert any("unregistered proposal exists" in issue for issue in before.issues)

    register_forward_proposal(tmp_path, trial, account, next_proposal)
    after = require_registered_forward_proposal(
        tmp_path,
        trial,
        account,
        next_proposal,
    )
    assert after.ready is True
    assert after.registered_proposals == 2


def test_forward_trial_rejects_configuration_change(tmp_path) -> None:
    _policy, account, proposal, trial = _baseline(tmp_path)
    create_forward_trial(
        tmp_path,
        trial,
        account,
        proposal,
        confirm=True,
        now=datetime(2026, 7, 21, tzinfo=UTC),
        policy_files=("policy.py",),
    )
    changed = tmp_path / "data/paper/proposals/us-qv-2026-10-01.json"
    _write_document(
        changed,
        PROPOSAL_DOCUMENT_KIND,
        _proposal_payload(
            canonical_payload_sha256(_account_payload()),
            proposal_id="changed-risk",
            decision_date="2026-10-01",
            generated_at="2026-10-01T22:00:00Z",
            risk_limit=0.2,
        ),
    )

    with pytest.raises(ValueError, match="changed frozen risk policy"):
        register_forward_proposal(tmp_path, trial, account, changed)


def test_forward_trial_records_a_readiness_block_without_false_drift(tmp_path) -> None:
    _policy, account, proposal, trial = _baseline(tmp_path)
    create_forward_trial(
        tmp_path,
        trial,
        account,
        proposal,
        confirm=True,
        now=datetime(2026, 7, 21, tzinfo=UTC),
        policy_files=("policy.py",),
    )
    blocked_path = tmp_path / "data/paper/proposals/us-qv-2026-10-01.json"
    blocked = _proposal_payload(
        canonical_payload_sha256(_account_payload()),
        proposal_id="blocked",
        decision_date="2026-10-01",
        generated_at="2026-10-01T22:00:00Z",
    )
    blocked["status"] = "blocked_readiness"
    blocked["targets"] = []
    _write_document(blocked_path, PROPOSAL_DOCUMENT_KIND, blocked)

    register_forward_proposal(tmp_path, trial, account, blocked_path)
    status = assess_forward_trial(
        tmp_path,
        trial,
        account,
        policy_files=("policy.py",),
    )
    assert status.ready is True
    assert status.registered_proposals == 2

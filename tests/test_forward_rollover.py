"""Fail-closed contracts for governed rollover preview and activation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import aios.forward_rollover as rollover_module
import aios.forward_rollover_activation as activation_module
from aios.alerts import AlertStore
from aios.forward import (
    create_forward_trial,
    read_forward_trial,
    register_forward_proposal,
)
from aios.forward_rollover import (
    ROLLOVER_PLAN_DOCUMENT_KIND,
    ForwardRolloverResult,
    execute_forward_rollover_from_plan,
    persist_forward_rollover_plan,
    preview_forward_rollover,
    read_forward_rollover_plan,
)
from aios.operations import (
    create_local_backup,
    restore_local_backup,
    verify_local_backup,
)
from aios.paper import (
    ACCOUNT_DOCUMENT_KIND,
    PROPOSAL_DOCUMENT_KIND,
    canonical_payload_sha256,
    read_paper_document,
)
from aios.risk.policy import PortfolioRiskPolicy
from aios.rollover_journal import scan_attempt_directories, validate_attempt_directory
from aios.storage.store import Store


def _write_document(path: Path, kind: str, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "document_schema_version": 1,
                "document_kind": kind,
                "payload_sha256": canonical_payload_sha256(payload),
                "payload": payload,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _account_payload() -> dict:
    return {
        "account_schema_version": 1,
        "account_id": "paper-test",
        "market": "US",
        "universe_id": "sp500",
        "strategy": "qv",
        "mode": "simulation_only",
        "broker_connected": False,
        "portfolio": {
            "last_date": None,
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
        "executions": [],
        "audit_events": [{"event": "account_initialized"}],
    }


def _risk_policy_payload() -> dict:
    return asdict(
        PortfolioRiskPolicy(
            minimum_positions=2,
            maximum_position_weight=0.5,
            maximum_sector_weight=0.5,
        )
    )


def _proposal_payload(
    account_sha256: str,
    *,
    proposal_id: str = "paper-2026-07-27-test",
    decision_date: str = "2026-07-27",
    scheduled_date: str = "2026-07-28",
) -> dict:
    return {
        "proposal_schema_version": 1,
        "proposal_id": proposal_id,
        "account_id": "paper-test",
        "account_payload_sha256": account_sha256,
        "market": "US",
        "universe_id": "sp500",
        "strategy": "qv",
        "mode": "simulation_only",
        "decision_date": decision_date,
        "scheduled_simulation_date": scheduled_date,
        "generated_at": f"{scheduled_date}T08:00:00Z",
        "status": "approved_for_supervised_simulation",
        "risk_policy": _risk_policy_payload(),
        "targets": [{"ticker": "AAA"}, {"ticker": "BBB"}],
    }


def _readiness(as_of: str = "2026-07-29", *, ready: bool = True) -> dict:
    checks = []
    for name in rollover_module.REQUIRED_READINESS_CHECKS:
        checks.append(
            {
                "check": name,
                "label": name.replace("_", " ").title(),
                "status": "pass",
                "observed": "ready",
                "required": "ready",
                "detail": "Bound exact-date test evidence.",
            }
        )
    if not ready:
        checks[-1]["status"] = "fail"
        checks[-1]["observed"] = "blocked"
    return {
        "as_of": as_of,
        "purpose": "paper",
        "generated_on": "2026-07-30",
        "universe_id": "sp500",
        "benchmark_ticker": "SPY",
        "certified_research_from": "2026-07-29",
        "certified_research_through": "2026-07-29",
        "raw_prices_through": "2026-07-29",
        "fundamentals_through": "2026-07-29",
        "macro_releases_through": "2026-07-29",
        "checks": checks,
        "ready": ready,
    }


def _preflight(
    tmp_path: Path,
    account: Path,
    proposal: Path,
    trial: Path,
    *,
    operations_available: bool = True,
    checked_at: str = "2026-07-30T08:00:00Z",
) -> dict:
    account_envelope = json.loads(account.read_text(encoding="utf-8"))
    proposal_envelope = json.loads(proposal.read_text(encoding="utf-8"))
    trial_document = read_forward_trial(trial)
    payload = {
        "document_kind": "aios.operator_preflight",
        "schema_version": "operator-preflight.v1",
        "read_only": True,
        "execution_boundary": {
            "simulation_only": True,
            "broker_connected": False,
            "broker_orders_enabled": False,
        },
        "checked_at": checked_at,
        "source_dates": {
            "certified_decision_close": "2026-07-29",
            "raw_prices_through": "2026-07-29",
            "fundamentals_through": "2026-07-29",
            "macro_releases_through": "2026-07-29",
        },
        "evidence_identity": {
            "account_id": account_envelope["payload"]["account_id"],
            "proposal_id": proposal_envelope["payload"]["proposal_id"],
            "trial_id": trial_document.payload["trial_id"],
            "account_path": str(account),
            "proposal_path": str(proposal),
            "trial_path": str(trial),
            "account_payload_sha256": account_envelope["payload_sha256"],
            "proposal_payload_sha256": proposal_envelope["payload_sha256"],
            "trial_payload_sha256": trial_document.payload_sha256,
        },
        "capabilities": {
            "research": {"available": True, "blockers": []},
            "proposal_creation": {
                "available": False,
                "blockers": ["active_registered_proposal_exists"],
            },
            "stress_review": {"available": True, "blockers": []},
            "paper_recording": {
                "available": False,
                "blockers": ["proposal_expired"],
            },
            "operations": {
                "available": operations_available,
                "blockers": [] if operations_available else ["case-test"],
            },
            "real_capital": {
                "available": False,
                "blockers": ["broker_disabled"],
            },
        },
        "next_action": {
            "kind": "command",
            "command": "aios anomaly-show case-test",
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {**payload, "payload_sha256": hashlib.sha256(encoded).hexdigest()}


def _write_successor_policy_files(tmp_path: Path) -> None:
    for relative in rollover_module.ROLLOVER_ADDITIONAL_POLICY_FILES:
        policy_file = tmp_path / relative
        policy_file.parent.mkdir(parents=True, exist_ok=True)
        policy_file.write_text(f"# {relative}\nPOLICY = 2\n", encoding="utf-8")


def _baseline(tmp_path: Path) -> tuple[Path, Path, Path]:
    (tmp_path / "policy.py").write_text("WEIGHT = 1\n", encoding="utf-8")
    _write_successor_policy_files(tmp_path)
    account = tmp_path / "data/paper/account.json"
    account_payload = _account_payload()
    _write_document(account, ACCOUNT_DOCUMENT_KIND, account_payload)
    proposal = tmp_path / "data/paper/proposals/us-qv-2026-07-27.json"
    _write_document(
        proposal,
        PROPOSAL_DOCUMENT_KIND,
        _proposal_payload(canonical_payload_sha256(account_payload)),
    )
    trial = tmp_path / "data/paper/trial.json"
    create_forward_trial(
        tmp_path,
        trial,
        account,
        proposal,
        confirm=True,
        now=datetime(2026, 7, 28, 8, 30, tzinfo=UTC),
        policy_files=("policy.py",),
    )
    return account, proposal, trial


class _ProofStore:
    def __init__(self, readiness: dict) -> None:
        self.readiness = readiness


@pytest.fixture(autouse=True)
def proposal_builder_calls(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def build(
        account_path,
        proposal_path,
        as_of,
        store,
        *,
        top_n,
        risk_policy,
        now,
    ):
        calls.append(
            {
                "account_path": Path(account_path),
                "proposal_path": Path(proposal_path),
                "as_of": as_of,
                "store": store,
                "top_n": top_n,
                "now": now,
            }
        )
        account = read_paper_document(account_path, expected_kind=ACCOUNT_DOCUMENT_KIND)
        targets = [
            {
                "ticker": f"T{index:02d}",
                "weight": 1.0 / top_n,
                "sector": "Test",
                "average_daily_dollar_volume": 1_000_000.0,
            }
            for index in range(top_n)
        ]
        payload = {
            "proposal_schema_version": 1,
            "proposal_id": "paper-proof-random-id",
            "account_id": account.payload["account_id"],
            "account_payload_sha256": account.payload_sha256,
            "market": account.payload["market"],
            "universe_id": account.payload["universe_id"],
            "strategy": account.payload["strategy"],
            "mode": "simulation_only",
            "decision_date": as_of.isoformat(),
            "scheduled_simulation_date": "2026-07-30",
            "generated_at": now.isoformat().replace("+00:00", "Z"),
            "readiness": getattr(store, "readiness", _readiness()),
            "risk_policy": asdict(risk_policy),
            "sector_classification": "SEC SIC division",
            "targets": targets,
            "exit_liquidity_evidence": [],
            "selection_skips": [],
            "factor_eligible_count": top_n,
            "factor_evidence_sha256": "a" * 64,
            "decision_evidence_sha256": "b" * 64,
            "risk_assessment": {"approved": True, "checks": []},
            "status": "approved_for_supervised_simulation",
            "notice": "Simulation only.",
        }
        _write_document(proposal_path, PROPOSAL_DOCUMENT_KIND, payload)
        return read_paper_document(proposal_path, expected_kind=PROPOSAL_DOCUMENT_KIND)

    monkeypatch.setattr(rollover_module, "create_paper_proposal", build)
    monkeypatch.setattr(activation_module, "create_paper_proposal", build)
    return calls


def _preview(
    tmp_path: Path,
    account: Path,
    proposal: Path,
    trial: Path,
    *,
    now: datetime = datetime(2026, 7, 30, 8, 0, tzinfo=UTC),
    readiness: dict | None = None,
    operations_available: bool = True,
    preflight_checked_at: str | None = None,
):
    reviewed_readiness = readiness or _readiness()
    return preview_forward_rollover(
        tmp_path,
        trial,
        account,
        store=_ProofStore(reviewed_readiness),
        successor_decision_date=date(2026, 7, 29),
        readiness_evidence=reviewed_readiness,
        operator_preflight_evidence=_preflight(
            tmp_path,
            account,
            proposal,
            trial,
            operations_available=operations_available,
            checked_at=(
                preflight_checked_at or now.astimezone(UTC).isoformat().replace("+00:00", "Z")
            ),
        ),
        now=now,
    )


def _activation_context(tmp_path, monkeypatch):
    monkeypatch.setattr(
        rollover_module,
        "ROLLOVER_ACTIVATION_ENABLED",
        True,
    )
    account, proposal, trial = _baseline(tmp_path)
    database = tmp_path / "data/aios.duckdb"
    Store(database).close()
    operations = tmp_path / "data/operations/alerts.sqlite3"
    AlertStore(operations)
    preview = _preview(tmp_path, account, proposal, trial)
    artifact = persist_forward_rollover_plan(tmp_path, preview)
    fixed = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
    monkeypatch.setattr(activation_module, "_utc_now", lambda: fixed)
    monkeypatch.setattr(
        activation_module,
        "_assess_fresh_readiness",
        lambda decision_date, _store: _readiness(decision_date.isoformat()),
    )
    monkeypatch.setattr(
        activation_module,
        "_assess_fresh_operator_preflight",
        lambda now: _preflight(
            tmp_path,
            account,
            proposal,
            trial,
            checked_at=now.isoformat().replace("+00:00", "Z"),
        ),
    )
    return account, proposal, trial, database, operations, preview, artifact


def _activate(tmp_path, context, *, confirm=True, plan_sha256=None):
    _account, _proposal, _trial, database, operations, preview, artifact = context
    return execute_forward_rollover_from_plan(
        tmp_path,
        artifact,
        preview.plan_sha256 if plan_sha256 is None else plan_sha256,
        database_path=database,
        operations_database_path=operations,
        application_version="test",
        confirm=confirm,
    )


def test_preview_is_deterministic_and_changes_no_governed_state(tmp_path) -> None:
    account, proposal, trial = _baseline(tmp_path)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    first = _preview(tmp_path, account, proposal, trial)
    repeated = _preview(tmp_path, account, proposal, trial)
    later = _preview(
        tmp_path,
        account,
        proposal,
        trial,
        now=datetime(2026, 7, 30, 8, 10, tzinfo=UTC),
    )

    assert first.source_eligible is True
    assert first.activation_available is True
    assert first.plan_sha256 == repeated.plan_sha256
    assert first.plan_sha256 == later.plan_sha256
    assert first.checked_at != later.checked_at
    assert first.observation != later.observation
    assert first.plan["plan_schema_version"] == "forward-rollover-plan.v4"
    assert first.plan["predecessor"]["timing_status"] == "expired"
    assert first.plan["successor"]["decision_date"] == "2026-07-29"
    assert first.plan["successor"]["top_n"] == 2
    assert first.plan["account"]["execution_count"] == 0
    assert len(first.plan["successor"]["readiness_payload_sha256"]) == 64
    detached = first.observation
    detached["source_eligible"] = False
    assert first.source_eligible is True
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not (tmp_path / "data/paper/forward_trials").exists()
    assert not (tmp_path / "data/paper/proposals/rollovers").exists()


def test_activation_requires_confirmation_and_exact_persisted_plan(
    tmp_path,
    monkeypatch,
) -> None:
    context = _activation_context(tmp_path, monkeypatch)
    account, _proposal, trial, _database, _operations, previewed, _artifact = context
    account_bytes = account.read_bytes()
    trial_bytes = trial.read_bytes()

    with pytest.raises(ValueError, match="confirm-rollover"):
        _activate(tmp_path, context, confirm=False)
    with pytest.raises(ValueError, match="content-addressed plan path"):
        _activate(tmp_path, context, plan_sha256="0" * 64)

    assert account.read_bytes() == account_bytes
    assert trial.read_bytes() == trial_bytes
    assert not (tmp_path / "backups").exists()
    assert not (tmp_path / "data/paper/rollovers").exists()


def test_activation_archives_exactly_and_records_no_fill_or_order(
    tmp_path,
    monkeypatch,
) -> None:
    context = _activation_context(tmp_path, monkeypatch)
    account, proposal, trial, _database, _operations, preview, _artifact = context
    account_bytes = account.read_bytes()
    trial_bytes = trial.read_bytes()
    proposal_bytes = proposal.read_bytes()

    result = _activate(tmp_path, context)

    assert verify_local_backup(result.backup.path) == result.backup
    assert result.archived_trial.read_bytes() == trial_bytes
    assert result.archived_proposal.read_bytes() == proposal_bytes
    assert proposal.read_bytes() == proposal_bytes
    assert account.read_bytes() == account_bytes
    account_document = read_paper_document(account, expected_kind=ACCOUNT_DOCUMENT_KIND)
    assert account_document.payload["executions"] == []
    active = read_forward_trial(trial)
    assert active.payload["trial_id"] == result.successor_trial_id
    assert active.payload["trial_id"] != result.predecessor_trial_id
    lineage = active.payload["rollover_lineage"]
    assert lineage["plan_sha256"] == preview.plan_sha256
    assert lineage["disposition"]["kind"] == "no_fill"
    assert lineage["disposition"]["paper_account_mutation"] is False
    assert lineage["disposition"]["broker_order"] is False
    attempt = validate_attempt_directory(result.journal_directory)
    assert attempt.latest.phase == "verified"
    assert attempt.latest.path == result.verified_receipt
    assert attempt.latest.payload["state"]["paper_fill_recorded"] is False
    assert attempt.latest.payload["state"]["broker_order_sent"] is False
    assert (
        attempt.latest.payload["state"]["recovered_after_process_interruption"]
        is False
    )


def test_activation_rechecks_readiness_after_backup_before_any_publication(
    tmp_path,
    monkeypatch,
) -> None:
    context = _activation_context(tmp_path, monkeypatch)
    account, _proposal, trial, _database, _operations, previewed, _artifact = context
    account_bytes = account.read_bytes()
    trial_bytes = trial.read_bytes()
    calls = 0

    def changing_readiness(decision_date, _store):
        nonlocal calls
        calls += 1
        evidence = _readiness(decision_date.isoformat())
        if calls == 2:
            evidence["checks"][-1]["status"] = "fail"
            evidence["checks"][-1]["observed"] = "blocked"
            evidence["ready"] = False
        return evidence

    monkeypatch.setattr(
        activation_module,
        "_assess_fresh_readiness",
        changing_readiness,
    )
    with pytest.raises(ValueError, match="no longer matches|refused activation"):
        _activate(tmp_path, context)

    assert calls == 2
    assert account.read_bytes() == account_bytes
    assert trial.read_bytes() == trial_bytes
    assert len(list((tmp_path / "backups").iterdir())) == 1
    assert not (tmp_path / "data/paper/rollovers").exists()
    assert not (tmp_path / previewed.plan["successor"]["proposal_path"]).exists()


def test_activation_final_policy_cas_rolls_back_published_outputs(
    tmp_path,
    monkeypatch,
) -> None:
    context = _activation_context(tmp_path, monkeypatch)
    account, _proposal, trial, _database, _operations, preview, _artifact = context
    account_bytes = account.read_bytes()
    trial_bytes = trial.read_bytes()
    original_publish = activation_module._publish_rollover_outputs

    def publish_then_drift(root, paths, candidate):
        original_publish(root, paths, candidate)
        (tmp_path / "policy.py").write_text("WEIGHT = 2\n", encoding="utf-8")

    monkeypatch.setattr(
        activation_module,
        "_publish_rollover_outputs",
        publish_then_drift,
    )
    with pytest.raises(RuntimeError, match="policy file"):
        _activate(tmp_path, context)

    assert account.read_bytes() == account_bytes
    assert trial.read_bytes() == trial_bytes
    assert not (tmp_path / preview.plan["successor"]["proposal_path"]).exists()
    assert not (
        tmp_path / preview.plan["predecessor"]["trial_archive_path"]
    ).exists()
    attempts = [
        validate_attempt_directory(path)
        for path in scan_attempt_directories(tmp_path)
    ]
    assert len(attempts) == 1
    assert attempts[0].latest.phase == "recovered_rolled_back"


def test_recovery_rolls_forward_after_process_loss_immediately_after_swap(
    tmp_path,
    monkeypatch,
) -> None:
    context = _activation_context(tmp_path, monkeypatch)
    account, _proposal, trial, _database, _operations, preview, artifact = context
    account_bytes = account.read_bytes()
    original_write = activation_module._write_followup_phase
    interrupted = False

    def interrupt_after_swap(journal, prepared, *, phase, state):
        nonlocal interrupted
        if phase == "active_swapped" and not interrupted:
            interrupted = True
            raise SystemExit("simulated process loss")
        return original_write(
            journal,
            prepared,
            phase=phase,
            state=state,
        )

    monkeypatch.setattr(
        activation_module,
        "_write_followup_phase",
        interrupt_after_swap,
    )
    with pytest.raises(SystemExit, match="simulated process loss"):
        _activate(tmp_path, context)

    incomplete = [
        validate_attempt_directory(path)
        for path in scan_attempt_directories(tmp_path)
    ]
    assert len(incomplete) == 1
    assert incomplete[0].terminal is False
    assert read_forward_trial(trial).payload["rollover_lineage"]["plan_sha256"] == (
        preview.plan_sha256
    )

    # A release can be emergency-disabled after an attempt starts. Recovery
    # must still accept that exact historical enabled plan without enabling a
    # new activation.
    monkeypatch.setattr(rollover_module, "ROLLOVER_ACTIVATION_ENABLED", False)
    recovered = rollover_module.recover_forward_rollover_from_plan(
        tmp_path,
        artifact,
        preview.plan_sha256,
        confirm=True,
    )

    assert len(recovered) == 1
    assert recovered[0].terminal_phase == "verified"
    recovered_attempt = validate_attempt_directory(recovered[0].journal_directory)
    assert recovered_attempt.terminal is True
    assert (
        recovered_attempt.latest.payload["state"][
            "recovered_after_process_interruption"
        ]
        is True
    )
    assert account.read_bytes() == account_bytes


def test_journal_rejects_checksum_consistent_terminal_schema_extension(
    tmp_path,
    monkeypatch,
) -> None:
    context = _activation_context(tmp_path, monkeypatch)
    result = _activate(tmp_path, context)
    receipt = result.verified_receipt
    envelope = json.loads(receipt.read_text(encoding="utf-8"))
    envelope["payload"]["state"]["unreviewed_extension"] = True
    envelope["payload_sha256"] = canonical_payload_sha256(envelope["payload"])
    receipt.write_text(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="verified evidence is invalid"):
        validate_attempt_directory(result.journal_directory)


def test_recovery_rolls_back_when_process_stops_before_active_swap(
    tmp_path,
    monkeypatch,
) -> None:
    context = _activation_context(tmp_path, monkeypatch)
    account, _proposal, trial, _database, _operations, preview, artifact = context
    account_bytes = account.read_bytes()
    trial_bytes = trial.read_bytes()

    def interrupt_before_swap(*_args, **_kwargs):
        raise SystemExit("simulated process loss")

    monkeypatch.setattr(
        activation_module,
        "_final_activation_recheck",
        interrupt_before_swap,
    )
    with pytest.raises(SystemExit, match="simulated process loss"):
        _activate(tmp_path, context)

    recovered = rollover_module.recover_forward_rollover_from_plan(
        tmp_path,
        artifact,
        preview.plan_sha256,
        confirm=True,
    )

    assert len(recovered) == 1
    assert recovered[0].terminal_phase == "recovered_rolled_back"
    assert account.read_bytes() == account_bytes
    assert trial.read_bytes() == trial_bytes
    assert not (tmp_path / preview.plan["successor"]["proposal_path"]).exists()


def test_restore_accepts_verified_rollover_and_reconciles_lineage(
    tmp_path,
    monkeypatch,
) -> None:
    context = _activation_context(tmp_path, monkeypatch)
    _account, _proposal, trial, database, operations, _preview, _artifact = context
    result = _activate(tmp_path, context)
    source = create_local_backup(
        tmp_path,
        database,
        operations_database_path=operations,
        output=tmp_path / "post-rollover-backup",
        application_version="test",
    )

    target = tmp_path / "restore-target"
    target_database = target / "data/aios.duckdb"
    Store(target_database).close()
    live = target / "data/paper/live.json"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text('{"live":true}', encoding="utf-8")

    restored = restore_local_backup(
        source.path,
        target,
        target_database,
        application_version="test",
        confirm=True,
        now=datetime(2026, 7, 30, 9, 0, tzinfo=UTC),
    )

    restored_trial = target / trial.relative_to(tmp_path)
    assert restored_trial.read_bytes() == result.active_trial.read_bytes()
    assert read_forward_trial(restored_trial).payload["rollover_lineage"][
        "attempt_id"
    ] == result.attempt_id
    assert not live.exists()
    assert verify_local_backup(restored.safety_backup).path == restored.safety_backup


def test_restore_rejects_nonterminal_rollover_before_live_swap(
    tmp_path,
    monkeypatch,
) -> None:
    context = _activation_context(tmp_path, monkeypatch)
    _account, _proposal, _trial, database, operations, _preview, _artifact = context

    def interrupt_before_swap(*_args, **_kwargs):
        raise SystemExit("simulated process loss")

    monkeypatch.setattr(
        activation_module,
        "_final_activation_recheck",
        interrupt_before_swap,
    )
    with pytest.raises(SystemExit):
        _activate(tmp_path, context)
    source = create_local_backup(
        tmp_path,
        database,
        operations_database_path=operations,
        output=tmp_path / "incomplete-rollover-backup",
        application_version="test",
    )

    target = tmp_path / "restore-target"
    target_database = target / "data/aios.duckdb"
    Store(target_database).close()
    live = target / "data/paper/live.json"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text('{"live":true}', encoding="utf-8")
    database_bytes = target_database.read_bytes()
    live_bytes = live.read_bytes()

    with pytest.raises(ValueError, match="incomplete rollover transaction"):
        restore_local_backup(
            source.path,
            target,
            target_database,
            application_version="test",
            confirm=True,
        )

    assert target_database.read_bytes() == database_bytes
    assert live.read_bytes() == live_bytes
    assert not list((target / "backups").glob("pre-restore-*"))


def test_plan_artifact_is_content_addressed_write_once_and_read_only(tmp_path) -> None:
    account, proposal, trial = _baseline(tmp_path)
    preview = _preview(tmp_path, account, proposal, trial)
    governed_before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in (account, proposal, trial)
    }

    artifact = persist_forward_rollover_plan(tmp_path, preview)

    assert artifact == (
        tmp_path
        / "data/reports/forward_rollovers/plans"
        / f"{preview.plan_sha256}.json"
    )
    envelope = json.loads(artifact.read_text(encoding="utf-8"))
    assert envelope["document_kind"] == ROLLOVER_PLAN_DOCUMENT_KIND
    assert envelope["schema_version"] == "forward-rollover-plan.v4"
    assert envelope["read_only"] is True
    assert envelope["payload_sha256"] == preview.plan_sha256
    assert envelope["payload"] == preview.plan
    assert "checked_at" not in envelope
    assert "observation" not in envelope
    assert read_forward_rollover_plan(artifact) == preview.plan
    assert persist_forward_rollover_plan(tmp_path, preview) == artifact
    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in (account, proposal, trial)
    } == governed_before


def test_plan_artifact_refuses_incomplete_preview_source_drift_and_collision(
    tmp_path,
) -> None:
    account, proposal, trial = _baseline(tmp_path)
    blocked = _preview(
        tmp_path,
        account,
        proposal,
        trial,
        readiness=_readiness(ready=False),
    )
    with pytest.raises(ValueError, match="plan is incomplete"):
        persist_forward_rollover_plan(tmp_path, blocked)

    preview = _preview(tmp_path, account, proposal, trial)
    destination = (
        tmp_path
        / "data/reports/forward_rollovers/plans"
        / f"{preview.plan_sha256}.json"
    )
    destination.parent.mkdir(parents=True)
    destination.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact collision"):
        persist_forward_rollover_plan(tmp_path, preview)

    destination.unlink()
    account.write_bytes(account.read_bytes() + b"\n")
    with pytest.raises(RuntimeError, match="paper account changed"):
        persist_forward_rollover_plan(tmp_path, preview)
    assert not destination.exists()


def test_plan_artifact_rejects_symlinked_report_namespace_and_tampering(
    tmp_path,
) -> None:
    account, proposal, trial = _baseline(tmp_path)
    preview = _preview(tmp_path, account, proposal, trial)
    reports = tmp_path / "data/reports"
    reports.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (reports / "forward_rollovers").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinks"):
        persist_forward_rollover_plan(tmp_path, preview)
    assert list(outside.iterdir()) == []

    (reports / "forward_rollovers").unlink()
    destination = (
        reports
        / "forward_rollovers/plans"
        / f"{preview.plan_sha256}.json"
    )
    destination.parent.mkdir(parents=True)
    hardlink_source = outside / "candidate.json"
    hardlink_source.write_text(preview.canonical_plan_artifact_json() + "\n")
    destination.hardlink_to(hardlink_source)
    with pytest.raises(ValueError, match="unalias"):
        persist_forward_rollover_plan(tmp_path, preview)
    destination.unlink()

    artifact = persist_forward_rollover_plan(tmp_path, preview)
    envelope = json.loads(artifact.read_text(encoding="utf-8"))
    envelope["payload"]["account"]["execution_count"] = 99
    artifact.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch"):
        read_forward_rollover_plan(artifact)


def test_plan_artifact_accepts_only_an_identical_concurrent_publication(
    tmp_path,
    monkeypatch,
) -> None:
    account, proposal, trial = _baseline(tmp_path)
    preview = _preview(tmp_path, account, proposal, trial)
    publish = rollover_module.publish_text_write_once

    def identical_race(path: Path, text: str) -> None:
        publish(path, text)
        raise FileExistsError(path)

    monkeypatch.setattr(
        rollover_module,
        "publish_text_write_once",
        identical_race,
    )

    artifact = persist_forward_rollover_plan(tmp_path, preview)

    assert read_forward_rollover_plan(artifact) == preview.plan


def test_preview_binds_readiness_and_operations_as_separate_gates(tmp_path) -> None:
    account, proposal, trial = _baseline(tmp_path)

    blocked_readiness = _preview(
        tmp_path,
        account,
        proposal,
        trial,
        readiness=_readiness(ready=False),
    )
    blocked_operations = _preview(
        tmp_path,
        account,
        proposal,
        trial,
        operations_available=False,
    )

    assert "successor_research_not_ready" in blocked_readiness.observation["blockers"]
    assert "operator_operations_not_available" in blocked_operations.observation["blockers"]
    assert blocked_readiness.plan_sha256 != blocked_operations.plan_sha256
    eligible = _preview(tmp_path, account, proposal, trial)
    assert blocked_operations.plan_sha256 == eligible.plan_sha256
    assert blocked_operations.plan == eligible.plan
    assert blocked_operations.plan_complete is True
    assert blocked_operations.source_eligible is False


def test_preview_blocks_mismatched_exact_date_evidence(tmp_path) -> None:
    account, proposal, trial = _baseline(tmp_path)

    preview = _preview(
        tmp_path,
        account,
        proposal,
        trial,
        readiness=_readiness("2026-07-28"),
    )

    assert preview.source_eligible is False
    assert "successor_readiness_date_mismatch" in preview.observation["blockers"]


def test_preview_requires_exactly_one_unresolved_proposal(tmp_path) -> None:
    account, proposal, trial = _baseline(tmp_path)
    next_proposal = tmp_path / "data/paper/proposals/us-qv-2026-07-28.json"
    account_sha256 = json.loads(account.read_text(encoding="utf-8"))["payload_sha256"]
    _write_document(
        next_proposal,
        PROPOSAL_DOCUMENT_KIND,
        _proposal_payload(
            account_sha256,
            proposal_id="paper-2026-07-28-test",
            decision_date="2026-07-28",
            scheduled_date="2026-07-29",
        ),
    )
    register_forward_proposal(
        tmp_path,
        trial,
        account,
        next_proposal,
        now=datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
    )

    preview = _preview(tmp_path, account, proposal, trial)

    assert preview.source_eligible is False
    assert "multiple_unresolved_registered_proposals" in preview.observation["blockers"]


def test_preview_requires_registry_and_proposal_lifecycle_parity(tmp_path) -> None:
    account, proposal, trial = _baseline(tmp_path)
    envelope = json.loads(trial.read_text(encoding="utf-8"))
    envelope["payload"]["proposals"][0]["generated_at"] = "2026-07-28T08:01:00Z"
    envelope["payload_sha256"] = canonical_payload_sha256(envelope["payload"])
    trial.write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")

    preview = _preview(tmp_path, account, proposal, trial)

    assert preview.source_eligible is False
    assert "registered_proposal_generated_at_mismatch" in preview.observation["blockers"]


def test_preview_rejects_tampered_preflight_checksum(tmp_path) -> None:
    account, proposal, trial = _baseline(tmp_path)
    preflight = _preflight(tmp_path, account, proposal, trial)
    preflight["capabilities"]["operations"]["available"] = False

    with pytest.raises(ValueError, match="preflight checksum mismatch"):
        preview_forward_rollover(
            tmp_path,
            trial,
            account,
            store=_ProofStore(_readiness()),
            successor_decision_date=date(2026, 7, 29),
            readiness_evidence=_readiness(),
            operator_preflight_evidence=preflight,
            now=datetime(2026, 7, 30, 8, 0, tzinfo=UTC),
        )


def test_preview_rejects_symlinked_governed_input(tmp_path) -> None:
    account, proposal, trial = _baseline(tmp_path)
    alias = tmp_path / "data/paper/trial-alias.json"
    alias.symlink_to(trial)

    with pytest.raises(ValueError, match="symlinks"):
        _preview(tmp_path, account, proposal, alias)


def test_preview_detects_account_change_while_plan_is_built(
    tmp_path,
    monkeypatch,
) -> None:
    account, proposal, trial = _baseline(tmp_path)
    original = rollover_module.paper_proposal_timing_status

    def mutate_account(*args, **kwargs):
        result = original(*args, **kwargs)
        account.write_bytes(account.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(
        rollover_module,
        "paper_proposal_timing_status",
        mutate_account,
    )

    with pytest.raises(RuntimeError, match="paper account changed"):
        _preview(tmp_path, account, proposal, trial)


def test_preview_does_not_resolve_same_id_with_a_different_execution_hash(
    tmp_path,
) -> None:
    account, proposal, trial = _baseline(tmp_path)
    changed = _account_payload()
    changed["executions"] = [
        {
            "proposal_id": "paper-2026-07-27-test",
            "proposal_payload_sha256": "0" * 64,
        }
    ]
    _write_document(account, ACCOUNT_DOCUMENT_KIND, changed)

    preview = _preview(tmp_path, account, proposal, trial)

    assert preview.source_eligible is False
    assert "proposal_execution_checksum_mismatch" in preview.observation["blockers"]
    assert preview.plan["predecessor"]["proposal_id"] == "paper-2026-07-27-test"


def test_preview_binds_frozen_risk_and_target_configuration(tmp_path) -> None:
    account, proposal, trial = _baseline(tmp_path)
    envelope = json.loads(trial.read_text(encoding="utf-8"))
    envelope["payload"]["frozen_configuration"]["risk_policy"] = {"maximum_position_weight": 0.25}
    envelope["payload"]["frozen_configuration"]["top_n"] = 3
    envelope["payload_sha256"] = canonical_payload_sha256(envelope["payload"])
    trial.write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")

    preview = _preview(tmp_path, account, proposal, trial)

    assert {
        "registered_proposal_risk_policy_mismatch",
        "registered_proposal_target_count_mismatch",
    }.issubset(preview.observation["blockers"])


@pytest.mark.parametrize(
    ("field", "value", "blocker"),
    [
        (
            "benchmark_ticker",
            "QQQ",
            "successor_readiness_benchmark_mismatch",
        ),
        (
            "certified_research_through",
            "2026-07-28",
            "successor_readiness_not_certified_for_exact_decision",
        ),
    ],
)
def test_preview_requires_exact_market_certification(
    tmp_path,
    field: str,
    value: str,
    blocker: str,
) -> None:
    account, proposal, trial = _baseline(tmp_path)
    readiness = _readiness()
    readiness[field] = value

    preview = _preview(
        tmp_path,
        account,
        proposal,
        trial,
        readiness=readiness,
    )

    assert preview.source_eligible is False
    assert blocker in preview.observation["blockers"]


def test_final_cas_rechecks_regular_unaliased_file_shape(tmp_path) -> None:
    account, _proposal, _trial = _baseline(tmp_path)
    expected = hashlib.sha256(account.read_bytes()).hexdigest()
    alias = account.with_name("account-hardlink.json")
    alias.hardlink_to(account)

    with pytest.raises(ValueError, match="regular unaliased"):
        rollover_module._require_file_unchanged(
            tmp_path,
            account,
            expected,
            label="paper account",
        )


def test_preview_selects_latest_unresolved_by_evidence_not_list_order(
    tmp_path,
) -> None:
    (tmp_path / "policy.py").write_text("WEIGHT = 1\n", encoding="utf-8")
    _write_successor_policy_files(tmp_path)
    account = tmp_path / "data/paper/account.json"
    initial_account = _account_payload()
    _write_document(account, ACCOUNT_DOCUMENT_KIND, initial_account)
    old_proposal = tmp_path / "data/paper/proposals/us-qv-2026-07-20.json"
    old_payload = _proposal_payload(
        canonical_payload_sha256(initial_account),
        proposal_id="paper-2026-07-20-old",
        decision_date="2026-07-20",
        scheduled_date="2026-07-21",
    )
    _write_document(old_proposal, PROPOSAL_DOCUMENT_KIND, old_payload)
    trial = tmp_path / "data/paper/trial.json"
    create_forward_trial(
        tmp_path,
        trial,
        account,
        old_proposal,
        confirm=True,
        now=datetime(2026, 7, 20, 20, 5, tzinfo=UTC),
        policy_files=("policy.py",),
    )

    current_account = _account_payload()
    current_account["executions"] = [
        {
            "proposal_id": old_payload["proposal_id"],
            "proposal_payload_sha256": canonical_payload_sha256(old_payload),
        }
    ]
    _write_document(account, ACCOUNT_DOCUMENT_KIND, current_account)
    latest = tmp_path / "data/paper/proposals/us-qv-2026-07-27.json"
    _write_document(
        latest,
        PROPOSAL_DOCUMENT_KIND,
        _proposal_payload(canonical_payload_sha256(current_account)),
    )
    register_forward_proposal(
        tmp_path,
        trial,
        account,
        latest,
        now=datetime(2026, 7, 28, 8, 5, tzinfo=UTC),
    )
    envelope = json.loads(trial.read_text(encoding="utf-8"))
    envelope["payload"]["proposals"].reverse()
    envelope["payload_sha256"] = canonical_payload_sha256(envelope["payload"])
    trial.write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")

    preview = _preview(tmp_path, account, latest, trial)

    assert preview.source_eligible is True
    assert preview.plan["predecessor"]["proposal_id"] == "paper-2026-07-27-test"


def test_current_release_exposes_only_plan_bound_rollover_activation() -> None:
    project_root = Path(__file__).resolve().parents[1]

    assert not (project_root / "src/aios/rollover.py").exists()
    assert callable(execute_forward_rollover_from_plan)
    assert ForwardRolloverResult.__name__ == "ForwardRolloverResult"
    assert rollover_module.ROLLOVER_ACTIVATION_ENABLED is True


def test_preview_binds_complete_policy_set_and_disposable_production_intent(
    tmp_path,
    proposal_builder_calls,
) -> None:
    account, proposal, trial = _baseline(tmp_path)

    preview = _preview(tmp_path, account, proposal, trial)

    policy_files = preview.plan["successor"]["policy_files"]
    assert set(rollover_module.ROLLOVER_ADDITIONAL_POLICY_FILES).issubset(policy_files)
    assert len(proposal_builder_calls) == 1
    staged = proposal_builder_calls[0]["proposal_path"]
    assert not staged.is_relative_to(tmp_path)
    assert not staged.exists()
    intent = preview.plan["successor"]["normalized_proposal_blueprint"]
    assert intent["proposal_id"] == "<generated-at-activation>"
    assert intent["generated_at"] == "<generated-at-activation>"
    assert len(intent["targets"]) == 2
    assert intent["risk_assessment"]["approved"] is True
    assert intent["factor_evidence_sha256"] == "a" * 64
    assert intent["decision_evidence_sha256"] == "b" * 64


def test_preview_allows_exact_archived_registration_and_blocks_custom_orphan(
    tmp_path,
) -> None:
    account, proposal, trial = _baseline(tmp_path)
    account_sha256 = json.loads(account.read_text(encoding="utf-8"))["payload_sha256"]
    archived_proposal = tmp_path / "data/paper/proposals/custom-archived-name.json"
    _write_document(
        archived_proposal,
        PROPOSAL_DOCUMENT_KIND,
        _proposal_payload(
            account_sha256,
            proposal_id="paper-2026-07-20-archived",
            decision_date="2026-07-20",
            scheduled_date="2026-07-21",
        ),
    )
    archived_trial = tmp_path / "data/paper/forward_trials/legitimate-archive.json"
    create_forward_trial(
        tmp_path,
        archived_trial,
        account,
        archived_proposal,
        confirm=True,
        now=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
        policy_files=("policy.py",),
    )

    allowed = _preview(tmp_path, account, proposal, trial)

    assert allowed.source_eligible is True
    orphan = tmp_path / "data/paper/proposals/custom-orphan.json"
    orphan.write_text("{}", encoding="utf-8")
    blocked = _preview(tmp_path, account, proposal, trial)
    assert blocked.source_eligible is False
    assert (
        "unregistered_governed_proposal:data/paper/proposals/custom-orphan.json"
        in blocked.observation["blockers"]
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda evidence: evidence["checks"].pop(),
        lambda evidence: evidence["checks"][0].__setitem__("status", "unknown"),
        lambda evidence: evidence.__setitem__("ready", False),
    ],
)
def test_preview_rejects_noncanonical_readiness_schema(tmp_path, mutate) -> None:
    account, proposal, trial = _baseline(tmp_path)
    readiness = _readiness()
    mutate(readiness)

    with pytest.raises(ValueError, match="readiness evidence"):
        _preview(
            tmp_path,
            account,
            proposal,
            trial,
            readiness=readiness,
        )


def test_preview_hash_binds_readiness_but_excludes_fresh_preflight_time(tmp_path) -> None:
    account, proposal, trial = _baseline(tmp_path)
    readiness = _readiness()
    first = _preview(tmp_path, account, proposal, trial, readiness=readiness)
    changed = _readiness()
    changed["checks"][0]["detail"] = "Different reviewed decision-date evidence."
    second = _preview(tmp_path, account, proposal, trial, readiness=changed)

    assert first.plan_sha256 != second.plan_sha256
    assert (
        first.observation["operator_preflight"]["checked_at"]
        == "2026-07-30T08:00:00Z"
    )
    assert "operator_preflight_evidence" not in first.plan["successor"]
    assert "operations_evidence_sha256" not in first.plan["successor"]

    stale = _preview(
        tmp_path,
        account,
        proposal,
        trial,
        preflight_checked_at="2026-07-30T07:57:59Z",
    )
    assert stale.source_eligible is False
    assert "operator_preflight_stale" in stale.observation["blockers"]


def test_preview_fails_closed_when_exact_production_constructor_fails(
    tmp_path,
    monkeypatch,
) -> None:
    account, proposal, trial = _baseline(tmp_path)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    def refuse(*_args, **_kwargs):
        raise ValueError("factor evidence unavailable")

    monkeypatch.setattr(rollover_module, "create_paper_proposal", refuse)
    with pytest.raises(ValueError, match="factor evidence unavailable"):
        _preview(tmp_path, account, proposal, trial)
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before

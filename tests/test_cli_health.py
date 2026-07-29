from __future__ import annotations

from contextlib import nullcontext
from datetime import date
from types import SimpleNamespace

from typer.testing import CliRunner

from aios import cli
from aios import forward as forward_module
from aios import paper as paper_module


def test_health_blocks_when_frozen_forward_policy_drifted(tmp_path, monkeypatch) -> None:
    account_path = tmp_path / "data" / "paper" / "us_qv_sandbox.json"
    forward_path = tmp_path / "data" / "paper" / "us_qv_forward_trial.json"
    account_path.parent.mkdir(parents=True)
    account_path.write_text("test", encoding="utf-8")
    forward_path.write_text("test", encoding="utf-8")

    readiness = SimpleNamespace(
        ready=True,
        blockers=(),
        checks=(
            SimpleNamespace(
                check="data_integrity",
                status="warn",
                observed="0 failures, 3 warnings",
            ),
        ),
        raw_prices_through="2026-07-24",
        fundamentals_through="2026-07-24",
        macro_releases_through="2026-07-24",
    )
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    scopes = []

    def scoped_store(**kwargs):
        scopes.append(kwargs)
        return nullcontext(object())

    monkeypatch.setattr(cli, "store_scope", scoped_store)
    monkeypatch.setattr(cli, "assess_us_readiness", lambda *args, **kwargs: readiness)
    monkeypatch.setattr(
        paper_module,
        "latest_paper_decision_date",
        lambda store: date(2026, 7, 24),
    )
    monkeypatch.setattr(
        paper_module,
        "paper_account_summary",
        lambda path, store: {"equity": 100_000.0, "holdings": []},
    )
    monkeypatch.setattr(
        forward_module,
        "assess_forward_trial",
        lambda *args, **kwargs: SimpleNamespace(
            ready=False,
            registered_proposals=2,
            issues=("frozen policy files changed", "frozen bundle changed"),
        ),
    )
    emitted = []
    monkeypatch.setattr(cli, "_emit_operational_alert", emitted.append)
    monkeypatch.setattr(cli, "_resolve_operational_alert", lambda fingerprint: None)

    result = CliRunner().invoke(cli.app, ["health"])

    assert result.exit_code == 1
    assert "Forward-test policy" in result.output
    assert "blocked; 2 policy issue(s) require review" in result.output
    assert "Certified decision date: 2026-07-24" in result.output
    assert "RESEARCH READY — forward paper execution is blocked" in result.output
    assert [alert.code for alert in emitted] == ["forward_policy_drift"]
    assert scopes == [{"read_only": True}]


def test_health_labels_a_blocked_readiness_date_as_a_candidate(tmp_path, monkeypatch) -> None:
    readiness = SimpleNamespace(
        ready=False,
        blockers=(SimpleNamespace(check="data_integrity"),),
        checks=(
            SimpleNamespace(
                check="data_integrity",
                status="fail",
                observed="1 failure, 3 warnings",
            ),
        ),
        raw_prices_through="2026-07-27",
        fundamentals_through="2026-07-27",
        macro_releases_through="2026-07-27",
    )
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(
        cli,
        "store_scope",
        lambda **kwargs: nullcontext(object()),
    )
    monkeypatch.setattr(cli, "assess_us_readiness", lambda *args, **kwargs: readiness)
    monkeypatch.setattr(
        paper_module,
        "latest_paper_decision_date",
        lambda store: date(2026, 7, 27),
    )
    monkeypatch.setattr(cli, "_emit_operational_alert", lambda alert: None)
    monkeypatch.setattr(cli, "_resolve_operational_alert", lambda fingerprint: None)

    result = CliRunner().invoke(cli.app, ["health"])

    assert result.exit_code == 1
    assert "Blocked decision-date candidate: 2026-07-27" in result.output
    assert "Certified decision date" not in result.output
    assert "BLOCKED — do not create or record a paper proposal" in result.output


def test_health_report_only_does_not_mutate_incident_lifecycle(
    tmp_path,
    monkeypatch,
) -> None:
    readiness = SimpleNamespace(
        ready=False,
        blockers=(SimpleNamespace(check="data_integrity"),),
        checks=(
            SimpleNamespace(
                check="data_integrity",
                status="fail",
                observed="1 failure, 3 warnings",
            ),
        ),
        raw_prices_through="2026-07-27",
        fundamentals_through="2026-07-27",
        macro_releases_through="2026-07-27",
    )
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(
        cli,
        "store_scope",
        lambda **kwargs: nullcontext(object()),
    )
    monkeypatch.setattr(cli, "assess_us_readiness", lambda *args, **kwargs: readiness)
    monkeypatch.setattr(
        paper_module,
        "latest_paper_decision_date",
        lambda store: date(2026, 7, 27),
    )
    mutations = []
    monkeypatch.setattr(
        cli,
        "_emit_operational_alert",
        lambda alert: mutations.append(("emit", alert.code)),
    )
    monkeypatch.setattr(
        cli,
        "_resolve_operational_alert",
        lambda fingerprint: mutations.append(("resolve", fingerprint)),
    )

    result = CliRunner().invoke(cli.app, ["health", "--report-only"])

    assert result.exit_code == 0
    assert "BLOCKED — do not create or record a paper proposal" in result.output
    assert mutations == []

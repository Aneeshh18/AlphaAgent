from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import date
from types import SimpleNamespace

from typer.testing import CliRunner

import aios.cli as cli
from aios.readiness import ReadinessCheck, USReadinessReport


def _report(*, ready: bool) -> USReadinessReport:
    status = "pass" if ready else "fail"
    return USReadinessReport(
        as_of="2026-07-21",
        purpose="paper",
        generated_on="2026-07-21",
        universe_id="sp500",
        benchmark_ticker="SPY",
        certified_research_from="2023-08-01",
        certified_research_through="2024-12-31",
        raw_prices_through="2026-07-15",
        fundamentals_through="2026-07-17",
        macro_releases_through="2026-07-15",
        checks=(
            ReadinessCheck(
                check="universe_membership",
                label="Dated investable universe",
                status=status,
                observed="503 members" if ready else "0 members",
                required="450–550 publicly known members",
                detail="test evidence",
            ),
        ),
    )


def test_readiness_is_a_strict_machine_gate_by_default(monkeypatch) -> None:
    store = object()
    scopes = []

    def scoped_store(**kwargs):
        scopes.append(kwargs)
        return nullcontext(store)

    def assess(*args, **kwargs):
        assert kwargs["store"] is store
        return _report(ready=False)

    monkeypatch.setattr(cli, "store_scope", scoped_store)
    monkeypatch.setattr(cli, "assess_us_readiness", assess)

    result = CliRunner().invoke(cli.app, ["readiness", "--as-of", "2026-07-21"])

    assert result.exit_code == 1
    assert "BLOCKED" in result.output
    assert "Broad-coverage candidate window" in result.output
    assert "2023-08-01 through 2024-12-31" in result.output
    assert scopes == [{"read_only": True}]


def test_readiness_report_only_writes_machine_readable_evidence(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(cli, "store_scope", lambda **kwargs: nullcontext(object()))
    monkeypatch.setattr(cli, "assess_us_readiness", lambda *args, **kwargs: _report(ready=False))
    output = tmp_path / "data" / "reports" / "readiness" / "readiness.json"

    result = CliRunner().invoke(
        cli.app,
        [
            "readiness",
            "--as-of",
            "2026-07-21",
            "--report-only",
            "--json-output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["ready"] is False

    original = output.read_bytes()
    repeated = CliRunner().invoke(
        cli.app,
        [
            "readiness",
            "--as-of",
            "2026-07-21",
            "--report-only",
            "--json-output",
            str(output),
        ],
    )

    assert repeated.exit_code == 1
    assert "already exists" in repeated.output
    assert output.read_bytes() == original


def test_readiness_output_cannot_overwrite_governed_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(cli, "store_scope", lambda **kwargs: nullcontext(object()))
    monkeypatch.setattr(cli, "assess_us_readiness", lambda *args, **kwargs: _report(ready=True))
    account = tmp_path / "data" / "paper" / "us_qv_sandbox.json"
    account.parent.mkdir(parents=True)
    account.write_text('{"governed":true}\n', encoding="utf-8")

    result = CliRunner().invoke(
        cli.app,
        [
            "readiness",
            "--as-of",
            "2026-07-21",
            "--report-only",
            "--json-output",
            str(account),
        ],
    )

    assert result.exit_code == 1
    assert "must stay under" in result.output
    assert "data/reports/readiness" in result.output
    assert account.read_text(encoding="utf-8") == '{"governed":true}\n'


def test_readiness_without_as_of_uses_latest_reviewed_decision_date(monkeypatch) -> None:
    store = object()
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        cli,
        "store_scope",
        lambda **kwargs: nullcontext(store),
    )

    def latest_decision_date(candidate_store):
        observed["store"] = candidate_store
        return date(2026, 7, 28)

    monkeypatch.setattr(
        "aios.paper.latest_paper_decision_date",
        latest_decision_date,
    )

    def assess(decision_date, **kwargs):
        observed.update(decision_date=decision_date, assessment_store=kwargs["store"])
        return _report(ready=True)

    monkeypatch.setattr(cli, "assess_us_readiness", assess)

    result = CliRunner().invoke(cli.app, ["readiness", "--report-only"])

    assert result.exit_code == 0
    assert observed == {
        "store": store,
        "decision_date": "2026-07-28",
        "assessment_store": store,
    }


def test_readiness_rejects_unknown_purpose_before_querying(monkeypatch) -> None:
    called = False

    def fake_assess(*args, **kwargs):
        nonlocal called
        called = True
        return _report(ready=True)

    monkeypatch.setattr(cli, "assess_us_readiness", fake_assess)

    result = CliRunner().invoke(cli.app, ["readiness", "--purpose", "live-trading"])

    assert result.exit_code == 1
    assert "purpose must be" in result.output
    assert called is False

from __future__ import annotations

import json

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
    monkeypatch.setattr(cli, "assess_us_readiness", lambda *args, **kwargs: _report(ready=False))

    result = CliRunner().invoke(cli.app, ["readiness", "--as-of", "2026-07-21"])

    assert result.exit_code == 1
    assert "BLOCKED" in result.output
    assert "2023-08-01 through 2024-12-31" in result.output


def test_readiness_report_only_writes_machine_readable_evidence(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli, "assess_us_readiness", lambda *args, **kwargs: _report(ready=False))
    output = tmp_path / "readiness.json"

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

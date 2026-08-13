"""Contracts for `paper-propose --trial` and `--factor-model qvml`.

`create_paper_proposal` is frozen `paper.py`: it always reads
`row.qv_score`/`row.qv_rank`, always calls its `composite_computer` with
`include_market_factors=False`, and always writes `payload["strategy"] = "qv"`.
These tests pin the CLI-level workaround that gets a genuine QVML-selected
proposal out of that contract without editing the frozen file — the
`composite_computer` dependency-injection point, plus a write-once sidecar
declaring the override, since the proposal's own `strategy` field cannot say
so honestly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aios import cli
from aios.factors.composite import CompositeRow


def _row(ticker: str, *, qv: float, qvml: float, qv_rank: int, qvml_rank: int) -> CompositeRow:
    return CompositeRow(
        ticker=ticker,
        as_of="2026-08-07",
        qv_score=qv,
        qv_rank=qv_rank,
        qvml_score=qvml,
        qvml_rank=qvml_rank,
    )


def test_qvml_selecting_computer_overwrites_qv_fields_from_qvml(monkeypatch) -> None:
    captured_kwargs: dict = {}

    def fake_compute_composite(tickers, as_of, store, **kwargs):
        captured_kwargs.update(kwargs)
        return [
            _row("AAA", qv=80.0, qvml=20.0, qv_rank=1, qvml_rank=3),
            _row("BBB", qv=10.0, qvml=95.0, qv_rank=3, qvml_rank=1),
        ]

    monkeypatch.setattr(
        "aios.factors.composite.compute_composite", fake_compute_composite
    )

    rows = cli._qvml_selecting_composite_computer(
        ["AAA", "BBB"], "2026-08-07", store=object(), include_market_factors=False
    )

    # The caller's include_market_factors=False is deliberately ignored: QVML
    # needs the momentum/low-vol sleeves computed, or every ticker is withheld.
    assert captured_kwargs["include_market_factors"] is True
    by_ticker = {row.ticker: row for row in rows}
    assert by_ticker["AAA"].qv_score == 20.0
    assert by_ticker["AAA"].qv_rank == 3
    assert by_ticker["BBB"].qv_score == 95.0
    assert by_ticker["BBB"].qv_rank == 1


def test_factor_model_override_note_is_write_once(tmp_path) -> None:
    proposal_path = tmp_path / "us-qv-2026-08-07.json"
    proposal_path.write_text("{}")

    note_path = cli._write_factor_model_override_note(
        proposal_path, factor_model="qvml"
    )

    assert note_path.name == "us-qv-2026-08-07.json.factor_model_override.json"
    payload = json.loads(note_path.read_text())
    assert payload["actual_factor_model"] == "qvml"
    assert payload["declared_strategy_field"] == "qv"
    assert "frozen in paper.py" in payload["reason"]

    with pytest.raises(FileExistsError):
        cli._write_factor_model_override_note(proposal_path, factor_model="qvml")


def test_paper_propose_rejects_an_unsupported_factor_model() -> None:
    from typer.testing import CliRunner

    result = CliRunner().invoke(
        cli.app, ["paper-propose", "--factor-model", "not-a-real-model"]
    )
    assert result.exit_code == 1
    assert "unsupported factor model" in result.output.lower()


def _invoke_paper_propose(tmp_path, monkeypatch, *, extra_args: list[str]) -> tuple:
    """Drive `paper-propose` with `create_paper_proposal` mocked out.

    `--trial` deliberately points at a path that does not exist, so the
    forward-trial registration branch (`if trial_path.exists():`) is skipped
    entirely — this test is only about the `composite_computer` selection and
    the override-note wiring, not the registration lifecycle, which is
    already covered in `tests/test_cli_maintenance_lock.py`.
    """
    from contextlib import nullcontext
    from types import SimpleNamespace

    from aios import paper as paper_module

    settings_stub = SimpleNamespace(
        project_root=tmp_path,
        duckdb_path=tmp_path / "data" / "aios.duckdb",
        operations_db_path=tmp_path / "data" / "operations" / "alerts.sqlite3",
    )
    monkeypatch.setattr(cli, "settings", settings_stub)
    monkeypatch.setattr(cli, "store_scope", lambda **_k: nullcontext(object()))

    captured: dict = {}

    class _Doc:
        path = tmp_path / "data" / "paper" / "proposals" / "us-qv-2026-08-07.json"
        payload = {
            "decision_date": "2026-08-07",
            "status": "blocked_readiness",
            "scheduled_simulation_date": "2026-08-10",
            "factor_eligible_count": 0,
            "targets": [],
            "notice": "",
        }

    def fake_create_paper_proposal(*args, **kwargs):
        captured["composite_computer"] = kwargs.get("composite_computer")
        _Doc.path.parent.mkdir(parents=True, exist_ok=True)
        _Doc.path.write_text("{}")
        return _Doc()

    monkeypatch.setattr(paper_module, "create_paper_proposal", fake_create_paper_proposal)

    notes_written: list[Path] = []
    monkeypatch.setattr(
        cli,
        "_write_factor_model_override_note",
        lambda path, *, factor_model: notes_written.append(path) or path,
    )

    from typer.testing import CliRunner

    result = CliRunner().invoke(
        cli.app,
        [
            "paper-propose",
            "--as-of",
            "2026-08-07",
            "--trial",
            str(tmp_path / "data" / "paper" / "does_not_exist_trial.json"),
            *extra_args,
        ],
    )
    return result, captured, notes_written


def test_paper_propose_passes_the_qvml_computer_and_writes_the_note(
    tmp_path, monkeypatch
) -> None:
    result, captured, notes_written = _invoke_paper_propose(
        tmp_path, monkeypatch, extra_args=["--factor-model", "qvml"]
    )

    assert result.exit_code == 0, result.output
    assert captured["composite_computer"] is cli._qvml_selecting_composite_computer
    assert len(notes_written) == 1
    assert "not the certified QV baseline" in result.output


def test_paper_propose_default_factor_model_uses_the_real_composite(
    tmp_path, monkeypatch
) -> None:
    result, captured, notes_written = _invoke_paper_propose(
        tmp_path, monkeypatch, extra_args=[]
    )

    assert result.exit_code == 0, result.output
    from aios.factors.composite import compute_composite

    assert captured["composite_computer"] is compute_composite
    assert notes_written == []
    assert "not the certified QV baseline" not in result.output

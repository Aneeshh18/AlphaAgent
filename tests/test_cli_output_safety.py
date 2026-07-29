from __future__ import annotations

import os
from contextlib import nullcontext
from datetime import date
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from aios import cli
from aios import paper as paper_module
from aios.artifacts import publish_text_write_once


def _settings(tmp_path):
    return SimpleNamespace(
        project_root=tmp_path,
        duckdb_path=tmp_path / "data" / "aios.duckdb",
        operations_db_path=tmp_path / "data" / "operations" / "alerts.sqlite3",
    )


@pytest.mark.parametrize(
    "relative",
    (
        "README.md",
        "data/aios.duckdb",
        "data/operations/alerts.sqlite3",
        "data/operations/maintenance.lock",
        "data/raw/provider/payload.json",
        "data/paper/us_qv_sandbox.json",
        "backups/aios-existing/manifest.json",
    ),
)
def test_generated_outputs_refuse_code_and_governed_state(
    tmp_path,
    monkeypatch,
    relative,
) -> None:
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))

    with pytest.raises(ValueError):
        cli._resolve_generated_output_path(
            tmp_path / relative,
            label="test artifact",
        )


def test_generated_output_is_write_once_and_preserves_existing_file(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))
    output = tmp_path / "data" / "reports" / "existing.json"
    output.parent.mkdir(parents=True)
    output.write_text('{"original":true}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="already exists"):
        cli._resolve_generated_output_path(
            output,
            label="test artifact",
            suffix=".json",
        )

    assert output.read_text(encoding="utf-8") == '{"original":true}\n'


def test_generated_output_refuses_symlink_ancestor(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))
    real_directory = tmp_path / "outside"
    real_directory.mkdir()
    link = tmp_path / "data" / "reports"
    link.parent.mkdir(parents=True)
    link.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        cli._resolve_generated_output_path(
            link / "report.json",
            label="test artifact",
            suffix=".json",
        )


def test_mutable_artifact_directory_refuses_hard_linked_files(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))
    governed = tmp_path / "data" / "paper" / "us_qv_sandbox.json"
    governed.parent.mkdir(parents=True)
    governed.write_text("governed", encoding="utf-8")
    artifact_directory = tmp_path / "data" / "factor_price_warmup"
    artifact_directory.mkdir(parents=True)
    os.link(governed, artifact_directory / "aliased.json")

    with pytest.raises(ValueError, match="hard-linked"):
        cli._resolve_generated_output_directory(
            artifact_directory,
            label="factor workspace",
        )

    assert governed.read_text(encoding="utf-8") == "governed"


def test_atomic_publisher_never_replaces_an_existing_artifact(tmp_path) -> None:
    output = tmp_path / "artifact.json"
    publish_text_write_once(output, '{"version":1}\n')

    with pytest.raises(FileExistsError):
        publish_text_write_once(output, '{"version":2}\n')

    assert output.read_text(encoding="utf-8") == '{"version":1}\n'


def test_paper_propose_cannot_target_the_account(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))
    monkeypatch.setattr(cli, "store_scope", lambda **kwargs: nullcontext(object()))
    monkeypatch.setattr(
        paper_module,
        "latest_paper_decision_date",
        lambda store: date(2026, 7, 28),
    )
    monkeypatch.setattr(
        paper_module,
        "create_paper_proposal",
        lambda *args, **kwargs: pytest.fail("unsafe destination must fail before creation"),
    )
    account = tmp_path / "data" / "paper" / "us_qv_sandbox.json"
    account.parent.mkdir(parents=True)
    account.write_text('{"account":"unchanged"}\n', encoding="utf-8")

    result = CliRunner().invoke(
        cli.app,
        [
            "paper-propose",
            "--account",
            str(account),
            "--output",
            str(account),
        ],
    )

    assert result.exit_code == 1
    assert "must stay under" in result.output
    assert "data/paper/proposals" in result.output
    assert account.read_text(encoding="utf-8") == '{"account":"unchanged"}\n'


def test_paper_propose_replace_validates_the_existing_decision_date(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))
    monkeypatch.setattr(cli, "store_scope", lambda **kwargs: nullcontext(object()))
    proposal = tmp_path / "data" / "paper" / "proposals" / "existing.json"
    proposal.parent.mkdir(parents=True)
    proposal.write_text('{"proposal":"unchanged"}\n', encoding="utf-8")
    account = tmp_path / "data" / "paper" / "us_qv_sandbox.json"

    def read_document(path, *, expected_kind):
        if expected_kind == paper_module.PROPOSAL_DOCUMENT_KIND:
            return SimpleNamespace(
                payload={
                    "account_id": "sandbox",
                    "decision_date": "2026-07-27",
                }
            )
        if expected_kind == paper_module.ACCOUNT_DOCUMENT_KIND:
            return SimpleNamespace(payload={"account_id": "sandbox"})
        raise AssertionError(f"unexpected document kind: {expected_kind}")

    monkeypatch.setattr(paper_module, "read_paper_document", read_document)
    monkeypatch.setattr(
        paper_module,
        "create_paper_proposal",
        lambda *args, **kwargs: pytest.fail("mismatched proposal must not be replaced"),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "paper-propose",
            "--as-of",
            "2026-07-28",
            "--account",
            str(account),
            "--output",
            str(proposal),
            "--replace",
        ],
    )

    assert result.exit_code == 1
    assert "different decision date" in result.output
    assert proposal.read_text(encoding="utf-8") == '{"proposal":"unchanged"}\n'


def test_refresh_rejects_unsafe_summary_before_mutating(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "settings", _settings(tmp_path))
    monkeypatch.setattr(
        "aios.refresh.refresh_us_current",
        lambda *args, **kwargs: pytest.fail("refresh must not start"),
    )
    account = tmp_path / "data" / "paper" / "us_qv_sandbox.json"
    account.parent.mkdir(parents=True)
    account.write_text('{"account":"unchanged"}\n', encoding="utf-8")

    result = CliRunner().invoke(
        cli.app,
        ["refresh-us-current", "--json-output", str(account)],
    )

    assert result.exit_code == 1
    assert "cannot target governed" in result.output
    assert "AIOS state" in result.output
    assert account.read_text(encoding="utf-8") == '{"account":"unchanged"}\n'

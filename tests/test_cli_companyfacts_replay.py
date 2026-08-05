from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY

import pytest
from typer.testing import CliRunner

from aios import cli


class _Preview:
    plan_sha256 = "a" * 64
    plan = {
        "summary": {
            "eligible_issuers": 1,
            "ineligible_issuers": 2,
            "excluded_source_observations": 3,
        }
    }

    def to_plan_envelope(self) -> dict:
        return {
            "document_kind": "aios.companyfacts-replay-plan",
            "schema_version": "companyfacts-replay-plan.v1",
            "read_only": True,
            "payload_sha256": self.plan_sha256,
            "payload": self.plan,
        }


def test_companyfacts_v3_plan_is_read_only_and_machine_readable(
    monkeypatch,
    tmp_path,
) -> None:
    store = object()
    scopes: list[dict] = []
    captured: dict = {}

    def store_scope(**kwargs):
        scopes.append(kwargs)
        return nullcontext(store)

    def preview(project_root, **kwargs):
        captured["project_root"] = project_root
        captured.update(kwargs)
        return _Preview()

    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(cli, "store_scope", store_scope)
    monkeypatch.setattr(
        "aios.companyfacts_replay.preview_companyfacts_v3_replay",
        preview,
    )
    monkeypatch.setattr(
        "aios.companyfacts_replay.persist_companyfacts_v3_plan",
        lambda *_args, **_kwargs: pytest.fail("preview must not publish"),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "companyfacts-v3-plan",
            "--as-of",
            "2026-07-29",
            "--issuer-id",
            "issuer-b",
            "--issuer-id",
            "issuer-a",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "preview"
    assert payload["publication"] == {"written": False, "path": None}
    assert payload["safety"]["activation"] is False
    assert captured == {
        "project_root": tmp_path,
        "store": store,
        "as_of": date(2026, 7, 29),
        "issuer_ids": ["issuer-b", "issuer-a"],
    }
    assert scopes == [{"read_only": True}]


def test_companyfacts_v3_plan_publishes_only_on_explicit_request(
    monkeypatch,
    tmp_path,
) -> None:
    destination = (
        tmp_path / "data" / "reports" / "companyfacts_replays" / "plans" / f"{'a' * 64}.json"
    )
    publications: list[tuple[Path, object]] = []
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(
        cli,
        "store_scope",
        lambda **_kwargs: nullcontext(object()),
    )
    monkeypatch.setattr(
        "aios.companyfacts_replay.preview_companyfacts_v3_replay",
        lambda *_args, **_kwargs: _Preview(),
    )

    def persist(project_root, preview):
        publications.append((project_root, preview))
        return destination

    monkeypatch.setattr(
        "aios.companyfacts_replay.persist_companyfacts_v3_plan",
        persist,
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "companyfacts-v3-plan",
            "--as-of",
            "2026-07-29",
            "--write-plan",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "published"
    assert payload["publication"] == {
        "written": True,
        "path": f"data/reports/companyfacts_replays/plans/{'a' * 64}.json",
    }
    assert publications == [(tmp_path, ANY)]


def test_companyfacts_v3_plan_fails_closed_without_activation(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(
        cli,
        "store_scope",
        lambda **_kwargs: nullcontext(object()),
    )
    monkeypatch.setattr(
        "aios.companyfacts_replay.preview_companyfacts_v3_replay",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("source proof mismatch")),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "companyfacts-v3-plan",
            "--as-of",
            "2026-07-29",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "withheld"
    assert payload["error"] == "source proof mismatch"
    assert payload["safety"]["activation"] is False

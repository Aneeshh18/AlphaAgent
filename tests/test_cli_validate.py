from __future__ import annotations

from contextlib import nullcontext

import pytest
from typer.testing import CliRunner

from aios import cli


class _FakeStore:
    def __init__(self, report: list[dict], samples: list[dict] | None = None) -> None:
        self.report = report
        self.samples = samples or []
        self.queries: list[tuple[str, tuple]] = []

    def data_quality_report(self) -> list[dict]:
        return self.report

    def query(self, sql: str, params: tuple) -> list[dict]:
        self.queries.append((sql, params))
        return self.samples


def test_validate_uses_a_scoped_read_only_database(monkeypatch) -> None:
    store = _FakeStore(
        [
            {
                "check": "prices_missing_close",
                "status": "ok",
                "count": 0,
                "detail": "Every price row has a close.",
            }
        ]
    )
    scopes = []

    def scoped_store(**kwargs):
        scopes.append(kwargs)
        return nullcontext(store)

    monkeypatch.setattr(cli, "store_scope", scoped_store)
    monkeypatch.setattr(
        cli,
        "get_store",
        lambda: pytest.fail("validate must not use the process-global writable store"),
    )

    result = CliRunner().invoke(cli.app, ["validate"])

    assert result.exit_code == 0
    assert scopes == [{"read_only": True}]
    assert store.queries == []


def test_validate_shows_a_bounded_deterministic_missing_close_sample(monkeypatch) -> None:
    store = _FakeStore(
        [
            {
                "check": "prices_missing_close",
                "status": "fail",
                "count": 8,
                "detail": "A price row without close cannot support valuation or returns.",
            }
        ],
        samples=[
            {"ticker": "IT", "date": "2026-07-27", "source": "yfinance"},
            {"ticker": "JBHT", "date": "2026-07-27", "source": "yfinance"},
            {"ticker": "MTD", "date": "2026-07-27", "source": "yfinance"},
            {"ticker": "WM", "date": "2026-07-27", "source": "yfinance"},
            {"ticker": "ABC", "date": "2026-07-26", "source": "unknown"},
        ],
    )
    monkeypatch.setattr(
        cli,
        "store_scope",
        lambda **kwargs: nullcontext(store),
    )

    result = CliRunner().invoke(cli.app, ["validate"])

    assert result.exit_code == 1
    assert len(store.queries) == 1
    sql, params = store.queries[0]
    assert "WHERE close IS NULL" in sql
    assert "NOT isfinite(close)" in sql
    assert "close <= 0" in sql
    assert "ORDER BY date DESC, ticker, source" in sql
    assert params == (5,)
    assert "Affected rows for prices_missing_close (showing 5 of 8)" in result.output
    assert "WM" in result.output
    assert "2026-07-27" in result.output
    assert "yfinance" in result.output
    assert "unknown" in result.output

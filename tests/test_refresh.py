from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

import aios.refresh as refresh_module
from aios import cli
from aios.refresh import RefreshFailure, USRefreshResult, refresh_us_current


class FakeStore:
    def __init__(self, members: list[dict]) -> None:
        self.members = members
        self.issuers_with_fundamentals: set[str] = set()

    def universe_membership_on(self, universe_id: str, as_of: date) -> list[dict]:
        assert universe_id == "sp500"
        assert as_of == date(2026, 7, 21)
        return self.members

    def issuer_id_for_security(self, security_id: str, as_of: date) -> str | None:
        assert as_of == date(2026, 7, 21)
        return {"sec-a": "issuer-a", "sec-b": "issuer-b"}.get(security_id)

    def provider_symbol_mappings(self, security_id: str, **kwargs) -> list[dict]:
        assert kwargs == {
            "start": date(2026, 7, 21),
            "end": date(2026, 7, 22),
        }
        return [{"security_id": security_id, "provider": "yfinance"}]

    def issuer_has_fundamentals(self, issuer_id: str) -> bool:
        return issuer_id in self.issuers_with_fundamentals


def test_current_us_refresh_runs_reviewed_identities_sequentially() -> None:
    store = FakeStore(
        [
            {"ticker": "AAA", "security_id": "sec-a"},
            {"ticker": "BBB", "security_id": "sec-b"},
        ]
    )
    issuers: list[str] = []
    securities: list[str] = []
    benchmarks: list[str] = []
    sleeps: list[float] = []
    progress: list[tuple[str, str, int, int]] = []

    result = refresh_us_current(
        "2026-07-21",
        today=date(2026, 7, 21),
        store=store,  # type: ignore[arg-type]
        minimum_members=1,
        maximum_members=10,
        macro_ingester=lambda: 3,
        issuer_ingester=lambda value: issuers.append(value) or 5,
        security_price_ingester=lambda value: securities.append(value) or 7,
        benchmark_ingester=lambda value: benchmarks.append(value) or 4,
        sleeper=sleeps.append,
        progress=lambda *args: progress.append(args),
    )

    assert result.ok is True
    assert result.members == 2
    assert result.issuers_attempted == 2
    assert result.securities_attempted == 2
    assert result.macro_rows == 3
    assert result.fundamental_rows == 10
    assert result.price_rows == 14
    assert result.benchmark_rows == 4
    assert issuers == ["issuer-a", "issuer-b"]
    assert securities == ["sec-a", "sec-b"]
    assert benchmarks == ["SPY"]
    assert len(sleeps) == 1
    assert progress[0] == ("macro", "release-aware series", 1, 1)
    assert result.to_dict()["ok"] is True


def test_current_us_refresh_refuses_missing_security_identity_before_network() -> None:
    store = FakeStore([{"ticker": "AAA", "security_id": None}])

    with pytest.raises(ValueError, match="without reviewed security IDs: AAA"):
        refresh_us_current(
            "2026-07-21",
            today=date(2026, 7, 21),
            store=store,  # type: ignore[arg-type]
            minimum_members=1,
            maximum_members=10,
            macro_ingester=lambda: 1,
        )


def test_current_us_refresh_retains_each_price_failure_and_continues() -> None:
    store = FakeStore(
        [
            {"ticker": "AAA", "security_id": "sec-a"},
            {"ticker": "BBB", "security_id": "sec-b"},
        ]
    )

    def ingest_security(security_id: str) -> int:
        if security_id == "sec-b":
            raise RuntimeError("provider throttled")
        return 5

    result = refresh_us_current(
        "2026-07-21",
        today=date(2026, 7, 21),
        store=store,  # type: ignore[arg-type]
        minimum_members=1,
        maximum_members=10,
        include_fundamentals=False,
        include_macro=False,
        security_price_ingester=ingest_security,
        benchmark_ingester=lambda _ticker: (_ for _ in ()).throw(
            RuntimeError("benchmark unavailable")
        ),
        sleeper=lambda _seconds: None,
    )

    assert result.ok is False
    assert result.price_rows == 5
    assert [(failure.kind, failure.identity) for failure in result.failures] == [
        ("prices", "sec-b"),
        ("benchmark", "SPY"),
    ]


def test_fundamental_transport_circuit_stops_systemic_provider_failure() -> None:
    members = [
        {"ticker": f"T{index}", "security_id": f"sec-{index}"}
        for index in range(1, 6)
    ]

    class CircuitStore(FakeStore):
        def issuer_id_for_security(self, security_id: str, as_of: date) -> str:
            assert as_of == date(2026, 7, 21)
            return security_id.replace("sec-", "issuer-")

    attempted: list[str] = []

    def fail_transport(issuer_id: str) -> int:
        attempted.append(issuer_id)
        raise httpx.DecodingError(
            "incorrect header check",
            request=httpx.Request("GET", "https://data.sec.gov/test"),
        )

    result = refresh_us_current(
        "2026-07-21",
        today=date(2026, 7, 21),
        store=CircuitStore(members),  # type: ignore[arg-type]
        minimum_members=1,
        maximum_members=10,
        include_prices=False,
        include_macro=False,
        issuer_ingester=fail_transport,
    )

    assert attempted == ["issuer-1", "issuer-2", "issuer-3"]
    assert result.issuers_attempted == 3
    assert [(failure.kind, failure.identity) for failure in result.failures] == [
        ("fundamentals", "issuer-1"),
        ("fundamentals", "issuer-2"),
        ("fundamentals", "issuer-3"),
        ("fundamentals_transport_circuit", "2 issuer(s) not attempted"),
    ]


def test_price_transport_circuit_does_not_attempt_remaining_securities() -> None:
    members = [
        {"ticker": f"T{index}", "security_id": f"sec-{index}"}
        for index in range(1, 6)
    ]
    attempted: list[str] = []

    def fail_transport(security_id: str) -> int:
        attempted.append(security_id)
        raise httpx.ConnectError(
            "provider unavailable",
            request=httpx.Request("GET", "https://query1.finance.yahoo.com/test"),
        )

    result = refresh_us_current(
        "2026-07-21",
        today=date(2026, 7, 21),
        store=FakeStore(members),  # type: ignore[arg-type]
        minimum_members=1,
        maximum_members=10,
        include_benchmark=False,
        include_fundamentals=False,
        include_macro=False,
        security_price_ingester=fail_transport,
        sleeper=lambda _seconds: None,
    )

    assert attempted == ["sec-1", "sec-2", "sec-3"]
    assert result.securities_attempted == 3
    assert [(failure.kind, failure.identity) for failure in result.failures] == [
        ("prices", "sec-1"),
        ("prices", "sec-2"),
        ("prices", "sec-3"),
        ("prices_transport_circuit", "2 security(s) not attempted"),
    ]


def test_current_us_refresh_can_skip_a_benchmark_already_bootstrapped_by_daily_cycle() -> None:
    store = FakeStore([{"ticker": "AAA", "security_id": "sec-a"}])
    benchmarks: list[str] = []

    result = refresh_us_current(
        "2026-07-21",
        today=date(2026, 7, 21),
        store=store,  # type: ignore[arg-type]
        minimum_members=1,
        maximum_members=10,
        include_benchmark=False,
        include_fundamentals=False,
        include_macro=False,
        security_price_ingester=lambda _security: 5,
        benchmark_ingester=lambda ticker: benchmarks.append(ticker) or 5,
    )

    assert result.ok is True
    assert result.price_rows == 5
    assert result.benchmark_rows == 0
    assert benchmarks == []


def test_current_us_refresh_warns_for_reviewed_pre_filing_issuer() -> None:
    store = FakeStore([{"ticker": "AAA", "security_id": "sec-a"}])

    result = refresh_us_current(
        "2026-07-21",
        today=date(2026, 7, 21),
        store=store,  # type: ignore[arg-type]
        minimum_members=1,
        maximum_members=10,
        include_prices=False,
        include_macro=False,
        issuer_ingester=lambda _issuer: 0,
    )

    assert result.ok is True
    assert result.failures == ()
    assert [(warning.kind, warning.identity) for warning in result.warnings] == [
        ("fundamentals_pending", "issuer-a")
    ]


def test_current_us_refresh_fails_if_established_issuer_returns_no_rows() -> None:
    store = FakeStore([{"ticker": "AAA", "security_id": "sec-a"}])
    store.issuers_with_fundamentals.add("issuer-a")

    result = refresh_us_current(
        "2026-07-21",
        today=date(2026, 7, 21),
        store=store,  # type: ignore[arg-type]
        minimum_members=1,
        maximum_members=10,
        include_prices=False,
        include_macro=False,
        issuer_ingester=lambda _issuer: 0,
    )

    assert result.ok is False
    assert result.warnings == ()
    assert [(failure.kind, failure.identity) for failure in result.failures] == [
        ("fundamentals", "issuer-a")
    ]


def test_current_us_refresh_refuses_a_stale_membership_date() -> None:
    with pytest.raises(ValueError, match="too old for current refresh"):
        refresh_us_current(
            "2026-07-13",
            today=date(2026, 7, 21),
            store=FakeStore([]),  # type: ignore[arg-type]
            minimum_members=1,
            maximum_members=10,
            macro_ingester=lambda: 1,
        )


def test_current_us_refresh_uses_latest_recent_reviewed_membership() -> None:
    class SnapshotStore(FakeStore):
        def universe_membership_on(self, universe_id: str, as_of: date) -> list[dict]:
            assert universe_id == "sp500"
            if as_of == date(2026, 7, 21):
                return self.members
            return []

        def issuer_id_for_security(self, security_id: str, as_of: date) -> str | None:
            assert as_of == date(2026, 7, 21)
            return super().issuer_id_for_security(security_id, as_of)

    store = SnapshotStore([{"ticker": "AAA", "security_id": "sec-a"}])
    result = refresh_us_current(
        today=date(2026, 7, 22),
        store=store,  # type: ignore[arg-type]
        minimum_members=1,
        maximum_members=10,
        include_prices=False,
        include_macro=False,
        issuer_ingester=lambda _issuer: 5,
    )

    assert result.as_of == "2026-07-22"
    assert result.membership_as_of == "2026-07-21"
    assert result.ok is True


def test_current_us_refresh_cli_keeps_membership_review_boundary(
    monkeypatch,
    tmp_path,
) -> None:
    captured: dict[str, object] = {}
    recovered: list[str] = []
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))

    def fake_refresh(as_of, **kwargs) -> USRefreshResult:
        captured.update(as_of=as_of, **kwargs)
        return USRefreshResult(
            as_of="2026-07-21",
            universe_id="sp500",
            members=503,
            issuers_attempted=500,
            securities_attempted=503,
            macro_rows=10,
            fundamental_rows=100,
            price_rows=1_000,
            benchmark_rows=2,
            failures=(),
        )

    monkeypatch.setattr(refresh_module, "refresh_us_current", fake_refresh)
    monkeypatch.setattr(cli, "_resolve_operational_alert", recovered.append)
    result = CliRunner().invoke(
        cli.app,
        ["refresh-us-current", "--as-of", "2026-07-21", "--no-fundamentals"],
    )

    assert result.exit_code == 0
    assert captured["as_of"] == "2026-07-21"
    assert captured["include_fundamentals"] is False
    assert "already reviewed identities" in result.output
    assert "Run `aios health`" in result.output
    assert recovered == []


def test_current_us_refresh_cli_records_zero_exit_degradation(
    monkeypatch,
    tmp_path,
) -> None:
    emitted = []
    recovered: list[str] = []
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))

    def fake_refresh(_as_of, **_kwargs) -> USRefreshResult:
        return USRefreshResult(
            as_of="2026-07-21",
            universe_id="sp500",
            members=503,
            issuers_attempted=0,
            securities_attempted=503,
            macro_rows=10,
            fundamental_rows=0,
            price_rows=1_000,
            benchmark_rows=2,
            failures=(),
            warnings=(RefreshFailure("price", "ABC", "provider delay"),),
        )

    monkeypatch.setattr(refresh_module, "refresh_us_current", fake_refresh)
    monkeypatch.setattr(cli, "_emit_operational_alert", emitted.append)
    monkeypatch.setattr(cli, "_resolve_operational_alert", recovered.append)

    result = CliRunner().invoke(
        cli.app,
        ["refresh-us-current", "--as-of", "2026-07-21", "--no-fundamentals"],
    )

    assert result.exit_code == 0
    assert [alert.code for alert in emitted] == ["current_refresh_partial"]
    assert emitted[0].payload["identities"] == ["ABC"]
    assert recovered == []

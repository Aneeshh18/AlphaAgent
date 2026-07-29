from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from aios import cli
from aios import forward as forward_module
from aios import paper as paper_module
from aios.market_calendar import us_equity_sessions
from aios.paper import (
    _paper_account_write_lock,
    canonical_payload_sha256,
    create_paper_proposal,
    execute_paper_proposal,
    initialize_paper_account,
    latest_paper_decision_date,
    latest_reviewed_market_close,
    mark_paper_account,
    paper_account_summary,
    paper_proposal_timing_status,
    read_paper_document,
    review_paper_proposal_execution,
)
from aios.readiness import USReadinessPolicy
from aios.storage.store import Store


class _ReadyReport:
    ready = True
    blockers = ()

    def to_dict(self) -> dict:
        return {"ready": True, "as_of": "2026-07-20", "checks": []}


def _ready(*args, **kwargs) -> _ReadyReport:
    return _ReadyReport()


def _scores(tickers, as_of, store, *, include_market_factors):
    assert include_market_factors is False
    return [
        SimpleNamespace(
            ticker=ticker,
            qv_score=100.0 - index,
            qv_rank=index,
            quality_score=80.0 - index,
            value_score=70.0 - index,
            quality_components_available=3,
            value_multiples_available=4,
            macro_regime="reflation",
            quality_weight=0.45,
            value_weight=0.55,
            regime_pit_ready=True,
        )
        for index, ticker in enumerate(sorted(tickers), 1)
    ]


def _seed_paper_store(store: Store) -> None:
    tickers = [f"T{index:02d}" for index in range(10)]
    sic_codes = (2000, 4000, 5000, 5200, 6000, 7000, 2100, 4100, 5100, 5300)
    store.upsert_securities(
        [
            {
                "ticker": ticker,
                "cik": index + 1,
                "name": f"Test {ticker}",
                "exchange": "NYSE",
                "sector": "test description",
                "industry": "test",
                "market_cap_bucket": "large",
                "sic_code": str(sic_codes[index]),
            }
            for index, ticker in enumerate(tickers)
        ]
    )
    store.upsert_universe_membership(
        [
            {
                "universe_id": "sp500",
                "ticker": ticker,
                "security_id": f"SEC-{ticker}",
                "effective_start": "2020-01-01",
                "known_date": "2019-12-01",
                "source": "test",
            }
            for ticker in tickers
        ]
    )
    store.upsert_security_identities(
        [
            {
                "universe_id": "sp500",
                "ticker": ticker,
                "security_id": f"SEC-{ticker}",
                "effective_start": "2020-01-01",
                "known_date": "2019-12-01",
                "identity_status": "bounded_ticker",
                "source": "test",
            }
            for ticker in tickers
        ]
    )
    sessions = us_equity_sessions(date(2026, 6, 15), date(2026, 7, 23))
    price_rows = []
    for ticker in tickers:
        for offset, session in enumerate(sessions):
            price_rows.append(
                {
                    "ticker": ticker,
                    "security_id": f"SEC-{ticker}",
                    "provider_symbol": ticker,
                    "date": session.isoformat(),
                    "close": 100.0 + offset,
                    "adj_close": 100.0 + offset,
                    "volume": 1_000_000,
                    "dividends": 0.0,
                    "split_ratio": 1.0,
                    "actions_complete": True,
                    "close_split_adjusted": False,
                    "source": "test",
                }
            )
        price_rows.extend(
            {
                "ticker": "SPY",
                "date": session.isoformat(),
                "close": 500.0 + offset,
                "adj_close": 500.0 + offset,
                "volume": 2_000_000,
                "dividends": 0.0,
                "split_ratio": 1.0,
                "actions_complete": True,
                "close_split_adjusted": False,
                "source": "test",
            }
            for offset, session in enumerate(sessions)
        )
    store.upsert_prices(price_rows)


def test_paper_document_checksum_detects_manual_payload_edit(tmp_path) -> None:
    store = Store(tmp_path / "paper-checksum.duckdb")
    try:
        path = tmp_path / "account.json"
        initialize_paper_account(path, store)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["payload"]["portfolio"]["cash"] = 999_999.0
        path.write_text(json.dumps(raw), encoding="utf-8")

        with pytest.raises(ValueError, match="checksum mismatch"):
            read_paper_document(path)
    finally:
        store.close()


def test_decision_date_stops_at_universe_boundary_but_valuation_can_advance(tmp_path) -> None:
    store = Store(tmp_path / "paper-clocks.duckdb")
    try:
        policy = USReadinessPolicy(
            minimum_universe_members=2,
            maximum_universe_members=3,
        )
        store.upsert_universe_membership(
            [
                {
                    "universe_id": "sp500",
                    "ticker": ticker,
                    "effective_start": "2026-01-01",
                    "effective_end": "2026-07-22",
                    "known_date": "2025-12-20",
                    "end_known_date": "2026-07-21",
                    "source": "test:certified-through-2026-07-21",
                }
                for ticker in ("A", "B")
            ]
        )
        store.upsert_prices(
            [
                {
                    "ticker": "SPY",
                    "date": observed.isoformat(),
                    "close": 500.0,
                    "source": "test",
                }
                for observed in (date(2026, 7, 21), date(2026, 7, 22))
            ]
        )

        assert latest_reviewed_market_close(store, today=date(2026, 7, 23)) == date(
            2026, 7, 22
        )
        assert latest_paper_decision_date(
            store,
            today=date(2026, 7, 23),
            policy=policy,
        ) == date(2026, 7, 21)
    finally:
        store.close()


def test_paper_proposal_executes_once_then_marks_without_broker(tmp_path) -> None:
    store = Store(tmp_path / "paper-flow.duckdb")
    try:
        _seed_paper_store(store)
        account_path = tmp_path / "account.json"
        proposal_path = tmp_path / "proposal.json"
        initialize_paper_account(
            account_path,
            store,
            initial_capital=10_000.0,
            now=datetime(2026, 7, 20, tzinfo=UTC),
        )

        proposal = create_paper_proposal(
            account_path,
            proposal_path,
            date(2026, 7, 20),
            store,
            now=datetime(2026, 7, 20, 22, tzinfo=UTC),
            readiness_assessor=_ready,
            composite_computer=_scores,
        )

        assert proposal.payload["status"] == "approved_for_supervised_simulation"
        assert proposal.payload["scheduled_simulation_date"] == "2026-07-21"
        assert len(proposal.payload["targets"]) == 10
        assert proposal.payload["risk_assessment"]["approved"] is True

        checksum_before_review = read_paper_document(account_path).payload_sha256
        review = review_paper_proposal_execution(
            account_path,
            proposal_path,
            store,
            now=datetime(2026, 7, 21, 20, 1, tzinfo=UTC),
            readiness_assessor=_ready,
            composite_computer=_scores,
        )
        assert review["status"] == "ready_for_confirmed_simulation"
        assert review["ready"] is True
        assert review["projected_trade_count"] == 10
        assert read_paper_document(account_path).payload_sha256 == checksum_before_review

        result = execute_paper_proposal(
            account_path,
            proposal_path,
            store,
            confirm_simulated=True,
            now=datetime(2026, 7, 21, 22, tzinfo=UTC),
            readiness_assessor=_ready,
            composite_computer=_scores,
        )
        assert result["execution"]["execution_date"] == "2026-07-21"
        assert len(result["execution"]["trades"]) == 10

        summary = paper_account_summary(account_path, store)
        assert summary["mode"] == "simulation_only"
        assert summary["broker_connected"] is False
        assert summary["execution_count"] == 1
        assert len(summary["holdings"]) == 10

        with pytest.raises(ValueError, match="account changed after proposal"):
            execute_paper_proposal(
                account_path,
                proposal_path,
                store,
                confirm_simulated=True,
                readiness_assessor=_ready,
                composite_computer=_scores,
            )

        marked = mark_paper_account(
            account_path,
            date(2026, 7, 22),
            store,
            now=datetime(2026, 7, 22, 22, tzinfo=UTC),
        )
        assert [point["date"] for point in marked["points"]] == ["2026-07-22"]
        assert paper_account_summary(account_path, store)["last_market_date"] == "2026-07-22"
    finally:
        store.close()


def test_paper_proposal_must_be_created_before_scheduled_session_opens(tmp_path) -> None:
    store = Store(tmp_path / "paper-prospective.duckdb")
    try:
        _seed_paper_store(store)
        account_path = tmp_path / "account.json"
        proposal_path = tmp_path / "proposal.json"
        initialize_paper_account(account_path, store)

        with pytest.raises(ValueError, match="no longer prospective"):
            create_paper_proposal(
                account_path,
                proposal_path,
                date(2026, 7, 20),
                store,
                now=datetime(2026, 7, 21, 13, 30, tzinfo=UTC),
                readiness_assessor=_ready,
                composite_computer=_scores,
            )
        assert not proposal_path.exists()
    finally:
        store.close()


@pytest.mark.parametrize(
    "moments",
    [
        (
            datetime(2026, 7, 21, 13, 29, tzinfo=UTC),
            datetime(2026, 7, 21, 13, 30, tzinfo=UTC),
        ),
        (
            datetime(2026, 7, 21, 13, 29, tzinfo=UTC),
            datetime(2026, 7, 21, 13, 29, 59, tzinfo=UTC),
            datetime(2026, 7, 21, 13, 30, tzinfo=UTC),
        ),
    ],
)
def test_paper_proposal_crossing_generation_deadline_is_never_persisted(
    tmp_path,
    moments,
) -> None:
    store = Store(tmp_path / "paper-generation-race.duckdb")
    try:
        _seed_paper_store(store)
        account_path = tmp_path / "account.json"
        proposal_path = tmp_path / "proposal.json"
        initialize_paper_account(account_path, store)
        clock_values = iter(moments)

        with pytest.raises(ValueError, match="no longer prospective"):
            create_paper_proposal(
                account_path,
                proposal_path,
                date(2026, 7, 20),
                store,
                clock=lambda: next(clock_values),
                readiness_assessor=_ready,
                composite_computer=_scores,
            )

        assert not proposal_path.exists()
        assert not list(tmp_path.glob(".proposal.json.*.tmp"))
    finally:
        store.close()


def test_paper_review_waits_for_close_without_changing_account(tmp_path) -> None:
    store = Store(tmp_path / "paper-before-close.duckdb")
    try:
        _seed_paper_store(store)
        account_path = tmp_path / "account.json"
        proposal_path = tmp_path / "proposal.json"
        initialize_paper_account(account_path, store)
        create_paper_proposal(
            account_path,
            proposal_path,
            date(2026, 7, 20),
            store,
            now=datetime(2026, 7, 20, 22, tzinfo=UTC),
            readiness_assessor=_ready,
            composite_computer=_scores,
        )
        checksum_before = read_paper_document(account_path).payload_sha256

        review = review_paper_proposal_execution(
            account_path,
            proposal_path,
            store,
            now=datetime(2026, 7, 21, 19, 59, tzinfo=UTC),
            readiness_assessor=_ready,
            composite_computer=_scores,
        )

        assert review["status"] == "waiting_for_scheduled_close"
        assert review["ready"] is False
        assert read_paper_document(account_path).payload_sha256 == checksum_before
        with pytest.raises(ValueError, match="has not reached"):
            execute_paper_proposal(
                account_path,
                proposal_path,
                store,
                confirm_simulated=True,
                now=datetime(2026, 7, 21, 19, 59, tzinfo=UTC),
                readiness_assessor=_ready,
                composite_computer=_scores,
            )
    finally:
        store.close()


def test_paper_review_crossing_expiry_returns_expired_without_mutation(tmp_path) -> None:
    store = Store(tmp_path / "paper-review-race.duckdb")
    try:
        _seed_paper_store(store)
        account_path = tmp_path / "account.json"
        proposal_path = tmp_path / "proposal.json"
        initialize_paper_account(account_path, store)
        create_paper_proposal(
            account_path,
            proposal_path,
            date(2026, 7, 20),
            store,
            now=datetime(2026, 7, 20, 22, tzinfo=UTC),
            readiness_assessor=_ready,
            composite_computer=_scores,
        )
        account_before = account_path.read_bytes()
        proposal_before = proposal_path.read_bytes()
        clock_values = iter(
            (
                datetime(2026, 7, 21, 20, 1, tzinfo=UTC),
                datetime(2026, 7, 22, 13, 30, tzinfo=UTC),
            )
        )

        review = review_paper_proposal_execution(
            account_path,
            proposal_path,
            store,
            clock=lambda: next(clock_values),
            readiness_assessor=_ready,
            composite_computer=_scores,
        )

        assert review["status"] == "expired"
        assert review["ready"] is False
        assert account_path.read_bytes() == account_before
        assert proposal_path.read_bytes() == proposal_before
    finally:
        store.close()


def test_paper_execution_expires_at_following_session_open(tmp_path) -> None:
    store = Store(tmp_path / "paper-expired.duckdb")
    try:
        _seed_paper_store(store)
        account_path = tmp_path / "account.json"
        proposal_path = tmp_path / "proposal.json"
        initialize_paper_account(account_path, store)
        create_paper_proposal(
            account_path,
            proposal_path,
            date(2026, 7, 20),
            store,
            now=datetime(2026, 7, 20, 22, tzinfo=UTC),
            readiness_assessor=_ready,
            composite_computer=_scores,
        )

        review = review_paper_proposal_execution(
            account_path,
            proposal_path,
            store,
            now=datetime(2026, 7, 22, 13, 30, tzinfo=UTC),
            readiness_assessor=_ready,
            composite_computer=_scores,
        )
        assert review["status"] == "expired"
        assert review["ready"] is False

        with pytest.raises(ValueError, match="window expired"):
            execute_paper_proposal(
                account_path,
                proposal_path,
                store,
                confirm_simulated=True,
                now=datetime(2026, 7, 22, 13, 30, tzinfo=UTC),
                readiness_assessor=_ready,
                composite_computer=_scores,
            )
        assert paper_account_summary(account_path, store)["execution_count"] == 0
    finally:
        store.close()


def test_paper_execution_crossing_expiry_cannot_replace_account(tmp_path) -> None:
    store = Store(tmp_path / "paper-execution-race.duckdb")
    try:
        _seed_paper_store(store)
        account_path = tmp_path / "account.json"
        proposal_path = tmp_path / "proposal.json"
        initialize_paper_account(account_path, store)
        create_paper_proposal(
            account_path,
            proposal_path,
            date(2026, 7, 20),
            store,
            now=datetime(2026, 7, 20, 22, tzinfo=UTC),
            readiness_assessor=_ready,
            composite_computer=_scores,
        )
        account_before = account_path.read_bytes()
        proposal_before = proposal_path.read_bytes()
        clock_values = iter(
            (
                datetime(2026, 7, 21, 20, 1, tzinfo=UTC),
                datetime(2026, 7, 22, 13, 29, tzinfo=UTC),
                datetime(2026, 7, 22, 13, 29, 30, tzinfo=UTC),
                datetime(2026, 7, 22, 13, 30, tzinfo=UTC),
            )
        )

        with pytest.raises(ValueError, match="window expired"):
            execute_paper_proposal(
                account_path,
                proposal_path,
                store,
                confirm_simulated=True,
                clock=lambda: next(clock_values),
                readiness_assessor=_ready,
                composite_computer=_scores,
            )

        assert account_path.read_bytes() == account_before
        assert proposal_path.read_bytes() == proposal_before
        assert paper_account_summary(account_path, store)["execution_count"] == 0
        assert not list(tmp_path.glob(".account.json.*.tmp"))
    finally:
        store.close()


def test_paper_timing_skips_holiday_and_weekend_with_new_york_dst() -> None:
    proposal = {
        "decision_date": "2026-11-25",
        "scheduled_simulation_date": "2026-11-27",
        "generated_at": "2026-11-25T22:00:00Z",
    }

    early_close_afternoon = paper_proposal_timing_status(
        proposal,
        now=datetime(2026, 11, 27, 19, tzinfo=UTC),
    )
    assert early_close_afternoon["status"] == "waiting_for_scheduled_close"
    assert early_close_afternoon["must_be_generated_before"] == "2026-11-27T14:30:00Z"
    assert early_close_afternoon["executable_after"] == "2026-11-27T21:00:00Z"
    assert early_close_afternoon["expires_at"] == "2026-11-30T14:30:00Z"

    conservative_close = paper_proposal_timing_status(
        proposal,
        now=datetime(2026, 11, 27, 21, tzinfo=UTC),
    )
    assert conservative_close["status"] == "execution_window_open"


@pytest.mark.parametrize(
    ("proposal", "expected_generation_deadline", "expected_close", "expected_expiry"),
    [
        (
            {
                "decision_date": "2026-03-06",
                "scheduled_simulation_date": "2026-03-09",
                "generated_at": "2026-03-06T22:00:00Z",
            },
            "2026-03-09T13:30:00Z",
            "2026-03-09T20:00:00Z",
            "2026-03-10T13:30:00Z",
        ),
        (
            {
                "decision_date": "2026-07-02",
                "scheduled_simulation_date": "2026-07-06",
                "generated_at": "2026-07-02T22:00:00Z",
            },
            "2026-07-06T13:30:00Z",
            "2026-07-06T20:00:00Z",
            "2026-07-07T13:30:00Z",
        ),
    ],
)
def test_paper_timing_handles_dst_and_full_market_holidays(
    proposal,
    expected_generation_deadline,
    expected_close,
    expected_expiry,
) -> None:
    status = paper_proposal_timing_status(
        proposal,
        now=datetime.fromisoformat(expected_close.replace("Z", "+00:00")),
    )

    assert status["status"] == "execution_window_open"
    assert status["must_be_generated_before"] == expected_generation_deadline
    assert status["executable_after"] == expected_close
    assert status["expires_at"] == expected_expiry


def test_paper_account_write_lock_refuses_a_concurrent_mutation(tmp_path) -> None:
    account_path = tmp_path / "account.json"

    with (
        _paper_account_write_lock(account_path),
        pytest.raises(ValueError, match="already in progress"),
        _paper_account_write_lock(account_path),
    ):
        pytest.fail("a second writer must never acquire the account lock")


def test_paper_execution_refuses_when_checks_cross_next_open(tmp_path) -> None:
    store = Store(tmp_path / "paper-execution-deadline.duckdb")
    try:
        _seed_paper_store(store)
        account_path = tmp_path / "account.json"
        proposal_path = tmp_path / "proposal.json"
        initialize_paper_account(account_path, store)
        create_paper_proposal(
            account_path,
            proposal_path,
            date(2026, 7, 20),
            store,
            now=datetime(2026, 7, 20, 22, tzinfo=UTC),
            readiness_assessor=_ready,
            composite_computer=_scores,
        )
        checksum_before = read_paper_document(account_path).payload_sha256
        moments = iter(
            (
                datetime(2026, 7, 22, 13, 29, 59, tzinfo=UTC),
                datetime(2026, 7, 22, 13, 30, tzinfo=UTC),
            )
        )

        with pytest.raises(ValueError, match="window expired"):
            execute_paper_proposal(
                account_path,
                proposal_path,
                store,
                confirm_simulated=True,
                clock=lambda: next(moments),
                readiness_assessor=_ready,
                composite_computer=_scores,
            )
        assert read_paper_document(account_path).payload_sha256 == checksum_before
    finally:
        store.close()


def test_paper_execution_detects_external_account_change_before_replace(tmp_path) -> None:
    store = Store(tmp_path / "paper-account-cas.duckdb")
    try:
        _seed_paper_store(store)
        account_path = tmp_path / "account.json"
        proposal_path = tmp_path / "proposal.json"
        initialize_paper_account(account_path, store)
        create_paper_proposal(
            account_path,
            proposal_path,
            date(2026, 7, 20),
            store,
            now=datetime(2026, 7, 20, 22, tzinfo=UTC),
            readiness_assessor=_ready,
            composite_computer=_scores,
        )

        def scores_with_external_change(*args, **kwargs):
            raw = json.loads(account_path.read_text(encoding="utf-8"))
            raw["payload"]["audit_events"].append(
                {
                    "event": "external_test_change",
                    "at": "2026-07-21T20:30:00Z",
                }
            )
            raw["payload_sha256"] = canonical_payload_sha256(raw["payload"])
            account_path.write_text(json.dumps(raw), encoding="utf-8")
            return _scores(*args, **kwargs)

        with pytest.raises(ValueError, match="changed while checks were running"):
            execute_paper_proposal(
                account_path,
                proposal_path,
                store,
                confirm_simulated=True,
                now=datetime(2026, 7, 21, 20, 30, tzinfo=UTC),
                readiness_assessor=_ready,
                composite_computer=scores_with_external_change,
            )
        assert paper_account_summary(account_path, store)["execution_count"] == 0
    finally:
        store.close()


def test_paper_review_detects_proposal_change_while_checks_run(tmp_path) -> None:
    store = Store(tmp_path / "paper-review-cas.duckdb")
    try:
        _seed_paper_store(store)
        account_path = tmp_path / "account.json"
        proposal_path = tmp_path / "proposal.json"
        initialize_paper_account(account_path, store)
        create_paper_proposal(
            account_path,
            proposal_path,
            date(2026, 7, 20),
            store,
            now=datetime(2026, 7, 20, 22, tzinfo=UTC),
            readiness_assessor=_ready,
            composite_computer=_scores,
        )
        account_before = account_path.read_bytes()

        def scores_with_external_change(*args, **kwargs):
            raw = json.loads(proposal_path.read_text(encoding="utf-8"))
            raw["payload"]["notice"] += " External test change."
            raw["payload_sha256"] = canonical_payload_sha256(raw["payload"])
            proposal_path.write_text(json.dumps(raw), encoding="utf-8")
            return _scores(*args, **kwargs)

        with pytest.raises(ValueError, match="proposal changed while checks were running"):
            review_paper_proposal_execution(
                account_path,
                proposal_path,
                store,
                now=datetime(2026, 7, 21, 20, 30, tzinfo=UTC),
                readiness_assessor=_ready,
                composite_computer=scores_with_external_change,
            )

        assert account_path.read_bytes() == account_before
        assert paper_account_summary(account_path, store)["execution_count"] == 0
    finally:
        store.close()


def test_paper_workflow_rejects_timezone_naive_timestamps(tmp_path) -> None:
    store = Store(tmp_path / "paper-naive-time.duckdb")
    try:
        with pytest.raises(ValueError, match="explicit timezone"):
            initialize_paper_account(
                tmp_path / "account.json",
                store,
                now=datetime(2026, 7, 20, 12),
            )
    finally:
        store.close()


def test_paper_cli_fails_closed_when_forward_trial_is_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(
        cli,
        "get_store",
        lambda: pytest.fail("the database must not open before forward governance passes"),
    )
    runner = CliRunner()

    review = runner.invoke(
        cli.app,
        ["paper-review", "--proposal", "data/paper/proposals/proposal.json"],
    )
    execute = runner.invoke(
        cli.app,
        [
            "paper-execute",
            "--proposal",
            "data/paper/proposals/proposal.json",
            "--confirm-simulated",
        ],
    )

    assert review.exit_code == 1
    assert execute.exit_code == 1
    assert "forward trial does not exist" in review.output
    assert "forward trial does not exist" in execute.output


@pytest.mark.parametrize(
    ("status", "expected_exit", "expected_text"),
    [
        ("waiting_for_scheduled_close", 0, "waiting for the scheduled U.S. close"),
        ("expired", 1, "retrospective fills are blocked"),
    ],
)
def test_paper_review_cli_reports_timing_states(
    tmp_path,
    monkeypatch,
    status,
    expected_exit,
    expected_text,
) -> None:
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(cli, "store_scope", lambda **kwargs: nullcontext(object()))
    monkeypatch.setattr(
        forward_module,
        "require_registered_forward_proposal",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        paper_module,
        "review_paper_proposal_execution",
        lambda *args, **kwargs: {
            "ready": False,
            "status": status,
            "detail": "Timing status verified.",
            "decision_date": "2026-07-20",
            "execution_date": "2026-07-21",
            "executable_after": "2026-07-21T20:00:00Z",
            "expires_at": "2026-07-22T13:30:00Z",
            "missing": [],
            "missing_count": 0,
        },
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "paper-review",
            "--proposal",
            "proposal.json",
            "--account",
            "account.json",
        ],
    )

    assert result.exit_code == expected_exit
    assert expected_text in result.output
    assert "account was not changed" in result.output


def test_paper_review_cli_prints_the_exact_confirmed_simulation_command(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(cli, "store_scope", lambda **kwargs: nullcontext(object()))
    monkeypatch.setattr(
        forward_module,
        "require_registered_forward_proposal",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        paper_module,
        "review_paper_proposal_execution",
        lambda *args, **kwargs: {
            "ready": True,
            "status": "ready_for_confirmed_simulation",
            "detail": "Every gate passed.",
            "decision_date": "2026-07-20",
            "execution_date": "2026-07-21",
            "executable_after": "2026-07-21T20:00:00Z",
            "expires_at": "2026-07-22T13:30:00Z",
            "missing": [],
            "missing_count": 0,
            "projected_trade_count": 10,
            "projected_transaction_costs": 99.90,
        },
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "paper-review",
            "--proposal",
            "proposal.json",
            "--account",
            "account.json",
        ],
    )

    assert result.exit_code == 0
    normalized_output = " ".join(result.output.split())
    assert (
        "aios paper-execute --proposal proposal.json --account account.json "
        "--confirm-simulated"
    ) in normalized_output


def test_paper_status_cli_uses_a_scoped_read_only_database(tmp_path, monkeypatch) -> None:
    scopes = []

    def scoped_store(**kwargs):
        scopes.append(kwargs)
        return nullcontext(object())

    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(cli, "store_scope", scoped_store)
    monkeypatch.setattr(
        paper_module,
        "paper_account_summary",
        lambda *args, **kwargs: {
            "last_market_date": None,
            "equity": 100_000.0,
            "cash": 100_000.0,
            "drawdown": 0.0,
            "execution_count": 0,
            "holdings": [],
        },
    )

    result = CliRunner().invoke(cli.app, ["paper-status"])

    assert result.exit_code == 0
    assert scopes == [{"read_only": True}]
    assert "Simulated account value: $100,000.00" in result.output


def test_paper_review_reports_missing_close_data_without_mutation(tmp_path) -> None:
    store = Store(tmp_path / "paper-missing-close.duckdb")
    try:
        _seed_paper_store(store)
        account_path = tmp_path / "account.json"
        proposal_path = tmp_path / "proposal.json"
        initialize_paper_account(account_path, store)
        create_paper_proposal(
            account_path,
            proposal_path,
            date(2026, 7, 20),
            store,
            now=datetime(2026, 7, 20, 22, tzinfo=UTC),
            readiness_assessor=_ready,
            composite_computer=_scores,
        )
        store.execute("DELETE FROM prices WHERE ticker = 'T00' AND date = '2026-07-21'")
        checksum_before = read_paper_document(account_path).payload_sha256

        review = review_paper_proposal_execution(
            account_path,
            proposal_path,
            store,
            now=datetime(2026, 7, 21, 20, 1, tzinfo=UTC),
            readiness_assessor=_ready,
            composite_computer=_scores,
        )

        assert review["status"] == "waiting_for_execution_data"
        assert review["ready"] is False
        assert review["missing_count"] >= 1
        assert any("T00:missing_scheduled_entry_price" in row for row in review["missing"])
        assert read_paper_document(account_path).payload_sha256 == checksum_before
    finally:
        store.close()


def test_simulated_execution_requires_explicit_confirmation(tmp_path) -> None:
    store = Store(tmp_path / "paper-confirm.duckdb")
    try:
        with pytest.raises(ValueError, match="confirm-simulated"):
            execute_paper_proposal(
                tmp_path / "missing-account.json",
                tmp_path / "missing-proposal.json",
                store,
                confirm_simulated=False,
            )
    finally:
        store.close()

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from aios.market_calendar import us_equity_sessions
from aios.paper import (
    create_paper_proposal,
    execute_paper_proposal,
    initialize_paper_account,
    mark_paper_account,
    paper_account_summary,
    read_paper_document,
)
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

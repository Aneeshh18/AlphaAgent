from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import uuid4

from aios.storage.store import Store
from aios.universe_change import capture_universe_change_state
from aios.universe_change_activation import (
    ACTIVATION_POLICY_VERSION,
    _apply_activation_transaction,
    _mutate_reference_state,
    parse_ivv_holdings,
)


def _seed_before(store: Store) -> None:
    tickers = ("AAA", "EA")
    store.upsert_universe_membership(
        [
            {
                "universe_id": "sp500",
                "ticker": ticker,
                "security_id": f"security:{ticker.lower()}",
                "effective_start": "2026-01-02",
                "effective_end": "2026-08-04",
                "known_date": "2025-12-20",
                "end_known_date": "2026-08-03",
                "source": "reviewed:test",
            }
            for ticker in tickers
        ]
    )
    store.upsert_security_identities(
        [
            {
                "universe_id": "sp500",
                "ticker": ticker,
                "security_id": f"security:{ticker.lower()}",
                "effective_start": "2026-01-02",
                "effective_end": "2026-08-04",
                "known_date": "2025-12-20",
                "identity_status": "bounded_ticker",
                "source": "reviewed:test",
            }
            for ticker in tickers
        ]
    )
    store.upsert_reference_identities(
        issuers=[
            {
                "issuer_id": f"issuer:{ticker.lower()}",
                "canonical_name": f"{ticker} Inc.",
                "canonical_ticker": ticker,
                "source": "sec:test",
            }
            for ticker in tickers
        ],
        cik_history=[
            {
                "issuer_id": f"issuer:{ticker.lower()}",
                "cik": f"{position:010d}",
                "effective_start": "2026-01-02",
                "effective_end": "2026-08-04",
                "verified_date": "2026-08-03",
                "source": "sec:test",
            }
            for position, ticker in enumerate(tickers, 1)
        ],
        security_issuers=[
            {
                "security_id": f"security:{ticker.lower()}",
                "issuer_id": f"issuer:{ticker.lower()}",
                "effective_start": "2026-01-02",
                "effective_end": "2026-08-04",
                "verified_date": "2026-08-03",
                "source": "sec:test",
            }
            for ticker in tickers
        ],
        provider_symbols=[
            {
                "provider": "yfinance",
                "provider_symbol": ticker,
                "security_id": f"security:{ticker.lower()}",
                "data_start": "2026-01-02",
                "data_end": "2026-08-04",
                "mapping_status": "verified",
                "verified_date": "2026-08-03",
                "source": "provider:test",
            }
            for ticker in tickers
        ],
    )


def _base_plan(before_state_sha256: str, before_member_set_sha256: str) -> dict:
    event_id = "uce-" + "a" * 64
    changes = [
        {
            "effective_date": "2026-08-05",
            "action": "addition",
            "company_name": "Ferguson Enterprises Inc.",
            "ticker": "FERG",
        },
        {
            "effective_date": "2026-08-05",
            "action": "deletion",
            "company_name": "Electronic Arts Inc.",
            "ticker": "EA",
        },
    ]
    return {
        "event_id": event_id,
        "universe_id": "sp500",
        "source_attestation_id": "attestation:test",
        "official_release_url": "https://press.spglobal.com/test-event",
        "announcement_date": "2026-07-31",
        "effective_date": "2026-08-05",
        "prior_coverage_through": "2026-08-03",
        "requested_coverage_through": "2026-08-06",
        "before_state": {"member_count": 2},
        "before_state_sha256": before_state_sha256,
        "before_member_set_sha256": before_member_set_sha256,
        "change_rows": changes,
        "change_rows_sha256": "b" * 64,
        "evidence": [
            {
                "role": "candidate_release_detail_000",
                "snapshot_id": "raw-detail",
            },
            {
                "role": "independent_component_snapshot",
                "snapshot_id": "raw-component",
            },
        ],
    }


def _reference() -> dict:
    return {
        "ticker": "FERG",
        "security_id": "security:ferg",
        "issuer_id": "issuer:ferg",
        "cik": "0000000003",
        "issuer": {
            "issuer_id": "issuer:ferg",
            "canonical_name": "Ferguson Enterprises Inc.",
            "canonical_ticker": "FERG",
            "cik": "0000000003",
            "effective_start": "2026-08-05",
            "effective_end": "2026-08-07",
            "verified_date": "2026-08-07",
            "source": "https://data.sec.gov/submissions/CIK0000000003.json",
        },
        "owner": {
            "security_id": "security:ferg",
            "issuer_id": "issuer:ferg",
            "effective_start": "2026-08-05",
            "effective_end": "2026-08-07",
            "verified_date": "2026-08-07",
            "source": "https://data.sec.gov/submissions/CIK0000000003.json",
        },
        "provider": {
            "provider": "yfinance",
            "provider_symbol": "FERG",
            "security_id": "security:ferg",
            "data_start": "2026-08-05",
            "data_end": "2026-08-07",
            "mapping_status": "verified",
            "verified_date": "2026-08-07",
            "source": "https://query1.finance.yahoo.com/v8/finance/chart/FERG",
        },
        "review": {
            "review_status": "accepted",
            "sec_payload_sha256": "c" * 64,
            "price_payload_sha256": "d" * 64,
        },
    }


def _prices() -> list[dict]:
    return [
        {
            "ticker": "FERG",
            "security_id": "security:ferg",
            "provider_symbol": "FERG",
            "date": day,
            "open": 250.0,
            "high": 260.0,
            "low": 245.0,
            "close": close,
            "adj_close": close,
            "volume": 1000,
            "dividends": 0.0,
            "split_ratio": 1.0,
            "actions_complete": True,
            "close_split_adjusted": True,
            "split_normalization_factor": 1.0,
            "split_normalization_through": "2026-08-06",
            "source": "yfinance",
        }
        for day, close in (("2026-08-05", 255.0), ("2026-08-06", 257.0))
    ]


def _insert_attestation(store: Store) -> None:
    store.execute(
        """
        INSERT INTO universe_coverage_attestations
        (attestation_id, run_id, universe_id, prior_coverage_through,
         requested_coverage_through, checked_at, completed_new_york_date,
         status, official_source_url, component_source_url,
         official_release_count, relevant_release_count, reviewed_member_count,
         component_count, reviewed_member_set_sha256, component_set_sha256,
         identity_match_count, identity_mismatch_count, candidate_releases_json,
         mismatch_detail_json, membership_rows_extended, security_rows_extended,
         owner_rows_extended, cik_rows_extended, provider_rows_extended, detail)
        VALUES ('attestation:test', 'attestation-run', 'sp500', DATE '2026-08-03',
                DATE '2026-08-06', now(), DATE '2026-08-06',
                'blocked_review_required', 'https://press.spglobal.com/archive',
                'https://example.test/components', 1, 1, 2, 2, ?, ?, 2, 0,
                '[]', '{}', 0, 0, 0, 0, 0, 'review required')
        """,
        ("e" * 64, "e" * 64),
    )


def test_parse_ivv_holdings_requires_post_event_ticker_set() -> None:
    header = (
        "Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,"
        "Quantity,Price,Location,Exchange,Currency,FX Rate,Market Currency,Accrual Date"
    )
    equities = [
        f'"T{position:03d}","Company {position}","Industrials","Equity","1","0.1",'
        '"1","1","1","United States","NYSE","USD","1","USD","-"'
        for position in range(489)
    ]
    equities.append(
        '"FERG","FERGUSON ENTERPRISES INC","Industrials","Equity","1","0.1",'
        '"1","1","1","United States","NYSE","USD","1","USD","-"'
    )
    payload = (
        "iShares Core S&P 500 ETF\n"
        'Fund Holdings as of,"Aug 05, 2026"\n'
        'Inception Date,"May 15, 2000"\nShares Outstanding,"1"\n'
        'Stock,"-"\nBond,"-"\nCash,"-"\nOther,"-"\n\n'
        f"{header}\n" + "\n".join(equities) + "\n"
    ).encode()

    parsed = parse_ivv_holdings(payload)

    assert parsed["as_of"] == "2026-08-05"
    assert parsed["equity_count"] == 490
    assert "FERG" in parsed["tickers"]
    assert "EA" not in parsed["tickers"]


def test_atomic_activation_receipt_reopens_and_paper_is_untouched(tmp_path: Path) -> None:
    database = tmp_path / "aios.duckdb"
    store = Store(database)
    try:
        _seed_before(store)
        _insert_attestation(store)
        before = capture_universe_change_state(
            store=store,
            universe_id="sp500",
            coverage_through=date(2026, 8, 3),
            expected_member_count=2,
        )
        base = _base_plan(before.state_sha256, before.member_set_sha256)
        store.execute("BEGIN TRANSACTION")
        counts = _mutate_reference_state(
            store=store,
            base_plan=base,
            reference=_reference(),
            price_rows=_prices(),
        )
        after = capture_universe_change_state(
            store=store,
            universe_id="sp500",
            coverage_through=date(2026, 8, 6),
            expected_member_count=2,
        )
        store.execute("ROLLBACK")
        fundamental_run = str(uuid4())
        price_run = str(uuid4())
        activation_run = str(uuid4())
        store.record_ingest(
            run_id=fundamental_run,
            source="test",
            table_name="fundamentals_staging",
            status="warning",
        )
        store.record_ingest(
            run_id=price_run,
            source="test",
            table_name="prices_staging",
            status="success",
        )
        plan = {
            "event_id": base["event_id"],
            "actor": "owner:test",
            "activation_run_id": activation_run,
            "base_plan": base,
            "reference": _reference(),
            "stage_evidence": {
                "prices": {"run_id": price_run, "rows": _prices()},
                "fundamentals": {
                    "run_id": fundamental_run,
                    "status": "fundamentals_pending",
                },
                "holdings": {"review": {"as_of": "2026-08-05"}},
            },
            "backup": {"manifest_sha256": "f" * 64},
            "expected_after": {
                "member_count": 2,
                "member_set_sha256": after.member_set_sha256,
                "state_sha256": after.state_sha256,
                "counts": counts,
            },
        }
        result = _apply_activation_transaction(
            store=store,
            project_root=tmp_path,
            plan=plan,
            plan_sha256="1" * 64,
            actor="owner:test",
        )
    finally:
        store.close()

    refreshed = Store(database)
    try:
        refreshed.execute(
            """
            UPDATE prices
            SET open = 251.0, high = 261.0, low = 246.0, close = 256.0,
                adj_close = 256.0, volume = 2000,
                source = 'yfinance:ohlc-envelope-v1'
            WHERE ticker = 'FERG' AND date = DATE '2026-08-05'
            """
        )
    finally:
        refreshed.close()

    reopened = Store(database, read_only=True)
    try:
        rows = reopened.query("SELECT * FROM universe_constituent_change_activations")
        current = capture_universe_change_state(
            store=reopened,
            universe_id="sp500",
            coverage_through=date(2026, 8, 6),
            expected_member_count=2,
        )
    finally:
        reopened.close()

    assert len(rows) == 1
    assert rows[0]["policy_version"] == ACTIVATION_POLICY_VERSION
    assert result.after_state_sha256 == current.state_sha256
    assert [row["ticker"] for row in current.payload["members"]] == ["AAA", "FERG"]
    assert result.paper_mutated is False
    assert result.broker_used is False

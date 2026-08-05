from __future__ import annotations

from datetime import date

import duckdb
import pytest

from aios.ingest.security_identity import (
    SecurityTransition,
    build_security_identity_assignments,
    load_security_identity_csv,
    load_security_transitions_csv,
    write_security_identity_csv,
)
from aios.storage.store import Store


def _membership(
    ticker: str,
    start: str,
    end: str | None,
    known: str = "2024-01-01",
) -> dict:
    return {
        "universe_id": "demo",
        "ticker": ticker,
        "effective_start": date.fromisoformat(start),
        "effective_end": date.fromisoformat(end) if end else None,
        "known_date": date.fromisoformat(known),
        "source": "https://example.com/membership",
    }


def _transition(
    from_ticker: str = "OLD",
    to_ticker: str = "NEW",
    effective: str = "2024-07-01",
) -> SecurityTransition:
    return SecurityTransition(
        universe_id="demo",
        security_id="aios:security:demo-common",
        from_ticker=from_ticker,
        to_ticker=to_ticker,
        effective_date=date.fromisoformat(effective),
        known_date=date(2024, 6, 15),
        transition_type="ticker_change",
        source="https://example.com/ticker-change",
    )


def test_builder_links_only_explicit_same_security_transitions():
    memberships = [
        _membership("OLD", "2024-01-01", "2024-07-01"),
        _membership("NEW", "2024-07-01", "2025-01-01", "2024-06-15"),
        _membership("WRK", "2024-01-01", "2024-07-08"),
        _membership("SW", "2024-07-08", "2025-01-01", "2024-06-27"),
    ]

    rows = build_security_identity_assignments(
        memberships,
        [_transition()],
        universe_id="demo",
    )
    by_ticker = {row["ticker"]: row for row in rows}

    assert by_ticker["OLD"]["security_id"] == "aios:security:demo-common"
    assert by_ticker["NEW"]["security_id"] == "aios:security:demo-common"
    assert by_ticker["OLD"]["identity_status"] == "verified_ticker_change"
    assert by_ticker["WRK"]["security_id"] != by_ticker["SW"]["security_id"]
    assert by_ticker["WRK"]["identity_status"] == "bounded_ticker"


def test_builder_rejects_transition_without_exact_membership_boundary():
    memberships = [
        _membership("OLD", "2024-01-01", "2024-07-02"),
        _membership("NEW", "2024-07-01", "2025-01-01", "2024-06-15"),
    ]

    with pytest.raises(ValueError, match="does not match"):
        build_security_identity_assignments(
            memberships,
            [_transition()],
            universe_id="demo",
        )


def test_transition_csv_requires_https_and_actionable_dates(tmp_path):
    path = tmp_path / "transitions.csv"
    path.write_text(
        "universe_id,security_id,from_ticker,to_ticker,effective_date,"
        "known_date,transition_type,source\n"
        "demo,aios:security:x,OLD,NEW,2024-07-01,2024-07-02,"
        "ticker_change,http://example.com/change\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="known_date follows"):
        load_security_transitions_csv(path, universe_id="demo")


def test_identity_csv_round_trip_and_transactional_import(tmp_path):
    memberships = [
        _membership("OLD", "2024-01-01", "2024-07-01"),
        _membership("NEW", "2024-07-01", "2025-01-01", "2024-06-15"),
    ]
    assignments = build_security_identity_assignments(
        memberships,
        [_transition()],
        universe_id="demo",
    )
    path = tmp_path / "identities.csv"
    write_security_identity_csv(path, assignments)
    assert load_security_identity_csv(path) == assignments

    store = Store(tmp_path / "identities.duckdb")
    try:
        store.upsert_universe_membership(memberships)
        assert store.upsert_security_identities(assignments) == 2

        before = store.universe_membership_on("demo", "2024-06-30")[0]
        after = store.universe_membership_on("demo", "2024-07-01")[0]
        assert before["ticker"] == "OLD"
        assert after["ticker"] == "NEW"
        assert before["security_id"] == after["security_id"]
        store.upsert_universe_membership(memberships)
        assert (
            store.universe_membership_on("demo", "2024-07-01")[0]["security_id"]
            == after["security_id"]
        )
        report = {row["check"]: row for row in store.data_quality_report()}
        assert report["universe_missing_security_ids"]["status"] == "ok"
        assert report["universe_orphan_security_ids"]["status"] == "ok"
        assert report["security_identity_membership_mismatches"]["status"] == "ok"
        assert report["security_identity_overlapping_tickers"]["status"] == "ok"
    finally:
        store.close()


def test_existing_universe_schema_migrates_without_rewriting_rows(tmp_path):
    db_path = tmp_path / "identity-migration.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE universe_membership (
                universe_id VARCHAR NOT NULL,
                ticker VARCHAR NOT NULL,
                effective_start DATE NOT NULL,
                effective_end DATE,
                known_date DATE NOT NULL,
                source VARCHAR NOT NULL,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (universe_id, ticker, effective_start)
            )
            """
        )
        con.execute(
            """
            INSERT INTO universe_membership
            (universe_id, ticker, effective_start, effective_end, known_date, source)
            VALUES ('demo', 'A', '2024-01-01', NULL, '2023-12-15', 'test')
            """
        )
    finally:
        con.close()

    store = Store(db_path, allow_schema_upgrade=True)
    try:
        columns = {
            row["column_name"] for row in store.query("DESCRIBE universe_membership")
        }
        assert "security_id" in columns
        row = store.query("SELECT ticker, security_id FROM universe_membership")[0]
        assert row == {"ticker": "A", "security_id": None}
        report = {row["check"]: row for row in store.data_quality_report()}
        assert report["universe_missing_security_ids"]["status"] == "fail"
    finally:
        store.close()


def test_identity_import_rejects_overlap_and_rolls_back(tmp_path):
    memberships = [
        _membership("A", "2024-01-01", "2025-01-01"),
        _membership("B", "2024-06-01", "2025-01-01"),
    ]
    assignments = [
        {
            **membership,
            "security_id": "aios:security:collision",
            "identity_status": "verified_ticker_change",
        }
        for membership in memberships
    ]
    store = Store(tmp_path / "overlap.duckdb")
    try:
        store.upsert_universe_membership(memberships)
        with pytest.raises(ValueError, match="overlapping ticker identities"):
            store.upsert_security_identities(assignments)

        assert store.query("SELECT COUNT(*) AS n FROM security_master")[0]["n"] == 0
        assert store.query(
            "SELECT COUNT(*) AS n FROM security_identity_assignments"
        )[0]["n"] == 0
        assert store.query(
            "SELECT COUNT(*) AS n FROM universe_membership WHERE security_id IS NOT NULL"
        )[0]["n"] == 0
    finally:
        store.close()


def test_universe_coverage_is_pit_and_uses_dated_membership_ticker(tmp_path):
    memberships = [
        _membership("OLD", "2024-01-01", "2024-07-01"),
        _membership("NEW", "2024-07-01", "2025-01-01", "2024-06-15"),
    ]
    assignments = build_security_identity_assignments(
        memberships,
        [_transition()],
        universe_id="demo",
    )
    store = Store(tmp_path / "coverage.duckdb")
    try:
        store.upsert_universe_membership(memberships)
        store.upsert_security_identities(assignments)
        store.upsert_prices(
            [
                {
                    "ticker": "OLD",
                    "date": "2024-06-28",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                    "adj_close": 100.0,
                    "volume": 1_000,
                    "source": "test",
                }
            ]
        )
        store.upsert_fundamentals(
            [
                {
                    "ticker": "OLD",
                    "period_end": "2024-03-31",
                    "as_of_date": "2024-05-01",
                    "fiscal_period": "Q1_2024",
                    "statement": "income",
                    "metric": "revenue",
                    "value": 100.0,
                    "quarter_value": 100.0,
                    "unit": "USD",
                    "source": "test",
                }
            ]
        )

        before = store.universe_data_coverage("demo", "2024-06-30")
        after = store.universe_data_coverage("demo", "2024-07-01")
        assert before[0]["ticker"] == "OLD"
        assert before[0]["has_price_history"] is True
        assert before[0]["has_pit_fundamentals"] is True
        assert after[0]["ticker"] == "NEW"
        assert after[0]["security_id"] == before[0]["security_id"]
        assert after[0]["has_price_history"] is False
        assert after[0]["has_pit_fundamentals"] is False
    finally:
        store.close()

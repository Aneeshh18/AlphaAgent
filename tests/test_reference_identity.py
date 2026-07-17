from __future__ import annotations

from datetime import date

import duckdb
import pytest

from aios.ingest import edgar, prices
from aios.ingest.reference_identity import (
    ingest_reference_identity_csvs,
    load_issuer_cik_csv,
    load_provider_symbol_csv,
)
from aios.storage.store import Store

SECURITY_ID = "aios:security:demo-common"
ISSUER_ID = "aios:issuer:demo"
EVIDENCE = "https://www.sec.gov/Archives/edgar/data/1/example.htm"
PROVIDER_EVIDENCE = "https://query1.finance.yahoo.com/v8/finance/chart/NEW"


def _membership(ticker: str, start: str, end: str | None, known: str) -> dict:
    return {
        "universe_id": "demo",
        "ticker": ticker,
        "effective_start": start,
        "effective_end": end,
        "known_date": known,
        "source": EVIDENCE,
    }


def _setup_security(store: Store) -> None:
    memberships = [
        _membership("OLD", "2024-01-01", "2024-07-01", "2023-12-15"),
        _membership("NEW", "2024-07-01", "2025-01-01", "2024-06-15"),
    ]
    identities = [
        {
            **row,
            "security_id": SECURITY_ID,
            "identity_status": "verified_ticker_change",
        }
        for row in memberships
    ]
    store.upsert_universe_membership(memberships)
    store.upsert_security_identities(identities)


def _reference_rows(
    *, provider_start: str = "2024-01-01"
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    issuers = [
        {
            "issuer_id": ISSUER_ID,
            "canonical_name": "Demo Corporation",
            "canonical_ticker": "NEW",
            "source": EVIDENCE,
        }
    ]
    ciks = [
        {
            "issuer_id": ISSUER_ID,
            "cik": "1",
            "effective_start": "2024-01-01",
            "effective_end": "2025-01-01",
            "verified_date": "2024-06-15",
            "source": EVIDENCE,
        }
    ]
    owners = [
        {
            "security_id": SECURITY_ID,
            "issuer_id": ISSUER_ID,
            "effective_start": "2024-01-01",
            "effective_end": "2025-01-01",
            "verified_date": "2024-06-15",
            "source": EVIDENCE,
        }
    ]
    providers = [
        {
            "provider": "yfinance",
            "provider_symbol": "NEW",
            "security_id": SECURITY_ID,
            "data_start": provider_start,
            "data_end": "2025-01-01",
            "mapping_status": "verified",
            "verified_date": "2024-06-15",
            "source": PROVIDER_EVIDENCE,
        }
    ]
    return issuers, ciks, owners, providers


def _install_reference_rows(store: Store, *, provider_start: str = "2024-01-01") -> None:
    store.upsert_reference_identities(*_reference_rows(provider_start=provider_start))


def _fundamental(
    value: float,
    *,
    ticker: str = "NEW",
    issuer_id: str | None = None,
    security_id: str | None = None,
) -> dict:
    return {
        "ticker": ticker,
        "issuer_id": issuer_id,
        "security_id": security_id,
        "period_end": "2024-03-31",
        "as_of_date": "2024-05-01",
        "fiscal_period": "Q1_2024",
        "statement": "income",
        "metric": "revenue",
        "value": value,
        "quarter_value": value,
        "unit": "USD",
        "source": "test",
    }


def test_existing_price_and_fundamental_tables_migrate_additively(tmp_path):
    db_path = tmp_path / "reference-migration.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE prices (
                ticker VARCHAR NOT NULL, date DATE NOT NULL, open DOUBLE,
                high DOUBLE, low DOUBLE, close DOUBLE, adj_close DOUBLE,
                volume BIGINT, dividends DOUBLE, split_ratio DOUBLE,
                source VARCHAR, fetched_at TIMESTAMP,
                PRIMARY KEY (ticker, date)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE fundamentals (
                ticker VARCHAR NOT NULL, period_end DATE NOT NULL,
                as_of_date DATE NOT NULL, fiscal_period VARCHAR,
                statement VARCHAR, metric VARCHAR NOT NULL, value DOUBLE,
                quarter_value DOUBLE, unit VARCHAR, source VARCHAR,
                fetched_at TIMESTAMP,
                PRIMARY KEY (ticker, period_end, as_of_date, metric)
            )
            """
        )
        con.execute(
            "INSERT INTO prices (ticker, date, close) VALUES ('OLD', '2024-01-02', 10)"
        )
        con.execute(
            """
            INSERT INTO fundamentals
            (ticker, period_end, as_of_date, metric, value)
            VALUES ('OLD', '2023-12-31', '2024-02-01', 'revenue', 100)
            """
        )
    finally:
        con.close()

    store = Store(db_path)
    try:
        price_columns = {row["column_name"] for row in store.query("DESCRIBE prices")}
        fundamental_columns = {
            row["column_name"] for row in store.query("DESCRIBE fundamentals")
        }
        assert {"security_id", "provider_symbol"} <= price_columns
        assert {"issuer_id", "security_id"} <= fundamental_columns
        assert store.query("SELECT close, security_id FROM prices")[0] == {
            "close": 10.0,
            "security_id": None,
        }
        assert store.query("SELECT value, issuer_id FROM fundamentals")[0] == {
            "value": 100.0,
            "issuer_id": None,
        }
    finally:
        store.close()


def test_strict_csv_import_normalizes_cik_and_is_idempotent(tmp_path):
    issuer_path = tmp_path / "issuers.csv"
    owner_path = tmp_path / "owners.csv"
    provider_path = tmp_path / "providers.csv"
    issuer_path.write_text(
        "issuer_id,canonical_name,canonical_ticker,cik,effective_start,"
        "effective_end,verified_date,source\n"
        f"{ISSUER_ID},Demo Corporation,NEW,1,2024-01-01,2025-01-01,"
        f"2024-06-15,{EVIDENCE}\n",
        encoding="utf-8",
    )
    owner_path.write_text(
        "security_id,issuer_id,effective_start,effective_end,verified_date,source\n"
        f"{SECURITY_ID},{ISSUER_ID},2024-01-01,2025-01-01,2024-06-15,"
        f"{EVIDENCE}\n",
        encoding="utf-8",
    )
    provider_path.write_text(
        "provider,provider_symbol,security_id,data_start,data_end,mapping_status,"
        "verified_date,source\n"
        f"yfinance,NEW,{SECURITY_ID},2024-01-01,2025-01-01,verified,"
        f"2024-06-15,{PROVIDER_EVIDENCE}\n",
        encoding="utf-8",
    )

    issuers, ciks = load_issuer_cik_csv(issuer_path)
    assert issuers[0]["canonical_ticker"] == "NEW"
    assert ciks[0]["cik"] == "0000000001"
    assert load_provider_symbol_csv(provider_path)[0]["mapping_status"] == "verified"

    store = Store(tmp_path / "reference-import.duckdb")
    try:
        _setup_security(store)
        first = ingest_reference_identity_csvs(
            issuer_path, owner_path, provider_path, store=store
        )
        second = ingest_reference_identity_csvs(
            issuer_path, owner_path, provider_path, store=store
        )
        assert first == second == {
            "issuers": 1,
            "cik_history": 1,
            "security_issuers": 1,
            "provider_symbols": 1,
        }
        assert store.issuer_reference(ISSUER_ID)["cik"] == "0000000001"
    finally:
        store.close()


def test_reference_import_overlap_rolls_back_every_table(tmp_path):
    store = Store(tmp_path / "reference-overlap.duckdb")
    try:
        _setup_security(store)
        issuers, ciks, owners, providers = _reference_rows()
        owners.append(
            {
                **owners[0],
                "effective_start": "2024-06-01",
                "effective_end": "2024-12-01",
            }
        )
        with pytest.raises(ValueError, match="overlapping security issuer"):
            store.upsert_reference_identities(issuers, ciks, owners, providers)
        assert store.query("SELECT COUNT(*) AS n FROM issuer_master")[0]["n"] == 0
        assert store.query("SELECT COUNT(*) AS n FROM provider_symbol_history")[0]["n"] == 0
    finally:
        store.close()


def test_pit_fundamentals_follow_issuer_and_ignore_ticker_contamination(tmp_path):
    store = Store(tmp_path / "issuer-pit.duckdb")
    try:
        _setup_security(store)
        _install_reference_rows(store)
        contamination = _fundamental(999)
        contamination["period_end"] = "2023-12-31"
        contamination["as_of_date"] = "2024-06-01"
        store.upsert_fundamentals(
            [
                contamination,
                _fundamental(
                    100,
                    issuer_id=ISSUER_ID,
                    security_id=SECURITY_ID,
                ),
            ]
        )

        old_ticker = store.pit_fundamentals("OLD", "2024-06-30", ["revenue"])
        new_ticker = store.pit_fundamentals("NEW", "2024-07-01", ["revenue"])
        assert old_ticker[0]["value"] == 100
        assert new_ticker[0]["value"] == 100
        assert store.fundamental_history("NEW", "2024-07-01", "revenue")[0][
            "quarter_value"
        ] == 100
    finally:
        store.close()


def test_provider_rows_are_cut_off_and_relabelled_by_security_date():
    mapping = {
        "provider": "yfinance",
        "provider_symbol": "NEW",
        "security_id": SECURITY_ID,
        "data_start": date(2024, 7, 1),
        "data_end": date(2025, 1, 1),
        "mapping_status": "verified",
    }
    assignments = [
        {"ticker": "OLD", "effective_start": "2024-01-01", "effective_end": "2024-07-01"},
        {"ticker": "NEW", "effective_start": "2024-07-01", "effective_end": "2025-01-01"},
    ]
    raw = [
        {"ticker": "NEW", "date": "2024-06-28", "close": 99.0},
        {"ticker": "NEW", "date": "2024-07-01", "close": 100.0},
    ]

    output = prices.relabel_provider_price_rows(raw, mapping, assignments)

    assert [(row["ticker"], row["date"]) for row in output] == [
        ("NEW", "2024-07-01")
    ]
    assert output[0]["security_id"] == SECURITY_ID
    assert output[0]["provider_symbol"] == "NEW"


def test_identity_price_ingest_preserves_transition_and_coverage(monkeypatch, tmp_path):
    store = Store(tmp_path / "identity-prices.duckdb")
    try:
        _setup_security(store)
        _install_reference_rows(store)
        store.upsert_fundamentals(
            [_fundamental(100, issuer_id=ISSUER_ID, security_id=SECURITY_ID)]
        )

        monkeypatch.setattr(
            prices,
            "fetch_provider_prices",
            lambda *_args, **_kwargs: [
                {"ticker": "NEW", "date": "2024-06-28", "close": 99.0},
                {"ticker": "NEW", "date": "2024-07-01", "close": 100.0},
            ],
        )
        inserted = prices.ingest_security_prices(
            SECURITY_ID,
            provider="yfinance",
            start="2024-01-01",
            end="2025-01-01",
            store=store,
        )

        assert inserted == 2
        stored = store.query(
            "SELECT ticker, date, security_id, provider_symbol FROM prices ORDER BY date"
        )
        assert [row["ticker"] for row in stored] == ["OLD", "NEW"]
        assert all(row["security_id"] == SECURITY_ID for row in stored)
        assert store.latest_price("NEW", "2024-07-01")["close"] == 100.0
        coverage = store.universe_data_coverage("demo", "2024-07-01")[0]
        assert coverage["issuer_id"] == ISSUER_ID
        assert coverage["has_price_history"] is True
        assert coverage["has_pit_fundamentals"] is True
        report = {row["check"]: row for row in store.data_quality_report()}
        assert report["tagged_prices_outside_provider_provenance"]["status"] == "ok"
        assert report["tagged_prices_wrong_dated_ticker"]["status"] == "ok"
    finally:
        store.close()


def test_ingest_issuer_uses_reviewed_cik_and_tags_rows(monkeypatch, tmp_path):
    store = Store(tmp_path / "issuer-ingest.duckdb")
    try:
        _setup_security(store)
        _install_reference_rows(store)

        def fake_extract(ticker, cik, *, issuer_id, security_id):
            assert (ticker, cik) == ("NEW", 1)
            assert issuer_id == ISSUER_ID
            assert security_id == SECURITY_ID
            return (
                [
                    _fundamental(
                        123,
                        issuer_id=issuer_id,
                        security_id=security_id,
                    )
                ],
                {"name": "Demo Corporation"},
            )

        monkeypatch.setattr(edgar, "extract_fundamentals", fake_extract)
        assert edgar.ingest_issuer(ISSUER_ID, store=store) == 1
        row = store.query("SELECT issuer_id, security_id FROM fundamentals")[0]
        assert row == {"issuer_id": ISSUER_ID, "security_id": SECURITY_ID}
    finally:
        store.close()

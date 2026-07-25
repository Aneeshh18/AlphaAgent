from __future__ import annotations

import gzip
import json
import sys
from datetime import date
from types import SimpleNamespace
from zipfile import ZipFile

import duckdb
import pandas as pd
import pytest

from aios.ingest import edgar, prices
from aios.ingest.reference_identity import (
    ingest_reference_identity_csvs,
    load_issuer_cik_csv,
    load_provider_symbol_csv,
)
from aios.raw_snapshots import verify_raw_snapshots
from aios.storage.store import Store

SECURITY_ID = "aios:security:demo-common"
ISSUER_ID = "aios:issuer:demo"
EVIDENCE = "https://www.sec.gov/Archives/edgar/data/1/example.htm"
PROVIDER_EVIDENCE = "https://query1.finance.yahoo.com/v8/finance/chart/NEW"


def test_yfinance_fetch_explicitly_requests_and_marks_actions(monkeypatch) -> None:
    captured: dict = {}

    def fake_download(*args, **kwargs):
        captured.update(kwargs)
        return pd.DataFrame(
            {
                "Open": [100.0, 50.0],
                "High": [101.0, 51.0],
                "Low": [99.0, 49.0],
                "Close": [100.0, 50.0],
                "Adj Close": [99.0, 50.0],
                "Volume": [1000, 2000],
                "Dividends": [1.0, 0.0],
                "Stock Splits": [0.0, 2.0],
            },
            # The later split is outside the caller's requested window, but
            # Yahoo has already normalized the earlier close for it.
            index=[pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-04")],
        )

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=fake_download))

    rows = prices.fetch_yfinance("TEST", start="2024-01-01", end="2024-01-03")

    assert captured["actions"] is True
    assert date.fromisoformat(captured["end"]) > date(2024, 1, 3)
    assert len(rows) == 1
    assert rows[0]["dividends"] == 1.0
    assert rows[0]["split_ratio"] == 1.0
    assert rows[0]["actions_complete"] is True
    assert rows[0]["close_split_adjusted"] is True
    assert rows[0]["split_normalization_factor"] == 2.0
    assert (
        rows[0]["split_normalization_through"]
        == prices.latest_completed_us_equity_session().isoformat()
    )


def test_yfinance_excludes_an_open_us_session_after_india_midnight(monkeypatch) -> None:
    def fake_download(*_args, **_kwargs):
        return pd.DataFrame(
            {
                "Open": [100.0, 50.0],
                "High": [101.0, 51.0],
                "Low": [99.0, 49.0],
                "Close": [100.0, 50.0],
                "Adj Close": [100.0, 50.0],
                "Volume": [1000, 2000],
                "Dividends": [0.0, 0.0],
                "Stock Splits": [0.0, 2.0],
            },
            index=[pd.Timestamp("2026-07-22"), pd.Timestamp("2026-07-23")],
        )

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=fake_download))
    monkeypatch.setattr(
        prices,
        "latest_completed_us_equity_session",
        lambda: date(2026, 7, 22),
    )

    rows = prices.fetch_yfinance("TEST", start="2026-07-22", end="2026-07-25")

    assert [row["date"] for row in rows] == ["2026-07-22"]
    assert rows[0]["split_normalization_factor"] == 1.0
    assert rows[0]["split_normalization_through"] == "2026-07-22"


def test_yfinance_fetch_retries_an_isolated_empty_response(monkeypatch) -> None:
    attempts = 0
    sleeps: list[float] = []

    def fake_download(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return pd.DataFrame()
        return pd.DataFrame(
            {
                "Open": [100.0],
                "High": [101.0],
                "Low": [99.0],
                "Close": [100.0],
                "Adj Close": [100.0],
                "Volume": [1000],
                "Dividends": [0.0],
                "Stock Splits": [0.0],
            },
            index=[pd.Timestamp("2024-01-02")],
        )

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=fake_download))
    monkeypatch.setattr(prices.time, "sleep", sleeps.append)

    rows = prices.fetch_yfinance("TEST", start="2024-01-01", end="2024-01-03")

    assert attempts == 2
    assert sleeps == [prices.settings.yfinance_retry_base_sec]
    assert [row["date"] for row in rows] == ["2024-01-02"]


def test_yfinance_fetch_fails_closed_after_bounded_empty_retries(monkeypatch) -> None:
    attempts = 0
    sleeps: list[float] = []

    def fake_download(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return pd.DataFrame()

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=fake_download))
    monkeypatch.setattr(prices.time, "sleep", sleeps.append)

    rows = prices.fetch_yfinance("TEST", start="2024-01-01", end="2024-01-03")

    assert rows == []
    assert attempts == prices.settings.yfinance_max_attempts
    assert sleeps == [
        prices.settings.yfinance_retry_base_sec,
        prices.settings.yfinance_retry_base_sec * 2,
    ]


def test_yfinance_normalized_export_is_linked_and_replay_verified(
    monkeypatch,
    tmp_path,
) -> None:
    def fake_download(*_args, **_kwargs):
        return pd.DataFrame(
            {
                "Open": [100.0, 50.0],
                "High": [101.0, 51.0],
                "Low": [99.0, 49.0],
                "Close": [100.0, 50.0],
                "Adj Close": [99.0, 50.0],
                "Volume": [1000, 2000],
                "Dividends": [1.0, 0.0],
                "Stock Splits": [0.0, 2.0],
            },
            index=[pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-04")],
        )

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=fake_download))
    monkeypatch.setattr(
        prices,
        "latest_completed_us_equity_session",
        lambda: date(2024, 1, 5),
    )
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        rows = prices.fetch_yfinance(
            "TEST",
            start="2024-01-01",
            end="2024-01-03",
            store=store,
            ingest_run_id="price-run",
            project_root=tmp_path,
        )

        snapshot = store.query(
            """
            SELECT snapshot_id, artifact_kind, parsed_row_count,
                   parsed_rows_sha256, payload_sha256
            FROM raw_snapshots
            """
        )[0]
        assert snapshot["artifact_kind"] == "normalized_provider_export"
        assert snapshot["parsed_row_count"] == 1
        assert snapshot["parsed_rows_sha256"]
        assert store.query(
            "SELECT run_id, role FROM ingest_raw_snapshots"
        ) == [{"run_id": "price-run", "role": "prices:TEST"}]
        payload_record = store.raw_payload_record(snapshot["payload_sha256"])
        payload = gzip.decompress(
            (tmp_path / payload_record["relative_path"]).read_bytes()
        )
        assert prices.parse_yfinance_normalized_export(payload) == rows
        verification = verify_raw_snapshots(store=store, project_root=tmp_path)
        assert verification.replayed_snapshots == 1

        store.execute(
            "UPDATE raw_snapshots SET parsed_rows_sha256 = ? WHERE snapshot_id = ?",
            ("0" * 64, snapshot["snapshot_id"]),
        )
        with pytest.raises(ValueError, match="replay checksum mismatch"):
            verify_raw_snapshots(store=store, project_root=tmp_path)
    finally:
        store.close()


def test_companyfacts_archive_reads_reviewed_cik_and_rejects_wrong_payload(tmp_path):
    valid_path = tmp_path / "companyfacts.zip"
    with ZipFile(valid_path, "w") as archive:
        archive.writestr(
            "nested/CIK0000000001.json",
            json.dumps({"cik": 1, "entityName": "Demo", "facts": {}}),
        )

    with edgar.CompanyFactsArchive(valid_path) as archive:
        archive.validate_ciks([1])
        assert archive.read(1)["entityName"] == "Demo"
        with pytest.raises(ValueError, match="no member for CIK 0000000002"):
            archive.validate_ciks([2])

    mismatch_path = tmp_path / "mismatch.zip"
    with ZipFile(mismatch_path, "w") as archive:
        archive.writestr(
            "CIK0000000001.json",
            json.dumps({"cik": 2, "entityName": "Wrong", "facts": {}}),
        )
    with (
        edgar.CompanyFactsArchive(mismatch_path) as archive,
        pytest.raises(ValueError, match="does not match reviewed CIK"),
    ):
        archive.read(1)


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


def test_universe_identity_labels_use_reviewed_issuer_names(tmp_path):
    store = Store(tmp_path / "identity-labels.duckdb")
    try:
        _setup_security(store)
        _install_reference_rows(store)

        old_label = store.universe_identity_labels("demo", "2024-06-30")[0]
        new_label = store.universe_identity_labels("demo", "2024-07-01")[0]

        assert old_label["ticker"] == "OLD"
        assert new_label["ticker"] == "NEW"
        assert old_label["security_id"] == new_label["security_id"] == SECURITY_ID
        assert old_label["issuer_id"] == new_label["issuer_id"] == ISSUER_ID
        assert old_label["canonical_name"] == new_label["canonical_name"] == "Demo Corporation"
        assert old_label["name_source"] == EVIDENCE
    finally:
        store.close()


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
        con.execute("INSERT INTO prices (ticker, date, close) VALUES ('OLD', '2024-01-02', 10)")
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
        fundamental_columns = {row["column_name"] for row in store.query("DESCRIBE fundamentals")}
        assert {
            "security_id",
            "provider_symbol",
            "actions_complete",
            "close_split_adjusted",
            "split_normalization_factor",
            "split_normalization_through",
        } <= price_columns
        assert {"issuer_id", "security_id"} <= fundamental_columns
        assert store.query("SELECT close, security_id FROM prices")[0] == {
            "close": 10.0,
            "security_id": None,
        }
        assert store.query("SELECT actions_complete FROM prices")[0]["actions_complete"] is False
        assert (
            store.query("SELECT close_split_adjusted FROM prices")[0]["close_split_adjusted"]
            is None
        )
        assert (
            store.query("SELECT split_normalization_factor FROM prices")[0][
                "split_normalization_factor"
            ]
            is None
        )
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
        first = ingest_reference_identity_csvs(issuer_path, owner_path, provider_path, store=store)
        second = ingest_reference_identity_csvs(issuer_path, owner_path, provider_path, store=store)
        assert (
            first
            == second
            == {
                "issuers": 1,
                "cik_history": 1,
                "security_issuers": 1,
                "provider_symbols": 1,
            }
        )
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
        assert store.fundamental_history("NEW", "2024-07-01", "revenue")[0]["quarter_value"] == 100
        assert store.pit_factor_fundamentals("OLD", "2024-06-30", ["revenue"])[0]["value"] == 100
        assert store.pit_factor_fundamentals("NEW", "2024-07-01", ["revenue"])[0]["value"] == 100
    finally:
        store.close()


def test_reviewed_owner_gap_cannot_fall_back_to_legacy_fundamentals(tmp_path):
    store = Store(tmp_path / "reviewed-owner-gap.duckdb")
    try:
        _setup_security(store)
        store.upsert_fundamentals([_fundamental(77, ticker="OLD")])

        assert store.pit_fundamentals("OLD", "2024-06-30", ["revenue"])[0]["value"] == 77
        assert store.fundamental_history("OLD", "2024-06-30", "revenue")[0]["quarter_value"] == 77

        issuers, ciks, owners, providers = _reference_rows()
        owners[0]["effective_start"] = "2024-07-01"
        store.upsert_reference_identities(issuers, ciks, owners, providers)

        assert store.pit_fundamentals("OLD", "2024-06-30", ["revenue"]) == []
        assert store.fundamental_history("OLD", "2024-06-30", "revenue") == []
        assert store.pit_factor_fundamentals("OLD", "2024-06-30", ["revenue"]) == []
        coverage = store.universe_data_coverage("demo", "2024-06-30")[0]
        assert coverage["has_pit_fundamentals"] is False
        assert coverage["latest_fundamental_date"] is None
    finally:
        store.close()


def test_future_period_end_is_rejected_reported_and_filtered(tmp_path):
    store = Store(tmp_path / "future-fundamental-period.duckdb")
    try:
        future_period = _fundamental(123, ticker="FUTURE")
        future_period["period_end"] = "2024-06-30"
        future_period["as_of_date"] = "2024-05-01"

        with pytest.raises(ValueError, match="period_end later than as_of_date"):
            store.upsert_fundamentals([future_period])

        store.execute(
            """
            INSERT INTO fundamentals
            (ticker, period_end, as_of_date, fiscal_period, statement, metric,
             value, quarter_value, unit, source)
            VALUES
            ('FUTURE', '2024-06-30', '2024-05-01', 'Q2_2024', 'income',
             'revenue', 123, 123, 'USD', 'legacy-test')
            """
        )

        report = {row["check"]: row for row in store.data_quality_report()}
        assert report["fundamentals_period_end_after_as_of_date"] == {
            "check": "fundamentals_period_end_after_as_of_date",
            "status": "fail",
            "count": 1,
            "detail": "A fiscal period cannot end after the filing became publicly knowable.",
        }
        assert store.pit_fundamentals("FUTURE", "2024-12-31", ["revenue"]) == []
        assert store.fundamental_history("FUTURE", "2024-12-31", "revenue") == []
        assert store.quarantine_invalid_fundamental_periods() == 1
        assert store.query("SELECT COUNT(*) AS n FROM fundamentals")[0]["n"] == 0
        quarantined = store.query("SELECT quarantine_reason FROM fundamentals_quarantine")
        assert quarantined == [{"quarantine_reason": "period_end_after_as_of_date"}]
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

    assert [(row["ticker"], row["date"]) for row in output] == [("NEW", "2024-07-01")]
    assert output[0]["security_id"] == SECURITY_ID
    assert output[0]["provider_symbol"] == "NEW"


def test_sec_submission_history_file_allows_only_official_shard_names(monkeypatch):
    class FakeHttp:
        def get_json(self, url):
            assert url.endswith("/CIK0000000001-submissions-001.json")
            return {"filingDate": ["2020-01-01"]}

    monkeypatch.setattr(edgar, "get_http", lambda: FakeHttp())

    payload = edgar.fetch_submission_file("CIK0000000001-submissions-001.json")
    assert payload == {"filingDate": ["2020-01-01"]}
    with pytest.raises(ValueError, match="invalid SEC submissions history filename"):
        edgar.fetch_submission_file("../companyfacts.json")


def test_tiingo_fetch_uses_header_token_and_preserves_exclusive_end(monkeypatch):
    class FakeHttp:
        def get_json(self, url, headers=None):
            assert "token=" not in url
            assert headers == {"Authorization": "Token test-token"}
            return [
                {
                    "date": "2024-01-02T00:00:00.000Z",
                    "open": 10,
                    "high": 12,
                    "low": 9,
                    "close": 11,
                    "adjClose": 10.5,
                    "volume": 1000,
                    "divCash": 0.25,
                    "splitFactor": 1,
                },
                {
                    "date": "2024-01-03T00:00:00.000Z",
                    "close": 12,
                },
            ]

    monkeypatch.setattr(prices, "settings", SimpleNamespace(tiingo_api_key="test-token"))
    monkeypatch.setattr(prices, "get_http", lambda: FakeHttp())

    rows = prices.fetch_tiingo("tst", start="2024-01-01", end="2024-01-03")

    assert len(rows) == 1
    assert rows[0] == {
        "ticker": "TST",
        "date": "2024-01-02",
        "open": 10.0,
        "high": 12.0,
        "low": 9.0,
        "close": 11.0,
        "adj_close": 10.5,
        "volume": 1000,
        "dividends": 0.25,
        "split_ratio": 1.0,
        "actions_complete": True,
        "close_split_adjusted": False,
        "split_normalization_factor": 1.0,
        "split_normalization_through": None,
        "source": "tiingo",
    }


def test_identity_price_ingest_preserves_transition_and_coverage(monkeypatch, tmp_path):
    store = Store(tmp_path / "identity-prices.duckdb")
    try:
        _setup_security(store)
        _install_reference_rows(store)
        store.upsert_fundamentals([_fundamental(100, issuer_id=ISSUER_ID, security_id=SECURITY_ID)])

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
        factor_rows = store.pit_factor_price_history("NEW", "2024-07-01", observations=2)
        assert [row["ticker"] for row in factor_rows] == ["OLD", "NEW"]
        assert store.price_action_refresh_candidates("yfinance", "2024-01-01", "2025-01-01") == [
            SECURITY_ID
        ]
        assert (
            store.unverified_price_action_count(SECURITY_ID, "yfinance", "2024-01-01", "2025-01-01")
            == 2
        )
        coverage = store.universe_data_coverage("demo", "2024-07-01")[0]
        assert coverage["issuer_id"] == ISSUER_ID
        assert coverage["has_price_history"] is True
        assert coverage["has_pit_fundamentals"] is True
        report = {row["check"]: row for row in store.data_quality_report()}
        assert report["tagged_prices_outside_provider_provenance"]["status"] == "ok"
        assert report["tagged_prices_wrong_dated_ticker"]["status"] == "ok"
    finally:
        store.close()


def test_terminal_provider_mapping_cannot_reactivate_legacy_price_rows(tmp_path):
    store = Store(tmp_path / "terminal-provider-mapping.duckdb")
    try:
        _setup_security(store)
        store.upsert_prices([{"ticker": "OLD", "date": "2024-06-28", "close": 99.0}])

        assert store.latest_price("OLD", "2024-06-30")["close"] == 99.0

        issuers, ciks, owners, providers = _reference_rows()
        providers[0]["mapping_status"] = "blocked_wrong_security"
        store.upsert_reference_identities(issuers, ciks, owners, providers)

        assert store.latest_price("OLD", "2024-06-30") is None
        assert store.pit_factor_price_history("OLD", "2024-06-30", observations=2) == []
        coverage = store.universe_data_coverage("demo", "2024-06-30")[0]
        assert coverage["has_price_history"] is False
        assert coverage["latest_price_date"] is None
    finally:
        store.close()


def test_ingest_issuer_uses_reviewed_cik_and_tags_rows(monkeypatch, tmp_path):
    store = Store(tmp_path / "issuer-ingest.duckdb")
    try:
        _setup_security(store)
        _install_reference_rows(store)
        store.upsert_fundamentals(
            [
                _fundamental(
                    50,
                    ticker="STALE",
                    issuer_id=ISSUER_ID,
                    security_id=SECURITY_ID,
                )
            ]
        )

        def fake_extract(
            ticker,
            cik,
            *,
            issuer_id,
            security_id,
            snapshot_store,
            ingest_run_id,
        ):
            assert (ticker, cik) == ("NEW", 1)
            assert issuer_id == ISSUER_ID
            assert security_id == SECURITY_ID
            assert snapshot_store is store
            assert isinstance(ingest_run_id, str) and ingest_run_id
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
        rows = store.query(
            "SELECT ticker, issuer_id, security_id FROM fundamentals ORDER BY ticker"
        )
        assert rows == [
            {
                "ticker": "NEW",
                "issuer_id": ISSUER_ID,
                "security_id": SECURITY_ID,
            }
        ]
    finally:
        store.close()

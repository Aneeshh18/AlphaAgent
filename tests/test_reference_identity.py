from __future__ import annotations

import gzip
import json
import sys
from datetime import UTC, date, datetime
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
from aios.raw_snapshots import canonical_parsed_rows_sha256, verify_raw_snapshots
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
                   parsed_rows_sha256, payload_sha256, parser_version
            FROM raw_snapshots
            """
        )[0]
        assert snapshot["artifact_kind"] == "normalized_provider_export"
        assert snapshot["parser_version"] == prices.YFINANCE_PARSER_VERSION
        assert snapshot["parsed_row_count"] == 1
        assert snapshot["parsed_rows_sha256"]
        assert store.query("SELECT run_id, role FROM ingest_raw_snapshots") == [
            {"run_id": "price-run", "role": "prices:TEST"}
        ]
        payload_record = store.raw_payload_record(snapshot["payload_sha256"])
        payload = gzip.decompress((tmp_path / payload_record["relative_path"]).read_bytes())
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


@pytest.mark.parametrize("bad_close", [float("nan"), float("inf"), 0.0, -1.0])
def test_yfinance_rejects_non_eligible_completed_close(
    monkeypatch,
    bad_close,
) -> None:
    def fake_download(*_args, **_kwargs):
        return pd.DataFrame(
            {
                "Open": [100.0],
                "High": [101.0],
                "Low": [99.0],
                "Close": [bad_close],
                "Adj Close": [100.0],
                "Volume": [1000],
                "Dividends": [0.0],
                "Stock Splits": [0.0],
            },
            index=[pd.Timestamp("2024-01-02")],
        )

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=fake_download))

    with pytest.raises(
        ValueError,
        match=r"yfinance TEST 2024-01-02 Close must be positive",
    ):
        prices.fetch_yfinance("TEST", start="2024-01-01", end="2024-01-03")


@pytest.mark.parametrize(
    ("open_value", "high_value", "low_value", "match"),
    [
        (None, 101.0, 99.0, "Open must be positive"),
        (100.0, 98.0, 99.0, "High is below another OHLC value"),
        (100.0, 101.0, 101.0, "Low is above another OHLC value"),
    ],
)
def test_yfinance_rejects_invalid_completed_ohlc(
    monkeypatch,
    open_value,
    high_value,
    low_value,
    match,
) -> None:
    def fake_download(*_args, **_kwargs):
        return pd.DataFrame(
            {
                "Open": [open_value],
                "High": [high_value],
                "Low": [low_value],
                "Close": [100.0],
                "Adj Close": [100.0],
                "Volume": [1000],
                "Dividends": [0.0],
                "Stock Splits": [0.0],
            },
            index=[pd.Timestamp("2024-01-02")],
        )

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=fake_download))

    with pytest.raises(ValueError, match=match):
        prices.fetch_yfinance("TEST", start="2024-01-01", end="2024-01-03")


@pytest.mark.parametrize(
    ("field", "bad_value", "match"),
    [
        ("Adj Close", None, "Adj Close must be positive"),
        ("Dividends", None, "Dividends must be non-negative"),
        ("Dividends", -0.01, "Dividends must be non-negative"),
        ("Stock Splits", None, "Stock Splits must be non-negative"),
        ("Stock Splits", -1.0, "Stock Splits must be non-negative"),
    ],
)
def test_yfinance_rejects_incomplete_or_invalid_action_evidence(
    monkeypatch,
    field,
    bad_value,
    match,
) -> None:
    frame = {
        "Open": [100.0],
        "High": [101.0],
        "Low": [99.0],
        "Close": [100.0],
        "Adj Close": [100.0],
        "Volume": [1000],
        "Dividends": [0.0],
        "Stock Splits": [0.0],
    }
    frame[field] = [bad_value]

    monkeypatch.setitem(
        sys.modules,
        "yfinance",
        SimpleNamespace(
            download=lambda *_args, **_kwargs: pd.DataFrame(
                frame,
                index=[pd.Timestamp("2024-01-02")],
            )
        ),
    )

    with pytest.raises(ValueError, match=match):
        prices.fetch_yfinance("TEST", start="2024-01-01", end="2024-01-03")


def test_yfinance_captures_and_replays_malformed_close_before_rejecting(
    monkeypatch,
    tmp_path,
) -> None:
    def fake_download(*_args, **_kwargs):
        return pd.DataFrame(
            {
                "Open": [100.0],
                "High": [101.0],
                "Low": [99.0],
                "Close": [float("nan")],
                "Adj Close": [100.0],
                "Volume": [1000],
                "Dividends": [0.0],
                "Stock Splits": [0.0],
            },
            index=[pd.Timestamp("2024-01-02")],
        )

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=fake_download))
    monkeypatch.setattr(
        prices,
        "latest_completed_us_equity_session",
        lambda: date(2024, 1, 5),
    )
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        with pytest.raises(ValueError, match="Close must be positive"):
            prices.fetch_yfinance(
                "TEST",
                start="2024-01-01",
                end="2024-01-03",
                store=store,
                ingest_run_id="price-run",
                project_root=tmp_path,
            )

        snapshot = store.query(
            """
            SELECT parsed_row_count, parsed_rows_sha256, payload_sha256
            FROM raw_snapshots
            """
        )[0]
        assert snapshot["parsed_row_count"] == 1
        assert snapshot["parsed_rows_sha256"]
        payload_record = store.raw_payload_record(snapshot["payload_sha256"])
        payload = gzip.decompress((tmp_path / payload_record["relative_path"]).read_bytes())
        replayed_rows = prices.parse_yfinance_normalized_export(payload)
        assert replayed_rows[0]["close"] is None
        assert (
            verify_raw_snapshots(
                store=store,
                project_root=tmp_path,
            ).replayed_snapshots
            == 1
        )
    finally:
        store.close()


def test_yfinance_v2_captures_negative_split_before_rejecting(
    monkeypatch,
    tmp_path,
) -> None:
    def fake_download(*_args, **_kwargs):
        return pd.DataFrame(
            {
                "Open": [100.0],
                "High": [101.0],
                "Low": [99.0],
                "Close": [100.0],
                "Adj Close": [100.0],
                "Volume": [1000],
                "Dividends": [0.0],
                "Stock Splits": [-1.0],
            },
            index=[pd.Timestamp("2024-01-02")],
        )

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=fake_download))
    monkeypatch.setattr(
        prices,
        "latest_completed_us_equity_session",
        lambda: date(2024, 1, 5),
    )
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        with pytest.raises(ValueError, match="Stock Splits must be non-negative"):
            prices.fetch_yfinance(
                "TEST",
                start="2024-01-01",
                end="2024-01-03",
                store=store,
                ingest_run_id="negative-split-run",
                project_root=tmp_path,
            )

        snapshot = store.query(
            """
            SELECT parser_version, payload_sha256
            FROM raw_snapshots
            """
        )[0]
        assert snapshot["parser_version"] == "yfinance-normalized-v2"
        payload_record = store.raw_payload_record(snapshot["payload_sha256"])
        payload = gzip.decompress((tmp_path / payload_record["relative_path"]).read_bytes())
        assert prices.parse_yfinance_normalized_export(payload)[0]["split_ratio"] == -1.0
        with pytest.raises(ValueError, match="invalid yfinance split ratio"):
            prices.parse_yfinance_normalized_export_v1(payload)
        assert (
            verify_raw_snapshots(
                store=store,
                project_root=tmp_path,
            ).replayed_snapshots
            == 1
        )
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


def test_issuer_owner_resolution_never_uses_a_future_review_date(tmp_path) -> None:
    store = Store(tmp_path / "issuer-owner-pit.duckdb")
    try:
        _setup_security(store)
        issuers, ciks, owners, providers = _reference_rows()
        owners[0] = {**owners[0], "verified_date": "2024-07-05"}
        store.upsert_reference_identities(issuers, ciks, owners, providers)

        assert store.issuer_id_for_security(SECURITY_ID, "2024-07-04") is None
        assert store.issuer_id_for_security(SECURITY_ID, "2024-07-05") == ISSUER_ID
        before = store.universe_identity_labels("demo", "2024-07-04")[0]
        after = store.universe_identity_labels("demo", "2024-07-05")[0]
        assert before["issuer_id"] is None
        assert after["issuer_id"] == ISSUER_ID
        before_coverage = store.universe_data_coverage("demo", "2024-07-04")[0]
        after_coverage = store.universe_data_coverage("demo", "2024-07-05")[0]
        assert before_coverage["issuer_id"] is None
        assert after_coverage["issuer_id"] == ISSUER_ID
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

    store = Store(db_path, allow_schema_upgrade=True)
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
        assert {
            "issuer_id",
            "security_id",
            "ingest_run_id",
            "source_snapshot_id",
            "source_rowset_sha256",
            "source_row_sha256",
        } <= fundamental_columns
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
        assert store.query(
            """
            SELECT ingest_run_id, source_snapshot_id, source_rowset_sha256,
                   source_row_sha256
            FROM fundamentals
            """
        )[0] == {
            "ingest_run_id": None,
            "source_snapshot_id": None,
            "source_rowset_sha256": None,
            "source_row_sha256": None,
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


def test_issuer_reference_resolves_only_the_effective_known_cik(tmp_path) -> None:
    store = Store(tmp_path / "issuer-reference-pit.duckdb")
    try:
        _setup_security(store)
        issuers, ciks, owners, providers = _reference_rows()
        ciks[0] = {
            **ciks[0],
            "effective_end": "2024-07-01",
            "verified_date": "2024-01-01",
        }
        ciks.append(
            {
                **ciks[0],
                "cik": "2",
                "effective_start": "2024-07-01",
                "effective_end": "2025-01-01",
                "verified_date": "2024-07-05",
            }
        )
        store.upsert_reference_identities(issuers, ciks, owners, providers)

        assert store.issuer_reference(ISSUER_ID, as_of="2024-06-30")["cik"] == ("0000000001")
        assert store.issuer_reference(ISSUER_ID, as_of="2024-07-02") is None
        assert store.issuer_reference(ISSUER_ID, as_of="2024-07-05")["cik"] == ("0000000002")
        assert store.issuer_reference(ISSUER_ID)["cik"] == "0000000002"
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


def test_factor_batch_reads_match_scalar_identity_and_restatement_policy(tmp_path):
    store = Store(tmp_path / "factor-batch-identity.duckdb")
    try:
        _setup_security(store)
        _install_reference_rows(store)
        first_filing = _fundamental(
            100,
            ticker="OLD",
            issuer_id=ISSUER_ID,
            security_id=SECURITY_ID,
        )
        restatement = {
            **first_filing,
            "as_of_date": "2024-06-01",
            "value": 110,
            "quarter_value": 110,
        }
        store.upsert_fundamentals([first_filing, restatement])
        store.upsert_prices(
            [
                {
                    "ticker": "OLD",
                    "security_id": SECURITY_ID,
                    "provider_symbol": "NEW",
                    "date": price_date,
                    "close": close,
                    "actions_complete": True,
                    "close_split_adjusted": False,
                    "source": "yfinance",
                }
                for price_date, close in [
                    ("2024-06-27", 99.0),
                    ("2024-06-28", 100.0),
                ]
            ]
        )

        scalar_fundamentals = store.pit_factor_fundamentals(
            "OLD",
            "2024-06-30",
            ["revenue"],
        )
        assert store.pit_factor_fundamentals_batch(
            ["OLD"],
            "2024-06-30",
            ["revenue"],
        ) == {"OLD": scalar_fundamentals}
        assert scalar_fundamentals[0]["value"] == 110

        scalar_latest = store.latest_price("OLD", "2024-06-30")
        batch_latest = store.pit_factor_latest_prices_batch(["OLD"], "2024-06-30")["OLD"]
        assert batch_latest is not None
        assert scalar_latest is not None
        assert {key: scalar_latest[key] for key in batch_latest} == batch_latest

        scalar_history = store.pit_factor_price_history(
            "OLD",
            "2024-06-30",
            observations=2,
        )
        assert store.pit_factor_price_histories_batch(
            ["OLD"],
            "2024-06-30",
            observations=2,
        ) == {"OLD": scalar_history}
    finally:
        store.close()


def test_factor_batch_never_uses_a_future_reviewed_owner(tmp_path) -> None:
    store = Store(tmp_path / "factor-batch-future-owner.duckdb")
    try:
        _setup_security(store)
        issuers, ciks, owners, providers = _reference_rows()
        owners[0] = {**owners[0], "verified_date": "2024-07-05"}
        store.upsert_reference_identities(issuers, ciks, owners, providers)
        store.upsert_fundamentals(
            [
                _fundamental(
                    100,
                    ticker="OLD",
                    issuer_id=ISSUER_ID,
                    security_id=SECURITY_ID,
                )
            ]
        )

        assert (
            store.pit_factor_fundamentals(
                "OLD",
                "2024-07-04",
                ["revenue"],
            )
            == []
        )
        assert store.pit_factor_fundamentals_batch(
            ["OLD"],
            "2024-07-04",
            ["revenue"],
        ) == {"OLD": []}

        scalar = store.pit_factor_fundamentals(
            "OLD",
            "2024-07-05",
            ["revenue"],
        )
        assert store.pit_factor_fundamentals_batch(
            ["OLD"],
            "2024-07-05",
            ["revenue"],
        ) == {"OLD": scalar}
        assert scalar[0]["value"] == 100
    finally:
        store.close()


def test_factor_batch_matches_scalar_same_day_tie_result(tmp_path):
    store = Store(tmp_path / "factor-batch-same-day-tie.duckdb")
    try:
        _setup_security(store)
        _install_reference_rows(store)
        store.upsert_fundamentals(
            [
                _fundamental(
                    111,
                    ticker="OLD",
                    issuer_id=ISSUER_ID,
                    security_id=SECURITY_ID,
                ),
                _fundamental(
                    222,
                    ticker="NEW",
                    issuer_id=ISSUER_ID,
                    security_id=SECURITY_ID,
                ),
            ]
        )

        scalar = store.pit_factor_fundamentals(
            "OLD",
            "2024-06-30",
            ["revenue"],
        )
        batch = store.pit_factor_fundamentals_batch(
            ["OLD"],
            "2024-06-30",
            ["revenue"],
        )

        assert batch == {"OLD": scalar}
    finally:
        store.close()


def test_factor_batch_preserves_ambiguous_security_failure(tmp_path):
    store = Store(tmp_path / "factor-batch-ambiguous-security.duckdb")
    try:
        _setup_security(store)
        store.execute(
            """
            INSERT INTO security_master
            (security_id, canonical_ticker, identity_status, source)
            VALUES ('aios:security:other', 'OLD', 'bounded_unverified', 'test')
            """
        )
        store.execute(
            """
            INSERT INTO security_identity_assignments
            (universe_id, ticker, effective_start, effective_end, security_id,
             known_date, identity_status, source)
            VALUES
            ('other', 'OLD', '2024-01-01', '2025-01-01',
             'aios:security:other', '2023-12-15', 'bounded_unverified', 'test')
            """
        )

        with pytest.raises(ValueError, match="ambiguous security identity"):
            store.pit_factor_fundamentals("OLD", "2024-06-30", ["revenue"])
        with pytest.raises(ValueError, match="ambiguous security identity"):
            store.pit_factor_fundamentals_batch(
                ["OLD"],
                "2024-06-30",
                ["revenue"],
            )
    finally:
        store.close()


def test_inactive_historical_ticker_reuse_is_ambiguous_only_for_fundamentals(
    tmp_path,
) -> None:
    store = Store(tmp_path / "factor-batch-historical-ticker-reuse.duckdb")
    try:
        _setup_security(store)
        store.execute(
            """
            INSERT INTO security_master
            (security_id, canonical_ticker, identity_status, source)
            VALUES ('aios:security:former-old', 'OLD', 'bounded_unverified', 'test')
            """
        )
        store.execute(
            """
            INSERT INTO security_identity_assignments
            (universe_id, ticker, effective_start, effective_end, security_id,
             known_date, identity_status, source)
            VALUES
            ('historical', 'OLD', '2023-01-01', '2023-07-01',
             'aios:security:former-old', '2022-12-15',
             'bounded_unverified', 'test')
            """
        )
        store.upsert_prices(
            [
                {
                    "ticker": "OLD",
                    "date": price_date,
                    "close": close,
                }
                for price_date, close in [
                    ("2024-06-27", 9.0),
                    ("2024-06-28", 10.0),
                ]
            ]
        )

        with pytest.raises(ValueError, match="ambiguous security identity"):
            store.pit_factor_fundamentals("OLD", "2024-07-05", ["revenue"])
        with pytest.raises(ValueError, match="ambiguous security identity"):
            store.pit_factor_fundamentals_batch(
                ["OLD"],
                "2024-07-05",
                ["revenue"],
            )
        scalar_latest = store.latest_price("OLD", "2024-07-05")
        batch_latest = store.pit_factor_latest_prices_batch(
            ["OLD"],
            "2024-07-05",
        )["OLD"]
        assert scalar_latest is not None
        assert batch_latest is not None
        assert batch_latest["close"] == scalar_latest["close"] == 10.0

        scalar_history = store.pit_factor_price_history(
            "OLD",
            "2024-07-05",
            observations=2,
        )
        assert store.pit_factor_price_histories_batch(
            ["OLD"],
            "2024-07-05",
            observations=2,
        ) == {"OLD": scalar_history}
    finally:
        store.close()


def test_price_routes_never_use_a_future_verified_provider_mapping(
    tmp_path,
) -> None:
    store = Store(tmp_path / "factor-price-future-provider-review.duckdb")
    try:
        _setup_security(store)
        issuers, ciks, owners, providers = _reference_rows()
        providers[0] = {**providers[0], "verified_date": "2024-07-05"}
        store.upsert_reference_identities(issuers, ciks, owners, providers)
        store.upsert_prices(
            [
                {
                    "ticker": "NEW",
                    "security_id": SECURITY_ID,
                    "provider_symbol": "NEW",
                    "date": price_date,
                    "close": close,
                    "actions_complete": True,
                    "close_split_adjusted": False,
                    "source": "yfinance",
                }
                for price_date, close in [
                    ("2024-07-01", 100.0),
                    ("2024-07-02", 101.0),
                ]
            ]
        )

        assert store.latest_price("NEW", "2024-07-02") is None
        assert store.pit_factor_latest_prices_batch(
            ["NEW"],
            "2024-07-02",
        ) == {"NEW": None}
        assert (
            store.pit_factor_price_history(
                "NEW",
                "2024-07-02",
                observations=2,
            )
            == []
        )
        assert store.pit_factor_price_histories_batch(
            ["NEW"],
            "2024-07-02",
            observations=2,
        ) == {"NEW": []}
        before = store.universe_data_coverage("demo", "2024-07-02")[0]
        assert before["has_price_history"] is False
        assert before["latest_price_date"] is None

        scalar_latest = store.latest_price("NEW", "2024-07-05")
        batch_latest = store.pit_factor_latest_prices_batch(
            ["NEW"],
            "2024-07-05",
        )["NEW"]
        assert scalar_latest is not None
        assert batch_latest is not None
        assert batch_latest["close"] == scalar_latest["close"] == 101.0
        scalar_history = store.pit_factor_price_history(
            "NEW",
            "2024-07-05",
            observations=2,
        )
        assert store.pit_factor_price_histories_batch(
            ["NEW"],
            "2024-07-05",
            observations=2,
        ) == {"NEW": scalar_history}
        after = store.universe_data_coverage("demo", "2024-07-05")[0]
        assert after["has_price_history"] is True
        assert str(after["latest_price_date"]) == "2024-07-02"
    finally:
        store.close()


def test_factor_batch_preserves_ambiguous_issuer_failure(tmp_path):
    store = Store(tmp_path / "factor-batch-ambiguous-issuer.duckdb")
    try:
        _setup_security(store)
        _install_reference_rows(store)
        store.execute(
            """
            INSERT INTO issuer_master
            (issuer_id, canonical_name, canonical_ticker, source)
            VALUES ('aios:issuer:other', 'Other Corporation', 'OLD', 'test')
            """
        )
        store.execute(
            """
            INSERT INTO security_issuer_assignments
            (security_id, issuer_id, effective_start, effective_end,
             verified_date, source)
            VALUES
            (?, 'aios:issuer:other', '2024-02-01', '2024-12-01',
             '2024-02-01', 'test')
            """,
            (SECURITY_ID,),
        )

        with pytest.raises(ValueError, match="ambiguous issuer identity"):
            store.pit_factor_fundamentals("OLD", "2024-06-30", ["revenue"])
        with pytest.raises(ValueError, match="ambiguous issuer identity"):
            store.pit_factor_fundamentals_batch(
                ["OLD"],
                "2024-06-30",
                ["revenue"],
            )
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
        assert store.pit_factor_fundamentals_batch(
            ["OLD"],
            "2024-06-30",
            ["revenue"],
        ) == {"OLD": []}
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
        def get_bytes(self, url, headers=None):
            assert "token=" not in url
            assert headers == {"Authorization": "Token test-token"}
            return json.dumps(
                [
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
                        "open": 11,
                        "high": 13,
                        "low": 10,
                        "close": 12,
                        "adjClose": 11.5,
                        "volume": 1100,
                        "divCash": 0,
                        "splitFactor": 1,
                    },
                ]
            ).encode()

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


def test_stooq_symbol_uses_us_suffix_without_corrupting_share_classes():
    assert prices._stooq_symbol("AAPL") == "aapl.us"
    assert prices._stooq_symbol("BRK.B") == "brk-b.us"
    assert prices._stooq_symbol("AAPL.US") == "aapl.us"


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


def test_identity_price_ingest_records_invalid_close_as_failed_without_writes(
    monkeypatch,
    tmp_path,
):
    store = Store(tmp_path / "invalid-identity-price.duckdb")
    try:
        _setup_security(store)
        _install_reference_rows(store)
        monkeypatch.setattr(
            prices,
            "fetch_provider_prices",
            lambda *_args, **_kwargs: [
                {
                    "ticker": "NEW",
                    "date": "2024-07-01",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": None,
                }
            ],
        )

        with pytest.raises(ValueError, match="price close must be positive and finite"):
            prices.ingest_security_prices(
                SECURITY_ID,
                provider="yfinance",
                start="2024-01-01",
                end="2025-01-01",
                store=store,
            )

        assert store.query("SELECT COUNT(*) AS n FROM prices")[0]["n"] == 0
        latest = store.ingest_history(1)[0]
        assert latest["status"] == "failed"
        assert latest["rows_inserted"] == 0
        assert "price close must be positive and finite" in latest["error"]
    finally:
        store.close()


def test_legacy_price_ingest_records_empty_provider_result_as_warning(
    monkeypatch,
    tmp_path,
):
    store = Store(tmp_path / "empty-price-ingest.duckdb")
    try:
        monkeypatch.setattr(prices, "fetch_prices", lambda *_args, **_kwargs: [])

        assert prices.ingest_prices("TEST", store=store) == 0

        latest = store.ingest_history(1)[0]
        assert latest["status"] == "warning"
        assert latest["rows_inserted"] == 0
        assert latest["error"] == "provider returned no usable price rows"
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
        assert store.pit_factor_latest_prices_batch(["OLD"], "2024-06-30") == {"OLD": None}
        assert store.pit_factor_price_histories_batch(
            ["OLD"],
            "2024-06-30",
            observations=2,
        ) == {"OLD": []}
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
            observed_at = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
            row = _fundamental(
                123,
                issuer_id=issuer_id,
                security_id=security_id,
            )
            row.update(
                {
                    "source": "edgar",
                    "ingest_run_id": ingest_run_id,
                    "source_snapshot_id": "reference-companyfacts",
                }
            )
            provider_row = {"cik": "0000000001", **row}
            provider_row = {
                key: provider_row[key]
                for key in (
                    "cik",
                    "period_end",
                    "as_of_date",
                    "fiscal_period",
                    "statement",
                    "metric",
                    "value",
                    "quarter_value",
                    "unit",
                    "source",
                )
            }
            rowset_sha256 = canonical_parsed_rows_sha256([provider_row])
            row["source_rowset_sha256"] = rowset_sha256
            row["source_row_sha256"] = edgar.canonical_sec_fundamental_row_sha256(provider_row)
            submissions_row = {
                "cik": "0000000001",
                "name": "Demo Corporation",
                "sic": None,
                "sic_description": None,
                "exchanges": [],
            }
            submissions_rowset_sha256 = canonical_parsed_rows_sha256([submissions_row])
            snapshot_store.record_raw_snapshot(
                payload={
                    "payload_sha256": "e" * 64,
                    "relative_path": "data/raw/sec/companyfacts/reference.json.gz",
                    "original_bytes": 120,
                    "stored_bytes": 80,
                    "compression": "gzip",
                },
                snapshot={
                    "snapshot_id": "reference-companyfacts",
                    "provider": "sec-edgar",
                    "dataset": "companyfacts",
                    "artifact_kind": "exact_response",
                    "requested_at": observed_at,
                    "received_at": observed_at,
                    "http_status": 200,
                    "content_type": "application/json",
                    "request_fingerprint": "f" * 64,
                    "payload_sha256": "e" * 64,
                    "adapter_name": "edgar",
                    "adapter_version": "v1",
                    "parser_version": "sec-companyfacts-v2",
                    "parsed_row_count": 1,
                    "parsed_rows_sha256": rowset_sha256,
                    "parsed_rows_rejected": 0,
                    "parsed_rejection_codes": None,
                },
                ingest_run_id=ingest_run_id,
                role="companyfacts",
            )
            snapshot_store.record_raw_snapshot(
                payload={
                    "payload_sha256": "d" * 64,
                    "relative_path": "data/raw/sec/submissions/reference.json.gz",
                    "original_bytes": 60,
                    "stored_bytes": 40,
                    "compression": "gzip",
                },
                snapshot={
                    "snapshot_id": "reference-submissions",
                    "provider": "sec-edgar",
                    "dataset": "submissions",
                    "artifact_kind": "exact_response",
                    "requested_at": observed_at,
                    "received_at": observed_at,
                    "http_status": 200,
                    "content_type": "application/json",
                    "request_fingerprint": "c" * 64,
                    "payload_sha256": "d" * 64,
                    "adapter_name": "edgar",
                    "adapter_version": "v1",
                    "parser_version": "sec-submissions-v2",
                    "parsed_row_count": 1,
                    "parsed_rows_sha256": submissions_rowset_sha256,
                },
                ingest_run_id=ingest_run_id,
                role="submissions",
            )
            return (
                [row],
                {
                    "name": "Demo Corporation",
                    "submissions_row": submissions_row,
                },
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
        evidence = store.ingest_evidence(store.ingest_history(1)[0]["run_id"])
        assert evidence is not None
        assert evidence["subject_type"] == "issuer"
        assert evidence["subject_id"] == ISSUER_ID
    finally:
        store.close()

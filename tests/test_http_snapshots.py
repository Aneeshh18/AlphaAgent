from __future__ import annotations

import gzip
import json
from datetime import date

import httpx
import pytest

from aios.ingest import edgar, fred, prices
from aios.ingest.http_client import HttpClient, RawSnapshotContext, _secret_free_url
from aios.raw_snapshots import verify_raw_snapshots
from aios.storage.store import Store


def _client(response_body: bytes, content_type: str = "application/json") -> HttpClient:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=response_body,
            headers={"content-type": content_type},
            request=request,
        )

    client = HttpClient()
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(respond))
    return client


def _context(tmp_path, store, run_id="run-1") -> RawSnapshotContext:
    return RawSnapshotContext(
        provider="sec-edgar",
        dataset="companyfacts",
        store=store,
        ingest_run_id=run_id,
        role="companyfacts",
        adapter_name="test-http",
        adapter_version="1",
        parser_version="test-parser-v1",
        project_root=tmp_path,
    )


def test_http_json_capture_keeps_exact_bytes_and_ingest_link(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    client = _client(b'{"cik":1,"facts":{}}')
    try:
        payload = client.get_json(
            "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
            raw_snapshot=_context(tmp_path, store),
        )

        assert payload["cik"] == 1
        snapshot = store.query(
            """
            SELECT snapshot_id, provider, dataset, artifact_kind, payload_sha256,
                   parser_version, parsed_row_count
            FROM raw_snapshots
            """
        )[0]
        assert snapshot["provider"] == "sec-edgar"
        assert snapshot["dataset"] == "companyfacts"
        assert snapshot["artifact_kind"] == "exact_response"
        assert snapshot["parser_version"] == "test-parser-v1"
        assert snapshot["parsed_row_count"] is None
        assert store.query(
            "SELECT run_id, snapshot_id, role FROM ingest_raw_snapshots"
        ) == [
            {
                "run_id": "run-1",
                "snapshot_id": snapshot["snapshot_id"],
                "role": "companyfacts",
            }
        ]
        raw = store.raw_payload_record(snapshot["payload_sha256"])
        assert raw is not None
        assert gzip.decompress((tmp_path / raw["relative_path"]).read_bytes()) == (
            b'{"cik":1,"facts":{}}'
        )
    finally:
        client.close()
        store.close()


def test_malformed_json_is_captured_before_parse_failure(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    client = _client(b"not-json")
    try:
        with pytest.raises(ValueError):
            client.get_json(
                "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
                raw_snapshot=_context(tmp_path, store),
            )

        assert store.query("SELECT COUNT(*) AS n FROM raw_snapshots")[0]["n"] == 1
        assert store.query("SELECT COUNT(*) AS n FROM raw_payloads")[0]["n"] == 1
    finally:
        client.close()
        store.close()


def test_request_description_redacts_secret_query_values_and_fragments() -> None:
    safe = _secret_free_url(
        "https://example.test/data?series=GDP&api_key=super-secret#local-fragment"
    )

    assert "series=GDP" in safe
    assert "super-secret" not in safe
    assert "local-fragment" not in safe
    assert "%3Credacted%3E" in safe


def test_sec_companyfacts_fetch_passes_reviewed_snapshot_context(monkeypatch, tmp_path) -> None:
    captured = {}

    class FakeHttp:
        def get_json(self, url, *, raw_snapshot):
            captured.update(url=url, context=raw_snapshot)
            return {"cik": 1, "facts": {}}

    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        monkeypatch.setattr(edgar, "get_http", lambda: FakeHttp())
        payload = edgar.fetch_facts(1, store=store, ingest_run_id="issuer-run")

        assert payload["cik"] == 1
        assert captured["context"].provider == "sec-edgar"
        assert captured["context"].dataset == "companyfacts"
        assert captured["context"].ingest_run_id == "issuer-run"
    finally:
        store.close()


def test_sec_issuer_responses_attach_canonical_replay_evidence(
    monkeypatch,
    tmp_path,
) -> None:
    facts_bytes = json.dumps(
        {
            "cik": 1,
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {
                            "USD": [
                                {
                                    "start": "2024-01-01",
                                    "end": "2024-03-31",
                                    "filed": "2024-05-01",
                                    "fp": "Q1",
                                    "fy": 2024,
                                    "accn": "0000000001-24-000001",
                                    "val": 100,
                                }
                            ]
                        }
                    }
                }
            },
        },
        separators=(",", ":"),
    ).encode()
    submissions_bytes = json.dumps(
        {
            "cik": "0000000001",
            "name": "Test Corporation",
            "sic": "1234",
            "sicDescription": "Test industry",
            "exchanges": ["NYSE"],
        },
        separators=(",", ":"),
    ).encode()

    def respond(request: httpx.Request) -> httpx.Response:
        body = submissions_bytes if "/submissions/" in str(request.url) else facts_bytes
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/json"},
            request=request,
        )

    client = HttpClient()
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(respond))
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        monkeypatch.setattr(edgar, "get_http", lambda: client)
        rows, meta = edgar.extract_fundamentals(
            "TEST",
            1,
            issuer_id="issuer-1",
            security_id="security-1",
            snapshot_store=store,
            ingest_run_id="issuer-run",
            snapshot_project_root=tmp_path,
        )

        assert len(rows) == 1
        assert rows[0]["metric"] == "revenue"
        assert rows[0]["as_of_date"] == "2024-05-01"
        assert meta["name"] == "Test Corporation"
        assert meta["exchange"] == "NYSE"
        snapshots = store.query(
            """
            SELECT dataset, parser_version, parsed_row_count, parsed_rows_sha256
            FROM raw_snapshots ORDER BY dataset
            """
        )
        assert [
            (row["dataset"], row["parser_version"], row["parsed_row_count"])
            for row in snapshots
        ] == [
            ("companyfacts", edgar.COMPANYFACTS_PARSER_VERSION, 1),
            ("submissions", edgar.SUBMISSIONS_PARSER_VERSION, 1),
        ]
        assert all(row["parsed_rows_sha256"] for row in snapshots)
        links = store.query(
            """
            SELECT role, COUNT(*) AS snapshots
            FROM ingest_raw_snapshots
            WHERE run_id = ?
            GROUP BY role ORDER BY role
            """,
            ("issuer-run",),
        )
        assert links == [
            {"role": "companyfacts", "snapshots": 1},
            {"role": "submissions", "snapshots": 1},
        ]
        assert (
            verify_raw_snapshots(
                store=store,
                project_root=tmp_path,
            ).replayed_snapshots
            == 2
        )
    finally:
        client.close()
        store.close()


def test_sec_companyfacts_identity_failure_keeps_byte_only_capture(
    monkeypatch,
    tmp_path,
) -> None:
    client = _client(b'{"cik":2,"facts":{}}')
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        monkeypatch.setattr(edgar, "get_http", lambda: client)
        with pytest.raises(ValueError, match="does not match reviewed CIK"):
            edgar.extract_fundamentals(
                "TEST",
                1,
                snapshot_store=store,
                ingest_run_id="issuer-run",
                snapshot_project_root=tmp_path,
            )

        snapshot = store.query(
            """
            SELECT parser_version, parsed_row_count, parsed_rows_sha256
            FROM raw_snapshots
            """
        )[0]
        assert snapshot == {
            "parser_version": edgar.COMPANYFACTS_CAPTURE_PARSER_VERSION,
            "parsed_row_count": None,
            "parsed_rows_sha256": None,
        }
        assert (
            verify_raw_snapshots(
                store=store,
                project_root=tmp_path,
            ).replayed_snapshots
            == 0
        )
    finally:
        client.close()
        store.close()


def test_treasury_fetch_passes_snapshot_context_and_parses_rows(monkeypatch, tmp_path) -> None:
    payload = (
        b"Date,2 Yr,10 Yr,30 Yr\n"
        b"07/25/2026,4.0,4.1,4.2\n"
        b"07/24/2026,4.1,4.2,4.3\n"
    )
    client = _client(payload, "text/csv")
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        monkeypatch.setattr(fred, "get_http", lambda: client)
        monkeypatch.setattr(
            fred,
            "latest_completed_us_equity_session",
            lambda: date(2026, 7, 24),
        )
        rows = fred.fetch_treasury_yield_curve(
            as_of="2026-07-25",
            store=store,
            ingest_run_id="macro-run",
            project_root=tmp_path,
        )

        assert [row["series_id"] for row in rows] == ["DGS2", "DGS10", "DGS30"]
        assert {row["date"] for row in rows} == {"2026-07-24"}
        snapshot = store.query(
            """
            SELECT provider, dataset, parser_version, parsed_row_count,
                   parsed_rows_sha256
            FROM raw_snapshots
            """
        )[0]
        assert snapshot["provider"] == "us-treasury"
        assert snapshot["dataset"] == "daily-yield-curve"
        assert snapshot["parser_version"] == fred.TREASURY_PARSER_VERSION
        # The immutable evidence covers all provider rows, including a row
        # excluded from the still-open decision boundary.
        assert snapshot["parsed_row_count"] == 6
        assert snapshot["parsed_rows_sha256"]
        assert store.query(
            "SELECT run_id, role FROM ingest_raw_snapshots"
        ) == [{"run_id": "macro-run", "role": "treasury-yields:2026"}]
        assert (
            verify_raw_snapshots(
                store=store,
                project_root=tmp_path,
            ).replayed_snapshots
            == 1
        )
    finally:
        client.close()
        store.close()


def test_exact_stooq_and_tiingo_responses_are_replayable(
    monkeypatch,
    tmp_path,
) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    stooq_client = _client(
        (
            b"Date,Open,High,Low,Close,Volume\n"
            b"2026-07-23,10,12,9,11,1000\n"
            b"2026-07-24,11,13,10,12,1100\n"
        ),
        "text/csv",
    )
    tiingo_client = _client(
        json.dumps(
            [
                {
                    "date": "2026-07-24T00:00:00.000Z",
                    "open": 20,
                    "high": 22,
                    "low": 19,
                    "close": 21,
                    "adjClose": 20.5,
                    "volume": 2000,
                    "divCash": 0.1,
                    "splitFactor": 1,
                }
            ],
            separators=(",", ":"),
        ).encode()
    )
    try:
        monkeypatch.setattr(
            prices,
            "latest_completed_us_equity_session",
            lambda: date(2026, 7, 24),
        )
        monkeypatch.setattr(prices, "get_http", lambda: stooq_client)
        stooq_rows = prices.fetch_stooq(
            "TST",
            start="2026-07-24",
            end="2026-07-25",
            store=store,
            ingest_run_id="stooq-run",
            project_root=tmp_path,
        )
        assert [(row["date"], row["close"]) for row in stooq_rows] == [
            ("2026-07-24", 12.0)
        ]

        monkeypatch.setattr(
            prices,
            "settings",
            type("Settings", (), {"tiingo_api_key": "test-token"})(),
        )
        monkeypatch.setattr(prices, "get_http", lambda: tiingo_client)
        tiingo_rows = prices.fetch_tiingo(
            "TST",
            start="2026-07-24",
            end="2026-07-25",
            store=store,
            ingest_run_id="tiingo-run",
            project_root=tmp_path,
        )
        assert [(row["date"], row["close"]) for row in tiingo_rows] == [
            ("2026-07-24", 21.0)
        ]

        snapshots = store.query(
            """
            SELECT provider, parser_version, parsed_row_count, parsed_rows_sha256
            FROM raw_snapshots ORDER BY provider
            """
        )
        assert [
            (row["provider"], row["parser_version"], row["parsed_row_count"])
            for row in snapshots
        ] == [
            ("stooq", prices.STOOQ_PARSER_VERSION, 2),
            ("tiingo", prices.TIINGO_PARSER_VERSION, 1),
        ]
        assert all(row["parsed_rows_sha256"] for row in snapshots)
        assert (
            verify_raw_snapshots(
                store=store,
                project_root=tmp_path,
            ).replayed_snapshots
            == 2
        )
    finally:
        stooq_client.close()
        tiingo_client.close()
        store.close()


def test_stooq_html_challenge_is_retained_but_never_promoted(
    monkeypatch,
    tmp_path,
) -> None:
    client = _client(
        b"<!DOCTYPE html><html><body>JavaScript verification</body></html>",
        "text/html",
    )
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        monkeypatch.setattr(prices, "get_http", lambda: client)
        monkeypatch.setattr(
            prices,
            "latest_completed_us_equity_session",
            lambda: date(2026, 7, 24),
        )
        with pytest.raises(ValueError, match="missing columns"):
            prices.fetch_stooq(
                "AAPL",
                start="2026-07-23",
                end="2026-07-25",
                store=store,
                ingest_run_id="stooq-run",
                project_root=tmp_path,
            )

        snapshot = store.query(
            """
            SELECT parser_version, parsed_row_count, parsed_rows_sha256
            FROM raw_snapshots
            """
        )[0]
        assert snapshot == {
            "parser_version": prices.STOOQ_CAPTURE_PARSER_VERSION,
            "parsed_row_count": None,
            "parsed_rows_sha256": None,
        }
        assert (
            verify_raw_snapshots(
                store=store,
                project_root=tmp_path,
            ).replayed_snapshots
            == 0
        )
    finally:
        client.close()
        store.close()


def test_exact_price_parsers_reject_semantically_unusable_rows() -> None:
    with pytest.raises(ValueError, match="Close must be positive"):
        prices.parse_stooq_daily_csv(
            b"Date,Open,High,Low,Close,Volume\n2026-07-24,10,12,9,,1000\n"
        )

    with pytest.raises(ValueError, match="High is below another OHLC value"):
        prices.parse_stooq_daily_csv(
            b"Date,Open,High,Low,Close,Volume\n2026-07-24,10,9,8,10,1000\n"
        )

    incomplete_actions = json.dumps(
        [
            {
                "date": "2026-07-24T00:00:00.000Z",
                "open": 20,
                "high": 22,
                "low": 19,
                "close": 21,
                "adjClose": 20.5,
                "volume": 2000,
                "divCash": 0,
            }
        ]
    ).encode()
    with pytest.raises(ValueError, match="splitFactor is required"):
        prices.parse_tiingo_eod_response(incomplete_actions)


def test_sec_ticker_map_attaches_identity_safe_replay_evidence(
    monkeypatch,
    tmp_path,
) -> None:
    payload = json.dumps(
        {
            "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
        },
        separators=(",", ":"),
    ).encode()
    client = _client(payload)
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        monkeypatch.setattr(edgar, "get_http", lambda: client)
        mapping = edgar.load_ticker_cik_map(
            store=store,
            ingest_run_id="ticker-run",
            project_root=tmp_path,
        )

        assert mapping["AAPL"] == 320193
        assert mapping["MSFT"] == 789019
        snapshot = store.query(
            """
            SELECT parser_version, parsed_row_count, parsed_rows_sha256
            FROM raw_snapshots
            """
        )[0]
        assert snapshot["parser_version"] == edgar.COMPANY_TICKERS_PARSER_VERSION
        assert snapshot["parsed_row_count"] == 2
        assert snapshot["parsed_rows_sha256"]
        assert (
            verify_raw_snapshots(
                store=store,
                project_root=tmp_path,
            ).replayed_snapshots
            == 1
        )
    finally:
        client.close()
        store.close()

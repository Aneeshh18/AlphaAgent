from __future__ import annotations

import gzip

import httpx
import pytest

from aios.ingest import edgar, fred
from aios.ingest.http_client import HttpClient, RawSnapshotContext, _secret_free_url
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


def test_treasury_fetch_passes_snapshot_context_and_parses_rows(monkeypatch, tmp_path) -> None:
    captured = {}

    class FakeHttp:
        def get_text(self, url, *, raw_snapshot):
            captured.update(url=url, context=raw_snapshot)
            return "Date,2 Yr,10 Yr,30 Yr\n2026-07-21,4.1,4.2,4.3\n"

    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        monkeypatch.setattr(fred, "get_http", lambda: FakeHttp())
        rows = fred.fetch_treasury_yield_curve(
            store=store,
            ingest_run_id="macro-run",
        )

        assert [row["series_id"] for row in rows] == ["DGS2", "DGS10", "DGS30"]
        assert captured["context"].provider == "us-treasury"
        assert captured["context"].dataset == "daily-yield-curve"
        assert captured["context"].ingest_run_id == "macro-run"
    finally:
        store.close()

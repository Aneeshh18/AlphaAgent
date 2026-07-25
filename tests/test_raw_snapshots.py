from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta

import pytest

from aios.raw_snapshots import (
    canonical_request_fingerprint,
    capture_raw_snapshot,
    verify_raw_snapshots,
)
from aios.storage.store import Store


def _capture(tmp_path, store, payload=b'{"value":1}', **overrides):
    requested = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    values = {
        "provider": "example",
        "dataset": "prices",
        "artifact_kind": "exact_response",
        "requested_at": requested,
        "received_at": requested + timedelta(seconds=1),
        "request_fingerprint": canonical_request_fingerprint(
            {"method": "GET", "url": "https://example.test/prices"}
        ),
        "adapter_name": "example-http",
        "adapter_version": "1",
        "parser_version": "1",
        "http_status": 200,
        "content_type": "application/json",
        "parsed_rows": [{"date": "2026-07-21", "close": 10.0}],
        "store": store,
        "project_root": tmp_path,
    }
    values.update(overrides)
    return capture_raw_snapshot(payload, **values)


def test_identical_payloads_share_one_file_but_keep_fetch_observations(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        first = _capture(tmp_path, store)
        second = _capture(tmp_path, store)
        third = _capture(tmp_path, store, provider="other", dataset="macro")

        assert first.payload_sha256 == second.payload_sha256
        assert first.relative_path == second.relative_path
        assert first.snapshot_id != second.snapshot_id
        assert third.relative_path == first.relative_path
        assert len(list((tmp_path / "data" / "raw").rglob("*.gz"))) == 1
        assert store.query("SELECT COUNT(*) AS n FROM raw_payloads")[0]["n"] == 1
        assert store.query("SELECT COUNT(*) AS n FROM raw_snapshots")[0]["n"] == 3
        assert verify_raw_snapshots(store=store, project_root=tmp_path).payloads == 1
    finally:
        store.close()


def test_changed_payload_creates_new_content_address(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        first = _capture(tmp_path, store, b"one", content_type="text/csv")
        second = _capture(tmp_path, store, b"two", content_type="text/csv")

        assert first.payload_sha256 != second.payload_sha256
        assert first.relative_path != second.relative_path
        assert verify_raw_snapshots(store=store, project_root=tmp_path).payloads == 2
    finally:
        store.close()


def test_snapshot_retains_payload_when_parsing_has_not_succeeded(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        result = _capture(tmp_path, store, parsed_rows=None)

        row = store.query(
            """
            SELECT parsed_row_count, parsed_rows_sha256
            FROM raw_snapshots WHERE snapshot_id = ?
            """,
            (result.snapshot_id,),
        )[0]
        assert row == {"parsed_row_count": None, "parsed_rows_sha256": None}
        assert gzip.decompress((tmp_path / result.relative_path).read_bytes()) == b'{"value":1}'
    finally:
        store.close()


def test_snapshot_links_to_an_ingest_run(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        run_id = store.record_ingest("example", "prices")
        result = _capture(tmp_path, store, ingest_run_id=run_id, role="response")

        assert store.query(
            "SELECT run_id, snapshot_id, role FROM ingest_raw_snapshots"
        ) == [{"run_id": run_id, "snapshot_id": result.snapshot_id, "role": "response"}]
    finally:
        store.close()


def test_snapshot_verification_detects_tampering(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        result = _capture(tmp_path, store)
        (tmp_path / result.relative_path).write_bytes(gzip.compress(b"changed", mtime=0))

        with pytest.raises(ValueError, match="stored size mismatch|checksum mismatch"):
            verify_raw_snapshots(store=store, project_root=tmp_path)
    finally:
        store.close()


def test_snapshot_rejects_unsafe_components_and_naive_timestamps(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        with pytest.raises(ValueError, match="safe path component"):
            _capture(tmp_path, store, provider="../escape")
        with pytest.raises(ValueError, match="timezone-aware"):
            _capture(
                tmp_path,
                store,
                requested_at=datetime(2026, 7, 22, 10, 0),
                received_at=datetime(2026, 7, 22, 10, 1),
            )
    finally:
        store.close()

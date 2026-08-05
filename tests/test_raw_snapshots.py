from __future__ import annotations

import gzip
import json
import os
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from aios import raw_snapshots as raw_snapshot_module
from aios.ingest import edgar
from aios.raw_snapshots import (
    attach_parsed_rows_evidence,
    canonical_request_fingerprint,
    capture_raw_snapshot,
    promote_legacy_sp500_change_snapshot,
    read_verified_raw_snapshot,
    verify_raw_snapshots,
)
from aios.storage.store import Store
from aios.universe_rollforward import (
    CHANGE_ANNOUNCEMENT_CAPTURE_VERSION,
    COMPONENT_SNAPSHOT_CAPTURE_VERSION,
    PRESS_ARCHIVE_CAPTURE_VERSION,
)


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
        "parsed_rows": None,
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

        assert store.query("SELECT run_id, snapshot_id, role FROM ingest_raw_snapshots") == [
            {"run_id": run_id, "snapshot_id": result.snapshot_id, "role": "response"}
        ]
    finally:
        store.close()


def test_verified_single_snapshot_reader_binds_metadata_and_bytes(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        payload = b'{"0":{"cik_str":1,"ticker":"AAA","title":"Alpha"}}'
        parsed_rows = [{"ticker": "AAA", "title": "Alpha", "cik": 1}]
        result = _capture(
            tmp_path,
            store,
            payload=payload,
            provider="sec-edgar",
            dataset="company-tickers",
            parser_version="sec-company-tickers-v2",
            parsed_rows=parsed_rows,
            ingest_run_id="staged-run",
            role="ticker-map",
        )

        verified = read_verified_raw_snapshot(
            store=store,
            expected_run_id="staged-run",
            expected_role="ticker-map",
            snapshot_id=result.snapshot_id,
            expected_provider="sec-edgar",
            expected_dataset="company-tickers",
            expected_artifact_kind="exact_response",
            expected_parser_version="sec-company-tickers-v2",
            expected_request_fingerprint=canonical_request_fingerprint(
                {"method": "GET", "url": "https://example.test/prices"}
            ),
            expected_adapter_name="example-http",
            expected_adapter_version="1",
            project_root=tmp_path,
        )

        assert verified.payload == payload
        assert verified.payload_sha256 == result.payload_sha256
        assert verified.metadata["snapshot_id"] == result.snapshot_id
        assert verified.metadata["http_status"] == 200
        assert verified.metadata["relative_path"] == result.relative_path

        staged = store.raw_snapshot_for_run_role("staged-run", "ticker-map")
        assert staged["snapshot_id"] == result.snapshot_id
        assert staged["parsed_rows_rejected"] is None
        assert staged["parsed_rejection_codes"] is None
        assert store.query(
            "SELECT COUNT(*) AS n FROM ingest_log WHERE run_id = 'staged-run'"
        ) == [{"n": 0}]
    finally:
        store.close()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("expected_provider", "other"),
        ("expected_dataset", "macro"),
        ("expected_artifact_kind", "normalized_provider_export"),
        ("expected_parser_version", "2"),
        ("expected_request_fingerprint", "f" * 64),
        ("expected_adapter_name", "other-http"),
        ("expected_adapter_version", "2"),
    ),
)
def test_verified_single_snapshot_reader_rejects_contract_mismatch(
    tmp_path,
    field,
    value,
) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        result = _capture(
            tmp_path,
            store,
            ingest_run_id="staged-run",
            role="source",
        )
        arguments = {
            "store": store,
            "expected_run_id": "staged-run",
            "expected_role": "source",
            "snapshot_id": result.snapshot_id,
            "expected_provider": "example",
            "expected_dataset": "prices",
            "expected_artifact_kind": "exact_response",
            "expected_parser_version": "1",
            "expected_request_fingerprint": canonical_request_fingerprint(
                {"method": "GET", "url": "https://example.test/prices"}
            ),
            "expected_adapter_name": "example-http",
            "expected_adapter_version": "1",
            "require_parsed_evidence": False,
            "project_root": tmp_path,
        }
        arguments[field] = value
        with pytest.raises(ValueError, match="mismatch"):
            read_verified_raw_snapshot(**arguments)
    finally:
        store.close()


def test_verified_single_snapshot_requires_reviewed_replay_by_default(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        result = _capture(tmp_path, store, ingest_run_id="staged-run", role="source")
        arguments = {
            "store": store,
            "expected_run_id": "staged-run",
            "expected_role": "source",
            "snapshot_id": result.snapshot_id,
            "expected_provider": "example",
            "expected_dataset": "prices",
            "expected_artifact_kind": "exact_response",
            "expected_parser_version": "1",
            "expected_request_fingerprint": canonical_request_fingerprint(
                {"method": "GET", "url": "https://example.test/prices"}
            ),
            "expected_adapter_name": "example-http",
            "expected_adapter_version": "1",
            "project_root": tmp_path,
        }

        with pytest.raises(ValueError, match="lacks reviewed parsed evidence"):
            read_verified_raw_snapshot(**arguments)
        checksum_only = read_verified_raw_snapshot(
            **arguments,
            require_parsed_evidence=False,
        )
        assert checksum_only.payload == b'{"value":1}'
    finally:
        store.close()


def test_raw_snapshot_run_role_lookup_rejects_ambiguous_staging(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        _capture(tmp_path, store, ingest_run_id="staged-run", role="component")
        _capture(tmp_path, store, ingest_run_id="staged-run", role="component")

        with pytest.raises(ValueError, match="resolved to 2"):
            store.raw_snapshot_for_run_role("staged-run", "component")
    finally:
        store.close()


def test_raw_snapshot_run_role_lookup_counts_dangling_links(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        _capture(tmp_path, store, ingest_run_id="staged-run", role="component")
        store.execute(
            """
            INSERT INTO ingest_raw_snapshots (run_id, snapshot_id, role)
            VALUES ('staged-run', 'raw-dangling', 'component')
            """
        )

        with pytest.raises(ValueError, match="resolved to 2"):
            store.raw_snapshot_for_run_role("staged-run", "component")
    finally:
        store.close()


def test_raw_snapshot_run_role_lookup_rejects_missing_payload_link(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        result = _capture(
            tmp_path,
            store,
            ingest_run_id="staged-run",
            role="component",
        )
        store.execute(
            "DELETE FROM raw_payloads WHERE payload_sha256 = ?",
            (result.payload_sha256,),
        )

        with pytest.raises(ValueError, match="incomplete snapshot or payload linkage"):
            store.raw_snapshot_for_run_role("staged-run", "component")
    finally:
        store.close()


def test_failed_sp500_parsers_remain_verifiable_capture_evidence(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        for position, (provider, dataset, parser_version) in enumerate(
            (
                ("spglobal", "sp500_press_archive", PRESS_ARCHIVE_CAPTURE_VERSION),
                (
                    "spglobal",
                    "sp500_change_announcement",
                    CHANGE_ANNOUNCEMENT_CAPTURE_VERSION,
                ),
                ("github", "sp500_current_components", COMPONENT_SNAPSHOT_CAPTURE_VERSION),
            )
        ):
            _capture(
                tmp_path,
                store,
                payload=f"malformed-{position}".encode(),
                provider=provider,
                dataset=dataset,
                parser_version=parser_version,
                content_type="text/plain",
            )

        verification = verify_raw_snapshots(store=store, project_root=tmp_path)
        assert verification.payloads == 3
        assert verification.replayed_snapshots == 0
    finally:
        store.close()


def test_legacy_sp500_change_snapshot_promotes_once_and_replays(tmp_path) -> None:
    payload = b"""
    <table>
      <tr><td>August 5, 2026</td><td>S&amp;P 500</td><td>Addition</td>
          <td>Ferguson Enterprises Inc.</td><td>FERG</td></tr>
      <tr><td>August 5, 2026</td><td>S&amp;P 500</td><td>Deletion</td>
          <td>Electronic Arts Inc.</td><td>EA</td></tr>
    </table>
    """
    fingerprint = canonical_request_fingerprint(
        {"method": "GET", "url": "https://press.spglobal.com/ferguson-change"}
    )
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        result = _capture(
            tmp_path,
            store,
            payload=payload,
            provider="spglobal",
            dataset="sp500_change_announcement",
            parser_version="1",
            request_fingerprint=fingerprint,
            ingest_run_id="legacy-run",
            role="candidate_release_detail_000",
            content_type="text/html",
        )
        arguments = {
            "store": store,
            "run_id": "legacy-run",
            "role": "candidate_release_detail_000",
            "snapshot_id": result.snapshot_id,
            "expected_request_fingerprint": fingerprint,
            "expected_adapter_name": "example-http",
            "expected_adapter_version": "1",
            "project_root": tmp_path,
        }

        first = promote_legacy_sp500_change_snapshot(**arguments)
        second = promote_legacy_sp500_change_snapshot(**arguments)

        assert first.payload_sha256 == second.payload_sha256 == result.payload_sha256
        evidence = store.query(
            """
            SELECT parser_version, parsed_row_count, parsed_rows_sha256,
                   parsed_rows_rejected, parsed_rejection_codes
            FROM raw_snapshots WHERE snapshot_id = ?
            """,
            (result.snapshot_id,),
        )[0]
        assert evidence["parser_version"] == "spglobal-constituent-change-html-v1"
        assert evidence["parsed_row_count"] == 2
        assert len(evidence["parsed_rows_sha256"]) == 64
        assert evidence["parsed_rows_rejected"] == 0
        assert evidence["parsed_rejection_codes"] is None
        assert verify_raw_snapshots(
            store=store,
            project_root=tmp_path,
        ).replayed_snapshots == 1
    finally:
        store.close()


def test_legacy_sp500_change_promotion_binds_request_before_mutation(tmp_path) -> None:
    payload = b"""
    <table>
      <tr><td>August 5, 2026</td><td>S&amp;P 500</td><td>Addition</td>
          <td>Ferguson Enterprises Inc.</td><td>FERG</td></tr>
      <tr><td>August 5, 2026</td><td>S&amp;P 500</td><td>Deletion</td>
          <td>Electronic Arts Inc.</td><td>EA</td></tr>
    </table>
    """
    fingerprint = canonical_request_fingerprint(
        {"method": "GET", "url": "https://press.spglobal.com/ferguson-change"}
    )
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        result = _capture(
            tmp_path,
            store,
            payload=payload,
            provider="spglobal",
            dataset="sp500_change_announcement",
            parser_version="1",
            request_fingerprint=fingerprint,
            ingest_run_id="legacy-run",
            role="candidate_release_detail_000",
            content_type="text/html",
        )

        with pytest.raises(ValueError, match="request_fingerprint mismatch"):
            promote_legacy_sp500_change_snapshot(
                store=store,
                run_id="legacy-run",
                role="candidate_release_detail_000",
                snapshot_id=result.snapshot_id,
                expected_request_fingerprint="f" * 64,
                expected_adapter_name="example-http",
                expected_adapter_version="1",
                project_root=tmp_path,
            )
        assert store.query(
            "SELECT parser_version, parsed_row_count FROM raw_snapshots"
        ) == [{"parser_version": "1", "parsed_row_count": None}]
    finally:
        store.close()


def test_linked_capture_is_promoted_once_with_idempotent_parsed_evidence(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    rows = [{"cik": "0000000001", "metric": "revenue", "value": 100.0}]
    try:
        result = _capture(
            tmp_path,
            store,
            parsed_rows=None,
            parser_version="capture-v1",
            ingest_run_id="issuer-run",
            role="companyfacts",
        )

        snapshot_id = attach_parsed_rows_evidence(
            store=store,
            ingest_run_id="issuer-run",
            role="companyfacts",
            capture_parser_version="capture-v1",
            parser_version="parser-v2",
            parsed_rows=rows,
            rows_rejected=1,
            rejection_codes=["future_period"],
        )
        assert snapshot_id == result.snapshot_id
        assert (
            attach_parsed_rows_evidence(
                store=store,
                ingest_run_id="issuer-run",
                role="companyfacts",
                capture_parser_version="capture-v1",
                parser_version="parser-v2",
                parsed_rows=rows,
                rows_rejected=1,
                rejection_codes=["future_period"],
            )
            == result.snapshot_id
        )
        evidence = store.query(
            """
            SELECT parser_version, parsed_row_count, parsed_rows_sha256,
                   parsed_rows_rejected, parsed_rejection_codes
            FROM raw_snapshots WHERE snapshot_id = ?
            """,
            (result.snapshot_id,),
        )[0]
        assert evidence["parser_version"] == "parser-v2"
        assert evidence["parsed_row_count"] == 1
        assert evidence["parsed_rows_sha256"]
        assert evidence["parsed_rows_rejected"] == 1
        assert evidence["parsed_rejection_codes"] == '["future_period"]'

        with pytest.raises(ValueError, match="conflicts with existing values"):
            attach_parsed_rows_evidence(
                store=store,
                ingest_run_id="issuer-run",
                role="companyfacts",
                capture_parser_version="capture-v1",
                parser_version="parser-v2",
                parsed_rows=[{**rows[0], "value": 101.0}],
                rows_rejected=1,
                rejection_codes=["future_period"],
            )
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


def test_snapshot_capture_rejects_tampered_reused_traversal_path(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        result = _capture(tmp_path, store)
        escaped = tmp_path / "escape"
        store.execute(
            """
            UPDATE raw_payloads
            SET relative_path = ?
            WHERE payload_sha256 = ?
            """,
            (
                f"data/raw/../../escape/{result.payload_sha256}.json.gz",
                result.payload_sha256,
            ),
        )

        with pytest.raises(ValueError, match="path is unsafe"):
            _capture(tmp_path, store)

        assert not escaped.exists()
        assert store.query("SELECT COUNT(*) AS n FROM raw_snapshots")[0]["n"] == 1
    finally:
        store.close()


def test_snapshot_verification_rejects_unregistered_compression(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        result = _capture(tmp_path, store)
        store.execute(
            """
            UPDATE raw_payloads
            SET compression = 'not-gzip'
            WHERE payload_sha256 = ?
            """,
            (result.payload_sha256,),
        )

        with pytest.raises(ValueError, match="compression is unsupported"):
            verify_raw_snapshots(store=store, project_root=tmp_path)
    finally:
        store.close()


def test_snapshot_verification_rejects_leaf_symlink(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        result = _capture(tmp_path, store)
        target = tmp_path / result.relative_path
        real_target = target.with_name(f"{target.name}.real")
        target.rename(real_target)
        target.symlink_to(real_target.name)

        with pytest.raises(ValueError, match="contains a symlink"):
            verify_raw_snapshots(store=store, project_root=tmp_path)
    finally:
        store.close()


def test_snapshot_verification_rejects_hardlinked_payload(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        result = _capture(tmp_path, store)
        target = tmp_path / result.relative_path
        os.link(target, target.with_name(f"{target.name}.copy"))

        with pytest.raises(ValueError, match="link count is unsafe"):
            verify_raw_snapshots(store=store, project_root=tmp_path)
    finally:
        store.close()


def test_snapshot_verification_rejects_fifo_without_blocking(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        result = _capture(tmp_path, store)
        target = tmp_path / result.relative_path
        target.rename(target.with_name(f"{target.name}.real"))
        os.mkfifo(target)

        with pytest.raises(ValueError, match="not a regular file"):
            verify_raw_snapshots(store=store, project_root=tmp_path)
    finally:
        store.close()


def test_snapshot_capture_rejects_symlinked_provider_directory(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    outside = tmp_path / "outside"
    outside.mkdir()
    raw_root = tmp_path / "data" / "raw"
    raw_root.mkdir(parents=True)
    (raw_root / "example").symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="contains a symlink"):
            _capture(tmp_path, store)

        assert list(outside.iterdir()) == []
        assert store.query("SELECT COUNT(*) AS n FROM raw_snapshots")[0]["n"] == 0
    finally:
        store.close()


def test_snapshot_capture_dirfd_walk_closes_ancestor_swap_race(
    monkeypatch,
    tmp_path,
) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    provider_dir = tmp_path / "data" / "raw" / "example"
    provider_dir.mkdir(parents=True)
    original_dir = provider_dir.with_name("example-original")
    outside = tmp_path / "outside"
    outside.mkdir()
    original_check = raw_snapshot_module._reject_symlink_ancestors
    checks = 0

    def swap_after_final_path_check(root, target):
        nonlocal checks
        original_check(root, target)
        checks += 1
        if checks == 2:
            provider_dir.rename(original_dir)
            provider_dir.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(
        raw_snapshot_module,
        "_reject_symlink_ancestors",
        swap_after_final_path_check,
    )
    try:
        with pytest.raises(ValueError, match="write path is unsafe"):
            _capture(tmp_path, store)

        assert list(outside.iterdir()) == []
        assert store.query("SELECT COUNT(*) AS n FROM raw_snapshots")[0]["n"] == 0
    finally:
        store.close()


def test_snapshot_verification_bounds_gzip_expansion_to_declared_size(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        result = _capture(tmp_path, store, b"small")
        compressed_bomb = gzip.compress(b"x" * 2_000_000, mtime=0)
        (tmp_path / result.relative_path).write_bytes(compressed_bomb)
        store.execute(
            """
            UPDATE raw_payloads
            SET stored_bytes = ?
            WHERE payload_sha256 = ?
            """,
            (len(compressed_bomb), result.payload_sha256),
        )

        with pytest.raises(ValueError, match="original size mismatch"):
            verify_raw_snapshots(store=store, project_root=tmp_path)
    finally:
        store.close()


def test_companyfacts_verification_replays_rejection_evidence(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    payload = json.dumps(
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
    rows, metadata = edgar.replay_sec_companyfacts_response(
        payload,
        parser_version=edgar.COMPANYFACTS_PARSER_VERSION,
    )
    try:
        result = _capture(
            tmp_path,
            store,
            payload,
            provider="sec-edgar",
            dataset="companyfacts",
            parser_version=edgar.COMPANYFACTS_CAPTURE_PARSER_VERSION,
            parsed_rows=None,
            ingest_run_id="issuer-run",
            role="companyfacts",
        )
        attach_parsed_rows_evidence(
            store=store,
            ingest_run_id="issuer-run",
            role="companyfacts",
            capture_parser_version=edgar.COMPANYFACTS_CAPTURE_PARSER_VERSION,
            parser_version=edgar.COMPANYFACTS_PARSER_VERSION,
            parsed_rows=rows,
            rows_rejected=metadata["rows_rejected"],
            rejection_codes=metadata["rejection_codes"],
        )
        assert (
            verify_raw_snapshots(
                store=store,
                project_root=tmp_path,
            ).replayed_snapshots
            == 1
        )

        store.execute(
            """
            UPDATE raw_snapshots
            SET parsed_rows_rejected = 1,
                parsed_rejection_codes = '["future_period"]'
            WHERE snapshot_id = ?
            """,
            (result.snapshot_id,),
        )
        with pytest.raises(ValueError, match="rejection evidence mismatch"):
            verify_raw_snapshots(store=store, project_root=tmp_path)
    finally:
        store.close()


def test_direct_companyfacts_capture_requires_rejection_evidence(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        with pytest.raises(
            ValueError,
            match="Company Facts snapshots require rejection evidence",
        ):
            _capture(
                tmp_path,
                store,
                b'{"cik":1,"facts":{}}',
                provider="sec-edgar",
                dataset="companyfacts",
                parser_version=edgar.COMPANYFACTS_PARSER_VERSION,
                parsed_rows=[],
            )
        assert store.query("SELECT COUNT(*) AS n FROM raw_snapshots")[0]["n"] == 0
    finally:
        store.close()


def test_parsed_snapshot_requires_a_reviewed_replay_parser(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        result = _capture(
            tmp_path,
            store,
            parsed_rows=[{"date": "2026-07-21", "close": 10.0}],
        )
        store.execute(
            """
            UPDATE raw_snapshots
            SET parser_version = 'unknown-parser'
            WHERE snapshot_id = ?
            """,
            (result.snapshot_id,),
        )

        with pytest.raises(ValueError, match="no reviewed replay parser"):
            verify_raw_snapshots(store=store, project_root=tmp_path)
    finally:
        store.close()


def test_companyfacts_rejection_migration_marker_is_required(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        _capture(
            tmp_path,
            store,
            b'{"cik":1,"facts":{}}',
            provider="sec-edgar",
            dataset="companyfacts",
            parser_version=edgar.COMPANYFACTS_PARSER_VERSION,
            parsed_rows=[],
            parsed_rows_rejected=0,
        )
        store.execute(
            """
            DELETE FROM schema_migrations
            WHERE name = 'raw_snapshot_rejection_evidence_v1'
            """
        )

        with pytest.raises(ValueError, match="migration marker is invalid"):
            verify_raw_snapshots(store=store, project_root=tmp_path)
    finally:
        store.close()


def test_companyfacts_rejection_schema_cannot_be_downgraded(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        _capture(
            tmp_path,
            store,
            b'{"cik":1,"facts":{}}',
            provider="sec-edgar",
            dataset="companyfacts",
            parser_version=edgar.COMPANYFACTS_PARSER_VERSION,
            parsed_rows=[],
            parsed_rows_rejected=0,
        )
        store.execute("ALTER TABLE raw_snapshots DROP COLUMN parsed_rows_rejected")
        store.execute("ALTER TABLE raw_snapshots DROP COLUMN parsed_rejection_codes")

        with pytest.raises(ValueError, match="evidence schema is incomplete"):
            verify_raw_snapshots(store=store, project_root=tmp_path)
    finally:
        store.close()


def test_companyfacts_rejection_evidence_cannot_be_backdated_away(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        result = _capture(
            tmp_path,
            store,
            b'{"cik":1,"facts":{}}',
            provider="sec-edgar",
            dataset="companyfacts",
            parser_version=edgar.COMPANYFACTS_PARSER_VERSION,
            parsed_rows=[],
            parsed_rows_rejected=0,
        )
        store.execute(
            """
            UPDATE raw_snapshots
            SET parsed_rows_rejected = NULL,
                parsed_rejection_codes = NULL,
                requested_at = TIMESTAMP '2000-01-01 00:00:00',
                received_at = TIMESTAMP '2000-01-01 00:00:00',
                created_at = TIMESTAMP '2000-01-01 00:00:00'
            WHERE snapshot_id = ?
            """,
            (result.snapshot_id,),
        )

        with pytest.raises(ValueError, match="lacks rejection evidence"):
            verify_raw_snapshots(store=store, project_root=tmp_path)
    finally:
        store.close()


def test_companyfacts_rejection_migration_replays_historical_exact_bytes(
    tmp_path,
) -> None:
    database = tmp_path / "data" / "test.duckdb"
    store = Store(database)
    try:
        result = _capture(
            tmp_path,
            store,
            b'{"cik":1,"facts":{}}',
            provider="sec-edgar",
            dataset="companyfacts",
            parser_version=edgar.COMPANYFACTS_PARSER_VERSION,
            parsed_rows=[],
            parsed_rows_rejected=0,
        )
    finally:
        store.close()

    connection = duckdb.connect(str(database))
    try:
        connection.execute(
            """
            DELETE FROM schema_migrations
            WHERE name = 'raw_snapshot_rejection_evidence_v1'
            """
        )
        connection.execute("ALTER TABLE raw_snapshots DROP COLUMN parsed_rows_rejected")
        connection.execute("ALTER TABLE raw_snapshots DROP COLUMN parsed_rejection_codes")
    finally:
        connection.close()

    migrated = Store(database)
    try:
        evidence = migrated.query(
            """
            SELECT parsed_rows_rejected, parsed_rejection_codes
            FROM raw_snapshots
            WHERE snapshot_id = ?
            """,
            (result.snapshot_id,),
        )[0]
        assert evidence == {
            "parsed_rows_rejected": 0,
            "parsed_rejection_codes": None,
        }
        assert (
            verify_raw_snapshots(
                store=migrated,
                project_root=tmp_path,
            ).replayed_snapshots
            == 1
        )
    finally:
        migrated.close()


def test_snapshot_rejects_unsafe_components_and_naive_timestamps(tmp_path) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        with pytest.raises(ValueError, match="safe path component"):
            _capture(tmp_path, store, provider="../escape")
        with pytest.raises(ValueError, match="lowercase 64-character"):
            _capture(tmp_path, store, request_fingerprint="z" * 64)
        with pytest.raises(ValueError, match="timezone-aware"):
            _capture(
                tmp_path,
                store,
                requested_at=datetime(2026, 7, 22, 10, 0),
                received_at=datetime(2026, 7, 22, 10, 1),
            )
    finally:
        store.close()

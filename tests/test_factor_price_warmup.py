from __future__ import annotations

import gzip
import json
import shutil
from datetime import date, timedelta

import pytest

from aios.ingest.factor_price_warmup import (
    REVIEW_POLICY,
    build_factor_price_warmup,
    ingest_factor_price_warmup,
    mark_factor_price_warmup_rejections_reviewed,
)
from aios.storage.store import Store

SECURITY_ID = "aios:security:demo-common"
ISSUER_ID = "aios:issuer:demo"
EVIDENCE = "https://example.com/review"
PROVIDER_SOURCE = "https://query1.finance.yahoo.com/v8/finance/chart/NEW"


def _install_identity(
    store: Store,
    *,
    anchor: str = "2023-08-01",
    blocked_start: str | None = None,
) -> None:
    membership = {
        "universe_id": "demo",
        "ticker": "NEW",
        "effective_start": anchor,
        "effective_end": "2025-01-01",
        "known_date": anchor,
        "source": EVIDENCE,
    }
    store.upsert_universe_membership([membership])
    store.upsert_security_identities(
        [
            {
                **membership,
                "security_id": SECURITY_ID,
                "identity_status": "verified_ticker_change",
            }
        ]
    )
    providers = []
    if blocked_start is not None:
        providers.append(
            {
                "provider": "yfinance",
                "provider_symbol": "OLD",
                "security_id": SECURITY_ID,
                "data_start": blocked_start,
                "data_end": anchor,
                "mapping_status": "blocked_wrong_security",
                "verified_date": anchor,
                "source": PROVIDER_SOURCE,
            }
        )
    providers.append(
        {
            "provider": "yfinance",
            "provider_symbol": "NEW",
            "security_id": SECURITY_ID,
            "data_start": anchor,
            "data_end": "2025-01-01",
            "mapping_status": "verified",
            "verified_date": anchor,
            "source": PROVIDER_SOURCE,
        }
    )
    store.upsert_reference_identities(
        [
            {
                "issuer_id": ISSUER_ID,
                "canonical_name": "Demo Corporation",
                "canonical_ticker": "NEW",
                "source": EVIDENCE,
            }
        ],
        [
            {
                "issuer_id": ISSUER_ID,
                "cik": "1",
                "effective_start": anchor,
                "effective_end": "2025-01-01",
                "verified_date": anchor,
                "source": EVIDENCE,
            }
        ],
        [
            {
                "security_id": SECURITY_ID,
                "issuer_id": ISSUER_ID,
                "effective_start": anchor,
                "effective_end": "2025-01-01",
                "verified_date": anchor,
                "source": EVIDENCE,
            }
        ],
        providers,
    )


def _price_row(
    day: str,
    close: float,
    *,
    tagged: bool = False,
    normalization_through: str = "2024-01-31",
) -> dict:
    row = {
        "ticker": "NEW",
        "date": day,
        "close": close,
        "adj_close": close,
        "dividends": 0.0,
        "split_ratio": 1.0,
        "actions_complete": True,
        "close_split_adjusted": True,
        "split_normalization_factor": 1.0,
        "split_normalization_through": normalization_through,
        "source": "yfinance",
    }
    if tagged:
        row |= {
            "security_id": SECURITY_ID,
            "provider_symbol": "NEW",
        }
    return row


def test_factor_price_warmup_store_merges_without_backdating_ticker(tmp_path):
    store = Store(tmp_path / "factor-warmup.duckdb")
    try:
        _install_identity(store)
        store.upsert_prices(
            [
                _price_row("2023-08-01", 12, tagged=True),
                _price_row("2023-08-02", 13, tagged=True),
            ]
        )
        provenance_id = "fpw:" + "a" * 64
        counts = store.upsert_factor_price_warmup(
            [
                {
                    "provenance_id": provenance_id,
                    "universe_id": "demo",
                    "security_id": SECURITY_ID,
                    "provider": "yfinance",
                    "provider_symbol": "NEW",
                    "data_start": "2023-07-01",
                    "data_end": "2023-08-01",
                    "overlap_start": "2023-08-01",
                    "overlap_end": "2023-08-08",
                    "verified_date": "2024-01-31",
                    "source": PROVIDER_SOURCE,
                    "payload_sha256": "b" * 64,
                    "overlap_sha256": "c" * 64,
                    "review_policy": REVIEW_POLICY,
                }
            ],
            [
                {
                    **_price_row("2023-07-31", 11),
                    "security_id": SECURITY_ID,
                    "provider": "yfinance",
                    "provider_symbol": "NEW",
                    "provenance_id": provenance_id,
                }
            ],
        )

        history = store.pit_factor_price_history(
            "NEW", "2023-08-02", observations=3
        )
        report = {row["check"]: row for row in store.data_quality_report()}

        assert counts == {"provenance": 1, "factor_prices": 1}
        assert [str(row["date"]) for row in history] == [
            "2023-07-31",
            "2023-08-01",
            "2023-08-02",
        ]
        assert store.query("SELECT * FROM factor_prices")[0].get("ticker") is None
        assert all(
            row["status"] == "ok"
            for name, row in report.items()
            if name.startswith("factor_price")
        )
    finally:
        store.close()


def test_factor_price_warmup_rejects_rows_outside_review_window(tmp_path):
    store = Store(tmp_path / "factor-warmup-invalid.duckdb")
    try:
        _install_identity(store)
        provenance_id = "fpw:" + "a" * 64
        with pytest.raises(ValueError, match="outside its reviewed provenance"):
            store.upsert_factor_price_warmup(
                [
                    {
                        "provenance_id": provenance_id,
                        "universe_id": "demo",
                        "security_id": SECURITY_ID,
                        "provider": "yfinance",
                        "provider_symbol": "NEW",
                        "data_start": "2023-07-01",
                        "data_end": "2023-08-01",
                        "overlap_start": "2023-08-01",
                        "overlap_end": "2023-08-08",
                        "verified_date": "2024-01-31",
                        "source": PROVIDER_SOURCE,
                        "payload_sha256": "b" * 64,
                        "overlap_sha256": "c" * 64,
                        "review_policy": REVIEW_POLICY,
                    }
                ],
                [
                    {
                        **_price_row("2023-08-01", 12),
                        "security_id": SECURITY_ID,
                        "provider": "yfinance",
                        "provider_symbol": "NEW",
                        "provenance_id": provenance_id,
                    }
                ],
            )
        assert store.query("SELECT COUNT(*) n FROM factor_prices")[0]["n"] == 0
    finally:
        store.close()


def test_warmup_batch_overlap_review_is_resumable_and_ingestable(tmp_path):
    store = Store(tmp_path / "factor-warmup-build.duckdb")
    checked_on = date(2024, 1, 31)
    try:
        _install_identity(store, anchor="2023-01-20")
        store.upsert_prices(
            [
                _price_row("2023-01-20", 13, tagged=True),
                _price_row("2023-01-23", 14, tagged=True),
            ]
        )
        fetched = [
            _price_row("2023-01-02", 10),
            _price_row("2023-01-03", 11),
            _price_row("2023-01-04", 12),
            _price_row("2023-01-19", 12.5),
            _price_row("2023-01-20", 13),
            _price_row("2023-01-23", 14),
        ]
        calls = 0

        def fetcher(_provider: str, _symbol: str, _start: str, _end: str) -> list[dict]:
            nonlocal calls
            calls += 1
            return fetched

        batch_dir = tmp_path / "batch"
        first = build_factor_price_warmup(
            batch_dir,
            universe_id="demo",
            start="2023-01-01",
            overlap_days=7,
            minimum_overlap_sessions=2,
            minimum_warmup_sessions=3,
            store=store,
            fetcher=fetcher,
            checked_on=checked_on,
        )
        second = build_factor_price_warmup(
            batch_dir,
            universe_id="demo",
            start="2023-01-01",
            overlap_days=7,
            minimum_overlap_sessions=2,
            minimum_warmup_sessions=3,
            store=store,
            fetcher=fetcher,
            checked_on=checked_on + timedelta(days=1),
        )
        counts = ingest_factor_price_warmup(batch_dir, store=store)

        assert first["accepted"] == 1
        assert second["reused"] == 1
        assert second["review_rows"][0]["cache_reused"] is True
        assert calls == 1
        assert counts == {"provenance": 1, "factor_prices": 4, "snapshots": 1}

        snapshot = next((batch_dir / "snapshots").glob("*.json.gz"))
        stale = snapshot.with_name("stale.json.gz")
        shutil.copyfile(snapshot, stale)
        with pytest.raises(ValueError, match="snapshot set disagrees"):
            ingest_factor_price_warmup(batch_dir, store=store)
        stale.unlink()

        with gzip.open(snapshot, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["rows"][0]["close"] = 999
        with gzip.open(snapshot, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle)
        with pytest.raises(ValueError, match="payload hash mismatch"):
            ingest_factor_price_warmup(batch_dir, store=store)
    finally:
        store.close()


def test_warmup_overlap_allows_newer_split_scan_provenance(tmp_path):
    store = Store(tmp_path / "factor-warmup-scan-date.duckdb")
    try:
        _install_identity(store, anchor="2023-01-20")
        store.upsert_prices(
            [
                _price_row("2023-01-20", 13, tagged=True),
                _price_row("2023-01-23", 14, tagged=True),
            ]
        )
        fetched = [
            _price_row(
                day,
                close,
                normalization_through="2024-02-01",
            )
            for day, close in (
                ("2023-01-02", 10),
                ("2023-01-03", 11),
                ("2023-01-04", 12),
                ("2023-01-19", 12.5),
                ("2023-01-20", 13),
                ("2023-01-23", 14),
            )
        ]

        result = build_factor_price_warmup(
            tmp_path / "scan-date-batch",
            universe_id="demo",
            start="2023-01-01",
            overlap_days=7,
            minimum_overlap_sessions=2,
            minimum_warmup_sessions=3,
            store=store,
            fetcher=lambda *_args: fetched,
            checked_on=date(2024, 2, 1),
        )

        assert result["accepted"] == 1
        assert result["rejected"] == 0
    finally:
        store.close()


def test_warmup_cache_rechecks_current_economic_overlap(tmp_path):
    store = Store(tmp_path / "factor-warmup-cache-overlap.duckdb")
    checked_on = date(2024, 1, 31)
    calls = 0
    try:
        _install_identity(store, anchor="2023-01-20")
        store.upsert_prices(
            [
                _price_row("2023-01-20", 13, tagged=True),
                _price_row("2023-01-23", 14, tagged=True),
            ]
        )
        fetched = [
            _price_row(day, close)
            for day, close in (
                ("2023-01-02", 10),
                ("2023-01-03", 11),
                ("2023-01-04", 12),
                ("2023-01-19", 12.5),
                ("2023-01-20", 13),
                ("2023-01-23", 14),
            )
        ]

        def fetcher(*_args) -> list[dict]:
            nonlocal calls
            calls += 1
            return fetched

        batch_dir = tmp_path / "cache-overlap-batch"
        first = build_factor_price_warmup(
            batch_dir,
            universe_id="demo",
            start="2023-01-01",
            overlap_days=7,
            minimum_overlap_sessions=2,
            minimum_warmup_sessions=3,
            store=store,
            fetcher=fetcher,
            checked_on=checked_on,
        )
        store.upsert_prices([_price_row("2023-01-20", 130, tagged=True)])
        second = build_factor_price_warmup(
            batch_dir,
            universe_id="demo",
            start="2023-01-01",
            overlap_days=7,
            minimum_overlap_sessions=2,
            minimum_warmup_sessions=3,
            store=store,
            fetcher=fetcher,
            checked_on=checked_on + timedelta(days=1),
        )

        assert first["accepted"] == 1
        assert second["accepted"] == 0
        assert second["rejected"] == 1
        assert "reviewed overlap mismatch for close" in second["review_rows"][0][
            "reason"
        ]
        assert calls == 1
    finally:
        store.close()


def test_warmup_batch_rejects_known_wrong_pre_anchor_provider_history(tmp_path):
    store = Store(tmp_path / "factor-warmup-blocked.duckdb")
    try:
        _install_identity(
            store,
            anchor="2023-01-20",
            blocked_start="2023-01-01",
        )
        called = False

        def fetcher(_provider: str, _symbol: str, _start: str, _end: str) -> list[dict]:
            nonlocal called
            called = True
            return []

        result = build_factor_price_warmup(
            tmp_path / "blocked-batch",
            universe_id="demo",
            start="2023-01-01",
            overlap_days=7,
            minimum_overlap_sessions=2,
            minimum_warmup_sessions=3,
            store=store,
            fetcher=fetcher,
            checked_on=date.today() - timedelta(days=1),
        )

        assert result["accepted"] == 0
        assert result["rejected"] == 1
        assert "blocked or unavailable" in result["review_rows"][0]["reason"]
        assert called is False
    finally:
        store.close()


def test_warmup_rejections_are_reviewed_without_repeating_provider_fetch(tmp_path):
    store = Store(tmp_path / "factor-warmup-reviewed-rejection.duckdb")
    calls = 0
    try:
        _install_identity(store, anchor="2023-01-20")
        store.upsert_prices(
            [
                _price_row("2023-01-20", 13, tagged=True),
                _price_row("2023-01-23", 14, tagged=True),
            ]
        )

        def fetcher(*_args) -> list[dict]:
            nonlocal calls
            calls += 1
            return [
                _price_row("2023-01-20", 13),
                _price_row("2023-01-23", 14),
            ]

        batch_dir = tmp_path / "reviewed-rejection-batch"
        result = build_factor_price_warmup(
            batch_dir,
            universe_id="demo",
            start="2023-01-01",
            overlap_days=7,
            minimum_overlap_sessions=2,
            minimum_warmup_sessions=3,
            store=store,
            fetcher=fetcher,
            checked_on=date(2024, 1, 31),
        )
        count = mark_factor_price_warmup_rejections_reviewed(
            batch_dir, reviewed_on=date(2024, 2, 1)
        )
        manifest = json.loads(
            (batch_dir / "factor_price_warmup_manifest.json").read_text()
        )

        assert result["rejected"] == 1
        assert count == 1
        assert calls == 1
        assert manifest["rejections_reviewed"] is True
        assert manifest["rejections_reviewed_on"] == "2024-02-01"
    finally:
        store.close()

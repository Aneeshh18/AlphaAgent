from pathlib import Path

import pytest

from aios.ingest.security_events import (
    ingest_security_conversion_csv,
    load_security_conversion_csv,
)
from aios.storage.store import Store


def _seed_security_ids(store: Store) -> None:
    memberships = [
        {
            "universe_id": "demo",
            "ticker": ticker,
            "effective_start": "2024-01-01",
            "effective_end": None,
            "known_date": "2024-01-01",
            "source": "https://example.com/membership",
        }
        for ticker in ("OLD", "NEW")
    ]
    store.upsert_universe_membership(memberships)
    store.upsert_security_identities(
        [
            {
                **membership,
                "security_id": f"aios:security:{membership['ticker'].lower()}",
                "identity_status": "bounded_ticker",
            }
            for membership in memberships
        ]
    )


def _manifest(path: Path, *, known_date: str = "2024-05-01") -> Path:
    path.write_text(
        "source_security_id,target_security_id,effective_date,known_date,"
        "share_ratio,basis_policy,review_status,verified_date,source,basis_source\n"
        f"aios:security:old,aios:security:new,2024-05-01,{known_date},"
        "2.0,carryover,verified,2024-06-01,"
        "https://www.sec.gov/Archives/event.htm,"
        "https://www.sec.gov/Archives/basis.htm\n",
        encoding="utf-8",
    )
    return path


def test_security_conversion_manifest_imports_reviewed_event(tmp_path):
    store = Store(tmp_path / "events.duckdb")
    try:
        _seed_security_ids(store)
        path = _manifest(tmp_path / "events.csv")

        loaded = load_security_conversion_csv(path)
        count = ingest_security_conversion_csv(path, store=store)

        assert loaded[0]["share_ratio"] == 2.0
        assert count == 1
        rows = store.security_conversions_between(
            {"aios:security:old"}, "2024-04-30", "2024-06-30"
        )
        assert len(rows) == 1
        assert rows[0]["target_security_id"] == "aios:security:new"
        assert store.ticker_for_security_id("aios:security:new", "2024-05-01") == "NEW"
    finally:
        store.close()


def test_security_conversion_manifest_rejects_lookahead_date(tmp_path):
    path = _manifest(tmp_path / "lookahead.csv", known_date="2024-05-02")

    with pytest.raises(ValueError, match="known_date follows effective_date"):
        load_security_conversion_csv(path)


def test_security_conversion_store_rejects_provenance_remap(tmp_path):
    store = Store(tmp_path / "conflict.duckdb")
    try:
        _seed_security_ids(store)
        event = load_security_conversion_csv(_manifest(tmp_path / "events.csv"))[0]
        store.upsert_security_conversions([event])

        with pytest.raises(ValueError, match="conflicts with existing provenance"):
            store.upsert_security_conversions(
                [{**event, "share_ratio": 1.5}]
            )
    finally:
        store.close()

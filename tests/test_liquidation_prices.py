from datetime import date
from pathlib import Path

import pytest

from aios.ingest.liquidation_prices import ingest_liquidation_extension_csv
from aios.storage.store import Store


def _seed_removed_security(store: Store) -> None:
    membership = {
        "universe_id": "demo",
        "ticker": "OLD",
        "effective_start": "2024-01-01",
        "effective_end": "2024-04-01",
        "known_date": "2024-01-01",
        "end_known_date": "2024-03-15",
        "source": "https://example.com/membership",
    }
    store.upsert_universe_membership([membership])
    store.upsert_security_identities(
        [
            {
                **membership,
                "security_id": "aios:security:old",
                "identity_status": "bounded_ticker",
            }
        ]
    )
    store.upsert_reference_identities(
        [
            {
                "issuer_id": "aios:issuer:old",
                "canonical_name": "Old Corp",
                "canonical_ticker": "OLD",
                "source": "https://www.sec.gov/Archives/old.htm",
            }
        ],
        [
            {
                "issuer_id": "aios:issuer:old",
                "cik": "0000000001",
                "effective_start": "2024-01-01",
                "effective_end": "2024-04-01",
                "verified_date": "2024-04-10",
                "source": "https://www.sec.gov/Archives/old.htm",
            }
        ],
        [
            {
                "security_id": "aios:security:old",
                "issuer_id": "aios:issuer:old",
                "effective_start": "2024-01-01",
                "effective_end": "2024-04-01",
                "verified_date": "2024-04-10",
                "source": "https://www.sec.gov/Archives/old.htm",
            }
        ],
        [
            {
                "provider": "yfinance",
                "provider_symbol": "OLD",
                "security_id": "aios:security:old",
                "data_start": "2024-01-01",
                "data_end": "2024-04-01",
                "mapping_status": "verified",
                "verified_date": "2024-04-10",
                "source": "https://query1.finance.yahoo.com/v8/finance/chart/OLD",
            }
        ],
    )


def _manifest(path: Path) -> Path:
    path.write_text(
        "universe_id,security_id,ticker,provider,provider_symbol,data_start,"
        "data_end,verified_date,identity_source,provider_source,purpose\n"
        "demo,aios:security:old,OLD,yfinance,OLD,2024-04-01,2024-04-04,"
        "2024-04-10,https://www.sec.gov/Archives/old.htm,"
        "https://query1.finance.yahoo.com/v8/finance/chart/OLD,"
        "portfolio_liquidation\n",
        encoding="utf-8",
    )
    return path


def _fetched_rows(*_args):
    return [
        {
            "ticker": "OLD",
            "date": observation_date,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "adj_close": price,
            "volume": 100,
            "dividends": 0.0,
            "split_ratio": 1.0,
            "actions_complete": True,
            "close_split_adjusted": True,
            "split_normalization_factor": 1.0,
            "split_normalization_through": "2024-04-10",
            "source": "yfinance",
        }
        for observation_date, price in (
            ("2024-04-01", 100.0),
            ("2024-04-02", 101.0),
            ("2024-04-03", 102.0),
        )
    ]


def test_liquidation_extension_adds_prices_without_restoring_membership(tmp_path):
    store = Store(tmp_path / "liquidation.duckdb")
    try:
        _seed_removed_security(store)

        counts = ingest_liquidation_extension_csv(
            _manifest(tmp_path / "liquidation.csv"),
            store=store,
            fetcher=_fetched_rows,
        )

        assert counts == {"extensions": 1, "provider_symbols": 1, "prices": 3}
        assert store.universe_membership_on("demo", "2024-04-02") == []
        assignments = store.security_ticker_assignments(
            "aios:security:old",
            start="2024-04-01",
            end="2024-04-04",
        )
        assert assignments == [
            {
                "ticker": "OLD",
                "effective_start": date(2024, 4, 1),
                "effective_end": date(2024, 4, 4),
            }
        ]
        assert store.query(
            "SELECT COUNT(*) AS n FROM prices WHERE security_id = 'aios:security:old'"
        )[0]["n"] == 3
        failures = [
            row for row in store.data_quality_report() if row["status"] == "fail"
        ]
        assert not any("ticker_extension" in row["check"] for row in failures)
        assert not any("tagged_prices" in row["check"] for row in failures)
    finally:
        store.close()


def test_liquidation_extension_is_idempotent_and_detects_price_tampering(tmp_path):
    store = Store(tmp_path / "tamper.duckdb")
    try:
        _seed_removed_security(store)
        manifest = _manifest(tmp_path / "liquidation.csv")

        ingest_liquidation_extension_csv(manifest, store=store, fetcher=_fetched_rows)
        ingest_liquidation_extension_csv(manifest, store=store, fetcher=_fetched_rows)

        assert store.query(
            "SELECT COUNT(*) AS n FROM security_ticker_extensions"
        )[0]["n"] == 1
        assert store.query(
            """
            SELECT COUNT(*) AS n
            FROM provider_symbol_history
            WHERE security_id = 'aios:security:old'
            """
        )[0]["n"] == 2
        assert store.query(
            "SELECT COUNT(*) AS n FROM prices WHERE security_id = 'aios:security:old'"
        )[0]["n"] == 3

        store.execute(
            """
            UPDATE prices SET close = close + 1
            WHERE security_id = 'aios:security:old' AND date = DATE '2024-04-02'
            """
        )
        mismatch = next(
            row
            for row in store.data_quality_report()
            if row["check"] == "security_ticker_extension_payload_mismatch"
        )
        assert mismatch["status"] == "fail"
        assert mismatch["count"] == 1
    finally:
        store.close()


def test_liquidation_extension_rejects_missing_market_session(tmp_path):
    store = Store(tmp_path / "missing.duckdb")
    try:
        _seed_removed_security(store)

        with pytest.raises(ValueError, match="incomplete: missing 2024-04-03"):
            ingest_liquidation_extension_csv(
                _manifest(tmp_path / "liquidation.csv"),
                store=store,
                fetcher=lambda *_args: _fetched_rows()[:-1],
            )

        assert store.query("SELECT COUNT(*) AS n FROM security_ticker_extensions")[0][
            "n"
        ] == 0
    finally:
        store.close()

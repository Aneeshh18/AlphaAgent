from __future__ import annotations

from datetime import date

from aios.macro.regime import MacroRegimeSnapshot
from aios.readiness import USReadinessPolicy, assess_us_readiness
from aios.storage.store import Store


def _ready_macro(as_of, store) -> MacroRegimeSnapshot:
    return MacroRegimeSnapshot(
        as_of=str(as_of),
        regime="goldilocks",
        growth_state="expanding",
        inflation_state="falling",
        curve_state="normal",
        stress_state="normal",
    )


def _seed_small_ready_market(store: Store) -> None:
    membership = [
        {
            "universe_id": "sp500",
            "ticker": ticker,
            "effective_start": "2024-01-01",
            "effective_end": "2025-01-01",
            "known_date": "2024-01-01",
            "end_known_date": "2024-12-31",
            "source": "test",
        }
        for ticker in ("A", "B")
    ]
    store.upsert_universe_membership(membership)
    store.upsert_security_identities(
        [
            {
                **row,
                "security_id": f"aios:security:{row['ticker'].lower()}",
                "identity_status": "bounded_ticker",
            }
            for row in membership
        ]
    )
    store.upsert_prices(
        [
            {
                "ticker": ticker,
                "security_id": security_id,
                "provider_symbol": ticker,
                "date": "2024-12-31",
                "close": 100.0,
                "source": "test",
            }
            for ticker, security_id in (
                ("A", "aios:security:a"),
                ("B", "aios:security:b"),
                ("SPY", None),
            )
        ]
    )
    store.upsert_fundamentals(
        [
            {
                "ticker": ticker,
                "period_end": "2024-09-30",
                "as_of_date": "2024-11-01",
                "fiscal_period": "Q3_2024",
                "statement": "balance",
                "metric": "total_assets",
                "value": 1_000.0,
                "unit": "USD",
                "source": "test",
            }
            for ticker in ("A", "B")
        ]
    )


def test_historical_readiness_passes_only_inside_reviewed_window(monkeypatch, tmp_path) -> None:
    store = Store(tmp_path / "readiness.duckdb")
    try:
        _seed_small_ready_market(store)
        monkeypatch.setattr("aios.readiness.compute_regime", _ready_macro)
        monkeypatch.setattr(store, "data_quality_report", lambda: [])
        policy = USReadinessPolicy(
            minimum_universe_members=2,
            maximum_universe_members=3,
            minimum_member_coverage=1.0,
        )

        historical = assess_us_readiness(
            "2024-12-31",
            purpose="historical_research",
            policy=policy,
            store=store,
            today=date(2026, 7, 21),
        )
        current = assess_us_readiness(
            "2026-07-21",
            purpose="paper",
            policy=policy,
            store=store,
            today=date(2026, 7, 21),
        )

        assert historical.ready is True
        assert historical.certified_research_through == "2024-12-31"
        assert current.ready is False
        assert current.certified_research_through == "2024-12-31"
        assert {check.check for check in current.blockers} >= {
            "universe_membership",
            "reviewed_price_freshness",
            "benchmark_freshness",
        }
    finally:
        store.close()


def test_certified_through_uses_dated_members_not_every_historical_security(
    monkeypatch, tmp_path
) -> None:
    store = Store(tmp_path / "member-relative-bounds.duckdb")
    try:
        _seed_small_ready_market(store)
        store.upsert_prices(
            [
                {
                    "ticker": "RETIRED",
                    "security_id": "aios:security:retired",
                    "provider_symbol": "RETIRED",
                    "date": "2024-06-03",
                    "close": 25.0,
                    "actions_complete": True,
                    "close_split_adjusted": False,
                    "split_normalization_factor": 1.0,
                    "source": "test",
                }
            ]
        )
        monkeypatch.setattr("aios.readiness.compute_regime", _ready_macro)
        monkeypatch.setattr(store, "data_quality_report", lambda: [])
        policy = USReadinessPolicy(
            minimum_universe_members=2,
            maximum_universe_members=3,
            minimum_member_coverage=1.0,
        )

        report = assess_us_readiness(
            "2024-12-31",
            purpose="historical_research",
            policy=policy,
            store=store,
            today=date(2026, 7, 21),
        )

        assert report.ready is True
        assert report.certified_research_through == "2024-12-31"
    finally:
        store.close()


def test_readiness_report_exposes_raw_dates_without_treating_them_as_certification(
    monkeypatch, tmp_path
) -> None:
    store = Store(tmp_path / "raw-is-not-reviewed.duckdb")
    try:
        _seed_small_ready_market(store)
        store.upsert_prices(
            [
                {
                    "ticker": "RAW",
                    "date": "2026-07-20",
                    "close": 50.0,
                    "actions_complete": False,
                    "source": "stooq",
                }
            ]
        )
        monkeypatch.setattr("aios.readiness.compute_regime", _ready_macro)
        monkeypatch.setattr(store, "data_quality_report", lambda: [])
        policy = USReadinessPolicy(
            minimum_universe_members=2,
            maximum_universe_members=3,
            minimum_member_coverage=1.0,
        )

        report = assess_us_readiness(
            "2026-07-21",
            purpose="paper",
            policy=policy,
            store=store,
            today=date(2026, 7, 21),
        )

        assert report.raw_prices_through == "2026-07-20"
        assert report.certified_research_through == "2024-12-31"
        assert report.ready is False
    finally:
        store.close()

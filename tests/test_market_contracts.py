"""INDIA_BUILD_PLAN.md phase I1 exit gate.

"A synthetic India fixture passes the same identity, calendar, PIT, factor,
portfolio, and readiness contracts without .NS, INR, or NSE logic inside
shared factor code."

This builds one synthetic non-U.S. security — ISIN-identified, INR-
denominated, listed on a synthetic NSE-shaped venue via the new
`markets`/`venues`/`security_listings` tables — and runs it through the exact
same shared code the U.S. reference data uses: `compute_composite`,
`security_id_for_ticker`, `universe_identity_labels`, `universe_data_coverage`,
and `assess_us_readiness` with an overridden policy. None of these functions
receive a market_id, currency, or venue argument; if any of them silently
assumed USD, "sp500", or a U.S. calendar, this fixture would surface it.

`assess_us_readiness`'s own name is a real, known naming debt — it is
parameterized and market-agnostic in practice (proven below), but the name
itself says otherwise. `readiness.py` is inside the active trial's frozen
policy bundle and cannot be renamed without drifting it; fixing the name is
future cleanup, not a blocker for what this gate actually tests.
"""

from __future__ import annotations

from aios.factors.composite import compute_composite
from aios.readiness import USReadinessPolicy, assess_us_readiness
from aios.storage.store import Store

SECURITY_ID = "aios:test:in:relitest"
TICKER = "RELITEST"
ISIN = "INE_TEST_0001"
UNIVERSE_ID = "nifty50_test"
VENUE_ID = "xnse_test"
MARKET_ID = "in_equity_test"
AS_OF = "2026-06-30"


def _build_synthetic_india_fixture(store: Store) -> None:
    """Insert one synthetic ISIN-identified, INR-denominated security.

    Every value is fabricated for this test only — never real NSE data, and
    never claimed as one. The point is to exercise the shared code paths with
    a genuinely non-U.S. identity shape, not to model a real company.
    """
    con = store._con

    con.execute(
        "INSERT INTO security_master "
        "(security_id, canonical_ticker, security_type, identity_status, source) "
        "VALUES (?,?,?,?,?)",
        [SECURITY_ID, TICKER, "common_stock", "verified_ticker_change", "test"],
    )
    con.execute(
        "INSERT INTO security_identity_assignments "
        "(universe_id, ticker, effective_start, effective_end, security_id, "
        "known_date, identity_status, source) "
        "VALUES (?, ?, '2025-01-01', NULL, ?, '2025-01-01', 'verified_ticker_change', 'test')",
        [UNIVERSE_ID, TICKER, SECURITY_ID],
    )
    con.execute(
        "INSERT INTO universe_membership "
        "(universe_id, ticker, security_id, effective_start, effective_end, "
        "known_date, end_known_date, source) "
        "VALUES (?, ?, ?, '2025-01-01', NULL, '2025-01-01', NULL, 'test')",
        [UNIVERSE_ID, TICKER, SECURITY_ID],
    )

    con.execute(
        "INSERT INTO markets "
        "(market_id, country, base_currency, timezone, default_venue_id, source) "
        "VALUES (?, 'IN', 'INR', 'Asia/Kolkata', ?, 'test')",
        [MARKET_ID, VENUE_ID],
    )
    con.execute(
        "INSERT INTO venues (venue_id, market_id, mic, name, source) "
        "VALUES (?, ?, 'XNSE', 'Synthetic NSE test venue', 'test')",
        [VENUE_ID, MARKET_ID],
    )
    con.execute(
        "INSERT INTO security_listings "
        "(security_id, venue_id, symbol, series, isin, security_type, currency, "
        "listed_start, listed_end, known_at, source) "
        "VALUES (?, ?, ?, 'EQ', ?, 'common_stock', 'INR', '2025-01-01', NULL, "
        "'2025-01-01', 'test')",
        [SECURITY_ID, VENUE_ID, TICKER, ISIN],
    )

    for day, close in [
        ("2026-06-25", 2480.0),
        ("2026-06-26", 2495.0),
        ("2026-06-29", 2510.0),
        ("2026-06-30", 2500.0),
    ]:
        con.execute(
            "INSERT INTO prices "
            "(ticker, security_id, provider_symbol, date, open, high, low, close, "
            "adj_close, volume, dividends, split_ratio, actions_complete, "
            "close_split_adjusted, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                TICKER, SECURITY_ID, TICKER, day,
                close, close, close, close, close,
                100_000, 0.0, 1.0, True, False, "test",
            ],
        )

    annual = [
        ("revenue", 500_000.0),
        ("operating_income", 90_000.0),
        ("cfo", 100_000.0),
        ("capex", 20_000.0),
        ("gross_profit", 200_000.0),
    ]
    snapshot = [
        ("total_assets", 800_000.0),
        ("stockholders_equity", 400_000.0),
        ("debt_total", 150_000.0),
        ("current_assets", 300_000.0),
        ("current_liabilities", 120_000.0),
        ("shares_out", 3_000.0),
        ("cash", 60_000.0),
    ]
    for statement, rows in (("income", annual), ("balance", snapshot)):
        for metric, value in rows:
            con.execute(
                "INSERT INTO fundamentals "
                "(ticker, issuer_id, security_id, period_end, as_of_date, "
                "fiscal_period, statement, metric, value, quarter_value, unit, source) "
                "VALUES (?, NULL, ?, '2026-03-31', '2026-05-15', 'FY2026', "
                "?, ?, ?, ?, 'INR', 'test')",
                [TICKER, SECURITY_ID, statement, metric, value, value],
            )


def test_synthetic_india_fixture_resolves_through_shared_identity_code(tmp_path) -> None:
    store = Store(tmp_path / "market-contracts.duckdb")
    try:
        _build_synthetic_india_fixture(store)

        assert store.security_id_for_ticker(TICKER, AS_OF) == SECURITY_ID

        labels = store.universe_identity_labels(UNIVERSE_ID, AS_OF)
        assert len(labels) == 1
        assert labels[0]["security_id"] == SECURITY_ID
        assert labels[0]["ticker"] == TICKER

        coverage = store.universe_data_coverage(UNIVERSE_ID, AS_OF)
        assert len(coverage) == 1
        assert coverage[0]["has_price_history"] is True
        assert coverage[0]["has_pit_fundamentals"] is True
        assert coverage[0]["latest_price_date"].isoformat() == "2026-06-30"
    finally:
        store.close()


def test_synthetic_india_fixture_scores_through_the_shared_factor_pipeline(tmp_path) -> None:
    """The exact `compute_composite` the U.S. reference build uses.

    No market_id, currency, or venue is passed to it — everything it reads
    comes from `security_id`/`ticker`/`as_of`, exactly as for a U.S. security.
    """
    store = Store(tmp_path / "market-contracts.duckdb")
    try:
        _build_synthetic_india_fixture(store)

        rows = compute_composite([TICKER], AS_OF, store, include_market_factors=False)
        assert len(rows) == 1
        row = rows[0]
        assert row.ticker == TICKER
        # Quality/Value ran and found real component data — a market-specific
        # code path that silently skipped a non-U.S. security would instead
        # leave these None with every component reported missing.
        assert row.quality_score is not None
        assert row.quality_components_available >= 2
        assert row.value_score is not None
        assert row.value_multiples_available >= 2
    finally:
        store.close()


def test_synthetic_india_fixture_passes_a_reparameterized_readiness_check(tmp_path) -> None:
    """`assess_us_readiness` accepts an overridden, non-U.S.-shaped policy.

    Its name is U.S.-specific; its behavior, exercised here, is not. Every
    threshold below is deliberately not the S&P 500 defaults (450-550
    members, benchmark 'SPY') — a Nifty-50-shaped policy instead.
    """
    store = Store(tmp_path / "market-contracts.duckdb")
    try:
        _build_synthetic_india_fixture(store)

        policy = USReadinessPolicy(
            universe_id=UNIVERSE_ID,
            benchmark_ticker=TICKER,
            minimum_universe_members=1,
            maximum_universe_members=5,
        )
        report = assess_us_readiness(
            AS_OF, purpose="historical_research", policy=policy, store=store
        )
        # This must not raise and must not silently fall back to sp500/SPY.
        # The member count is the proof: it reflects the one-security
        # synthetic universe, not the real ~503-member sp500 universe, so the
        # check genuinely ran against rules.universe_id, not a hardcoded one.
        membership_checks = [c for c in report.checks if c.check == "universe_membership"]
        assert membership_checks
        assert membership_checks[0].observed == "1 members"
        assert membership_checks[0].status == "pass"

        identity_checks = [c for c in report.checks if c.check == "stable_security_identity"]
        assert identity_checks
        assert identity_checks[0].observed == "1/1 members"
        assert identity_checks[0].status == "pass"
    finally:
        store.close()

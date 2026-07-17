from __future__ import annotations

import sys
from datetime import date
from http.client import IncompleteRead
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest
from tenacity import wait_none

from aios.factors import composite as composite_factor
from aios.factors import value as value_factor
from aios.factors.common import ttm_sum
from aios.factors.policy import BASELINE_FACTOR_WEIGHTS, REGIME_FACTOR_WEIGHTS
from aios.factors.quality import QualitySnapshot
from aios.factors.value import ValueSnapshot, compute_value_raw
from aios.ingest import edgar
from aios.ingest import fred as fred_ingest
from aios.macro.regime import GROWTH_SERIES, compute_regime
from aios.storage.store import Store


def _fundamental(
    ticker: str,
    period_end: str,
    as_of: str,
    metric: str,
    value: float,
    *,
    quarter_value: float | None = None,
    fiscal_period: str = "Q1_2023",
) -> dict:
    return {
        "ticker": ticker,
        "period_end": period_end,
        "as_of_date": as_of,
        "fiscal_period": fiscal_period,
        "statement": "income",
        "metric": metric,
        "value": value,
        "quarter_value": value if quarter_value is None else quarter_value,
        "unit": "USD",
        "source": "test",
    }


def test_pit_fundamentals_selects_only_known_latest_row(tmp_path):
    store = Store(tmp_path / "pit.duckdb")
    try:
        store.upsert_fundamentals(
            [
                _fundamental("TEST", "2023-12-31", "2024-02-01", "revenue", 100),
                _fundamental("TEST", "2023-12-31", "2024-03-01", "revenue", 110),
                _fundamental("TEST", "2023-12-31", "2025-01-01", "revenue", 120),
            ]
        )

        before_restatement = store.pit_fundamentals("TEST", "2024-02-15", ["revenue"])
        after_restatement = store.pit_fundamentals("TEST", "2024-12-31", ["revenue"])

        assert before_restatement[0]["value"] == 100
        assert after_restatement[0]["value"] == 110
    finally:
        store.close()


def test_price_upsert_preserves_provider_source(tmp_path):
    store = Store(tmp_path / "prices.duckdb")
    try:
        store.upsert_prices(
            [
                {
                    "ticker": "TEST",
                    "date": date(2024, 1, 2),
                    "open": 1,
                    "high": 2,
                    "low": 0.5,
                    "close": 1.5,
                    "adj_close": None,
                    "volume": 100,
                    "dividends": 0,
                    "split_ratio": 1,
                    "source": "stooq",
                }
            ]
        )

        assert store.price_on("TEST", "2024-01-02")["source"] == "stooq"
        assert store.latest_price_date("TEST") == date(2024, 1, 2)
    finally:
        store.close()


def test_ingest_history_records_success_and_failure(tmp_path):
    store = Store(tmp_path / "audit.duckdb")
    try:
        success_id = store.record_ingest(
            source="test",
            table_name="prices",
            rows_inserted=4,
        )
        failure_id = store.record_ingest(
            source="test",
            table_name="fundamentals",
            status="failed",
            error="fixture failure",
        )

        history = store.ingest_history(2)

        assert history[0]["run_id"] == failure_id
        assert history[0]["status"] == "failed"
        assert history[0]["error"] == "fixture failure"
        assert history[1]["run_id"] == success_id
        assert history[1]["rows_inserted"] == 4
    finally:
        store.close()


def test_data_quality_report_flags_legacy_ebitda(tmp_path):
    store = Store(tmp_path / "quality.duckdb")
    try:
        store.upsert_fundamentals(
            [
                _fundamental("TEST", "2023-12-31", "2024-02-01", "ebitda", 999),
            ]
        )

        report = {row["check"]: row for row in store.data_quality_report()}

        assert report["fundamentals_missing_as_of_date"]["status"] == "ok"
        assert report["legacy_mislabeled_ebitda"]["status"] == "warn"
        assert report["legacy_mislabeled_ebitda"]["count"] == 1
    finally:
        store.close()


def test_sec_ticker_override_wins_over_bad_master_map(monkeypatch):
    class FakeHttp:
        def get_json(self, _url):
            return {
                "0": {"cik_str": 2115436, "ticker": "XOM", "title": "Holdings"},
            }

    monkeypatch.setattr(edgar, "get_http", lambda: FakeHttp())

    assert edgar.load_ticker_cik_map()["XOM"] == 34088


def test_purge_legacy_ebitda_is_narrow(tmp_path):
    store = Store(tmp_path / "cleanup.duckdb")
    try:
        store.upsert_fundamentals(
            [
                _fundamental("TEST", "2023-12-31", "2024-02-01", "ebitda", 999),
                _fundamental("TEST", "2023-12-31", "2024-02-01", "revenue", 100),
            ]
        )

        assert store.purge_legacy_ebitda() == 1
        assert store.query("SELECT COUNT(*) AS n FROM fundamentals")[0]["n"] == 1
        assert store.query("SELECT metric FROM fundamentals")[0]["metric"] == "revenue"
    finally:
        store.close()


def test_value_score_requires_minimum_multiple_coverage(monkeypatch, tmp_path):
    store = Store(tmp_path / "coverage.duckdb")
    try:

        def fake_raw(ticker, as_of, store):
            return ValueSnapshot(ticker=ticker, as_of=str(as_of), pe=10)

        monkeypatch.setattr(value_factor, "compute_value_raw", fake_raw)
        snap = value_factor.compute_value_ranked(["TEST"], "2024-02-01", store)["TEST"]

        assert snap.multiples_available == 1
        assert snap.value_score is None
        assert "minimum_value_multiples:2" in snap.missing
    finally:
        store.close()


def test_composite_does_not_publish_one_sided_score(monkeypatch, tmp_path):
    store = Store(tmp_path / "composite-coverage.duckdb")
    try:
        monkeypatch.setattr(
            composite_factor,
            "compute_value_ranked",
            lambda tickers, as_of, store: {
                "TEST": ValueSnapshot(
                    ticker="TEST",
                    as_of=str(as_of),
                    value_score=80,
                    multiples_available=2,
                )
            },
        )
        monkeypatch.setattr(
            composite_factor,
            "compute_quality",
            lambda ticker, as_of, store: QualitySnapshot(
                ticker=ticker,
                as_of=str(as_of),
                roic=0.2,
            ),
        )

        row = composite_factor.compute_composite(["TEST"], "2024-02-01", store)[0]

        assert row.quality_components_available == 1
        assert row.quality_score is None
        assert row.value_score == 80
        assert row.qv_score is None
        assert row.grade == "N/A"
    finally:
        store.close()


def test_ttm_sum_uses_four_known_quarters(tmp_path):
    store = Store(tmp_path / "ttm.duckdb")
    try:
        rows = []
        for i, (period_end, quarter_value) in enumerate(
            [
                ("2023-03-31", 10),
                ("2023-06-30", 20),
                ("2023-09-30", 30),
                ("2023-12-31", 40),
            ],
            start=1,
        ):
            rows.append(
                _fundamental(
                    "TEST",
                    period_end,
                    "2024-02-01",
                    "revenue",
                    quarter_value,
                    quarter_value=quarter_value,
                    fiscal_period=f"Q{i}_2023",
                )
            )
        rows.append(
            _fundamental(
                "TEST",
                "2024-03-31",
                "2025-01-01",
                "revenue",
                999,
                quarter_value=999,
                fiscal_period="Q1_2024",
            )
        )
        store.upsert_fundamentals(rows)

        assert ttm_sum(store, "TEST", "2024-12-31", "revenue") == 100
    finally:
        store.close()


def test_value_derives_ebitda_instead_of_using_mislabeled_net_income(tmp_path):
    store = Store(tmp_path / "value.duckdb")
    try:
        rows = []
        periods = ["2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31"]
        for i, period_end in enumerate(periods, start=1):
            as_of = "2024-02-01"
            fp = f"Q{i}_2023"
            rows.extend(
                [
                    _fundamental(
                        "TEST", period_end, as_of, "operating_income", 10, fiscal_period=fp
                    ),
                    _fundamental("TEST", period_end, as_of, "depreciation", 2, fiscal_period=fp),
                    _fundamental("TEST", period_end, as_of, "eps_diluted", 1, fiscal_period=fp),
                    _fundamental("TEST", period_end, as_of, "cfo", 8, fiscal_period=fp),
                    _fundamental("TEST", period_end, as_of, "capex", -2, fiscal_period=fp),
                    _fundamental("TEST", period_end, as_of, "revenue", 100, fiscal_period=fp),
                ]
            )
        # This row represents the historical bug: net income was stored under
        # the ebitda metric. The value factor must not read it.
        rows.append(
            _fundamental(
                "TEST",
                "2023-12-31",
                "2024-02-01",
                "ebitda",
                999,
                fiscal_period="Q4_2023",
            )
        )
        rows.extend(
            [
                _fundamental("TEST", "2023-12-31", "2024-02-01", "shares_out", 10),
                _fundamental("TEST", "2023-12-31", "2024-02-01", "debt_total", 20),
                _fundamental("TEST", "2023-12-31", "2024-02-01", "cash", 5),
                _fundamental("TEST", "2023-12-31", "2024-02-01", "stockholders_equity", 50),
            ]
        )
        store.upsert_fundamentals(rows)
        store.upsert_prices(
            [
                {
                    "ticker": "TEST",
                    "date": "2024-01-31",
                    "open": 10,
                    "high": 10,
                    "low": 10,
                    "close": 10,
                    "adj_close": 10,
                    "volume": 100,
                    "dividends": 0,
                    "split_ratio": 1,
                    "source": "test",
                }
            ]
        )

        snap = compute_value_raw("TEST", "2024-02-01", store)

        assert snap.ttm_ebitda == 48
        assert snap.ev_ebitda == 115 / 48
    finally:
        store.close()


def _macro(
    series_id: str,
    observation_date: str,
    release_date: str,
    value: float,
    *,
    source: str = "test",
) -> dict:
    return {
        "series_id": series_id,
        "date": observation_date,
        "release_date": release_date,
        "value": value,
        "unit": "test",
        "source": source,
    }


def test_macro_upsert_requires_release_date(tmp_path):
    store = Store(tmp_path / "macro-required-release.duckdb")
    try:
        try:
            store.upsert_macro(
                [
                    {
                        "series_id": "GDP",
                        "date": "2024-01-01",
                        "value": 1,
                    }
                ]
            )
        except ValueError as exc:
            assert "release_date" in str(exc)
        else:
            raise AssertionError("macro rows without release_date must be rejected")
    finally:
        store.close()


def test_fred_fetch_preserves_observation_and_release_dates(monkeypatch):
    class FakeFred:
        def __init__(self, api_key):
            assert api_key == "test-key"

        def get_series_vintage_dates(self, series_id):
            assert series_id == "GDP"
            return [pd.Timestamp("2020-01-30")]

        def get_series_all_releases(self, series_id, realtime_start=None, realtime_end=None):
            assert series_id == "GDP"
            assert realtime_start == "2020-01-30"
            assert realtime_end == "2020-01-30"
            return pd.DataFrame(
                {
                    "date": [pd.Timestamp("2019-10-01")],
                    "realtime_start": [pd.Timestamp("2020-01-30")],
                    "value": [100.0],
                }
            )

    monkeypatch.setattr(fred_ingest.settings, "fred_api_key", "test-key")
    monkeypatch.setitem(sys.modules, "fredapi", SimpleNamespace(Fred=FakeFred))

    rows = fred_ingest.fetch_series_fred("GDP", realtime_start="2020-01-01")

    assert rows[0]["date"] == "2019-10-01"
    assert rows[0]["release_date"] == "2020-01-30"


def test_fred_fetch_chunks_oversized_vintage_histories(monkeypatch):
    calls: list[tuple[str, str]] = []

    class FakeFred:
        def __init__(self, api_key):
            assert api_key == "test-key"

        def get_series_vintage_dates(self, _series_id):
            return [pd.Timestamp(f"2020-01-0{day}") for day in range(1, 6)]

        def get_series_all_releases(self, _series_id, realtime_start, realtime_end):
            calls.append((realtime_start, realtime_end))
            return pd.DataFrame(
                {
                    "date": [pd.Timestamp("2019-10-01")],
                    "realtime_start": [pd.Timestamp(realtime_end)],
                    "value": [float(len(calls))],
                }
            )

    monkeypatch.setattr(fred_ingest.settings, "fred_api_key", "test-key")
    monkeypatch.setattr(fred_ingest, "FRED_VINTAGE_CHUNK_SIZE", 2)
    monkeypatch.setitem(sys.modules, "fredapi", SimpleNamespace(Fred=FakeFred))

    rows = fred_ingest.fetch_series_fred("DGS2")

    assert calls == [
        ("2020-01-01", "2020-01-02"),
        ("2020-01-03", "2020-01-04"),
        ("2020-01-05", "2020-01-05"),
    ]
    assert [row["release_date"] for row in rows] == [
        "2020-01-02",
        "2020-01-04",
        "2020-01-05",
    ]


def test_fred_fetch_retries_truncated_responses(monkeypatch):
    attempts = 0

    class FakeFred:
        def __init__(self, api_key):
            assert api_key == "test-key"

        def get_series_vintage_dates(self, _series_id):
            return [pd.Timestamp("2020-01-01")]

        def get_series_all_releases(self, _series_id, realtime_start, realtime_end):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise IncompleteRead(b"partial", 100)
            return pd.DataFrame(
                {
                    "date": [pd.Timestamp("2019-10-01")],
                    "realtime_start": [pd.Timestamp(realtime_end)],
                    "value": [1.0],
                }
            )

    monkeypatch.setattr(fred_ingest.settings, "fred_api_key", "test-key")
    monkeypatch.setattr(
        fred_ingest,
        "_fetch_release_window",
        fred_ingest._fetch_release_window.retry_with(wait=wait_none()),
    )
    monkeypatch.setitem(sys.modules, "fredapi", SimpleNamespace(Fred=FakeFred))

    rows = fred_ingest.fetch_series_fred("DGS2")

    assert attempts == 2
    assert rows[0]["release_date"] == "2020-01-01"


def test_macro_ingest_continues_after_one_series_fails(monkeypatch, tmp_path):
    store = Store(tmp_path / "macro-resilient.duckdb")
    calls: list[str] = []

    def fake_fetch(series_id, realtime_start=None, realtime_end=None):
        calls.append(series_id)
        if series_id == "BAD":
            raise ValueError("fixture failure")
        return [_macro(series_id, "2024-01-01", "2024-02-01", 1.0)]

    monkeypatch.setattr(fred_ingest, "get_store", lambda: store)
    monkeypatch.setattr(fred_ingest, "fetch_series_fred", fake_fetch)
    monkeypatch.setattr(fred_ingest, "fetch_treasury_yield_curve", lambda: [])
    try:
        with pytest.raises(fred_ingest.MacroIngestError, match="BAD"):
            fred_ingest.ingest_macro(["BAD", "GOOD"])

        assert calls == ["BAD", "GOOD"]
        assert store.query(
            "SELECT COUNT(*) AS n FROM macro WHERE series_id = 'GOOD'"
        )[0]["n"] == 1
        run = store.ingest_history(1)[0]
        assert run["status"] == "failed"
        assert run["rows_inserted"] == 1
        assert "BAD: fixture failure" in run["error"]
    finally:
        store.close()


def test_macro_pit_history_selects_vintage_known_on_date(tmp_path):
    store = Store(tmp_path / "macro-pit.duckdb")
    try:
        store.upsert_macro(
            [
                _macro("GDP", "2020-01-01", "2020-02-01", 100),
                _macro("GDP", "2020-01-01", "2020-04-01", 110),
                _macro("GDP", "2020-04-01", "2020-05-01", 120),
            ]
        )

        before_revision = store.pit_macro_history("GDP", "2020-03-01")
        after_revision = store.pit_macro_history("GDP", "2020-12-31")

        assert [(row["date"], row["value"]) for row in before_revision] == [
            (date(2020, 1, 1), 100.0)
        ]
        assert [(row["date"], row["value"]) for row in after_revision] == [
            (date(2020, 1, 1), 110.0),
            (date(2020, 4, 1), 120.0),
        ]
        assert store.pit_macro_latest(["GDP"], "2020-03-01")[0]["value"] == 100.0
        assert store.latest_macro_release_date("GDP", source="test") == date(2020, 5, 1)
    finally:
        store.close()


def test_old_macro_schema_is_migrated_and_excluded_from_pit(tmp_path):
    db_path = tmp_path / "macro-migration.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE macro (
                series_id VARCHAR NOT NULL,
                date DATE NOT NULL,
                value DOUBLE,
                unit VARCHAR,
                source VARCHAR,
                fetched_at TIMESTAMP,
                PRIMARY KEY (series_id, date)
            )
            """
        )
        con.execute(
            """
            INSERT INTO macro VALUES
            ('GDP', '2020-01-01', 100, 'test', 'fred', CURRENT_TIMESTAMP)
            """
        )
    finally:
        con.close()

    store = Store(db_path)
    try:
        columns = {row["column_name"] for row in store.query("DESCRIBE macro")}
        assert "release_date" in columns
        assert store.query("SELECT COUNT(*) AS n FROM macro_legacy")[0]["n"] == 1
        assert store.query(
            "SELECT source, release_date FROM macro WHERE series_id = 'GDP'"
        )[0] == {"source": "legacy_unversioned", "release_date": None}
        report = {row["check"]: row for row in store.data_quality_report()}
        assert report["macro_unversioned_rows"]["status"] == "fail"
        assert store.pit_macro_history("GDP", "2020-12-31") == []

        with pytest.raises(ValueError, match="replacements are missing"):
            store.purge_legacy_macro()
        store.upsert_macro([_macro("GDP", "2020-01-01", "2020-02-01", 101)])
        assert store.purge_legacy_macro() == 1
        assert store.query("SELECT COUNT(*) AS n FROM macro_legacy")[0]["n"] == 1
        assert store.pit_macro_history("GDP", "2020-12-31")[0]["value"] == 101.0
        store.close()

        store = Store(db_path)
        assert store.query(
            "SELECT COUNT(*) AS n FROM macro WHERE release_date IS NULL"
        )[0]["n"] == 0
        assert store.query("SELECT COUNT(*) AS n FROM macro_legacy")[0]["n"] == 1
        reopened_report = {row["check"]: row for row in store.data_quality_report()}
        assert reopened_report["macro_unversioned_rows"]["status"] == "ok"
    finally:
        store.close()


def test_macro_regime_uses_revision_only_after_release(tmp_path):
    store = Store(tmp_path / "regime.duckdb")
    try:
        store.upsert_macro(
            [
                _macro(GROWTH_SERIES, "2024-09-30", "2024-11-01", 2.0),
                _macro(GROWTH_SERIES, "2024-09-30", "2025-02-01", -1.0),
                _macro("CPIAUCSL", "2022-12-01", "2023-01-15", 95.0),
                _macro("CPIAUCSL", "2023-12-01", "2024-01-15", 100.0),
                _macro("CPIAUCSL", "2024-12-01", "2025-01-15", 105.0),
                _macro("T10Y2Y", "2024-12-31", "2024-12-31", -0.5),
                _macro("VIXCLS", "2024-12-31", "2024-12-31", 15.0),
                _macro("VIXCLS", "2025-01-31", "2025-02-01", 35.0),
                _macro("BAA10Y", "2024-12-31", "2024-12-31", 1.0),
            ]
        )

        before_revision = compute_regime("2024-12-31", store)
        after_revision = compute_regime("2025-03-01", store)

        assert before_revision.regime == "reflation"
        assert before_revision.growth_state == "expansion"
        assert after_revision.regime == "risk_off"
        assert after_revision.growth_state == "contraction"
        assert after_revision.metrics["inflation_yoy_pct"] == pytest.approx(5.0)
        assert after_revision.is_pit_ready
        assert all(
            release_date is None or release_date <= after_revision.as_of
            for release_date in after_revision.release_dates.values()
        )
    finally:
        store.close()


def test_regime_factor_weights_are_explicit_and_normalized():
    assert set(REGIME_FACTOR_WEIGHTS) == {
        "goldilocks",
        "reflation",
        "stagflation",
        "deflationary",
        "risk_off",
        "unknown",
    }
    for weights in REGIME_FACTOR_WEIGHTS.values():
        assert weights.quality >= 0
        assert weights.value >= 0
        assert weights.quality + weights.value == pytest.approx(1.0)


def test_composite_uses_pit_regime_weights(monkeypatch, tmp_path):
    store = Store(tmp_path / "regime-composite.duckdb")
    try:
        store.upsert_macro(
            [
                _macro(GROWTH_SERIES, "2024-09-30", "2024-11-01", 2.0),
                _macro(GROWTH_SERIES, "2024-09-30", "2025-02-01", -1.0),
                _macro("CPIAUCSL", "2022-12-01", "2023-01-15", 95.0),
                _macro("CPIAUCSL", "2023-12-01", "2024-01-15", 100.0),
                _macro("CPIAUCSL", "2024-12-01", "2025-01-15", 105.0),
                _macro("T10Y2Y", "2024-12-31", "2024-12-31", -0.5),
                _macro("VIXCLS", "2024-12-31", "2024-12-31", 15.0),
                _macro("VIXCLS", "2025-01-31", "2025-02-01", 35.0),
                _macro("BAA10Y", "2024-12-31", "2024-12-31", 1.0),
            ]
        )
        monkeypatch.setattr(
            composite_factor,
            "compute_value_ranked",
            lambda tickers, as_of, store: {
                "TEST": ValueSnapshot(
                    ticker="TEST",
                    as_of=str(as_of),
                    value_score=80,
                    multiples_available=2,
                )
            },
        )
        monkeypatch.setattr(
            composite_factor,
            "compute_quality",
            lambda ticker, as_of, store: QualitySnapshot(
                ticker=ticker,
                as_of=str(as_of),
                roic=0.2,
                fcf_margin=0.1,
            ),
        )

        before_revision = composite_factor.compute_composite(["TEST"], "2024-12-31", store)[0]
        after_revision = composite_factor.compute_composite(["TEST"], "2025-03-01", store)[0]

        assert before_revision.macro_regime == "reflation"
        assert before_revision.regime_pit_ready is True
        assert before_revision.quality_weight == pytest.approx(0.45)
        assert before_revision.value_weight == pytest.approx(0.55)
        assert before_revision.qv_score == pytest.approx(
            before_revision.quality_score * 0.45 + before_revision.value_score * 0.55
        )

        assert after_revision.macro_regime == "risk_off"
        assert after_revision.regime_pit_ready is True
        assert after_revision.quality_weight == pytest.approx(0.70)
        assert after_revision.value_weight == pytest.approx(0.30)
        assert after_revision.qv_score == pytest.approx(
            after_revision.quality_score * 0.70 + after_revision.value_score * 0.30
        )
        assert before_revision.qv_score != after_revision.qv_score
    finally:
        store.close()


def test_composite_uses_baseline_and_marks_missing_macro_regime(monkeypatch, tmp_path):
    store = Store(tmp_path / "unknown-regime-composite.duckdb")
    try:
        monkeypatch.setattr(
            composite_factor,
            "compute_value_ranked",
            lambda tickers, as_of, store: {
                "TEST": ValueSnapshot(
                    ticker="TEST",
                    as_of=str(as_of),
                    value_score=80,
                    multiples_available=2,
                )
            },
        )
        monkeypatch.setattr(
            composite_factor,
            "compute_quality",
            lambda ticker, as_of, store: QualitySnapshot(
                ticker=ticker,
                as_of=str(as_of),
                roic=0.2,
                fcf_margin=0.1,
            ),
        )

        row = composite_factor.compute_composite(["TEST"], "2024-02-01", store)[0]

        assert row.macro_regime == "unknown"
        assert row.regime_pit_ready is False
        assert row.quality_weight == BASELINE_FACTOR_WEIGHTS.quality
        assert row.value_weight == BASELINE_FACTOR_WEIGHTS.value
        assert row.qv_score == pytest.approx(
            row.quality_score * BASELINE_FACTOR_WEIGHTS.quality
            + row.value_score * BASELINE_FACTOR_WEIGHTS.value
        )
        assert "macro_regime_pit_unavailable" in row.missing
        assert "macro:growth" in row.missing
    finally:
        store.close()

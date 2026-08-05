"""Red contract for the successor forward-policy release.

This module is intentionally not named ``test_*.py``. Its assertions require
new factor evidence codes in files frozen by the active predecessor trial.
Rename it back into the collected suite only when that predecessor is archived
byte-for-byte and the successor policy bundle explicitly freezes the change.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from aios.factors import composite as composite_factor
from aios.factors import quality as quality_factor
from aios.factors import value as value_factor
from aios.factors.quality import QualitySnapshot
from aios.factors.value import ValueSnapshot
from aios.storage.store import Store


def test_industrial_quality_names_each_missing_component_input(monkeypatch) -> None:
    ttm = {
        "revenue": 100.0,
        "operating_income": 10.0,
        "cfo": None,
        "capex": 5.0,
        "gross_profit": None,
        "net_income": None,
    }
    instant = {
        "total_assets": 200.0,
        "stockholders_equity": 100.0,
        "debt_total": None,
        "current_assets": None,
        "current_liabilities": None,
        "shares_out": None,
    }
    monkeypatch.setattr(quality_factor.fc, "is_financials", lambda *args: False)
    monkeypatch.setattr(
        quality_factor.fc,
        "ttm_sum",
        lambda store, ticker, as_of, metric: ttm.get(metric),
    )
    monkeypatch.setattr(
        quality_factor.fc,
        "metric_value",
        lambda store, ticker, as_of, metric, use_quarter: instant.get(metric),
    )

    snapshot = quality_factor.compute_quality(
        "TEST",
        "2024-12-31",
        store=object(),
    )

    assert snapshot.roic is None
    assert snapshot.fcf_margin is None
    assert snapshot.gross_margin is None
    assert snapshot.piotroski_f is None
    assert "roic_missing_debt_total" in snapshot.missing
    assert "fcf_margin_missing_ttm_cfo" in snapshot.missing
    assert "gross_margin_missing_ttm_gross_profit" in snapshot.missing
    assert "piotroski_missing_ttm_net_income" in snapshot.missing


def test_quality_invalid_diagnostics_do_not_change_completeness_semantics(monkeypatch) -> None:
    ttm = {
        "revenue": 0.0,
        "operating_income": 10.0,
        "cfo": 5.0,
        "capex": 2.0,
        "gross_profit": 0.0,
        "net_income": 5.0,
    }
    instant = {
        "total_assets": 0.0,
        "stockholders_equity": 100.0,
        "debt_total": -100.0,
        "current_assets": 10.0,
        "current_liabilities": 5.0,
        "shares_out": 10.0,
    }
    monkeypatch.setattr(quality_factor.fc, "is_financials", lambda *args: False)
    monkeypatch.setattr(
        quality_factor.fc,
        "ttm_sum",
        lambda store, ticker, as_of, metric: ttm.get(metric),
    )
    monkeypatch.setattr(
        quality_factor.fc,
        "metric_value",
        lambda store, ticker, as_of, metric, use_quarter: instant.get(metric),
    )

    snapshot = quality_factor.compute_quality(
        "TEST",
        "2024-12-31",
        store=object(),
    )

    # The pre-existing completeness flag is based on presence, not validity.
    assert snapshot.inputs_complete is True
    assert snapshot.roic is None
    assert snapshot.fcf_margin is None
    assert snapshot.gross_margin is None
    assert snapshot.piotroski_f is None
    assert "roic_invalid_invested_capital_nonpositive" in snapshot.missing
    assert "fcf_margin_invalid_ttm_revenue_nonpositive" in snapshot.missing
    assert "gross_margin_invalid_ttm_gross_profit_zero" in snapshot.missing
    assert "gross_margin_invalid_ttm_revenue_nonpositive" in snapshot.missing
    assert "piotroski_invalid_total_assets_nonpositive" in snapshot.missing


def test_financial_quality_names_bank_component_causes(monkeypatch) -> None:
    ttm = {
        "net_income": None,
        "revenue": 0.0,
        "cfo": 4.0,
        "capex": 1.0,
    }
    instant = {
        "stockholders_equity": 0.0,
        "total_assets": 0.0,
        "shares_out": 10.0,
    }
    monkeypatch.setattr(quality_factor.fc, "is_financials", lambda *args: True)
    monkeypatch.setattr(
        quality_factor.fc,
        "ttm_sum",
        lambda store, ticker, as_of, metric: ttm.get(metric),
    )
    monkeypatch.setattr(
        quality_factor.fc,
        "metric_value",
        lambda store, ticker, as_of, metric, use_quarter: instant.get(metric),
    )

    snapshot = quality_factor.compute_quality(
        "BANK",
        "2024-12-31",
        store=object(),
    )

    assert snapshot._is_financials is True
    assert "bank_roe_missing_ttm_net_income" in snapshot.missing
    assert "bank_roe_invalid_stockholders_equity_nonpositive" in snapshot.missing
    assert "bank_equity_ratio_invalid_total_assets_nonpositive" in snapshot.missing
    assert "bank_net_margin_invalid_ttm_revenue_nonpositive" in snapshot.missing
    assert "piotroski_invalid_total_assets_nonpositive" in snapshot.missing


def test_value_names_each_unavailable_multiple_cause(monkeypatch) -> None:
    instant = {
        "shares_out": 10.0,
        "stockholders_equity": -10.0,
        "debt_total": 20.0,
        "cash": 5.0,
    }
    ttm = {
        "eps_diluted": None,
        "operating_income": 10.0,
        "depreciation": None,
        "cfo": 5.0,
        "capex": 10.0,
        "revenue": 0.0,
    }
    monkeypatch.setattr(value_factor.fc, "latest_price", lambda *args: 10.0)
    monkeypatch.setattr(
        value_factor.fc,
        "metric_value",
        lambda store, ticker, as_of, metric, use_quarter: instant.get(metric),
    )
    monkeypatch.setattr(
        value_factor.fc,
        "ttm_sum",
        lambda store, ticker, as_of, metric: ttm.get(metric),
    )

    snapshot = value_factor.compute_value_ranked(
        ["TEST"],
        "2024-12-31",
        store=object(),
    )["TEST"]

    # Preserve the old presence-only completeness result and score gate.
    assert snapshot.inputs_complete is True
    assert snapshot.multiples_available == 0
    assert snapshot.value_score is None
    assert "pe_missing_ttm_eps" in snapshot.missing
    assert "ev_ebitda_missing_ttm_depreciation" in snapshot.missing
    assert "p_fcf_invalid_ttm_fcf_nonpositive" in snapshot.missing
    assert "ev_sales_invalid_ttm_revenue_nonpositive" in snapshot.missing
    assert "p_b_invalid_stockholders_equity_nonpositive" in snapshot.missing
    assert "minimum_value_multiples:2" in snapshot.missing


def test_diagnostic_detail_cannot_change_scores_or_ranks(monkeypatch, tmp_path) -> None:
    store = Store(tmp_path / "diagnostic-invariance.duckdb")
    qualities = {
        "A": QualitySnapshot(
            ticker="A",
            as_of="2024-12-31",
            roic=0.20,
            fcf_margin=0.10,
            missing=["gross_margin_missing_ttm_gross_profit"],
        ),
        "B": QualitySnapshot(
            ticker="B",
            as_of="2024-12-31",
            roic=0.10,
            fcf_margin=0.05,
            missing=["gross_margin_missing_ttm_gross_profit"],
        ),
    }
    values = {
        "A": ValueSnapshot(
            ticker="A",
            as_of="2024-12-31",
            value_score=80.0,
            multiples_available=2,
            missing=["p_b_missing_stockholders_equity"],
        ),
        "B": ValueSnapshot(
            ticker="B",
            as_of="2024-12-31",
            value_score=40.0,
            multiples_available=2,
            missing=["p_b_missing_stockholders_equity"],
        ),
    }
    regime = SimpleNamespace(
        as_of="2024-12-31",
        is_pit_ready=False,
        regime="unknown",
        missing=[],
    )
    monkeypatch.setattr(
        composite_factor,
        "compute_quality",
        lambda ticker, as_of, store: qualities[ticker],
    )
    monkeypatch.setattr(
        composite_factor,
        "compute_value_ranked",
        lambda tickers, as_of, store: values,
    )
    try:
        detailed = composite_factor.compute_composite(
            ["A", "B"],
            "2024-12-31",
            store,
            regime_snapshot=regime,
        )
        for snapshot in qualities.values():
            snapshot.missing = []
        no_quality_detail = deepcopy(values)
        for snapshot in no_quality_detail.values():
            snapshot.missing = []
        monkeypatch.setattr(
            composite_factor,
            "compute_value_ranked",
            lambda tickers, as_of, store: no_quality_detail,
        )
        plain = composite_factor.compute_composite(
            ["A", "B"],
            "2024-12-31",
            store,
            regime_snapshot=regime,
        )
    finally:
        store.close()

    def score_projection(rows):
        return [
            (
                row.ticker,
                row.quality_score,
                row.value_score,
                row.qv_score,
                row.grade,
                row.quality_rank,
                row.value_rank,
                row.qv_rank,
            )
            for row in rows
        ]

    assert score_projection(detailed) == score_projection(plain)
    assert detailed[0].missing != plain[0].missing
    assert "q:gross_margin_missing_ttm_gross_profit" in detailed[0].missing
    assert "v:p_b_missing_stockholders_equity" in detailed[0].missing

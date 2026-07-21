from __future__ import annotations

import pytest

from aios.dashboard_copy import (
    MODEL_QV_LABEL,
    MODEL_QVML_LABEL,
    VIEW_OPTIONS,
    VIEW_PAPER,
    friendly_missing_reasons,
    friendly_missing_summary,
    friendly_regime,
    model_key,
)


def test_dashboard_includes_plain_language_paper_monitor() -> None:
    assert VIEW_PAPER in VIEW_OPTIONS
    assert "Paper Monitor" in VIEW_PAPER


def test_dashboard_model_labels_map_to_internal_keys() -> None:
    assert model_key(MODEL_QV_LABEL) == "qv"
    assert model_key(MODEL_QVML_LABEL) == "qvml"
    with pytest.raises(ValueError, match="unknown dashboard model label"):
        model_key("Buy the best stocks")


def test_dashboard_regime_codes_are_plain_language() -> None:
    assert friendly_regime("reflation") == "growth with higher inflation"
    assert friendly_regime("risk_off") == "stressed or risk-averse markets"
    assert friendly_regime(None) == "unclear because some economic data is missing"


def test_dashboard_missing_reasons_hide_internal_codes_without_losing_meaning() -> None:
    labels = friendly_missing_reasons(
        [
            "minimum_quality_components:2",
            "q:ttm_cfo",
            "market:minimum_price_observations:253",
            "market:corporate_actions_unverified",
        ]
    )

    assert labels == [
        "Fewer than 2 business-quality measures were available",
        "Missing business-quality data: operating cash flow for the last 12 months",
        "Fewer than 253 usable trading days were available",
        "Dividend and stock-split history has not been fully verified",
    ]
    assert all("_" not in label and not label.startswith(("q:", "market:")) for label in labels)


def test_dashboard_missing_summary_is_bounded() -> None:
    summary = friendly_missing_summary(
        ["q:ttm_revenue", "q:ttm_cfo", "v:shares_out", "v:cash"],
        limit=2,
    )

    assert summary.endswith("+2 more")
    assert "ttm_" not in summary

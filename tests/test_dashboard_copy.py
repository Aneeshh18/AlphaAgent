from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from aios.dashboard_copy import (
    MODEL_QV_LABEL,
    MODEL_QVML_LABEL,
    VIEW_HOME,
    VIEW_OPTIONS,
    VIEW_PAPER,
    VIEW_SYSTEM,
    company_symbol_label,
    coverage_value,
    display_date,
    friendly_missing_reasons,
    friendly_missing_summary,
    friendly_regime,
    model_key,
    us_certification_freshness_message,
    us_eod_freshness_message,
)


def test_dashboard_includes_plain_language_paper_monitor() -> None:
    assert VIEW_PAPER in VIEW_OPTIONS
    assert "Portfolio Monitor" in VIEW_PAPER


def test_dashboard_has_a_separate_system_control_workspace() -> None:
    assert VIEW_SYSTEM in VIEW_OPTIONS
    assert VIEW_OPTIONS.index(VIEW_SYSTEM) > VIEW_OPTIONS.index(VIEW_PAPER)


def test_dashboard_opens_on_clean_operating_overview() -> None:
    assert VIEW_OPTIONS[0] == VIEW_HOME == "Overview"
    assert all(not label.startswith(("📈", "🔎", "🧪", "📖")) for label in VIEW_OPTIONS)


def test_dashboard_formats_dates_for_people() -> None:
    assert display_date("2026-07-20") == "Jul 20, 2026"
    assert display_date(None) == "Not available"
    assert display_date("not-a-date") == "not-a-date"


def test_dashboard_uses_reviewed_company_name_with_symbol() -> None:
    assert company_symbol_label("Apple Inc.", "aapl") == "Apple Inc. (AAPL)"
    assert company_symbol_label(None, "aapl") == "AAPL"
    assert company_symbol_label("AAPL", "AAPL") == "AAPL"


def test_dashboard_parses_coverage_without_trusting_display_percentage() -> None:
    assert coverage_value("500/503 (12.3%)") == pytest.approx((500, 503, 99.4035785))
    assert coverage_value("503 / 503 members") == (503, 503, 100.0)
    assert coverage_value("not available") is None
    assert coverage_value("504/503") is None


def test_dashboard_readiness_uses_latest_certified_decision_close() -> None:
    dashboard = Path("src/aios/dashboard.py").read_text(encoding="utf-8")

    assert "decision_date = latest_paper_decision_date(store)" in dashboard
    assert "assess_us_readiness(decision_date, purpose=\"paper\", store=store)" in dashboard
    assert "Certified decision close" in dashboard
    assert "found no change through" in dashboard


def test_dashboard_releases_duckdb_between_cached_reads() -> None:
    dashboard = Path("src/aios/dashboard.py").read_text(encoding="utf-8")

    assert "store_scope(read_only=True)" in dashboard
    assert "close_global_store()" in dashboard
    assert "get_store()" not in dashboard


def test_dashboard_explains_us_freshness_instead_of_matching_local_midnight() -> None:
    current, message = us_eod_freshness_message(
        "2026-07-22",
        now=datetime(2026, 7, 24, 1, 5, tzinfo=ZoneInfo("Asia/Kolkata")),
    )
    awaiting, awaiting_message = us_eod_freshness_message(
        "2026-07-22",
        now=datetime(2026, 7, 24, 2, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    assert current is True
    assert "latest completed U.S. session" in message
    assert awaiting is False
    assert "Jul 23, 2026" in awaiting_message


def test_dashboard_separates_safe_certification_from_raw_source_freshness() -> None:
    current, current_message = us_certification_freshness_message(
        "2026-07-23",
        now=datetime(2026, 7, 24, 21, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
    )
    behind, behind_message = us_certification_freshness_message(
        "2026-07-22",
        now=datetime(2026, 7, 24, 21, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    assert current is True
    assert "latest completed U.S. session" in current_message
    assert behind is False
    assert "1 market session behind" in behind_message
    assert "new paper decisions remain paused" in behind_message


def test_dashboard_overview_is_source_backed_and_not_presented_as_live_trading() -> None:
    dashboard = Path("src/aios/dashboard.py").read_text(encoding="utf-8")

    assert "Executive Research Dashboard" in dashboard
    assert "Proposed Research Basket" in dashboard
    assert "Proposal composition only" in dashboard
    assert "Company Filings (PIT)" in dashboard
    assert "Simulation only" in dashboard


def test_dashboard_uses_dark_institutional_theme() -> None:
    config = Path(".streamlit/config.toml").read_text(encoding="utf-8")

    assert 'base = "dark"' in config
    assert 'backgroundColor = "#07111F"' in config
    assert 'primaryColor = "#2DD4BF"' in config


def test_system_control_reads_real_independent_incident_history() -> None:
    dashboard = Path("src/aios/dashboard.py").read_text(encoding="utf-8")

    assert "incident_store = get_alert_store()" in dashboard
    assert '"Unresolved incidents"' in dashboard
    assert "System failures are written to an independent local incident ledger" in dashboard
    assert "aios alert-show INCIDENT_REF" in dashboard


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

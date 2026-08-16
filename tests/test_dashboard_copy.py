from __future__ import annotations

import re
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
    assert VIEW_PAPER == "Paper Trial"


def test_dashboard_routes_pending_proposals_through_read_only_review() -> None:
    dashboard = Path("src/aios/dashboard.py").read_text(encoding="utf-8")

    assert "registered_in_forward" in dashboard
    assert "aios paper-review --proposal" in dashboard
    assert "aios paper-execute --proposal" not in dashboard
    assert '{"expired", "invalid"}' in dashboard


def test_dashboard_has_a_separate_system_control_workspace() -> None:
    assert VIEW_SYSTEM in VIEW_OPTIONS
    assert VIEW_OPTIONS.index(VIEW_SYSTEM) > VIEW_OPTIONS.index(VIEW_PAPER)


def test_dashboard_opens_on_clean_operating_overview() -> None:
    assert VIEW_OPTIONS[0] == VIEW_HOME == "Today"
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
    assert 'assess_us_readiness(decision_date, purpose="paper", store=store)' in dashboard
    assert "Certified decision close" in dashboard
    assert '"Membership evidence"' in dashboard
    assert "latest_universe_attestation['requested_coverage_through']" in dashboard


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

    assert "AIOS" in dashboard
    assert "Overview" in dashboard
    assert "Research readiness" in dashboard
    assert "Paper-trial progress" in dashboard
    assert "Current proposal targets" in dashboard
    assert "Open reviews" in dashboard
    assert "Simulation only · No broker" in dashboard
    assert "build_home_view_model(report, monitor, operations)" in dashboard
    assert "load_operating_summary()" in dashboard


def test_dashboard_home_uses_progressive_disclosure_and_one_safe_action() -> None:
    dashboard = Path("src/aios/dashboard.py").read_text(encoding="utf-8")
    components = Path("src/aios/dashboard_components.py").read_text(encoding="utf-8")

    assert 'label="Priority action"' in dashboard
    assert "render_action_notice(" in dashboard
    assert 'key=f"{key}_cta"' in components
    assert 'type="primary"' in components
    assert 'with st.expander(f"View proposal targets' not in dashboard
    assert 'with st.expander("View data health' not in dashboard
    assert "paper-execute --proposal" not in dashboard


def test_dashboard_research_renders_only_the_selected_surface() -> None:
    dashboard = Path("src/aios/dashboard.py").read_text(encoding="utf-8")

    assert "def _research_surface_selector()" in dashboard
    assert "st.segmented_control(" in dashboard
    assert "default=None" in dashboard
    assert 'key="surface"' in dashboard
    assert 'research_surface == "Opportunity Map"' in dashboard
    assert 'research_surface == "Data Coverage"' in dashboard
    assert "st.tabs(" not in dashboard
    assert "show_market_factors=use_qvml" in dashboard
    assert "if show_market_factors:" in dashboard


def test_dashboard_preserves_debuggable_workspace_state_in_the_url() -> None:
    dashboard = Path("src/aios/dashboard.py").read_text(encoding="utf-8")

    assert "st.query_params" in dashboard
    assert "def _hydrate_widget_from_url(" in dashboard
    assert "def _persist_widget_query(" in dashboard
    assert 'requested_view_slug = raw_view if raw_view in _VIEWS_BY_SLUG else "today"' in dashboard
    assert 'st.query_params["view"] = view_slug' in dashboard
    for key in ("date", "model", "surface", "company"):
        assert f'_hydrate_widget_from_url("{key}"' in dashboard
        assert f'_persist_widget_query("{key}"' in dashboard
    assert '_hydrate_widget_from_url("q"' in dashboard
    assert '_persist_widget_query("q"' in dashboard
    assert "_aios_url_seen_" in dashboard


def test_dashboard_uses_bone_specimen_theme_with_light_navigation() -> None:
    config = Path(".streamlit/config.toml").read_text(encoding="utf-8")

    assert 'base = "light"' in config
    assert 'backgroundColor = "#F0ECE1"' in config
    assert 'primaryColor = "#3C4A2A"' in config
    assert "baseFontSize = 17" in config
    assert "[theme.sidebar]" in config
    assert 'backgroundColor = "#E8E2D2"' in config
    assert 'borderColor = "#B3A890"' in config


def test_dashboard_visual_system_keeps_columns_symmetric_and_status_colors_semantic() -> None:
    dashboard = Path("src/aios/dashboard.py").read_text(encoding="utf-8")
    components = Path("src/aios/dashboard_components.py").read_text(encoding="utf-8")
    stylesheet = Path("src/aios/dashboard.css").read_text(encoding="utf-8")

    assert "@media (max-width: 1280px)" not in dashboard
    assert "apply_design_system()" in dashboard
    assert 'Path(__file__).with_name("dashboard.css")' in components
    assert "--aios-canvas: #f0ece1" in stylesheet
    assert "--aios-clay: #52633c" in stylesheet
    assert "--aios-success: #2f6b3f" in stylesheet
    assert "html {\n    font-size: 17px" in stylesheet
    assert "linear-gradient(" not in stylesheet
    assert "radial-gradient(" not in stylesheet
    assert "transition: all" not in stylesheet


def test_dashboard_visual_system_has_no_sub_14px_interface_text() -> None:
    stylesheet = Path("src/aios/dashboard.css").read_text(encoding="utf-8")
    rem_sizes = [float(value) for value in re.findall(r"font-size:\s*([0-9.]+)rem", stylesheet)]
    pixel_sizes = [float(value) for value in re.findall(r"font-size:\s*([0-9.]+)px", stylesheet)]

    # The mobile root is 16px, so 0.875rem is exactly 14px.
    assert rem_sizes and min(rem_sizes) >= 0.875
    assert not pixel_sizes or min(pixel_sizes) >= 14


def test_dashboard_hides_stale_workspace_content_while_new_results_load() -> None:
    dashboard = Path("src/aios/dashboard.py").read_text(encoding="utf-8")
    stylesheet = Path("src/aios/dashboard.css").read_text(encoding="utf-8")

    assert 'key="overview_workspace"' in dashboard
    assert '[data-stale="true"]' in stylesheet
    assert "visibility: hidden !important" in stylesheet
    assert "pointer-events: none !important" in stylesheet
    assert '[data-testid="stStatusWidget"]' in stylesheet


def test_dashboard_overview_uses_independent_vertical_panel_stacks() -> None:
    dashboard = Path("src/aios/dashboard.py").read_text(encoding="utf-8")
    stylesheet = Path("src/aios/dashboard.css").read_text(encoding="utf-8")

    assert 'key="home_layout"' in dashboard
    assert "target_col, incident_col = st.columns" not in dashboard
    assert ".st-key-home_paper .aios-step-copy > p" in stylesheet
    assert "Recorded simulations" not in dashboard


def test_dashboard_dependency_matches_the_tested_streamlit_contract() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

    assert '"streamlit>=1.58.0,<2.0"' in pyproject


def test_system_control_reads_real_independent_case_and_incident_history() -> None:
    dashboard = Path("src/aios/dashboard.py").read_text(encoding="utf-8")

    assert "load_operations_evidence_read_only(operations_path)" in dashboard
    assert "get_alert_store" not in dashboard
    assert '"Open reviews"' in dashboard
    assert 'state["incident_summary"] = operations["incident_summary"]' in dashboard
    assert 'state["anomaly_cases"] = operations["anomaly_cases"]' in dashboard
    assert (
        'state["anomaly_case_summary"] = operations["anomaly_case_summary"]'
        in dashboard
    )
    assert 'state["notification_summary"] = operations["notification_summary"]' in dashboard
    assert "External email is off. Incidents are still saved locally" in dashboard
    assert '"Email alerts"' in dashboard
    assert "email_worker_enabled" in dashboard
    assert '"Incidents"' in dashboard
    assert "Alert delivery" in dashboard
    assert "Data review cases" in dashboard
    assert "aios anomaly-show CASE_REF" in dashboard
    assert "aios alert-show INCIDENT_REF" in dashboard
    assert "aios notification-show NOTIFICATION_REF" in dashboard
    assert "sqlite3.Error" in dashboard


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

from __future__ import annotations

from pathlib import Path

import pytest

from aios.config import settings

streamlit_testing = pytest.importorskip("streamlit.testing.v1")
AppTest = streamlit_testing.AppTest


_DUCKDB_PATH = (
    settings.duckdb_path
    if settings.duckdb_path.is_absolute()
    else settings.project_root / settings.duckdb_path
)
_DASHBOARD_PATH = Path("src/aios/dashboard.py")
_SIDEBAR_MARKER = (
    "# ----------------------------------------------------------------------\n"
    "# Sidebar\n"
    "# ----------------------------------------------------------------------\n"
)
_SHA256_A = "a" * 64
_SHA256_B = "b" * 64
_SHA256_C = "c" * 64
_SHA256_D = "d" * 64
_SHA256_E = "e" * 64


def _paper_monitor() -> dict:
    return {
        "exists": True,
        "account_path": "/tmp/aios-paper-account.json",
        "account_payload_sha256": _SHA256_A,
        "summary": {
            "equity": 100_000.0,
            "cash": 100_000.0,
            "holdings": [],
            "drawdown": 0.0,
            "last_market_date": None,
            "execution_count": 0,
            "transaction_costs": 0.0,
            "curve": [],
            "transaction_cost_policy": {
                "commission_bps": 1.0,
                "slippage_bps": 5.0,
            },
            "tax_policy": {
                "short_term_rate": 0.0,
                "long_term_rate": 0.0,
                "dividend_rate": 0.0,
            },
        },
        "proposal": {
            "proposal_id": "proposal-app-test",
            "status": "approved_for_supervised_simulation",
            "already_simulated": False,
            "registered_in_forward": True,
            "timing": {
                "status": "waiting_for_scheduled_close",
                "detail": "The scheduled decision close has not passed.",
            },
            "decision_date": "2026-07-27",
            "scheduled_simulation_date": "2026-07-28",
            "factor_eligible_count": 302,
            "sector_classification": "reviewed broad business group",
            "targets": [
                {
                    "factor_rank": 1,
                    "ticker": "AAA",
                    "target_weight": 0.6,
                    "sector": "Technology",
                    "qv_score": 82.3,
                },
                {
                    "factor_rank": 2,
                    "ticker": "BBB",
                    "target_weight": 0.3,
                    "sector": "Industrials",
                    "qv_score": 77.3,
                },
            ],
        },
        "proposal_path": "/tmp/aios-paper-proposal.json",
        "proposal_payload_sha256": _SHA256_B,
        "forward": {
            "ready": True,
            "trial_id": "forward-app-test",
            "registered_proposals": 1,
            "issues": [],
        },
        "trial_path": "/tmp/aios-forward-trial.json",
        "trial_payload_sha256": _SHA256_C,
    }


def _complete_stress_report() -> dict:
    return {
        "schema_version": 1,
        "kind": "aios.paper.proposal_stress_review",
        "payload": {
            "report_id": "stress-app-test",
            "report_generation_status": "complete",
            "scenario_bundle": {
                "bundle_id": "us-equity-reference-v1",
                "bundle_sha256": _SHA256_D,
            },
            "source_code": {"source_bundle_sha256": _SHA256_E},
            "evidence": {
                "target_evidence_sha256": "f" * 64,
                "blockers": [],
            },
            "analysis": {
                "report_generation_status": "complete",
                "scenarios": [
                    {
                        "scenario_id": "broad_mark_48",
                        "label": "Broad market decline",
                        "result_kind": "deterministic_mark_shock",
                        "status": "calculated",
                        "portfolio_loss": 48_000.0,
                        "portfolio_loss_pct": 0.48,
                        "stressed_drawdown": -0.48,
                        "blockers": [],
                        "positions": [
                            {
                                "ticker": "AAA",
                                "sector": "Technology",
                                "loss": 33_000.0,
                                "loss_pct_of_starting_equity": 0.33,
                            },
                            {
                                "ticker": "BBB",
                                "sector": "Industrials",
                                "loss": 15_000.0,
                                "loss_pct_of_starting_equity": 0.15,
                            },
                        ],
                        "sector_contributions": [
                            {
                                "sector": "Technology",
                                "loss": 33_000.0,
                                "loss_pct_of_starting_equity": 0.33,
                            },
                            {
                                "sector": "Industrials",
                                "loss": 15_000.0,
                                "loss_pct_of_starting_equity": 0.15,
                            },
                        ],
                        "reference_limit_findings": [
                            {
                                "finding_id": "drawdown_reference",
                                "message": (
                                    "Hypothetical drawdown is beyond the sandbox "
                                    "reference limit."
                                ),
                                "observed": 0.48,
                                "limit": 0.25,
                                "unit": "fraction",
                            }
                        ],
                    },
                    {
                        "scenario_id": "volatility_proxy",
                        "label": "Volatility and correlation proxy",
                        "result_kind": "statistical_loss_proxy",
                        "status": "calculated",
                        "portfolio_loss": 21_000.0,
                        "portfolio_loss_pct": 0.21,
                        "stressed_drawdown": None,
                        "blockers": [],
                    },
                ],
            },
        },
        "payload_sha256": "9" * 64,
    }


def _dashboard_source_with_paper_fakes(
    *,
    stress_report: dict | None,
    stress_error: str | None = None,
    monitor: dict | None = None,
    review_result: dict | None = None,
    execute_result: dict | None = None,
    execute_raises: str | None = None,
) -> str:
    """Inject deterministic read-only loaders before dashboard routing.

    AppTest executes the dashboard as a standalone script, so replacing the
    loaders after their definitions is the narrowest way to characterize the
    real Paper Trial composition without reading mutable local paper files.
    ``review_result``/``execute_result``/``execute_raises`` fake the one write
    path the dashboard has, so a test can drive it without ever touching the
    real paper account or the real project maintenance lease.
    """
    source = _DASHBOARD_PATH.read_text(encoding="utf-8")
    assert source.count(_SIDEBAR_MARKER) == 1
    monitor = monitor if monitor is not None else _paper_monitor()
    readiness = {
        "ready": True,
        "certified_research_from": "2023-08-01",
        "certified_research_through": "2026-07-27",
        "checks": [],
    }
    if stress_error is None:
        stress_loader = (
            "def load_proposal_stress_review(**kwargs):\n"
            "    expected = {\n"
            f"        'account_path': {monitor['account_path']!r},\n"
            f"        'account_payload_sha256': {_SHA256_A!r},\n"
            f"        'proposal_path': {monitor['proposal_path']!r},\n"
            f"        'proposal_payload_sha256': {_SHA256_B!r},\n"
            f"        'trial_path': {monitor['trial_path']!r},\n"
            f"        'trial_payload_sha256': {_SHA256_C!r},\n"
            f"        'scenario_bundle_sha256': {_SHA256_D!r},\n"
            f"        'source_bundle_sha256': {_SHA256_E!r},\n"
            "    }\n"
            "    if kwargs != expected:\n"
            "        raise AssertionError(f'unexpected stress cache identity: {kwargs!r}')\n"
            f"    return {stress_report!r}\n"
        )
    else:
        stress_loader = (
            "def load_proposal_stress_review(**kwargs):\n"
            f"    raise RuntimeError({stress_error!r})\n"
        )
    overrides = (
        "\n# AppTest-only deterministic loader overrides.\n"
        f"def load_us_readiness():\n    return {readiness!r}\n\n"
        f"def load_paper_monitor():\n    return {monitor!r}\n\n"
        "def load_identity_labels(as_of):\n"
        "    if as_of != '2026-07-27':\n"
        "        raise AssertionError(f'unexpected identity date: {as_of!r}')\n"
        "    return {'AAA': 'Alpha Systems', 'BBB': 'Beta Industries'}\n\n"
        "class _AppTestScenarioBundle:\n"
        f"    payload_sha256 = {_SHA256_D!r}\n\n"
        "def load_scenario_bundle():\n"
        "    return _AppTestScenarioBundle()\n\n"
        "def build_stress_source_identity(project_root):\n"
        f"    return {{'source_bundle_sha256': {_SHA256_E!r}}}\n\n"
        f"{stress_loader}\n"
    )
    if review_result is not None:
        overrides += (
            "def review_paper_proposal_execution(account_path, proposal_path, store):\n"
            f"    return {review_result!r}\n\n"
        )
    if execute_raises is not None:
        overrides += (
            "def _execute_paper_proposal_from_dashboard(account_path, proposal_path):\n"
            f"    raise ValueError({execute_raises!r})\n\n"
        )
    elif execute_result is not None:
        overrides += (
            "def _execute_paper_proposal_from_dashboard(account_path, proposal_path):\n"
            f"    return {execute_result!r}\n\n"
        )
    return source.replace(_SIDEBAR_MARKER, overrides + _SIDEBAR_MARKER, 1)


def _paper_app(
    *,
    stress_report: dict | None,
    stress_error: str | None = None,
    monitor: dict | None = None,
    review_result: dict | None = None,
    execute_result: dict | None = None,
    execute_raises: str | None = None,
) -> AppTest:
    app = AppTest.from_string(
        _dashboard_source_with_paper_fakes(
            stress_report=stress_report,
            stress_error=stress_error,
            monitor=monitor,
            review_result=review_result,
            execute_result=execute_result,
            execute_raises=execute_raises,
        ),
        default_timeout=120,
    )
    app.query_params["view"] = ["paper"]
    return app


def _execution_ready_monitor(*, already_simulated: bool = False) -> dict:
    monitor = _paper_monitor()
    monitor["proposal"] = dict(monitor["proposal"])
    monitor["proposal"]["already_simulated"] = already_simulated
    monitor["proposal"]["timing"] = {
        "status": "execution_window_open",
        "detail": "The prospective window is open.",
    }
    return monitor


def _rendered_values(app: AppTest) -> str:
    return "\n".join(
        str(element.value)
        for element_type in (
            app.markdown,
            app.caption,
            app.info,
            app.warning,
            app.error,
            app.success,
            app.code,
        )
        for element in element_type
    )


def test_paper_stress_panel_renders_advisory_results_without_an_execution_cta() -> None:
    app = _paper_app(stress_report=_complete_stress_report()).run()

    assert not app.exception
    rendered = _rendered_values(app)
    assert "Proposal downside review" in rendered
    assert "Proposal-target sensitivities only" in rendered
    assert "Largest deterministic loss" in rendered
    assert "$48,000" in rendered
    assert "48.0% of starting equity" in rendered
    assert "Deterministic mark shocks" in rendered
    assert "Statistical sensitivity — not a mark shock" in rendered
    assert "Hypothetical reference-limit findings" in rendered

    deterministic_tables = [
        table.value
        for table in app.dataframe
        if {
            "Scenario",
            "State",
            "Loss",
            "Loss / equity",
            "Resulting drawdown",
        }.issubset(table.value.columns)
    ]
    statistical_tables = [
        table.value
        for table in app.dataframe
        if {
            "Scenario",
            "State",
            "Loss proxy",
            "Loss proxy / equity",
        }.issubset(table.value.columns)
    ]
    assert len(deterministic_tables) == 1
    assert len(statistical_tables) == 1
    assert deterministic_tables[0].iloc[0]["Scenario"] == "Broad market decline"
    assert deterministic_tables[0].iloc[0]["Loss / equity"] == "48.0%"
    assert statistical_tables[0].iloc[0]["Scenario"] == (
        "Volatility and correlation proxy"
    )
    assert statistical_tables[0].iloc[0]["Loss proxy / equity"] == "21.0%"

    assert len(app.button) == 0
    assert "Run stress" not in rendered
    assert "Execute stress" not in rendered


def test_paper_stress_failure_is_scoped_and_rest_of_page_still_renders() -> None:
    failure = "synthetic fail-closed stress calculation error"
    app = _paper_app(stress_report=None, stress_error=failure).run()

    assert not app.exception
    rendered = _rendered_values(app)
    assert "Proposal downside review" in rendered
    assert "Unavailable. The read-only stress review could not be produced safely." in rendered
    assert "stress review:calculation unavailable" in rendered
    assert failure in rendered
    assert "The account is still entirely simulated cash." in rendered
    assert "Simulation assumptions" in rendered
    assert "Commission assumption" in rendered
    assert len(app.button) == 0


def test_paper_record_action_is_absent_outside_the_execution_window() -> None:
    """The default fixture is 'waiting_for_scheduled_close'; no write UI appears."""
    app = _paper_app(stress_report=None, stress_error="unused").run()
    assert not app.exception
    assert len(app.button) == 0
    assert "Record simulated fill" not in _rendered_values(app)


def test_paper_record_action_is_absent_once_already_simulated() -> None:
    monitor = _execution_ready_monitor(already_simulated=True)
    app = _paper_app(
        stress_report=None, stress_error="unused", monitor=monitor
    ).run()
    assert not app.exception
    assert "Record simulated fill" not in _rendered_values(app)


def test_paper_record_action_blocks_on_missing_execution_evidence() -> None:
    monitor = _execution_ready_monitor()
    review = {
        "ready": False,
        "detail": "Reviewed closing-price evidence is not yet available.",
        "missing_count": 2,
        "missing": ["AAA close", "BBB close"],
    }
    app = _paper_app(
        stress_report=None,
        stress_error="unused",
        monitor=monitor,
        review_result=review,
    ).run()
    assert not app.exception
    rendered = _rendered_values(app)
    assert "Record this simulated fill" in rendered
    assert "2 required item(s)" in rendered
    assert "AAA close" in rendered
    # No button is offered while evidence is missing.
    assert len(app.button) == 0
    assert len(app.checkbox) == 0


def test_paper_record_action_offers_a_disabled_button_until_acknowledged() -> None:
    monitor = _execution_ready_monitor()
    review = {
        "ready": True,
        "detail": "Ready for explicit local simulation.",
        "missing_count": 0,
        "missing": [],
        "projected_trade_count": 2,
        "projected_transaction_costs": 12.5,
    }
    app = _paper_app(
        stress_report=None,
        stress_error="unused",
        monitor=monitor,
        review_result=review,
    ).run()
    assert not app.exception
    rendered = _rendered_values(app)
    assert "Record this simulated fill" in rendered
    assert "2 simulated trade(s)" in rendered
    assert "$12.50 modeled costs" in rendered

    buttons = [b for b in app.button if b.label == "Record simulated fill"]
    assert len(buttons) == 1
    assert buttons[0].disabled is True

    checkboxes = list(app.checkbox)
    assert len(checkboxes) == 1
    assert checkboxes[0].value is False


def test_paper_record_action_records_after_explicit_acknowledgement() -> None:
    """Checking the box and clicking runs the write path, then shows success."""
    monitor = _execution_ready_monitor()
    review = {
        "ready": True,
        "detail": "Ready for explicit local simulation.",
        "missing_count": 0,
        "missing": [],
        "projected_trade_count": 1,
        "projected_transaction_costs": 5.0,
    }
    execution = {
        "execution": {
            "execution_date": "2026-08-10",
            "trades": [{"ticker": "AAA", "shares": 10}],
            "transaction_costs": 5.0,
        }
    }
    app = _paper_app(
        stress_report=None,
        stress_error="unused",
        monitor=monitor,
        review_result=review,
        execute_result=execution,
    ).run()

    app.checkbox[0].check().run()
    buttons = [b for b in app.button if b.label == "Record simulated fill"]
    assert buttons[0].disabled is False
    buttons[0].click().run()

    assert not app.exception
    rendered = _rendered_values(app)
    assert "Simulation recorded for 2026-08-10" in rendered
    assert "1 simulated trade(s)" in rendered
    assert "No order was sent to a broker" in rendered


def test_paper_record_action_surfaces_a_refused_write_without_crashing() -> None:
    monitor = _execution_ready_monitor()
    review = {
        "ready": True,
        "detail": "Ready for explicit local simulation.",
        "missing_count": 0,
        "missing": [],
        "projected_trade_count": 1,
        "projected_transaction_costs": 5.0,
    }
    app = _paper_app(
        stress_report=None,
        stress_error="unused",
        monitor=monitor,
        review_result=review,
        execute_raises="synthetic refusal: proposal changed on disk",
    ).run()

    app.checkbox[0].check().run()
    buttons = [b for b in app.button if b.label == "Record simulated fill"]
    buttons[0].click().run()

    assert not app.exception
    rendered = _rendered_values(app)
    assert "Simulated execution refused" in rendered
    assert "synthetic refusal: proposal changed on disk" in rendered


def test_dashboard_blocks_invalid_readiness_evidence_without_an_unhandled_exception() -> None:
    source = _DASHBOARD_PATH.read_text(encoding="utf-8")
    override = (
        "\n# AppTest-only invalid readiness evidence.\n"
        "def load_us_readiness():\n"
        "    raise ValueError('synthetic readiness checksum mismatch')\n\n"
    )
    app = AppTest.from_string(
        source.replace(_SIDEBAR_MARKER, override + _SIDEBAR_MARKER, 1),
        default_timeout=120,
    ).run()

    assert not app.exception
    rendered = _rendered_values(app)
    assert "could not validate the current readiness evidence" in rendered
    assert "synthetic readiness checksum mismatch" in rendered
    assert "no local research or paper state was changed" in rendered


@pytest.mark.skipif(
    not _DUCKDB_PATH.exists(),
    reason="rendered dashboard characterization requires the reviewed local checkpoint",
)
def test_dashboard_workspaces_and_research_surfaces_render_without_exceptions() -> None:
    app = AppTest.from_file(_DASHBOARD_PATH, default_timeout=120)

    app.run()
    assert not app.exception
    home_html = "\n".join(str(element.value) for element in app.markdown)
    assert "AIOS Home" in home_html
    assert "Research" in home_html
    assert "Investment Command Center" in home_html
    assert "Priority action" in home_html
    assert "Current proposal targets" in home_html
    assert "Open reviews" in home_html
    assert app.sidebar.radio("workspace").value == "today"
    assert app.query_params["view"] == ["today"]

    app.query_params["view"] = ["method"]
    app.run(timeout=120)
    assert not app.exception
    assert app.sidebar.radio("workspace").value == "method"
    assert app.query_params["view"] == ["method"]
    assert any("How AIOS Works" in str(item.value) for item in app.markdown)

    app.sidebar.radio("workspace").set_value("today").run(timeout=120)
    assert not app.exception
    assert app.sidebar.radio("workspace").value == "today"
    assert app.query_params["view"] == ["today"]

    app.sidebar.radio("workspace").set_value("research").run(timeout=120)
    assert not app.exception
    assert app.query_params["view"] == ["research"]
    assert len(app.sidebar.date_input) == 0
    assert len(app.sidebar.get("segmented_control")) == 0
    assert app.date_input("date").value is not None
    assert app.segmented_control("model").value == "qv"
    assert app.query_params["surface"] == ["ranked"]
    assert app.segmented_control("surface").value == "ranked"
    assert len(app.tabs) == 0
    assert len(app.dataframe) == 1
    assert len(app.get("plotly_chart")) == 0

    app.query_params["surface"] = ["map"]
    app.run(timeout=120)
    assert not app.exception
    assert app.segmented_control("surface").value == "map"
    assert app.query_params["surface"] == ["map"]
    assert len(app.dataframe) == 0
    assert len(app.get("plotly_chart")) == 1

    app.segmented_control("surface").set_value("coverage").run(timeout=120)
    assert not app.exception
    assert app.segmented_control("surface").value == "coverage"
    assert app.query_params["surface"] == ["coverage"]
    assert len(app.dataframe) == 2
    assert len(app.get("plotly_chart")) == 0

    for slug, title in (
        ("company", "Company Detail"),
        ("paper", "Paper Trial"),
        ("system", "System Health"),
        ("method", "How AIOS Works"),
    ):
        app.query_params["view"] = [slug]
        app.run(timeout=120)
        assert not app.exception
        assert app.query_params["view"] == [slug]
        expected_nav = "research" if slug == "company" else slug
        assert app.sidebar.radio("workspace").value == expected_nav
        assert any(title in str(item.value) for item in app.markdown), slug
        if slug == "system":
            system_html = "\n".join(str(item.value) for item in app.markdown)
            assert "Research experiments" in system_html

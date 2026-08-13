"""AI Investment OS — Dashboard (Streamlit).

Six decision-oriented views:
  1. TODAY          — scoped status and one highest-priority safe action
  2. RESEARCH       — one selected ranked, map, or coverage surface
  3. COMPANY DETAIL — one-stock score and evidence review
  4. PAPER TRIAL    — supervised local simulation state and next action
  5. SYSTEM HEALTH  — scheduler, ingests, backups, incidents, and policy evidence
  6. HOW IT WORKS   — non-technical explanation + optional audit details

Run:  .venv/bin/aios dashboard
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date, timedelta
from html import escape
from pathlib import Path
from threading import Lock

import duckdb
import pandas as pd
import plotly.express as px
import streamlit as st

# Ensure src/ on path when run via `streamlit run`
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aios.config import settings  # noqa: E402
from aios.dashboard_components import (  # noqa: E402
    apply_design_system,
    evidence_list,
    key_value_list,
    page_header,
    render_action_notice,
    render_control_list,
    render_metric_strip,
    render_pipeline_stepper,
    section_header,
)
from aios.dashboard_copy import (  # noqa: E402
    MODEL_QV_LABEL,
    MODEL_QVML_LABEL,
    RESEARCH_ONLY_NOTICE,
    VIEW_DETAILS,
    VIEW_HOME,
    VIEW_METHOD,
    VIEW_PAPER,
    VIEW_RANKINGS,
    VIEW_SYSTEM,
    company_symbol_label,
    coverage_value,
    display_date,
    friendly_missing_reasons,
    friendly_missing_summary,
    friendly_regime,
    model_key,
    us_certification_freshness_message,
)
from aios.dashboard_ui import (  # noqa: E402
    NextAction,
    PaperViewModel,
    StressReviewViewModel,
    build_home_view_model,
    build_paper_view_model,
    build_stress_review_view_model,
    notification_route_matches,
)
from aios.experiments import list_experiments  # noqa: E402
from aios.factor_batch import DecisionScopedFactorStore  # noqa: E402
from aios.factors.composite import compute_composite  # noqa: E402
from aios.forward import (  # noqa: E402
    DEFAULT_FORWARD_RELATIVE_PATH,
    require_registered_forward_proposal,
)
from aios.maintenance import (  # noqa: E402
    MaintenanceLockBusyError,
    MaintenanceLockError,
    project_maintenance_lock,
)
from aios.notifications import (  # noqa: E402
    smtp_email_config,
)
from aios.operations import verify_local_backup  # noqa: E402
from aios.operator_evidence import (  # noqa: E402
    load_operations_evidence_read_only,
    load_paper_monitor_evidence,
)
from aios.paper import (  # noqa: E402
    execute_paper_proposal,
    latest_paper_decision_date,
    review_paper_proposal_execution,
)
from aios.readiness import assess_us_readiness  # noqa: E402
from aios.risk.stress import (  # noqa: E402
    build_stress_source_identity,
    load_scenario_bundle,
    review_registered_paper_proposal_stress,
)
from aios.scheduler import (  # noqa: E402
    TIMER_NAMES,
    email_scheduler_status,
    user_linger_status,
    user_scheduler_status,
)
from aios.storage.store import close_global_store, store_scope  # noqa: E402

_VIEW_SLUGS = {
    VIEW_HOME: "today",
    VIEW_RANKINGS: "research",
    VIEW_DETAILS: "company",
    VIEW_PAPER: "paper",
    VIEW_SYSTEM: "system",
    VIEW_METHOD: "method",
}
_VIEWS_BY_SLUG = {slug: label for label, slug in _VIEW_SLUGS.items()}
_PRIMARY_NAVIGATION = {
    "today": "Overview",
    "research": "Research",
    "paper": "Paper Trial",
    "system": "Operations",
    "method": "Methodology & Sources",
}
_MODEL_SLUGS = {
    MODEL_QV_LABEL: "qv",
    MODEL_QVML_LABEL: "qvml",
}
_MODELS_BY_SLUG = {slug: label for label, slug in _MODEL_SLUGS.items()}
_MODEL_CONTROL_LABELS = {
    "qv": "Baseline · Quality + Value",
    "qvml": "Experimental · Four factor",
}
_RESEARCH_SURFACE_SLUGS = {
    "Ranked List": "ranked",
    "Opportunity Map": "map",
    "Data Coverage": "coverage",
}
_RESEARCH_SURFACES_BY_SLUG = {slug: label for label, slug in _RESEARCH_SURFACE_SLUGS.items()}


def _query_value(key: str) -> str:
    value = st.query_params.get(key, "")
    if isinstance(value, list):
        value = value[-1] if value else ""
    return str(value or "")


def _url_marker(key: str) -> str:
    return f"_aios_url_seen_{key}"


def _hydrate_widget_from_url(key: str, decoded_value: object) -> None:
    """Let an external URL change win without clobbering a widget interaction."""
    raw_value = _query_value(key)
    marker = _url_marker(key)
    if (
        key not in st.session_state
        or marker not in st.session_state
        or st.session_state[marker] != raw_value
    ):
        st.session_state[key] = decoded_value
    st.session_state[marker] = raw_value


def _persist_widget_query(key: str, encoded_value: str) -> None:
    """Canonicalize one widget value in the URL and remember its origin."""
    if _query_value(key) != encoded_value:
        st.query_params[key] = encoded_value
    st.session_state[_url_marker(key)] = encoded_value


def _go_to_workspace(slug: str) -> None:
    """Navigate through the URL-backed workspace control on the next rerun."""
    if slug in _VIEWS_BY_SLUG:
        st.query_params["view"] = slug
        st.session_state["workspace"] = "research" if slug == "company" else slug


st.set_page_config(
    page_title="AI Investment OS",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="auto",
)


apply_design_system()
st.markdown(
    '<a class="aios-skip-link" href="#aios-main">Skip to main content</a>',
    unsafe_allow_html=True,
)
# Older dashboard versions held one process-wide writable DuckDB connection.
# Release it once after a hot reload; every loader below now uses a bounded,
# read-only scope so scheduled writers can run while the dashboard stays open.
close_global_store()
_COMPOSITE_BUILD_LOCK = Lock()


# ----------------------------------------------------------------------
# Cached data loaders
# ----------------------------------------------------------------------
@st.cache_data(
    ttl=300,
    show_spinner="Building reviewed stock scores from verified filings and prices…",
)
def load_composite(as_of: str, include_market_factors: bool) -> pd.DataFrame:
    # Batch reads trade a bounded working set for a much shorter cold load.
    # Serialize cache misses so two local sessions cannot double that memory
    # footprint with competing cold DuckDB scans.
    with _COMPOSITE_BUILD_LOCK, store_scope(read_only=True) as store:
        tickers = [row["ticker"] for row in store.universe_membership_on("sp500", as_of)]
        factor_store = DecisionScopedFactorStore(store, tickers)
        rows = compute_composite(
            tickers,
            as_of,
            factor_store,
            include_market_factors=include_market_factors,
        )
        labels = {
            str(row["ticker"]): row.get("canonical_name")
            for row in store.universe_identity_labels("sp500", as_of)
        }
    return _rows_to_df(rows, labels)


@st.cache_data(ttl=300)
def load_us_readiness() -> dict:
    """Load the same fail-closed current-use gate exposed by the CLI."""
    with store_scope(read_only=True) as store:
        decision_date = latest_paper_decision_date(store)
        return assess_us_readiness(decision_date, purpose="paper", store=store).to_dict()


@st.cache_data(ttl=60)
def load_paper_monitor() -> dict:
    """Load the checksum-validated account and active registered proposal."""
    with store_scope(read_only=True) as store:
        monitor = load_paper_monitor_evidence(
            settings.project_root,
            store,
        )
    # Dashboard internals pass these values to file-backed stress-review calls.
    # Keep their established absolute-path contract while the shared operator
    # adapter exposes project-relative labels to the CLI.
    for key in ("account_path", "proposal_path", "trial_path"):
        value = monitor.get(key)
        if value:
            path = Path(str(value))
            monitor[key] = str(
                path.resolve()
                if path.is_absolute()
                else (settings.project_root / path).resolve()
            )
    return monitor


@st.cache_data(ttl=60, show_spinner=False)
def load_proposal_stress_review(
    *,
    account_path: str,
    account_payload_sha256: str,
    proposal_path: str,
    proposal_payload_sha256: str,
    trial_path: str,
    trial_payload_sha256: str,
    scenario_bundle_sha256: str,
    source_bundle_sha256: str,
) -> dict:
    """Load one governed stress report keyed by every immutable input identity."""
    governed = review_registered_paper_proposal_stress(
        settings.project_root,
        Path(trial_path),
        Path(account_path),
        Path(proposal_path),
    )
    report = governed.report
    payload = report.payload
    source = payload.get("source", {})
    governance = source.get("forward_governance", {})
    scenario_bundle = payload.get("scenario_bundle", {})
    source_code = payload.get("source_code", {})
    observed = {
        "account": source.get("account_payload_sha256"),
        "proposal": source.get("proposal_payload_sha256"),
        "trial": governance.get("trial_payload_sha256"),
        "scenario_bundle": scenario_bundle.get("bundle_sha256"),
        "source_bundle": source_code.get("source_bundle_sha256"),
    }
    expected = {
        "account": account_payload_sha256,
        "proposal": proposal_payload_sha256,
        "trial": trial_payload_sha256,
        "scenario_bundle": scenario_bundle_sha256,
        "source_bundle": source_bundle_sha256,
    }
    if observed != expected:
        raise ValueError("stress review inputs changed while the dashboard cache was loading")
    return report.envelope()


@st.cache_data(ttl=300)
def load_identity_labels(as_of: str) -> dict[str, str | None]:
    """Load reviewed issuer names for display; symbols remain the security key."""
    with store_scope(read_only=True) as store:
        return {
            str(row["ticker"]): row.get("canonical_name")
            for row in store.universe_identity_labels("sp500", as_of)
        }


@st.cache_data(ttl=60)
def load_operating_summary() -> dict:
    """Load only the operating evidence needed by the decision-first home page."""
    path = settings.operations_db_path
    if not path.is_absolute():
        path = settings.project_root / path
    return load_operations_evidence_read_only(path)


@st.cache_data(ttl=60)
def load_system_operations() -> dict:
    """Collect bounded, read-only operating evidence for the control workspace."""
    with store_scope(read_only=True) as store:
        state: dict = {
            "ingests": store.ingest_history(limit=20),
            "universe_attestations": store.universe_coverage_attestations(limit=10),
        }
    try:
        state["scheduler"] = user_scheduler_status()
        state["scheduler_error"] = None
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        state["scheduler"] = {}
        state["scheduler_error"] = str(exc)
    state["linger_enabled"] = user_linger_status()
    try:
        state["email_scheduler"] = email_scheduler_status()
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        state["email_scheduler"] = {
            "enabled": False,
            "active": False,
            "runtime_verified": False,
        }

    state["backup"] = load_latest_backup()
    operations_path = settings.operations_db_path
    if not operations_path.is_absolute():
        operations_path = settings.project_root / operations_path
    operations = load_operations_evidence_read_only(operations_path)
    state["incidents"] = operations["incidents"]
    state["incident_summary"] = operations["incident_summary"]
    state["anomaly_cases"] = operations["anomaly_cases"]
    state["anomaly_case_summary"] = operations["anomaly_case_summary"]
    state["latest_anomaly_scan"] = operations["latest_anomaly_scan"]
    state["daily_cycle"] = operations["daily_cycle"]
    state["incident_error"] = operations["error"]
    state["notification_summary"] = operations["notification_summary"]
    state["notifications"] = operations["notifications"]
    state["notification_route"] = operations["notification_route"]
    state["notification_error"] = operations["error"]
    if operations["error"] is None:
        route = operations["notification_route"]
        try:
            email_config = smtp_email_config()
            state["email_config_complete"] = True
            state["email_config_matches"] = notification_route_matches(
                route,
                email_config.fingerprint,
            )
        except ValueError:
            state["email_config_complete"] = False
            state["email_config_matches"] = False
    else:
        state["email_config_complete"] = False
        state["email_config_matches"] = False
    return state


@st.cache_data(ttl=60)
def load_research_experiments() -> list[dict]:
    """Load registered backtest/factor experiments for read-only display.

    Reads write-once JSON directly off disk; no store connection, no ledger
    write, nothing to invalidate beyond the cache TTL.
    """
    experiments_dir = settings.project_root / "data" / "experiments"
    return list_experiments(experiments_dir=experiments_dir)


@st.cache_data(ttl=600)
def load_latest_backup() -> dict:
    """Hash-verify the newest local backup on a slower cache cadence."""
    backup_dirs = sorted(
        path
        for path in (settings.project_root / "backups").glob("aios-*")
        if path.is_dir() and not path.is_symlink()
    )
    if not backup_dirs:
        return {"status": "missing"}

    latest = backup_dirs[-1]
    try:
        verified = verify_local_backup(latest)
        manifest = json.loads((latest / "manifest.json").read_text(encoding="utf-8"))
        return {
            "status": "verified",
            "path": str(verified.path),
            "created_at": manifest.get("created_at"),
            "files": verified.files,
            "bytes": verified.bytes,
            "manifest_sha256": verified.manifest_sha256,
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "failed",
            "path": str(latest),
            "error": str(exc),
        }


def _rows_to_df(rows, labels: dict[str, str | None] | None = None) -> pd.DataFrame:
    labels = labels or {}
    data = []
    for r in rows:
        company_name = labels.get(r.ticker)
        data.append(
            {
                "Rank": r.qv_rank,
                "Ticker": r.ticker,
                "Company": company_name or r.ticker,
                "Company + Symbol": company_symbol_label(company_name, r.ticker),
                "Grade": r.grade,
                "QV Score": round(r.qv_score, 1) if r.qv_score is not None else None,
                "QVML Rank": r.qvml_rank,
                "QVML Grade": r.qvml_grade,
                "QVML Score": round(r.qvml_score, 1) if r.qvml_score is not None else None,
                "Quality": round(r.quality_score, 1) if r.quality_score is not None else None,
                "Value": round(r.value_score, 1) if r.value_score is not None else None,
                "Momentum": round(r.momentum_score, 1) if r.momentum_score is not None else None,
                "Low Volatility": round(r.low_volatility_score, 1)
                if r.low_volatility_score is not None
                else None,
                "12-1 Momentum %": round(r.momentum_12_1 * 100, 1)
                if r.momentum_12_1 is not None
                else None,
                "Annualized Volatility %": round(r.annualized_volatility * 100, 1)
                if r.annualized_volatility is not None
                else None,
                "Market observations": r.market_price_observations,
                "Macro regime": r.macro_regime,
                "Quality weight %": round(r.quality_weight * 100, 1),
                "Value weight %": round(r.value_weight * 100, 1),
                "QVML Quality weight %": round(r.qvml_quality_weight * 100, 1),
                "QVML Value weight %": round(r.qvml_value_weight * 100, 1),
                "QVML Momentum weight %": round(r.qvml_momentum_weight * 100, 1),
                "QVML Low Volatility weight %": round(r.qvml_low_volatility_weight * 100, 1),
                "Macro PIT ready": r.regime_pit_ready,
                "Quality inputs": r.quality_components_available,
                "Value inputs": r.value_multiples_available,
                "ROIC %": round(r.roic * 100, 1) if r.roic is not None else None,
                "FCF Margin %": round(r.fcf_margin * 100, 1) if r.fcf_margin is not None else None,
                "Gross Margin %": round(r.gross_margin * 100, 1)
                if r.gross_margin is not None
                else None,
                "Piotroski F": r.piotroski_f,
                "P/E": round(r.pe, 1) if r.pe is not None else None,
                "EV/EBITDA": round(r.ev_ebitda, 1) if r.ev_ebitda is not None else None,
                "P/FCF": round(r.p_fcf, 1) if r.p_fcf is not None else None,
                "EV/Sales": round(r.ev_sales, 1) if r.ev_sales is not None else None,
                "P/B": round(r.p_b, 1) if r.p_b is not None else None,
                "Price": round(r.price, 2) if r.price is not None else None,
                "Market Cap ($B)": round(r.market_cap / 1e9, 1)
                if r.market_cap is not None
                else None,
                "Missing inputs": list(r.missing),
                "Missing data": friendly_missing_summary(r.missing),
                "Missing data details": friendly_missing_reasons(r.missing),
            }
        )
    return pd.DataFrame(data)


def _checks_by_key(report: dict) -> dict[str, dict]:
    """Index readiness checks once so cards and tables always reconcile."""
    return {str(row["check"]): row for row in report.get("checks", [])}


def _page_header(
    eyebrow: str,
    title: str,
    note: str,
    *,
    chips: list[tuple[str, str]] | None = None,
) -> None:
    """Compatibility wrapper around the shared page-header component."""
    page_header(
        eyebrow,
        title,
        note,
        metadata=chips or (),
    )


def _render_kpi_grid(cards: list[tuple[str, str, str, str]]) -> None:
    """Compatibility wrapper for the shared, border-light metric strip."""
    render_metric_strip(cards)


def _render_paper_workflow(model: PaperViewModel) -> None:
    """Render one governance answer followed by the fixed prospective workflow."""
    tone = model.status.tone if model.status.tone in {"success", "warning", "danger"} else ""
    role = "alert" if tone == "danger" else "status"
    st.markdown(
        f'<section class="aios-paper-state {escape(tone)}" role="{role}">'
        '<div class="aios-paper-state-label">Current governance state</div>'
        f'<div class="aios-paper-state-value">{escape(model.status.value)}</div>'
        f'<div class="aios-paper-state-detail">{escape(model.status.detail)}</div>'
        "</section>",
        unsafe_allow_html=True,
    )
    render_pipeline_stepper(model.stages)


def _execute_paper_proposal_from_dashboard(
    account_path: Path, proposal_path: Path
) -> dict:
    """Record one simulated fill using the exact call chain `aios paper-execute` uses.

    Every safety property of the CLI command is reproduced: the maintenance
    lease that serializes AIOS mutation workflows, the registered-proposal
    check, and the same `confirm_simulated=True` gate inside the frozen
    `paper.py`. This function only orchestrates that existing chain — it does
    not implement paper-state mutation itself, and it touches no file inside
    the active trial's frozen policy bundle.
    """
    previous_umask = os.umask(0o077)
    try:
        with project_maintenance_lock(settings.project_root, operation="paper-execute"):
            trial_path = settings.project_root / DEFAULT_FORWARD_RELATIVE_PATH
            require_registered_forward_proposal(
                settings.project_root, trial_path, account_path, proposal_path
            )
            with store_scope(read_only=True) as store:
                return execute_paper_proposal(
                    account_path,
                    proposal_path,
                    store,
                    confirm_simulated=True,
                )
    finally:
        os.umask(previous_umask)


def _render_paper_record_action(
    monitor: dict, account_path: Path, proposal_path: Path
) -> None:
    """One deliberate, explicit confirmation to record a simulated fill.

    This is the only write path in an otherwise read-only dashboard. It
    exists so recording a paper fill does not require leaving the browser for
    a terminal — it does not remove the human decision the CLI requires.
    """
    proposal = monitor.get("proposal")
    timing = proposal.get("timing") if isinstance(proposal, dict) else None
    timing_status = timing.get("status") if isinstance(timing, dict) else None
    already_simulated = bool(proposal and proposal.get("already_simulated"))
    if already_simulated or timing_status != "execution_window_open":
        return

    with st.container(border=True, key="paper_record_action"):
        section_header(
            "Record this simulated fill",
            "Local simulation only. No order is sent to a broker.",
        )
        try:
            with store_scope(read_only=True) as store:
                review = review_paper_proposal_execution(
                    account_path, proposal_path, store
                )
        except Exception as exc:
            st.error("The read-only pre-fill review failed and nothing was recorded.")
            with st.expander("Technical error details"):
                st.code(str(exc))
            return

        st.caption(str(review.get("detail") or ""))
        missing_count = review.get("missing_count") or 0
        if not review.get("ready"):
            st.warning(
                "Not ready to record yet. "
                f"{missing_count} required item(s) of execution evidence are missing."
            )
            if review.get("missing"):
                for item in list(review["missing"])[:6]:
                    st.write(f"- {item}")
            return

        trade_count = review.get("projected_trade_count", 0)
        modeled_costs = review.get("projected_transaction_costs", 0.0)
        st.write(
            f"Projected: {trade_count} simulated trade(s), "
            f"${modeled_costs:,.2f} modeled costs."
        )
        acknowledged = st.checkbox(
            "I understand this records a local, simulation-only fill. "
            "No broker is contacted and no real money moves.",
            key="paper_record_ack",
        )
        if st.button(
            "Record simulated fill",
            type="primary",
            disabled=not acknowledged,
            key="paper_record_button",
        ):
            try:
                result = _execute_paper_proposal_from_dashboard(
                    account_path, proposal_path
                )
            except (MaintenanceLockBusyError, MaintenanceLockError) as exc:
                st.warning(f"Another AIOS mutation workflow is already running. {exc}")
                return
            except Exception as exc:
                st.error("Simulated execution refused.")
                with st.expander("Technical error details"):
                    st.code(str(exc))
                return
            execution = result["execution"]
            st.success(
                f"Simulation recorded for {execution['execution_date']}. "
                f"{len(execution['trades'])} simulated trade(s), "
                f"${execution['transaction_costs']:,.2f} modeled costs. "
                "No order was sent to a broker."
            )
            st.cache_data.clear()
            st.rerun()


def _render_stress_review(
    model: StressReviewViewModel,
    *,
    report: dict | None,
    proposal_path: str | None,
    review_error: str | None = None,
) -> None:
    """Render one compact advisory panel without adding a workflow stage or CTA."""
    with st.container(border=True, key="proposal_stress_review"):
        section_header(
            "Proposal downside review",
            "Deterministic mark shocks and a separate statistical sensitivity proxy.",
        )
        if model.status.tone == "danger":
            st.error(f"{model.status.value}. {model.status.detail}")
        elif model.status.tone == "warning":
            st.warning(f"{model.status.value}. {model.status.detail}")
        else:
            st.info(f"{model.status.value}. {model.status.detail}")
        st.caption(model.advisory)

        if model.state not in {"complete", "partial"}:
            if model.blockers:
                st.markdown(
                    "\n".join(
                        f"- {blocker.replace('_', ' ')}"
                        for blocker in model.blockers
                    )
                )
            if review_error:
                with st.expander("Technical error details"):
                    st.code(review_error)
            return

        largest_loss = (
            f"${model.largest_fixed_loss:,.0f}"
            if model.largest_fixed_loss is not None
            else "Unavailable"
        )
        largest_loss_pct = (
            f"{model.largest_fixed_loss_pct:.1%} of starting equity"
            if model.largest_fixed_loss_pct is not None
            else "No calculated fixed mark"
        )
        drawdown = (
            f"{model.largest_fixed_drawdown:.1%}"
            if model.largest_fixed_drawdown is not None
            else "Unavailable"
        )
        _render_kpi_grid(
            [
                (
                    "Largest deterministic loss",
                    largest_loss,
                    largest_loss_pct,
                    "warning",
                ),
                (
                    "Resulting drawdown",
                    drawdown,
                    model.largest_fixed_scenario_id or "No calculated fixed mark",
                    "warning",
                ),
                (
                    "Scenario coverage",
                    f"{model.calculated_count} / {model.generated_count}",
                    f"{model.withheld_count} result(s) withheld",
                    "",
                ),
                (
                    "Reference findings",
                    str(len(model.reference_findings)),
                    "Advisory comparisons; never approval gates.",
                    "",
                ),
            ]
        )

        if model.fixed_marks:
            st.markdown("#### Deterministic mark shocks")
            fixed_table = pd.DataFrame(
                [
                    {
                        "Scenario": row.label,
                        "State": (
                            "Calculated"
                            if row.status == "calculated"
                            else "Withheld"
                        ),
                        "Loss": (
                            f"${row.loss:,.0f}" if row.loss is not None else "Withheld"
                        ),
                        "Loss / equity": (
                            f"{row.loss_pct:.1%}"
                            if row.loss_pct is not None
                            else "Withheld"
                        ),
                        "Resulting drawdown": (
                            f"{row.drawdown:.1%}"
                            if row.drawdown is not None
                            else "Withheld"
                        ),
                    }
                    for row in model.fixed_marks
                ]
            )
            st.dataframe(fixed_table, hide_index=True, width="stretch")

        if model.statistical_proxies:
            st.markdown("#### Statistical sensitivity — not a mark shock")
            proxy_table = pd.DataFrame(
                [
                    {
                        "Scenario": row.label,
                        "State": (
                            "Calculated"
                            if row.status == "calculated"
                            else "Withheld"
                        ),
                        "Loss proxy": (
                            f"${row.loss:,.0f}" if row.loss is not None else "Withheld"
                        ),
                        "Loss proxy / equity": (
                            f"{row.loss_pct:.1%}"
                            if row.loss_pct is not None
                            else "Withheld"
                        ),
                    }
                    for row in model.statistical_proxies
                ]
            )
            st.dataframe(proxy_table, hide_index=True, width="stretch")
            st.caption(
                "This volatility/correlation result allocates statistical risk. It does "
                "not create stressed holdings, fills, liquidation paths, or a forecast."
            )

        if model.top_position_contributions or model.top_sector_contributions:
            position_column, sector_column = st.columns(2, gap="large")
            with position_column:
                st.markdown("#### Largest position contributions")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Target": row.label,
                                "Business group": row.sector or "Unclassified",
                                "Loss": f"${row.loss:,.0f}",
                                "Loss / equity": (
                                    f"{row.loss_pct_of_starting_equity:.1%}"
                                ),
                            }
                            for row in model.top_position_contributions
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )
            with sector_column:
                st.markdown("#### Largest business-group contributions")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Business group": row.label,
                                "Loss": f"${row.loss:,.0f}",
                                "Loss / equity": (
                                    f"{row.loss_pct_of_starting_equity:.1%}"
                                ),
                            }
                            for row in model.top_sector_contributions
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )

        if model.reference_findings:
            st.markdown("#### Hypothetical reference-limit findings")
            for finding in model.reference_findings[:6]:
                st.markdown(f"- {finding.message}")
            if len(model.reference_findings) > 6:
                st.caption(
                    f"{len(model.reference_findings) - 6} additional scenario finding(s) "
                    "remain in the canonical report."
                )

        if model.blockers:
            st.warning(
                "Some dependent calculations were withheld because required evidence "
                "failed closed."
            )
            st.markdown(
                "\n".join(
                    f"- {blocker.replace('_', ' ')}"
                    for blocker in model.blockers
                )
            )

        if report is not None or review_error:
            payload = report.get("payload", report) if report is not None else {}
            with st.expander("Technical evidence and reproducible command"):
                if proposal_path:
                    st.code(
                        f"aios stress-review --proposal {proposal_path}",
                        language="bash",
                    )
                if report is not None:
                    st.json(
                        {
                            "report_id": payload.get("report_id"),
                            "report_payload_sha256": report.get("payload_sha256"),
                            "scenario_bundle": payload.get("scenario_bundle"),
                            "source_bundle_sha256": payload.get("source_code", {}).get(
                                "source_bundle_sha256"
                            ),
                            "target_evidence_sha256": payload.get("evidence", {}).get(
                                "target_evidence_sha256"
                            ),
                        },
                        expanded=False,
                    )
                if review_error:
                    st.code(review_error)


def _render_next_action(action: NextAction) -> None:
    render_action_notice(
        label="Priority action",
        title=action.title,
        detail=action.detail,
        cta_label=action.cta_label,
        on_click=_go_to_workspace,
        on_click_args=(action.destination,),
        tone=action.tone,
        technical_command=action.command,
        key="next_action",
    )


def _research_surface_selector() -> str:
    """Render one URL-bound research surface switch."""
    options = tuple(_RESEARCH_SURFACES_BY_SLUG)
    raw_surface = _query_value("surface")
    default_surface = raw_surface if raw_surface in _RESEARCH_SURFACES_BY_SLUG else "ranked"
    _hydrate_widget_from_url("surface", default_surface)
    help_text = (
        "Start with the ranked list. Use the map to compare quality and value, "
        "or Data Coverage to inspect withheld scores."
    )
    selected = st.segmented_control(
        "Research view",
        options,
        default=None,
        format_func=_RESEARCH_SURFACES_BY_SLUG.__getitem__,
        key="surface",
        width="stretch",
        help=help_text,
    )
    surface_slug = str(selected or "ranked")
    _persist_widget_query("surface", surface_slug)
    return _RESEARCH_SURFACES_BY_SLUG[surface_slug]


def _research_context_controls(
    certified_from: str,
    certified_through: str,
    *,
    include_search: bool = True,
    compact: bool = False,
) -> tuple[str, str, str]:
    """Render URL-backed research scope controls in the page where they apply."""

    minimum_date = date.fromisoformat(certified_from)
    maximum_date = date.fromisoformat(certified_through)
    try:
        query_date = date.fromisoformat(_query_value("date"))
    except ValueError:
        query_date = maximum_date
    if not minimum_date <= query_date <= maximum_date:
        query_date = maximum_date
    _hydrate_widget_from_url("date", query_date)

    raw_model = _query_value("model")
    default_model_slug = raw_model if raw_model in _MODELS_BY_SLUG else "qv"
    _hydrate_widget_from_url("model", default_model_slug)
    raw_search = _query_value("q").strip()
    _hydrate_widget_from_url("q", raw_search)

    toolbar_key = "company_toolbar" if compact else "research_toolbar"
    with st.container(key=toolbar_key):
        toolbar_help = (
            "" if compact else "<span>Change the evidence date or comparison method.</span>"
        )
        st.markdown(
            '<div class="aios-command-heading">'
            f"<strong>{'Evidence scope' if compact else 'Research scope'}</strong>"
            f"{toolbar_help}"
            "</div>",
            unsafe_allow_html=True,
        )
        column_widths = [1, 2.1, 1.45] if include_search else [1, 2.1]
        control_columns = st.columns(column_widths, vertical_alignment="bottom")
        date_col, model_col = control_columns[:2]
        with date_col:
            selected_date = st.date_input(
                "Research date",
                value=None,
                min_value=minimum_date,
                max_value=maximum_date,
                key="date",
                format="YYYY-MM-DD",
                help="Scores use only information that was publicly available by this date.",
            )
        with model_col:
            selected_model = st.segmented_control(
                "Scoring model",
                tuple(_MODELS_BY_SLUG),
                default=None,
                format_func=_MODEL_CONTROL_LABELS.__getitem__,
                key="model",
                width="stretch",
                help=(
                    "Quality + Value is the reviewed baseline. The experimental method "
                    "also considers price trend and stability."
                ),
            )
        if include_search:
            with control_columns[2]:
                search_value = st.text_input(
                    "Find a company",
                    key="q",
                    placeholder="Name or symbol",
                    help="Filter the current reviewed universe by company name or symbol.",
                )
        else:
            search_value = raw_search
        if not compact:
            st.markdown(
                '<div class="aios-command-footnote">'
                f"Reviewed range: {escape(display_date(certified_from))}–"
                f"{escape(display_date(certified_through))}"
                "</div>",
                unsafe_allow_html=True,
            )

    as_of_value = (selected_date or maximum_date).isoformat()
    model_slug = str(selected_model or "qv")
    _persist_widget_query("date", as_of_value)
    _persist_widget_query("model", model_slug)
    normalized_search = str(search_value or "").strip()
    _persist_widget_query("q", normalized_search)
    return as_of_value, _MODELS_BY_SLUG[model_slug], normalized_search


def _source_list(rows: list[tuple[str, str]]) -> str:
    return evidence_list(rows)


def _style_figure(figure, *, height: int, show_legend: bool = True) -> None:
    """Apply one accessible institutional chart treatment across the dashboard."""
    figure.update_layout(
        height=height,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
        font=dict(color="#3D3D3A", size=14),
        hoverlabel=dict(bgcolor="#FFFFFF", font_color="#141413", bordercolor="#B9B5AA"),
        legend=dict(
            bgcolor="rgba(0,0,0,0)",
            font=dict(color="#3D3D3A", size=14),
            title_font=dict(color="#3D3D3A", size=14),
        ),
        showlegend=show_legend,
    )
    figure.update_xaxes(gridcolor="#ECEAE2", zerolinecolor="#D9D6CC", linecolor="#D9D6CC")
    figure.update_yaxes(gridcolor="#ECEAE2", zerolinecolor="#D9D6CC", linecolor="#D9D6CC")


def _render_coverage_chart(report: dict) -> None:
    """Visualize only readiness controls that expose an exact numerator/denominator."""
    checks = _checks_by_key(report)
    rows = []
    for key in (
        "stable_security_identity",
        "fundamental_coverage",
        "price_history_coverage",
        "reviewed_price_freshness",
    ):
        check = checks.get(key, {})
        parsed = coverage_value(check.get("observed"))
        if parsed is None:
            continue
        covered, total, percentage = parsed
        rows.append(
            {
                "Control": check.get("label", key),
                "Coverage %": percentage,
                "Observed": f"{covered} / {total}",
                "Status": str(check.get("status", "warn")).title(),
            }
        )
    if not rows:
        st.info("Exact coverage counts are not available for this reviewed date.")
        return

    coverage = pd.DataFrame(rows)
    figure = px.bar(
        coverage,
        x="Coverage %",
        y="Control",
        orientation="h",
        text="Observed",
        color="Status",
        color_discrete_map={"Pass": "#437426", "Warn": "#805C1F", "Fail": "#A73D39"},
        hover_data={"Coverage %": ":.1f", "Observed": True, "Status": True},
    )
    figure.add_vline(x=95, line_dash="dot", line_color="#73726C")
    figure.update_traces(textposition="inside")
    figure.update_layout(
        xaxis=dict(range=[0, 105], title="Reviewed coverage (%)"),
        yaxis=dict(title=None, autorange="reversed"),
        legend_title_text="Gate",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(l=15, r=34, t=15, b=30),
    )
    _style_figure(
        figure,
        height=292,
        show_legend=coverage["Status"].nunique() > 1,
    )
    st.plotly_chart(figure, width="stretch")


def _render_overview(report: dict) -> None:
    """Render the decision-first command center with essential evidence visible."""
    checks = _checks_by_key(report)
    universe = checks.get("universe_membership", {})
    fundamentals = checks.get("fundamental_coverage", {})
    prices = checks.get("reviewed_price_freshness", {})
    macro = checks.get("macro_pit_readiness", {})
    integrity = checks.get("data_integrity", {})
    certification_current, _ = us_certification_freshness_message(
        report.get("certified_research_through")
    )

    try:
        monitor = load_paper_monitor()
    except Exception as exc:
        monitor = {
            "exists": False,
            "proposal": None,
            "error": str(exc),
        }
    operations = load_operating_summary()
    model = build_home_view_model(report, monitor, operations)
    unresolved = [row for row in operations.get("incidents", []) if row.get("state") != "resolved"]
    unresolved_cases = [
        row
        for row in operations.get("anomaly_cases", [])
        if row.get("state") != "resolved"
    ]
    critical_incident_count = sum(
        row.get("severity") == "critical" for row in unresolved
    )
    critical_case_count = sum(
        row.get("severity") == "critical" for row in unresolved_cases
    )
    proposal = monitor.get("proposal") if isinstance(monitor, dict) else None
    targets = proposal.get("targets", []) if isinstance(proposal, dict) else []

    _page_header(
        "AIOS Home",
        "Investment Command Center",
        "Know what is safe, what needs attention, and the next permitted action.",
        chips=[
            (
                f"Reviewed through {display_date(report.get('certified_research_through'))}",
                "success" if report.get("ready") else "danger",
            ),
            (
                "Latest session certified"
                if certification_current
                else "Daily certification pending",
                "success" if certification_current else "warning",
            ),
            ("Simulation only · No broker", "warning"),
        ],
    )
    _render_next_action(model.next_action)
    _render_kpi_grid(
        [
            (
                "Research",
                model.research.value,
                display_date(report.get("certified_research_through")),
                model.research.tone,
            ),
            (
                "Paper trial",
                model.paper.value,
                (
                    f"${monitor['summary']['equity']:,.0f} simulated"
                    if monitor.get("exists") and not monitor.get("error")
                    else "Local state unavailable"
                ),
                model.paper.tone,
            ),
            (
                "Operations",
                model.operations.value,
                (
                    f"{len(unresolved_cases)} data case(s) "
                    f"({critical_case_count} critical) · "
                    f"{len(unresolved)} incident(s) "
                    f"({critical_incident_count} critical)"
                ),
                model.operations.tone,
            ),
            (
                "Evidence universe",
                str(universe.get("observed", "Unavailable")),
                "Point-in-time members reviewed",
                "success" if report.get("ready") else "danger",
            ),
        ]
    )

    with st.container(key="home_layout"):
        evidence_col, paper_col = st.columns([1.15, 0.85], gap="large")
        with evidence_col:
            with st.container(border=True, key="home_evidence"):
                section_header(
                    "Research readiness",
                    "The evidence currently permitted for screening and comparison.",
                )
                st.markdown(
                    key_value_list(
                        [
                            (
                                "Certified decision close",
                                display_date(report.get("certified_research_through")),
                            ),
                            ("Point-in-time universe", universe.get("observed", "Unavailable")),
                            ("Company filings", fundamentals.get("observed", "Unavailable")),
                            ("Reviewed prices", prices.get("observed", "Unavailable")),
                            (
                                "Economic regime",
                                str(macro.get("observed") or "Unknown").replace("_", " ").title(),
                            ),
                            ("Database integrity", integrity.get("observed", "Unavailable")),
                        ]
                    ),
                    unsafe_allow_html=True,
                )
                st.caption(
                    "Raw provider dates never advance the certified decision close by themselves."
                )

            with st.container(border=True, key="home_targets"):
                section_header(
                    "Current proposal targets",
                    "The first five research targets in the local simulation proposal.",
                )
                if not targets:
                    st.info("No reviewed paper-trial proposal is available.")
                else:
                    identity_labels = load_identity_labels(report["certified_research_through"])
                    target_table = pd.DataFrame(
                        [
                            {
                                "Rank": row["factor_rank"],
                                "Company": company_symbol_label(
                                    identity_labels.get(row["ticker"]), row["ticker"]
                                ),
                                "Score": round(row["qv_score"], 1),
                                "Target": f"{row['target_weight']:.1%}",
                                "Business group": row["sector"],
                            }
                            for row in targets[:5]
                        ]
                    )
                    st.dataframe(
                        target_table,
                        hide_index=True,
                        width="stretch",
                        height=270,
                        row_height=44,
                        column_config={
                            "Rank": st.column_config.NumberColumn(width="small", format="%d"),
                            "Company": st.column_config.TextColumn(width="large"),
                            "Score": st.column_config.NumberColumn(width="small", format="%.1f"),
                            "Target": st.column_config.TextColumn(width="small"),
                        },
                    )
                    st.caption(
                        f"Showing 5 of {len(targets)} simulation-only targets. "
                        "These are not holdings or personal recommendations."
                    )

        with paper_col:
            with st.container(border=True, key="home_paper"):
                section_header(
                    "Paper-trial progress",
                    "Prospective, checksum-protected simulation only.",
                )
                if monitor.get("error"):
                    st.error("The local paper state could not be verified.")
                elif not monitor.get("exists"):
                    st.info("No verified local paper account is available.")
                else:
                    paper_model = build_paper_view_model(monitor)
                    render_pipeline_stepper(paper_model.stages)
                    summary = monitor["summary"]
                    st.markdown(
                        key_value_list(
                            [
                                ("Current stage", paper_model.status.value),
                                ("Simulated cash", f"${summary['cash']:,.2f}"),
                                ("Holdings", len(summary["holdings"])),
                            ]
                        ),
                        unsafe_allow_html=True,
                    )

            with st.container(border=True, key="home_incidents"):
                section_header(
                    "Open reviews",
                    "Data-quality cases and operating incidents remain separate and visible.",
                )
                if operations.get("error"):
                    st.warning(
                        "The operations ledger is unavailable; research evidence is unchanged."
                    )
                elif not unresolved_cases and not unresolved:
                    st.success(
                        "No unresolved data-review cases or operating incidents are recorded."
                    )
                else:
                    open_reviews = [
                        ("Data case", row)
                        for row in unresolved_cases
                    ] + [
                        ("Incident", row)
                        for row in unresolved
                    ]
                    open_reviews.sort(
                        key=lambda item: (
                            item[1].get("severity") != "critical",
                            item[0] != "Data case",
                            str(
                                item[1].get("case_id")
                                or item[1].get("incident_id")
                                or ""
                            ),
                        )
                    )
                    for review_kind, row in open_reviews[:4]:
                        severity = str(row.get("severity") or "unknown").title()
                        title = str(
                            row.get("title")
                            or f"Untitled {review_kind.lower()}"
                        )
                        tone = "danger" if row.get("severity") == "critical" else "warning"
                        st.markdown(
                            f'<div class="aios-incident-row {tone}">'
                            f"<span>{escape(review_kind)} · {escape(severity)}</span>"
                            f"<strong>{escape(title)}</strong>"
                            "</div>",
                            unsafe_allow_html=True,
                        )
                    if len(open_reviews) > 4:
                        st.caption(
                            f"{len(open_reviews) - 4} more open review(s) in Operations."
                        )


def _render_opportunity_map(
    df: pd.DataFrame,
    score_col: str,
    model_name: str,
    *,
    show_market_factors: bool,
) -> None:
    """Show the two core factor dimensions without labeling every security."""
    st.markdown("### Quality vs. Relative Value")
    st.caption(
        "Upper-right names combine stronger measured businesses with less-expensive "
        "valuations. Bubble size represents company size; color represents the selected score."
    )
    plot_df = df.dropna(subset=["Quality", "Value", score_col]).copy()
    if plot_df.empty:
        st.info("No stock has all inputs required for this comparison on the selected date.")
        return
    median_mcap = plot_df["Market Cap ($B)"].median()
    plot_df["Market Cap ($B)"] = plot_df["Market Cap ($B)"].fillna(
        median_mcap if pd.notna(median_mcap) else 100
    )
    label_tickers = set(plot_df.nlargest(12, score_col)["Ticker"])
    plot_df["Chart label"] = plot_df["Ticker"].where(plot_df["Ticker"].isin(label_tickers), "")
    hover_data = {
        "Chart label": False,
        "P/E": True,
        "EV/EBITDA": True,
        "ROIC %": True,
    }
    if show_market_factors:
        hover_data.update(
            {
                "12-1 Momentum %": True,
                "Annualized Volatility %": True,
            }
        )
    figure = px.scatter(
        plot_df,
        x="Value",
        y="Quality",
        text="Chart label",
        size="Market Cap ($B)",
        size_max=38,
        color=score_col,
        color_continuous_scale=["#D6E4F6", "#7196C5", "#3266AD"],
        labels={
            "Value": "Relative value score",
            "Quality": "Business quality score",
            score_col: f"{model_name} score",
            "Market Cap ($B)": "Company size ($ billions)",
            "12-1 Momentum %": "Past 12-to-1 month return %",
            "Annualized Volatility %": "Past price volatility %",
        },
        hover_name="Company + Symbol",
        hover_data=hover_data,
        range_x=[-5, 105],
        range_y=[-5, 105],
    )
    figure.update_traces(textposition="top center", textfont_size=10, marker_opacity=0.72)
    figure.add_hline(y=50, line_dash="dot", line_color="#73726C", opacity=0.7)
    figure.add_vline(x=50, line_dash="dot", line_color="#73726C", opacity=0.7)
    figure.update_layout(
        coloraxis_colorbar=dict(title="Research<br>score"),
        margin=dict(l=35, r=30, t=20, b=35),
    )
    _style_figure(figure, height=550)
    st.plotly_chart(figure, width="stretch")


def _render_ranked_universe(
    df: pd.DataFrame,
    *,
    rank_col: str,
    grade_col: str,
    score_col: str,
    show_market_factors: bool,
) -> None:
    """Render the lookup surface at one consistent universe grain."""
    section_header(
        "Ranked universe",
        "Sort the reviewed names and open company evidence directly. Missing scores "
        "remain visible and are never filled with estimates.",
    )
    table_columns = [
        rank_col,
        "Company",
        "Ticker",
        grade_col,
        score_col,
        "Quality",
        "Value",
    ]
    if show_market_factors:
        table_columns.extend(
            [
                "Momentum",
                "Low Volatility",
                "12-1 Momentum %",
                "Annualized Volatility %",
            ]
        )
    table_columns.append("Missing inputs")
    full_table = df.sort_values([score_col, "Ticker"], ascending=[False, True], na_position="last")[
        table_columns
    ].rename(
        columns={
            rank_col: "Rank",
            "Company": "Company",
            "Ticker": "Symbol",
            grade_col: "Grade",
            score_col: "Overall",
            "Quality": "Quality",
            "Value": "Value",
            "Momentum": "Trend",
            "Low Volatility": "Stability",
            "12-1 Momentum %": "12–1M return %",
            "Annualized Volatility %": "Volatility %",
            "Missing inputs": "Evidence status",
        }
    )
    full_table["Evidence status"] = full_table["Evidence status"].apply(
        lambda gaps: "Complete" if not gaps else f"{len(gaps)} gap{'s' if len(gaps) != 1 else ''}"
    )
    model_slug = _query_value("model")
    if model_slug not in _MODELS_BY_SLUG:
        model_slug = "qv"
    full_table["Review"] = full_table["Symbol"].map(
        lambda ticker: (
            f"?view=company&date={_query_value('date')}&model={model_slug}&company={ticker}"
        )
    )
    ordered_columns = [
        "Rank",
        "Company",
        "Symbol",
        "Overall",
        "Quality",
        "Value",
        "Grade",
    ]
    if show_market_factors:
        ordered_columns.extend(["Trend", "Stability", "12–1M return %", "Volatility %"])
    ordered_columns.extend(["Evidence status", "Review"])
    full_table = full_table[ordered_columns]
    st.dataframe(
        full_table,
        key="ranked_universe",
        width="stretch",
        hide_index=True,
        height=650,
        row_height=46,
        column_config={
            "Rank": st.column_config.NumberColumn(width="small", format="%d"),
            "Company": st.column_config.TextColumn(width="large"),
            "Symbol": st.column_config.TextColumn(width="small"),
            "Grade": st.column_config.TextColumn(width="small"),
            "Overall": st.column_config.NumberColumn(width="small", format="%.1f"),
            "Quality": st.column_config.NumberColumn(width="small", format="%.1f"),
            "Value": st.column_config.NumberColumn(width="small", format="%.1f"),
            "Trend": st.column_config.NumberColumn(width="small", format="%.1f"),
            "Stability": st.column_config.NumberColumn(width="small", format="%.1f"),
            "12–1M return %": st.column_config.NumberColumn(width="small", format="%.1f"),
            "Volatility %": st.column_config.NumberColumn(width="small", format="%.1f"),
            "Evidence status": st.column_config.TextColumn(width="medium"),
            "Review": st.column_config.LinkColumn(
                width="small",
                display_text="Open",
                help="Open the complete evidence view for this company.",
            ),
        },
    )
    st.caption(
        "Scores are cross-sectional research measures from 0–100, not expected "
        "returns or probabilities."
    )


def _render_research_coverage(
    df: pd.DataFrame,
    ranked_df: pd.DataFrame,
    *,
    grade_col: str,
    score_col: str,
) -> None:
    """Separate usable shortlists from explicit missing-evidence review."""
    shortlist_col, gap_col = st.columns([0.8, 1.2], gap="large")
    with shortlist_col, st.container(border=True):
        st.markdown("### Highest Complete Scores")
        st.caption("A research queue—not a buy list.")
        if ranked_df.empty:
            st.info("No complete score is available for this method.")
        else:
            shortlist = ranked_df.head(10)[
                ["Company", "Ticker", grade_col, score_col, "Quality", "Value"]
            ].rename(
                columns={
                    "Company": "Company",
                    "Ticker": "Symbol",
                    grade_col: "Grade",
                    score_col: "Overall",
                }
            )
            st.dataframe(shortlist, hide_index=True, width="stretch")

    with gap_col, st.container(border=True):
        st.markdown("### Missing-Evidence Queue")
        st.caption("These names are withheld rather than guessed.")
        gaps = df[df[score_col].isna()][
            [
                "Company",
                "Ticker",
                "Quality inputs",
                "Value inputs",
                "Market observations",
                "Missing data",
            ]
        ].rename(
            columns={
                "Company": "Company",
                "Ticker": "Symbol",
                "Quality inputs": "Quality inputs",
                "Value inputs": "Value inputs",
                "Market observations": "Price days",
                "Missing data": "Reason withheld",
            }
        )
        if gaps.empty:
            st.success("Every covered stock has a complete score for this method.")
        else:
            st.dataframe(gaps, hide_index=True, width="stretch", height=420)


_TIMER_LABELS = {
    "aios-us-daily.timer": "Recoverable weekday U.S. update",
    "aios-us-filings.timer": "Weekly company filings",
    "aios-backup.timer": "Weekly verified backup",
}


def _latest_ingest_failures(ingests: list[dict]) -> list[dict]:
    """Return failures only when they are the newest outcome for that ingest stream."""
    latest: dict[tuple[str, str], dict] = {}
    for row in ingests:
        key = (str(row.get("source")), str(row.get("table_name")))
        latest.setdefault(key, row)
    return [row for row in latest.values() if row.get("status") == "failed"]


def _next_operator_review(report: dict, operations: dict, monitor: dict | None) -> str:
    certification_current, _ = us_certification_freshness_message(
        report.get("certified_research_through")
    )
    daily_cycle = operations.get("daily_cycle")
    if not certification_current:
        if daily_cycle and daily_cycle.get("state") == "running":
            return "Allow the recoverable daily update to finish, then refresh this page."
        return "Run or inspect the recoverable daily update for the latest completed U.S. session."

    failures = [row for row in report["checks"] if row["status"] == "fail"]
    if failures:
        return f"Resolve the blocking gate: {failures[0]['label']}."

    unresolved_incidents = [
        row for row in operations.get("incidents", []) if row.get("state") != "resolved"
    ]
    unresolved_cases = [
        row
        for row in operations.get("anomaly_cases", [])
        if row.get("state") != "resolved"
    ]
    critical_cases = [
        row for row in unresolved_cases if row.get("severity") == "critical"
    ]
    if critical_cases:
        return f"Review critical data-quality case {critical_cases[0]['case_id']}."
    critical_incidents = [row for row in unresolved_incidents if row.get("severity") == "critical"]
    if critical_incidents:
        return f"Review critical incident {critical_incidents[0]['incident_id']}."
    if unresolved_cases:
        return f"Review data-quality case {unresolved_cases[0]['case_id']}."

    notification_summary = operations.get("notification_summary", {})
    if int(notification_summary.get("dead_letter", 0)) > 0:
        return (
            "Review alert messages that exhausted delivery attempts with "
            "`aios notifications --needs-review`."
        )
    route = operations.get("notification_route")
    if (
        route is not None
        and route.get("state") == "enabled"
        and not operations.get("email_config_matches")
    ):
        return "Repair the changed email configuration shown by `aios email-status`."
    email_scheduler = operations.get("email_scheduler", {})
    if route is not None and route.get("state") == "enabled" and not email_scheduler.get("enabled"):
        return "Repair the stopped email worker shown by `aios email-status`."

    newest_ingest_failures = _latest_ingest_failures(operations.get("ingests", []))
    if newest_ingest_failures:
        failed = newest_ingest_failures[0]
        return f"Review and retry the failed {failed['source']} → {failed['table_name']} ingest."

    scheduler = operations.get("scheduler", {})
    if not scheduler or any(not row.get("enabled") for row in scheduler.values()):
        return "Verify that every local scheduler timer is installed and enabled."
    if any(not row.get("runtime_verified") for row in scheduler.values()):
        return "Verify scheduler runtime from the normal logged-in desktop terminal."
    if any(row.get("service_result") not in {"success", "not-run"} for row in scheduler.values()):
        return "Inspect the most recent scheduled service failure before the next research cycle."

    backup = operations.get("backup", {})
    if backup.get("status") != "verified":
        return "Create or repair a checksum-verified local backup."

    forward = (monitor or {}).get("forward")
    if forward and not forward.get("ready"):
        return "Review forward-policy drift before recording another simulated execution."

    proposal = (monitor or {}).get("proposal")
    if proposal and not proposal.get("already_simulated"):
        if proposal.get("status") != "approved_for_supervised_simulation":
            return "Review the proposal's data or risk blocker; do not simulate it."
        timing_status = proposal.get("timing", {}).get("status")
        if timing_status == "waiting_for_scheduled_close":
            return "Wait for the scheduled U.S. close, then run the read-only paper review."
        if timing_status == "execution_window_open":
            return "Run `aios paper-review` before confirming any local simulation."
        if timing_status in {"expired", "invalid"}:
            return "Let current data refresh, then create a new prospective paper proposal."
        return "Review the proposal timing evidence before any local simulation."
    return "Review the next naturally triggered refresh, health, and backup cycle."


def _render_system_control(report: dict) -> None:
    """Render a source-backed operator workspace without fabricated telemetry."""
    _page_header(
        "Operations",
        "Operations & System Health",
        "Start with active exceptions, then verify automation, data flow, safeguards, "
        "and alert delivery from source-backed local evidence.",
        chips=[
            (
                f"Reviewed through {display_date(report.get('certified_research_through'))}",
                "success" if report.get("ready") else "danger",
            ),
            ("Read-only controls", "info"),
        ],
    )
    try:
        operations = load_system_operations()
    except Exception as exc:
        st.error("Operating evidence could not be loaded. Research data was not changed.")
        with st.expander("Technical detail"):
            st.code(str(exc))
        return
    try:
        monitor = load_paper_monitor()
    except Exception:
        monitor = None

    scheduler = operations.get("scheduler", {})
    enabled_timers = sum(bool(row.get("enabled")) for row in scheduler.values())
    universe_attestations = operations.get("universe_attestations", [])
    latest_universe_attestation = universe_attestations[0] if universe_attestations else None
    backup = operations.get("backup", {})
    backup_date = str(backup.get("created_at") or "")[:10] or None
    forward = (monitor or {}).get("forward")
    daily_cycle = operations.get("daily_cycle")
    certification_current, certification_detail = us_certification_freshness_message(
        report.get("certified_research_through")
    )

    incidents = operations.get("incidents", [])
    incident_summary = operations.get("incident_summary", {})
    unresolved_incidents = [row for row in incidents if row.get("state") != "resolved"]
    critical_incidents = [row for row in unresolved_incidents if row.get("severity") == "critical"]
    unresolved_count = int(incident_summary.get("unresolved", len(unresolved_incidents)))
    critical_count = int(incident_summary.get("critical_unresolved", len(critical_incidents)))
    anomaly_cases = operations.get("anomaly_cases", [])
    anomaly_summary = operations.get("anomaly_case_summary", {})
    unresolved_cases = [
        row for row in anomaly_cases if row.get("state") != "resolved"
    ]
    critical_cases = [
        row for row in unresolved_cases if row.get("severity") == "critical"
    ]
    unresolved_case_count = int(
        anomaly_summary.get("unresolved", len(unresolved_cases))
    )
    critical_case_count = int(
        anomaly_summary.get("critical_unresolved", len(critical_cases))
    )

    next_review = _next_operator_review(report, operations, monitor)
    if critical_cases:
        first_critical_case = critical_cases[0]
        case_label = "case" if critical_case_count == 1 else "cases"
        review_verb = "requires" if critical_case_count == 1 else "require"
        st.error(
            f"{critical_case_count} critical data-quality {case_label} {review_verb} review. "
            f"First issue: {first_critical_case.get('title') or 'Untitled data-quality case'}. "
            "Next action: inspect its governed evidence using the command below."
        )
        case_id = str(first_critical_case.get("case_id") or "").strip()
        if case_id:
            with st.expander("Technical inspection command"):
                st.code(f"aios anomaly-show {case_id}", language="bash")
    elif critical_incidents:
        first_critical = critical_incidents[0]
        incident_label = "incident" if critical_count == 1 else "incidents"
        review_verb = "requires" if critical_count == 1 else "require"
        st.error(
            f"{critical_count} critical operating {incident_label} {review_verb} review. "
            f"First issue: {first_critical.get('title') or 'Untitled operating incident'}. "
            "Next action: inspect the first incident using the technical command below."
        )
        incident_id = str(first_critical.get("incident_id") or "").strip()
        if incident_id:
            with st.expander("Technical inspection command"):
                st.code(f"aios alert-show {incident_id}", language="bash")
    elif daily_cycle and daily_cycle.get("state") in {"failed", "interrupted"}:
        st.error(f"The latest guarded daily workflow did not finish safely. {next_review}")
    else:
        st.info(f"Next required human review: {next_review}")

    _render_kpi_grid(
        [
            (
                "Research readiness",
                "Ready" if report["ready"] else "Blocked",
                display_date(report.get("certified_research_through")),
                "success" if report["ready"] else "danger",
            ),
            (
                "Local automation",
                f"{enabled_timers} / {len(TIMER_NAMES)}",
                "Enabled scheduler timers",
                "success" if enabled_timers == len(TIMER_NAMES) else "warning",
            ),
            (
                "Verified backup",
                (
                    display_date(backup_date)
                    if backup.get("status") == "verified"
                    else "Needs attention"
                ),
                "Latest checksum-verified snapshot",
                "success" if backup.get("status") == "verified" else "warning",
            ),
            (
                "Open reviews",
                str(unresolved_case_count + unresolved_count),
                (
                    f"{unresolved_case_count} data case(s) · "
                    f"{unresolved_count} incident(s)"
                ),
                (
                    "danger"
                    if critical_case_count or critical_count
                    else (
                        "warning"
                        if unresolved_case_count or unresolved_count
                        else "success"
                    )
                ),
            ),
        ]
    )

    if certification_current:
        st.success(certification_detail)
    else:
        st.warning(certification_detail)

    policy_col, readiness_col = st.columns(2, gap="medium", border=True)
    with policy_col:
        section_header(
            "Safeguard status",
            "Current recovery, policy, backup, and membership controls.",
        )
        warnings = [row for row in report["checks"] if row["status"] == "warn"]
        control_rows: list[tuple[str, str, str, str]] = []
        if daily_cycle is None:
            control_rows.append(
                (
                    "Daily update",
                    "Not verified",
                    "No recoverable daily update has completed yet.",
                    "warning",
                )
            )
        elif daily_cycle["state"] == "success":
            control_rows.append(
                (
                    "Daily update",
                    "Passed",
                    f"Completed for {display_date(daily_cycle['target_session'])}.",
                    "success",
                )
            )
        elif daily_cycle["state"] == "running":
            control_rows.append(
                (
                    "Daily update",
                    "Running",
                    f"Updating {display_date(daily_cycle['target_session'])}.",
                    "info",
                )
            )
        else:
            control_rows.append(
                (
                    "Daily update",
                    "Failed",
                    "The latest workflow did not finish safely; startup catch-up will retry.",
                    "danger",
                )
            )
        if forward is None:
            control_rows.append(
                (
                    "Forward policy",
                    "Unavailable",
                    "Policy evidence could not be verified.",
                    "warning",
                )
            )
        elif forward["ready"]:
            control_rows.append(
                (
                    "Forward policy",
                    "Unchanged",
                    "The frozen factor, risk, cost, and paper rules still match.",
                    "success",
                )
            )
        else:
            control_rows.append(
                (
                    "Forward policy",
                    "Drift detected",
                    "A governed policy change requires review.",
                    "danger",
                )
            )
        if backup.get("status") == "verified":
            control_rows.append(
                (
                    "Local backup",
                    "Verified",
                    f"Checksums match across {backup['files']} file(s).",
                    "success",
                )
            )
        elif backup.get("status") == "failed":
            control_rows.append(
                (
                    "Local backup",
                    "Failed",
                    "The newest backup did not pass checksum verification.",
                    "danger",
                )
            )
        else:
            control_rows.append(
                (
                    "Local backup",
                    "Unavailable",
                    "No checksum-verified local backup is available.",
                    "warning",
                )
            )
        if latest_universe_attestation is None:
            control_rows.append(
                (
                    "Membership evidence",
                    "Not reviewed",
                    "No automatic membership evidence review has run yet.",
                    "warning",
                )
            )
        elif latest_universe_attestation["status"] == "accepted_no_change":
            control_rows.append(
                (
                    "Membership evidence",
                    "No change",
                    "Reviewed through "
                    f"{display_date(latest_universe_attestation['requested_coverage_through'])}.",
                    "success",
                )
            )
        else:
            control_rows.append(
                (
                    "Membership evidence",
                    "Review required",
                    "The check stopped safely; no reference dates were extended.",
                    "danger",
                )
            )
        render_control_list(control_rows)
        st.caption(f"{len(warnings)} non-blocking readiness warning(s).")
        if backup.get("status") == "verified":
            with st.expander("Backup checksum"):
                st.code(backup["manifest_sha256"])
    with readiness_col:
        section_header(
            "Reviewed data coverage",
            "Counts reconcile directly to the current fail-closed readiness report.",
        )
        _render_coverage_chart(report)

    section_header(
        "Source freshness",
        "Provider dates are shown separately from the certified decision close.",
    )
    source_clock = pd.DataFrame(
        [
            {
                "Evidence": "Certified research decision",
                "Through": display_date(report["certified_research_through"]),
            },
            {"Evidence": "Market prices", "Through": display_date(report["raw_prices_through"])},
            {
                "Evidence": "Company filings",
                "Through": display_date(report["fundamentals_through"]),
            },
            {
                "Evidence": "Economic releases",
                "Through": display_date(report["macro_releases_through"]),
            },
        ]
    )
    st.dataframe(source_clock, hide_index=True, width="stretch")
    st.caption("Raw provider dates cannot advance the certified decision date by themselves.")

    warning_checks = [row for row in report["checks"] if row["status"] == "warn"]
    if warning_checks:
        with st.expander(f"Historical and readiness warnings ({len(warning_checks)})"):
            for row in warning_checks:
                st.markdown(f"**{row['label']} — {row['observed']}**")
                st.write(row["detail"])

    section_header(
        "Automation & recovery",
        "Runtime status is queried with a strict timeout. If the user service bus is "
        "unavailable, installation evidence is shown as unverified instead of guessed.",
    )
    if operations.get("linger_enabled") is True:
        st.success("Automatic updates remain active after desktop logout while the computer is on.")
    elif operations.get("linger_enabled") is False:
        st.warning("Automatic updates pause after desktop logout and catch up at the next login.")
    else:
        st.info("Keep-running-after-logout status could not be verified.")
    if operations.get("scheduler_error"):
        st.error("Scheduler state could not be read.")
        with st.expander("Technical detail"):
            st.code(operations["scheduler_error"])
    elif scheduler:
        scheduler_table = pd.DataFrame(
            [
                {
                    "Job": _TIMER_LABELS.get(timer, timer),
                    "Enabled": "Yes" if row.get("enabled") else "No",
                    "Waiting": (
                        "Yes"
                        if row.get("active")
                        else ("No" if row.get("runtime_verified") else "Not verified")
                    ),
                    "Last run": row.get("last_run", "Unknown"),
                    "Result": str(row.get("service_result", "Unknown")).replace("-", " ").title(),
                    "Next run": row.get("next_trigger", "Unknown"),
                    "Runtime verified": "Yes" if row.get("runtime_verified") else "No",
                }
                for timer, row in scheduler.items()
            ]
        )
        st.dataframe(scheduler_table, hide_index=True, width="stretch")
    else:
        st.warning("No scheduler evidence is available.")

    section_header(
        "Data review cases",
        "Deduplicated anomaly findings stay separate from operating incidents. "
        "Review actions never repair source data or change paper state.",
    )
    latest_anomaly_scan = operations.get("latest_anomaly_scan")
    if operations.get("incident_error"):
        st.error("Data-review case history could not be opened.")
    elif anomaly_cases:
        anomaly_table = pd.DataFrame(
            [
                {
                    "Last seen": pd.to_datetime(
                        row["last_seen_at"],
                        errors="coerce",
                    ).strftime("%b %d, %Y %H:%M UTC"),
                    "Severity": str(row["severity"]).title(),
                    "State": str(row["state"]).title(),
                    "Rule": f"{row['rule_id']} · {row['rule_version']}",
                    "Subject": f"{row['subject_type']} · {row['subject_id']}",
                    "Owner": row.get("owner") or "Unassigned",
                    "Summary": row["title"],
                    "Case ID": row["case_id"],
                }
                for row in anomaly_cases
            ]
        )
        st.dataframe(anomaly_table, hide_index=True, width="stretch", height=360)
        st.caption(
            "Inspect with `aios anomaly-show CASE_REF`; acknowledge with "
            "`aios anomaly-ack CASE_REF`. Acknowledgement and resolution require "
            "the current evidence hash, named owner, and audit note in the CLI."
        )
    elif latest_anomaly_scan is None:
        st.info("No governed anomaly scan or data-review case is recorded yet.")
    else:
        st.success("The latest governed anomaly scan has no recorded review cases.")
    if latest_anomaly_scan is not None:
        recorded_at = pd.to_datetime(
            latest_anomaly_scan.get("recorded_at"),
            errors="coerce",
        ).strftime("%b %d, %Y %H:%M UTC")
        source_boundary_at = pd.to_datetime(
            latest_anomaly_scan.get("source_boundary_at"),
            errors="coerce",
        ).strftime("%b %d, %Y %H:%M UTC")
        st.caption(
            f"Latest scan recorded: {recorded_at} · "
            f"source boundary: {source_boundary_at} · "
            f"{latest_anomaly_scan.get('observation_count', 0)} observation(s) · "
            f"rule bundle {latest_anomaly_scan.get('rule_bundle_version', 'unknown')}."
        )

    section_header(
        "Research experiments",
        "Every registered backtest binds exact code, data and policy identity "
        "before its metrics are trusted. Comparison never picks a winner; "
        "activation still goes through the existing forward-restart gate.",
    )
    try:
        experiments = load_research_experiments()
    except Exception as exc:
        st.error("Research experiment registry could not be read.")
        with st.expander("Technical detail"):
            st.code(str(exc))
        experiments = []
    if experiments:

        def _pct(value: object) -> str:
            return f"{value:.1%}" if isinstance(value, (int, float)) else "unknown"

        experiment_table = pd.DataFrame(
            [
                {
                    "Recorded": pd.to_datetime(
                        doc.get("recorded_at"), errors="coerce"
                    ).strftime("%b %d, %Y %H:%M UTC"),
                    "Experiment ID": doc["experiment_id"],
                    "Purpose": str(doc.get("purpose", "")).title(),
                    "Factor model": doc.get("parameters", {}).get("factor_model", "unknown"),
                    "Universe": doc.get("parameters", {}).get("universe_id", "unknown"),
                    "Regime return": _pct(
                        doc.get("metrics", {}).get("regime", {}).get("cumulative_return")
                    ),
                    "Baseline return": _pct(
                        doc.get("metrics", {}).get("baseline", {}).get("cumulative_return")
                    ),
                    "Policy": (
                        f"{doc.get('policy', {}).get('name', 'unknown')} · "
                        f"{doc.get('policy', {}).get('version', 'unknown')}"
                        if doc.get("policy")
                        else "unversioned"
                    ),
                    "Commit": (
                        f"{doc.get('git', {}).get('commit_sha', 'unknown')[:8]}"
                        f"{' (dirty)' if doc.get('git', {}).get('dirty') else ''}"
                    ),
                }
                for doc in sorted(
                    experiments, key=lambda d: d.get("recorded_at", ""), reverse=True
                )
            ]
        )
        st.dataframe(experiment_table, hide_index=True, width="stretch", height=280)
        st.caption(
            "Compare two or more with `aios compare-experiments ID ID...`; "
            "register a new one with `aios backtest-qv --register-experiment "
            "--experiment-purpose exploratory|frozen|holdout`."
        )
    else:
        st.info("No research experiment has been registered yet.")

    section_header(
        "Data pipeline",
        "Recent source outcomes stay visible so a failed or partial ingest cannot hide.",
    )
    ingests = operations.get("ingests", [])
    if ingests:
        ingest_table = pd.DataFrame(ingests)
        ingest_table["finished_at"] = pd.to_datetime(
            ingest_table["finished_at"], errors="coerce"
        ).dt.strftime("%b %d, %Y %H:%M")
        ingest_table["status"] = ingest_table["status"].fillna("unknown").str.title()
        ingest_table = ingest_table.rename(
            columns={
                "source": "Source",
                "table_name": "Dataset",
                "rows_inserted": "Rows added",
                "rows_rejected": "Rows rejected",
                "finished_at": "Finished",
                "status": "Status",
                "error": "Failure detail",
            }
        )[
            [
                "Finished",
                "Source",
                "Dataset",
                "Status",
                "Rows added",
                "Rows rejected",
                "Failure detail",
            ]
        ]
        st.dataframe(ingest_table, hide_index=True, width="stretch", height=470)
    else:
        st.info("No ingest outcomes have been recorded.")

    section_header(
        "Incidents & alert delivery",
        "Incidents are the authoritative local record. Channel-neutral alert copies are "
        "stored separately and can never replace or hide that history.",
    )
    if operations.get("incident_error"):
        st.error("Local incident history could not be opened.")
        with st.expander("Technical detail"):
            st.code(operations["incident_error"])
    elif incidents:
        incident_table = pd.DataFrame(
            [
                {
                    "Last seen": pd.to_datetime(row["last_seen_at"], errors="coerce").strftime(
                        "%b %d, %Y %H:%M UTC"
                    ),
                    "Severity": str(row["severity"]).title(),
                    "State": str(row["state"]).title(),
                    "Source": row["source_job"],
                    "Summary": row["title"],
                    "Occurrences": row["occurrence_count"],
                    "Incident ID": row["incident_id"],
                }
                for row in incidents
            ]
        )
        st.dataframe(incident_table, hide_index=True, width="stretch", height=330)
        st.caption(
            "Inspect with `aios alert-show INCIDENT_REF`; acknowledge with "
            "`aios alert-ack INCIDENT_REF`. Resolution never deletes history."
        )
    else:
        st.success("No local operating incidents have been recorded.")

    notification_summary = operations.get("notification_summary", {})
    notifications = operations.get("notifications", [])
    held_count = int(notification_summary.get("held", 0))
    waiting_count = int(notification_summary.get("pending", 0)) + int(
        notification_summary.get("leased", 0)
    )
    dead_letter_count = int(notification_summary.get("dead_letter", 0))
    notification_route = operations.get("notification_route")
    email_enabled = notification_route is not None and notification_route.get("state") == "enabled"
    email_config_matches = bool(operations.get("email_config_matches"))
    email_scheduler = operations.get("email_scheduler", {})
    email_worker_enabled = bool(email_scheduler.get("enabled"))
    email_worker_verified = bool(email_scheduler.get("runtime_verified"))
    alert_status, waiting_status = st.columns(2)
    alert_status.metric(
        "Email alerts",
        (
            "On"
            if email_enabled
            and email_config_matches
            and email_worker_enabled
            and email_worker_verified
            else "Off"
        ),
    )
    waiting_status.metric("Messages waiting", waiting_count)

    if operations.get("notification_error"):
        st.error("Alert-delivery history could not be opened. Incident history remains available.")
        with st.expander("Technical detail"):
            st.code(operations["notification_error"])
    else:
        if (
            email_enabled
            and email_config_matches
            and email_worker_enabled
            and email_worker_verified
        ):
            st.success(
                "Email is enabled for new incident changes. Existing held messages "
                f"remain local ({held_count} held)."
            )
        elif email_enabled and email_config_matches and not email_worker_enabled:
            st.error(
                "The email route is enabled, but its optional delivery timer is off. "
                "No queued message will be sent automatically. Run `aios email-status`."
            )
        elif email_enabled and email_config_matches:
            st.warning(
                "Email is configured and enabled, but this dashboard could not verify "
                "the delivery timer. Check `aios email-status` in a desktop terminal."
            )
        elif email_enabled:
            st.error(
                "Email is enabled, but the current local SMTP configuration no longer "
                "matches the activated route. Delivery fails closed. Run `aios email-status`."
            )
        else:
            st.info(
                "External email is off. Incidents are still saved locally, and "
                f"{held_count} alert copy/copies are deliberately held on this computer."
            )
        if waiting_count:
            st.warning(
                f"{waiting_count} message(s) are waiting to retry. Local incident history is safe."
            )
        if dead_letter_count:
            st.error(
                f"{dead_letter_count} message(s) could not be delivered after the "
                "bounded retry policy. Run `aios notifications --needs-review`."
            )
        if notifications:
            state_labels = {
                "held": "Held locally",
                "pending": "Waiting to retry",
                "leased": "Sending now",
                "delivered": "Sent",
                "dead_letter": "Needs review",
            }
            notification_table = pd.DataFrame(
                [
                    {
                        "Created": pd.to_datetime(row["created_at"], errors="coerce").strftime(
                            "%b %d, %Y %H:%M UTC"
                        ),
                        "State": state_labels.get(row["state"], row["state"]),
                        "Event": str(row["event_type"]).replace("_", " ").title(),
                        "Severity": str(row["severity"]).title(),
                        "Summary": row["title"],
                        "Attempts": row["attempt_count"],
                        "Notification ID": row["notification_id"],
                    }
                    for row in notifications
                ]
            )
            st.dataframe(
                notification_table,
                hide_index=True,
                width="stretch",
                height=300,
            )
            st.caption(
                "Inspect one message with `aios notification-show NOTIFICATION_REF`. "
                "Sending and retry controls stay outside this read-only dashboard."
            )


# ----------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------
st.sidebar.markdown(
    """
    <div class="aios-brand">
      <div class="aios-brand-mark" aria-hidden="true">◆</div>
      <div>
        <div class="aios-brand-name">AIOS</div>
        <div class="aios-brand-sub">AI Investment OS</div>
      </div>
    </div>
    <div class="aios-market-tag">U.S. reference market</div>
    """,
    unsafe_allow_html=True,
)

try:
    readiness = load_us_readiness()
except (duckdb.IOException, FileNotFoundError):
    st.sidebar.markdown(
        '<span class="aios-chip warning">● Safe update in progress</span>',
        unsafe_allow_html=True,
    )
    st.info(
        "AIOS is updating its local research database. The previous reviewed data remains "
        "safe; this page will be available again when the short database step finishes."
    )
    st.stop()
except ValueError as exc:
    st.sidebar.markdown(
        '<span class="aios-chip danger">● Readiness evidence blocked</span>',
        unsafe_allow_html=True,
    )
    st.error(
        "AIOS could not validate the current readiness evidence. New decisions remain "
        "blocked, and no local research or paper state was changed."
    )
    with st.expander("Technical detail"):
        st.code(str(exc))
    st.stop()
certified_from = readiness["certified_research_from"]
certified_through = readiness["certified_research_through"]
certification_current, certification_detail = us_certification_freshness_message(certified_through)
if readiness["ready"] and certification_current:
    sidebar_status = '<span class="aios-chip success">● Research gates passed</span>'
elif readiness["ready"]:
    sidebar_status = '<span class="aios-chip warning">● Daily update pending</span>'
else:
    sidebar_status = '<span class="aios-chip danger">● New decisions blocked</span>'

st.sidebar.markdown(sidebar_status, unsafe_allow_html=True)
st.sidebar.caption(f"Reviewed through {display_date(certified_through)}")
st.sidebar.divider()
st.sidebar.markdown('<div class="aios-nav-label">Navigate</div>', unsafe_allow_html=True)
raw_view = _query_value("view")
requested_view_slug = raw_view if raw_view in _VIEWS_BY_SLUG else "today"
default_workspace = "research" if requested_view_slug == "company" else requested_view_slug
workspace_marker = "_aios_workspace_route"
if "workspace" not in st.session_state or st.session_state.get(workspace_marker) != raw_view:
    st.session_state["workspace"] = default_workspace
st.session_state[workspace_marker] = raw_view
workspace_slug = st.sidebar.radio(
    "Workspace",
    tuple(_PRIMARY_NAVIGATION),
    index=None,
    format_func=_PRIMARY_NAVIGATION.__getitem__,
    key="workspace",
    label_visibility="collapsed",
)
workspace_slug = str(workspace_slug or default_workspace)
if workspace_slug != default_workspace:
    view_slug = workspace_slug
    st.query_params["view"] = view_slug
    st.session_state[workspace_marker] = view_slug
else:
    view_slug = requested_view_slug
if _query_value("view") != view_slug:
    st.query_params["view"] = view_slug
view = _VIEWS_BY_SLUG[view_slug]
default_as_of = certified_through or (date.today() - timedelta(days=1)).isoformat()
as_of = default_as_of
ranking_model = MODEL_QV_LABEL

st.sidebar.divider()
st.sidebar.markdown(
    '<div class="aios-sidebar-footer">'
    "<strong>Local simulation</strong>"
    "<span>Stored on this computer · Tamper-evident</span>"
    "<span>No broker connection</span>"
    "</div>",
    unsafe_allow_html=True,
)


df = pd.DataFrame()


# ----------------------------------------------------------------------
# VIEW 1: TODAY
# ----------------------------------------------------------------------
if view == VIEW_HOME:
    with st.container(key="overview_workspace"):
        _render_overview(readiness)


# ----------------------------------------------------------------------
# VIEW 2: RESEARCH
# ----------------------------------------------------------------------
elif view == VIEW_RANKINGS:
    _page_header(
        "Research",
        "Research Universe",
        "Screen the reviewed market, compare factor trade-offs, and open a company "
        "only when its evidence deserves deeper work.",
        chips=[
            (
                f"Certified through {display_date(certified_through)}",
                "success" if readiness.get("ready") else "danger",
            ),
            ("Research only · Not a forecast", "info"),
        ],
    )
    if certified_from is None or certified_through is None:
        st.error("No complete reviewed U.S. research window is available yet.")
        st.stop()
    as_of, ranking_model, company_search = _research_context_controls(
        certified_from,
        certified_through,
    )
    use_qvml = model_key(ranking_model) == "qvml"
    rank_col = "QVML Rank" if use_qvml else "Rank"
    grade_col = "QVML Grade" if use_qvml else "Grade"
    score_col = "QVML Score" if use_qvml else "QV Score"
    model_name = "Four-factor" if use_qvml else "Quality + Value"

    try:
        full_df = load_composite(as_of, include_market_factors=use_qvml)
    except Exception as exc:
        st.error(
            "The research scores could not be calculated. Check the selected evidence "
            "date and refresh state, then try again."
        )
        with st.expander("Technical error details"):
            st.code(str(exc))
        st.stop()
    df = full_df
    if company_search:
        search_mask = df["Company"].fillna("").str.contains(
            company_search, case=False, regex=False
        ) | df["Ticker"].fillna("").str.contains(company_search, case=False, regex=False)
        df = df[search_mask].copy()

    if df.empty:
        st.info(
            "No reviewed company matches the current scope. Clear the search or choose "
            "another research date."
        )
        st.stop()

    macro_regime = friendly_regime(full_df["Macro regime"].iloc[0])
    quality_weight = full_df["Quality weight %"].iloc[0]
    value_weight = full_df["Value weight %"].iloc[0]
    macro_pit_ready = bool(full_df["Macro PIT ready"].iloc[0])
    if use_qvml:
        st.warning(
            "The four-factor method is experimental. It completed the engineering test, "
            "but performed worse than the simpler Quality + Value method in the short "
            "historical window. Do not treat it as a stronger buy signal."
        )
    if not macro_pit_ready:
        st.warning(
            "The economic picture was incomplete on this date, so the app is using its "
            "standard 60% business-quality and 40% relative-value mix."
        )

    full_ranked_df = full_df[full_df[score_col].notna()].sort_values(
        [score_col, "Ticker"], ascending=[False, True]
    )
    ranked_df = df[df[score_col].notna()].sort_values(
        [score_col, "Ticker"], ascending=[False, True]
    )
    action_unverified = int(
        full_df["Missing inputs"]
        .apply(lambda values: "market:corporate_actions_unverified" in values)
        .sum()
    )
    if use_qvml and action_unverified:
        st.error(
            f"{action_unverified} stocks do not have fully verified dividend and stock-split "
            "history. Their four-factor scores are hidden instead of being guessed."
        )

    complete_count = len(full_ranked_df)
    withheld_count = len(full_df) - complete_count
    coverage_pct = complete_count / len(full_df) * 100
    _render_kpi_grid(
        [
            (
                "Reviewed universe",
                str(len(full_df)),
                f"{len(df)} shown in the current scope",
                "info",
            ),
            (
                "Complete scores",
                str(complete_count),
                "Names with every required input",
                "success" if complete_count else "danger",
            ),
            (
                "Score coverage",
                f"{coverage_pct:.1f}%",
                f"{complete_count} of {len(full_df)} reviewed names",
                "success" if coverage_pct >= 95 else "warning",
            ),
            (
                "Withheld",
                str(withheld_count),
                "Missing or unverified evidence",
                "warning" if withheld_count else "success",
            ),
        ]
    )
    if use_qvml:
        weight_copy = (
            f"quality {full_df['QVML Quality weight %'].iloc[0]:.0f}% · "
            f"value {full_df['QVML Value weight %'].iloc[0]:.0f}% · "
            f"trend {full_df['QVML Momentum weight %'].iloc[0]:.0f}% · "
            f"stability {full_df['QVML Low Volatility weight %'].iloc[0]:.0f}%"
        )
    else:
        weight_copy = f"quality {quality_weight:.0f}% · value {value_weight:.0f}%"
    st.markdown(
        '<div class="aios-context-strip">'
        "<strong>Economic backdrop</strong>"
        f"<span>{escape(macro_regime.title())}</span>"
        f"<span>{escape(display_date(as_of))} evidence close</span>"
        f"<span>{escape(weight_copy)}</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    research_surface = _research_surface_selector()
    if research_surface == "Opportunity Map":
        _render_opportunity_map(
            df,
            score_col,
            model_name,
            show_market_factors=use_qvml,
        )
    elif research_surface == "Data Coverage":
        _render_research_coverage(
            df,
            ranked_df,
            grade_col=grade_col,
            score_col=score_col,
        )
    else:
        _render_ranked_universe(
            df,
            rank_col=rank_col,
            grade_col=grade_col,
            score_col=score_col,
            show_market_factors=use_qvml,
        )

# ----------------------------------------------------------------------
# VIEW 3: COMPANY DETAIL
# ----------------------------------------------------------------------
elif view == VIEW_DETAILS:
    _page_header(
        "Research / Company evidence",
        "Company Detail",
        "Trace one company from its relative score to the evidence and explicit gaps "
        "behind that result.",
        chips=[
            (
                f"Certified through {display_date(certified_through)}",
                "success" if readiness.get("ready") else "danger",
            ),
            ("Contextual research detail", "info"),
        ],
    )
    if certified_from is None or certified_through is None:
        st.error("No complete reviewed U.S. research window is available yet.")
        st.stop()
    as_of, ranking_model, _ = _research_context_controls(
        certified_from,
        certified_through,
        include_search=False,
        compact=True,
    )
    try:
        df = load_composite(as_of, include_market_factors=True)
    except Exception as exc:
        st.error(
            "Company evidence could not be calculated for this research date. "
            "Choose another reviewed date or inspect the refresh state."
        )
        with st.expander("Technical error details"):
            st.code(str(exc))
        st.stop()
    st.link_button(
        "← Back to research universe",
        (
            f"?view=research&date={as_of}&model="
            f"{_MODEL_SLUGS.get(ranking_model, 'qv')}&surface=ranked"
        ),
        type="secondary",
    )

    label_to_ticker = (
        dict(
            sorted(
                zip(df["Company + Symbol"], df["Ticker"], strict=True),
                key=lambda item: item[0],
            )
        )
        if "Company + Symbol" in df.columns
        else {}
    )
    if not label_to_ticker:
        st.info(
            "No stocks are available for this research date. Choose another date or "
            "refresh the underlying company and price information."
        )
        st.stop()

    ticker_to_label = {str(ticker).upper(): label for label, ticker in label_to_ticker.items()}
    company_tickers = sorted(ticker_to_label, key=ticker_to_label.__getitem__)
    raw_company = _query_value("company").strip().upper()
    default_company = raw_company if raw_company in ticker_to_label else company_tickers[0]
    _hydrate_widget_from_url("company", default_company)
    ticker = st.selectbox(
        "Choose a company",
        company_tickers,
        index=None,
        format_func=ticker_to_label.__getitem__,
        key="company",
        help="Each company is shown with its market symbol, such as Apple Inc. (AAPL).",
    )
    ticker = str(ticker or default_company)
    _persist_widget_query("company", ticker)
    row = df[df["Ticker"] == ticker].iloc[0]
    use_qvml = model_key(ranking_model) == "qvml"
    score_col = "QVML Score" if use_qvml else "QV Score"
    grade_col = "QVML Grade" if use_qvml else "Grade"
    model_name = "Four-factor" if use_qvml else "Quality + Value"
    displayed_score = row[score_col]
    displayed_grade = row[grade_col]

    score_text = f"{displayed_score:.1f}" if pd.notna(displayed_score) else "N/A"
    grade_text = str(displayed_grade) if pd.notna(displayed_grade) else "N/A"
    st.markdown(
        f'<section class="aios-company-hero">'
        f'<div class="aios-company-name">{escape(str(row["Company + Symbol"]))}</div>'
        f'<div class="aios-company-context">{escape(model_name)} research evidence · '
        f"{escape(display_date(as_of))}</div>"
        "</section>",
        unsafe_allow_html=True,
    )
    economic_backdrop = friendly_regime(row["Macro regime"])
    if use_qvml:
        score_mix_copy = (
            f"business quality "
            f"{row['QVML Quality weight %']:.0f}%, relative value "
            f"{row['QVML Value weight %']:.0f}%, price trend "
            f"{row['QVML Momentum weight %']:.0f}%, price stability "
            f"{row['QVML Low Volatility weight %']:.0f}% · experimental"
        )
    else:
        score_mix_copy = (
            f"business quality "
            f"{row['Quality weight %']:.0f}% and relative value "
            f"{row['Value weight %']:.0f}% · economic information complete: "
            f"{'yes' if row['Macro PIT ready'] else 'no; standard mix used'}"
        )
    st.markdown(
        '<div class="aios-context-strip">'
        "<strong>Relative research grade · not buy/sell</strong>"
        f"<span>{escape(economic_backdrop.title())} backdrop</span>"
        f"<span>{escape(score_mix_copy)}</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    _render_kpi_grid(
        [
            (
                "Overall Score",
                f"{score_text} / 100" if score_text != "N/A" else "N/A",
                f"{model_name} cross-sectional score.",
                "",
            ),
            (
                "Research Grade",
                grade_text,
                "Relative grade within the covered universe.",
                "",
            ),
            (
                "Business Quality",
                f"{row['Quality']:.1f} / 100" if pd.notna(row["Quality"]) else "N/A",
                "Profitability and financial health versus covered peers.",
                "",
            ),
            (
                "Relative Value",
                f"{row['Value']:.1f} / 100" if pd.notna(row["Value"]) else "N/A",
                "Valuation relative to other covered companies.",
                "",
            ),
        ]
    )

    evidence_left, evidence_right = st.columns(2, gap="medium", border=True)
    with evidence_left:
        st.markdown("### Business Evidence")
        st.caption("Profitability, cash generation, margins, and financial health.")
        st.markdown(
            _source_list(
                [
                    (
                        "Return on invested capital",
                        f"{row['ROIC %']}%" if pd.notna(row["ROIC %"]) else "N/A",
                    ),
                    (
                        "Free-cash-flow margin",
                        f"{row['FCF Margin %']}%" if pd.notna(row["FCF Margin %"]) else "N/A",
                    ),
                    (
                        "Gross margin",
                        f"{row['Gross Margin %']}%" if pd.notna(row["Gross Margin %"]) else "N/A",
                    ),
                    (
                        "Financial-health checks",
                        f"{row['Piotroski F']}/9" if pd.notna(row["Piotroski F"]) else "N/A",
                    ),
                ]
            ),
            unsafe_allow_html=True,
        )
    with evidence_right:
        st.markdown("### Valuation Evidence")
        st.caption("Lower multiples can mean cheaper—or reflect genuine business risk.")
        st.markdown(
            _source_list(
                [
                    ("Price / earnings", row["P/E"] if pd.notna(row["P/E"]) else "N/A"),
                    (
                        "Enterprise value / EBITDA",
                        row["EV/EBITDA"] if pd.notna(row["EV/EBITDA"]) else "N/A",
                    ),
                    (
                        "Price / free cash flow",
                        row["P/FCF"] if pd.notna(row["P/FCF"]) else "N/A",
                    ),
                    (
                        "Enterprise value / sales",
                        row["EV/Sales"] if pd.notna(row["EV/Sales"]) else "N/A",
                    ),
                    ("Price / book value", row["P/B"] if pd.notna(row["P/B"]) else "N/A"),
                ]
            ),
            unsafe_allow_html=True,
        )

    with st.container(border=True, key="company_market_context"):
        section_header(
            "Market context",
            "Price and size context is visible for interpretation; it is not a forecast.",
        )
        st.markdown(
            _source_list(
                [
                    (
                        "Past 12-to-1 month return",
                        f"{row['12-1 Momentum %']:.1f}%"
                        if pd.notna(row["12-1 Momentum %"])
                        else "N/A",
                    ),
                    (
                        "Annualized price volatility",
                        f"{row['Annualized Volatility %']:.1f}%"
                        if pd.notna(row["Annualized Volatility %"])
                        else "N/A",
                    ),
                    ("Share price", f"${row['Price']}" if pd.notna(row["Price"]) else "N/A"),
                    (
                        "Market value",
                        f"${row['Market Cap ($B)']} billion"
                        if pd.notna(row["Market Cap ($B)"])
                        else "N/A",
                    ),
                ]
            ),
            unsafe_allow_html=True,
        )

    # The row already contains universe-relative scores. Reusing it avoids the
    # former five full-universe recomputations on every ticker selection.
    factor_profile = [
        ("Business quality", row["Quality"]),
        ("Relative value", row["Value"]),
    ]
    if use_qvml:
        factor_profile.extend(
            [
                ("Price trend", row["Momentum"]),
                ("Price stability", row["Low Volatility"]),
            ]
        )
    available_profile = [
        (label, float(value)) for label, value in factor_profile if pd.notna(value)
    ]
    if available_profile:
        st.subheader("Selected Model Profile")
        st.caption(
            "A score above 50 is above the covered-universe midpoint. These are relative "
            "research measures, not forecasts."
        )
        profile_df = pd.DataFrame(available_profile, columns=["Factor", "Score"])
        fig2 = px.bar(
            profile_df,
            x="Score",
            y="Factor",
            orientation="h",
            text="Score",
            range_x=[0, 100],
            color="Score",
            color_continuous_scale=["#D6E4F6", "#7196C5", "#3266AD"],
        )
        fig2.add_vline(x=50, line_dash="dot", line_color="#73726C")
        fig2.update_traces(texttemplate="%{text:.1f}", textposition="outside")
        fig2.update_layout(
            coloraxis_showscale=False,
            margin=dict(l=20, r=45, t=10, b=25),
            yaxis=dict(categoryorder="array", categoryarray=profile_df["Factor"].tolist()[::-1]),
        )
        _style_figure(fig2, height=320, show_legend=False)
        st.plotly_chart(fig2, width="stretch")

    missing_inputs = row["Missing inputs"]
    if missing_inputs:
        friendly_reasons = row["Missing data details"]
        with st.container(border=True, key="company_evidence_gaps"):
            section_header(
                f"Missing or unverified information ({len(friendly_reasons)})",
                "The affected score is withheld rather than filled with an estimate.",
            )
            for reason in friendly_reasons:
                st.markdown(f"- {reason}")
    else:
        st.success("All information required for the selected score is available.")


# ----------------------------------------------------------------------
# VIEW 4: PAPER TRIAL
# ----------------------------------------------------------------------
elif view == VIEW_PAPER:
    _page_header(
        "Simulation-only portfolio",
        "Paper Trial",
        "Follow the governed proposal, holdings, modeled costs, and daily account value "
        "without connecting to a broker.",
        chips=[
            ("Simulation only", ""),
            ("No broker connection", ""),
        ],
    )
    st.caption(
        "Read-only dashboard. A local record is possible only through the separately "
        "confirmed CLI workflow after every prospective governance check passes."
    )
    try:
        monitor = load_paper_monitor()
    except Exception as e:
        st.error(
            "The local simulation file could not be verified. It was not used because its "
            "checksum, structure, or portfolio state may be invalid."
        )
        with st.expander("Technical error details"):
            st.code(str(e))
        st.stop()

    if not monitor["exists"]:
        st.info(
            "No local paper account exists yet. Create it once with `aios paper-init`; this "
            "starts with simulated cash and does not ask for broker details."
        )
        st.stop()

    summary = monitor["summary"]
    identity_labels = load_identity_labels(readiness["certified_research_through"])
    paper_model = build_paper_view_model(monitor)
    _render_paper_workflow(paper_model)

    if monitor.get("proposal_path") and monitor.get("account_path"):
        _render_paper_record_action(
            monitor,
            settings.project_root / monitor["account_path"],
            settings.project_root / monitor["proposal_path"],
        )

    st.subheader("Account Snapshot")
    _render_kpi_grid(
        [
            (
                "Simulated Account Value",
                f"${summary['equity']:,.2f}",
                "Cash plus checksum-verified local holdings.",
                "",
            ),
            (
                "Simulated Cash",
                f"${summary['cash']:,.2f}",
                "Unallocated cash in the local paper account.",
                "",
            ),
            (
                "Simulated Holdings",
                str(len(summary["holdings"])),
                "Positions recorded after prospective review.",
                "",
            ),
            (
                "Drawdown From Peak",
                f"{summary['drawdown']:.2%}",
                "Change from the account's highest recorded value.",
                "",
            ),
        ]
    )
    st.caption(
        f"Last reviewed market date: {summary['last_market_date'] or 'not invested yet'} • "
        f"Recorded rebalances: {summary['execution_count']} • "
        f"Modeled trading costs so far: ${summary['transaction_costs']:,.2f}"
    )

    forward = monitor["forward"]
    with st.container(border=True, key="paper_safeguards"):
        section_header(
            "Safeguards",
            "These conditions remain visible because they govern whether any local "
            "simulation can proceed.",
        )
        if forward is None:
            st.warning(
                "The untouched forward-test baseline has not been frozen. Recording a "
                "simulated fill stays blocked until a reviewed trial is frozen and the "
                "proposal is registered."
            )
            st.code("aios forward-status", language="bash")
        elif forward["ready"]:
            st.success(
                "The frozen factor, risk, cost, tax, calendar, and paper rules are unchanged. "
                f"{forward['registered_proposals']} proposal(s) are registered."
            )
        else:
            st.error(
                "Forward-policy drift detected. Do not count newer observations until the "
                "changes are reviewed and a new trial is deliberately started."
            )
            st.code(
                "aios forward-status\n"
                "# After a later reviewed decision close:\n"
                "aios forward-restart --confirm-restart",
                language="bash",
            )
            st.markdown("**Detected changes**")
            for issue in forward["issues"]:
                st.write(f"- {issue}")

    proposal = monitor["proposal"]
    st.subheader("Latest research portfolio proposal")
    if proposal is None:
        st.info(
            "No proposal has been created. After the data readiness checks pass, run "
            "`aios paper-propose` to create a reviewable simulation plan."
        )
    else:
        proposal_status = {
            "approved_for_supervised_simulation": "Passed data and risk checks",
            "blocked_readiness": "Blocked because required data is not ready",
            "blocked_risk": "Blocked by a portfolio-risk rule",
        }.get(proposal["status"], str(proposal["status"]).replace("_", " "))
        if proposal.get("already_simulated"):
            st.success(f"Recorded in the simulation on {proposal['scheduled_simulation_date']}.")
        elif proposal["status"] == "approved_for_supervised_simulation":
            timing = proposal.get("timing", {})
            if forward is None or not forward.get("ready"):
                st.error(
                    "This proposal cannot be recorded because the forward-policy evidence "
                    "is missing or drifted. Start a reviewed prospective trial first."
                )
            elif not proposal.get("registered_in_forward"):
                st.error(
                    "This proposal is not registered in the active forward trial and cannot "
                    "be recorded."
                )
            elif timing.get("status") in {"expired", "invalid"}:
                st.error(str(timing.get("detail", "The proposal timing check failed.")))
            elif timing.get("status") == "execution_window_open":
                st.warning(
                    f"{proposal_status}. {timing['detail']} This must happen before "
                    "the next U.S. session opens."
                )
            else:
                st.info(f"{proposal_status}. {timing.get('detail', '')}")
        else:
            st.error(proposal_status + ". Nothing can be recorded until the blocker is fixed.")
        st.caption(
            f"Research date: {display_date(proposal['decision_date'])} • "
            f"Complete Quality + Value scores: {proposal['factor_eligible_count']} • "
            f"Broad grouping used for concentration checks: "
            f"{proposal['sector_classification']}"
        )
        if proposal["targets"]:
            proposal_table = pd.DataFrame(
                [
                    {
                        "Research rank": row["factor_rank"],
                        "Company": identity_labels.get(row["ticker"]) or row["ticker"],
                        "Stock symbol": row["ticker"],
                        "Simulated target": f"{row['target_weight']:.1%}",
                        "Broad business group": row["sector"],
                        "Research score": round(row["qv_score"], 1),
                    }
                    for row in proposal["targets"]
                ]
            )
            st.dataframe(proposal_table, hide_index=True, width="stretch")
            st.caption(
                "This is a model portfolio for engineering and forward monitoring. It is "
                "not tailored to your finances and is not a personal action list."
            )

    stress_report: dict | None = None
    stress_error: str | None = None
    stress_availability = build_stress_review_view_model(None, monitor)
    if stress_availability.availability_reason == "report_absent":
        try:
            cache_inputs = {
                "account_path": monitor.get("account_path"),
                "account_payload_sha256": monitor.get("account_payload_sha256"),
                "proposal_path": monitor.get("proposal_path"),
                "proposal_payload_sha256": monitor.get("proposal_payload_sha256"),
                "trial_path": monitor.get("trial_path"),
                "trial_payload_sha256": monitor.get("trial_payload_sha256"),
            }
            if any(
                not isinstance(value, str) or not value
                for value in cache_inputs.values()
            ):
                raise ValueError("governed stress-review source identities are incomplete")
            scenario_bundle_sha256 = load_scenario_bundle().payload_sha256
            source_bundle_sha256 = build_stress_source_identity(
                settings.project_root
            )["source_bundle_sha256"]
            stress_report = load_proposal_stress_review(
                **cache_inputs,
                scenario_bundle_sha256=scenario_bundle_sha256,
                source_bundle_sha256=source_bundle_sha256,
            )
        except Exception as exc:
            stress_error = str(exc)
    stress_model = build_stress_review_view_model(
        stress_report,
        monitor,
        review_error=stress_error,
    )
    _render_stress_review(
        stress_model,
        report=stress_report,
        proposal_path=monitor.get("proposal_path"),
        review_error=stress_error,
    )

    if summary["holdings"]:
        st.subheader("Current simulated holdings")
        holdings_table = pd.DataFrame(
            [
                {
                    "Company": identity_labels.get(row["ticker"]) or row["ticker"],
                    "Stock symbol": row["ticker"],
                    "Share of simulated account": f"{row['weight']:.2%}",
                }
                for row in summary["holdings"]
            ]
        )
        st.dataframe(holdings_table, hide_index=True, width="stretch")
    else:
        st.info(
            "The account is still entirely simulated cash. An approved proposal is not "
            "counted as a holding until its scheduled close has been reviewed and recorded."
        )

    curve = pd.DataFrame(summary["curve"])
    if not curve.empty:
        st.subheader("Daily simulated account value")
        curve["date"] = pd.to_datetime(curve["date"])
        figure = px.line(
            curve,
            x="date",
            y="equity",
            labels={"date": "Market date", "equity": "Simulated account value ($)"},
        )
        figure.update_traces(line=dict(color="#3266AD", width=2.4))
        figure.update_layout(margin=dict(l=25, r=20, t=12, b=28))
        _style_figure(figure, height=370, show_legend=False)
        st.plotly_chart(figure, width="stretch")

    with st.container(border=True, key="paper_assumptions"):
        section_header(
            "Simulation assumptions",
            "Modeled costs and tax boundaries applied to the engineering account.",
        )
        costs = summary["transaction_cost_policy"]
        taxes = summary["tax_policy"]
        st.markdown(
            f"- Commission assumption: {costs['commission_bps']:.1f} basis points per trade\n"
            f"- Slippage assumption: {costs['slippage_bps']:.1f} basis points per trade\n"
            f"- Short-term, long-term, and dividend tax rates: "
            f"{taxes['short_term_rate']:.0%}, {taxes['long_term_rate']:.0%}, and "
            f"{taxes['dividend_rate']:.0%}\n"
            "- Tax rates are zero because jurisdiction and account type have not been set. "
            "Reported values are therefore pre-tax engineering results."
        )
    timing = proposal.get("timing", {}) if proposal is not None else {}
    if (
        proposal is not None
        and not proposal.get("already_simulated")
        and proposal.get("status") == "approved_for_supervised_simulation"
        and proposal.get("registered_in_forward")
        and forward is not None
        and forward.get("ready")
        and timing.get("status") == "execution_window_open"
    ):
        with st.expander("Technical operator command"):
            st.code(
                f"aios paper-review --proposal {monitor['proposal_path']}",
                language="bash",
            )
            st.caption(
                "This command does not change the account. Only if it reports ready should "
                "you run paper-execute with explicit simulated confirmation."
            )


# ----------------------------------------------------------------------
# VIEW 5: SYSTEM HEALTH
# ----------------------------------------------------------------------
elif view == VIEW_SYSTEM:
    _render_system_control(readiness)


# ----------------------------------------------------------------------
# VIEW 6: HOW IT WORKS
# ----------------------------------------------------------------------
elif view == VIEW_METHOD:
    _page_header(
        "How AIOS Works",
        "Methodology & Sources",
        "Understand the scoring model, point-in-time protections, source boundaries, and "
        "current operating limits.",
        chips=[("Plain-language guide", "info"), ("Research only", "warning")],
    )
    st.caption(RESEARCH_ONLY_NOTICE)
    method_body = st.container(key="reading_width")
    method_body.markdown("""
### What this page answers

The dashboard compares stocks using the same rules and shows which companies
deserve **more research**. It does not know your income, existing investments,
risk tolerance, taxes, or time horizon, so it cannot decide what you personally
should buy or sell.

### The four questions behind a score

1. **Business quality:** Is the company profitable, cash-generative, and
   financially healthy compared with other covered companies?
2. **Relative value:** Does the stock look inexpensive compared with earnings,
   cash flow, sales, and assets? Cheap can still mean risky.
3. **Price trend:** Has the price been relatively strong over roughly the last
   year, excluding the most recent month?
4. **Price stability:** Has the price moved more steadily than other stocks?

Each score runs from 0 to 100 and is **relative to other stocks with usable
data**. A score of 80 does not mean an 80% chance of profit or an 80% expected
return.

### The two scoring methods

- **Quality + Value** is the baseline and the default view.
- **Quality + Value + Trend + Stability** is experimental. It completed the
  engineering test but performed worse than the simpler method in the short
  U.S. test window, so it is not treated as an improvement.

### Information is never backdated

When you choose a research date, the system uses only company filings, economic
updates, index announcements, and prices that were knowable by then. If evidence
is missing or its identity cannot be verified, the score is hidden instead of
being guessed.

### Current coverage

The current dashboard uses the audited U.S. reference dataset. India is the
intended primary market, but NSE/BSE instruments, filings, corporate actions,
historical index membership, taxes, and broker workflows are not loaded yet.

### Buy/sell readiness

**Ready for supervised U.S. research and local paper simulation; not ready for
personal buy/sell instructions or real-money orders.** Current U.S.
membership, identity, company filings, prices, corporate actions, macro data,
and portfolio-risk gates now support the paper monitor. The six-period stateful
U.S. engineering test is complete, but it is short and does not prove that the
method can beat the market. Real-money use still requires an untouched forward
test, clean scheduled refresh and backup observations, current-news and event
review, and your account, tax, broker, and final risk rules. India remains the
next market-integration phase after those U.S. operating gates are closed.

### Evidence sources

- **Company identity and filings:** SEC EDGAR submissions and reviewed filing
  facts with publication dates.
- **Market prices and corporate actions:** locally stored, reviewed histories
  from the configured yfinance, Tiingo, or Stooq source path.
- **Economic context:** FRED observations with release or vintage dates.
- **Membership and provenance:** dated local universe records and source
  attestations; provider freshness alone never changes the certified close.
- **Storage and audit:** local DuckDB tables plus checksum-protected paper and
  forward-trial documents.
""")

    with method_body.expander("Technical methodology and audit details"):
        st.markdown("""
**Point-in-time rules.** Fundamentals use SEC filing dates; macro data uses
public release/vintage dates; historical membership stores separate public
dates for interval starts and ends. A backtest target must be known at decision
close and effective on execution day.

**Factor publication.** Quality + Value requires at least two supported quality
measures and two supported valuation multiples. The four-factor method also
requires 253 verified price observations for 12-minus-1 Momentum and one-year
Low Volatility. Missing factors are not silently reweighted.

**Trailing twelve months.** Flow metrics use:

`TTM = latest annual + post-annual quarters − matching prior-year quarters`

**Economic weighting.** The baseline Quality/Value mix is 60/40. Reviewed
release-dated evidence changes it to 45/55 in reflation, 65/35 in stagflation,
55/45 in deflationary conditions, or 70/30 in stressed markets. Incomplete
evidence falls back to 60/40 and is marked.

**Four-factor mix.** The experimental score keeps the Quality/Value relative
tilt inside a 60% core, then adds 25% Momentum and 15% Low Volatility.

**Engineering audit.** Historical tests use persistent holdings, daily account
values, trading costs, dividends, stock splits, reviewed security conversions,
and SPY as a comparison. A result is accepted only when every scheduled period
has complete reviewed evidence. Incomplete runs remain diagnostics and are not
shown here as investment-performance claims. Taxes stay at zero until a real
jurisdiction and account type are configured.

All calculations and validation gates are deterministic Python and DuckDB.
Language models are optional explanation tools and never provide numeric or
provenance evidence.
""")

    method_body.info(
        "For the complete audit trail, read ARCHITECTURE.md and "
        "SP500_DATA_PROVENANCE.md in the project folder."
    )

"""AI Investment OS — Dashboard (Streamlit).

Six decision-oriented views:
  1. OVERVIEW          — operating status, evidence clocks, and next workflow
  2. RESEARCH EXPLORER — baseline or experimental comparison + chart
  3. COMPANY LENS      — one-stock score and evidence review
  4. PORTFOLIO MONITOR — supervised local simulation state and next action
  5. SYSTEM CONTROL    — scheduler, ingests, backups, and policy evidence
  6. METHODOLOGY       — non-technical explanation + optional audit details

Run:  .venv/bin/aios dashboard
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from html import escape
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import streamlit as st

# Ensure src/ on path when run via `streamlit run`
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aios.alerts import get_alert_store  # noqa: E402
from aios.config import settings  # noqa: E402
from aios.daily import DAILY_JOB_NAME  # noqa: E402
from aios.dashboard_copy import (  # noqa: E402
    MODEL_OPTIONS,
    MODEL_QV_LABEL,
    RESEARCH_ONLY_NOTICE,
    VIEW_DETAILS,
    VIEW_HOME,
    VIEW_METHOD,
    VIEW_OPTIONS,
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
    us_eod_freshness_message,
)
from aios.factors.composite import compute_composite  # noqa: E402
from aios.forward import (  # noqa: E402
    DEFAULT_FORWARD_RELATIVE_PATH,
    assess_forward_trial,
)
from aios.operations import verify_local_backup  # noqa: E402
from aios.paper import (  # noqa: E402
    ACCOUNT_DOCUMENT_KIND,
    DEFAULT_ACCOUNT_RELATIVE_PATH,
    PROPOSAL_DOCUMENT_KIND,
    latest_paper_decision_date,
    paper_account_summary,
    read_paper_document,
)
from aios.readiness import assess_us_readiness  # noqa: E402
from aios.scheduler import (  # noqa: E402
    TIMER_NAMES,
    user_linger_status,
    user_scheduler_status,
)
from aios.storage.store import close_global_store, store_scope  # noqa: E402

st.set_page_config(
    page_title="AI Investment OS",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _apply_visual_system() -> None:
    """Apply a compact institutional shell without changing data semantics."""
    st.markdown(
        """
        <style>
        html { color-scheme: dark; }
        :root {
            --aios-bg: #07111F;
            --aios-sidebar: #091626;
            --aios-surface: #0E1C2D;
            --aios-surface-2: #12243A;
            --aios-surface-3: #172C45;
            --aios-line: #20364F;
            --aios-line-strong: #2B4766;
            --aios-text: #E6EDF7;
            --aios-muted: #91A4BA;
            --aios-subtle: #6F849B;
            --aios-teal: #2DD4BF;
            --aios-green: #34D399;
            --aios-amber: #FBBF24;
            --aios-red: #FB7185;
            --aios-blue: #60A5FA;
        }
        [data-testid="stAppViewContainer"] {
            background:
                radial-gradient(circle at 72% -10%, rgba(45, 212, 191, 0.08), transparent 28rem),
                var(--aios-bg);
            color: var(--aios-text);
        }
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #0A1829 0%, var(--aios-sidebar) 100%);
            border-right: 1px solid var(--aios-line);
        }
        [data-testid="stSidebarContent"] { padding-top: 0.55rem; }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
            color: var(--aios-muted);
        }
        header[data-testid="stHeader"] {
            background: transparent;
            height: 2.25rem;
        }
        .block-container {
            max-width: 1540px;
            padding-top: 1.25rem;
            padding-bottom: 3rem;
            padding-left: 1.65rem;
            padding-right: 1.65rem;
        }
        h1, h2, h3 {
            color: var(--aios-text);
            letter-spacing: -0.025em;
            text-wrap: balance;
        }
        h1 { font-size: 1.7rem !important; font-weight: 700 !important; }
        h2 { font-size: 1.08rem !important; font-weight: 680 !important; }
        h3 { font-size: 0.94rem !important; font-weight: 670 !important; }
        p, li, label { color: var(--aios-muted); }
        hr { border-color: var(--aios-line) !important; }
        [data-testid="stMetric"] {
            background: var(--aios-surface);
            border: 1px solid var(--aios-line);
            border-radius: 10px;
            padding: 0.78rem 0.9rem;
            min-height: 92px;
            box-shadow: 0 10px 28px rgba(0, 0, 0, 0.12);
        }
        [data-testid="stMetricLabel"] {
            color: var(--aios-muted);
            font-size: 0.72rem;
            font-weight: 600;
            letter-spacing: 0.02em;
        }
        [data-testid="stMetricValue"] {
            color: var(--aios-text);
            font-size: 1.55rem;
            font-variant-numeric: tabular-nums;
        }
        [data-testid="stAlert"] {
            border-radius: 9px;
            border-width: 1px;
            background: var(--aios-surface) !important;
        }
        [data-testid="stDataFrame"] {
            border: 1px solid var(--aios-line);
            border-radius: 9px;
            overflow: hidden;
        }
        [data-testid="stPlotlyChart"] {
            background: var(--aios-surface);
            border: 1px solid var(--aios-line);
            border-radius: 10px;
            padding: 0.2rem;
        }
        [data-testid="stAppDeployButton"] { display: none; }
        [data-testid="stSidebar"] div[role="radiogroup"] {
            gap: 0.18rem;
        }
        [data-testid="stSidebar"] div[role="radiogroup"] label {
            border-radius: 7px;
            min-height: 40px;
            padding: 0.55rem 0.62rem;
            border: 1px solid transparent;
            cursor: pointer;
            touch-action: manipulation;
        }
        [data-testid="stSidebar"] div[role="radiogroup"] label > div:first-child {
            display: none;
        }
        [data-testid="stSidebar"] div[role="radiogroup"] label p::before {
            display: inline-block;
            width: 1.35rem;
            color: #6F849B;
            font-weight: 700;
        }
        [data-testid=stSidebar] [role=radiogroup] label:nth-child(1) p::before { content: "◫"; }
        [data-testid=stSidebar] [role=radiogroup] label:nth-child(2) p::before { content: "⌕"; }
        [data-testid=stSidebar] [role=radiogroup] label:nth-child(3) p::before { content: "◎"; }
        [data-testid=stSidebar] [role=radiogroup] label:nth-child(4) p::before { content: "◇"; }
        [data-testid=stSidebar] [role=radiogroup] label:nth-child(5) p::before { content: "⚙"; }
        [data-testid=stSidebar] [role=radiogroup] label:nth-child(6) p::before { content: "≡"; }
        [data-testid="stSidebar"] div[role="radiogroup"] label:hover {
            background: rgba(45, 212, 191, 0.07);
            border-color: rgba(45, 212, 191, 0.18);
        }
        [data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) {
            background: linear-gradient(90deg, rgba(45, 212, 191, 0.16), rgba(45, 212, 191, 0.05));
            border-color: rgba(45, 212, 191, 0.3);
            box-shadow: inset 3px 0 0 var(--aios-teal);
        }
        [data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) p {
            color: #D8FFFA !important;
            font-weight: 650;
        }
        button:focus-visible,
        [data-testid="stSidebar"] label:has(input:focus-visible) {
            outline: 2px solid var(--aios-teal) !important;
            outline-offset: 2px;
        }
        [data-testid="stExpander"] {
            background: rgba(14, 28, 45, 0.72);
            border-color: var(--aios-line) !important;
            border-radius: 9px !important;
        }
        [data-testid="stVerticalBlockBorderWrapper"] {
            border-color: var(--aios-line) !important;
            border-radius: 10px !important;
            background: linear-gradient(180deg, rgba(18, 36, 58, 0.68), rgba(14, 28, 45, 0.82));
        }
        .aios-eyebrow {
            color: var(--aios-teal);
            font-size: 0.68rem;
            font-weight: 700;
            letter-spacing: 0.11em;
            text-transform: uppercase;
            margin-bottom: 0.12rem;
            line-height: 1.5;
        }
        .aios-header-note {
            color: var(--aios-muted);
            font-size: 0.82rem;
            margin-top: -0.45rem;
            margin-bottom: 0.7rem;
        }
        .aios-section-note {
            color: var(--aios-muted);
            font-size: 0.78rem;
            margin-top: -0.35rem;
            margin-bottom: 0.62rem;
        }
        .aios-brand {
            display: flex;
            align-items: center;
            gap: 0.72rem;
            padding: 0.45rem 0 0.7rem;
        }
        .aios-brand-mark {
            width: 36px;
            height: 36px;
            display: grid;
            place-items: center;
            border-radius: 10px;
            color: #06211F;
            background: linear-gradient(135deg, #5EEAD4, #2DD4BF);
            font-size: 1.05rem;
            font-weight: 900;
            box-shadow: 0 8px 22px rgba(45, 212, 191, 0.2);
        }
        .aios-brand-name { color: var(--aios-text); font-size: 1.05rem; font-weight: 760; }
        .aios-brand-sub { color: var(--aios-subtle); font-size: 0.68rem; margin-top: 0.04rem; }
        .aios-market-tag {
            display: inline-flex;
            align-items: center;
            gap: 0.42rem;
            color: #A8C0D8;
            font-size: 0.72rem;
            font-weight: 650;
            margin: 0.1rem 0 0.55rem;
        }
        .aios-market-tag::before {
            content: "";
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: var(--aios-green);
            box-shadow: 0 0 0 4px rgba(52, 211, 153, 0.1);
        }
        .aios-title-row {
            display: flex;
            align-items: flex-end;
            justify-content: space-between;
            gap: 1rem;
        }
        .aios-title-row h1 { margin: 0.15rem 0 0.22rem; }
        .aios-chip-row { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 0.42rem; }
        .aios-chip {
            display: inline-flex;
            align-items: center;
            min-height: 30px;
            padding: 0.28rem 0.62rem;
            border: 1px solid var(--aios-line);
            border-radius: 7px;
            background: rgba(18, 36, 58, 0.86);
            color: #BFD0E2;
            font-size: 0.7rem;
            font-weight: 650;
            white-space: nowrap;
        }
        .aios-chip.success { color: #78F4D7; border-color: rgba(45, 212, 191, 0.28); }
        .aios-chip.warning { color: #FDE68A; border-color: rgba(251, 191, 36, 0.28); }
        .aios-chip.danger { color: #FDB3C0; border-color: rgba(251, 113, 133, 0.3); }
        .aios-status-strip {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            margin: 0.5rem 0 0.72rem;
            padding: 0.58rem 0.78rem;
            border: 1px solid rgba(45, 212, 191, 0.24);
            border-radius: 8px;
            background: rgba(45, 212, 191, 0.07);
            color: #A7F3E4;
            font-size: 0.76rem;
        }
        .aios-status-strip strong { color: #58E8CC; }
        .aios-status-strip.danger {
            border-color: rgba(251, 113, 133, 0.3);
            background: rgba(251, 113, 133, 0.08);
            color: #FDC5CF;
        }
        .aios-status-strip.danger strong { color: #FB9AAB; }
        .aios-kpi-grid {
            display: grid;
            grid-template-columns: repeat(6, minmax(0, 1fr));
            gap: 0.58rem;
            margin-bottom: 0.9rem;
        }
        .aios-kpi-card {
            min-width: 0;
            padding: 0.72rem 0.78rem 0.68rem;
            border: 1px solid var(--aios-line);
            border-radius: 9px;
            background: linear-gradient(145deg, rgba(20, 41, 65, 0.96), rgba(13, 28, 45, 0.98));
            box-shadow: 0 12px 30px rgba(0, 0, 0, 0.12);
        }
        .aios-kpi-label {
            color: #91A4BA;
            font-size: 0.67rem;
            font-weight: 650;
            letter-spacing: 0.015em;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .aios-kpi-value {
            color: var(--aios-text);
            font-size: 1.32rem;
            font-weight: 720;
            line-height: 1.15;
            margin: 0.32rem 0 0.27rem;
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
        }
        .aios-kpi-value.success { color: var(--aios-green); }
        .aios-kpi-value.warning { color: var(--aios-amber); }
        .aios-kpi-value.danger { color: var(--aios-red); }
        .aios-kpi-detail {
            color: var(--aios-subtle);
            font-size: 0.64rem;
            line-height: 1.35;
            min-height: 1.72rem;
        }
        .aios-panel-title {
            color: var(--aios-text);
            font-size: 0.88rem !important;
            font-weight: 680 !important;
            margin: 0 !important;
            letter-spacing: -0.01em;
        }
        .aios-panel-kicker { color: var(--aios-subtle); font-size: 0.68rem; margin-top: 0.08rem; }
        .aios-account-value {
            color: var(--aios-text);
            font-size: 1.78rem;
            line-height: 1.1;
            font-weight: 720;
            font-variant-numeric: tabular-nums;
            margin: 0.4rem 0 0.15rem;
        }
        .aios-account-label { color: var(--aios-subtle); font-size: 0.68rem; }
        .aios-divider { height: 1px; background: var(--aios-line); margin: 0.72rem 0; }
        .aios-next-action {
            margin-top: 0.72rem;
            padding: 0.62rem 0.68rem;
            border-radius: 7px;
            border: 1px solid rgba(96, 165, 250, 0.22);
            background: rgba(96, 165, 250, 0.07);
            color: #B9D7FA;
            font-size: 0.7rem;
            line-height: 1.45;
        }
        .aios-next-action strong { color: #D9EAFE; }
        .aios-key-row {
            display: flex;
            justify-content: space-between;
            gap: 0.75rem;
            padding: 0.29rem 0;
            color: var(--aios-muted);
            font-size: 0.7rem;
        }
        .aios-key-row strong { color: var(--aios-text); font-variant-numeric: tabular-nums; }
        .aios-source-list { display: grid; gap: 0.42rem; }
        .aios-source-item {
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto;
            align-items: center;
            gap: 0.75rem;
            padding-bottom: 0.42rem;
            border-bottom: 1px solid rgba(32, 54, 79, 0.72);
            color: var(--aios-muted);
            font-size: 0.7rem;
        }
        .aios-source-item:last-child { border-bottom: 0; padding-bottom: 0; }
        .aios-source-item strong { color: #C6D5E5; font-variant-numeric: tabular-nums; }
        .aios-footnote { color: var(--aios-subtle); font-size: 0.64rem; line-height: 1.45; }
        [data-testid="stTabs"] button { min-height: 42px; }
        [data-testid="stTabs"] button[aria-selected="true"] { color: var(--aios-teal); }
        [data-testid="stSelectbox"] > div > div,
        [data-testid="stDateInput"] > div > div { background: var(--aios-surface); }
        code {
            overflow-wrap: anywhere;
            color: #9FE8DB;
            background: #0A1726;
        }
        @media (max-width: 1280px) {
            .aios-kpi-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
            [data-testid="stHorizontalBlock"] { flex-wrap: wrap; }
            [data-testid="stColumn"] {
                flex: 1 1 320px !important;
                width: 100% !important;
                min-width: 0 !important;
            }
        }
        @media (max-width: 900px) {
            .block-container { padding: 1rem 0.9rem 2.5rem; }
            h1 { font-size: 1.45rem !important; }
            .aios-title-row { align-items: flex-start; flex-direction: column; }
            .aios-chip-row { justify-content: flex-start; }
            .aios-header-note { margin-top: 0.3rem; }
            .aios-status-strip { align-items: flex-start; flex-direction: column; gap: 0.3rem; }
            .aios-kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            [data-testid="stMetric"] { min-height: 84px; }
        }
        @media (max-width: 520px) {
            .aios-kpi-grid { grid-template-columns: 1fr; }
        }
        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after {
                scroll-behavior: auto !important;
                animation-duration: 0.01ms !important;
                animation-iteration-count: 1 !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


_apply_visual_system()
# Older dashboard versions held one process-wide writable DuckDB connection.
# Release it once after a hot reload; every loader below now uses a bounded,
# read-only scope so scheduled writers can run while the dashboard stays open.
close_global_store()


# ----------------------------------------------------------------------
# Cached data loaders
# ----------------------------------------------------------------------
@st.cache_data(ttl=300)
def load_composite(as_of: str) -> pd.DataFrame:
    with store_scope(read_only=True) as store:
        tickers = [row["ticker"] for row in store.universe_membership_on("sp500", as_of)]
        rows = compute_composite(tickers, as_of, store, include_market_factors=True)
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
    """Load checksum-validated local simulation state and its newest proposal."""
    account_path = settings.project_root / DEFAULT_ACCOUNT_RELATIVE_PATH
    if not account_path.exists():
        return {"exists": False, "account_path": str(account_path), "proposal": None}
    with store_scope(read_only=True) as store:
        summary = paper_account_summary(account_path, store)
    proposals_dir = settings.project_root / "data" / "paper" / "proposals"
    proposal_paths = sorted(proposals_dir.glob("us-qv-*.json"), reverse=True)
    proposal = None
    proposal_path = None
    if proposal_paths:
        proposal_document = read_paper_document(
            proposal_paths[0], expected_kind=PROPOSAL_DOCUMENT_KIND
        )
        proposal = proposal_document.payload
        proposal_path = str(proposal_document.path)
        account_document = read_paper_document(account_path, expected_kind=ACCOUNT_DOCUMENT_KIND)
        executed_ids = {row.get("proposal_id") for row in account_document.payload["executions"]}
        proposal["already_simulated"] = proposal.get("proposal_id") in executed_ids
    forward = None
    forward_path = settings.project_root / DEFAULT_FORWARD_RELATIVE_PATH
    if forward_path.exists():
        try:
            status = assess_forward_trial(settings.project_root, forward_path, account_path)
            forward = {
                "ready": status.ready,
                "trial_id": status.trial_id,
                "registered_proposals": status.registered_proposals,
                "issues": list(status.issues),
            }
        except (OSError, ValueError) as exc:
            forward = {
                "ready": False,
                "trial_id": "unavailable",
                "registered_proposals": 0,
                "issues": [str(exc)],
            }
    return {
        "exists": True,
        "account_path": str(account_path),
        "summary": summary,
        "proposal": proposal,
        "proposal_path": proposal_path,
        "forward": forward,
    }


@st.cache_data(ttl=300)
def load_identity_labels(as_of: str) -> dict[str, str | None]:
    """Load reviewed issuer names for display; symbols remain the security key."""
    with store_scope(read_only=True) as store:
        return {
            str(row["ticker"]): row.get("canonical_name")
            for row in store.universe_identity_labels("sp500", as_of)
        }


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
    except (OSError, RuntimeError, ValueError) as exc:
        state["scheduler"] = {}
        state["scheduler_error"] = str(exc)
    state["linger_enabled"] = user_linger_status()

    state["backup"] = load_latest_backup()
    try:
        incident_store = get_alert_store()
        incidents = incident_store.list(limit=100)
        state["incidents"] = [incident.__dict__ for incident in incidents]
        daily_cycle = incident_store.latest_job(DAILY_JOB_NAME)
        state["daily_cycle"] = daily_cycle.__dict__ if daily_cycle is not None else None
        state["incident_error"] = None
    except (OSError, RuntimeError, ValueError) as exc:
        state["incidents"] = []
        state["daily_cycle"] = None
        state["incident_error"] = str(exc)
    return state


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
    chip_html = "".join(
        f'<span class="aios-chip {escape(tone)}">{escape(label)}</span>'
        for label, tone in (chips or [])
    )
    st.markdown(
        f"""
        <div class="aios-title-row">
          <div>
            <div class="aios-eyebrow">{escape(eyebrow)}</div>
            <h1>{escape(title)}</h1>
          </div>
          <div class="aios-chip-row">{chip_html}</div>
        </div>
        <div class="aios-header-note">{escape(note)}</div>
        """,
        unsafe_allow_html=True,
    )


def _kpi_card(label: str, value: str, detail: str, tone: str = "") -> str:
    return (
        '<div class="aios-kpi-card">'
        f'<div class="aios-kpi-label">{escape(label)}</div>'
        f'<div class="aios-kpi-value {escape(tone)}">{escape(value)}</div>'
        f'<div class="aios-kpi-detail">{escape(detail)}</div>'
        "</div>"
    )


def _render_kpi_grid(cards: list[tuple[str, str, str, str]]) -> None:
    st.markdown(
        '<div class="aios-kpi-grid">'
        + "".join(_kpi_card(label, value, detail, tone) for label, value, detail, tone in cards)
        + "</div>",
        unsafe_allow_html=True,
    )


def _source_list(rows: list[tuple[str, str]]) -> str:
    items = "".join(
        '<div class="aios-source-item">'
        f"<span>{escape(label)}</span><strong>{escape(value)}</strong>"
        "</div>"
        for label, value in rows
    )
    return f'<div class="aios-source-list">{items}</div>'


def _key_row(label: str, value: object) -> str:
    return (
        '<div class="aios-key-row">'
        f"<span>{escape(label)}</span><strong>{escape(str(value))}</strong>"
        "</div>"
    )


def _style_figure(figure, *, height: int, show_legend: bool = True) -> None:
    """Apply one accessible dark chart treatment across the dashboard."""
    figure.update_layout(
        height=height,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#0E1C2D",
        font=dict(color="#B9C8D8", size=11),
        hoverlabel=dict(bgcolor="#172C45", font_color="#F2F7FC", bordercolor="#2B4766"),
        legend=dict(
            bgcolor="rgba(0,0,0,0)",
            font=dict(color="#AFC0D2"),
            title_font=dict(color="#91A4BA"),
        ),
        showlegend=show_legend,
    )
    figure.update_xaxes(gridcolor="#20364F", zerolinecolor="#2B4766", linecolor="#2B4766")
    figure.update_yaxes(gridcolor="#20364F", zerolinecolor="#2B4766", linecolor="#2B4766")


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
        color_discrete_map={"Pass": "#2DD4BF", "Warn": "#FBBF24", "Fail": "#FB7185"},
        hover_data={"Coverage %": ":.1f", "Observed": True, "Status": True},
    )
    figure.add_vline(x=95, line_dash="dot", line_color="#6F849B")
    figure.update_traces(textposition="inside")
    figure.update_layout(
        xaxis=dict(range=[0, 101], title="Reviewed coverage (%)"),
        yaxis=dict(title=None, autorange="reversed"),
        legend_title_text="Gate",
        margin=dict(l=15, r=15, t=15, b=30),
    )
    _style_figure(figure, height=292)
    st.plotly_chart(figure, width="stretch")


def _render_overview(report: dict) -> None:
    """Render the default operating cockpit from governed readiness evidence."""
    checks = _checks_by_key(report)
    decision = checks.get("decision_date", {})
    universe = checks.get("universe_membership", {})
    fundamentals = checks.get("fundamental_coverage", {})
    prices = checks.get("reviewed_price_freshness", {})
    macro = checks.get("macro_pit_readiness", {})
    integrity = checks.get("data_integrity", {})
    filing_coverage = str(fundamentals.get("observed", "Not available")).split(" (")[0]
    price_coverage = str(prices.get("observed", "Not available")).split(" (")[0]
    reviewed_date = display_date(report["certified_research_through"])
    macro_label = str(macro.get("observed") or "Unknown").replace("_", " ").title()
    _, freshness_detail = us_eod_freshness_message(
        report["raw_prices_through"]
    )
    certification_current, certification_detail = us_certification_freshness_message(
        report["certified_research_through"]
    )

    _page_header(
        "U.S. reference market",
        "Executive Research Dashboard",
        "Decision readiness, proposal state, source evidence, and operating controls in one view.",
        chips=[
            (
                f"Economic regime: {macro_label}",
                "success" if macro.get("status") == "pass" else "warning",
            ),
            (f"Certified decision close: {reviewed_date}", ""),
            (
                "Latest U.S. session certified"
                if certification_current
                else "Daily certification pending",
                "success" if certification_current else "warning",
            ),
            ("Simulation only", "warning"),
        ],
    )

    if report["ready"]:
        status_title = "Research gates passed"
        status_detail = (
            "Supervised research and local paper monitoring are available for the certified date."
        )
        status_class = ""
    else:
        failed = [row for row in report["checks"] if row["status"] == "fail"]
        if any(row["check"] == "universe_membership" for row in failed):
            dependent = {
                "stable_security_identity",
                "fundamental_coverage",
                "price_history_coverage",
                "reviewed_price_freshness",
            }
            failed = [row for row in failed if row["check"] not in dependent]
        blockers = [row["label"] for row in failed]
        status_title = "New paper decisions blocked"
        status_detail = (
            ", ".join(blockers)
            + ". Historical research remains available inside the reviewed window."
        )
        status_class = "danger"

    st.markdown(
        f'<div class="aios-status-strip {status_class}">'
        f'<span><strong>{escape(status_title)}</strong> — {escape(status_detail)}</span>'
        f'<span>{escape(str(integrity.get("observed", "Integrity unavailable")))}</span>'
        "</div>",
        unsafe_allow_html=True,
    )
    if certification_current:
        st.success(certification_detail)
    else:
        st.warning(certification_detail)

    _render_kpi_grid(
        [
            (
                "Universe Coverage",
                str(universe.get("observed", "Not available")),
                "Point-in-time S&P 500 members",
                "",
            ),
            (
                "Company Filings (PIT)",
                filing_coverage,
                "Filings public by the decision close",
                "",
            ),
            (
                "Current Prices",
                price_coverage,
                "Action-safe member prices",
                "",
            ),
            (
                "Latest Filing Evidence",
                display_date(report["fundamentals_through"]),
                "Raw SEC evidence through",
                "",
            ),
            (
                "Latest Macro Release",
                display_date(report["macro_releases_through"]),
                "Required release-dated inputs",
                "",
            ),
            (
                "Research Readiness",
                "READY" if report["ready"] else "BLOCKED",
                "Paper research gate",
                "success" if report["ready"] else "danger",
            ),
        ]
    )

    try:
        monitor = load_paper_monitor()
    except Exception as exc:
        st.error(
            "The local simulation state could not be verified. No portfolio information was "
            "used; inspect the technical detail before continuing."
        )
        with st.expander("Technical detail"):
            st.code(str(exc))
        monitor = {"exists": False, "proposal": None}

    identity_labels = load_identity_labels(report["certified_research_through"])
    proposal = monitor.get("proposal")
    allocation_col, proposal_col, account_col = st.columns([0.82, 1.28, 0.72], gap="medium")

    with allocation_col, st.container(border=True):
        st.markdown(
            '<h2 class="aios-panel-title">Proposed Research Basket</h2>'
            '<div class="aios-panel-kicker">Target mix by broad business group</div>',
            unsafe_allow_html=True,
        )
        targets = proposal.get("targets", []) if proposal else []
        if targets:
            allocation = pd.DataFrame(
                [
                    {"Business group": row["sector"], "Target weight": row["target_weight"]}
                    for row in targets
                ]
            )
            allocation = allocation.groupby("Business group", as_index=False)[
                "Target weight"
            ].sum()
            allocation["Business group"] = allocation["Business group"].replace(
                {
                    "Finance, insurance and real estate": "Finance & real estate",
                    "Transport, communications and utilities": "Transport & utilities",
                    "Wholesale trade": "Wholesale",
                    "Retail trade": "Retail",
                }
            )
            allocation_figure = px.pie(
                allocation,
                names="Business group",
                values="Target weight",
                hole=0.68,
                color_discrete_sequence=[
                    "#2DD4BF",
                    "#60A5FA",
                    "#A78BFA",
                    "#F59E0B",
                    "#34D399",
                    "#F472B6",
                ],
            )
            allocation_figure.update_traces(
                textinfo="percent",
                textfont_size=10,
                marker=dict(line=dict(color="#0E1C2D", width=2)),
                hovertemplate="%{label}<br>Target: %{percent}<extra></extra>",
            )
            allocation_figure.add_annotation(
                text=f"{len(targets)}<br>targets",
                x=0.5,
                y=0.5,
                showarrow=False,
                font=dict(color="#E6EDF7", size=14),
            )
            allocation_figure.update_layout(
                margin=dict(l=4, r=4, t=4, b=4),
                legend=dict(orientation="h", y=-0.06, x=0.5, xanchor="center"),
            )
            _style_figure(allocation_figure, height=270)
            st.plotly_chart(allocation_figure, width="stretch")
            st.markdown(
                '<div class="aios-footnote">Proposal composition only—not current holdings '
                "or a personal allocation.</div>",
                unsafe_allow_html=True,
            )
        else:
            st.info("No current proposal is available to chart.")

    with proposal_col, st.container(border=True):
        st.markdown(
            '<h2 class="aios-panel-title">Current Research Proposal</h2>'
            '<div class="aios-panel-kicker">Reviewed targets waiting for the '
            "supervised workflow</div>",
            unsafe_allow_html=True,
        )
        if proposal is None:
            st.info("No proposal exists yet. Create one only after the operating gates pass.")
        else:
            already_simulated = bool(proposal.get("already_simulated"))
            if already_simulated:
                status_text = "Recorded in the paper simulation"
            elif proposal["status"] == "approved_for_supervised_simulation":
                status_text = "Approved; waiting for reviewed execution-date prices"
            else:
                status_text = str(proposal["status"]).replace("_", " ").title()
            st.markdown(
                f'<div class="aios-status-strip"><span><strong>{escape(status_text)}</strong>'
                f'</span><span>{escape(display_date(proposal["decision_date"]))} → '
                f'{escape(display_date(proposal["scheduled_simulation_date"]))}</span></div>',
                unsafe_allow_html=True,
            )
            if proposal.get("targets"):
                targets = pd.DataFrame(
                    [
                        {
                            "Rank": row["factor_rank"],
                            "Company": company_symbol_label(
                                identity_labels.get(row["ticker"]), row["ticker"]
                            ),
                            "Target": f"{row['target_weight']:.1%}",
                            "Score": round(row["qv_score"], 1),
                        }
                        for row in proposal["targets"]
                    ]
                )
                st.dataframe(targets, hide_index=True, width="stretch", height=280)
            if not already_simulated:
                st.markdown(
                    '<div class="aios-footnote">Next control: verify the scheduled close, '
                    "then explicitly record or reject the local simulation. No broker order "
                    "can be created here.</div>",
                    unsafe_allow_html=True,
                )

    with account_col, st.container(border=True):
        st.markdown(
            '<h2 class="aios-panel-title">Paper Account & Policy</h2>'
            '<div class="aios-panel-kicker">Checksum-protected local simulation</div>',
            unsafe_allow_html=True,
        )
        if not monitor.get("exists"):
            st.info("No verified local paper account is available.")
        else:
            summary = monitor["summary"]
            forward = monitor.get("forward")
            account_html = (
                '<div class="aios-account-label">Simulated account value</div>'
                f'<div class="aios-account-value">${summary["equity"]:,.2f}</div>'
                + _key_row("Cash", f'${summary["cash"]:,.2f}')
                + _key_row("Holdings", len(summary["holdings"]))
                + _key_row("Drawdown", f'{summary["drawdown"]:.2%}')
                + _key_row("Recorded rebalances", summary["execution_count"])
                + '<div class="aios-divider"></div>'
            )
            if forward and forward["ready"]:
                policy_text = "Forward policy unchanged"
                policy_tone = "success"
            elif forward:
                policy_text = "Policy drift requires review"
                policy_tone = "danger"
            else:
                policy_text = "Forward policy unavailable"
                policy_tone = "warning"
            account_html += (
                f'<span class="aios-chip {policy_tone}">{escape(policy_text)}</span>'
                '<div class="aios-next-action"><strong>Current position:</strong> Entirely '
                "simulated cash until the scheduled close is reviewed and explicitly recorded."
                "</div>"
            )
            st.markdown(account_html, unsafe_allow_html=True)

    coverage_col, evidence_col = st.columns([1.34, 0.66], gap="medium")
    with coverage_col, st.container(border=True):
        st.markdown(
            '<h2 class="aios-panel-title">Coverage by Operating Gate</h2>'
            '<div class="aios-panel-kicker">Exact reviewed counts against '
            "fail-closed requirements</div>",
            unsafe_allow_html=True,
        )
        _render_coverage_chart(report)

    with evidence_col, st.container(border=True):
        st.markdown(
            '<h2 class="aios-panel-title">Evidence Clock</h2>'
            '<div class="aios-panel-kicker">Raw freshness never overrides certification</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            _source_list(
                [
                    ("Certified decision", reviewed_date),
                    ("Market prices", display_date(report["raw_prices_through"])),
                    ("Company filings", display_date(report["fundamentals_through"])),
                    ("Economic releases", display_date(report["macro_releases_through"])),
                ]
            ),
            unsafe_allow_html=True,
        )
        st.caption(freshness_detail)
        st.markdown('<div class="aios-divider"></div>', unsafe_allow_html=True)
        st.markdown(
            _key_row("Decision evidence", decision.get("observed", "Unavailable"))
            + _key_row("Database integrity", integrity.get("observed", "Unavailable"))
            + '<div class="aios-next-action"><strong>Boundary:</strong> Research and local '
            "simulation only. No broker connection or personal recommendation.</div>",
            unsafe_allow_html=True,
        )

    with st.expander("All operating-gate details"):
        gate_table = pd.DataFrame(
            [
                {
                    "Control": row["label"],
                    "Status": row["status"].title(),
                    "Observed": row["observed"],
                    "Required": row["required"],
                }
                for row in report["checks"]
            ]
        )
        st.dataframe(gate_table, hide_index=True, width="stretch", height=388)

    warning_checks = [row for row in report["checks"] if row["status"] == "warn"]
    if warning_checks:
        with st.expander(f"Known limitations ({len(warning_checks)})"):
            for row in warning_checks:
                st.markdown(f"**{row['label']} — {row['observed']}**")
                st.write(row["detail"])


def _render_opportunity_map(df: pd.DataFrame, score_col: str, model_name: str) -> None:
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
    figure = px.scatter(
        plot_df,
        x="Value",
        y="Quality",
        text="Chart label",
        size="Market Cap ($B)",
        size_max=38,
        color=score_col,
        color_continuous_scale=["#164E63", "#2DD4BF", "#60A5FA"],
        labels={
            "Value": "Relative value score",
            "Quality": "Business quality score",
            score_col: f"{model_name} score",
            "Market Cap ($B)": "Company size ($ billions)",
            "12-1 Momentum %": "Past 12-to-1 month return %",
            "Annualized Volatility %": "Past price volatility %",
        },
        hover_name="Company + Symbol",
        hover_data={
            "Chart label": False,
            "P/E": True,
            "EV/EBITDA": True,
            "ROIC %": True,
            "12-1 Momentum %": True,
            "Annualized Volatility %": True,
        },
        range_x=[-5, 105],
        range_y=[-5, 105],
    )
    figure.update_traces(textposition="top center", textfont_size=10, marker_opacity=0.72)
    figure.add_hline(y=50, line_dash="dot", line_color="#6F849B", opacity=0.7)
    figure.add_vline(x=50, line_dash="dot", line_color="#6F849B", opacity=0.7)
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
) -> None:
    """Render the lookup surface at one consistent universe grain."""
    st.markdown("### Ranked Universe")
    st.caption(
        "Sort and scan the full reviewed universe. Missing scores remain visible and are "
        "never filled with estimates."
    )
    table_columns = [
        rank_col,
        "Company",
        "Ticker",
        grade_col,
        score_col,
        "Quality",
        "Value",
        "Momentum",
        "Low Volatility",
        "12-1 Momentum %",
        "Annualized Volatility %",
        "Missing data",
    ]
    full_table = df.sort_values(
        [score_col, "Ticker"], ascending=[False, True], na_position="last"
    )[table_columns].rename(
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
            "Missing data": "Evidence gap",
        }
    )
    st.dataframe(full_table, width="stretch", hide_index=True, height=600)
    st.caption(
        "Scores are cross-sectional research measures from 0–100, not expected returns or "
        "probabilities."
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
    critical_incidents = [
        row for row in unresolved_incidents if row.get("severity") == "critical"
    ]
    if critical_incidents:
        return f"Review critical incident {critical_incidents[0]['incident_id']}."

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
        return (
            "After the scheduled close is reviewed, confirm or reject the pending paper "
            "proposal; no broker action is involved."
        )
    return "Review the next naturally triggered refresh, health, and backup cycle."


def _render_system_control(report: dict) -> None:
    """Render a source-backed operator workspace without fabricated telemetry."""
    _page_header(
        "Local operations",
        "System Control",
        "Check data readiness, scheduler execution, ingests, backups, policy stability, "
        "and the next human action without opening DuckDB or systemd.",
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
    unresolved_incidents = [row for row in incidents if row.get("state") != "resolved"]
    critical_incidents = [
        row for row in unresolved_incidents if row.get("severity") == "critical"
    ]

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Research readiness", "Ready" if report["ready"] else "Blocked")
    c2.metric("Certified decision date", display_date(report["certified_research_through"]))
    c3.metric("Enabled local timers", f"{enabled_timers} / {len(TIMER_NAMES)}")
    c4.metric(
        "Latest verified backup",
        display_date(backup_date) if backup.get("status") == "verified" else "Needs attention",
    )
    c5.metric(
        "Unresolved incidents",
        len(unresolved_incidents),
        f"{len(critical_incidents)} critical",
    )

    if certification_current:
        st.success(certification_detail)
    else:
        st.warning(certification_detail)
    next_review = _next_operator_review(report, operations, monitor)
    st.info(f"Next required human review: {next_review}")

    readiness_col, policy_col = st.columns([1.35, 0.65], gap="large")
    with readiness_col, st.container(border=True):
        st.markdown("### Reviewed Data Coverage")
        st.caption("These counts reconcile directly to the current fail-closed readiness report.")
        _render_coverage_chart(report)
    with policy_col, st.container(border=True):
        st.markdown("### Control Status")
        warnings = [row for row in report["checks"] if row["status"] == "warn"]
        st.metric("Readiness warnings", len(warnings))
        if daily_cycle is None:
            st.warning("No recoverable daily update has completed yet.")
        elif daily_cycle["state"] == "success":
            st.success(
                "Latest daily workflow passed for "
                f"{display_date(daily_cycle['target_session'])}."
            )
        elif daily_cycle["state"] == "running":
            st.info(
                "The daily workflow is currently updating "
                f"{display_date(daily_cycle['target_session'])}."
            )
        else:
            st.error(
                "The latest daily workflow did not finish safely. Startup catch-up "
                "will retry it."
            )
        if forward is None:
            st.warning("Forward-policy evidence is unavailable.")
        elif forward["ready"]:
            st.success("Forward-policy baseline unchanged.")
        else:
            st.error("Forward-policy drift requires review.")
        if backup.get("status") == "verified":
            st.success(f"Backup checksums verified across {backup['files']} file(s).")
            st.caption(f"Manifest SHA-256: {backup['manifest_sha256'][:16]}…")
        elif backup.get("status") == "failed":
            st.error("The newest backup failed verification.")
        else:
            st.warning("No local backup is available.")
        if latest_universe_attestation is None:
            st.warning("No automatic membership evidence review has run yet.")
        elif latest_universe_attestation["status"] == "accepted_no_change":
            st.success(
                "The latest free-source membership check found no change through "
                f"{display_date(latest_universe_attestation['requested_coverage_through'])}."
            )
        else:
            st.error(
                "The latest membership check stopped for human review; no reference "
                "dates were silently extended."
            )

    st.subheader("Source Freshness")
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

    st.subheader("Scheduler Runtime")
    st.caption(
        "Runtime status is queried with a strict timeout. If the user service bus is "
        "unavailable, installation evidence is shown as unverified instead of guessed."
    )
    if operations.get("linger_enabled") is True:
        st.success("Automatic updates remain active after desktop logout while the computer is on.")
    elif operations.get("linger_enabled") is False:
        st.warning(
            "Automatic updates pause after desktop logout and catch up at the next login."
        )
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

    st.subheader("Recent Ingest Outcomes")
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

    st.subheader("Notifications & Incident History")
    st.caption(
        "System failures are written to an independent local incident ledger, so they remain "
        "recordable even when the analytical DuckDB cannot be opened."
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
    st.info(
        "External delivery is not configured yet. The durable local ledger and systemd "
        "failure capture are active; email or Slack remains the next transport milestone."
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
certified_from = readiness["certified_research_from"]
certified_through = readiness["certified_research_through"]
certification_current, certification_detail = us_certification_freshness_message(
    certified_through
)
if readiness["ready"] and certification_current:
    sidebar_status = '<span class="aios-chip success">● Research gates passed</span>'
elif readiness["ready"]:
    sidebar_status = '<span class="aios-chip warning">● Daily update pending</span>'
else:
    sidebar_status = '<span class="aios-chip danger">● New decisions blocked</span>'

st.sidebar.markdown(sidebar_status, unsafe_allow_html=True)
st.sidebar.caption(f"Reviewed through {display_date(certified_through)}")
st.sidebar.divider()
view = st.sidebar.radio("Workspace", VIEW_OPTIONS, label_visibility="collapsed")
default_as_of = certified_through or (date.today() - timedelta(days=1)).isoformat()
as_of = default_as_of
ranking_model = MODEL_QV_LABEL
if view in {VIEW_RANKINGS, VIEW_DETAILS}:
    if certified_from is None or certified_through is None:
        st.error("No complete reviewed U.S. research window is available yet.")
        st.stop()
    selected_date = st.sidebar.date_input(
        "Research date",
        value=date.fromisoformat(default_as_of),
        min_value=date.fromisoformat(certified_from),
        max_value=date.fromisoformat(certified_through),
        help="Scores use only information that was publicly available by this date.",
    )
    as_of = selected_date.isoformat()
    st.sidebar.caption(
        f"Reviewed range: {display_date(certified_from)}–{display_date(certified_through)}"
    )

    ranking_model = st.sidebar.radio(
        "Scoring method",
        MODEL_OPTIONS,
        index=MODEL_OPTIONS.index(MODEL_QV_LABEL),
        help=(
            "Quality + Value is the baseline. The experimental method also considers "
            "past price trend and price stability. Both are research scores, not forecasts."
        ),
    )

st.sidebar.divider()
st.sidebar.markdown("**Research Boundary**")
st.sidebar.caption("Supervised analysis only. No personal recommendation or broker order.")
with st.sidebar.expander("Data sources & freshness"):
    prices_current, freshness_detail = us_eod_freshness_message(
        readiness["raw_prices_through"]
    )
    st.markdown(
        "The current U.S. reference build uses SEC EDGAR, yfinance/Tiingo/Stooq, "
        "release-dated FRED data, and local DuckDB storage. Normal use does not "
        "require database knowledge."
    )
    st.markdown(
        f"Prices: {display_date(readiness['raw_prices_through'])}  \n"
        f"Company filings: {display_date(readiness['fundamentals_through'])}  \n"
        f"Economic releases: {display_date(readiness['macro_releases_through'])}  \n"
        "These raw dates do not override the reviewed window."
    )
    if prices_current:
        st.success(freshness_detail)
    else:
        st.warning(freshness_detail)
    if certification_current:
        st.success(certification_detail)
    else:
        st.warning(certification_detail)
    st.caption(
        "Because India is ahead of New York, a U.S. session dated today normally becomes "
        "complete after midnight IST and is loaded by the following scheduled refresh."
    )
st.sidebar.markdown(
    '<div class="aios-next-action"><strong>Local mode</strong><br>'
    "DuckDB + checksum-protected paper state. No broker connection.</div>",
    unsafe_allow_html=True,
)


# ----------------------------------------------------------------------
# Load factor data only for views that display stock scores
# ----------------------------------------------------------------------
df = pd.DataFrame()
if view in {VIEW_RANKINGS, VIEW_DETAILS}:
    try:
        df = load_composite(as_of)
    except Exception as e:
        st.error(
            "The research scores could not be calculated. Check the research date and data "
            "refresh, then try again."
        )
        with st.expander("Technical error details"):
            st.code(str(e))
        st.stop()


# ----------------------------------------------------------------------
# VIEW 1: OPERATING OVERVIEW
# ----------------------------------------------------------------------
if view == VIEW_HOME:
    _render_overview(readiness)


# ----------------------------------------------------------------------
# VIEW 2: RESEARCH EXPLORER
# ----------------------------------------------------------------------
elif view == VIEW_RANKINGS:
    use_qvml = model_key(ranking_model) == "qvml"
    rank_col = "QVML Rank" if use_qvml else "Rank"
    grade_col = "QVML Grade" if use_qvml else "Grade"
    score_col = "QVML Score" if use_qvml else "QV Score"
    model_name = "Four-factor" if use_qvml else "Quality + Value"

    _page_header(
        "Cross-sectional factor research",
        "Research Explorer",
        "Compare the reviewed universe, inspect factor trade-offs, and identify companies "
        "for deeper research.",
    )
    st.caption(RESEARCH_ONLY_NOTICE)
    if df.empty:
        st.info(
            "No stock scores are available for this date. Choose another research date "
            "or refresh the underlying company and price information."
        )
        st.stop()

    macro_regime = friendly_regime(df["Macro regime"].iloc[0])
    quality_weight = df["Quality weight %"].iloc[0]
    value_weight = df["Value weight %"].iloc[0]
    macro_pit_ready = bool(df["Macro PIT ready"].iloc[0])
    if use_qvml:
        st.caption(
            f"Research date: {display_date(as_of)} • {len(df)} stocks checked • "
            f"Economic backdrop: {macro_regime} • Score mix: business quality "
            f"{df['QVML Quality weight %'].iloc[0]:.0f}%, relative value "
            f"{df['QVML Value weight %'].iloc[0]:.0f}%, price trend "
            f"{df['QVML Momentum weight %'].iloc[0]:.0f}%, price stability "
            f"{df['QVML Low Volatility weight %'].iloc[0]:.0f}%"
        )
        st.warning(
            "The four-factor method is experimental. It completed the engineering test, "
            "but performed worse than the simpler Quality + Value method in the short "
            "historical window. Do not treat it as a stronger buy signal."
        )
    else:
        st.caption(
            f"Research date: {display_date(as_of)} • {len(df)} stocks checked • "
            f"Economic backdrop: {macro_regime} • Score mix: business quality "
            f"{quality_weight:.0f}% and relative value {value_weight:.0f}%"
        )
    if not macro_pit_ready:
        st.warning(
            "The economic picture was incomplete on this date, so the app is using its "
            "standard 60% business-quality and 40% relative-value mix."
        )

    ranked_df = df[df[score_col].notna()].sort_values(
        [score_col, "Ticker"], ascending=[False, True]
    )
    action_unverified = int(
        df["Missing inputs"]
        .apply(lambda values: "market:corporate_actions_unverified" in values)
        .sum()
    )
    if use_qvml and action_unverified:
        st.error(
            f"{action_unverified} stocks do not have fully verified dividend and stock-split "
            "history. Their four-factor scores are hidden instead of being guessed."
        )

    # Top metrics row
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Stocks checked", len(df), help="All stocks examined for the selected date.")
    c2.metric(
        "Complete scores",
        len(ranked_df),
        help="Stocks with enough verified information for the selected scoring method.",
    )
    average_score = ranked_df[score_col].mean()
    c3.metric(
        "Average complete score",
        f"{average_score:.1f} / 100" if pd.notna(average_score) else "N/A",
        help="A relative comparison score, not an expected investment return.",
    )
    c4.metric(
        "Incomplete scores",
        len(df) - len(ranked_df),
        help="Stocks withheld because required information was missing or unverified.",
    )

    map_tab, ranking_tab, coverage_tab = st.tabs(
        ["Opportunity Map", "Ranked Universe", "Coverage Review"]
    )
    with map_tab:
        _render_opportunity_map(df, score_col, model_name)
    with ranking_tab:
        _render_ranked_universe(
            df,
            rank_col=rank_col,
            grade_col=grade_col,
            score_col=score_col,
        )
    with coverage_tab:
        _render_research_coverage(
            df,
            ranked_df,
            grade_col=grade_col,
            score_col=score_col,
        )

# ----------------------------------------------------------------------
# VIEW 2: COMPANY DETAILS
# ----------------------------------------------------------------------
elif view == VIEW_DETAILS:
    _page_header(
        "Security-level evidence",
        "Company Lens",
        "Trace one stock from its relative score to the underlying quality, valuation, "
        "trend, stability, and missing-evidence checks.",
    )
    st.caption(RESEARCH_ONLY_NOTICE)

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

    selected_company = st.selectbox(
        "Choose a company",
        list(label_to_ticker),
        help="Each company is shown with its market symbol, such as Apple Inc. (AAPL).",
    )
    ticker = label_to_ticker[selected_company]
    row = df[df["Ticker"] == ticker].iloc[0]
    use_qvml = model_key(ranking_model) == "qvml"
    score_col = "QVML Score" if use_qvml else "QV Score"
    grade_col = "QVML Grade" if use_qvml else "Grade"
    model_name = "Four-factor" if use_qvml else "Quality + Value"
    displayed_score = row[score_col]
    displayed_grade = row[grade_col]

    # Header card
    score_text = f"{displayed_score:.1f}" if pd.notna(displayed_score) else "N/A"
    grade_text = str(displayed_grade) if pd.notna(displayed_grade) else "N/A"
    st.markdown(
        f"### {row['Company + Symbol']} — research grade {grade_text} — "
        f"score {score_text} / 100"
    )
    st.caption(
        f"Research date: {display_date(as_of)} • {model_name} method • The grade compares "
        "this stock "
        "with other covered stocks; it is not a buy, hold, or sell rating."
    )
    economic_backdrop = friendly_regime(row["Macro regime"])
    if use_qvml:
        st.caption(
            f"Economic backdrop: {economic_backdrop} • Score mix: business quality "
            f"{row['QVML Quality weight %']:.0f}%, relative value "
            f"{row['QVML Value weight %']:.0f}%, price trend "
            f"{row['QVML Momentum weight %']:.0f}%, price stability "
            f"{row['QVML Low Volatility weight %']:.0f}% • Experimental"
        )
    else:
        st.caption(
            f"Economic backdrop: {economic_backdrop} • Score mix: business quality "
            f"{row['Quality weight %']:.0f}% and relative value "
            f"{row['Value weight %']:.0f}% • Economic information complete: "
            f"{'yes' if row['Macro PIT ready'] else 'no; standard mix used'}"
        )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric(
        "Business quality",
        f"{row['Quality']:.1f} / 100" if pd.notna(row["Quality"]) else "N/A",
        help="Higher means stronger profitability and financial health versus covered stocks.",
    )
    col2.metric(
        "Relative value",
        f"{row['Value']:.1f} / 100" if pd.notna(row["Value"]) else "N/A",
        help="Higher means the stock looks less expensive on several valuation measures.",
    )
    col3.metric(
        "Price trend",
        f"{row['Momentum']:.1f} / 100" if pd.notna(row["Momentum"]) else "N/A",
        help="Higher means a stronger past price trend, excluding the most recent month.",
    )
    col4.metric(
        "Price stability",
        f"{row['Low Volatility']:.1f} / 100" if pd.notna(row["Low Volatility"]) else "N/A",
        help="Higher means the stock price moved more steadily than other covered stocks.",
    )

    mc1, mc2, mc3, mc4 = st.columns(4)
    mc1.metric(
        "Past 12-to-1 month return",
        f"{row['12-1 Momentum %']:.1f}%" if pd.notna(row["12-1 Momentum %"]) else "N/A",
        help="Return over roughly one year, leaving out the most recent month.",
    )
    mc2.metric(
        "Past price volatility",
        f"{row['Annualized Volatility %']:.1f}%"
        if pd.notna(row["Annualized Volatility %"])
        else "N/A",
        help="How widely daily returns varied, expressed as a yearly rate. Lower is steadier.",
    )
    mc3.metric("Share price", f"${row['Price']}" if pd.notna(row["Price"]) else "N/A")
    mc4.metric(
        "Company market value",
        f"${row['Market Cap ($B)']} billion" if pd.notna(row["Market Cap ($B)"]) else "N/A",
    )

    # Quality breakdown
    st.subheader("Business strength details")
    st.caption(
        "These measures describe profitability, cash generation, margins, and financial health."
    )
    qc1, qc2, qc3, qc4 = st.columns(4)
    qc1.metric(
        "Return on invested capital",
        f"{row['ROIC %']}%" if pd.notna(row["ROIC %"]) else "N/A",
        help="How efficiently the company turns invested money into operating profit.",
    )
    qc2.metric(
        "Free-cash-flow margin",
        f"{row['FCF Margin %']}%" if pd.notna(row["FCF Margin %"]) else "N/A",
        help="Free cash generated for every dollar of revenue.",
    )
    qc3.metric(
        "Gross margin",
        f"{row['Gross Margin %']}%" if pd.notna(row["Gross Margin %"]) else "N/A",
        help="Revenue left after the direct cost of products or services.",
    )
    qc4.metric(
        "Financial-health checks",
        f"{row['Piotroski F']}/9" if pd.notna(row["Piotroski F"]) else "N/A",
        help="Number of available Piotroski financial-health checks passed.",
    )

    # Value breakdown
    st.subheader("Price and valuation details")
    st.caption(
        "These ratios compare the market price with company earnings, cash flow, sales, "
        "and assets. "
        "Lower ratios often mean cheaper, but can also reflect genuine business risk."
    )
    vc1, vc2, vc3, vc4, vc5 = st.columns(5)
    vc1.metric(
        "Price / earnings",
        f"{row['P/E']}" if pd.notna(row["P/E"]) else "N/A",
        help="Share price divided by earnings per share.",
    )
    vc2.metric(
        "Business value / operating profit",
        f"{row['EV/EBITDA']}" if pd.notna(row["EV/EBITDA"]) else "N/A",
        help="Enterprise value divided by EBITDA, a rough operating-profit measure.",
    )
    vc3.metric(
        "Price / free cash flow",
        f"{row['P/FCF']}" if pd.notna(row["P/FCF"]) else "N/A",
        help="Share price compared with cash left after operating and capital spending.",
    )
    vc4.metric(
        "Business value / sales",
        f"{row['EV/Sales']}" if pd.notna(row["EV/Sales"]) else "N/A",
        help="Enterprise value divided by annual sales.",
    )
    vc5.metric(
        "Price / book value",
        f"{row['P/B']}" if pd.notna(row["P/B"]) else "N/A",
        help="Share price compared with accounting net assets per share.",
    )

    # The row already contains universe-relative scores. Reusing it avoids the
    # former five full-universe recomputations on every ticker selection.
    factor_profile = [
        ("Business quality", row["Quality"]),
        ("Relative value", row["Value"]),
        ("Price trend", row["Momentum"]),
        ("Price stability", row["Low Volatility"]),
    ]
    available_profile = [
        (label, float(value)) for label, value in factor_profile if pd.notna(value)
    ]
    if available_profile:
        st.subheader("Factor Profile")
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
            color_continuous_scale=["#164E63", "#2DD4BF", "#60A5FA"],
        )
        fig2.add_vline(x=50, line_dash="dot", line_color="#6F849B")
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
        with st.expander(f"Missing or unverified information ({len(friendly_reasons)})"):
            for reason in friendly_reasons:
                st.markdown(f"- {reason}")
    else:
        st.success("All information required for the selected score is available.")


# ----------------------------------------------------------------------
# VIEW 3: PAPER MONITOR
# ----------------------------------------------------------------------
elif view == VIEW_PAPER:
    _page_header(
        "Simulation-only portfolio",
        "Portfolio Monitor",
        "Follow the governed proposal, holdings, modeled costs, and daily account value "
        "without connecting to a broker.",
    )
    st.warning(
        "Simulation only — no broker is connected and this page cannot place an order. "
        "It tracks what the rules would have done using reviewed closing prices."
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
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Simulated account value", f"${summary['equity']:,.2f}")
    c2.metric("Simulated cash", f"${summary['cash']:,.2f}")
    c3.metric("Stocks currently simulated", len(summary["holdings"]))
    c4.metric("Change from highest value", f"{summary['drawdown']:.2%}")
    st.caption(
        f"Last reviewed market date: {summary['last_market_date'] or 'not invested yet'} • "
        f"Recorded rebalances: {summary['execution_count']} • "
        f"Modeled trading costs so far: ${summary['transaction_costs']:,.2f}"
    )

    forward = monitor["forward"]
    if forward is None:
        st.warning(
            "The untouched forward-test baseline has not been frozen yet. Historical and "
            "paper features still work, but new observations cannot yet prove that the "
            "research policy stayed unchanged."
        )
    elif forward["ready"]:
        st.success(
            "Forward-test baseline unchanged. Factor, risk, cost, tax, calendar, and "
            f"paper rules match the freeze; {forward['registered_proposals']} proposal(s) "
            "are recorded."
        )
    else:
        st.error(
            "Forward-test policy drift detected. Do not count newer observations until "
            "the changes are reviewed and a new trial is deliberately started."
        )
        with st.expander("What changed"):
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
            st.info(
                f"{proposal_status}. It is waiting for reviewed closing prices from "
                f"{proposal['scheduled_simulation_date']} and explicit human confirmation."
            )
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
        figure.update_traces(line=dict(color="#2DD4BF", width=2.2))
        figure.update_layout(margin=dict(l=25, r=20, t=12, b=28))
        _style_figure(figure, height=370, show_legend=False)
        st.plotly_chart(figure, width="stretch")

    with st.expander("Assumptions and operator steps"):
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
        if proposal is not None and not proposal.get("already_simulated"):
            st.code(
                f"aios paper-execute --proposal {monitor['proposal_path']} --confirm-simulated",
                language="bash",
            )
            st.caption(
                "That command still refuses to proceed if reviewed next-session prices, "
                "readiness, risk checks, or the saved evidence do not match."
            )


# ----------------------------------------------------------------------
# VIEW 5: SYSTEM CONTROL
# ----------------------------------------------------------------------
elif view == VIEW_SYSTEM:
    _render_system_control(readiness)


# ----------------------------------------------------------------------
# VIEW 6: HOW IT WORKS
# ----------------------------------------------------------------------
elif view == VIEW_METHOD:
    _page_header(
        "Research governance",
        "Methodology & Data",
        "Understand the scoring model, point-in-time protections, source boundaries, and "
        "current operating limits.",
    )
    st.caption(RESEARCH_ONLY_NOTICE)
    st.markdown("""
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
""")

    with st.expander("Technical methodology and audit details"):
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

    st.info(
        "For the complete audit trail, read ARCHITECTURE.md and "
        "SP500_DATA_PROVENANCE.md in the project folder."
    )

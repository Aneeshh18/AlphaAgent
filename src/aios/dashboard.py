"""AI Investment OS — Dashboard (Streamlit).

Four plain-language views:
  1. STOCK RANKINGS  — baseline or experimental comparison + chart
  2. COMPANY DETAILS — one-stock score and evidence review
  3. PAPER MONITOR   — supervised local simulation state and next action
  4. HOW IT WORKS    — non-technical explanation + optional audit details

Run:  .venv/bin/aios dashboard
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# Ensure src/ on path when run via `streamlit run`
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aios.config import settings  # noqa: E402
from aios.dashboard_copy import (  # noqa: E402
    MODEL_OPTIONS,
    MODEL_QV_LABEL,
    RESEARCH_ONLY_NOTICE,
    VIEW_DETAILS,
    VIEW_METHOD,
    VIEW_OPTIONS,
    VIEW_PAPER,
    VIEW_RANKINGS,
    friendly_missing_reasons,
    friendly_missing_summary,
    friendly_regime,
    model_key,
)
from aios.factors.composite import compute_composite  # noqa: E402
from aios.forward import (  # noqa: E402
    DEFAULT_FORWARD_RELATIVE_PATH,
    assess_forward_trial,
)
from aios.paper import (  # noqa: E402
    ACCOUNT_DOCUMENT_KIND,
    DEFAULT_ACCOUNT_RELATIVE_PATH,
    PROPOSAL_DOCUMENT_KIND,
    paper_account_summary,
    read_paper_document,
)
from aios.readiness import assess_us_readiness  # noqa: E402
from aios.storage.store import get_store  # noqa: E402

st.set_page_config(page_title="AI Investment OS", page_icon="📊", layout="wide")


# ----------------------------------------------------------------------
# Cached data loaders
# ----------------------------------------------------------------------
@st.cache_data(ttl=300)
def load_composite(as_of: str) -> pd.DataFrame:
    store = get_store()
    tickers = [row["ticker"] for row in store.universe_membership_on("sp500", as_of)]
    rows = compute_composite(tickers, as_of, store, include_market_factors=True)
    return _rows_to_df(rows)


@st.cache_data(ttl=300)
def load_us_readiness() -> dict:
    """Load the same fail-closed current-use gate exposed by the CLI."""
    return assess_us_readiness(purpose="paper").to_dict()


@st.cache_data(ttl=60)
def load_paper_monitor() -> dict:
    """Load checksum-validated local simulation state and its newest proposal."""
    account_path = settings.project_root / DEFAULT_ACCOUNT_RELATIVE_PATH
    if not account_path.exists():
        return {"exists": False, "account_path": str(account_path), "proposal": None}
    store = get_store()
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


def _rows_to_df(rows) -> pd.DataFrame:
    data = []
    for r in rows:
        data.append(
            {
                "Rank": r.qv_rank,
                "Ticker": r.ticker,
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


# ----------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------
st.sidebar.title("📊 AI Investment OS")
st.sidebar.caption("Plain-language stock research")
st.sidebar.warning("Research only — this app does not issue buy or sell instructions.")
st.sidebar.info(
    "Current scope: audited U.S. reference data. India starts only after the U.S. gates pass."
)

readiness = load_us_readiness()
certified_from = readiness["certified_research_from"]
certified_through = readiness["certified_research_through"]
if readiness["ready"]:
    st.sidebar.success("Current U.S. paper-monitoring data passed every operating gate.")
else:
    st.sidebar.error(
        "Current U.S. paper decisions are blocked. Reviewed historical research is available "
        f"from {certified_from or 'an unavailable date'} through "
        f"{certified_through or 'an unavailable date'}."
    )

view = st.sidebar.radio("Page", VIEW_OPTIONS)
default_as_of = certified_through or (date.today() - timedelta(days=1)).isoformat()
as_of = default_as_of
ranking_model = MODEL_QV_LABEL
if view in {VIEW_RANKINGS, VIEW_DETAILS}:
    as_of = st.sidebar.text_input(
        "Research date",
        value=default_as_of,
        help="Scores use only information that was publicly available by this date.",
    )
    st.sidebar.caption(f"Latest date with broad reviewed coverage: {default_as_of}")
    try:
        selected_date = date.fromisoformat(as_of)
    except ValueError:
        st.error("Enter the research date as YYYY-MM-DD, for example 2024-12-31.")
        st.stop()
    if certified_from is None or certified_through is None:
        st.error("No complete reviewed U.S. research window is available yet.")
        st.stop()
    if selected_date < date.fromisoformat(certified_from) or selected_date > date.fromisoformat(
        certified_through
    ):
        st.error(
            f"This date is outside the reviewed U.S. window ({certified_from} through "
            f"{certified_through}). Raw downloads are never substituted for reviewed evidence."
        )
        st.stop()

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
st.sidebar.markdown("**Where the information comes from**")
st.sidebar.markdown("• Company filings from the SEC\n• Daily market prices")
with st.sidebar.expander("Technical details"):
    st.markdown(
        "The current U.S. reference build uses SEC EDGAR, yfinance/Tiingo/Stooq, "
        "release-dated FRED data, and local DuckDB storage. Normal use does not "
        "require database knowledge."
    )
    st.markdown(
        f"Raw source dates — prices: {readiness['raw_prices_through']}; company filings: "
        f"{readiness['fundamentals_through']}; economic releases: "
        f"{readiness['macro_releases_through']}. These dates do not override the reviewed "
        "window shown above."
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
# VIEW 1: STOCK RANKINGS
# ----------------------------------------------------------------------
if view == VIEW_RANKINGS:
    use_qvml = model_key(ranking_model) == "qvml"
    rank_col = "QVML Rank" if use_qvml else "Rank"
    grade_col = "QVML Grade" if use_qvml else "Grade"
    score_col = "QVML Score" if use_qvml else "QV Score"
    model_name = "Four-factor" if use_qvml else "Quality + Value"

    st.title("📈 Stock Research Rankings")
    st.info(RESEARCH_ONLY_NOTICE)
    st.caption(
        "Use higher-ranked stocks as a starting point for investigation. Before acting, "
        "review the company, current news, portfolio risk, taxes, and your own goals."
    )
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
            f"Research date: {as_of} • {len(df)} stocks checked • "
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
            f"Research date: {as_of} • {len(df)} stocks checked • "
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

    # Scatter: business quality vs relative value.
    st.subheader("Where stocks sit on business quality and relative value")
    st.caption(
        "Stocks toward the upper-right have stronger measured businesses and look less "
        "expensive than other covered stocks. Bubble size shows company size; darker "
        f"color means a higher {model_name.lower()} research score."
    )
    plot_df = df.dropna(subset=["Quality", "Value", score_col]).copy()
    if plot_df.empty:
        st.info(
            "This comparison chart is unavailable because no stock has all required "
            "information. Review the explanations in the table below."
        )
    else:
        # Market cap may be NaN for some tickers — fill with median so the bubble
        # still renders (size is purely visual, not data).
        median_mcap = plot_df["Market Cap ($B)"].median()
        plot_df["Market Cap ($B)"] = plot_df["Market Cap ($B)"].fillna(
            median_mcap if pd.notna(median_mcap) else 100
        )
        fig = px.scatter(
            plot_df,
            x="Value",
            y="Quality",
            text="Ticker",
            size="Market Cap ($B)",
            size_max=45,
            color=score_col,
            color_continuous_scale="Blues",
            labels={
                "Value": "Relative value score",
                "Quality": "Business quality score",
                score_col: "Overall research score",
                "Market Cap ($B)": "Company size ($ billions)",
                "12-1 Momentum %": "Past 12-to-1 month return %",
                "Annualized Volatility %": "Past price volatility %",
            },
            hover_data=[
                "P/E",
                "EV/EBITDA",
                "ROIC %",
                "12-1 Momentum %",
                "Annualized Volatility %",
            ],
            range_x=[-5, 105],
            range_y=[-5, 105],
        )
        fig.update_traces(textposition="top center", textfont_size=10)
        fig.add_hline(y=50, line_dash="dash", line_color="gray", opacity=0.4)
        fig.add_vline(x=50, line_dash="dash", line_color="gray", opacity=0.4)
        fig.update_layout(height=520, coloraxis_colorbar=dict(title="Research score"))
        st.plotly_chart(fig, width="stretch")

    # Full ranking table
    st.subheader("All stock scores")
    st.caption(
        "Scores compare covered stocks with one another; they do not predict future returns."
    )
    table_columns = [
        rank_col,
        "Ticker",
        grade_col,
        score_col,
        "Quality",
        "Value",
        "Momentum",
        "Low Volatility",
        "12-1 Momentum %",
        "Annualized Volatility %",
        "Quality inputs",
        "Value inputs",
        "Market observations",
        "Missing data",
    ]
    full_table = df.sort_values([score_col, "Ticker"], ascending=[False, True], na_position="last")[
        table_columns
    ].rename(
        columns={
            rank_col: "Research rank",
            "Ticker": "Stock symbol",
            grade_col: "Research grade",
            score_col: "Overall score",
            "Quality": "Business quality",
            "Value": "Relative value",
            "Momentum": "Price trend",
            "Low Volatility": "Price stability",
            "12-1 Momentum %": "Past 12-to-1 month return %",
            "Annualized Volatility %": "Past price volatility %",
            "Quality inputs": "Quality measures found",
            "Value inputs": "Value measures found",
            "Market observations": "Price days used",
            "Missing data": "Why a score is incomplete",
        }
    )
    st.dataframe(
        full_table,
        width="stretch",
        hide_index=True,
        height=480,
    )

    # Top 5 / Bottom 5
    if ranked_df.empty:
        st.info("No stocks currently have all information required for this scoring method.")
    else:
        summary_columns = [
            "Ticker",
            grade_col,
            score_col,
            "Quality",
            "Value",
            "Momentum",
            "Low Volatility",
        ]
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("##### Highest five research scores")
            st.caption("A shortlist for deeper research—not a buy list.")
            st.dataframe(
                ranked_df.head(5)[summary_columns].rename(
                    columns={
                        "Ticker": "Stock symbol",
                        grade_col: "Grade",
                        score_col: "Overall score",
                        "Quality": "Business quality",
                        "Value": "Relative value",
                        "Momentum": "Price trend",
                        "Low Volatility": "Price stability",
                    }
                ),
                hide_index=True,
                width="stretch",
            )
        with col2:
            st.markdown("##### Lowest five complete research scores")
            st.caption("A low score is not automatically a sell instruction.")
            st.dataframe(
                ranked_df.tail(5)[summary_columns].rename(
                    columns={
                        "Ticker": "Stock symbol",
                        grade_col: "Grade",
                        score_col: "Overall score",
                        "Quality": "Business quality",
                        "Value": "Relative value",
                        "Momentum": "Price trend",
                        "Low Volatility": "Price stability",
                    }
                ),
                hide_index=True,
                width="stretch",
            )


# ----------------------------------------------------------------------
# VIEW 2: COMPANY DETAILS
# ----------------------------------------------------------------------
elif view == VIEW_DETAILS:
    st.title("🔎 Company Research Details")
    st.info(RESEARCH_ONLY_NOTICE)

    ticker_options = sorted(df["Ticker"].tolist()) if "Ticker" in df.columns else []
    if not ticker_options:
        st.info(
            "No stocks are available for this research date. Choose another date or "
            "refresh the underlying company and price information."
        )
        st.stop()

    ticker = st.selectbox(
        "Choose a stock symbol",
        ticker_options,
        help="A stock symbol is the short market code for a listed company, such as AAPL.",
    )
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
    st.markdown(f"### {ticker} — research grade {grade_text} — score {score_text} / 100")
    st.caption(
        f"Research date: {as_of} • {model_name} method • The grade compares this stock "
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
        st.subheader("How this stock compares with other covered stocks")
        st.caption("Farther from the center means a higher relative score; it is not a forecast.")
        radar_labels = [label for label, _ in available_profile]
        radar_values = [value for _, value in available_profile]
        fig2 = go.Figure(
            data=go.Scatterpolar(
                r=radar_values + [radar_values[0]],
                theta=radar_labels + [radar_labels[0]],
                fill="toself",
                name=ticker,
                line=dict(color="#1f77b4"),
            )
        )
        fig2.update_layout(
            polar=dict(radialaxis=dict(range=[0, 100])),
            height=420,
            margin=dict(l=40, r=40, t=40, b=40),
        )
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
    st.title("🧪 Supervised Paper Monitor")
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
            f"Research date: {proposal['decision_date']} • "
            f"Complete Quality + Value scores: {proposal['factor_eligible_count']} • "
            f"Broad grouping used for concentration checks: "
            f"{proposal['sector_classification']}"
        )
        if proposal["targets"]:
            proposal_table = pd.DataFrame(
                [
                    {
                        "Research rank": row["factor_rank"],
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
        figure.update_layout(height=380)
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
# VIEW 4: HOW IT WORKS
# ----------------------------------------------------------------------
elif view == VIEW_METHOD:
    st.title("📖 How the Stock Rankings Work")
    st.info(RESEARCH_ONLY_NOTICE)
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

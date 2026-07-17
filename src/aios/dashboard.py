"""AI Investment OS — Dashboard (Streamlit).

Three views, designed product-style:
  1. UNIVERSE   — sortable ranking table + scatter (Quality vs Value)
  2. DEEP DIVE  — single-ticker full factor report
  3. METHOD     — how scores are computed (transparency)

Run:  streamlit run src/aios/dashboard.py
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

from aios.factors.composite import compute_composite  # noqa: E402
from aios.storage.store import get_store  # noqa: E402

st.set_page_config(page_title="AI Investment OS", page_icon="📊", layout="wide")


# ----------------------------------------------------------------------
# Cached data loaders
# ----------------------------------------------------------------------
@st.cache_data(ttl=300)
def load_composite(as_of: str) -> pd.DataFrame:
    store = get_store()
    tickers = [r["ticker"] for r in store.query("SELECT ticker FROM securities ORDER BY ticker")]
    rows = compute_composite(tickers, as_of, store)
    return _rows_to_df(rows)


@st.cache_data(ttl=300)
def load_universe_tickers() -> list[str]:
    store = get_store()
    return [r["ticker"] for r in store.query("SELECT ticker FROM securities ORDER BY ticker")]


def _rows_to_df(rows) -> pd.DataFrame:
    data = []
    for r in rows:
        data.append(
            {
                "Rank": r.qv_rank,
                "Ticker": r.ticker,
                "Grade": r.grade,
                "QV Score": round(r.qv_score, 1) if r.qv_score is not None else None,
                "Quality": round(r.quality_score, 1) if r.quality_score is not None else None,
                "Value": round(r.value_score, 1) if r.value_score is not None else None,
                "Macro regime": r.macro_regime,
                "Quality weight %": round(r.quality_weight * 100, 1),
                "Value weight %": round(r.value_weight * 100, 1),
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
            }
        )
    return pd.DataFrame(data)


# ----------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------
st.sidebar.title("📊 AI Investment OS")
st.sidebar.caption("In-house • PIT-correct • Path B build")

default_as_of = (date.today() - timedelta(days=1)).isoformat()
as_of = st.sidebar.text_input("As-of date", value=default_as_of)

view = st.sidebar.radio("View", ["🌍 Universe Ranking", "🔬 Deep Dive", "📐 Methodology"])

st.sidebar.divider()
st.sidebar.markdown("**Data sources (all free)**")
st.sidebar.markdown("• SEC EDGAR XBRL (fundamentals)\n• yfinance (prices)")
st.sidebar.markdown("**Storage:** DuckDB, point-in-time safe")


# ----------------------------------------------------------------------
# Load data (shared)
# ----------------------------------------------------------------------
try:
    df = load_composite(as_of)
except Exception as e:
    st.error(f"Failed to compute factors: {e}")
    st.stop()


# ----------------------------------------------------------------------
# VIEW 1: UNIVERSE RANKING
# ----------------------------------------------------------------------
if view == "🌍 Universe Ranking":
    st.title("🌍 Universe Ranking — QV Composite")
    if not df.empty:
        macro_regime = str(df["Macro regime"].iloc[0])
        quality_weight = df["Quality weight %"].iloc[0]
        value_weight = df["Value weight %"].iloc[0]
        macro_pit_ready = bool(df["Macro PIT ready"].iloc[0])
    else:
        macro_regime, quality_weight, value_weight, macro_pit_ready = "unknown", 60.0, 40.0, False
    st.caption(
        f"As of {as_of} • {len(df)} tickers • Regime: {macro_regime} • "
        f"Quality {quality_weight:.0f}% / Value {value_weight:.0f}%"
    )
    if not macro_pit_ready:
        st.warning(
            "Macro regime evidence is not PIT-ready for this date; baseline 60/40 "
            "weights are being used."
        )

    # Top metrics row
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tickers", len(df))
    c2.metric("Avg QV", f"{df['QV Score'].mean():.1f}")
    c3.metric("Top Grade A+", int((df["Grade"] == "A+").sum()))
    c4.metric("Grade D", int((df["Grade"] == "D").sum()))

    # Scatter: Quality vs Value (the money chart)
    st.subheader("Quality vs Value — the opportunity map")
    st.caption("Top-right = high quality AND cheap (the sweet spot). Bubble size = market cap.")
    plot_df = df.dropna(subset=["Quality", "Value"]).copy()
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
        color="QV Score",
        color_continuous_scale="RdYlGn",
        hover_data=["P/E", "EV/EBITDA", "ROIC %", "FCF Margin %"],
        range_x=[-5, 105],
        range_y=[-5, 105],
    )
    fig.update_traces(textposition="top center", textfont_size=10)
    fig.add_hline(y=50, line_dash="dash", line_color="gray", opacity=0.4)
    fig.add_vline(x=50, line_dash="dash", line_color="gray", opacity=0.4)
    fig.update_layout(height=520, coloraxis_colorbar=dict(title="QV"))
    st.plotly_chart(fig, width="stretch")

    # Full ranking table
    st.subheader("Full ranking")
    st.dataframe(
        df.style.format({"QV Score": "{:.1f}", "Quality": "{:.1f}", "Value": "{:.1f}"}),
        width="stretch",
        hide_index=True,
        height=480,
    )

    # Top 5 / Bottom 5
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("##### 🟢 Top 5 by QV")
        st.dataframe(
            df.head(5)[["Ticker", "Grade", "QV Score", "Quality", "Value"]],
            hide_index=True,
            width="stretch",
        )
    with col2:
        st.markdown("##### 🔴 Bottom 5 by QV")
        st.dataframe(
            df.tail(5)[["Ticker", "Grade", "QV Score", "Quality", "Value"]],
            hide_index=True,
            width="stretch",
        )


# ----------------------------------------------------------------------
# VIEW 2: DEEP DIVE
# ----------------------------------------------------------------------
elif view == "🔬 Deep Dive":
    st.title("🔬 Single-Ticker Deep Dive")

    ticker = st.selectbox("Select ticker", sorted(df["Ticker"].tolist()))
    row = df[df["Ticker"] == ticker].iloc[0]

    snap = None
    for r in compute_composite([ticker], as_of, get_store()):
        if r.ticker == ticker:
            snap = r
            break

    # Header card
    grade_color = {"A+": "green", "A": "green", "B": "blue", "C": "orange", "D": "red"}.get(
        row["Grade"], "gray"
    )
    st.markdown(f"### {ticker}  &nbsp; :{grade_color}[{row['Grade']}]  &nbsp; QV {row['QV Score']}")
    st.caption(f"As of {as_of}")
    st.caption(
        f"Macro regime: {row['Macro regime']} • "
        f"QV weights: Quality {row['Quality weight %']:.0f}% / "
        f"Value {row['Value weight %']:.0f}% • "
        f"PIT-ready: {'yes' if row['Macro PIT ready'] else 'no (baseline fallback)'}"
    )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Quality Score", f"{row['Quality']:.1f}" if pd.notna(row["Quality"]) else "N/A")
    col2.metric("Value Score", f"{row['Value']:.1f}" if pd.notna(row["Value"]) else "N/A")
    col3.metric("Price", f"${row['Price']}" if pd.notna(row["Price"]) else "N/A")
    col4.metric(
        "Market Cap", f"${row['Market Cap ($B)']}B" if pd.notna(row["Market Cap ($B)"]) else "N/A"
    )

    # Quality breakdown
    st.subheader("🟦 Quality breakdown")
    qc1, qc2, qc3, qc4 = st.columns(4)
    qc1.metric("ROIC", f"{row['ROIC %']}%" if pd.notna(row["ROIC %"]) else "N/A")
    qc2.metric("FCF Margin", f"{row['FCF Margin %']}%" if pd.notna(row["FCF Margin %"]) else "N/A")
    qc3.metric(
        "Gross Margin", f"{row['Gross Margin %']}%" if pd.notna(row["Gross Margin %"]) else "N/A"
    )
    qc4.metric("Piotroski F", f"{row['Piotroski F']}/9" if pd.notna(row["Piotroski F"]) else "N/A")

    # Value breakdown
    st.subheader("🟩 Value breakdown")
    st.caption(
        "Lower multiples = cheaper. Score reflects percentile rank vs universe (higher = cheaper)."
    )
    vc1, vc2, vc3, vc4, vc5 = st.columns(5)
    vc1.metric("P/E", f"{row['P/E']}" if pd.notna(row["P/E"]) else "N/A")
    vc2.metric("EV/EBITDA", f"{row['EV/EBITDA']}" if pd.notna(row["EV/EBITDA"]) else "N/A")
    vc3.metric("P/FCF", f"{row['P/FCF']}" if pd.notna(row["P/FCF"]) else "N/A")
    vc4.metric("EV/Sales", f"{row['EV/Sales']}" if pd.notna(row["EV/Sales"]) else "N/A")
    vc5.metric("P/B", f"{row['P/B']}" if pd.notna(row["P/B"]) else "N/A")

    # Radar: Quality vs Value percentile vs universe
    if snap:
        st.subheader("📈 Factor profile vs universe")
        from aios.factors import common as fc

        store = get_store()
        # Build radar from available percentile components
        radar_labels, radar_values = [], []
        # Quality components
        for name, val in [
            ("ROIC", snap.roic),
            ("FCF mgn", snap.fcf_margin),
            ("Gross mgn", snap.gross_margin),
        ]:
            if val is not None:
                peer_rows = compute_composite(sorted(df["Ticker"].tolist()), as_of, store)
                peers = []
                for pr in peer_rows:
                    v = {
                        "ROIC": pr.roic,
                        "FCF mgn": pr.fcf_margin,
                        "Gross mgn": pr.gross_margin,
                    }.get(name)
                    if v is not None:
                        peers.append(v)
                pct = fc.percentile_rank(val, peers)
                if pct is not None:
                    radar_labels.append(name)
                    radar_values.append(pct * 100)
        # Value: invert multiples → cheapness percentile
        for name, val in [("Cheap P/E", snap.pe), ("Cheap EV/E", snap.ev_ebitda)]:
            if val is not None and val > 0:
                peer_rows = compute_composite(sorted(df["Ticker"].tolist()), as_of, store)
                peers = []
                attr = {"Cheap P/E": "pe", "Cheap EV/E": "ev_ebitda"}[name]
                for pr in peer_rows:
                    v = getattr(pr, attr)
                    if v is not None and v > 0:
                        peers.append(v)
                pct = fc.percentile_rank(-val, [-p for p in peers])
                if pct is not None:
                    radar_labels.append(name)
                    radar_values.append(pct * 100)
        if radar_labels:
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

    if snap and snap.missing:
        with st.expander(f"⚠️ Missing inputs ({len(snap.missing)})"):
            st.write(snap.missing)


# ----------------------------------------------------------------------
# VIEW 3: METHODOLOGY
# ----------------------------------------------------------------------
else:
    st.title("📐 Methodology — how scores are computed")
    st.markdown("""
### Point-in-time correctness (the foundation)
Every fundamentals row carries an `as_of_date` — the **filing date**, when the
market could first know the number. Every factor computation and the as-of
slider filter on `as_of_date <= decision_date`. **Look-ahead bias is designed
out from row one.** This is the #1 source of fake alpha in retail quant, and
it's eliminated here.

### Quality factor
Composite percentile blend of:
- **ROIC** = NOPAT / average invested capital (debt + equity)
- **FCF margin** = (CFO − capex) / revenue
- **Gross margin** = gross profit / revenue
- **Piotroski F-Score** (up to 9 profitability, leverage, liquidity, and
  efficiency criteria)

### Value factor
Equal-weighted percentile rank of 5 multiples (cheapness = low multiple → high percentile):
- **P/E**, **EV/EBITDA**, **P/FCF**, **EV/Sales**, **P/B**

### TTM (trailing twelve months) — done right
The naive "sum last 4 quarters" **double-counts** because companies report both
an annual row AND its constituent quarters. We use the correct roll-forward:

`TTM = latest_annual + Σ(post_annual_quarters) − Σ(matching_prior_year_quarters)`

### Missing-input policy
Scores are published only when Quality has at least 2 of its 4 components and
Value has at least 2 of its 5 multiples. The QV composite requires both
sub-factors; it never turns a one-sided score into a complete-looking rank.
Coverage is shown in the ranking table and missing inputs remain attached to
each result.

### Regime-aware composite QV
The composite is `QV = w_quality × Quality + w_value × Value`. The weights are
selected from the release-aware macro regime available on the decision date:

| Regime | Quality | Value |
|--------|---------|-------|
| Goldilocks | 60% | 40% |
| Reflation | 45% | 55% |
| Stagflation | 65% | 35% |
| Deflationary | 55% | 45% |
| Risk-off | 70% | 30% |
| Unknown / incomplete macro | 60% | 40% |

These are explicit starting-policy hypotheses, not backtest-validated alpha.
An incomplete or non-PIT-ready macro snapshot uses the baseline and marks the
row so it cannot be mistaken for a validated regime tilt.

### Letter grades
| Grade | QV Score | Meaning |
|-------|----------|---------|
| A+ | ≥85 | Elite quality at reasonable price |
| A | 70–84 | Strong |
| B | 55–69 | Good |
| C | 40–54 | Average |
| D | <40 | Weak or expensive |

### Known limitations (honest)
- **Banks/financials** (JPM, BAC, V, MA) show `N/A` ROIC — banking business models
  don't fit the debt+equity invested-capital formula. Needs a financials-specific
  quality model (later phase).
- **Momentum and Low-Vol** factors not yet wired in (the "M" and the "L" of QVML).
- No transaction costs modeled yet (critical for backtesting, irrelevant for ranking).
""")
    st.info(
        "Full architecture decisions: see `ARCHITECTURE.md`. "
        "Strategy basis: see the original prompt analysis."
    )

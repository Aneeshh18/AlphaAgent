"""FRED macro data fetcher — our macro backbone.

Pulls the key macro series for the regime-classification engine. FRED is free;
needs an API key (settings.fred_api_key). We use the official `fredapi` client.

If the key is missing we degrade gracefully: fetch only the no-key US Treasury
yield-curve CSV. The macro table is filled with what we can get.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from structlog import get_logger

from aios.config import settings
from aios.ingest.http_client import get_http
from aios.storage.store import get_store

log = get_logger(__name__)

# The core macro series. group → series_id → {label, unit, freq}
MACRO_SERIES: dict[str, dict[str, str]] = {
    # --- Inflation ---
    "CPIAUCSL": {"label": "CPI All Urban", "unit": "index", "group": "inflation"},
    "PCEPI": {"label": "PCE Price Index", "unit": "index", "group": "inflation"},
    "PCEPILFE": {"label": "Core PCE", "unit": "index", "group": "inflation"},
    # --- Growth ---
    "GDP": {"label": "Real GDP", "unit": "bn_usd", "group": "growth"},
    "A191RL1Q225SBEA": {"label": "Real GDP Growth QoQ SAAR", "unit": "pct", "group": "growth"},
    # --- Employment ---
    "UNRATE": {"label": "Unemployment Rate", "unit": "pct", "group": "employment"},
    "PAYEMS": {"label": "Nonfarm Payrolls", "unit": "thousands", "group": "employment"},
    # --- Interest rates ---
    "FEDFUNDS": {"label": "Fed Funds Rate", "unit": "pct", "group": "rates"},
    "DGS2": {"label": "2Y Treasury", "unit": "pct", "group": "rates"},
    "DGS10": {"label": "10Y Treasury", "unit": "pct", "group": "rates"},
    "DGS30": {"label": "30Y Treasury", "unit": "pct", "group": "rates"},
    "T10Y2Y": {"label": "10Y-2Y Spread", "unit": "pct", "group": "rates"},
    "T10YIE": {"label": "10Y Breakeven Inflation", "unit": "pct", "group": "rates"},
    "DFII10": {"label": "10Y TIPS Real Yield", "unit": "pct", "group": "rates"},
    # --- Credit / liquidity ---
    "BAA10Y": {"label": "Moody's Baa - 10Y spread", "unit": "pct", "group": "credit"},
    "WALCL": {"label": "Fed Balance Sheet", "unit": "mn_usd", "group": "liquidity"},
    "M2SL": {"label": "M2 Money Supply", "unit": "bn_usd", "group": "liquidity"},
    # --- Sentiment / risk ---
    "VIXCLS": {"label": "VIX", "unit": "index", "group": "sentiment"},
    "DTWEXBGS": {"label": "Trade-Weighted Dollar", "unit": "index", "group": "sentiment"},
}


def fetch_series_fred(series_id: str) -> list[dict]:
    """Fetch one series via fredapi. Requires FRED_API_KEY."""
    if not settings.fred_api_key:
        log.warning("fred.no_api_key", series_id=series_id)
        return []
    try:
        from fredapi import Fred

        fred = Fred(api_key=settings.fred_api_key)
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("fredapi not installed") from e

    s = fred.get_series(series_id)
    meta = MACRO_SERIES.get(series_id, {})
    rows: list[dict] = []
    for ts, val in s.dropna().items():
        rows.append({
            "series_id": series_id,
            "date": ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10],
            "value": float(val),
            "unit": meta.get("unit", "na"),
            "source": "fred",
        })
    log.info("fred.series_fetched", series_id=series_id, rows=len(rows))
    return rows


def fetch_treasury_yield_curve() -> list[dict]:
    """Fallback: US Treasury daily yield curve (no key). Returns rows."""
    url = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/all/all?type=daily_rate_field&field_tdr_date_value=all&page&_format=csv"
    try:
        import io

        import pandas as pd

        csv_text = get_http().get_text(url)
        df = pd.read_csv(io.StringIO(csv_text))
    except Exception as e:  # pragma: no cover
        log.error("treasury.fetch_failed", error=str(e))
        return []

    rows: list[dict] = []
    # Columns: Date, 1 Mo, 2 Mo, ..., 30 Yr, etc.
    rate_cols = [c for c in df.columns if c not in ("Date", "date")]
    for _, r in df.iterrows():
        d = str(r.get("Date") or r.get("date"))
        if not d or d == "nan":
            continue
        for col in rate_cols:
            val = r.get(col)
            if pd.isna(val):
                continue
            # Map Treasury tenor names to our series_id convention (DGS<n>).
            series_id = _tenor_to_series(col)
            if not series_id:
                continue
            rows.append({
                "series_id": series_id,
                "date": d,
                "value": float(val),
                "unit": "pct",
                "source": "treasury",
            })
    log.info("treasury.yields_fetched", rows=len(rows))
    return rows


def _tenor_to_series(col: str) -> str | None:
    """Map Treasury CSV column ('2 Yr','10 Yr','30 Yr') to our series_id."""
    col = col.strip()
    mapping = {"2 Yr": "DGS2", "10 Yr": "DGS10", "30 Yr": "DGS30"}
    return mapping.get(col)


def ingest_macro(series_ids: list[str] | None = None) -> int:
    """Fetch + store macro series. Defaults to all MACRO_SERIES + Treasury fallback."""
    total = 0
    ids = series_ids or list(MACRO_SERIES.keys())
    for sid in ids:
        rows = fetch_series_fred(sid)
        if rows:
            total += get_store().upsert_macro(rows)

    # Always also pull the no-key Treasury curve as a fallback/cross-check for yields.
    try:
        trows = fetch_treasury_yield_curve()
        if trows:
            total += get_store().upsert_macro(trows)
    except Exception as e:  # pragma: no cover
        log.warning("treasury.fallback_skipped", error=str(e))

    log.info("macro.ingest_done", series=len(ids), rows=total)
    return total

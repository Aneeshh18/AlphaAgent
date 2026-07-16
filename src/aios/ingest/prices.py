"""Daily EOD price fetcher: yfinance (primary) + Stooq (fallback).

DESIGN
------
Both sources produce rows in the SAME schema so the storage layer never cares
which one supplied a given day. If yfinance returns nothing (throttled /
broken), we try Stooq. We log which source each row came from for auditability.

yfinance gives us adjusted prices + dividends + splits in one call.
Stooq gives raw OHLCV (unadjusted) — we store it raw and flag source='stooq';
adjustment reconciliation is a downstream concern, not an ingest one.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
from structlog import get_logger

from aios.ingest.http_client import get_http
from aios.storage.store import get_store

log = get_logger(__name__)

STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}&i=d"


# ----------------------------------------------------------------------
# yfinance (primary)
# ----------------------------------------------------------------------
def fetch_yfinance(ticker: str, start: str | None = None, end: str | None = None) -> list[dict]:
    """Fetch EOD prices via yfinance. Returns row dicts."""
    try:
        import yfinance as yf
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("yfinance not installed") from e

    period_start = start or "1995-01-01"
    period_end = end or date.today().isoformat()

    df: pd.DataFrame = yf.download(
        ticker,
        start=period_start,
        end=period_end,
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if df is None or df.empty:
        log.warning("prices.yfinance_empty", ticker=ticker)
        return []

    # yfinance may return MultiIndex columns when a single ticker is passed in
    # newer versions. Flatten to plain columns.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    rows: list[dict] = []
    for ts, r in df.iterrows():
        rows.append({
            "ticker": ticker.upper(),
            "date": ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10],
            "open": _f(r.get("Open")),
            "high": _f(r.get("High")),
            "low": _f(r.get("Low")),
            "close": _f(r.get("Close")),
            "adj_close": _f(r.get("Adj Close")),
            "volume": int(r["Volume"]) if pd.notna(r.get("Volume")) else None,
            "dividends": _f(r.get("Dividends")) or 0.0,
            "split_ratio": _f(r.get("Stock Splits")) or 1.0,
            "source": "yfinance",
        })
    log.info("prices.yfinance_fetched", ticker=ticker, rows=len(rows))
    return rows


# ----------------------------------------------------------------------
# Stooq (fallback)
# ----------------------------------------------------------------------
def _stooq_symbol(ticker: str) -> str:
    """Map a US ticker to Stooq's symbol convention (US tickers → lowercase)."""
    t = ticker.upper().replace(".", "-")
    return t.lower()


def fetch_stooq(ticker: str) -> list[dict]:
    """Fetch EOD prices via Stooq CSV download (no key). Returns row dicts."""
    sym = _stooq_symbol(ticker)
    url = STOOQ_URL.format(sym=sym)
    csv_text = get_http().get_text(url)
    if not csv_text or "No data" in csv_text:
        log.warning("prices.stooq_empty", ticker=ticker)
        return []

    import io

    df = pd.read_csv(io.StringIO(csv_text))
    if df.empty or "Close" not in df.columns:
        return []

    rows: list[dict] = []
    for _, r in df.iterrows():
        rows.append({
            "ticker": ticker.upper(),
            "date": str(r["Date"]),
            "open": _f(r.get("Open")),
            "high": _f(r.get("High")),
            "low": _f(r.get("Low")),
            "close": _f(r.get("Close")),
            "adj_close": None,  # Stooq is unadjusted; mark None
            "volume": int(r["Volume"]) if pd.notna(r.get("Volume")) else None,
            "dividends": 0.0,
            "split_ratio": 1.0,
            "source": "stooq",
        })
    log.info("prices.stooq_fetched", ticker=ticker, rows=len(rows))
    return rows


# ----------------------------------------------------------------------
# Unified fetch + store
# ----------------------------------------------------------------------
def fetch_prices(ticker: str, start: str | None = None, end: str | None = None) -> list[dict]:
    """Try yfinance first; fall back to Stooq on empty/failure."""
    try:
        rows = fetch_yfinance(ticker, start=start, end=end)
    except Exception as e:
        log.warning("prices.yfinance_failed", ticker=ticker, error=str(e))
        rows = []
    if rows:
        return rows
    log.warning("prices.fallback_to_stooq", ticker=ticker)
    try:
        return fetch_stooq(ticker)
    except Exception as e:  # pragma: no cover
        log.error("prices.stooq_failed", ticker=ticker, error=str(e))
        return []


def ingest_prices(ticker: str, start: str | None = None, end: str | None = None) -> int:
    """Fetch + store daily prices for one ticker. Returns rows stored."""
    rows = fetch_prices(ticker, start=start, end=end)
    if not rows:
        return 0
    n = get_store().upsert_prices(rows)
    log.info("prices.ingest_done", ticker=ticker, rows=n)
    return n


def _f(x: Any) -> float | None:
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

"""Daily EOD prices: yfinance, optional Tiingo, and Stooq fallback.

DESIGN
------
All sources produce rows in the SAME schema so the storage layer never cares
which one supplied a given day. If yfinance returns nothing (throttled or
broken), we try configured Tiingo and then Stooq. We retain the source on every
row for auditability.

yfinance and Tiingo give us adjusted prices + dividends + splits.
Stooq gives raw OHLCV (unadjusted) — we store it raw and flag source='stooq';
adjustment reconciliation is a downstream concern, not an ingest one.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode
from uuid import uuid4

import pandas as pd
from structlog import get_logger

from aios.config import settings
from aios.ingest.http_client import get_http
from aios.storage.store import Store, get_store

log = get_logger(__name__)

STOOQ_URL = "https://stooq.com/q/d/l/"
TIINGO_EOD_URL = "https://api.tiingo.com/tiingo/daily/{symbol}/prices"


# ----------------------------------------------------------------------
# yfinance (primary)
# ----------------------------------------------------------------------
def fetch_yfinance(ticker: str, start: str | None = None, end: str | None = None) -> list[dict]:
    """Fetch EOD prices and enough later split actions to restore raw basis.

    Yahoo retrospectively split-normalizes historical ``Close``. The bounded
    rows returned to callers therefore carry the cumulative later-split factor
    required to recover the price that was actually quoted on each date. The
    extra rows fetched only for that normalization scan are never returned.
    """
    try:
        import yfinance as yf
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("yfinance not installed") from e

    period_start = start or "1995-01-01"
    period_end = end or date.today().isoformat()
    requested_start = date.fromisoformat(period_start)
    requested_end = date.fromisoformat(period_end)
    normalization_end = max(requested_end, date.today() + timedelta(days=1))

    df: pd.DataFrame = yf.download(
        ticker,
        start=period_start,
        end=normalization_end.isoformat(),
        actions=True,
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
        row_date = ts.date() if hasattr(ts, "date") else date.fromisoformat(str(ts)[:10])
        rows.append(
            {
                "ticker": ticker.upper(),
                "date": row_date.isoformat(),
                "open": _f(r.get("Open")),
                "high": _f(r.get("High")),
                "low": _f(r.get("Low")),
                "close": _f(r.get("Close")),
                "adj_close": _f(r.get("Adj Close")),
                "volume": int(r["Volume"]) if pd.notna(r.get("Volume")) else None,
                "dividends": _f(r.get("Dividends")) or 0.0,
                "split_ratio": _f(r.get("Stock Splits")) or 1.0,
                "actions_complete": True,
                "close_split_adjusted": True,
                "split_normalization_factor": None,
                "split_normalization_through": date.today().isoformat(),
                "source": "yfinance",
            }
        )
    cumulative_later_splits = 1.0
    for row in reversed(rows):
        row["split_normalization_factor"] = cumulative_later_splits
        split_ratio = float(row["split_ratio"])
        if split_ratio <= 0:
            raise ValueError(f"invalid yfinance split ratio for {ticker}: {split_ratio}")
        cumulative_later_splits *= split_ratio

    # Never certify the local calendar's current date as EOD. Depending on the
    # server timezone and provider timing it may still be an open/partial U.S.
    # session. It becomes eligible on the next local day.
    completed_end = min(requested_end, date.today())
    bounded = [
        row
        for row in rows
        if requested_start <= date.fromisoformat(row["date"]) < completed_end
    ]
    log.info(
        "prices.yfinance_fetched",
        ticker=ticker,
        rows=len(bounded),
        split_scan_through=date.today().isoformat(),
    )
    return bounded


# ----------------------------------------------------------------------
# Stooq (fallback)
# ----------------------------------------------------------------------
def _stooq_symbol(ticker: str) -> str:
    """Map a US ticker to Stooq's symbol convention (US tickers → lowercase)."""
    t = ticker.upper().replace(".", "-")
    return t.lower()


def fetch_stooq(
    ticker: str,
    start: str | None = None,
    end: str | None = None,
) -> list[dict]:
    """Fetch EOD prices via Stooq CSV download (no key). Returns row dicts."""
    sym = _stooq_symbol(ticker)
    params = {"s": sym, "i": "d"}
    if start:
        params["d1"] = start.replace("-", "")
    if end:
        params["d2"] = end.replace("-", "")
    url = f"{STOOQ_URL}?{urlencode(params)}"
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
        rows.append(
            {
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
                "actions_complete": False,
                "close_split_adjusted": False,
                "split_normalization_factor": 1.0,
                "split_normalization_through": None,
                "source": "stooq",
            }
        )
    log.info("prices.stooq_fetched", ticker=ticker, rows=len(rows))
    return rows


# ----------------------------------------------------------------------
# Tiingo EOD (optional user-token provider)
# ----------------------------------------------------------------------
def fetch_tiingo(
    ticker: str,
    start: str | None = None,
    end: str | None = None,
) -> list[dict]:
    """Fetch explicit Tiingo EOD history without placing the token in the URL."""
    token = settings.tiingo_api_key.strip()
    if not token:
        raise ValueError("TIINGO_API_KEY is required for the Tiingo provider")
    params: dict[str, str] = {}
    if start:
        params["startDate"] = start
    if end:
        params["endDate"] = end
    query = f"?{urlencode(params)}" if params else ""
    url = TIINGO_EOD_URL.format(symbol=quote(ticker.upper(), safe="-")) + query
    payload = get_http().get_json(
        url,
        headers={"Authorization": f"Token {token}"},
    )
    if not isinstance(payload, list):
        raise ValueError("Tiingo EOD response is not a row array")

    end_date = date.fromisoformat(end) if end else None
    rows: list[dict] = []
    for raw in payload:
        if not isinstance(raw, dict) or not raw.get("date"):
            continue
        row_date = date.fromisoformat(str(raw["date"])[:10])
        if end_date is not None and row_date >= end_date:
            continue
        rows.append(
            {
                "ticker": ticker.upper(),
                "date": row_date.isoformat(),
                "open": _f(raw.get("open")),
                "high": _f(raw.get("high")),
                "low": _f(raw.get("low")),
                "close": _f(raw.get("close")),
                "adj_close": _f(raw.get("adjClose")),
                "volume": (int(raw["volume"]) if raw.get("volume") is not None else None),
                "dividends": _f(raw.get("divCash")) or 0.0,
                "split_ratio": _f(raw.get("splitFactor")) or 1.0,
                "actions_complete": True,
                "close_split_adjusted": False,
                "split_normalization_factor": 1.0,
                "split_normalization_through": None,
                "source": "tiingo",
            }
        )
    log.info("prices.tiingo_fetched", ticker=ticker, rows=len(rows))
    return rows


# ----------------------------------------------------------------------
# Unified fetch + store
# ----------------------------------------------------------------------
def fetch_prices(ticker: str, start: str | None = None, end: str | None = None) -> list[dict]:
    """Try yfinance, configured Tiingo, then Stooq on empty/failure."""
    try:
        rows = fetch_yfinance(ticker, start=start, end=end)
    except Exception as e:
        log.warning("prices.yfinance_failed", ticker=ticker, error=str(e))
        rows = []
    if rows:
        return rows
    if settings.tiingo_api_key.strip():
        log.warning("prices.fallback_to_tiingo", ticker=ticker)
        try:
            rows = fetch_tiingo(ticker, start=start, end=end)
        except Exception as e:
            log.warning("prices.tiingo_failed", ticker=ticker, error=str(e))
            rows = []
        if rows:
            return rows
    log.warning("prices.fallback_to_stooq", ticker=ticker)
    try:
        return fetch_stooq(ticker, start=start, end=end)
    except Exception as e:  # pragma: no cover
        log.error("prices.stooq_failed", ticker=ticker, error=str(e))
        return []


def fetch_provider_prices(
    provider: str,
    provider_symbol: str,
    start: str,
    end: str,
) -> list[dict]:
    """Fetch one explicitly reviewed provider mapping without cross-provider fallback."""
    provider = provider.lower()
    if provider == "yfinance":
        return fetch_yfinance(provider_symbol, start=start, end=end)
    if provider == "stooq":
        return fetch_stooq(provider_symbol, start=start, end=end)
    if provider == "tiingo":
        return fetch_tiingo(provider_symbol, start=start, end=end)
    raise ValueError(f"unsupported price provider {provider!r}")


def relabel_provider_price_rows(
    rows: list[dict],
    mapping: dict,
    ticker_assignments: list[dict],
) -> list[dict]:
    """Apply hard provider cutoffs and restore the market ticker for each date.

    A provider may expose all predecessor history under today's symbol. The
    returned label is therefore never trusted as the stored ticker. Every row
    must land inside both the reviewed provider window and exactly one dated
    security assignment, otherwise the import is refused.
    """
    if mapping.get("mapping_status") != "verified":
        raise ValueError("only verified provider mappings may produce prices")
    security_id = str(mapping.get("security_id") or "").strip()
    provider = str(mapping.get("provider") or "").strip().lower()
    provider_symbol = str(mapping.get("provider_symbol") or "").strip().upper()
    if not security_id or not provider or not provider_symbol:
        raise ValueError("provider mapping is missing an identity field")
    data_start = _as_date(mapping["data_start"])
    data_end = _as_date(mapping["data_end"]) if mapping.get("data_end") else None

    normalized_assignments = [
        {
            "ticker": str(assignment["ticker"]).upper(),
            "effective_start": _as_date(assignment["effective_start"]),
            "effective_end": (
                _as_date(assignment["effective_end"]) if assignment.get("effective_end") else None
            ),
        }
        for assignment in ticker_assignments
    ]
    output: list[dict] = []
    for row in rows:
        row_date = _as_date(row["date"])
        if row_date < data_start or (data_end is not None and row_date >= data_end):
            continue
        active_tickers = {
            assignment["ticker"]
            for assignment in normalized_assignments
            if assignment["effective_start"] <= row_date
            and (assignment["effective_end"] is None or assignment["effective_end"] > row_date)
        }
        if len(active_tickers) != 1:
            raise ValueError(
                "provider price cannot be mapped to exactly one market ticker: "
                f"{security_id}@{row_date}"
            )
        output.append(
            {
                **row,
                "ticker": active_tickers.pop(),
                "security_id": security_id,
                "provider_symbol": provider_symbol,
                "source": provider,
            }
        )
    return output


def ingest_security_prices(
    security_id: str,
    *,
    provider: str | None = None,
    start: str | None = None,
    end: str | None = None,
    store: Store | None = None,
) -> int:
    """Fetch prices through reviewed mappings and store dated market tickers."""
    db = store or get_store()
    started_at = datetime.now()
    run_id = str(uuid4())
    source = f"identity-price:{provider or 'reviewed'}"
    try:
        mappings = db.provider_symbol_mappings(
            security_id,
            provider=provider,
            start=start,
            end=end,
        )
        if not mappings:
            raise ValueError(f"no verified provider mapping for {security_id!r}")
        if provider is None:
            providers = {mapping["provider"] for mapping in mappings}
            selected = "yfinance" if "yfinance" in providers else sorted(providers)[0]
            mappings = [mapping for mapping in mappings if mapping["provider"] == selected]
            source = f"identity-price:{selected}"

        latest = db.latest_security_price_date(security_id)
        incremental_start = latest - timedelta(days=5) if latest is not None else None
        all_rows: list[dict] = []
        for mapping in mappings:
            mapping_start = _as_date(mapping["data_start"])
            mapping_end = _as_date(mapping["data_end"]) if mapping.get("data_end") else date.today()
            segment_start = max(
                value
                for value in (
                    mapping_start,
                    _as_date(start) if start else None,
                    incremental_start if start is None else None,
                )
                if value is not None
            )
            segment_end = min(
                value
                for value in (
                    mapping_end,
                    _as_date(end) if end else None,
                )
                if value is not None
            )
            if segment_start >= segment_end:
                continue
            raw_rows = fetch_provider_prices(
                mapping["provider"],
                mapping["provider_symbol"],
                segment_start.isoformat(),
                segment_end.isoformat(),
            )
            assignments = db.security_ticker_assignments(
                security_id,
                start=segment_start,
                end=segment_end,
            )
            all_rows.extend(relabel_provider_price_rows(raw_rows, mapping, assignments))
        inserted = db.upsert_prices(all_rows) if all_rows else 0
        db.record_ingest(
            run_id=run_id,
            source=source,
            table_name="prices",
            rows_inserted=inserted,
            started_at=started_at,
            status="success" if inserted else "warning",
            error=None if inserted else "provider returned no rows in verified intervals",
        )
        return inserted
    except Exception as exc:
        db.record_ingest(
            run_id=run_id,
            source=source,
            table_name="prices",
            started_at=started_at,
            status="failed",
            error=str(exc),
        )
        raise


def ingest_prices(
    ticker: str,
    start: str | None = None,
    end: str | None = None,
    *,
    store: Store | None = None,
) -> int:
    """Fetch + store daily prices for one ticker. Returns rows stored."""
    store = store or get_store()
    started_at = datetime.now()
    run_id = str(uuid4())
    try:
        fetch_start = start
        if fetch_start is None:
            latest = store.latest_price_date(ticker)
            if latest is not None:
                # Re-fetch a short overlap so recent corrections, dividends,
                # and exchange-date revisions can replace existing rows.
                fetch_start = (latest - timedelta(days=5)).isoformat()
        rows = fetch_prices(ticker, start=fetch_start, end=end)
        n = store.upsert_prices(rows) if rows else 0
        store.record_ingest(
            run_id=run_id,
            source="yfinance_or_stooq",
            table_name="prices",
            rows_inserted=n,
            started_at=started_at,
        )
        log.info("prices.ingest_done", ticker=ticker, rows=n, run_id=run_id)
        return n
    except Exception as e:
        store.record_ingest(
            run_id=run_id,
            source="yfinance_or_stooq",
            table_name="prices",
            started_at=started_at,
            status="failed",
            error=str(e),
        )
        raise


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


def _as_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))

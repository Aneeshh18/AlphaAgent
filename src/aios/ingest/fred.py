"""FRED macro data fetcher — our macro backbone.

Pulls the key macro series for the regime-classification engine. FRED is free;
needs an API key (settings.fred_api_key). We use the official `fredapi` client.

If the key is missing we degrade gracefully: fetch only the no-key US Treasury
yield-curve CSV. The macro table is filled with what we can get.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from http.client import IncompleteRead, RemoteDisconnected
from typing import Any
from urllib.error import URLError
from uuid import uuid4

from structlog import get_logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from aios.config import settings
from aios.ingest.http_client import get_http
from aios.storage.store import Store, get_store

log = get_logger(__name__)

# FRED limits XML/JSON observation requests to 2,000 vintage dates. Keep a
# margin below that hard cap so this remains safe if endpoint accounting changes.
FRED_VINTAGE_CHUNK_SIZE = 1_900
FRED_INCREMENTAL_OVERLAP_DAYS = 31
TREASURY_YIELD_SERIES = {"DGS2", "DGS10", "DGS30"}
FRED_TRANSIENT_ERRORS = (
    IncompleteRead,
    RemoteDisconnected,
    URLError,
    TimeoutError,
    ConnectionError,
)

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


class MacroIngestError(RuntimeError):
    """Raised after all macro sources are attempted and one or more failed."""


def fetch_series_fred(
    series_id: str,
    realtime_start: date | str | None = None,
    realtime_end: date | str | None = None,
) -> list[dict]:
    """Fetch every available FRED vintage for one series.

    ``date`` is the economic observation date; ``release_date`` is FRED's
    ``realtime_start`` — the date that vintage became public. Keeping both is
    required to answer historical questions without revision look-ahead.
    """
    if not settings.fred_api_key:
        log.warning("fred.no_api_key", series_id=series_id)
        return []
    try:
        from fredapi import Fred

        fred = Fred(api_key=settings.fred_api_key)
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("fredapi not installed") from e

    vintage_dates = _bounded_vintage_dates(
        _fetch_vintage_dates(fred, series_id),
        realtime_start=realtime_start,
        realtime_end=realtime_end,
    )
    if not vintage_dates:
        log.info("fred.series_no_vintages", series_id=series_id)
        return []

    meta = MACRO_SERIES.get(series_id, {})
    rows_by_key: dict[tuple[str, str], dict] = {}
    chunks = list(_chunks(vintage_dates, FRED_VINTAGE_CHUNK_SIZE))
    for chunk_number, vintage_chunk in enumerate(chunks, start=1):
        window_start = vintage_chunk[0].isoformat()
        window_end = vintage_chunk[-1].isoformat()
        observations = _fetch_release_window(fred, series_id, window_start, window_end)
        for _, observation in observations.dropna(subset=["value"]).iterrows():
            observation_date = _date_string(observation["date"])
            release_date = _date_string(observation["realtime_start"])
            rows_by_key[(observation_date, release_date)] = {
                "series_id": series_id,
                "date": observation_date,
                "release_date": release_date,
                "value": float(observation["value"]),
                "unit": meta.get("unit", "na"),
                "source": "fred",
            }
        log.info(
            "fred.series_chunk_fetched",
            series_id=series_id,
            chunk=chunk_number,
            chunks=len(chunks),
            realtime_start=window_start,
            realtime_end=window_end,
        )

    rows = sorted(
        rows_by_key.values(),
        key=lambda row: (row["date"], row["release_date"]),
    )
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
            rows.append(
                {
                    "series_id": series_id,
                    "date": d,
                    # Treasury's historical CSV does not expose a separate
                    # release timestamp. Its daily rate is treated as
                    # available at the end of the observation date; callers
                    # must make decisions after that close (or next session).
                    "release_date": d,
                    "value": float(val),
                    "unit": "pct",
                    "source": "treasury",
                }
            )
    log.info("treasury.yields_fetched", rows=len(rows))
    return rows


def _tenor_to_series(col: str) -> str | None:
    """Map Treasury CSV column ('2 Yr','10 Yr','30 Yr') to our series_id."""
    col = col.strip()
    mapping = {"2 Yr": "DGS2", "10 Yr": "DGS10", "30 Yr": "DGS30"}
    return mapping.get(col)


def _date_string(value: object) -> str:
    """Convert pandas/FRED date-like values to an ISO calendar date."""
    if hasattr(value, "date"):
        value = value.date()
    return str(value)[:10]


@retry(
    retry=retry_if_exception_type(FRED_TRANSIENT_ERRORS),
    stop=stop_after_attempt(4),
    wait=wait_exponential_jitter(initial=0.5, max=8.0),
    reraise=True,
)
def _fetch_vintage_dates(fred: Any, series_id: str) -> list[Any]:
    """Fetch a series' release dates with bounded transient retries."""
    return fred.get_series_vintage_dates(series_id)


@retry(
    retry=retry_if_exception_type(FRED_TRANSIENT_ERRORS),
    stop=stop_after_attempt(4),
    wait=wait_exponential_jitter(initial=0.5, max=8.0),
    reraise=True,
)
def _fetch_release_window(
    fred: Any,
    series_id: str,
    realtime_start: str,
    realtime_end: str,
) -> Any:
    """Fetch one legal-size vintage window with bounded transient retries."""
    return fred.get_series_all_releases(
        series_id,
        realtime_start=realtime_start,
        realtime_end=realtime_end,
    )


def _as_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _bounded_vintage_dates(
    vintage_dates: list[Any],
    realtime_start: date | str | None,
    realtime_end: date | str | None,
) -> list[date]:
    """Normalize, deduplicate, and bound FRED vintage dates."""
    start = _as_date(realtime_start) if realtime_start else None
    end = _as_date(realtime_end) if realtime_end else date.today()
    end = min(end, date.today())
    if start and start > end:
        raise ValueError(f"realtime_start {start} is after realtime_end {end}")
    return sorted(
        {
            vintage_date
            for raw_date in vintage_dates
            if (vintage_date := _as_date(raw_date)) <= end
            and (start is None or vintage_date >= start)
        }
    )


def _chunks(values: list[date], size: int) -> list[list[date]]:
    """Split values into non-overlapping chunks of at most `size`."""
    if size < 1:
        raise ValueError("chunk size must be positive")
    return [values[offset : offset + size] for offset in range(0, len(values), size)]


def ingest_macro(
    series_ids: list[str] | None = None,
    *,
    store: Store | None = None,
) -> int:
    """Fetch + store macro series. Defaults to all MACRO_SERIES + Treasury fallback."""
    store = store or get_store()
    started_at = datetime.now()
    run_id = str(uuid4())
    total = 0
    failures: dict[str, str] = {}
    ids = series_ids or list(MACRO_SERIES.keys())
    for sid in ids:
        try:
            latest_release = store.latest_macro_release_date(sid, source="fred")
            realtime_start = (
                latest_release - timedelta(days=FRED_INCREMENTAL_OVERLAP_DAYS)
                if latest_release
                else None
            )
            rows = fetch_series_fred(sid, realtime_start=realtime_start)
            if rows:
                total += store.upsert_macro(rows)
        except Exception as exc:
            failures[sid] = str(exc)
            log.error("fred.series_failed", series_id=sid, error=str(exc))

    # Treasury is a true fallback: avoid a redundant automated request when
    # FRED is configured and all requested yield series succeeded.
    needs_treasury = not settings.fred_api_key or bool(TREASURY_YIELD_SERIES & failures.keys())
    if needs_treasury:
        try:
            trows = fetch_treasury_yield_curve()
            if trows:
                total += store.upsert_macro(trows)
            else:
                failures.setdefault("treasury", "no rows returned")
        except Exception as exc:  # pragma: no cover
            failures["treasury"] = str(exc)
            log.error("treasury.fallback_failed", error=str(exc))
    else:
        log.info("treasury.fallback_not_needed")

    error = "; ".join(f"{source}: {message}" for source, message in failures.items()) or None
    status = "failed" if failures else "success"
    store.record_ingest(
        run_id=run_id,
        source="fred_or_treasury",
        table_name="macro",
        rows_inserted=total,
        started_at=started_at,
        status=status,
        error=error,
    )
    if failures:
        raise MacroIngestError(
            f"Macro ingest completed with {len(failures)} failed source(s): {error}"
        )
    log.info("macro.ingest_done", series=len(ids), rows=total, run_id=run_id)
    return total

"""FRED macro data fetcher — our macro backbone.

Pulls the key macro series for the regime-classification engine. FRED is free;
needs an API key (settings.fred_api_key). We use the official `fredapi` client.

If the key is missing we degrade gracefully: fetch only the no-key US Treasury
yield-curve CSV. The macro table is filled with what we can get.
"""

from __future__ import annotations

import csv
import io
import json
import math
from datetime import UTC, date, datetime, timedelta
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from uuid import uuid4

from structlog import get_logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from aios.config import secret_value, settings
from aios.ingest.http_client import RawSnapshotContext, get_http
from aios.market_calendar import latest_completed_us_equity_session
from aios.raw_snapshots import (
    attach_parsed_rows_evidence,
    canonical_request_fingerprint,
    capture_raw_snapshot,
)
from aios.storage.store import Store, get_store

log = get_logger(__name__)

# FRED limits XML/JSON observation requests to 2,000 vintage dates. Keep a
# margin below that hard cap so this remains safe if endpoint accounting changes.
FRED_VINTAGE_CHUNK_SIZE = 1_900
FRED_INCREMENTAL_OVERLAP_DAYS = 31
FRED_EXPORT_SCHEMA_VERSION = 1
FRED_PARSER_VERSION = "fred-normalized-v1"
TREASURY_CAPTURE_PARSER_VERSION = "treasury-yield-csv-capture-v1"
TREASURY_PARSER_VERSION = "treasury-yield-csv-v2"
TREASURY_DATASET = "daily-yield-curve"
TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all"
)
TREASURY_TENORS = {
    "2 Yr": "DGS2",
    "10 Yr": "DGS10",
    "30 Yr": "DGS30",
}
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
    *,
    store: Store | None = None,
    ingest_run_id: str | None = None,
    project_root: Path | None = None,
) -> list[dict]:
    """Fetch every available FRED vintage for one series.

    ``date`` is the economic observation date; ``release_date`` is FRED's
    ``realtime_start`` — the date that vintage became public. Keeping both is
    required to answer historical questions without revision look-ahead.
    """
    api_key = secret_value(settings.fred_api_key)
    if not api_key:
        log.warning("fred.no_api_key", series_id=series_id)
        return []
    requested_at = datetime.now(UTC)
    try:
        from fredapi import Fred

        fred = Fred(api_key=api_key)
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("fredapi not installed") from e

    vintage_dates = _bounded_vintage_dates(
        _fetch_vintage_dates(fred, series_id),
        realtime_start=realtime_start,
        realtime_end=realtime_end,
    )

    meta = MACRO_SERIES.get(series_id, {})
    provider_rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    chunks = list(_chunks(vintage_dates, FRED_VINTAGE_CHUNK_SIZE))
    for chunk_number, vintage_chunk in enumerate(chunks, start=1):
        window_start = vintage_chunk[0].isoformat()
        window_end = vintage_chunk[-1].isoformat()
        observations = _fetch_release_window(fred, series_id, window_start, window_end)
        for _, observation in observations.dropna(subset=["value"]).iterrows():
            observation_date = _date_string(observation["date"])
            release_date = _date_string(observation["realtime_start"])
            provider_rows_by_key[(observation_date, release_date)] = {
                "date": observation_date,
                "realtime_start": release_date,
                "value": float(observation["value"]),
            }
        log.info(
            "fred.series_chunk_fetched",
            series_id=series_id,
            chunk=chunk_number,
            chunks=len(chunks),
            realtime_start=window_start,
            realtime_end=window_end,
        )

    export = {
        "export_schema_version": FRED_EXPORT_SCHEMA_VERSION,
        "provider": "fred",
        "series_id": series_id,
        "requested_realtime_start": (
            _as_date(realtime_start).isoformat() if realtime_start is not None else None
        ),
        "requested_realtime_end": (
            _as_date(realtime_end).isoformat() if realtime_end is not None else None
        ),
        "selected_vintage_dates": [value.isoformat() for value in vintage_dates],
        "unit": meta.get("unit", "na"),
        "provider_rows": sorted(
            provider_rows_by_key.values(),
            key=lambda row: (row["date"], row["realtime_start"]),
        ),
    }
    payload = json.dumps(
        export,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    rows = parse_fred_normalized_export(payload)
    if store is not None:
        capture_raw_snapshot(
            payload,
            provider="fred",
            dataset="series-vintages",
            artifact_kind="normalized_provider_export",
            requested_at=requested_at,
            received_at=datetime.now(UTC),
            request_fingerprint=canonical_request_fingerprint(
                {
                    "adapter": "fredapi",
                    "series_id": series_id,
                    "realtime_start": export["requested_realtime_start"],
                    "realtime_end": export["requested_realtime_end"],
                    "vintage_chunk_size": FRED_VINTAGE_CHUNK_SIZE,
                }
            ),
            adapter_name="aios-fred-library",
            adapter_version="1",
            parser_version=FRED_PARSER_VERSION,
            content_type="application/vnd.aios.fred-normalized+json",
            parsed_rows=rows,
            ingest_run_id=ingest_run_id,
            role=f"macro:{series_id}",
            store=store,
            project_root=project_root,
        )
    if not vintage_dates:
        log.info("fred.series_no_vintages", series_id=series_id)
        return []
    log.info("fred.series_fetched", series_id=series_id, rows=len(rows))
    return rows


def parse_fred_normalized_export(payload: bytes) -> list[dict[str, Any]]:
    """Replay one canonical fredapi export into release-dated macro rows."""
    try:
        export = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("FRED normalized export is not valid JSON") from exc
    if not isinstance(export, dict):
        raise ValueError("FRED normalized export must be an object")
    if (
        export.get("export_schema_version") != FRED_EXPORT_SCHEMA_VERSION
        or export.get("provider") != "fred"
    ):
        raise ValueError("unsupported FRED normalized export")

    series_id = str(export.get("series_id") or "").strip().upper()
    unit = str(export.get("unit") or "").strip()
    provider_rows = export.get("provider_rows")
    if not series_id or not unit:
        raise ValueError("FRED normalized export lacks series metadata")
    if not isinstance(provider_rows, list):
        raise ValueError("FRED normalized export has no provider row array")

    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for provider_row in provider_rows:
        if not isinstance(provider_row, dict):
            raise ValueError("FRED normalized export contains a non-object row")
        observation_date = date.fromisoformat(str(provider_row["date"])).isoformat()
        release_date = date.fromisoformat(
            str(provider_row["realtime_start"])
        ).isoformat()
        value = float(provider_row["value"])
        key = (observation_date, release_date)
        if key in rows_by_key:
            raise ValueError(
                "FRED normalized export contains a duplicate vintage: "
                f"{series_id}@{observation_date}/{release_date}"
            )
        rows_by_key[key] = {
            "series_id": series_id,
            "date": observation_date,
            "release_date": release_date,
            "value": value,
            "unit": unit,
            "source": "fred",
        }
    return sorted(
        rows_by_key.values(),
        key=lambda row: (row["date"], row["release_date"]),
    )


def fetch_treasury_yield_curve(
    *,
    as_of: date | str | None = None,
    store: Store | None = None,
    ingest_run_id: str | None = None,
    project_root: Path | None = None,
) -> list[dict]:
    """Fetch one bounded official Treasury yield-curve year.

    The source file may contain the current observation before the U.S. equity
    session is complete. Returned rows are therefore capped at the latest
    completed New York session (and an earlier explicit ``as_of`` when given).
    The exact CSV is captured before parsing; only a successful strict parse is
    promoted to replayable evidence.
    """
    latest_completed = latest_completed_us_equity_session()
    requested_as_of = _as_date(as_of) if as_of is not None else latest_completed
    target = min(requested_as_of, latest_completed)
    if target.year < 1990:
        raise ValueError("Treasury par-yield data is unavailable before 1990")
    query = urlencode(
        {
            "_format": "csv",
            "field_tdr_date_value": str(target.year),
            "page": "",
            "type": "daily_treasury_yield_curve",
        }
    )
    url = f"{TREASURY_URL.format(year=target.year)}?{query}"
    role = f"treasury-yields:{target.year}"
    try:
        http = get_http()
        if store is None:
            payload = http.get_bytes(url)
        else:
            payload = http.get_bytes(
                url,
                raw_snapshot=RawSnapshotContext(
                    provider="us-treasury",
                    dataset=TREASURY_DATASET,
                    store=store,
                    ingest_run_id=ingest_run_id,
                    role=role,
                    adapter_name="aios-treasury-http",
                    adapter_version="2",
                    parser_version=TREASURY_CAPTURE_PARSER_VERSION,
                    project_root=project_root,
                ),
            )
        provider_rows = parse_treasury_yield_curve_csv(payload)
        if store is not None and ingest_run_id is not None:
            attach_parsed_rows_evidence(
                store=store,
                ingest_run_id=ingest_run_id,
                role=role,
                capture_parser_version=TREASURY_CAPTURE_PARSER_VERSION,
                parser_version=TREASURY_PARSER_VERSION,
                parsed_rows=provider_rows,
            )
    except Exception as e:  # pragma: no cover
        log.error("treasury.fetch_failed", error=str(e))
        return []

    wrong_years = {
        date.fromisoformat(str(row["date"])).year
        for row in provider_rows
        if date.fromisoformat(str(row["date"])).year != target.year
    }
    if wrong_years:
        raise ValueError(
            "Treasury response escaped its requested year: "
            + ", ".join(str(year) for year in sorted(wrong_years))
        )
    rows = [
        row
        for row in provider_rows
        if date.fromisoformat(str(row["date"])) <= target
    ]
    log.info(
        "treasury.yields_fetched",
        rows=len(rows),
        response_rows=len(provider_rows),
        through=target.isoformat(),
    )
    return rows


def parse_treasury_yield_curve_csv(payload: bytes) -> list[dict[str, Any]]:
    """Replay an official Treasury CSV into canonical daily tenor rows."""
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Treasury yield CSV is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise ValueError("Treasury yield CSV has no header")
    field_map = {
        str(field).strip().lstrip("\ufeff"): field
        for field in reader.fieldnames
        if field is not None
    }
    required = {"Date", *TREASURY_TENORS}
    missing = sorted(required - field_map.keys())
    if missing:
        raise ValueError(
            "Treasury yield CSV is missing required columns: " + ", ".join(missing)
        )

    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    tenor_rank = {series_id: rank for rank, series_id in enumerate(TREASURY_TENORS.values())}
    for row_number, raw in enumerate(reader, start=2):
        if None in raw:
            raise ValueError(f"Treasury yield CSV row {row_number} has extra columns")
        observation_date = _parse_treasury_date(raw[field_map["Date"]], row_number)
        for column, series_id in TREASURY_TENORS.items():
            raw_value = str(raw[field_map[column]] or "").strip()
            if not raw_value or raw_value.casefold() in {"n/a", "na", "null"}:
                continue
            try:
                value = float(raw_value)
            except ValueError as exc:
                raise ValueError(
                    f"Treasury yield CSV row {row_number} has invalid {column}"
                ) from exc
            if not math.isfinite(value) or value < 0.0 or value > 100.0:
                raise ValueError(
                    f"Treasury yield CSV row {row_number} has out-of-range {column}"
                )
            key = (series_id, observation_date.isoformat())
            if key in rows_by_key:
                raise ValueError(
                    "Treasury yield CSV contains a duplicate observation: "
                    f"{series_id}@{observation_date}"
                )
            rows_by_key[key] = {
                "series_id": series_id,
                "date": observation_date.isoformat(),
                # Treasury publishes these end-of-day CMT observations without
                # a distinct revision timestamp. Treat the observation date as
                # the conservative public date and never use it before close.
                "release_date": observation_date.isoformat(),
                "value": value,
                "unit": "pct",
                "source": "treasury",
            }
    if not rows_by_key:
        raise ValueError("Treasury yield CSV contains no supported yield observations")
    return sorted(
        rows_by_key.values(),
        key=lambda row: (row["date"], tenor_rank[str(row["series_id"])]),
    )


def _parse_treasury_date(value: object, row_number: int) -> date:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        try:
            return datetime.strptime(text, "%m/%d/%Y").date()
        except ValueError as exc:
            raise ValueError(
                f"Treasury yield CSV row {row_number} has invalid Date"
            ) from exc


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
    """Fetch macro series plus one independent official Treasury cross-check.

    Treasury is required when FRED is unavailable for a covered yield series.
    When FRED is healthy it remains a non-blocking cross-check: its exact
    response and values are retained, but a Treasury outage does not turn the
    healthy primary provider into a failed daily run.
    """
    store = store or get_store()
    started_at = datetime.now()
    run_id = str(uuid4())
    total = 0
    failures: dict[str, str] = {}
    advisories: dict[str, str] = {}
    ids = series_ids or list(MACRO_SERIES.keys())
    for sid in ids:
        try:
            latest_release = store.latest_macro_release_date(sid, source="fred")
            realtime_start = (
                latest_release - timedelta(days=FRED_INCREMENTAL_OVERLAP_DAYS)
                if latest_release
                else None
            )
            rows = fetch_series_fred(
                sid,
                realtime_start=realtime_start,
                store=store,
                ingest_run_id=run_id,
            )
            if rows:
                total += store.upsert_macro(rows)
        except Exception as exc:
            failures[sid] = str(exc)
            log.error("fred.series_failed", series_id=sid, error=str(exc))

    treasury_required = not secret_value(settings.fred_api_key) or bool(
        TREASURY_YIELD_SERIES & failures.keys()
    )
    try:
        trows = fetch_treasury_yield_curve(
            store=store,
            ingest_run_id=run_id,
        )
        if trows:
            total += store.upsert_macro(trows)
            treasury_series = {str(row["series_id"]) for row in trows}
            for series_id in sorted(TREASURY_YIELD_SERIES & failures.keys()):
                if series_id in treasury_series:
                    advisories[f"fred:{series_id}"] = (
                        f"{failures.pop(series_id)}; official Treasury fallback succeeded"
                    )
        else:
            target = failures if treasury_required else advisories
            target.setdefault("treasury", "no rows returned")
    except Exception as exc:  # pragma: no cover
        target = failures if treasury_required else advisories
        target["treasury"] = str(exc)
        log.error("treasury.crosscheck_failed", required=treasury_required, error=str(exc))

    details = [
        *(f"{source}: {message}" for source, message in failures.items()),
        *(f"advisory {source}: {message}" for source, message in advisories.items()),
    ]
    error = "; ".join(details) or None
    status = "failed" if failures else ("warning" if advisories else "success")
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
    log.info(
        "macro.ingest_done",
        series=len(ids),
        rows=total,
        advisories=len(advisories),
        run_id=run_id,
    )
    return total

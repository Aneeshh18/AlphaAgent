"""Conservative reference-identity batches for unchanged securities.

This module automates only the low-ambiguity case: one market ticker maps to
one security for the entire certified window, the SEC's current ticker file and
submissions record agree on the CIK/ticker, and one explicitly selected price
provider returns a complete bounded history. Exact market-dot/SEC-hyphen share
class notation is normalized, and SEC-named older submissions shards are
followed when the current file does not bracket the window. Corporate actions,
retired tickers, ticker reuse, missing provider history, and ambiguous CIKs are
rejected for manual evidence work.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from contextlib import nullcontext
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aios.ingest.edgar import (
    SUBMISSIONS_FILE_URL,
    SUBMISSIONS_URL,
    TICKER_MAP_URL,
    CompanyFactsArchive,
    fetch_submission_file,
    fetch_submissions,
)
from aios.ingest.http_client import get_http
from aios.ingest.prices import (
    fetch_provider_prices,
    ingest_security_prices,
    relabel_provider_price_rows,
)
from aios.ingest.reference_identity import (
    ingest_reference_identity_csvs,
    load_issuer_cik_csv,
    load_provider_symbol_csv,
    load_security_issuer_csv,
)
from aios.market_calendar import us_equity_sessions
from aios.storage.store import Store, get_store

TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.-]*$")
BATCH_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
BATCH_WINDOW_FIELDS = ("ticker", "start", "end")

ISSUER_FIELDS = (
    "issuer_id",
    "canonical_name",
    "canonical_ticker",
    "cik",
    "effective_start",
    "effective_end",
    "verified_date",
    "source",
)
OWNER_FIELDS = (
    "security_id",
    "issuer_id",
    "effective_start",
    "effective_end",
    "verified_date",
    "source",
)
PROVIDER_FIELDS = (
    "provider",
    "provider_symbol",
    "security_id",
    "data_start",
    "data_end",
    "mapping_status",
    "verified_date",
    "source",
)
REVIEW_FIELDS = (
    "ticker",
    "security_id",
    "issuer_id",
    "cik",
    "sec_name",
    "sec_ticker",
    "assignment_start",
    "assignment_end",
    "provider",
    "provider_symbol",
    "provider_rows",
    "provider_first_date",
    "provider_last_date",
    "sec_payload_sha256",
    "sec_history_sources",
    "price_payload_sha256",
    "review_status",
    "reason",
    "verified_date",
    "sec_source",
    "provider_source",
)


def load_batch_tickers(path: str | Path) -> list[str]:
    """Load one unique ticker per line, preserving file order."""
    output: list[str] = []
    seen: set[str] = set()
    for line_number, raw in enumerate(Path(path).read_text().splitlines(), start=1):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        ticker = value.upper()
        if not TICKER_PATTERN.fullmatch(ticker):
            raise ValueError(f"ticker file line {line_number}: invalid ticker {value!r}")
        if ticker in seen:
            raise ValueError(f"ticker file line {line_number}: duplicate ticker {ticker}")
        seen.add(ticker)
        output.append(ticker)
    if not output:
        raise ValueError("ticker file has no tickers")
    return output


def load_batch_windows(path: str | Path) -> list[dict[str, Any]]:
    """Load strict per-ticker half-open windows from ``ticker,start,end`` CSV."""
    parsed_rows: list[tuple[int, dict[str, str]]] = []
    header: tuple[str, ...] | None = None
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        for line_number, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                values = next(csv.reader([raw], strict=True))
            except csv.Error as exc:
                raise ValueError(
                    f"batch window CSV line {line_number}: invalid CSV: {exc}"
                ) from exc
            if header is None:
                header = tuple(value.strip().lower() for value in values)
                if header != BATCH_WINDOW_FIELDS:
                    raise ValueError("batch window CSV header must be exactly ticker,start,end")
                continue
            if len(values) != len(BATCH_WINDOW_FIELDS):
                raise ValueError(f"batch window CSV line {line_number}: expected 3 fields")
            parsed_rows.append(
                (
                    line_number,
                    dict(zip(BATCH_WINDOW_FIELDS, values, strict=True)),
                )
            )
    if header is None:
        raise ValueError("batch window CSV has no header")
    return _normalize_batch_windows(parsed_rows, label="batch window CSV")


def plan_missing_reference_windows(
    *,
    universe_id: str,
    as_of: date | str,
    start_floor: date | str,
    end: date | str,
    provider: str = "yfinance",
    store: Store | None = None,
) -> list[dict[str, date | str]]:
    """Plan current-member windows still missing complete reviewed identity.

    The planner is intentionally resumable: a security is omitted only when
    its owner/CIK chain and verified provider mapping all cover ``as_of``.
    It does not guess through a partial overlapping interval; such a state is
    rejected before any network certification starts.
    """
    db = store or get_store()
    decision_date = _as_date(as_of, "as_of")
    floor = _as_date(start_floor, "start_floor")
    window_end = _as_date(end, "end")
    provider = provider.strip().lower()
    if not universe_id.strip():
        raise ValueError("universe_id is required")
    if not floor <= decision_date < window_end:
        raise ValueError("reference plan requires start_floor <= as_of < end")
    if provider not in {"yfinance", "tiingo"}:
        raise ValueError("reference plan supports yfinance or tiingo only")

    members = db.query(
        """
        SELECT membership.ticker, membership.security_id,
               identity.effective_start AS assignment_start,
               identity.effective_end AS assignment_end,
               EXISTS (
                   SELECT 1
                   FROM security_issuer_assignments AS owner
                   JOIN issuer_cik_history AS cik
                     ON cik.issuer_id = owner.issuer_id
                    AND cik.effective_start <= CAST(? AS DATE)
                    AND (cik.effective_end IS NULL OR cik.effective_end > CAST(? AS DATE))
                   WHERE owner.security_id = membership.security_id
                     AND owner.effective_start <= CAST(? AS DATE)
                     AND (owner.effective_end IS NULL OR owner.effective_end > CAST(? AS DATE))
               ) AS has_owner_and_cik,
               EXISTS (
                   SELECT 1
                   FROM provider_symbol_history AS mapping
                   WHERE mapping.security_id = membership.security_id
                     AND mapping.provider = ?
                     AND mapping.mapping_status = 'verified'
                     AND mapping.data_start <= CAST(? AS DATE)
                     AND (mapping.data_end IS NULL OR mapping.data_end > CAST(? AS DATE))
               ) AS has_provider
        FROM universe_membership AS membership
        LEFT JOIN security_identity_assignments AS identity
          ON identity.universe_id = membership.universe_id
         AND identity.ticker = membership.ticker
         AND identity.effective_start = membership.effective_start
         AND identity.effective_end IS NOT DISTINCT FROM membership.effective_end
         AND identity.security_id = membership.security_id
        WHERE membership.universe_id = ?
          AND membership.effective_start <= CAST(? AS DATE)
          AND (
              membership.effective_end IS NULL
              OR membership.effective_end > CAST(? AS DATE)
          )
        ORDER BY membership.ticker
        """,
        (
            decision_date,
            decision_date,
            decision_date,
            decision_date,
            provider,
            decision_date,
            decision_date,
            universe_id,
            decision_date,
            decision_date,
        ),
    )
    if not members:
        raise ValueError(f"universe {universe_id!r} has no active members on {decision_date}")

    windows: list[dict[str, date | str]] = []
    seen: set[str] = set()
    for member in members:
        ticker = str(member["ticker"])
        if ticker in seen:
            raise ValueError(f"active universe has duplicate ticker {ticker!r}")
        seen.add(ticker)
        if not member.get("security_id") or member.get("assignment_start") is None:
            raise ValueError(f"active member {ticker} has no exact security identity")
        if member["has_owner_and_cik"] and member["has_provider"]:
            continue
        assignment_start = _as_date(member["assignment_start"], "assignment_start")
        assignment_end = (
            _as_date(member["assignment_end"], "assignment_end")
            if member.get("assignment_end") is not None
            else None
        )
        start = max(floor, assignment_start)
        if assignment_end is not None and assignment_end < window_end:
            raise ValueError(f"active assignment for {ticker} ends before requested window end")
        if start >= window_end:
            raise ValueError(f"planned window for {ticker} is empty")

        overlaps = db.query(
            """
            SELECT 'owner' AS kind
            FROM security_issuer_assignments
            WHERE security_id = ?
              AND effective_start < CAST(? AS DATE)
              AND COALESCE(effective_end, DATE '9999-12-31') > CAST(? AS DATE)
            UNION ALL
            SELECT 'provider' AS kind
            FROM provider_symbol_history
            WHERE security_id = ?
              AND provider = ?
              AND data_start < CAST(? AS DATE)
              AND COALESCE(data_end, DATE '9999-12-31') > CAST(? AS DATE)
            """,
            (
                member["security_id"],
                window_end,
                start,
                member["security_id"],
                provider,
                window_end,
                start,
            ),
        )
        if overlaps:
            kinds = ", ".join(sorted({str(row["kind"]) for row in overlaps}))
            raise ValueError(
                f"{ticker} has partial overlapping {kinds} provenance; manual window required"
            )
        windows.append({"ticker": ticker, "start": start, "end": window_end})
    return windows


def plan_historical_reference_gaps(
    *,
    universe_id: str,
    start: date | str,
    end: date | str,
    store: Store | None = None,
) -> list[dict[str, date | str]]:
    """Plan uncovered owner/CIK/provider segments for every historical member.

    Unlike :func:`plan_missing_reference_windows`, this scans every exact
    security assignment that intersects the requested half-open research
    window, including securities that subsequently left the index. Existing
    reviewed providers are interchangeable only for coverage detection; the
    later batch build still certifies one explicitly selected provider.
    """
    db = store or get_store()
    window_start = _as_date(start, "start")
    window_end = _as_date(end, "end")
    if not universe_id.strip():
        raise ValueError("universe_id is required")
    if window_end <= window_start:
        raise ValueError("historical reference end must follow start")
    assignments = db.query(
        """
        SELECT identity.ticker, identity.security_id,
               identity.effective_start, identity.effective_end
        FROM security_identity_assignments AS identity
        JOIN universe_membership AS membership
          ON membership.universe_id = identity.universe_id
         AND membership.ticker = identity.ticker
         AND membership.effective_start = identity.effective_start
         AND membership.effective_end IS NOT DISTINCT FROM identity.effective_end
         AND membership.security_id = identity.security_id
        WHERE identity.universe_id = ?
          AND identity.effective_start < CAST(? AS DATE)
          AND COALESCE(identity.effective_end, DATE '9999-12-31') > CAST(? AS DATE)
        ORDER BY identity.ticker, identity.effective_start
        """,
        (universe_id, window_end.isoformat(), window_start.isoformat()),
    )
    if not assignments:
        raise ValueError(f"universe {universe_id!r} has no assignments in the window")

    planned: list[dict[str, date | str]] = []
    tickers_with_gap: set[str] = set()
    for assignment in assignments:
        ticker = str(assignment["ticker"])
        security_id = str(assignment["security_id"] or "")
        if not security_id:
            raise ValueError(f"historical member {ticker} has no stable security identity")
        assignment_start = _as_date(assignment["effective_start"], "assignment_start")
        assignment_end = (
            _as_date(assignment["effective_end"], "assignment_end")
            if assignment.get("effective_end") is not None
            else window_end
        )
        target_start = max(window_start, assignment_start)
        target_end = min(window_end, assignment_end)
        if target_end <= target_start:
            continue

        owner_rows = db.query(
            """
            SELECT owner.effective_start AS owner_start,
                   owner.effective_end AS owner_end,
                   cik.effective_start AS cik_start,
                   cik.effective_end AS cik_end
            FROM security_issuer_assignments AS owner
            JOIN issuer_cik_history AS cik ON cik.issuer_id = owner.issuer_id
            WHERE owner.security_id = ?
              AND owner.effective_start < CAST(? AS DATE)
              AND COALESCE(owner.effective_end, DATE '9999-12-31') > CAST(? AS DATE)
              AND cik.effective_start < CAST(? AS DATE)
              AND COALESCE(cik.effective_end, DATE '9999-12-31') > CAST(? AS DATE)
            """,
            (
                security_id,
                target_end.isoformat(),
                target_start.isoformat(),
                target_end.isoformat(),
                target_start.isoformat(),
            ),
        )
        owner_coverage = []
        for row in owner_rows:
            start_value = max(
                target_start,
                _as_date(row["owner_start"], "owner_start"),
                _as_date(row["cik_start"], "cik_start"),
            )
            end_value = min(
                target_end,
                _optional_interval_end(row.get("owner_end"), target_end),
                _optional_interval_end(row.get("cik_end"), target_end),
            )
            if start_value < end_value:
                owner_coverage.append((start_value, end_value))

        provider_rows = db.query(
            """
            SELECT data_start, data_end
            FROM provider_symbol_history
            WHERE security_id = ?
              AND mapping_status = 'verified'
              AND data_start < CAST(? AS DATE)
              AND COALESCE(data_end, DATE '9999-12-31') > CAST(? AS DATE)
            """,
            (security_id, target_end.isoformat(), target_start.isoformat()),
        )
        provider_coverage = [
            (
                max(target_start, _as_date(row["data_start"], "data_start")),
                min(target_end, _optional_interval_end(row.get("data_end"), target_end)),
            )
            for row in provider_rows
        ]
        complete_coverage = _intersect_intervals(owner_coverage, provider_coverage)
        gaps = _subtract_intervals(target_start, target_end, complete_coverage)
        if len(gaps) > 1:
            raise ValueError(
                f"{ticker} has multiple disjoint provenance gaps; split manual windows required"
            )
        if not gaps:
            continue
        if ticker in tickers_with_gap:
            raise ValueError(
                f"{ticker} has gaps in multiple identity assignments; manual windows required"
            )
        tickers_with_gap.add(ticker)
        gap_start, gap_end = gaps[0]
        planned.append({"ticker": ticker, "start": gap_start, "end": gap_end})
    return sorted(planned, key=lambda row: (str(row["ticker"]), row["start"], row["end"]))


def write_reference_window_batches(
    windows: Iterable[Mapping[str, Any]],
    *,
    output_dir: str | Path,
    batch_prefix: str,
    batch_size: int = 25,
    start_number: int = 1,
) -> list[Path]:
    """Write deterministic, import-validated window batches for review."""
    if not BATCH_NAME_PATTERN.fullmatch(batch_prefix):
        raise ValueError("batch_prefix may contain only letters, numbers, dot, dash, underscore")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if start_number < 1:
        raise ValueError("start_number must be positive")
    normalized = _normalize_batch_windows(
        enumerate(windows, start=1),
        label="planned windows",
    )
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for offset in range(0, len(normalized), batch_size):
        number = start_number + len(paths)
        path = directory / f"{batch_prefix}_{number:02d}_windows.csv"
        _write_csv(path, BATCH_WINDOW_FIELDS, normalized[offset : offset + batch_size])
        load_batch_windows(path)
        paths.append(path)
    return paths


def _optional_interval_end(value: Any, fallback: date) -> date:
    return _as_date(value, "interval_end") if value is not None else fallback


def _merge_intervals(intervals: Iterable[tuple[date, date]]) -> list[tuple[date, date]]:
    ordered = sorted((start, end) for start, end in intervals if start < end)
    merged: list[tuple[date, date]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _intersect_intervals(
    left: Iterable[tuple[date, date]],
    right: Iterable[tuple[date, date]],
) -> list[tuple[date, date]]:
    left_rows = _merge_intervals(left)
    right_rows = _merge_intervals(right)
    output: list[tuple[date, date]] = []
    left_index = 0
    right_index = 0
    while left_index < len(left_rows) and right_index < len(right_rows):
        left_start, left_end = left_rows[left_index]
        right_start, right_end = right_rows[right_index]
        start = max(left_start, right_start)
        end = min(left_end, right_end)
        if start < end:
            output.append((start, end))
        if left_end <= right_end:
            left_index += 1
        else:
            right_index += 1
    return _merge_intervals(output)


def _subtract_intervals(
    start: date,
    end: date,
    covered: Iterable[tuple[date, date]],
) -> list[tuple[date, date]]:
    gaps: list[tuple[date, date]] = []
    cursor = start
    for covered_start, covered_end in _merge_intervals(covered):
        clipped_start = max(start, covered_start)
        clipped_end = min(end, covered_end)
        if clipped_end <= cursor:
            continue
        if clipped_start > cursor:
            gaps.append((cursor, clipped_start))
        cursor = max(cursor, clipped_end)
        if cursor >= end:
            break
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def fetch_sec_ticker_records() -> dict[str, list[dict[str, Any]]]:
    """Fetch SEC ticker records without discarding duplicate ticker mappings."""
    raw = get_http().get_json(TICKER_MAP_URL)
    if not isinstance(raw, dict):
        raise ValueError("SEC ticker response is not an object")
    output: dict[str, list[dict[str, Any]]] = {}
    for entry in raw.values():
        if not isinstance(entry, dict):
            continue
        ticker = str(entry.get("ticker") or "").strip().upper()
        title = str(entry.get("title") or "").strip()
        cik_value = entry.get("cik_str")
        if not ticker or not title or cik_value is None:
            continue
        output.setdefault(ticker, []).append(
            {"ticker": ticker, "title": title, "cik": int(cik_value)}
        )
    return output


def build_stable_reference_batch(
    tickers: Iterable[str],
    *,
    universe_id: str,
    start: date | str,
    end: date | str,
    provider: str = "yfinance",
    verified_date: date | str | None = None,
    store: Store | None = None,
    sec_records: dict[str, list[dict[str, Any]]] | None = None,
    submissions_fetcher: Callable[[int], dict[str, Any]] = fetch_submissions,
    submission_file_fetcher: Callable[[str], dict[str, Any]] = (fetch_submission_file),
    price_fetcher: Callable[[str, str, str, str], list[dict]] = (fetch_provider_prices),
) -> dict[str, Any]:
    """Certify straightforward identities and return import-ready row sets.

    Rejections are retained in ``review_rows`` and never enter a manifest.
    """
    db = store or get_store()
    window_start = _as_date(start, "start")
    window_end = _as_date(end, "end")
    if window_end <= window_start:
        raise ValueError("end must follow start")
    checked_on = _as_date(verified_date or date.today(), "verified_date")
    if checked_on > date.today():
        raise ValueError("verified_date cannot be in the future")
    if window_end > checked_on + timedelta(days=1):
        raise ValueError("end cannot extend beyond the day after verified_date")
    live_half_open_endpoint = checked_on == date.today() and window_end == checked_on + timedelta(
        days=1
    )
    provider = provider.strip().lower()
    if provider not in {"yfinance", "tiingo"}:
        raise ValueError("stable batch certification currently supports yfinance or tiingo only")

    normalized = [str(ticker).strip().upper() for ticker in tickers]
    if not normalized or len(normalized) != len(set(normalized)):
        raise ValueError("batch tickers must be non-empty and unique")
    if any(not TICKER_PATTERN.fullmatch(ticker) for ticker in normalized):
        raise ValueError("batch contains an invalid ticker")

    ticker_records = sec_records if sec_records is not None else fetch_sec_ticker_records()
    issuer_rows_by_id: dict[str, dict] = {}
    owner_rows: list[dict] = []
    provider_rows: list[dict] = []
    review_rows: list[dict] = []

    for ticker in normalized:
        context = _blank_review(ticker, provider, checked_on)
        try:
            assignment = _stable_assignment(
                db,
                ticker=ticker,
                universe_id=universe_id,
                start=window_start,
                end=window_end,
            )
            context.update(
                {
                    "security_id": assignment["security_id"],
                    "assignment_start": assignment["effective_start"],
                    "assignment_end": assignment["effective_end"],
                }
            )

            record, sec_ticker = _resolve_sec_ticker_record(ticker, ticker_records)
            cik = int(record["cik"])
            submissions = submissions_fetcher(cik)
            submission_cik = int(submissions.get("cik") or 0)
            # SEC publishes the primary security first. Preserve that order for
            # the issuer's canonical display label while removing duplicates.
            # Alphabetical sorting can incorrectly promote a debt symbol (for
            # example CCZ) ahead of the listed common stock (CMCSA).
            submission_tickers = list(
                dict.fromkeys(
                    str(value).strip().upper()
                    for value in submissions.get("tickers", [])
                    if str(value).strip()
                )
            )
            if submission_cik != cik:
                raise ValueError("SEC ticker-map CIK disagrees with submissions CIK")
            if sec_ticker not in submission_tickers:
                raise ValueError("ticker is absent from the SEC submissions identity")
            filing_dates, history_payloads, history_sources = _sec_filing_evidence(
                submissions,
                start=window_start,
                end=window_end,
                file_fetcher=submission_file_fetcher,
            )
            if not filing_dates:
                raise ValueError("SEC submissions identity has no filing dates")
            if min(filing_dates) > window_start:
                raise ValueError("SEC submissions filing history does not reach the window start")
            # A completed historical interval needs filings on both sides so a
            # current ticker response cannot rewrite old issuer history.  For
            # the one live exception, the SEC ticker map and submissions
            # identity fetched on ``checked_on`` prove the endpoint itself;
            # ``window_end`` is merely tomorrow's half-open boundary.
            if max(filing_dates) < window_end and not live_half_open_endpoint:
                raise ValueError(
                    "SEC submissions filing history does not extend past the window end"
                )
            sec_name = str(submissions.get("name") or record.get("title") or "").strip()
            if not sec_name:
                raise ValueError("SEC identity has no issuer name")

            issuer_id = f"aios:issuer:sec:{cik:010d}"
            sec_source = SUBMISSIONS_URL.format(cik=f"{cik:010d}")
            canonical_ticker = submission_tickers[0]
            issuer_row = {
                "issuer_id": issuer_id,
                "canonical_name": sec_name,
                "canonical_ticker": canonical_ticker,
                "cik": f"{cik:010d}",
                "effective_start": window_start,
                "effective_end": window_end,
                "verified_date": checked_on,
                "source": sec_source,
            }
            previous_issuer = issuer_rows_by_id.get(issuer_id)
            if previous_issuer is not None and previous_issuer != issuer_row:
                raise ValueError("selected share classes disagree on issuer metadata")

            provider_symbol = _provider_symbol(provider, ticker, sec_ticker)
            raw_prices = price_fetcher(
                provider,
                provider_symbol,
                window_start.isoformat(),
                window_end.isoformat(),
            )
            price_profile = _validate_price_history(
                raw_prices,
                assignment=assignment,
                provider=provider,
                provider_symbol=provider_symbol,
                start=window_start,
                end=window_end,
            )
            provider_source = _provider_source(provider, provider_symbol)

            issuer_rows_by_id[issuer_id] = issuer_row
            owner_rows.append(
                {
                    "security_id": assignment["security_id"],
                    "issuer_id": issuer_id,
                    "effective_start": window_start,
                    "effective_end": window_end,
                    "verified_date": checked_on,
                    "source": sec_source,
                }
            )
            provider_rows.append(
                {
                    "provider": provider,
                    "provider_symbol": provider_symbol,
                    "security_id": assignment["security_id"],
                    "data_start": window_start,
                    "data_end": window_end,
                    "mapping_status": "verified",
                    "verified_date": checked_on,
                    "source": provider_source,
                }
            )
            context.update(
                {
                    "issuer_id": issuer_id,
                    "cik": f"{cik:010d}",
                    "sec_name": sec_name,
                    "sec_ticker": sec_ticker,
                    "provider_symbol": provider_symbol,
                    "provider_rows": price_profile["rows"],
                    "provider_first_date": price_profile["first_date"],
                    "provider_last_date": price_profile["last_date"],
                    "sec_payload_sha256": _payload_hash(
                        submissions
                        if not history_payloads
                        else {
                            "submissions": submissions,
                            "history_files": history_payloads,
                        }
                    ),
                    "sec_history_sources": ";".join(history_sources),
                    "price_payload_sha256": price_profile["sha256"],
                    "review_status": "accepted",
                    "sec_source": sec_source,
                    "provider_source": provider_source,
                }
            )
        except Exception as exc:
            context["review_status"] = "rejected"
            context["reason"] = str(exc)
        review_rows.append(context)

    return {
        "issuer_rows": sorted(issuer_rows_by_id.values(), key=lambda row: row["canonical_ticker"]),
        "owner_rows": sorted(owner_rows, key=lambda row: row["security_id"]),
        "provider_rows": sorted(provider_rows, key=lambda row: row["provider_symbol"]),
        "review_rows": review_rows,
        "accepted": sum(row["review_status"] == "accepted" for row in review_rows),
        "rejected": sum(row["review_status"] == "rejected" for row in review_rows),
    }


def build_stable_reference_window_batch(
    windows: Iterable[Mapping[str, Any]],
    *,
    universe_id: str,
    provider: str = "yfinance",
    verified_date: date | str | None = None,
    store: Store | None = None,
    sec_records: dict[str, list[dict[str, Any]]] | None = None,
    submissions_fetcher: Callable[[int], dict[str, Any]] = fetch_submissions,
    submission_file_fetcher: Callable[[str], dict[str, Any]] = (fetch_submission_file),
    price_fetcher: Callable[[str, str, str, str], list[dict]] = (fetch_provider_prices),
) -> dict[str, Any]:
    """Certify one strict bounded window per ticker and merge the row sets."""
    normalized = _normalize_batch_windows(
        enumerate(windows, start=1),
        label="batch windows",
    )
    ticker_records = sec_records if sec_records is not None else fetch_sec_ticker_records()
    results: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for window in sorted(
        normalized,
        key=lambda row: (row["ticker"], row["start"], row["end"]),
    ):
        result = build_stable_reference_batch(
            [window["ticker"]],
            universe_id=universe_id,
            start=window["start"],
            end=window["end"],
            provider=provider,
            verified_date=verified_date,
            store=store,
            sec_records=ticker_records,
            submissions_fetcher=submissions_fetcher,
            submission_file_fetcher=submission_file_fetcher,
            price_fetcher=price_fetcher,
        )
        results.append((window, result))
    return _merge_reference_window_results(results)


def build_anchored_reference_extension_batch(
    windows: Iterable[Mapping[str, Any]],
    *,
    universe_id: str,
    provider: str,
    provider_symbols: Mapping[str, str] | None = None,
    verified_date: date | str | None = None,
    store: Store | None = None,
    submissions_fetcher: Callable[[int], dict[str, Any]] = fetch_submissions,
    submission_file_fetcher: Callable[[str], dict[str, Any]] = fetch_submission_file,
    price_fetcher: Callable[[str, str, str, str], list[dict]] = fetch_provider_prices,
) -> dict[str, Any]:
    """Extend a reviewed reference chain across a retired-ticker gap.

    This path is deliberately narrower than accepting an arbitrary historical
    ticker/CIK pair. The exact security assignment must already be reviewed,
    and both the issuer/CIK chain and exact provider symbol must touch the gap
    on either side. SEC submissions must still match the anchored CIK and have
    filing evidence near the other endpoint. This supports acquisitions and
    reviewed ticker transitions without trusting the SEC's current-ticker map
    to describe retired symbols.
    """
    db = store or get_store()
    normalized = _normalize_batch_windows(
        enumerate(windows, start=1),
        label="anchored extension windows",
    )
    checked_on = _as_date(verified_date or date.today(), "verified_date")
    if checked_on > date.today():
        raise ValueError("verified_date cannot be in the future")
    normalized_provider = provider.strip().lower()
    if normalized_provider not in {"yfinance", "tiingo"}:
        raise ValueError("anchored extension supports yfinance or tiingo only")
    symbol_overrides = {
        str(ticker).strip().upper(): str(symbol).strip().upper()
        for ticker, symbol in (provider_symbols or {}).items()
    }
    results: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for window in sorted(
        normalized,
        key=lambda row: (row["ticker"], row["start"], row["end"]),
    ):
        ticker = str(window["ticker"])
        start = window["start"]
        end = window["end"]
        context = _blank_review(ticker, normalized_provider, checked_on)
        issuer_rows: list[dict[str, Any]] = []
        owner_rows: list[dict[str, Any]] = []
        provider_rows: list[dict[str, Any]] = []
        try:
            if end > checked_on + timedelta(days=1):
                raise ValueError("end cannot extend beyond the day after verified_date")
            assignment = _stable_assignment(
                db,
                ticker=ticker,
                universe_id=universe_id,
                start=start,
                end=end,
            )
            context.update(
                {
                    "security_id": assignment["security_id"],
                    "assignment_start": assignment["effective_start"],
                    "assignment_end": assignment["effective_end"],
                }
            )
            anchor = _adjacent_issuer_anchor(
                db,
                security_id=assignment["security_id"],
                start=start,
                end=end,
            )
            provider_symbol = symbol_overrides.get(ticker, ticker)
            provider_anchor = _adjacent_provider_anchor(
                db,
                security_id=assignment["security_id"],
                provider=normalized_provider,
                provider_symbol=provider_symbol,
                start=start,
                end=end,
            )
            cik = int(anchor["cik"])
            submissions = submissions_fetcher(cik)
            if int(submissions.get("cik") or 0) != cik:
                raise ValueError("anchored CIK disagrees with SEC submissions")
            filing_dates, history_payloads, history_sources = _sec_filing_evidence(
                submissions,
                start=start,
                end=end,
                file_fetcher=submission_file_fetcher,
            )
            if not filing_dates or min(filing_dates) > start:
                raise ValueError("SEC filing history does not reach the extension start")
            latest_required = end - timedelta(days=45)
            if max(filing_dates) < latest_required:
                raise ValueError(
                    "SEC filing history does not provide identity evidence near the gap end"
                )
            raw_prices = price_fetcher(
                normalized_provider,
                provider_symbol,
                start.isoformat(),
                end.isoformat(),
            )
            price_profile = _validate_price_history(
                raw_prices,
                assignment=assignment,
                provider=normalized_provider,
                provider_symbol=provider_symbol,
                start=start,
                end=end,
            )
            sec_source = SUBMISSIONS_URL.format(cik=f"{cik:010d}")
            continuity_source = (
                f"{sec_source}|anchor:{anchor['source']}|identity:{assignment['source']}"
            )
            covering_cik = db.query(
                """
                SELECT effective_start, effective_end, source
                FROM issuer_cik_history
                WHERE issuer_id = ? AND cik = ?
                  AND effective_start <= CAST(? AS DATE)
                  AND COALESCE(effective_end, DATE '9999-12-31') >= CAST(? AS DATE)
                ORDER BY effective_start
                """,
                (
                    anchor["issuer_id"],
                    f"{cik:010d}",
                    start.isoformat(),
                    end.isoformat(),
                ),
            )
            if len(covering_cik) > 1:
                raise ValueError("multiple existing CIK intervals cover the extension")
            cik_start = covering_cik[0]["effective_start"] if covering_cik else start
            cik_end = covering_cik[0]["effective_end"] if covering_cik else end
            cik_source = covering_cik[0]["source"] if covering_cik else continuity_source
            issuer_row = {
                "issuer_id": anchor["issuer_id"],
                "canonical_name": anchor["canonical_name"],
                "canonical_ticker": anchor["canonical_ticker"],
                "cik": f"{cik:010d}",
                "effective_start": cik_start,
                "effective_end": cik_end,
                "verified_date": checked_on,
                "source": cik_source,
            }
            owner_row = {
                "security_id": assignment["security_id"],
                "issuer_id": anchor["issuer_id"],
                "effective_start": start,
                "effective_end": end,
                "verified_date": checked_on,
                "source": continuity_source,
            }
            provider_source = _provider_source(normalized_provider, provider_symbol)
            provider_row = {
                "provider": normalized_provider,
                "provider_symbol": provider_symbol,
                "security_id": assignment["security_id"],
                "data_start": start,
                "data_end": end,
                "mapping_status": "verified",
                "verified_date": checked_on,
                "source": (
                    f"{provider_source}|anchor:{provider_anchor['source']}|"
                    f"identity:{assignment['source']}"
                ),
            }
            issuer_rows.append(issuer_row)
            owner_rows.append(owner_row)
            provider_rows.append(provider_row)
            context.update(
                {
                    "issuer_id": anchor["issuer_id"],
                    "cik": f"{cik:010d}",
                    "sec_name": str(submissions.get("name") or anchor["canonical_name"]),
                    "sec_ticker": "",
                    "provider_symbol": provider_symbol,
                    "provider_rows": price_profile["rows"],
                    "provider_first_date": price_profile["first_date"],
                    "provider_last_date": price_profile["last_date"],
                    "sec_payload_sha256": _payload_hash(
                        submissions
                        if not history_payloads
                        else {
                            "submissions": submissions,
                            "history_files": history_payloads,
                        }
                    ),
                    "sec_history_sources": ";".join(history_sources),
                    "price_payload_sha256": price_profile["sha256"],
                    "review_status": "accepted",
                    "reason": "accepted via adjacent reviewed issuer and provider anchors",
                    "sec_source": continuity_source,
                    "provider_source": provider_row["source"],
                }
            )
        except Exception as exc:
            context["review_status"] = "rejected"
            context["reason"] = str(exc)
        results.append(
            (
                window,
                {
                    "issuer_rows": issuer_rows,
                    "owner_rows": owner_rows,
                    "provider_rows": provider_rows,
                    "review_rows": [context],
                    "accepted": int(context["review_status"] == "accepted"),
                    "rejected": int(context["review_status"] == "rejected"),
                },
            )
        )
    return _merge_reference_window_results(results)


def write_reference_batch(
    result: dict[str, Any],
    *,
    output_dir: str | Path,
    batch_name: str,
) -> dict[str, Path]:
    """Write and re-parse manifests so generated output is import-compatible."""
    if not BATCH_NAME_PATTERN.fullmatch(batch_name):
        raise ValueError("batch_name may contain only letters, numbers, dot, dash, underscore")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "issuer_ciks": directory / f"{batch_name}_issuer_ciks.csv",
        "security_issuers": directory / f"{batch_name}_security_issuers.csv",
        "provider_symbols": directory / f"{batch_name}_provider_symbols.csv",
        "review": directory / f"{batch_name}_review.csv",
    }
    _write_csv(paths["review"], REVIEW_FIELDS, result["review_rows"])
    if not result.get("issuer_rows"):
        return {"review": paths["review"]}

    _write_csv(paths["issuer_ciks"], ISSUER_FIELDS, result["issuer_rows"])
    _write_csv(paths["security_issuers"], OWNER_FIELDS, result["owner_rows"])
    _write_csv(paths["provider_symbols"], PROVIDER_FIELDS, result["provider_rows"])

    load_issuer_cik_csv(paths["issuer_ciks"])
    load_security_issuer_csv(paths["security_issuers"])
    load_provider_symbol_csv(paths["provider_symbols"])
    return paths


def merge_reference_batch_files(batch_stems: Iterable[str | Path]) -> dict[str, Any]:
    """Merge accepted rows from independently reviewed, validated batch files.

    A stem is the path before ``_issuer_ciks.csv``. Original rejected review
    rows remain in their source batch; a later accepted retry may therefore be
    merged without duplicating the failed candidate in the final manifest.
    """
    stems = [Path(stem) for stem in batch_stems]
    if not stems:
        raise ValueError("at least one reference batch stem is required")
    issuer_candidates: list[dict] = []
    owner_candidates: list[dict] = []
    provider_candidates: list[dict] = []
    accepted_reviews: list[dict[str, str]] = []
    seen_reviews: set[tuple[str, str, str]] = set()

    for stem in stems:
        issuer_path = Path(f"{stem}_issuer_ciks.csv")
        owner_path = Path(f"{stem}_security_issuers.csv")
        provider_path = Path(f"{stem}_provider_symbols.csv")
        review_path = Path(f"{stem}_review.csv")
        issuers, cik_rows = load_issuer_cik_csv(issuer_path)
        issuer_by_id = {row["issuer_id"]: row for row in issuers}
        for cik_row in cik_rows:
            issuer_candidates.append({**issuer_by_id[cik_row["issuer_id"]], **cik_row})
        owners = load_security_issuer_csv(owner_path)
        providers = load_provider_symbol_csv(provider_path)
        reviews = _load_reference_review_csv(review_path)
        accepted = [row for row in reviews if row["review_status"] == "accepted"]
        accepted_ids = {row["security_id"] for row in accepted}
        owner_ids = {row["security_id"] for row in owners}
        provider_ids = {row["security_id"] for row in providers}
        if not owner_ids <= accepted_ids or not provider_ids <= accepted_ids:
            raise ValueError(f"batch {stem} contains manifests for a rejected review")
        if not accepted_ids <= owner_ids or not accepted_ids <= provider_ids:
            raise ValueError(f"batch {stem} has an accepted review without complete manifests")
        for review in accepted:
            key = (
                review["ticker"],
                review["assignment_start"],
                review["assignment_end"],
            )
            if key in seen_reviews:
                raise ValueError(f"duplicate accepted review for {review['ticker']}")
            seen_reviews.add(key)
            accepted_reviews.append(review)
        owner_candidates.extend(owners)
        provider_candidates.extend(providers)

    issuer_rows = _unique_manifest_rows(
        issuer_candidates,
        label="issuer",
        key_fields=("issuer_id", "effective_start"),
        allow_identical=True,
    )
    owner_rows = _unique_manifest_rows(
        owner_candidates,
        label="security issuer",
        key_fields=("security_id", "effective_start"),
        allow_identical=True,
    )
    provider_rows = _unique_manifest_rows(
        provider_candidates,
        label="provider symbol",
        key_fields=("provider", "security_id", "data_start"),
        allow_identical=True,
    )
    _reject_conflicting_issuer_metadata(issuer_rows)
    _reject_manifest_overlaps(
        issuer_rows,
        label="issuer",
        group_fields=("issuer_id",),
        start_field="effective_start",
        end_field="effective_end",
    )
    _reject_manifest_overlaps(
        owner_rows,
        label="security issuer",
        group_fields=("security_id",),
        start_field="effective_start",
        end_field="effective_end",
    )
    _reject_manifest_overlaps(
        provider_rows,
        label="provider symbol",
        group_fields=("provider", "security_id"),
        start_field="data_start",
        end_field="data_end",
    )
    _reject_provider_symbol_reuse(provider_rows)
    return {
        "issuer_rows": sorted(
            issuer_rows,
            key=lambda row: (row["canonical_ticker"], row["effective_start"]),
        ),
        "owner_rows": sorted(
            owner_rows,
            key=lambda row: (row["security_id"], row["effective_start"]),
        ),
        "provider_rows": sorted(
            provider_rows,
            key=lambda row: (row["provider_symbol"], row["data_start"]),
        ),
        "review_rows": sorted(
            accepted_reviews,
            key=lambda row: (row["ticker"], row["assignment_start"]),
        ),
        "accepted": len(accepted_reviews),
        "rejected": 0,
    }


def _load_reference_review_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != REVIEW_FIELDS:
            raise ValueError(f"reference review {path} has an invalid header")
        rows: list[dict[str, str]] = []
        for row_number, raw in enumerate(reader, start=2):
            if None in raw:
                raise ValueError(f"reference review {path} row {row_number} has extra fields")
            row = {field: str(raw.get(field) or "").strip() for field in REVIEW_FIELDS}
            if row["review_status"] not in {"accepted", "rejected"}:
                raise ValueError(f"reference review {path} row {row_number} has invalid status")
            if not row["ticker"] or not row["security_id"]:
                raise ValueError(
                    f"reference review {path} row {row_number} lacks ticker/security_id"
                )
            rows.append(row)
    return rows


def ingest_reviewed_reference_batch(
    issuer_cik_path: str | Path,
    security_issuer_path: str | Path,
    provider_symbol_path: str | Path,
    *,
    start: date | str,
    end: date | str,
    store: Store | None = None,
    companyfacts_zip_path: str | Path | None = None,
) -> dict[str, Any]:
    """Import one reviewed batch, then ingest every issuer and verified security.

    A local official Company Facts ZIP may replace per-CIK facts requests for
    larger batches. Submissions metadata remains a separate per-CIK request.
    """
    from aios.ingest.edgar import ingest_issuer

    db = store or get_store()
    window_start = _as_date(start, "start")
    window_end = _as_date(end, "end")
    if window_end <= window_start:
        raise ValueError("end must follow start")
    issuers, cik_history = load_issuer_cik_csv(issuer_cik_path)
    providers = load_provider_symbol_csv(provider_symbol_path)
    latest_cik_by_issuer: dict[str, dict] = {}
    for row in cik_history:
        previous = latest_cik_by_issuer.get(row["issuer_id"])
        if previous is None or row["effective_start"] > previous["effective_start"]:
            latest_cik_by_issuer[row["issuer_id"]] = row
    if set(latest_cik_by_issuer) != {row["issuer_id"] for row in issuers}:
        raise ValueError("every reviewed issuer must have exactly one latest CIK")
    fundamental_rows = 0
    price_rows = 0
    failures: list[dict[str, str]] = []
    archive_context = (
        CompanyFactsArchive(companyfacts_zip_path)
        if companyfacts_zip_path is not None
        else nullcontext(None)
    )
    with archive_context as facts_archive:
        if facts_archive is not None:
            facts_archive.validate_ciks([int(row["cik"]) for row in latest_cik_by_issuer.values()])
        counts = ingest_reference_identity_csvs(
            issuer_cik_path,
            security_issuer_path,
            provider_symbol_path,
            store=db,
        )
        for row in issuers:
            try:
                ingest_kwargs: dict[str, Any] = {"store": db}
                if facts_archive is not None:
                    cik = int(latest_cik_by_issuer[row["issuer_id"]]["cik"])
                    ingest_kwargs["facts_payload"] = facts_archive.read(cik)
                fundamental_rows += ingest_issuer(row["issuer_id"], **ingest_kwargs)
            except Exception as exc:
                failures.append(
                    {
                        "kind": "fundamentals",
                        "id": row["issuer_id"],
                        "error": str(exc),
                    }
                )
    security_ids = sorted(
        {row["security_id"] for row in providers if row["mapping_status"] == "verified"}
    )
    for security_id in security_ids:
        try:
            inserted = ingest_security_prices(
                security_id,
                start=window_start.isoformat(),
                end=window_end.isoformat(),
                store=db,
            )
            if inserted <= 0:
                raise ValueError("provider returned no rows in the reviewed interval")
            price_rows += inserted
        except Exception as exc:
            failures.append({"kind": "prices", "id": security_id, "error": str(exc)})
    return {
        "reference_counts": counts,
        "fundamental_rows": fundamental_rows,
        "price_rows": price_rows,
        "failures": failures,
        "companyfacts_source": (
            str(companyfacts_zip_path) if companyfacts_zip_path is not None else "sec-api"
        ),
    }


def _normalize_batch_windows(
    rows: Iterable[tuple[int, Mapping[str, Any]]],
    *,
    label: str,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: dict[str, tuple[date, date, int]] = {}
    expected = set(BATCH_WINDOW_FIELDS)
    for row_number, raw in rows:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{label} row {row_number}: expected a mapping")
        fields = [str(field).strip().lower() for field in raw]
        if len(fields) != len(set(fields)):
            raise ValueError(f"{label} row {row_number}: duplicate fields")
        actual = set(fields)
        if actual != expected:
            missing = expected - actual
            unknown = actual - expected
            details = []
            if missing:
                details.append(f"missing {', '.join(sorted(missing))}")
            if unknown:
                details.append(f"unsupported {', '.join(sorted(unknown))}")
            raise ValueError(f"{label} row {row_number}: invalid fields: {'; '.join(details)}")
        values = {str(field).strip().lower(): value for field, value in raw.items()}
        ticker = str(values["ticker"] or "").strip().upper()
        if not TICKER_PATTERN.fullmatch(ticker):
            raise ValueError(f"{label} row {row_number}: invalid ticker {values['ticker']!r}")
        try:
            start = _as_date(values["start"], "start")
            end = _as_date(values["end"], "end")
        except ValueError as exc:
            raise ValueError(f"{label} row {row_number}: {exc}") from exc
        if end <= start:
            raise ValueError(f"{label} row {row_number}: end must follow start")
        previous = seen.get(ticker)
        if previous is not None:
            previous_start, previous_end, previous_row = previous
            if (start, end) == (previous_start, previous_end):
                raise ValueError(
                    f"{label} row {row_number}: duplicate ticker/window {ticker} "
                    f"(first seen on row {previous_row})"
                )
            raise ValueError(
                f"{label} row {row_number}: conflicting window for ticker {ticker} "
                f"(first seen on row {previous_row})"
            )
        seen[ticker] = (start, end, row_number)
        output.append({"ticker": ticker, "start": start, "end": end})
    if not output:
        raise ValueError(f"{label} has no data rows")
    return output


def _merge_reference_window_results(
    results: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    issuer_candidates: list[dict] = []
    owner_candidates: list[dict] = []
    provider_candidates: list[dict] = []
    reviews: list[tuple[dict[str, Any], dict]] = []
    for window, result in results:
        review_rows = result.get("review_rows")
        if not isinstance(review_rows, list) or len(review_rows) != 1:
            raise ValueError(
                f"stable certification for {window['ticker']} did not return one review row"
            )
        review = dict(review_rows[0])
        status = review.get("review_status")
        if status not in {"accepted", "rejected"}:
            raise ValueError(f"stable certification for {window['ticker']} returned invalid status")
        reviews.append((window, review))
        manifest_rows = {
            "issuer": result.get("issuer_rows", []),
            "security issuer": result.get("owner_rows", []),
            "provider symbol": result.get("provider_rows", []),
        }
        if status == "rejected":
            if any(manifest_rows.values()):
                raise ValueError(
                    f"rejected certification for {window['ticker']} returned manifest rows"
                )
            continue
        if any(not isinstance(rows, list) or len(rows) != 1 for rows in manifest_rows.values()):
            raise ValueError(
                f"accepted certification for {window['ticker']} returned incomplete manifests"
            )
        issuer_candidates.append(dict(manifest_rows["issuer"][0]))
        owner_candidates.append(dict(manifest_rows["security issuer"][0]))
        provider_candidates.append(dict(manifest_rows["provider symbol"][0]))

    issuer_rows = _unique_manifest_rows(
        issuer_candidates,
        label="issuer",
        key_fields=("issuer_id", "effective_start"),
        allow_identical=True,
    )
    owner_rows = _unique_manifest_rows(
        owner_candidates,
        label="security issuer",
        key_fields=("security_id", "effective_start"),
    )
    provider_rows = _unique_manifest_rows(
        provider_candidates,
        label="provider symbol",
        key_fields=("provider", "security_id", "data_start"),
    )
    _reject_conflicting_issuer_metadata(issuer_rows)
    _reject_manifest_overlaps(
        issuer_rows,
        label="issuer",
        group_fields=("issuer_id",),
        start_field="effective_start",
        end_field="effective_end",
    )
    _reject_manifest_overlaps(
        owner_rows,
        label="security issuer",
        group_fields=("security_id",),
        start_field="effective_start",
        end_field="effective_end",
    )
    _reject_manifest_overlaps(
        provider_rows,
        label="provider symbol",
        group_fields=("provider", "security_id"),
        start_field="data_start",
        end_field="data_end",
    )
    _reject_provider_symbol_reuse(provider_rows)

    review_rows = [
        review
        for _window, review in sorted(
            reviews,
            key=lambda item: (
                item[1].get("ticker", ""),
                item[0]["start"],
                item[0]["end"],
            ),
        )
    ]
    return {
        "issuer_rows": sorted(
            issuer_rows,
            key=lambda row: (
                row["canonical_ticker"],
                row["issuer_id"],
                row["effective_start"],
                row["effective_end"],
            ),
        ),
        "owner_rows": sorted(
            owner_rows,
            key=lambda row: (
                row["security_id"],
                row["effective_start"],
                row["effective_end"],
                row["issuer_id"],
            ),
        ),
        "provider_rows": sorted(
            provider_rows,
            key=lambda row: (
                row["provider_symbol"],
                row["provider"],
                row["security_id"],
                row["data_start"],
                row["data_end"],
            ),
        ),
        "review_rows": review_rows,
        "accepted": sum(row["review_status"] == "accepted" for row in review_rows),
        "rejected": sum(row["review_status"] == "rejected" for row in review_rows),
    }


def _unique_manifest_rows(
    rows: list[dict],
    *,
    label: str,
    key_fields: tuple[str, ...],
    allow_identical: bool = False,
) -> list[dict]:
    output: list[dict] = []
    seen: dict[tuple[Any, ...], dict] = {}
    for row in rows:
        key = tuple(row[field] for field in key_fields)
        previous = seen.get(key)
        if previous is None:
            seen[key] = row
            output.append(row)
            continue
        if allow_identical and previous == row:
            continue
        kind = "duplicate" if previous == row else "conflicting"
        raise ValueError(f"window batch produced {kind} {label} rows for {key!r}")
    return output


def _reject_conflicting_issuer_metadata(rows: list[dict]) -> None:
    seen: dict[str, tuple[Any, ...]] = {}
    fields = ("canonical_name", "canonical_ticker", "cik", "source")
    for row in rows:
        issuer_id = row["issuer_id"]
        metadata = tuple(row[field] for field in fields)
        previous = seen.setdefault(issuer_id, metadata)
        if previous != metadata:
            raise ValueError(f"window batch produced conflicting issuer metadata for {issuer_id}")


def _reject_manifest_overlaps(
    rows: list[dict],
    *,
    label: str,
    group_fields: tuple[str, ...],
    start_field: str,
    end_field: str,
) -> None:
    grouped: dict[tuple[Any, ...], list[dict]] = {}
    for row in rows:
        key = tuple(row[field] for field in group_fields)
        grouped.setdefault(key, []).append(row)
    for key, group in grouped.items():
        ordered = sorted(group, key=lambda row: (row[start_field], row[end_field]))
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current[start_field] < previous[end_field]:
                raise ValueError(f"window batch produced overlapping {label} rows for {key!r}")


def _reject_provider_symbol_reuse(rows: list[dict]) -> None:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (row["provider"], row["provider_symbol"])
        grouped.setdefault(key, []).append(row)
    for key, group in grouped.items():
        ordered = sorted(group, key=lambda row: (row["data_start"], row["data_end"]))
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if (
                previous["security_id"] != current["security_id"]
                and current["data_start"] < previous["data_end"]
            ):
                raise ValueError(
                    f"window batch produced conflicting provider-symbol reuse for {key!r}"
                )


def _stable_assignment(
    store: Store,
    *,
    ticker: str,
    universe_id: str,
    start: date,
    end: date,
) -> dict:
    rows = store.query(
        """
        SELECT ticker, security_id, effective_start, effective_end, source
        FROM security_identity_assignments
        WHERE universe_id = ? AND ticker = ?
          AND effective_start <= CAST(? AS DATE)
          AND (effective_end IS NULL OR effective_end >= CAST(? AS DATE))
        """,
        (universe_id, ticker, start.isoformat(), end.isoformat()),
    )
    if len(rows) != 1:
        raise ValueError("ticker does not have one full-window security assignment")
    assignment = rows[0]
    aliases = store.security_ticker_assignments(assignment["security_id"], start=start, end=end)
    if len(aliases) != 1 or aliases[0]["ticker"] != ticker:
        raise ValueError("security has a ticker transition in the certified window")
    return assignment


def _adjacent_issuer_anchor(
    store: Store,
    *,
    security_id: str,
    start: date,
    end: date,
) -> dict[str, Any]:
    rows = store.query(
        """
        SELECT owner.issuer_id, owner.effective_start AS owner_start,
               owner.effective_end AS owner_end, owner.source AS owner_source,
               cik.cik, cik.effective_start AS cik_start,
               cik.effective_end AS cik_end, cik.source AS cik_source,
               issuer.canonical_name, issuer.canonical_ticker
        FROM security_issuer_assignments AS owner
        JOIN issuer_cik_history AS cik ON cik.issuer_id = owner.issuer_id
        JOIN issuer_master AS issuer ON issuer.issuer_id = owner.issuer_id
        WHERE owner.security_id = ?
        """,
        (security_id,),
    )
    anchors: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        anchor_start = max(
            _as_date(row["owner_start"], "owner_start"),
            _as_date(row["cik_start"], "cik_start"),
        )
        anchor_end = min(
            _optional_interval_end(row.get("owner_end"), date.max),
            _optional_interval_end(row.get("cik_end"), date.max),
        )
        if anchor_end != start and anchor_start != end:
            continue
        key = (str(row["issuer_id"]), str(row["cik"]))
        anchors[key] = {
            "issuer_id": key[0],
            "cik": key[1],
            "canonical_name": row["canonical_name"],
            "canonical_ticker": row["canonical_ticker"],
            "source": f"{row['owner_source']}|{row['cik_source']}",
        }
    if len(anchors) != 1:
        raise ValueError("gap does not touch exactly one reviewed issuer/CIK anchor")
    return next(iter(anchors.values()))


def _adjacent_provider_anchor(
    store: Store,
    *,
    security_id: str,
    provider: str,
    provider_symbol: str,
    start: date,
    end: date,
) -> dict[str, Any]:
    rows = store.query(
        """
        SELECT data_start, data_end, source
        FROM provider_symbol_history
        WHERE security_id = ?
          AND provider = ?
          AND provider_symbol = ?
          AND mapping_status = 'verified'
          AND (data_end = CAST(? AS DATE) OR data_start = CAST(? AS DATE))
        """,
        (security_id, provider, provider_symbol, start.isoformat(), end.isoformat()),
    )
    if not rows:
        raise ValueError("gap does not touch a reviewed exact provider-symbol anchor")
    return {"source": "|".join(sorted({str(row["source"]) for row in rows}))}


def _validate_price_history(
    rows: list[dict],
    *,
    assignment: dict,
    provider: str,
    provider_symbol: str,
    start: date,
    end: date,
) -> dict[str, Any]:
    if not rows:
        raise ValueError("provider returned no price rows")
    dates = [_as_date(row.get("date"), "price date") for row in rows]
    if len(dates) != len(set(dates)):
        raise ValueError("provider returned duplicate price dates")
    if any(day < start or day >= end for day in dates):
        raise ValueError("provider returned rows outside the requested window")
    if any(row.get("close") is None or float(row["close"]) <= 0 for row in rows):
        raise ValueError("provider returned a missing or non-positive close")

    # A live half-open window can end tomorrow, but today's U.S. close may not
    # exist yet.  Certify only completed dates and use the exchange calendar so
    # holidays are not misclassified as missing provider data.
    completed_end = min(end, date.today())
    expected_sessions = us_equity_sessions(start, completed_end)
    if not expected_sessions:
        raise ValueError("certified window contains no completed market sessions")
    expected_set = set(expected_sessions)
    actual_dates = set(dates)
    unexpected = sorted(day for day in actual_dates if day not in expected_set)
    if unexpected:
        raise ValueError(f"provider returned non-session price date: {unexpected[0]}")
    minimum_rows = max(1, int(len(expected_sessions) * 0.95))
    first_date = min(dates)
    last_date = max(dates)
    if len(rows) < minimum_rows:
        raise ValueError(f"provider history is incomplete: {len(rows)} rows, need {minimum_rows}")
    missing_positions = [
        index for index, session in enumerate(expected_sessions) if session not in actual_dates
    ]
    if _longest_consecutive_run(missing_positions) > 5:
        raise ValueError("provider history has more than five consecutive missing sessions")
    if first_date > start + timedelta(days=7):
        raise ValueError(f"provider history begins too late: {first_date}")
    if last_date < end - timedelta(days=7):
        raise ValueError(f"provider history ends too early: {last_date}")

    mapping = {
        "provider": provider,
        "provider_symbol": provider_symbol,
        "security_id": assignment["security_id"],
        "data_start": start,
        "data_end": end,
        "mapping_status": "verified",
    }
    relabeled = relabel_provider_price_rows(rows, mapping, [assignment])
    if len(relabeled) != len(rows):
        raise ValueError("provider rows did not fully fit the security assignment")
    fingerprint = [
        {
            "date": day.isoformat(),
            "close": row.get("close"),
            "adj_close": row.get("adj_close"),
            "volume": row.get("volume"),
        }
        for day, row in sorted(zip(dates, rows, strict=True))
    ]
    return {
        "rows": len(rows),
        "first_date": first_date,
        "last_date": last_date,
        "sha256": _payload_hash(fingerprint),
    }


def _longest_consecutive_run(positions: list[int]) -> int:
    longest = 0
    current = 0
    previous: int | None = None
    for position in positions:
        current = current + 1 if previous is not None and position == previous + 1 else 1
        longest = max(longest, current)
        previous = position
    return longest


def _blank_review(ticker: str, provider: str, checked_on: date) -> dict[str, Any]:
    return {field: "" for field in REVIEW_FIELDS} | {
        "ticker": ticker,
        "provider": provider,
        "review_status": "rejected",
        "reason": "",
        "verified_date": checked_on,
    }


def _provider_source(provider: str, symbol: str) -> str:
    if provider == "yfinance":
        return f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}"
    if provider == "tiingo":
        return f"https://api.tiingo.com/tiingo/daily/{quote(symbol)}"
    raise ValueError(f"unsupported provider {provider!r}")


def _resolve_sec_ticker_record(
    market_ticker: str,
    records_by_ticker: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any], str]:
    exact = records_by_ticker.get(market_ticker, [])
    if len(exact) == 1:
        return exact[0], market_ticker
    if len(exact) > 1:
        raise ValueError(f"SEC current ticker map has {len(exact)} records; manual review required")

    sec_ticker = market_ticker.replace(".", "-")
    notation_matches = records_by_ticker.get(sec_ticker, []) if sec_ticker != market_ticker else []
    if len(notation_matches) == 1:
        return notation_matches[0], sec_ticker
    if len(notation_matches) > 1:
        raise ValueError(
            "SEC dot/hyphen ticker notation has "
            f"{len(notation_matches)} records; manual review required"
        )
    raise ValueError("SEC current ticker map has 0 records; manual review required")


def _provider_symbol(provider: str, market_ticker: str, sec_ticker: str) -> str:
    if (
        provider in {"yfinance", "tiingo"}
        and market_ticker != sec_ticker
        and market_ticker.replace(".", "-") == sec_ticker
    ):
        return sec_ticker
    return market_ticker


def _sec_filing_evidence(
    submissions: dict[str, Any],
    *,
    start: date,
    end: date,
    file_fetcher: Callable[[str], dict[str, Any]],
) -> tuple[list[date], list[dict[str, Any]], list[str]]:
    """Follow only SEC-named history shards needed to bracket the window."""
    dates = _sec_filing_dates(submissions)
    history_payloads: list[dict[str, Any]] = []
    history_sources: list[str] = []
    fetched: set[str] = set()
    files = submissions.get("filings", {}).get("files", [])
    metadata = [item for item in files if isinstance(item, dict)] if isinstance(files, list) else []

    def fetch_candidates(candidates: list[dict[str, Any]], *, need_start: bool) -> None:
        nonlocal dates
        for item in candidates:
            name = str(item.get("name") or "").strip()
            if not name or name in fetched:
                continue
            payload = file_fetcher(name)
            fetched.add(name)
            history_payloads.append(payload)
            history_sources.append(SUBMISSIONS_FILE_URL.format(name=name))
            dates = sorted(set(dates + _sec_filing_dates(payload)))
            has_required_boundary = dates and (
                (need_start and min(dates) <= start) or (not need_start and max(dates) >= end)
            )
            if has_required_boundary:
                break

    if not dates or min(dates) > start:
        earlier = [
            item
            for item in metadata
            if _metadata_date(item.get("filingFrom")) is not None
            and _metadata_date(item.get("filingFrom")) <= start
        ]
        earlier.sort(
            key=lambda item: _metadata_date(item.get("filingTo")) or date.min,
            reverse=True,
        )
        fetch_candidates(earlier, need_start=True)

    if not dates or max(dates) < end:
        later = [
            item
            for item in metadata
            if _metadata_date(item.get("filingTo")) is not None
            and _metadata_date(item.get("filingTo")) >= end
        ]
        later.sort(key=lambda item: _metadata_date(item.get("filingFrom")) or date.max)
        fetch_candidates(later, need_start=False)

    return dates, history_payloads, history_sources


def _sec_filing_dates(payload: dict[str, Any]) -> list[date]:
    filings = payload.get("filings")
    if isinstance(filings, dict):
        recent = filings.get("recent", {})
        values = recent.get("filingDate", []) if isinstance(recent, dict) else []
    else:
        values = payload.get("filingDate", [])
    if not isinstance(values, list):
        return []
    return sorted({_as_date(value, "SEC filing date") for value in values if value})


def _metadata_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return _as_date(value, "SEC history metadata date")
    except ValueError:
        return None


def _payload_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _as_date(value: date | str | Any, field: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    field: value.isoformat() if isinstance(value, date) else value
                    for field, value in row.items()
                    if field in fields
                }
            )

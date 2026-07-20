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
            if max(filing_dates) < window_end:
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
            price_rows += ingest_security_prices(
                security_id,
                start=window_start.isoformat(),
                end=window_end.isoformat(),
                store=db,
            )
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
        SELECT ticker, security_id, effective_start, effective_end
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

    weekday_count = sum(
        (start + timedelta(days=offset)).weekday() < 5 for offset in range((end - start).days)
    )
    minimum_rows = max(1, int(weekday_count * 0.95))
    first_date = min(dates)
    last_date = max(dates)
    if len(rows) < minimum_rows:
        raise ValueError(f"provider history is incomplete: {len(rows)} rows, need {minimum_rows}")
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

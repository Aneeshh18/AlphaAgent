"""Strict issuer/CIK/security/provider identity manifests.

This layer deliberately keeps four concepts separate:

* issuer_id: internal identity for one legal/reporting entity;
* SEC CIK: the SEC's identifier for that reporting entity;
* security_id: internal identity for one listed security; and
* provider_symbol: a provider-specific query label valid only for a bounded
  slice of returned history.

The separation prevents current ticker endpoints from joining unrelated
companies after ticker reuse. All intervals are half-open: [start, end).
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from aios.ingest.security_identity import SECURITY_ID_PATTERN
from aios.storage.store import Store, get_store

ISSUER_CIK_COLUMNS = {
    "issuer_id",
    "canonical_name",
    "canonical_ticker",
    "cik",
    "effective_start",
    "effective_end",
    "verified_date",
    "source",
}
SECURITY_ISSUER_COLUMNS = {
    "security_id",
    "issuer_id",
    "effective_start",
    "effective_end",
    "verified_date",
    "source",
}
PROVIDER_SYMBOL_COLUMNS = {
    "provider",
    "provider_symbol",
    "security_id",
    "data_start",
    "data_end",
    "mapping_status",
    "verified_date",
    "source",
}
ALLOWED_PROVIDERS = {"yfinance", "stooq"}
ALLOWED_MAPPING_STATUSES = {"verified", "unavailable", "blocked_wrong_security"}


def load_issuer_cik_csv(path: str | Path) -> tuple[list[dict], list[dict]]:
    """Load canonical issuers and dated SEC CIK assignments."""
    rows = _read_csv(path, ISSUER_CIK_COLUMNS, "issuer CIK")
    issuers: list[dict] = []
    cik_history: list[dict] = []
    issuer_values: dict[str, tuple[str, str, str]] = {}
    seen_intervals: set[tuple[str, date]] = set()
    for row_number, row in rows:
        issuer_id = _internal_id(row["issuer_id"], "issuer_id", row_number)
        canonical_name = _required(row["canonical_name"], "canonical_name", row_number)
        canonical_ticker = _ticker(
            row["canonical_ticker"], "canonical_ticker", row_number
        )
        source = _https_source(row["source"], row_number)
        values = (canonical_name, canonical_ticker, source)
        previous = issuer_values.get(issuer_id)
        if previous is not None and previous != values:
            raise ValueError(
                f"issuer CIK row {row_number}: conflicting canonical issuer metadata"
            )
        if previous is None:
            issuer_values[issuer_id] = values
            issuers.append(
                {
                    "issuer_id": issuer_id,
                    "canonical_name": canonical_name,
                    "canonical_ticker": canonical_ticker,
                    "source": source,
                }
            )

        cik = _cik(row["cik"], row_number)
        start, end = _interval(
            row["effective_start"],
            row["effective_end"],
            "effective",
            row_number,
        )
        key = (issuer_id, start)
        if key in seen_intervals:
            raise ValueError(f"issuer CIK row {row_number}: duplicate interval")
        seen_intervals.add(key)
        cik_history.append(
            {
                "issuer_id": issuer_id,
                "cik": cik,
                "effective_start": start,
                "effective_end": end,
                "verified_date": _verification_date(row["verified_date"], row_number),
                "source": source,
            }
        )
    _reject_overlaps(cik_history, "issuer_id", "effective", "issuer CIK")
    return issuers, cik_history


def load_security_issuer_csv(path: str | Path) -> list[dict]:
    """Load dated links from listed securities to reporting issuers."""
    rows = _read_csv(path, SECURITY_ISSUER_COLUMNS, "security issuer")
    output: list[dict] = []
    seen: set[tuple[str, date]] = set()
    for row_number, row in rows:
        security_id = _internal_id(row["security_id"], "security_id", row_number)
        issuer_id = _internal_id(row["issuer_id"], "issuer_id", row_number)
        start, end = _interval(
            row["effective_start"],
            row["effective_end"],
            "effective",
            row_number,
        )
        key = (security_id, start)
        if key in seen:
            raise ValueError(f"security issuer row {row_number}: duplicate interval")
        seen.add(key)
        output.append(
            {
                "security_id": security_id,
                "issuer_id": issuer_id,
                "effective_start": start,
                "effective_end": end,
                "verified_date": _verification_date(row["verified_date"], row_number),
                "source": _https_source(row["source"], row_number),
            }
        )
    _reject_overlaps(output, "security_id", "effective", "security issuer")
    return output


def load_provider_symbol_csv(path: str | Path) -> list[dict]:
    """Load bounded provider symbols; no symbol is assumed globally valid."""
    rows = _read_csv(path, PROVIDER_SYMBOL_COLUMNS, "provider symbol")
    output: list[dict] = []
    seen: set[tuple[str, str, date]] = set()
    for row_number, row in rows:
        provider = _required(row["provider"], "provider", row_number).lower()
        if provider not in ALLOWED_PROVIDERS:
            raise ValueError(
                f"provider symbol row {row_number}: unsupported provider {provider!r}"
            )
        security_id = _internal_id(row["security_id"], "security_id", row_number)
        provider_symbol = _ticker(
            row["provider_symbol"], "provider_symbol", row_number
        )
        start, end = _interval(
            row["data_start"], row["data_end"], "data", row_number
        )
        status = _required(row["mapping_status"], "mapping_status", row_number)
        if status not in ALLOWED_MAPPING_STATUSES:
            raise ValueError(
                f"provider symbol row {row_number}: unsupported status {status!r}"
            )
        key = (provider, security_id, start)
        if key in seen:
            raise ValueError(f"provider symbol row {row_number}: duplicate interval")
        seen.add(key)
        output.append(
            {
                "provider": provider,
                "provider_symbol": provider_symbol,
                "security_id": security_id,
                "data_start": start,
                "data_end": end,
                "mapping_status": status,
                "verified_date": _verification_date(row["verified_date"], row_number),
                "source": _https_source(row["source"], row_number),
            }
        )
    _reject_overlaps(
        output,
        ("provider", "security_id"),
        "data",
        "provider symbol",
    )
    _reject_overlapping_symbol_reuse(output)
    return output


def ingest_reference_identity_csvs(
    issuer_cik_path: str | Path,
    security_issuer_path: str | Path,
    provider_symbol_path: str | Path,
    *,
    store: Store | None = None,
) -> dict[str, int]:
    """Validate and atomically import all reference-identity manifests."""
    db = store or get_store()
    started_at = datetime.now()
    run_id = str(uuid4())
    paths = [Path(issuer_cik_path), Path(security_issuer_path), Path(provider_symbol_path)]
    source = "csv:" + ",".join(path.name for path in paths)
    try:
        issuers, cik_history = load_issuer_cik_csv(paths[0])
        security_issuers = load_security_issuer_csv(paths[1])
        provider_symbols = load_provider_symbol_csv(paths[2])
        counts = db.upsert_reference_identities(
            issuers,
            cik_history,
            security_issuers,
            provider_symbols,
        )
        db.record_ingest(
            run_id=run_id,
            source=source,
            table_name="reference_identities",
            rows_inserted=sum(counts.values()),
            started_at=started_at,
        )
        return counts
    except Exception as exc:
        db.record_ingest(
            run_id=run_id,
            source=source,
            table_name="reference_identities",
            status="failed",
            error=str(exc),
            started_at=started_at,
        )
        raise


def _read_csv(
    path: str | Path,
    expected_columns: set[str],
    label: str,
) -> list[tuple[int, dict[str, str]]]:
    csv_path = Path(path)
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = [str(field or "").strip().lower() for field in reader.fieldnames or []]
        if len(fields) != len(set(fields)):
            raise ValueError(f"{label} CSV has duplicate columns")
        actual = set(fields)
        if actual != expected_columns:
            missing = expected_columns - actual
            unknown = actual - expected_columns
            details = []
            if missing:
                details.append(f"missing {', '.join(sorted(missing))}")
            if unknown:
                details.append(f"unsupported {', '.join(sorted(unknown))}")
            raise ValueError(f"{label} CSV columns invalid: {'; '.join(details)}")
        output: list[tuple[int, dict[str, str]]] = []
        for row_number, raw in enumerate(reader, start=2):
            if None in raw:
                raise ValueError(f"{label} CSV row {row_number} has extra fields")
            row = {
                str(key).strip().lower(): str(value or "").strip()
                for key, value in raw.items()
            }
            if not any(row.values()):
                continue
            output.append((row_number, row))
    if not output:
        raise ValueError(f"{label} CSV has no data rows")
    return output


def _required(value: str, field: str, row_number: int) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"row {row_number} requires {field}")
    return value


def _internal_id(value: str, field: str, row_number: int) -> str:
    value = _required(value, field, row_number)
    if not SECURITY_ID_PATTERN.fullmatch(value):
        raise ValueError(f"row {row_number}: invalid {field}")
    return value


def _ticker(value: str, field: str, row_number: int) -> str:
    value = _required(value, field, row_number).upper()
    if not SECURITY_ID_PATTERN.fullmatch(value):
        raise ValueError(f"row {row_number}: invalid {field}")
    return value


def _cik(value: str, row_number: int) -> str:
    value = _required(value, "cik", row_number)
    if not value.isdigit() or len(value) > 10:
        raise ValueError(f"issuer CIK row {row_number}: invalid CIK {value!r}")
    return value.zfill(10)


def _parse_date(value: str, field: str, row_number: int) -> date:
    value = _required(value, field, row_number)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"row {row_number}: invalid {field} {value!r}") from exc


def _interval(
    start_value: str,
    end_value: str,
    prefix: str,
    row_number: int,
) -> tuple[date, date | None]:
    start = _parse_date(start_value, f"{prefix}_start", row_number)
    end = _parse_date(end_value, f"{prefix}_end", row_number) if end_value else None
    if end is not None and end <= start:
        raise ValueError(f"row {row_number}: {prefix}_end must follow {prefix}_start")
    return start, end


def _verification_date(value: str, row_number: int) -> date:
    verified = _parse_date(value, "verified_date", row_number)
    if verified > date.today():
        raise ValueError(f"row {row_number}: verified_date cannot be in the future")
    return verified


def _https_source(value: str, row_number: int) -> str:
    value = _required(value, "source", row_number)
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"row {row_number}: source must be an HTTPS URL")
    return value


def _reject_overlaps(
    rows: list[dict],
    group_key: str | tuple[str, ...],
    prefix: str,
    label: str,
) -> None:
    keys = (group_key,) if isinstance(group_key, str) else group_key
    grouped: dict[tuple[str, ...], list[dict]] = {}
    for row in rows:
        grouped.setdefault(tuple(row[key] for key in keys), []).append(row)
    for key, intervals in grouped.items():
        ordered = sorted(intervals, key=lambda row: row[f"{prefix}_start"])
        previous_end: date | None = None
        for index, row in enumerate(ordered):
            start = row[f"{prefix}_start"]
            if index and (previous_end is None or start < previous_end):
                raise ValueError(f"{label} has overlapping intervals for {key}")
            previous_end = row[f"{prefix}_end"]


def _reject_overlapping_symbol_reuse(rows: list[dict]) -> None:
    verified = [row for row in rows if row["mapping_status"] == "verified"]
    for index, left in enumerate(verified):
        for right in verified[index + 1 :]:
            if (
                left["provider"] != right["provider"]
                or left["provider_symbol"] != right["provider_symbol"]
                or left["security_id"] == right["security_id"]
            ):
                continue
            left_end = left["data_end"] or date.max
            right_end = right["data_end"] or date.max
            if left_end > right["data_start"] and right_end > left["data_start"]:
                raise ValueError(
                    "provider symbol maps to overlapping securities: "
                    f"{left['provider']}:{left['provider_symbol']}"
                )

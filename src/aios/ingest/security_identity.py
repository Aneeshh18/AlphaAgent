"""Audited stable-security identities for historical universe intervals.

A ticker is a dated label, not a permanent security key. This module turns a
certified membership CSV plus explicit same-security transition evidence into
an assignment file that can be imported transactionally. It never guesses that
an index replacement or corporate combination preserves security identity.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from aios.ingest.universe import load_membership_csv
from aios.storage.store import Store, get_store

TRANSITION_REQUIRED_COLUMNS = {
    "security_id",
    "from_ticker",
    "to_ticker",
    "effective_date",
    "known_date",
    "transition_type",
    "source",
}
TRANSITION_OPTIONAL_COLUMNS = {"universe_id"}
ASSIGNMENT_REQUIRED_COLUMNS = {
    "universe_id",
    "ticker",
    "effective_start",
    "effective_end",
    "security_id",
    "known_date",
    "identity_status",
    "source",
}
ASSIGNMENT_COLUMNS = (
    "universe_id",
    "ticker",
    "effective_start",
    "effective_end",
    "security_id",
    "known_date",
    "identity_status",
    "source",
)
TRANSITION_STATUS = {
    "ticker_change": "verified_ticker_change",
    "surviving_security_ticker_change": (
        "verified_surviving_security_ticker_change"
    ),
}
SECURITY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]*$")


@dataclass(frozen=True)
class SecurityTransition:
    """One verified change where two dated tickers identify one security."""

    universe_id: str
    security_id: str
    from_ticker: str
    to_ticker: str
    effective_date: date
    known_date: date
    transition_type: str
    source: str


def load_security_transitions_csv(
    path: str | Path,
    *,
    universe_id: str = "sp500",
) -> list[SecurityTransition]:
    """Read strict same-security transition evidence from CSV."""
    csv_path = Path(path)
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        _validate_columns(
            reader,
            required=TRANSITION_REQUIRED_COLUMNS,
            optional=TRANSITION_OPTIONAL_COLUMNS,
            label="security transition",
        )
        rows: list[SecurityTransition] = []
        seen: set[tuple[str, str, str, date]] = set()
        ticker_security: dict[tuple[str, str], str] = {}
        for row_number, raw in enumerate(reader, start=2):
            row = _normalize_csv_row(raw, row_number, "security transition")
            row_universe = row.get("universe_id") or universe_id
            if row_universe != universe_id:
                raise ValueError(
                    f"security transition row {row_number}: universe_id "
                    f"{row_universe!r} does not match {universe_id!r}"
                )
            security_id = row["security_id"]
            if not SECURITY_ID_PATTERN.fullmatch(security_id):
                raise ValueError(
                    f"security transition row {row_number}: invalid security_id"
                )
            from_ticker = row["from_ticker"].upper()
            to_ticker = row["to_ticker"].upper()
            if not from_ticker or not to_ticker or from_ticker == to_ticker:
                raise ValueError(
                    f"security transition row {row_number}: tickers must be distinct"
                )
            effective_date = _parse_date(
                row["effective_date"], "effective_date", row_number
            )
            known_date = _parse_date(row["known_date"], "known_date", row_number)
            if known_date > effective_date:
                raise ValueError(
                    f"security transition row {row_number}: known_date follows effective_date"
                )
            transition_type = row["transition_type"]
            if transition_type not in TRANSITION_STATUS:
                raise ValueError(
                    f"security transition row {row_number}: unsupported transition_type "
                    f"{transition_type!r}"
                )
            source = row["source"]
            parsed = urlparse(source)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError(
                    f"security transition row {row_number}: source must be an HTTPS URL"
                )
            key = (row_universe, from_ticker, to_ticker, effective_date)
            if key in seen:
                raise ValueError(
                    f"security transition row {row_number}: duplicate transition"
                )
            seen.add(key)
            for ticker in (from_ticker, to_ticker):
                ticker_key = (row_universe, ticker)
                previous = ticker_security.get(ticker_key)
                if previous is not None and previous != security_id:
                    raise ValueError(
                        f"security transition row {row_number}: {ticker} maps to "
                        "conflicting security IDs"
                    )
                ticker_security[ticker_key] = security_id
            rows.append(
                SecurityTransition(
                    universe_id=row_universe,
                    security_id=security_id,
                    from_ticker=from_ticker,
                    to_ticker=to_ticker,
                    effective_date=effective_date,
                    known_date=known_date,
                    transition_type=transition_type,
                    source=source,
                )
            )
    return sorted(rows, key=lambda row: (row.effective_date, row.security_id))


def build_security_identity_assignments(
    memberships: list[dict],
    transitions: list[SecurityTransition],
    *,
    universe_id: str,
) -> list[dict]:
    """Assign stable IDs without equating ordinary additions and deletions."""
    scoped = [row for row in memberships if row["universe_id"] == universe_id]
    if len(scoped) != len(memberships):
        raise ValueError("membership rows contain an unexpected universe_id")
    if not scoped:
        raise ValueError("membership rows are empty")

    ticker_override: dict[str, SecurityTransition] = {}
    for transition in transitions:
        if transition.universe_id != universe_id:
            raise ValueError("transition universe does not match membership universe")
        _validate_transition_boundary(scoped, transition)
        for ticker in (transition.from_ticker, transition.to_ticker):
            previous = ticker_override.get(ticker)
            if previous is not None and previous.security_id != transition.security_id:
                raise ValueError(f"ticker {ticker} has conflicting transition identities")
            if (
                previous is not None
                and TRANSITION_STATUS[previous.transition_type]
                != TRANSITION_STATUS[transition.transition_type]
            ):
                raise ValueError(f"ticker {ticker} has inconsistent transition types")
            ticker_override[ticker] = transition

    assignments: list[dict] = []
    for membership in scoped:
        ticker = membership["ticker"]
        transition = ticker_override.get(ticker)
        if transition is None:
            security_id = _bounded_security_id(universe_id, ticker)
            status = "bounded_ticker"
            source = f"bounded-membership:{membership['source']}"
        else:
            security_id = transition.security_id
            status = TRANSITION_STATUS[transition.transition_type]
            source = transition.source
        assignments.append(
            {
                "universe_id": universe_id,
                "ticker": ticker,
                "effective_start": membership["effective_start"],
                "effective_end": membership["effective_end"],
                "security_id": security_id,
                "known_date": membership["known_date"],
                "identity_status": status,
                "source": source,
            }
        )

    _validate_assignment_overlaps(assignments)
    return sorted(
        assignments,
        key=lambda row: (row["security_id"], row["effective_start"], row["ticker"]),
    )


def write_security_identity_csv(path: str | Path, rows: list[dict]) -> None:
    """Write validated assignment rows for review and controlled import."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ASSIGNMENT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **row,
                    "effective_start": row["effective_start"].isoformat(),
                    "effective_end": (
                        row["effective_end"].isoformat()
                        if row["effective_end"]
                        else ""
                    ),
                    "known_date": row["known_date"].isoformat(),
                }
            )


def load_security_identity_csv(path: str | Path) -> list[dict]:
    """Read an assignment CSV without weakening any field requirement."""
    csv_path = Path(path)
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        _validate_columns(
            reader,
            required=ASSIGNMENT_REQUIRED_COLUMNS,
            optional=set(),
            label="security identity",
        )
        rows: list[dict] = []
        seen: set[tuple[str, str, date]] = set()
        for row_number, raw in enumerate(reader, start=2):
            row = _normalize_csv_row(raw, row_number, "security identity")
            effective_start = _parse_date(
                row["effective_start"], "effective_start", row_number
            )
            effective_end = (
                _parse_date(row["effective_end"], "effective_end", row_number)
                if row["effective_end"]
                else None
            )
            known_date = _parse_date(row["known_date"], "known_date", row_number)
            if known_date > effective_start:
                raise ValueError(
                    f"security identity row {row_number}: known_date follows start"
                )
            if effective_end is not None and effective_end <= effective_start:
                raise ValueError(
                    f"security identity row {row_number}: end must follow start"
                )
            if not SECURITY_ID_PATTERN.fullmatch(row["security_id"]):
                raise ValueError(
                    f"security identity row {row_number}: invalid security_id"
                )
            if row["identity_status"] not in set(TRANSITION_STATUS.values()) | {
                "bounded_ticker"
            }:
                raise ValueError(
                    f"security identity row {row_number}: unsupported identity_status"
                )
            key = (row["universe_id"], row["ticker"].upper(), effective_start)
            if key in seen:
                raise ValueError(
                    f"security identity row {row_number}: duplicate assignment"
                )
            seen.add(key)
            rows.append(
                {
                    "universe_id": row["universe_id"],
                    "ticker": row["ticker"].upper(),
                    "effective_start": effective_start,
                    "effective_end": effective_end,
                    "security_id": row["security_id"],
                    "known_date": known_date,
                    "identity_status": row["identity_status"],
                    "source": row["source"],
                }
            )
    _validate_assignment_overlaps(rows)
    return rows


def ingest_security_identity_csv(
    path: str | Path,
    *,
    store: Store | None = None,
) -> int:
    """Import assignments transactionally and record the outcome."""
    db = store or get_store()
    started_at = datetime.now()
    run_id = str(uuid4())
    csv_path = Path(path)
    source = f"csv:{csv_path.name}"
    try:
        rows = load_security_identity_csv(csv_path)
        inserted = db.upsert_security_identities(rows)
        db.record_ingest(
            run_id=run_id,
            source=source,
            table_name="security_identity_assignments",
            rows_inserted=inserted,
            started_at=started_at,
        )
        return inserted
    except Exception as exc:
        db.record_ingest(
            run_id=run_id,
            source=source,
            table_name="security_identity_assignments",
            status="failed",
            error=str(exc),
            started_at=started_at,
        )
        raise


def build_security_identity_csv(
    membership_path: str | Path,
    transition_path: str | Path,
    output_path: str | Path,
    *,
    universe_id: str = "sp500",
) -> list[dict]:
    """Convenience orchestration used by the CLI and reproducibility tests."""
    memberships = load_membership_csv(membership_path, universe_id=universe_id)
    transitions = load_security_transitions_csv(
        transition_path,
        universe_id=universe_id,
    )
    rows = build_security_identity_assignments(
        memberships,
        transitions,
        universe_id=universe_id,
    )
    write_security_identity_csv(output_path, rows)
    return rows


def _validate_transition_boundary(
    memberships: list[dict],
    transition: SecurityTransition,
) -> None:
    outgoing = [
        row
        for row in memberships
        if row["ticker"] == transition.from_ticker
        and row["effective_end"] == transition.effective_date
    ]
    incoming = [
        row
        for row in memberships
        if row["ticker"] == transition.to_ticker
        and row["effective_start"] == transition.effective_date
    ]
    if len(outgoing) != 1 or len(incoming) != 1:
        raise ValueError(
            "security transition does not match one outgoing and one incoming "
            f"membership boundary: {transition.from_ticker}->{transition.to_ticker} "
            f"on {transition.effective_date}"
        )


def _validate_assignment_overlaps(rows: list[dict]) -> None:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        grouped.setdefault((row["universe_id"], row["security_id"]), []).append(row)
    for (universe_id, security_id), assignments in grouped.items():
        ordered = sorted(
            assignments,
            key=lambda row: (row["effective_start"], row["ticker"]),
        )
        previous_end: date | None = None
        for index, row in enumerate(ordered):
            if index and (previous_end is None or row["effective_start"] < previous_end):
                raise ValueError(
                    f"overlapping ticker assignments for {universe_id}:{security_id}"
                )
            previous_end = row["effective_end"]


def _bounded_security_id(universe_id: str, ticker: str) -> str:
    safe_universe = re.sub(r"[^a-z0-9._-]+", "-", universe_id.casefold()).strip("-")
    safe_ticker = re.sub(r"[^a-z0-9._-]+", "-", ticker.casefold()).strip("-")
    if not safe_universe or not safe_ticker:
        raise ValueError("cannot derive bounded security ID")
    return f"aios:bounded:{safe_universe}:{safe_ticker}"


def _parse_date(value: str, field: str, row_number: int) -> date:
    if not value:
        raise ValueError(f"row {row_number} needs {field}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"row {row_number}: invalid {field} {value!r}") from exc


def _validate_columns(
    reader: csv.DictReader,
    *,
    required: set[str],
    optional: set[str],
    label: str,
) -> None:
    fields = [str(column or "").strip().lower() for column in reader.fieldnames or []]
    if len(fields) != len(set(fields)):
        raise ValueError(f"{label} CSV has duplicate columns")
    columns = set(fields)
    missing = required - columns
    if missing:
        raise ValueError(
            f"{label} CSV is missing required columns: {', '.join(sorted(missing))}"
        )
    unknown = columns - required - optional
    if unknown:
        raise ValueError(
            f"{label} CSV has unsupported columns: {', '.join(sorted(unknown))}"
        )


def _normalize_csv_row(
    raw: dict[str | None, str | list[str] | None],
    row_number: int,
    label: str,
) -> dict[str, str]:
    if None in raw:
        raise ValueError(f"{label} CSV row {row_number} has extra fields")
    return {
        str(key).strip().lower(): str(value or "").strip()
        for key, value in raw.items()
    }

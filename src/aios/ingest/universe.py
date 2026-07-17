"""Import release-aware historical investable-universe membership.

The importer deliberately requires ``known_date``. An effective membership
date answers *when the constituent belongs in the index/universe*; known_date
answers *when a backtest could have known that fact*. Treating those as the
same date would reintroduce survivorship and membership look-ahead bias.

Expected CSV columns::

    universe_id,ticker,effective_start,effective_end,known_date,source

``universe_id`` and ``source`` may be supplied as command-line defaults, but
``effective_start`` and ``known_date`` are always required per row.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from aios.storage.store import Store, get_store

REQUIRED_COLUMNS = {"ticker", "effective_start", "known_date"}
OPTIONAL_COLUMNS = {"universe_id", "effective_end", "source"}
SPAN_COLUMNS = {"ticker", "start_date", "end_date"}
EVENT_REQUIRED_COLUMNS = {"ticker", "effective_date", "action", "known_date", "source"}
EVENT_OPTIONAL_COLUMNS = {"universe_id", "index_name", "source_kind"}
MEMBERSHIP_COLUMNS = (
    "universe_id",
    "ticker",
    "effective_start",
    "effective_end",
    "known_date",
    "source",
)


@dataclass(frozen=True)
class EffectiveSpan:
    """One reference membership spell using a half-open interval."""

    ticker: str
    start_date: date
    end_date: date | None


@dataclass(frozen=True)
class UniverseEvent:
    """One publicly announced addition or deletion."""

    universe_id: str
    ticker: str
    effective_date: date
    action: str
    known_date: date
    source: str
    source_kind: str = "index_announcement"


@dataclass(frozen=True)
class BoundaryReconciliation:
    """Differences between reference interval edges and announced events."""

    missing_events: tuple[tuple[str, date, str], ...]
    unexpected_events: tuple[tuple[str, date, str], ...]
    date_conflicts: tuple[tuple[str, date, date, str], ...] = ()

    @property
    def is_complete(self) -> bool:
        """Whether each boundary identity has evidence in both sources."""
        return not self.missing_events and not self.unexpected_events

    @property
    def is_clean(self) -> bool:
        return self.is_complete and not self.date_conflicts


def load_effective_spans_csv(path: str | Path) -> list[EffectiveSpan]:
    """Load a ticker/start/end reference file such as fja05680's span export."""
    csv_path = Path(path)
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        _validate_columns(reader, required=SPAN_COLUMNS, optional=set(), label="span")
        spans: list[EffectiveSpan] = []
        seen: set[tuple[str, date]] = set()
        for row_number, raw in enumerate(reader, start=2):
            row = _normalize_csv_row(raw, row_number, "span")
            ticker = row["ticker"].upper()
            if not ticker:
                raise ValueError(f"span CSV row {row_number} needs ticker")
            start_date = _parse_date(row["start_date"], "start_date", row_number)
            end_date = (
                _parse_date(row["end_date"], "end_date", row_number)
                if row["end_date"]
                else None
            )
            if end_date is not None and end_date <= start_date:
                raise ValueError(f"span CSV row {row_number}: end_date must follow start_date")
            key = (ticker, start_date)
            if key in seen:
                raise ValueError(
                    f"span CSV row {row_number}: duplicate interval start for {ticker}"
                )
            seen.add(key)
            spans.append(EffectiveSpan(ticker, start_date, end_date))

    ordered = sorted(spans, key=lambda span: (span.ticker, span.start_date))
    previous: EffectiveSpan | None = None
    for span in ordered:
        if (
            previous is not None
            and previous.ticker == span.ticker
            and (previous.end_date is None or span.start_date < previous.end_date)
        ):
            raise ValueError(f"span CSV has overlapping intervals for {span.ticker}")
        previous = span
    return ordered


def load_universe_events_csv(
    path: str | Path,
    *,
    universe_id: str = "sp500",
    require_official_sources: bool = False,
) -> list[UniverseEvent]:
    """Load event-level membership changes with independent public dates."""
    csv_path = Path(path)
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        _validate_columns(
            reader,
            required=EVENT_REQUIRED_COLUMNS,
            optional=EVENT_OPTIONAL_COLUMNS,
            label="event",
        )
        events: list[UniverseEvent] = []
        seen: set[tuple[str, str, date, str]] = set()
        for row_number, raw in enumerate(reader, start=2):
            row = _normalize_csv_row(raw, row_number, "event")
            row_universe = row.get("universe_id") or universe_id
            if row_universe != universe_id:
                raise ValueError(
                    f"event CSV row {row_number}: universe_id {row_universe!r} "
                    f"does not match {universe_id!r}"
                )
            index_name = row.get("index_name", "")
            normalized_index = index_name.casefold().replace("&", "").replace(" ", "")
            if index_name and normalized_index != "sp500":
                raise ValueError(
                    f"event CSV row {row_number}: index_name must identify the S&P 500"
                )
            ticker = row["ticker"].upper()
            if not ticker:
                raise ValueError(f"event CSV row {row_number} needs ticker")
            action = row["action"].casefold()
            if action not in {"addition", "deletion"}:
                raise ValueError(
                    f"event CSV row {row_number}: action must be Addition or Deletion"
                )
            effective_date = _parse_date(
                row["effective_date"], "effective_date", row_number
            )
            known_date = _parse_date(row["known_date"], "known_date", row_number)
            if known_date > effective_date:
                raise ValueError(
                    f"event CSV row {row_number}: known_date follows effective_date"
                )
            source = row["source"]
            if not source:
                raise ValueError(f"event CSV row {row_number} needs source")
            source_kind = row.get("source_kind") or "index_announcement"
            if source_kind not in {"index_announcement", "issuer_announcement"}:
                raise ValueError(
                    f"event CSV row {row_number}: unsupported source_kind {source_kind!r}"
                )
            if require_official_sources:
                parsed = urlparse(source)
                if parsed.scheme != "https" or not parsed.hostname:
                    raise ValueError(
                        f"event CSV row {row_number}: source is not an HTTPS URL"
                    )
                if (
                    source_kind == "index_announcement"
                    and parsed.hostname != "press.spglobal.com"
                ):
                    raise ValueError(
                        f"event CSV row {row_number}: index announcement is not an official "
                        "press.spglobal.com URL"
                    )
            key = (row_universe, ticker, effective_date, action)
            if key in seen:
                raise ValueError(f"event CSV row {row_number}: duplicate event {key}")
            seen.add(key)
            events.append(
                UniverseEvent(
                    universe_id=row_universe,
                    ticker=ticker,
                    effective_date=effective_date,
                    action=action,
                    known_date=known_date,
                    source=source,
                    source_kind=source_kind,
                )
            )
    return sorted(
        events,
        key=lambda event: (
            event.effective_date,
            0 if event.action == "deletion" else 1,
            event.ticker,
        ),
    )


def reconcile_event_boundaries(
    spans: list[EffectiveSpan],
    events: list[UniverseEvent],
    *,
    coverage_start: date,
    coverage_end: date,
    date_tolerance_days: int = 3,
) -> BoundaryReconciliation:
    """Compare every post-baseline reference edge with the event manifest.

    Free reference files sometimes collapse staggered spin-off changes onto one
    date. Matching ticker/action edges within ``date_tolerance_days`` are kept
    as explicit conflicts; the official event date remains authoritative.
    """
    if coverage_end < coverage_start:
        raise ValueError("coverage_end cannot precede coverage_start")
    if date_tolerance_days < 0:
        raise ValueError("date_tolerance_days cannot be negative")
    expected: set[tuple[str, date, str]] = set()
    for span in spans:
        if coverage_start < span.start_date <= coverage_end:
            expected.add((span.ticker, span.start_date, "addition"))
        if span.end_date is not None and coverage_start < span.end_date <= coverage_end:
            expected.add((span.ticker, span.end_date, "deletion"))
    actual = {
        (event.ticker, event.effective_date, event.action)
        for event in events
        if coverage_start < event.effective_date <= coverage_end
    }
    missing = expected - actual
    unexpected = actual - expected
    conflicts: list[tuple[str, date, date, str]] = []
    for reference in sorted(tuple(missing), key=_boundary_sort_key):
        ticker, reference_date, action = reference
        candidates = [
            event
            for event in unexpected
            if event[0] == ticker
            and event[2] == action
            and abs((event[1] - reference_date).days) <= date_tolerance_days
        ]
        if not candidates:
            continue
        matched = min(candidates, key=lambda event: abs((event[1] - reference_date).days))
        missing.remove(reference)
        unexpected.remove(matched)
        conflicts.append((ticker, reference_date, matched[1], action))
    return BoundaryReconciliation(
        missing_events=tuple(sorted(missing, key=_boundary_sort_key)),
        unexpected_events=tuple(sorted(unexpected, key=_boundary_sort_key)),
        date_conflicts=tuple(
            sorted(conflicts, key=lambda item: (item[2], item[3], item[0]))
        ),
    )


def build_membership_from_events(
    spans: list[EffectiveSpan],
    events: list[UniverseEvent],
    *,
    coverage_start: date,
    coverage_end: date,
    universe_id: str,
    baseline_source: str,
    reconcile_reference: bool = True,
) -> list[dict]:
    """Build bounded intervals from a baseline snapshot plus announced events.

    The reference spans define the baseline and, by default, act as an
    independent boundary check. Events drive all changes after the baseline.
    Every still-active interval is closed the day after ``coverage_end`` so a
    backtest cannot silently run beyond the certified window.
    """
    if coverage_end < coverage_start:
        raise ValueError("coverage_end cannot precede coverage_start")
    if not universe_id or not baseline_source:
        raise ValueError("universe_id and baseline_source are required")
    if reconcile_reference:
        reconciliation = reconcile_event_boundaries(
            spans,
            events,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        )
        if not reconciliation.is_complete:
            raise ValueError(_reconciliation_error(reconciliation))

    baseline = {
        span.ticker
        for span in spans
        if span.start_date <= coverage_start
        and (span.end_date is None or span.end_date > coverage_start)
    }
    if not baseline:
        raise ValueError(f"reference spans have no active members on {coverage_start}")

    # ticker -> (effective_start, known_date, start_source)
    active: dict[str, tuple[date, date, str]] = {
        ticker: (coverage_start, coverage_start, f"baseline:{baseline_source}")
        for ticker in baseline
    }
    output: list[dict] = []
    in_window = [
        event
        for event in events
        if coverage_start < event.effective_date <= coverage_end
    ]
    for event in in_window:
        if event.universe_id != universe_id:
            raise ValueError(
                f"event universe {event.universe_id!r} does not match {universe_id!r}"
            )
        if event.action == "deletion":
            opened = active.pop(event.ticker, None)
            if opened is None:
                raise ValueError(
                    f"{event.effective_date}: deletion for inactive ticker {event.ticker}"
                )
            start_date, known_date, start_source = opened
            output.append(
                _membership_row(
                    universe_id,
                    event.ticker,
                    start_date,
                    event.effective_date,
                    known_date,
                    f"{start_source}|end:{event.source}",
                )
            )
        else:
            if event.ticker in active:
                raise ValueError(
                    f"{event.effective_date}: addition for active ticker {event.ticker}"
                )
            active[event.ticker] = (
                event.effective_date,
                event.known_date,
                f"start:{event.source}",
            )

    certified_end = date.fromordinal(coverage_end.toordinal() + 1)
    for ticker, (start_date, known_date, start_source) in active.items():
        output.append(
            _membership_row(
                universe_id,
                ticker,
                start_date,
                certified_end,
                known_date,
                f"{start_source}|coverage-end:{coverage_end.isoformat()}",
            )
        )
    return sorted(output, key=lambda row: (row["ticker"], row["effective_start"]))


def write_membership_csv(path: str | Path, rows: list[dict]) -> None:
    """Write import-ready membership rows after all validation has succeeded."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MEMBERSHIP_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **row,
                    "effective_start": row["effective_start"].isoformat(),
                    "effective_end": (
                        row["effective_end"].isoformat() if row["effective_end"] else ""
                    ),
                    "known_date": row["known_date"].isoformat(),
                }
            )


def load_membership_csv(
    path: str | Path,
    *,
    universe_id: str | None = None,
    source: str | None = None,
) -> list[dict]:
    """Parse a membership CSV into validated storage-ready rows."""
    csv_path = Path(path)
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        _validate_columns(
            reader,
            required=REQUIRED_COLUMNS,
            optional=OPTIONAL_COLUMNS,
            label="membership",
        )

        rows: list[dict] = []
        for row_number, raw in enumerate(reader, start=2):
            row = _normalize_csv_row(raw, row_number, "membership")
            row_universe = row.get("universe_id") or universe_id or ""
            row_source = row.get("source") or source or f"csv:{csv_path.name}"
            ticker = row.get("ticker", "").upper()
            if not row_universe or not ticker:
                raise ValueError(f"membership CSV row {row_number} needs universe_id and ticker")
            try:
                effective_start = _parse_date(row["effective_start"], "effective_start", row_number)
                known_date = _parse_date(row["known_date"], "known_date", row_number)
                effective_end = (
                    _parse_date(row["effective_end"], "effective_end", row_number)
                    if row.get("effective_end")
                    else None
                )
            except KeyError as exc:
                raise ValueError(f"membership CSV row {row_number} has an empty field") from exc
            if known_date > effective_start:
                raise ValueError(
                    f"membership CSV row {row_number}: known_date follows effective_start"
                )
            if effective_end is not None and effective_end <= effective_start:
                raise ValueError(
                    f"membership CSV row {row_number}: effective_end must follow start"
                )
            rows.append(
                {
                    "universe_id": row_universe.strip(),
                    "ticker": ticker,
                    "effective_start": effective_start,
                    "effective_end": effective_end,
                    "known_date": known_date,
                    "source": row_source.strip(),
                }
            )
    return rows


def ingest_membership_csv(
    path: str | Path,
    *,
    universe_id: str | None = None,
    source: str | None = None,
    store: Store | None = None,
) -> int:
    """Parse, upsert, and audit one membership CSV import."""
    db = store or get_store()
    started_at = datetime.now()
    run_id = str(uuid4())
    csv_path = Path(path)
    try:
        rows = load_membership_csv(
            csv_path,
            universe_id=universe_id,
            source=source,
        )
        inserted = db.upsert_universe_membership(rows)
        db.record_ingest(
            run_id=run_id,
            source=source or f"csv:{csv_path.name}",
            table_name="universe_membership",
            rows_inserted=inserted,
            started_at=started_at,
        )
        return inserted
    except Exception as exc:
        db.record_ingest(
            run_id=run_id,
            source=source or f"csv:{csv_path.name}",
            table_name="universe_membership",
            status="failed",
            error=str(exc),
            started_at=started_at,
        )
        raise


def _parse_date(value: str, field: str, row_number: int) -> date:
    if not value:
        raise ValueError(f"membership CSV row {row_number} needs {field}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"membership CSV row {row_number}: invalid {field} {value!r}") from exc


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


def _membership_row(
    universe_id: str,
    ticker: str,
    effective_start: date,
    effective_end: date | None,
    known_date: date,
    source: str,
) -> dict:
    return {
        "universe_id": universe_id,
        "ticker": ticker,
        "effective_start": effective_start,
        "effective_end": effective_end,
        "known_date": known_date,
        "source": source,
    }


def _boundary_sort_key(boundary: tuple[str, date, str]) -> tuple[date, str, str]:
    ticker, effective_date, action = boundary
    return effective_date, action, ticker


def _reconciliation_error(reconciliation: BoundaryReconciliation) -> str:
    parts = ["event manifest does not reconcile with reference membership boundaries"]
    if reconciliation.missing_events:
        sample = ", ".join(
            f"{ticker}:{action}@{effective_date}"
            for ticker, effective_date, action in reconciliation.missing_events[:5]
        )
        parts.append(
            f"missing {len(reconciliation.missing_events)} event(s), e.g. {sample}"
        )
    if reconciliation.unexpected_events:
        sample = ", ".join(
            f"{ticker}:{action}@{effective_date}"
            for ticker, effective_date, action in reconciliation.unexpected_events[:5]
        )
        parts.append(
            f"unexpected {len(reconciliation.unexpected_events)} event(s), e.g. {sample}"
        )
    if reconciliation.date_conflicts:
        sample = ", ".join(
            f"{ticker}:{action} reference {reference_date} vs event {event_date}"
            for ticker, reference_date, event_date, action in reconciliation.date_conflicts[:3]
        )
        parts.append(
            f"date conflicts {len(reconciliation.date_conflicts)}, e.g. {sample}"
        )
    return "; ".join(parts)

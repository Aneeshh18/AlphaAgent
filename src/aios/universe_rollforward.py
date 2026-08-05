"""Fail-closed, evidence-backed roll-forward for the current S&P 500 universe.

This module does not infer announcement dates and does not auto-approve index
changes. It can extend an unchanged reviewed reference window only when:

1. exact S&P Global press-archive responses are saved locally;
2. no unreviewed S&P 500 constituent-change headline exists through the target;
3. an independently maintained current-component file exactly matches the
   reviewed ticker set; and
4. its CIK values match the reviewed security-to-issuer references.

Any disagreement produces a review-required result and leaves every dated
reference table unchanged.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from aios.ingest.http_client import RawSnapshotContext, get_http
from aios.market_calendar import latest_completed_us_equity_session
from aios.paper import latest_reviewed_market_close
from aios.raw_snapshots import attach_parsed_rows_evidence
from aios.storage.store import Store, get_store

OFFICIAL_ARCHIVE_URL = "https://press.spglobal.com/index.php?s=2429&l=100"
COMPONENT_SNAPSHOT_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/sp500.csv"
)
NEW_YORK = ZoneInfo("America/New_York")
MAX_ARCHIVE_PAGES = 10
ARCHIVE_PAGE_SIZE = 100
MAX_ARCHIVE_STALENESS_DAYS = 14
MINIMUM_MEMBERS = 450
MAXIMUM_MEMBERS = 550
PRESS_ARCHIVE_PARSER_VERSION = "spglobal-press-archive-html-v1"
CHANGE_ANNOUNCEMENT_PARSER_VERSION = "spglobal-constituent-change-html-v1"
COMPONENT_SNAPSHOT_PARSER_VERSION = "sp500-components-csv-v1"
PRESS_ARCHIVE_CAPTURE_VERSION = "spglobal-press-archive-html-capture-v1"
CHANGE_ANNOUNCEMENT_CAPTURE_VERSION = "spglobal-constituent-change-html-capture-v1"
COMPONENT_SNAPSHOT_CAPTURE_VERSION = "sp500-components-csv-capture-v1"

FetchBytes = Callable[[str, RawSnapshotContext], bytes]


@dataclass(frozen=True)
class PressRelease:
    release_date: date
    title: str
    url: str

    def to_dict(self) -> dict[str, str]:
        return {
            "release_date": self.release_date.isoformat(),
            "title": self.title,
            "url": self.url,
        }


@dataclass(frozen=True)
class ComponentReference:
    ticker: str
    cik: str


@dataclass(frozen=True)
class ConstituentChange:
    """One strictly parsed S&P 500 row from an official announcement."""

    effective_date: date
    action: str
    company_name: str
    ticker: str

    def to_dict(self) -> dict[str, str]:
        return {
            "effective_date": self.effective_date.isoformat(),
            "action": self.action,
            "company_name": self.company_name,
            "ticker": self.ticker,
        }


@dataclass(frozen=True)
class UniverseRollForwardResult:
    """Operator-facing outcome of one bounded certification attempt."""

    status: str
    universe_id: str
    prior_coverage_through: str
    requested_coverage_through: str
    checked_at: str
    attestation_id: str | None
    run_id: str | None
    member_count: int
    official_release_count: int
    relevant_release_count: int
    identity_mismatch_count: int
    rows_extended: dict[str, int]
    detail: str

    @property
    def review_required(self) -> bool:
        return self.status == "review_required"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def roll_forward_sp500_coverage(
    *,
    store: Store | None = None,
    now: datetime | None = None,
    project_root: Path | None = None,
    fetch_bytes: FetchBytes | None = None,
) -> UniverseRollForwardResult:
    """Extend unchanged S&P 500/reference coverage or stop for human review."""
    db = store or get_store()
    checked_at = _aware_utc(now or datetime.now(UTC))
    root = Path(project_root).resolve() if project_root else None
    universe_id = "sp500"
    completed_new_york_date = latest_completed_us_equity_session(checked_at)
    prior, members = _bounded_coverage_boundary(db, universe_id)
    target = latest_reviewed_market_close(db, today=completed_new_york_date)
    if target <= prior:
        return UniverseRollForwardResult(
            status="up_to_date",
            universe_id=universe_id,
            prior_coverage_through=prior.isoformat(),
            requested_coverage_through=prior.isoformat(),
            checked_at=checked_at.isoformat(),
            attestation_id=None,
            run_id=None,
            member_count=len(members),
            official_release_count=0,
            relevant_release_count=0,
            identity_mismatch_count=0,
            rows_extended={},
            detail="Certified universe coverage already reaches the newest eligible market close.",
        )

    run_id = str(uuid4())
    attestation_id = f"uca-{uuid4().hex}"
    started_at = checked_at
    getter = fetch_bytes or _fetch_bytes
    try:
        releases = _fetch_official_archive(
            getter,
            db,
            run_id,
            prior=prior,
            checked_at=checked_at,
            completed_new_york_date=completed_new_york_date,
            project_root=root,
        )
        components = _fetch_component_snapshot(
            getter,
            db,
            run_id,
            checked_at=checked_at,
            project_root=root,
        )
        references, identity_issues = _reviewed_references(db, universe_id, prior)
        reviewed_tickers = {str(row["ticker"]) for row in references}
        component_by_ticker = {row.ticker: row for row in components}
        component_tickers = set(component_by_ticker)
        missing_components = sorted(reviewed_tickers - component_tickers)
        unexpected_components = sorted(component_tickers - reviewed_tickers)

        cik_mismatches: list[dict[str, str]] = []
        successor_lineage_matches: list[dict[str, object]] = []
        for reference in references:
            ticker = str(reference["ticker"])
            component = component_by_ticker.get(ticker)
            if component is None:
                continue
            lineage_ciks = {str(value) for value in reference["lineage_ciks"]}
            if component.cik not in lineage_ciks:
                cik_mismatches.append(
                    {
                        "ticker": ticker,
                        "active_reviewed_cik": str(reference["cik"]),
                        "component_cik": component.cik,
                    }
                )
            elif component.cik != str(reference["cik"]):
                successor_lineage_matches.append(
                    {
                        "ticker": ticker,
                        "active_reviewed_cik": str(reference["cik"]),
                        "component_cik": component.cik,
                        "reviewed_lineage_ciks": sorted(lineage_ciks),
                    }
                )

        reviewed_sources = "\n".join(
            str(row["source"])
            for row in db.query(
                "SELECT DISTINCT source FROM universe_membership WHERE universe_id = ?",
                (universe_id,),
            )
        )
        candidates = [
            release
            for release in releases
            if release.release_date <= target
            and is_constituent_change_title(release.title)
            and not _source_contains_release_url(reviewed_sources, release.url)
        ]
        blocking_candidates, future_changes, candidate_errors = (
            _classify_candidate_release_timing(
                getter,
                db,
                run_id,
                candidates=candidates,
                target=target,
                project_root=root,
            )
        )
        mismatch_detail = {
            "missing_from_component_snapshot": missing_components[:100],
            "unexpected_in_component_snapshot": unexpected_components[:100],
            "reference_identity_issues": identity_issues[:100],
            "cik_mismatches": cik_mismatches[:100],
            "reviewed_successor_lineage_matches": successor_lineage_matches[:100],
            "future_effective_releases": future_changes[:100],
            "candidate_release_parse_errors": candidate_errors[:100],
        }
        mismatch_count = (
            len(missing_components)
            + len(unexpected_components)
            + len(identity_issues)
            + len(cik_mismatches)
        )
        accepted = not blocking_candidates and mismatch_count == 0
        status = "accepted_no_change" if accepted else "blocked_review_required"
        if accepted and future_changes:
            detail = (
                "No constituent change is effective through the requested date and "
                "no reference drift was found. Future official changes remain pending "
                "formal event import."
            )
        elif accepted:
            detail = (
                "No unreviewed constituent-change announcement or reference drift "
                "was found."
            )
        else:
            detail = _blocked_detail(blocking_candidates, mismatch_count)
        matching_identities = max(0, len(references) - len(identity_issues) - len(cik_mismatches))
        attestation = {
            "attestation_id": attestation_id,
            "run_id": run_id,
            "universe_id": universe_id,
            "prior_coverage_through": prior,
            "requested_coverage_through": target,
            "checked_at": checked_at,
            "completed_new_york_date": completed_new_york_date,
            "status": status,
            "official_source_url": OFFICIAL_ARCHIVE_URL,
            "component_source_url": COMPONENT_SNAPSHOT_URL,
            "official_release_count": len(releases),
            "relevant_release_count": len(blocking_candidates),
            "reviewed_member_count": len(references),
            "component_count": len(components),
            "reviewed_member_set_sha256": _set_sha256(reviewed_tickers),
            "component_set_sha256": _set_sha256(component_tickers),
            "identity_match_count": matching_identities,
            "identity_mismatch_count": mismatch_count,
            "candidate_releases_json": _canonical_json(
                [release.to_dict() for release in blocking_candidates]
            ),
            "mismatch_detail_json": _canonical_json(mismatch_detail),
            "detail": detail,
        }
        counts = db.apply_universe_coverage_attestation(attestation, references)
        db.record_ingest(
            run_id=run_id,
            source="spglobal+fja05680",
            table_name="universe_coverage_attestations",
            rows_inserted=1 if accepted else 0,
            rows_rejected=(
                0
                if accepted
                else max(1, len(blocking_candidates) + mismatch_count)
            ),
            started_at=started_at,
            finished_at=datetime.now(UTC),
            status="success" if accepted else "warning",
            error=None if accepted else detail,
        )
        return UniverseRollForwardResult(
            status="extended" if accepted else "review_required",
            universe_id=universe_id,
            prior_coverage_through=prior.isoformat(),
            requested_coverage_through=target.isoformat(),
            checked_at=checked_at.isoformat(),
            attestation_id=attestation_id,
            run_id=run_id,
            member_count=len(references),
            official_release_count=len(releases),
            relevant_release_count=len(blocking_candidates),
            identity_mismatch_count=mismatch_count,
            rows_extended=counts,
            detail=detail,
        )
    except Exception as exc:
        db.record_ingest(
            run_id=run_id,
            source="spglobal+fja05680",
            table_name="universe_coverage_attestations",
            started_at=started_at,
            finished_at=datetime.now(UTC),
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise


def parse_press_archive(payload: bytes) -> list[PressRelease]:
    """Parse the public S&P Global release archive without optional HTML packages."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("S&P press archive is not valid UTF-8") from exc
    parser = _PressArchiveParser()
    parser.feed(text)
    parser.close()
    if parser.in_item:
        raise ValueError("S&P press archive ended inside a release item")
    releases: dict[str, PressRelease] = {}
    for item in parser.items:
        raw_date = " ".join(item["date"].split())
        title = " ".join(unescape(item["title"]).split())
        url = urljoin(OFFICIAL_ARCHIVE_URL, unescape(item["url"]).strip())
        try:
            release_date = datetime.strptime(raw_date, "%b %d, %Y").date()
        except ValueError as exc:
            raise ValueError(f"invalid S&P press release date: {raw_date!r}") from exc
        split = urlsplit(url)
        if split.scheme != "https" or split.hostname != "press.spglobal.com":
            raise ValueError(f"unexpected S&P press release URL: {url}")
        if not title:
            raise ValueError("S&P press archive contains an empty release title")
        releases[url] = PressRelease(release_date, title, url)
    if not releases:
        raise ValueError("S&P press archive returned no release items")
    return sorted(releases.values(), key=lambda row: (row.release_date, row.url), reverse=True)


def parse_sp500_constituent_changes(payload: bytes) -> list[ConstituentChange]:
    """Strictly parse dated S&P 500 action rows from an official release page."""

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("S&P change announcement is not valid UTF-8") from exc
    parser = _ReleaseTableParser()
    parser.feed(text)
    parser.close()
    changes: list[ConstituentChange] = []
    for raw_cells in parser.rows:
        cells = [" ".join(unescape(cell).replace("\xa0", " ").split()) for cell in raw_cells]
        normalized_cells = [cell.casefold().replace(" ", "") for cell in cells]
        index_positions = [
            position
            for position, cell in enumerate(normalized_cells)
            if cell == "s&p500"
        ]
        sp_index_positions = [
            position
            for position, cell in enumerate(normalized_cells)
            if cell.startswith("s&p")
        ]
        action_positions = [
            position
            for position, cell in enumerate(cells)
            if cell.casefold() in {"addition", "deletion"}
        ]
        if not index_positions and not action_positions:
            continue
        if not index_positions:
            if (
                len(action_positions) != 1
                or len(sp_index_positions) != 1
                or sp_index_positions[0] + 1 != action_positions[0]
            ):
                raise ValueError("S&P change table has an ambiguous index/action row")
            # Official multi-index announcements share one table. Other named
            # S&P indices are outside this parser's S&P 500 scope.
            continue
        if (
            len(index_positions) != 1
            or len(sp_index_positions) != 1
            or len(action_positions) != 1
        ):
            raise ValueError("S&P 500 change table has an ambiguous index/action row")
        action_position = action_positions[0]
        if index_positions[0] + 1 != action_position:
            raise ValueError("S&P 500 change table has an ambiguous index/action row")
        if action_position + 2 >= len(cells):
            raise ValueError("S&P 500 change table row is incomplete")
        date_cells = [
            cell
            for cell in cells
            if re.fullmatch(
                r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
                r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|"
                r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?) \d{1,2}, \d{4}",
                cell,
            )
        ]
        if len(date_cells) != 1:
            raise ValueError("S&P 500 change table row has no unique effective date")
        effective_date = None
        for date_format in ("%B %d, %Y", "%b %d, %Y"):
            try:
                effective_date = datetime.strptime(date_cells[0], date_format).date()
                break
            except ValueError:
                continue
        if effective_date is None:
            raise ValueError("S&P 500 change table has an invalid effective date")
        ticker = cells[action_position + 2].upper()
        if not re.fullmatch(r"[A-Z0-9.]+", ticker):
            raise ValueError("S&P 500 change table has an invalid ticker")
        changes.append(
            ConstituentChange(
                effective_date=effective_date,
                action=cells[action_position].casefold(),
                company_name=cells[action_position + 1],
                ticker=ticker,
            )
        )
    if not changes:
        raise ValueError("official announcement has no strict S&P 500 change rows")
    additions = sum(change.action == "addition" for change in changes)
    deletions = sum(change.action == "deletion" for change in changes)
    if additions != deletions:
        raise ValueError("S&P 500 change table additions and deletions are unbalanced")
    keys = {
        (change.effective_date, change.action, change.ticker)
        for change in changes
    }
    if len(keys) != len(changes):
        raise ValueError("S&P 500 change table repeats an event row")
    return sorted(
        changes,
        key=lambda change: (
            change.effective_date,
            change.action,
            change.ticker,
        ),
    )


def parse_component_snapshot(payload: bytes) -> list[ComponentReference]:
    """Parse and strictly validate the independent current-component CSV."""
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("component snapshot is not valid UTF-8") from exc
    reader = csv.DictReader(StringIO(text))
    if reader.fieldnames is None or not {"Symbol", "CIK"}.issubset(reader.fieldnames):
        raise ValueError("component snapshot requires Symbol and CIK columns")
    rows: list[ComponentReference] = []
    seen: set[str] = set()
    for line_number, raw in enumerate(reader, 2):
        if None in raw:
            raise ValueError(f"component snapshot row {line_number} has extra fields")
        ticker = str(raw.get("Symbol") or "").strip().upper()
        cik = _normalize_cik(raw.get("CIK"), line_number)
        if not ticker or not re.fullmatch(r"[A-Z0-9.]+", ticker):
            raise ValueError(f"component snapshot row {line_number} has invalid Symbol")
        if ticker in seen:
            raise ValueError(f"component snapshot repeats ticker {ticker}")
        seen.add(ticker)
        rows.append(ComponentReference(ticker, cik))
    if not MINIMUM_MEMBERS <= len(rows) <= MAXIMUM_MEMBERS:
        raise ValueError(
            f"component snapshot has {len(rows)} rows; expected "
            f"{MINIMUM_MEMBERS}-{MAXIMUM_MEMBERS}"
        )
    return sorted(rows, key=lambda row: row.ticker)


def _fetch_official_archive(
    getter: FetchBytes,
    store: Store,
    run_id: str,
    *,
    prior: date,
    checked_at: datetime,
    completed_new_york_date: date,
    project_root: Path | None,
) -> list[PressRelease]:
    releases: dict[str, PressRelease] = {}
    reached_prior = False
    for page in range(MAX_ARCHIVE_PAGES):
        url = sp500_archive_page_url(page)
        role = sp500_archive_page_role(page)
        payload = getter(
            url,
            RawSnapshotContext(
                provider="spglobal",
                dataset="sp500_press_archive",
                store=store,
                ingest_run_id=run_id,
                role=role,
                adapter_name="spglobal_press_archive_html",
                adapter_version="1",
                parser_version=PRESS_ARCHIVE_CAPTURE_VERSION,
                project_root=project_root,
            ),
        )
        page_releases = parse_press_archive(payload)
        attach_parsed_rows_evidence(
            store=store,
            ingest_run_id=run_id,
            role=role,
            capture_parser_version=PRESS_ARCHIVE_CAPTURE_VERSION,
            parser_version=PRESS_ARCHIVE_PARSER_VERSION,
            parsed_rows=[row.to_dict() for row in page_releases],
        )
        releases.update((row.url, row) for row in page_releases)
        if min(row.release_date for row in page_releases) <= prior:
            reached_prior = True
            break
        if len(page_releases) < ARCHIVE_PAGE_SIZE:
            break
    if not reached_prior:
        raise ValueError(
            "S&P press archive pages do not reach the existing certification boundary"
        )
    result = sorted(
        (
            release
            for release in releases.values()
            if release.release_date <= completed_new_york_date
        ),
        key=lambda row: (row.release_date, row.url),
        reverse=True,
    )
    if not result:
        raise ValueError("S&P press archive has no releases through the completed New York day")
    newest = max(row.release_date for row in result)
    if (completed_new_york_date - newest).days > MAX_ARCHIVE_STALENESS_DAYS:
        raise ValueError(
            f"S&P press archive appears stale: newest release is {newest.isoformat()}"
        )
    if any(row.release_date > checked_at.astimezone(NEW_YORK).date() for row in releases.values()):
        raise ValueError("S&P press archive contains a future-dated release")
    return result


def _fetch_component_snapshot(
    getter: FetchBytes,
    store: Store,
    run_id: str,
    *,
    checked_at: datetime,
    project_root: Path | None,
) -> list[ComponentReference]:
    del checked_at  # The raw snapshot layer records the actual request/receive timestamps.
    role = "independent_component_snapshot"
    payload = getter(
        COMPONENT_SNAPSHOT_URL,
        RawSnapshotContext(
            provider="github",
            dataset="sp500_current_components",
            store=store,
            ingest_run_id=run_id,
            role=role,
            adapter_name="fja05680_sp500_csv",
            adapter_version="1",
            parser_version=COMPONENT_SNAPSHOT_CAPTURE_VERSION,
            project_root=project_root,
        ),
    )
    components = parse_component_snapshot(payload)
    attach_parsed_rows_evidence(
        store=store,
        ingest_run_id=run_id,
        role=role,
        capture_parser_version=COMPONENT_SNAPSHOT_CAPTURE_VERSION,
        parser_version=COMPONENT_SNAPSHOT_PARSER_VERSION,
        parsed_rows=[asdict(row) for row in components],
    )
    return components


def _fetch_bytes(url: str, context: RawSnapshotContext) -> bytes:
    return get_http().get_bytes(url, raw_snapshot=context)


def _source_contains_release_url(sources: str, release_url: str) -> bool:
    """Match one official release despite equivalent percent-encoding.

    Membership provenance stores labeled URLs inside pipe-delimited source
    strings. The press archive has emitted both literal and percent-encoded
    commas for the same release path, so bytewise substring matching can
    manufacture a historical review gap. Only canonical S&P Global HTTPS URLs
    participate in this equivalence check.
    """

    expected = _canonical_spglobal_release_url(release_url)
    if expected is None:
        return False
    for candidate in re.findall(r"https://press\.spglobal\.com/[^|\s]+", sources):
        if _canonical_spglobal_release_url(candidate) == expected:
            return True
    return False


def _canonical_spglobal_release_url(value: str) -> str | None:
    split = urlsplit(unescape(value).strip())
    if split.scheme.casefold() != "https" or split.hostname != "press.spglobal.com":
        return None
    if split.username or split.password or split.port is not None or split.fragment:
        return None
    normalized_path = quote(unquote(split.path), safe="/-._~")
    return urlunsplit(("https", "press.spglobal.com", normalized_path, split.query, ""))


def _classify_candidate_release_timing(
    getter: FetchBytes,
    store: Store,
    run_id: str,
    *,
    candidates: list[PressRelease],
    target: date,
    project_root: Path | None,
) -> tuple[list[PressRelease], list[dict[str, object]], list[dict[str, str]]]:
    """Separate already-effective blockers from strictly future changes.

    A release title alone never advances coverage.  Exact official detail is
    captured and its S&P 500 table must parse completely.  A parsed event can
    permit unchanged coverage only while every effective date remains later
    than the requested decision close.  The release stays unreviewed and will
    be reconsidered on every roll-forward until its event rows are imported.
    """

    blocking: list[PressRelease] = []
    future: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    for position, release in enumerate(candidates):
        try:
            role = f"candidate_release_detail_{position:03d}"
            payload = getter(
                release.url,
                RawSnapshotContext(
                    provider="spglobal",
                    dataset="sp500_change_announcement",
                    store=store,
                    ingest_run_id=run_id,
                    role=role,
                    adapter_name="spglobal_constituent_change_html",
                    adapter_version="1",
                    parser_version=CHANGE_ANNOUNCEMENT_CAPTURE_VERSION,
                    project_root=project_root,
                ),
            )
            changes = parse_sp500_constituent_changes(payload)
            attach_parsed_rows_evidence(
                store=store,
                ingest_run_id=run_id,
                role=role,
                capture_parser_version=CHANGE_ANNOUNCEMENT_CAPTURE_VERSION,
                parser_version=CHANGE_ANNOUNCEMENT_PARSER_VERSION,
                parsed_rows=[change.to_dict() for change in changes],
            )
        except Exception as exc:
            blocking.append(release)
            errors.append(
                {
                    "release_url": release.url,
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                }
            )
            continue
        if all(change.effective_date > target for change in changes):
            future.append(
                {
                    **release.to_dict(),
                    "changes": [change.to_dict() for change in changes],
                }
            )
        else:
            blocking.append(release)
    return blocking, future, errors


def _bounded_coverage_boundary(
    store: Store,
    universe_id: str,
) -> tuple[date, list[dict]]:
    row = store.query(
        """
        SELECT MAX(effective_end) AS boundary_end,
               COUNT(*) FILTER (WHERE effective_end IS NULL) AS open_rows
        FROM universe_membership
        WHERE universe_id = ?
        """,
        (universe_id,),
    )[0]
    boundary_end = row["boundary_end"]
    if not isinstance(boundary_end, date):
        raise ValueError(f"{universe_id} has no finite certified coverage boundary")
    if int(row["open_rows"]) != 0:
        raise ValueError(f"{universe_id} contains unbounded membership rows")
    prior = boundary_end - timedelta(days=1)
    members = store.universe_membership_on(universe_id, prior)
    if not MINIMUM_MEMBERS <= len(members) <= MAXIMUM_MEMBERS:
        raise ValueError(
            f"{universe_id} boundary has {len(members)} members; expected "
            f"{MINIMUM_MEMBERS}-{MAXIMUM_MEMBERS}"
        )
    if any(
        row["effective_end"] != boundary_end or row["end_known_date"] != prior
        for row in members
    ):
        raise ValueError(f"{universe_id} membership rows do not share one bounded edge")
    return prior, members


def _reviewed_references(
    store: Store,
    universe_id: str,
    as_of: date,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    rows = store.query(
        """
        WITH members AS (
            SELECT ticker, security_id
            FROM universe_membership
            WHERE universe_id = ?
              AND known_date <= CAST(? AS DATE)
              AND effective_start <= CAST(? AS DATE)
              AND (
                  effective_end IS NULL
                  OR effective_end > CAST(? AS DATE)
                  OR end_known_date > CAST(? AS DATE)
              )
        ), identities AS (
            SELECT member.ticker,
                   COUNT(DISTINCT identity.security_id) AS identity_count,
                   MIN(identity.security_id) AS identity_security_id
            FROM members AS member
            LEFT JOIN security_identity_assignments AS identity
              ON identity.universe_id = ?
             AND identity.ticker = member.ticker
             AND identity.known_date <= CAST(? AS DATE)
             AND identity.effective_start <= CAST(? AS DATE)
             AND (identity.effective_end IS NULL OR identity.effective_end > CAST(? AS DATE))
            GROUP BY member.ticker
        ), owners AS (
            SELECT member.security_id,
                   COUNT(DISTINCT owner.issuer_id) AS owner_count,
                   MIN(owner.issuer_id) AS issuer_id
            FROM members AS member
            LEFT JOIN security_issuer_assignments AS owner
              ON owner.security_id = member.security_id
             AND owner.effective_start <= CAST(? AS DATE)
             AND (owner.effective_end IS NULL OR owner.effective_end > CAST(? AS DATE))
            GROUP BY member.security_id
        ), ciks AS (
            SELECT owner.issuer_id,
                   COUNT(DISTINCT cik.cik) AS cik_count,
                   MIN(cik.cik) AS cik
            FROM owners AS owner
            LEFT JOIN issuer_cik_history AS cik
              ON cik.issuer_id = owner.issuer_id
             AND cik.effective_start <= CAST(? AS DATE)
             AND (cik.effective_end IS NULL OR cik.effective_end > CAST(? AS DATE))
            GROUP BY owner.issuer_id
        ), providers AS (
            SELECT member.security_id,
                   COUNT(DISTINCT mapping.provider || ':' || mapping.provider_symbol)
                       AS provider_count
            FROM members AS member
            LEFT JOIN provider_symbol_history AS mapping
              ON mapping.security_id = member.security_id
             AND mapping.mapping_status = 'verified'
             AND mapping.data_start <= CAST(? AS DATE)
             AND (mapping.data_end IS NULL OR mapping.data_end > CAST(? AS DATE))
            GROUP BY member.security_id
        )
        SELECT member.ticker, member.security_id,
               identity.identity_count, identity.identity_security_id,
               owner.owner_count, owner.issuer_id,
               cik.cik_count, cik.cik,
               provider.provider_count
        FROM members AS member
        LEFT JOIN identities AS identity USING (ticker)
        LEFT JOIN owners AS owner USING (security_id)
        LEFT JOIN ciks AS cik USING (issuer_id)
        LEFT JOIN providers AS provider USING (security_id)
        ORDER BY member.ticker
        """,
        (
            universe_id,
            as_of.isoformat(),
            as_of.isoformat(),
            as_of.isoformat(),
            as_of.isoformat(),
            universe_id,
            as_of.isoformat(),
            as_of.isoformat(),
            as_of.isoformat(),
            as_of.isoformat(),
            as_of.isoformat(),
            as_of.isoformat(),
            as_of.isoformat(),
            as_of.isoformat(),
            as_of.isoformat(),
        ),
    )
    accepted: list[dict[str, object]] = []
    issues: list[dict[str, object]] = []
    for row in rows:
        valid = (
            row["security_id"]
            and int(row["identity_count"] or 0) == 1
            and row["identity_security_id"] == row["security_id"]
            and int(row["owner_count"] or 0) == 1
            and row["issuer_id"]
            and int(row["cik_count"] or 0) == 1
            and row["cik"]
            and int(row["provider_count"] or 0) >= 1
        )
        if not valid:
            issues.append(
                {
                    "ticker": row["ticker"],
                    "identity_count": int(row["identity_count"] or 0),
                    "owner_count": int(row["owner_count"] or 0),
                    "cik_count": int(row["cik_count"] or 0),
                    "provider_count": int(row["provider_count"] or 0),
                }
            )
            continue
        accepted.append(
            {
                "ticker": str(row["ticker"]),
                "security_id": str(row["security_id"]),
                "issuer_id": str(row["issuer_id"]),
                "cik": str(row["cik"]),
            }
        )
    lineage = _reviewed_lineage_ciks(
        store,
        [str(row["security_id"]) for row in accepted],
    )
    for reference in accepted:
        reference["lineage_ciks"] = sorted(
            lineage.get(str(reference["security_id"]), {str(reference["cik"])})
        )
    return accepted, issues


def sp500_archive_page_url(page: int) -> str:
    """Return the exact reviewed S&P press-archive URL for one page."""

    if (
        isinstance(page, bool)
        or not isinstance(page, int)
        or not 0 <= page < MAX_ARCHIVE_PAGES
    ):
        raise ValueError("S&P press archive page is out of bounds")
    return (
        OFFICIAL_ARCHIVE_URL
        if page == 0
        else f"{OFFICIAL_ARCHIVE_URL}&o={page * ARCHIVE_PAGE_SIZE}"
    )


def sp500_archive_page_role(page: int) -> str:
    """Return the canonical evidence role for one S&P archive page."""

    if (
        isinstance(page, bool)
        or not isinstance(page, int)
        or not 0 <= page < MAX_ARCHIVE_PAGES
    ):
        raise ValueError("S&P press archive page is out of bounds")
    return f"official_release_archive_page_{page:03d}"


def is_constituent_change_title(title: str) -> bool:
    normalized = " ".join(unescape(title).casefold().split())
    mentions_sp500 = bool(re.search(r"\bs\s*&\s*p\s*500\b", normalized))
    mentions_us_indices = "s&p u.s. indices" in normalized
    change_language = bool(
        re.search(
            r"\b(join|joins|joining|replace|replaces|replacement|"
            r"added|addition|remove|removed|deletion|constituent|rebalance|changes)\b",
            normalized,
        )
    )
    return change_language and (mentions_sp500 or mentions_us_indices)


def _blocked_detail(
    candidates: list[PressRelease],
    mismatch_count: int,
) -> str:
    parts: list[str] = []
    if candidates:
        parts.append(f"{len(candidates)} unreviewed official change announcement(s)")
    if mismatch_count:
        parts.append(f"{mismatch_count} component/identity mismatch(es)")
    return "; ".join(parts) + ". Manual event review is required; no dates were extended."


def _reviewed_lineage_ciks(
    store: Store,
    security_ids: list[str],
) -> dict[str, set[str]]:
    """Return all reviewed issuer CIKs ever attached to each stable security."""
    if not security_ids:
        return {}
    placeholders = ", ".join("?" for _ in security_ids)
    rows = store.query(
        f"""
        SELECT DISTINCT owner.security_id, cik.cik
        FROM security_issuer_assignments AS owner
        JOIN issuer_cik_history AS cik USING (issuer_id)
        WHERE owner.security_id IN ({placeholders})
        """,
        tuple(security_ids),
    )
    lineage: dict[str, set[str]] = {}
    for row in rows:
        lineage.setdefault(str(row["security_id"]), set()).add(str(row["cik"]))
    return lineage


def _normalize_cik(value: object, line_number: int) -> str:
    raw = str(value or "").strip()
    if not raw.isdigit() or len(raw) > 10:
        raise ValueError(f"component snapshot row {line_number} has invalid CIK")
    return raw.zfill(10)


def _set_sha256(values: set[str]) -> str:
    return hashlib.sha256("\n".join(sorted(values)).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("universe coverage check time must be timezone-aware")
    return value.astimezone(UTC)


class _PressArchiveParser(HTMLParser):
    """Extract only ``li.wd_item`` date/title/link fields from the official page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, str]] = []
        self._item: dict[str, str] | None = None
        self._field: str | None = None
        self._field_depth = 0

    @property
    def in_item(self) -> bool:
        return self._item is not None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "li" and "wd_item" in classes:
            if self._item is not None:
                raise ValueError("nested S&P press release items are not supported")
            self._item = {"date": "", "title": "", "url": ""}
            return
        if self._item is None:
            return
        if self._field is not None:
            self._field_depth += 1
            if self._field == "title" and tag == "a" and attributes.get("href"):
                self._item["url"] = str(attributes["href"])
            return
        if tag == "div" and "wd_date" in classes:
            self._field = "date"
            self._field_depth = 1
        elif tag == "div" and "wd_title" in classes:
            self._field = "title"
            self._field_depth = 1

    def handle_data(self, data: str) -> None:
        if self._item is not None and self._field is not None:
            self._item[self._field] += data

    def handle_endtag(self, tag: str) -> None:
        if self._item is None:
            return
        if self._field is not None:
            self._field_depth -= 1
            if self._field_depth == 0:
                self._field = None
            return
        if tag == "li":
            if not all(self._item.values()):
                raise ValueError("S&P press archive contains an incomplete release item")
            self.items.append(self._item)
            self._item = None


class _ReleaseTableParser(HTMLParser):
    """Collect text cells from official release tables without HTML dependencies."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "tr":
            if self._row is not None:
                raise ValueError("nested S&P change table rows are not supported")
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            if self._cell is not None:
                raise ValueError("nested S&P change table cells are not supported")
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None:
            if self._row is None:
                raise ValueError("S&P change table cell ended outside a row")
            self._row.append("".join(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._cell is not None:
                raise ValueError("S&P change table row ended inside a cell")
            if self._row:
                self.rows.append(self._row)
            self._row = None

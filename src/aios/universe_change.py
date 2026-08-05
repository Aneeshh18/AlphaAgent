"""Read-only semantic CAS snapshots for governed constituent changes.

This module deliberately contains no network, paper, portfolio, execution, or
broker surface.  It captures the exact bounded membership/reference state that
a future content-addressed activation plan must compare-and-set inside one
DuckDB transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from aios.market_calendar import us_equity_sessions
from aios.raw_snapshots import (
    VerifiedRawSnapshot,
    canonical_request_fingerprint,
    read_verified_raw_snapshot,
)
from aios.storage.store import Store
from aios.universe_rollforward import (
    ARCHIVE_PAGE_SIZE,
    CHANGE_ANNOUNCEMENT_PARSER_VERSION,
    COMPONENT_SNAPSHOT_PARSER_VERSION,
    COMPONENT_SNAPSHOT_URL,
    MAX_ARCHIVE_PAGES,
    MAX_ARCHIVE_STALENESS_DAYS,
    OFFICIAL_ARCHIVE_URL,
    PRESS_ARCHIVE_PARSER_VERSION,
    is_constituent_change_title,
    parse_component_snapshot,
    parse_press_archive,
    parse_sp500_constituent_changes,
    sp500_archive_page_url,
)

UNIVERSE_CHANGE_STATE_SCHEMA_VERSION = "universe-change-state.v1"
UNIVERSE_CHANGE_PLAN_SCHEMA_VERSION = "universe-change-plan.v1"
UNIVERSE_CHANGE_PLAN_POLICY_VERSION = "governed-universe-change-plan.v1"
UNIVERSE_CHANGE_EVENT_SCHEMA_VERSION = "sp500-constituent-event.v1"

_ARCHIVE_ROLE = re.compile(r"^official_release_archive_page_([0-9]{3})$")
_MAX_PAPER_FILES = 10_000
_MAX_PAPER_ENTRIES = 20_000
_MAX_PAPER_DEPTH = 12
_MAX_PAPER_RELATIVE_PATH_BYTES = 2_048
_MAX_PAPER_FILE_BYTES = 16 * 1024 * 1024
_MAX_PAPER_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_LEGACY_TIMESTAMP_ZONE_SKEW = timedelta(hours=18)


@dataclass(frozen=True)
class UniverseChangeStateSnapshot:
    """One deterministic semantic snapshot over bounded universe references."""

    universe_id: str
    coverage_through: str
    member_count: int
    member_set_sha256: str
    security_set_sha256: str
    state_sha256: str
    _canonical_payload_json: str

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self._canonical_payload_json)


@dataclass(frozen=True)
class RawEvidenceExpectation:
    """Exact immutable run/role observation required by a change plan."""

    run_id: str
    role: str
    snapshot_id: str
    source_url: str
    provider: str
    dataset: str
    artifact_kind: str
    parser_version: str
    request_fingerprint: str
    adapter_name: str
    adapter_version: str


@dataclass(frozen=True)
class UniverseChangePlan:
    """One deterministic, non-executable constituent-change plan."""

    plan_sha256: str
    event_id: str
    universe_id: str
    announcement_date: str
    effective_date: str
    prior_coverage_through: str
    requested_coverage_through: str
    activation_available: bool
    _canonical_payload_json: str

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self._canonical_payload_json)


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    links: int
    size: int
    modified_ns: int
    changed_ns: int


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _required_os_flag(name: str) -> int:
    value = getattr(os, name, None)
    if not isinstance(value, int) or value == 0:
        raise RuntimeError(f"secure universe-change planning requires os.{name}")
    return value


def _open_absolute_parent(path: Path) -> tuple[int, str]:
    absolute = _absolute_without_resolving(path)
    if absolute == Path(absolute.anchor):
        raise ValueError("filesystem root cannot be a governed file")
    if os.open not in os.supports_dir_fd:
        raise RuntimeError("secure universe-change planning requires dir_fd support")
    directory_flags = (
        os.O_RDONLY
        | _required_os_flag("O_DIRECTORY")
        | _required_os_flag("O_NOFOLLOW")
        | _required_os_flag("O_CLOEXEC")
    )
    descriptor = os.open(absolute.anchor, directory_flags)
    try:
        for component in absolute.parent.parts[1:]:
            if component in {"", ".", ".."} or Path(component).name != component:
                raise ValueError("governed path contains an unsafe component")
            child = os.open(component, directory_flags, dir_fd=descriptor)
            metadata = os.fstat(child)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(child)
                raise ValueError("governed path ancestor is not a directory")
            os.close(descriptor)
            descriptor = child
        return descriptor, absolute.name
    except OSError as exc:
        os.close(descriptor)
        raise ValueError("governed path is missing or unsafe") from exc
    except Exception:
        os.close(descriptor)
        raise


def _open_secure_file(path: Path) -> tuple[int, int, str]:
    parent, filename = _open_absolute_parent(path)
    flags = (
        os.O_RDONLY
        | _required_os_flag("O_NONBLOCK")
        | _required_os_flag("O_NOFOLLOW")
        | _required_os_flag("O_CLOEXEC")
    )
    try:
        descriptor = os.open(filename, flags, dir_fd=parent)
        opened = os.fstat(descriptor)
        current = os.stat(filename, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            os.close(descriptor)
            raise ValueError("governed file must be one regular unaliased file")
        return parent, descriptor, filename
    except OSError as exc:
        os.close(parent)
        raise ValueError("governed file is missing or unsafe") from exc
    except Exception:
        os.close(parent)
        raise


def _file_identity(metadata: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        links=metadata.st_nlink,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
    )


def _file_identity_tuple(identity: _FileIdentity) -> tuple[int, int, int, int, int, int]:
    return (
        identity.device,
        identity.inode,
        identity.links,
        identity.size,
        identity.modified_ns,
        identity.changed_ns,
    )


def _secure_file_identity(path: Path) -> _FileIdentity:
    parent, descriptor, filename = _open_secure_file(path)
    try:
        before = os.fstat(descriptor)
        current = os.stat(filename, dir_fd=parent, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("governed file path changed while it was inspected")
        return _file_identity(before)
    finally:
        os.close(descriptor)
        os.close(parent)


def _secure_read_file(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    parent, descriptor, filename = _open_secure_file(path)
    try:
        before = os.fstat(descriptor)
        if not 0 <= before.st_size <= maximum_bytes:
            raise ValueError(f"{label} exceeds the governed byte limit")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(maximum_bytes + 1)
        after = os.fstat(descriptor)
        current = os.stat(filename, dir_fd=parent, follow_symlinks=False)
        if (
            len(payload) != before.st_size
            or _file_identity(before) != _file_identity(after)
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ValueError(f"{label} changed while it was read")
        return payload
    finally:
        os.close(descriptor)
        os.close(parent)


def _bind_project_database(
    *,
    store: Store,
    project_root: Path,
) -> tuple[Path, _FileIdentity]:
    root = _absolute_without_resolving(project_root)
    if root == Path(root.anchor):
        raise ValueError("universe change project root is unsafe")
    root_descriptor, _sentinel = _open_absolute_parent(root / ".aios-project-root")
    try:
        if not stat.S_ISDIR(os.fstat(root_descriptor).st_mode):
            raise ValueError("universe change project root is not a directory")
    finally:
        os.close(root_descriptor)
    expected_database = root / "data" / "aios.duckdb"
    observed_database = _absolute_without_resolving(store.db_path)
    if observed_database != expected_database:
        raise ValueError("read-only Store is not bound to the supplied project root")
    current_identity = _secure_file_identity(expected_database)
    if (
        store.database_file_identity is None
        or _file_identity_tuple(current_identity) != store.database_file_identity
    ):
        raise ValueError("read-only Store database identity no longer matches its path")
    return root, current_identity


def capture_universe_change_state(
    *,
    store: Store,
    universe_id: str,
    coverage_through: date,
    expected_member_count: int,
) -> UniverseChangeStateSnapshot:
    """Capture and validate the exact pre-activation semantic CAS boundary."""

    normalized_universe = str(universe_id).strip()
    if not normalized_universe:
        raise ValueError("universe change state requires a universe_id")
    if not isinstance(coverage_through, date) or isinstance(coverage_through, datetime):
        raise ValueError("coverage_through must be a date")
    if (
        isinstance(expected_member_count, bool)
        or not isinstance(expected_member_count, int)
        or expected_member_count < 1
    ):
        raise ValueError("expected_member_count must be a positive integer")
    store.require_universe_change_activation_schema()

    coverage = coverage_through.isoformat()
    boundary = coverage_through + timedelta(days=1)
    members = store.query(
        """
        SELECT universe_id, ticker, security_id, effective_start,
               effective_end, known_date, end_known_date, source
        FROM universe_membership
        WHERE universe_id = ?
          AND known_date <= CAST(? AS DATE)
          AND effective_start <= CAST(? AS DATE)
          AND (
              effective_end IS NULL
              OR effective_end > CAST(? AS DATE)
              OR end_known_date > CAST(? AS DATE)
          )
        ORDER BY ticker, effective_start
        """,
        (normalized_universe, coverage, coverage, coverage, coverage),
    )
    if len(members) != expected_member_count:
        raise ValueError(
            f"{normalized_universe} has {len(members)} members at the certified "
            f"boundary; expected {expected_member_count}"
        )
    tickers = [str(row["ticker"]) for row in members]
    if len(set(tickers)) != len(tickers):
        raise ValueError("universe change state has duplicate active tickers")
    security_ids = [str(row["security_id"] or "") for row in members]
    if any(not security_id for security_id in security_ids):
        raise ValueError("universe change state has a missing security identity")
    if len(set(security_ids)) != len(security_ids):
        raise ValueError("universe change state reuses one active security identity")
    if any(
        row["effective_end"] != boundary or row["end_known_date"] != coverage_through
        for row in members
    ):
        raise ValueError("universe membership does not share the certified finite edge")

    placeholders = ",".join("?" for _ in security_ids)
    identities = store.query(
        """
        SELECT universe_id, ticker, effective_start, effective_end, security_id,
               known_date, identity_status, source
        FROM security_identity_assignments
        WHERE universe_id = ?
          AND known_date <= CAST(? AS DATE)
          AND effective_start <= CAST(? AS DATE)
          AND (effective_end IS NULL OR effective_end > CAST(? AS DATE))
        ORDER BY ticker, effective_start
        """,
        (normalized_universe, coverage, coverage, coverage),
    )
    member_identities = {
        (str(row["ticker"]), str(row["security_id"])) for row in members
    }
    observed_identities = {
        (str(row["ticker"]), str(row["security_id"])) for row in identities
    }
    if observed_identities != member_identities or len(identities) != len(members):
        raise ValueError("security identity assignments do not exactly mirror membership")
    if any(row["effective_end"] != boundary for row in identities):
        raise ValueError("security identity assignments do not share the member edge")

    security_master = store.query(
        f"""
        SELECT security_id, canonical_ticker, security_type, identity_status, source
        FROM security_master
        WHERE security_id IN ({placeholders})
        ORDER BY security_id
        """,
        tuple(security_ids),
    )
    if len(security_master) != len(security_ids):
        raise ValueError("universe change state has orphan security identities")

    owners = store.query(
        f"""
        SELECT security_id, issuer_id, effective_start, effective_end,
               verified_date, source
        FROM security_issuer_assignments
        WHERE security_id IN ({placeholders})
          AND effective_start <= CAST(? AS DATE)
          AND (effective_end IS NULL OR effective_end > CAST(? AS DATE))
        ORDER BY security_id, effective_start
        """,
        (*security_ids, coverage, coverage),
    )
    if len(owners) != len(security_ids) or {
        str(row["security_id"]) for row in owners
    } != set(security_ids):
        raise ValueError("universe change state requires one active issuer per security")
    if any(row["effective_end"] != boundary for row in owners):
        raise ValueError("security issuer assignments do not share the certified edge")

    issuer_ids = sorted({str(row["issuer_id"]) for row in owners})
    issuer_placeholders = ",".join("?" for _ in issuer_ids)
    issuers = store.query(
        f"""
        SELECT issuer_id, canonical_name, canonical_ticker, source
        FROM issuer_master
        WHERE issuer_id IN ({issuer_placeholders})
        ORDER BY issuer_id
        """,
        tuple(issuer_ids),
    )
    if len(issuers) != len(issuer_ids):
        raise ValueError("universe change state has orphan issuer identities")
    ciks = store.query(
        f"""
        SELECT issuer_id, cik, effective_start, effective_end, verified_date, source
        FROM issuer_cik_history
        WHERE issuer_id IN ({issuer_placeholders})
          AND effective_start <= CAST(? AS DATE)
          AND (effective_end IS NULL OR effective_end > CAST(? AS DATE))
        ORDER BY issuer_id, effective_start
        """,
        (*issuer_ids, coverage, coverage),
    )
    if len(ciks) != len(issuer_ids) or {
        str(row["issuer_id"]) for row in ciks
    } != set(issuer_ids):
        raise ValueError("universe change state requires one active CIK per issuer")
    if any(row["effective_end"] != boundary for row in ciks):
        raise ValueError("issuer CIK assignments do not share the certified edge")

    providers = store.query(
        f"""
        SELECT provider, provider_symbol, security_id, data_start, data_end,
               mapping_status, verified_date, source
        FROM provider_symbol_history
        WHERE security_id IN ({placeholders})
          AND mapping_status = 'verified'
          AND data_start <= CAST(? AS DATE)
          AND (data_end IS NULL OR data_end > CAST(? AS DATE))
        ORDER BY security_id, provider, data_start
        """,
        (*security_ids, coverage, coverage),
    )
    if {str(row["security_id"]) for row in providers} != set(security_ids):
        raise ValueError("universe change state requires a provider mapping per security")
    if any(row["data_end"] != boundary for row in providers):
        raise ValueError("provider mappings do not share the certified edge")

    payload = {
        "schema_version": UNIVERSE_CHANGE_STATE_SCHEMA_VERSION,
        "universe_id": normalized_universe,
        "coverage_through": coverage,
        "finite_boundary_end": boundary.isoformat(),
        "members": _json_safe_rows(members),
        "security_identities": _json_safe_rows(identities),
        "security_master": _json_safe_rows(security_master),
        "security_issuers": _json_safe_rows(owners),
        "issuer_master": _json_safe_rows(issuers),
        "issuer_ciks": _json_safe_rows(ciks),
        "provider_symbols": _json_safe_rows(providers),
    }
    canonical = _canonical_json(payload)
    return UniverseChangeStateSnapshot(
        universe_id=normalized_universe,
        coverage_through=coverage,
        member_count=len(members),
        member_set_sha256=_canonical_payload_sha256(sorted(tickers)),
        security_set_sha256=_canonical_payload_sha256(sorted(security_ids)),
        state_sha256=_canonical_payload_sha256(payload),
        _canonical_payload_json=canonical,
    )


def build_universe_change_plan(
    *,
    store: Store,
    project_root: Path,
    universe_id: str,
    source_attestation_id: str,
    official_release_url: str,
    archive_evidence: tuple[RawEvidenceExpectation, ...],
    detail_evidence: tuple[RawEvidenceExpectation, ...],
    component_evidence: RawEvidenceExpectation,
    expected_effective_date: date,
    expected_member_count: int,
) -> UniverseChangePlan:
    """Build a deterministic read-only plan from exact replayable evidence.

    The returned plan is deliberately non-executable. It binds the before
    state, every source observation, the expected set transition, entering
    identities, and the complete paper tree without mutating the database or
    filesystem.
    """

    if not store.read_only:
        raise ValueError("universe change planning requires a read-only Store")
    root, database_identity = _bind_project_database(
        store=store,
        project_root=project_root,
    )
    normalized_universe = str(universe_id).strip()
    attestation_id = str(source_attestation_id).strip()
    release_url = str(official_release_url).strip()
    if not normalized_universe or not attestation_id or not release_url:
        raise ValueError("universe change plan requires universe, attestation, and URL")
    canonical_release_url = _validate_official_release_url(release_url)
    if not archive_evidence:
        raise ValueError("universe change plan requires official archive evidence")
    ordered_archives = tuple(sorted(archive_evidence, key=lambda item: item.role))
    ordered_details = tuple(sorted(detail_evidence, key=lambda item: item.role))
    if not ordered_details:
        raise ValueError("universe change plan requires official detail evidence")
    if (
        isinstance(expected_member_count, bool)
        or not isinstance(expected_member_count, int)
        or expected_member_count < 1
    ):
        raise ValueError("expected_member_count must be a positive integer")
    if not isinstance(expected_effective_date, date) or isinstance(
        expected_effective_date,
        datetime,
    ):
        raise ValueError("expected_effective_date must be a date")

    attestations = store.query(
        """
        SELECT * FROM universe_coverage_attestations
        WHERE attestation_id = ?
        """,
        (attestation_id,),
    )
    if len(attestations) != 1:
        raise ValueError("universe change plan requires one source attestation")
    attestation = attestations[0]
    if str(attestation["universe_id"]) != normalized_universe:
        raise ValueError("source attestation universe does not match the plan")
    if str(attestation["status"]) != "blocked_review_required":
        raise ValueError("source attestation is not blocked for governed review")
    mutation_counts = (
        "membership_rows_extended",
        "security_rows_extended",
        "owner_rows_extended",
        "cik_rows_extended",
        "provider_rows_extended",
    )
    if any(int(attestation[name]) != 0 for name in mutation_counts):
        raise ValueError("blocked source attestation already mutated reference state")
    run_id = str(attestation["run_id"])
    evidence_items = (*ordered_archives, *ordered_details, component_evidence)
    if any(item.run_id != run_id for item in evidence_items):
        raise ValueError("plan evidence does not share the source attestation run")
    linked = store.query(
        """
        SELECT role, snapshot_id FROM ingest_raw_snapshots
        WHERE run_id = ? ORDER BY role, snapshot_id
        """,
        (run_id,),
    )
    expected_links = sorted(
        (item.role, item.snapshot_id) for item in evidence_items
    )
    observed_links = [(str(row["role"]), str(row["snapshot_id"])) for row in linked]
    if observed_links != expected_links:
        raise ValueError("plan evidence does not exactly cover the attestation run")
    archive_roles = [item.role for item in ordered_archives]
    archive_matches = [_ARCHIVE_ROLE.fullmatch(role) for role in archive_roles]
    if (
        len(set(archive_roles)) != len(archive_roles)
        or not all(archive_matches)
        or len(archive_roles) > MAX_ARCHIVE_PAGES
    ):
        raise ValueError("official archive evidence roles are incomplete or duplicated")
    archive_pages = [int(match.group(1)) for match in archive_matches if match]
    if archive_pages != list(range(len(archive_pages))):
        raise ValueError("official archive evidence pages are not contiguous")
    detail_roles = [item.role for item in ordered_details]
    expected_detail_roles = [
        f"candidate_release_detail_{position:03d}"
        for position in range(len(ordered_details))
    ]
    if detail_roles != expected_detail_roles:
        raise ValueError("official detail evidence roles are not canonical and contiguous")
    selected_details = [
        item for item in ordered_details if item.source_url == release_url
    ]
    if len(selected_details) != 1:
        raise ValueError("one detail observation must match the selected official release")
    selected_detail = selected_details[0]
    if component_evidence.role != "independent_component_snapshot":
        raise ValueError("component evidence has an invalid semantic role")
    for item in ordered_archives:
        _require_evidence_contract(
            expected=item,
            evidence_kind="archive",
            release_url=release_url,
        )
    for item in ordered_details:
        _validate_official_release_url(item.source_url)
        _require_evidence_contract(
            expected=item,
            evidence_kind="detail",
            release_url=item.source_url,
        )
    _require_evidence_contract(
        expected=component_evidence,
        evidence_kind="component",
        release_url=release_url,
    )

    verified_archives = tuple(
        _read_expected_evidence(store=store, root=root, expected=item)
        for item in ordered_archives
    )
    verified_details = tuple(
        _read_expected_evidence(store=store, root=root, expected=item)
        for item in ordered_details
    )
    selected_detail_index = ordered_details.index(selected_detail)
    verified_component = _read_expected_evidence(
        store=store,
        root=root,
        expected=component_evidence,
    )
    archive_page_releases = tuple(
        parse_press_archive(verified.payload) for verified in verified_archives
    )
    replayed_releases = [
        release for page_releases in archive_page_releases for release in page_releases
    ]
    release_urls = [release.url for release in replayed_releases]
    if len(release_urls) != len(set(release_urls)):
        raise ValueError("official archive evidence contains duplicate release URLs")
    if sum(release.url == release_url for release in replayed_releases) != 1:
        raise ValueError("official release must occur once in archive evidence")
    archived_releases = {release.url: release for release in replayed_releases}
    archived_release = archived_releases.get(release_url)
    if archived_release is None:
        raise ValueError("official release URL is absent from replayed archive evidence")
    candidates = _canonical_json_list(
        attestation["candidate_releases_json"],
        label="source attestation candidate releases",
    )
    matching_candidates = [
        item for item in candidates if isinstance(item, dict) and item.get("url") == release_url
    ]
    if len(candidates) != 1 or len(matching_candidates) != 1:
        raise ValueError("source attestation does not identify the exact official release")
    candidate = matching_candidates[0]
    if (
        candidate.get("release_date") != archived_release.release_date.isoformat()
        or candidate.get("title") != archived_release.title
    ):
        raise ValueError("source attestation release metadata conflicts with archive replay")
    if not is_constituent_change_title(archived_release.title):
        raise ValueError("official release is not a recognized S&P 500 change")

    detail_changes = tuple(
        parse_sp500_constituent_changes(verified.payload)
        for verified in verified_details
    )
    changes = detail_changes[selected_detail_index]
    effective_dates = {change.effective_date for change in changes}
    if len(effective_dates) != 1:
        raise ValueError("one change plan cannot span multiple effective dates")
    effective_date = next(iter(effective_dates))
    if effective_date != expected_effective_date:
        raise ValueError("official detail effective date differs from operator intent")
    announcement_date = archived_release.release_date
    if announcement_date > effective_date:
        raise ValueError("constituent change was announced after its effective date")
    prior_coverage = attestation["prior_coverage_through"]
    target_coverage = attestation["requested_coverage_through"]
    checked_at = attestation["checked_at"]
    completed_date = attestation["completed_new_york_date"]
    if (
        not isinstance(prior_coverage, date)
        or not isinstance(target_coverage, date)
        or not isinstance(checked_at, datetime)
        or not isinstance(completed_date, date)
    ):
        raise ValueError("source attestation has invalid review clocks")
    if target_coverage > completed_date:
        raise ValueError("source attestation review clocks are inconsistent")
    if (
        us_equity_sessions(target_coverage, target_coverage + timedelta(days=1))
        != [target_coverage]
        or us_equity_sessions(completed_date, completed_date + timedelta(days=1))
        != [completed_date]
    ):
        raise ValueError("source attestation coverage dates are not U.S. equity sessions")
    event_sessions = us_equity_sessions(effective_date, effective_date + timedelta(days=1))
    prior_sessions = us_equity_sessions(
        effective_date - timedelta(days=31),
        effective_date,
    )
    if event_sessions != [effective_date] or not prior_sessions:
        raise ValueError("official effective date is not a reviewed U.S. equity session")
    if prior_coverage != prior_sessions[-1]:
        raise ValueError("certified state does not reach the prior U.S. equity session")
    if target_coverage < effective_date:
        raise ValueError("source attestation did not review the effective date")
    future_release_evidence: list[dict[str, Any]] = []
    for item, observed_changes in zip(
        ordered_details,
        detail_changes,
        strict=True,
    ):
        if item is selected_detail:
            continue
        release = archived_releases.get(item.source_url)
        if release is None:
            raise ValueError("detail evidence URL is absent from archive replay")
        if any(change.effective_date <= target_coverage for change in observed_changes):
            raise ValueError("one plan cannot omit another blocking official release")
        future_release_evidence.append(
            {
                **release.to_dict(),
                "changes": [change.to_dict() for change in observed_changes],
            }
        )
    archived_release_count = _validate_archive_replay(
        pages=archive_page_releases,
        prior_coverage=prior_coverage,
        completed_date=completed_date,
        checked_at=checked_at,
        store=store,
    )
    ingest_outcome = _validate_run_lineage(
        store=store,
        attestation=attestation,
        verified_evidence=(*verified_archives, *verified_details, verified_component),
        candidate_count=len(candidates),
    )

    before = capture_universe_change_state(
        store=store,
        universe_id=normalized_universe,
        coverage_through=prior_coverage,
        expected_member_count=expected_member_count,
    )
    before_tickers = {str(row["ticker"]) for row in before.payload["members"]}
    additions = {change.ticker: change for change in changes if change.action == "addition"}
    deletions = {change.ticker: change for change in changes if change.action == "deletion"}
    if set(deletions) - before_tickers:
        raise ValueError("official deletion is absent from the certified before state")
    if set(additions) & before_tickers:
        raise ValueError("official addition already exists in the certified before state")

    components = parse_component_snapshot(verified_component.payload)
    component_ciks = {component.ticker: component.cik for component in components}
    expected_after_tickers = (before_tickers - set(deletions)) | set(additions)
    component_tickers = set(component_ciks)
    if component_tickers == before_tickers:
        component_classification = "matches_before"
    elif component_tickers == expected_after_tickers:
        component_classification = "matches_after"
    else:
        raise ValueError("component snapshot matches neither before nor planned set")
    if len(expected_after_tickers) != expected_member_count:
        raise ValueError("planned constituent change does not preserve the member count")
    mismatch_detail = _validate_attestation_projection(
        attestation=attestation,
        candidates=candidates,
        archived_release_count=archived_release_count,
        before_tickers=before_tickers,
        component_tickers=component_tickers,
        additions=set(additions),
        deletions=set(deletions),
        component_classification=component_classification,
        expected_future_releases=future_release_evidence,
    )
    incoming_component_evidence = _validate_component_lineage(
        store=store,
        before=before,
        components=components,
        additions=set(additions),
        mismatch_detail=mismatch_detail,
    )

    change_rows = [change.to_dict() for change in changes]
    change_rows_sha256 = _canonical_payload_sha256(change_rows)
    event_id = "uce-" + _canonical_payload_sha256(
        {
            "event_schema_version": UNIVERSE_CHANGE_EVENT_SCHEMA_VERSION,
            "universe_id": normalized_universe,
            "canonical_official_release_url": canonical_release_url,
            "announcement_date": announcement_date.isoformat(),
            "effective_date": effective_date.isoformat(),
            "change_rows": change_rows,
        }
    )
    paper = capture_paper_tree_state(root)
    attestation_projection = _attestation_plan_payload(attestation)
    payload = {
        "schema_version": UNIVERSE_CHANGE_PLAN_SCHEMA_VERSION,
        "policy_version": UNIVERSE_CHANGE_PLAN_POLICY_VERSION,
        "event_schema_version": UNIVERSE_CHANGE_EVENT_SCHEMA_VERSION,
        "event_id": event_id,
        "universe_id": normalized_universe,
        "source_attestation_id": attestation_id,
        "source_attestation": {
            **attestation_projection,
            "projection_sha256": _canonical_payload_sha256(
                attestation_projection
            ),
        },
        "ingest_outcome": {
            **ingest_outcome,
            "projection_sha256": _canonical_payload_sha256(ingest_outcome),
        },
        "announcement_date": announcement_date.isoformat(),
        "effective_date": effective_date.isoformat(),
        "prior_coverage_through": prior_coverage.isoformat(),
        "requested_coverage_through": target_coverage.isoformat(),
        "coverage_status": "requested_not_certified",
        "official_release_url": release_url,
        "canonical_official_release_url": canonical_release_url,
        "change_rows": change_rows,
        "change_rows_sha256": change_rows_sha256,
        "before_state": {
            "member_count": before.member_count,
            "member_set_sha256": before.member_set_sha256,
            "security_set_sha256": before.security_set_sha256,
            "state_sha256": before.state_sha256,
            "finite_boundary_end": before.payload["finite_boundary_end"],
        },
        "before_member_set_sha256": before.member_set_sha256,
        "before_security_set_sha256": before.security_set_sha256,
        "before_state_sha256": before.state_sha256,
        "planned_after_member_tickers": sorted(expected_after_tickers),
        "planned_after_member_ticker_set_sha256": _canonical_payload_sha256(
            sorted(expected_after_tickers)
        ),
        "evidence": [
            _verified_evidence_payload(expected, verified)
            for expected, verified in zip(
                evidence_items,
                (*verified_archives, *verified_details, verified_component),
                strict=True,
            )
        ],
        "paper_state": {
            **paper,
            "assurance": "byte_identity_only_not_semantically_validated",
        },
        "component_set_classification": component_classification,
        "incoming_component_evidence": incoming_component_evidence,
        "safety": {
            "network_used": False,
            "database_mutation": False,
            "paper_mutation": False,
            "broker_used": False,
            "activation_available": False,
        },
        "activation_blockers": [
            "source_attestation_blocked_review_required",
            "addition_identity_evidence_not_reviewed",
            "activation_receipt_v2_not_installed",
            "atomic_activation_not_implemented",
            "fundamental_staging_not_verified",
            "price_staging_not_verified",
            "paper_semantic_validation_not_verified",
            "backup_restore_drill_not_bound_to_plan",
            "legacy_timestamp_clock_provenance_not_persisted",
            *(
                ["post_event_component_reconciliation_not_observed"]
                if component_classification == "matches_before"
                else []
            ),
            *(
                ["multi_page_archive_not_double_observed"]
                if len(ordered_archives) > 1
                else []
            ),
        ],
    }
    canonical = _canonical_json(payload)
    if _secure_file_identity(root / "data" / "aios.duckdb") != database_identity:
        raise ValueError("project database changed while the plan was built")
    return UniverseChangePlan(
        plan_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        event_id=event_id,
        universe_id=normalized_universe,
        announcement_date=announcement_date.isoformat(),
        effective_date=effective_date.isoformat(),
        prior_coverage_through=prior_coverage.isoformat(),
        requested_coverage_through=target_coverage.isoformat(),
        activation_available=False,
        _canonical_payload_json=canonical,
    )


def capture_paper_tree_state(project_root: Path) -> dict[str, Any]:
    """Double-capture the bounded governed JSON tree without taking locks."""

    root = _absolute_without_resolving(project_root)
    first = _capture_paper_tree_once(root)
    second = _capture_paper_tree_once(root)
    if first != second:
        raise ValueError("paper state changed while the plan was built")
    return {
        "files": first,
        "tree_sha256": _canonical_payload_sha256(first),
    }


def _capture_paper_tree_once(root: Path) -> list[dict[str, Any]]:
    paper_root = root / "data" / "paper"
    candidates = _enumerate_paper_files(paper_root)
    files: list[dict[str, Any]] = []
    total_bytes = 0
    for candidate in candidates:
        if candidate.name.endswith(".lock"):
            continue
        if candidate.suffix != ".json":
            raise ValueError("paper state contains an unexpected non-JSON file")
        if len(files) >= _MAX_PAPER_FILES:
            raise ValueError("paper state exceeds the governed file-count limit")
        payload = _secure_read_file(
            candidate,
            maximum_bytes=_MAX_PAPER_FILE_BYTES,
            label="paper JSON",
        )
        total_bytes += len(payload)
        if total_bytes > _MAX_PAPER_TOTAL_BYTES:
            raise ValueError("paper state exceeds the governed total-byte limit")
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("paper state contains invalid JSON") from exc
        if not isinstance(document, dict):
            raise ValueError("paper state JSON must contain an object envelope")
        files.append(
            {
                "path": candidate.relative_to(root).as_posix(),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    if not files:
        raise ValueError("universe change plan requires existing paper state")
    return files


def _enumerate_paper_files(paper_root: Path) -> list[Path]:
    descriptor, _sentinel = _open_absolute_parent(
        paper_root / ".aios-paper-root"
    )
    directory_flags = (
        os.O_RDONLY
        | _required_os_flag("O_DIRECTORY")
        | _required_os_flag("O_NOFOLLOW")
        | _required_os_flag("O_CLOEXEC")
    )
    entries_seen = 0
    files: list[Path] = []

    def walk(directory: int, relative: Path, depth: int) -> None:
        nonlocal entries_seen
        if depth > _MAX_PAPER_DEPTH:
            raise ValueError("paper state exceeds the governed directory depth")
        before = _file_identity(os.fstat(directory))
        entries: list[tuple[str, os.stat_result]] = []
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    entries_seen += 1
                    if entries_seen > _MAX_PAPER_ENTRIES:
                        raise ValueError("paper state exceeds the governed entry limit")
                    entries.append((entry.name, entry.stat(follow_symlinks=False)))
        except OSError as exc:
            raise ValueError("paper state changed during enumeration") from exc
        for name, metadata in sorted(entries, key=lambda item: item[0]):
            child_relative = relative / name
            if (
                len(child_relative.as_posix().encode("utf-8"))
                > _MAX_PAPER_RELATIVE_PATH_BYTES
            ):
                raise ValueError("paper state contains an overlong relative path")
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("paper state cannot contain symbolic links")
            if stat.S_ISDIR(metadata.st_mode):
                try:
                    child = os.open(name, directory_flags, dir_fd=directory)
                except OSError as exc:
                    raise ValueError("paper state directory is missing or unsafe") from exc
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise ValueError("paper state directory changed during enumeration")
                    walk(child, child_relative, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(metadata.st_mode):
                files.append(paper_root / child_relative)
            else:
                raise ValueError("paper state contains an unsupported filesystem entry")
        if _file_identity(os.fstat(directory)) != before:
            raise ValueError("paper state directory changed during enumeration")

    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError("universe change plan requires a regular data/paper directory")
        walk(descriptor, Path(), 0)
    finally:
        os.close(descriptor)
    return files


def _read_expected_evidence(
    *,
    store: Store,
    root: Path,
    expected: RawEvidenceExpectation,
) -> VerifiedRawSnapshot:
    return read_verified_raw_snapshot(
        store=store,
        expected_run_id=expected.run_id,
        expected_role=expected.role,
        snapshot_id=expected.snapshot_id,
        expected_provider=expected.provider,
        expected_dataset=expected.dataset,
        expected_artifact_kind=expected.artifact_kind,
        expected_parser_version=expected.parser_version,
        expected_request_fingerprint=expected.request_fingerprint,
        expected_adapter_name=expected.adapter_name,
        expected_adapter_version=expected.adapter_version,
        require_timestamps_not_future=False,
        project_root=root,
    )


def _validate_official_release_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "press.spglobal.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or not parsed.path.startswith("/")
        or parsed.fragment
    ):
        raise ValueError("universe change plan requires an official S&P Global URL")
    return urlunsplit(("https", "press.spglobal.com", parsed.path, parsed.query, ""))


def _canonical_json_list(value: Any, *, label: str) -> list[Any]:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not canonical JSON") from exc
    if not isinstance(decoded, list) or _canonical_json(decoded) != str(value):
        raise ValueError(f"{label} is not a canonical JSON list")
    return decoded


def _require_evidence_contract(
    *,
    expected: RawEvidenceExpectation,
    evidence_kind: str,
    release_url: str,
) -> None:
    if evidence_kind == "archive":
        try:
            page = int(expected.role.rsplit("_", maxsplit=1)[1])
        except (IndexError, ValueError) as exc:
            raise ValueError("official archive role has no page number") from exc
        source_url = sp500_archive_page_url(page)
        contract = {
            "provider": "spglobal",
            "dataset": "sp500_press_archive",
            "adapter_name": "spglobal_press_archive_html",
            "adapter_version": "1",
            "parser_version": PRESS_ARCHIVE_PARSER_VERSION,
        }
    elif evidence_kind == "detail":
        source_url = release_url
        contract = {
            "provider": "spglobal",
            "dataset": "sp500_change_announcement",
            "adapter_name": "spglobal_constituent_change_html",
            "adapter_version": "1",
            "parser_version": CHANGE_ANNOUNCEMENT_PARSER_VERSION,
        }
    elif evidence_kind == "component":
        source_url = COMPONENT_SNAPSHOT_URL
        contract = {
            "provider": "github",
            "dataset": "sp500_current_components",
            "adapter_name": "fja05680_sp500_csv",
            "adapter_version": "1",
            "parser_version": COMPONENT_SNAPSHOT_PARSER_VERSION,
        }
    else:  # pragma: no cover - constant callers evolve together
        raise RuntimeError("unsupported universe-change evidence kind")
    observed = {
        "provider": expected.provider,
        "dataset": expected.dataset,
        "adapter_name": expected.adapter_name,
        "adapter_version": expected.adapter_version,
        "parser_version": expected.parser_version,
    }
    if observed != contract or expected.artifact_kind != "exact_response":
        raise ValueError("universe-change evidence contract is not reviewed")
    fingerprint = canonical_request_fingerprint({"method": "GET", "url": source_url})
    if expected.source_url != source_url or expected.request_fingerprint != fingerprint:
        raise ValueError("universe-change evidence request identity is inconsistent")


def _validate_archive_replay(
    *,
    pages: tuple[list[Any], ...],
    prior_coverage: date,
    completed_date: date,
    checked_at: datetime,
    store: Store,
) -> int:
    del store  # Legacy TIMESTAMP rows carry no portable timezone provenance.
    if not pages or len(pages) > MAX_ARCHIVE_PAGES:
        raise ValueError("official archive evidence page count is invalid")
    for page_number, releases in enumerate(pages):
        if not releases or len(releases) > ARCHIVE_PAGE_SIZE:
            raise ValueError("official archive page size is invalid")
        oldest = min(release.release_date for release in releases)
        final_page = page_number == len(pages) - 1
        if not final_page and (
            len(releases) != ARCHIVE_PAGE_SIZE or oldest <= prior_coverage
        ):
            raise ValueError("official archive evidence violates producer paging")
        if final_page and oldest > prior_coverage:
            raise ValueError("official archive evidence does not reach prior coverage")
        if page_number:
            previous_oldest = min(
                release.release_date for release in pages[page_number - 1]
            )
            current_newest = max(release.release_date for release in releases)
            if current_newest > previous_oldest:
                raise ValueError("official archive pages are chronologically inverted")
    releases = [release for page in pages for release in page]
    maximum_plausible_date = checked_at.date() + timedelta(days=1)
    if any(release.release_date > maximum_plausible_date for release in releases):
        raise ValueError("official archive evidence contains an implausible future date")
    eligible = [
        release for release in releases if release.release_date <= completed_date
    ]
    if not eligible:
        raise ValueError("official archive evidence has no completed-date releases")
    newest = max(release.release_date for release in eligible)
    if (completed_date - newest).days > MAX_ARCHIVE_STALENESS_DAYS:
        raise ValueError("official archive evidence is stale")
    return len(eligible)


def _naive_timestamp(value: Any, *, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{label} is not an ISO timestamp") from exc
    else:
        raise ValueError(f"{label} is not a timestamp")
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        raise ValueError(f"{label} unexpectedly claims timezone provenance")
    return parsed


def _validate_run_lineage(
    *,
    store: Store,
    attestation: dict[str, Any],
    verified_evidence: tuple[VerifiedRawSnapshot, ...],
    candidate_count: int,
) -> dict[str, Any]:
    outcomes = store.query(
        "SELECT * FROM ingest_log WHERE run_id = ? ORDER BY id",
        (attestation["run_id"],),
    )
    if len(outcomes) != 1:
        raise ValueError("source attestation requires one exact ingest outcome")
    outcome = outcomes[0]
    expected_rejections = max(
        1,
        candidate_count + int(attestation["identity_mismatch_count"]),
    )
    if (
        str(outcome["source"]) != "spglobal+fja05680"
        or str(outcome["table_name"]) != "universe_coverage_attestations"
        or outcome["subject_type"] is not None
        or outcome["subject_id"] is not None
        or int(outcome["rows_inserted"]) != 0
        or int(outcome["rows_rejected"]) != expected_rejections
        or str(outcome["status"]) != "warning"
        or str(outcome["error"]) != str(attestation["detail"])
        or outcome["rejection_codes"] is not None
    ):
        raise ValueError("source attestation ingest outcome is inconsistent")
    checked = _naive_timestamp(attestation["checked_at"], label="attestation checked_at")
    attestation_created = _naive_timestamp(
        attestation["created_at"],
        label="attestation created_at",
    )
    started = _naive_timestamp(outcome["started_at"], label="ingest started_at")
    finished = _naive_timestamp(outcome["finished_at"], label="ingest finished_at")
    plausible_upper_bound = (
        datetime.now(UTC).replace(tzinfo=None) + _MAX_LEGACY_TIMESTAMP_ZONE_SKEW
    )
    if (
        started != checked
        or attestation_created > finished
        or finished > plausible_upper_bound
    ):
        raise ValueError("source attestation and ingest clocks are inconsistent")
    for verified in verified_evidence:
        metadata = verified.metadata
        requested = _naive_timestamp(
            metadata["requested_at"],
            label="raw requested_at",
        )
        received = _naive_timestamp(
            metadata["received_at"],
            label="raw received_at",
        )
        snapshot_created = _naive_timestamp(
            metadata["created_at"],
            label="raw snapshot created_at",
        )
        linked = _naive_timestamp(metadata["linked_at"], label="raw linked_at")
        if not (
            started
            <= requested
            <= received
            <= snapshot_created
            <= linked
            <= attestation_created
            <= finished
        ):
            raise ValueError("source attestation evidence timeline is inconsistent")
    return _json_safe_row(outcome)


def _validate_component_lineage(
    *,
    store: Store,
    before: UniverseChangeStateSnapshot,
    components: list[Any],
    additions: set[str],
    mismatch_detail: dict[str, Any],
) -> list[dict[str, str]]:
    members = {str(row["ticker"]): str(row["security_id"]) for row in before.payload["members"]}
    owners = {
        str(row["security_id"]): str(row["issuer_id"])
        for row in before.payload["security_issuers"]
    }
    active_ciks = {
        str(row["issuer_id"]): str(row["cik"])
        for row in before.payload["issuer_ciks"]
    }
    security_ids = sorted(members.values())
    placeholders = ",".join("?" for _ in security_ids)
    lineage_rows = store.query(
        f"""
        SELECT DISTINCT owner.security_id, cik.cik
        FROM security_issuer_assignments AS owner
        JOIN issuer_cik_history AS cik USING (issuer_id)
        WHERE owner.security_id IN ({placeholders})
        ORDER BY owner.security_id, cik.cik
        """,
        tuple(security_ids),
    )
    lineage: dict[str, set[str]] = {}
    for row in lineage_rows:
        lineage.setdefault(str(row["security_id"]), set()).add(str(row["cik"]))
    component_by_ticker = {str(component.ticker): component for component in components}
    expected_successors: list[dict[str, Any]] = []
    for ticker, security_id in sorted(members.items()):
        component = component_by_ticker.get(ticker)
        if component is None:
            continue
        component_cik = str(component.cik)
        reviewed_lineage = lineage.get(security_id, set())
        if component_cik not in reviewed_lineage:
            raise ValueError("unchanged component CIK is outside reviewed security lineage")
        active_cik = active_ciks[owners[security_id]]
        if component_cik != active_cik:
            expected_successors.append(
                {
                    "ticker": ticker,
                    "active_reviewed_cik": active_cik,
                    "component_cik": component_cik,
                    "reviewed_lineage_ciks": sorted(reviewed_lineage),
                }
            )
    if mismatch_detail["reviewed_successor_lineage_matches"] != expected_successors:
        raise ValueError("source attestation successor lineage evidence is inconsistent")
    return [
        {"ticker": ticker, "component_cik": str(component_by_ticker[ticker].cik)}
        for ticker in sorted(additions)
        if ticker in component_by_ticker
    ]


def _validate_attestation_projection(
    *,
    attestation: dict[str, Any],
    candidates: list[Any],
    archived_release_count: int,
    before_tickers: set[str],
    component_tickers: set[str],
    additions: set[str],
    deletions: set[str],
    component_classification: str,
    expected_future_releases: list[dict[str, Any]],
) -> dict[str, Any]:
    if (
        str(attestation["official_source_url"]) != OFFICIAL_ARCHIVE_URL
        or str(attestation["component_source_url"]) != COMPONENT_SNAPSHOT_URL
        or int(attestation["official_release_count"]) != archived_release_count
        or int(attestation["relevant_release_count"]) != len(candidates)
        or int(attestation["reviewed_member_count"]) != len(before_tickers)
        or int(attestation["component_count"]) != len(component_tickers)
        or str(attestation["reviewed_member_set_sha256"])
        != _newline_set_sha256(before_tickers)
        or str(attestation["component_set_sha256"])
        != _newline_set_sha256(component_tickers)
    ):
        raise ValueError("source attestation projection conflicts with replayed evidence")
    mismatch = _canonical_json_object(
        attestation["mismatch_detail_json"],
        label="source attestation mismatch detail",
    )
    expected_mismatch_fields = {
        "missing_from_component_snapshot",
        "unexpected_in_component_snapshot",
        "reference_identity_issues",
        "cik_mismatches",
        "reviewed_successor_lineage_matches",
        "future_effective_releases",
        "candidate_release_parse_errors",
    }
    if set(mismatch) != expected_mismatch_fields:
        raise ValueError("source attestation mismatch detail has an unsupported shape")
    missing = mismatch.get("missing_from_component_snapshot")
    unexpected = mismatch.get("unexpected_in_component_snapshot")
    expected_missing = [] if component_classification == "matches_before" else sorted(deletions)
    expected_unexpected = [] if component_classification == "matches_before" else sorted(additions)
    if missing != expected_missing or unexpected != expected_unexpected:
        raise ValueError("source attestation component delta is unrelated to the event")
    for field in (
        "reference_identity_issues",
        "cik_mismatches",
        "candidate_release_parse_errors",
    ):
        if mismatch.get(field) != []:
            raise ValueError("source attestation contains unrelated review blockers")
    if mismatch["future_effective_releases"] != expected_future_releases:
        raise ValueError("source attestation future-release evidence is inconsistent")
    mismatch_count = len(expected_missing) + len(expected_unexpected)
    if (
        int(attestation["identity_mismatch_count"]) != mismatch_count
        or int(attestation["identity_match_count"]) != len(before_tickers)
    ):
        raise ValueError("source attestation mismatch count is inconsistent")
    return mismatch


def _canonical_json_object(value: Any, *, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not canonical JSON") from exc
    if not isinstance(decoded, dict) or _canonical_json(decoded) != str(value):
        raise ValueError(f"{label} is not a canonical JSON object")
    return decoded


def _newline_set_sha256(values: set[str]) -> str:
    return hashlib.sha256("\n".join(sorted(values)).encode("utf-8")).hexdigest()


def _verified_evidence_payload(
    expected: RawEvidenceExpectation,
    verified: VerifiedRawSnapshot,
) -> dict[str, Any]:
    metadata = verified.metadata
    return {
        "run_id": expected.run_id,
        "role": expected.role,
        "snapshot_id": verified.snapshot_id,
        "source_url": expected.source_url,
        "provider": metadata["provider"],
        "dataset": metadata["dataset"],
        "artifact_kind": metadata["artifact_kind"],
        "parser_version": metadata["parser_version"],
        "request_fingerprint": metadata["request_fingerprint"],
        "adapter_name": metadata["adapter_name"],
        "adapter_version": metadata["adapter_version"],
        "payload_sha256": verified.payload_sha256,
        "requested_at": metadata["requested_at"],
        "received_at": metadata["received_at"],
        "created_at": metadata["created_at"],
        "parsed_row_count": metadata["parsed_row_count"],
        "parsed_rows_sha256": metadata["parsed_rows_sha256"],
        "parsed_rows_rejected": metadata["parsed_rows_rejected"],
        "parsed_rejection_codes": metadata["parsed_rejection_codes"],
        "linked_at": metadata["linked_at"],
    }


def _attestation_plan_payload(attestation: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "attestation_id",
        "run_id",
        "universe_id",
        "prior_coverage_through",
        "requested_coverage_through",
        "checked_at",
        "completed_new_york_date",
        "status",
        "official_source_url",
        "component_source_url",
        "official_release_count",
        "relevant_release_count",
        "reviewed_member_count",
        "component_count",
        "reviewed_member_set_sha256",
        "component_set_sha256",
        "identity_match_count",
        "identity_mismatch_count",
        "candidate_releases_json",
        "mismatch_detail_json",
        "membership_rows_extended",
        "security_rows_extended",
        "owner_rows_extended",
        "cik_rows_extended",
        "provider_rows_extended",
        "detail",
        "created_at",
    )
    if set(attestation) != set(fields):
        raise ValueError("source attestation row has an unsupported schema")
    projection = _json_safe_row({field: attestation[field] for field in fields})
    projection["candidate_releases"] = _canonical_json_list(
        attestation["candidate_releases_json"],
        label="source attestation candidate releases",
    )
    projection["mismatch_detail"] = _canonical_json_object(
        attestation["mismatch_detail_json"],
        label="source attestation mismatch detail",
    )
    return projection


def _json_safe_row(row: dict[str, Any]) -> dict[str, Any]:
    return _json_safe_rows([row])[0]


def _json_safe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: value.isoformat() if isinstance(value, (date, datetime)) else value
            for key, value in row.items()
        }
        for row in rows
    ]


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _canonical_payload_sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

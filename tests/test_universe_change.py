from __future__ import annotations

import hashlib
import os
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import aios.universe_change as universe_change_module
from aios.ingest.http_client import RawSnapshotContext
from aios.raw_snapshots import canonical_request_fingerprint, capture_raw_snapshot
from aios.storage.store import Store
from aios.universe_change import (
    RawEvidenceExpectation,
    build_universe_change_plan,
    capture_universe_change_state,
)
from aios.universe_rollforward import (
    COMPONENT_SNAPSHOT_URL,
    OFFICIAL_ARCHIVE_URL,
    roll_forward_sp500_coverage,
)


def _seed_state(
    store: Store,
    *,
    boundary: str = "2026-08-05",
    prior: str = "2026-08-04",
) -> None:
    tickers = ("AAA", "BBB")
    store.upsert_universe_membership(
        [
            {
                "universe_id": "sp500",
                "ticker": ticker,
                "security_id": f"security:{ticker.lower()}",
                "effective_start": "2026-01-02",
                "effective_end": boundary,
                "known_date": "2025-12-20",
                "end_known_date": prior,
                "source": "reviewed:test",
            }
            for ticker in tickers
        ]
    )
    store.upsert_security_identities(
        [
            {
                "universe_id": "sp500",
                "ticker": ticker,
                "security_id": f"security:{ticker.lower()}",
                "effective_start": "2026-01-02",
                "effective_end": boundary,
                "known_date": "2025-12-20",
                "identity_status": "bounded_ticker",
                "source": "reviewed:test",
            }
            for ticker in tickers
        ]
    )
    store.upsert_reference_identities(
        issuers=[
            {
                "issuer_id": f"issuer:{ticker.lower()}",
                "canonical_name": f"{ticker} Incorporated",
                "canonical_ticker": ticker,
                "source": "sec:test",
            }
            for ticker in tickers
        ],
        cik_history=[
            {
                "issuer_id": f"issuer:{ticker.lower()}",
                "cik": f"{position:010d}",
                "effective_start": "2026-01-02",
                "effective_end": boundary,
                "verified_date": prior,
                "source": "sec:test",
            }
            for position, ticker in enumerate(tickers, 1)
        ],
        security_issuers=[
            {
                "security_id": f"security:{ticker.lower()}",
                "issuer_id": f"issuer:{ticker.lower()}",
                "effective_start": "2026-01-02",
                "effective_end": boundary,
                "verified_date": prior,
                "source": "sec:test",
            }
            for ticker in tickers
        ],
        provider_symbols=[
            {
                "provider": "yfinance",
                "provider_symbol": ticker,
                "security_id": f"security:{ticker.lower()}",
                "data_start": "2026-01-02",
                "data_end": boundary,
                "mapping_status": "verified",
                "verified_date": prior,
                "source": "provider:test",
            }
            for ticker in tickers
        ],
    )


def test_universe_change_state_is_deterministic_and_read_only(tmp_path: Path) -> None:
    database = tmp_path / "aios.duckdb"
    writable = Store(database)
    try:
        _seed_state(writable)
    finally:
        writable.close()

    store = Store(database, read_only=True)
    try:
        before = store.query(
            "SELECT table_name, estimated_size FROM duckdb_tables() ORDER BY table_name"
        )
        first = capture_universe_change_state(
            store=store,
            universe_id="sp500",
            coverage_through=date(2026, 8, 4),
            expected_member_count=2,
        )
        second = capture_universe_change_state(
            store=store,
            universe_id="sp500",
            coverage_through=date(2026, 8, 4),
            expected_member_count=2,
        )
        after = store.query(
            "SELECT table_name, estimated_size FROM duckdb_tables() ORDER BY table_name"
        )
    finally:
        store.close()

    assert first == second
    assert first.member_count == 2
    assert len(first.member_set_sha256) == 64
    assert len(first.security_set_sha256) == 64
    assert len(first.state_sha256) == 64
    assert [row["ticker"] for row in first.payload["members"]] == ["AAA", "BBB"]
    assert before == after


def test_universe_change_state_hash_detects_semantic_source_drift(tmp_path: Path) -> None:
    database = tmp_path / "aios.duckdb"
    store = Store(database)
    try:
        _seed_state(store)
        before = capture_universe_change_state(
            store=store,
            universe_id="sp500",
            coverage_through=date(2026, 8, 4),
            expected_member_count=2,
        )
        store.execute(
            "UPDATE universe_membership SET source = 'reviewed:changed' WHERE ticker = 'AAA'"
        )
        after = capture_universe_change_state(
            store=store,
            universe_id="sp500",
            coverage_through=date(2026, 8, 4),
            expected_member_count=2,
        )
    finally:
        store.close()

    assert before.member_set_sha256 == after.member_set_sha256
    assert before.security_set_sha256 == after.security_set_sha256
    assert before.state_sha256 != after.state_sha256


def test_universe_change_state_rejects_misaligned_reference_edge(tmp_path: Path) -> None:
    store = Store(tmp_path / "aios.duckdb")
    try:
        _seed_state(store)
        store.execute(
            "UPDATE provider_symbol_history SET data_end = DATE '2026-08-06' "
            "WHERE security_id = 'security:aaa'"
        )

        with pytest.raises(ValueError, match="provider mappings do not share"):
            capture_universe_change_state(
                store=store,
                universe_id="sp500",
                coverage_through=date(2026, 8, 4),
                expected_member_count=2,
            )
    finally:
        store.close()


def _press_archive_html(
    release_url: str,
    *,
    release_date: str = "Jul 22, 2026",
    future_release_url: str | None = None,
) -> bytes:
    future_item = (
        ""
        if future_release_url is None
        else f"""
      <li class="wd_item"><div class="wd_item_wrapper">
        <div class="wd_date">{release_date}</div>
        <div class="wd_title"><a href="{future_release_url}">
          Gamma Set to Join S&amp;P 500
        </a></div>
      </div></li>
        """
    )
    return f"""
    <html><body><ul class="wd_layout-simple wd_item_list">
      {future_item}
      <li class="wd_item"><div class="wd_item_wrapper">
        <div class="wd_date">{release_date}</div>
        <div class="wd_title"><a href="{release_url}">
          Ferguson Enterprises Set to Join S&amp;P 500
        </a></div>
      </div></li>
      <li class="wd_item"><div class="wd_item_wrapper">
        <div class="wd_date">Jul 20, 2026</div>
        <div class="wd_title"><a href="https://press.spglobal.com/older">
          S&amp;P Global publishes a market study
        </a></div>
      </div></li>
    </ul></body></html>
    """.encode()


def _change_detail_html(*, effective_date: str = "July 23, 2026") -> bytes:
    return f"""
    <html><body><table>
      <tr><th>Effective Date</th><th>Index Name</th><th>Action</th>
          <th>Company Name</th><th>Ticker</th></tr>
      <tr><td>{effective_date}</td><td>S&amp;P 500</td><td>Addition</td>
          <td>Ferguson Enterprises Inc.</td><td>FERG</td></tr>
      <tr><td>{effective_date}</td><td>S&amp;P 500</td><td>Deletion</td>
          <td>Beta Incorporated</td><td>BBB</td></tr>
    </table></body></html>
    """.encode()


def _future_change_detail_html() -> bytes:
    return b"""
    <html><body><table>
      <tr><th>Effective Date</th><th>Index Name</th><th>Action</th>
          <th>Company Name</th><th>Ticker</th></tr>
      <tr><td>July 24, 2026</td><td>S&amp;P 500</td><td>Addition</td>
          <td>Gamma Incorporated</td><td>GAM</td></tr>
      <tr><td>July 24, 2026</td><td>S&amp;P 500</td><td>Deletion</td>
          <td>Beta Incorporated</td><td>BBB</td></tr>
    </table></body></html>
    """


def _post_event_components() -> bytes:
    return (
        b"Symbol,Security,GICS Sector,GICS Sub-Industry,Headquarters Location,"
        b"Date added,CIK,Founded\n"
        b"AAA,Alpha,,,,2020-01-01,1,2000\n"
        b"FERG,Ferguson,,,,2026-07-23,3,1953\n"
    )


def _before_event_components() -> bytes:
    return (
        b"Symbol,Security,GICS Sector,GICS Sub-Industry,Headquarters Location,"
        b"Date added,CIK,Founded\n"
        b"AAA,Alpha,,,,2020-01-01,1,2000\n"
        b"BBB,Beta,,,,2020-01-01,2,2000\n"
    )


def _producer_fetcher(payload_by_url: dict[str, bytes], *, now: datetime):
    def fetch(url: str, context: RawSnapshotContext) -> bytes:
        payload = payload_by_url[url]
        capture_raw_snapshot(
            payload,
            provider=context.provider,
            dataset=context.dataset,
            artifact_kind=context.artifact_kind,
            requested_at=now,
            received_at=now,
            request_fingerprint=canonical_request_fingerprint(
                {"method": "GET", "url": url}
            ),
            adapter_name=context.adapter_name,
            adapter_version=context.adapter_version,
            parser_version=context.parser_version,
            http_status=200,
            content_type="text/csv" if url == COMPONENT_SNAPSHOT_URL else "text/html",
            ingest_run_id=context.ingest_run_id,
            role=context.role,
            store=context.store,
            project_root=context.project_root,
        )
        return payload

    return fetch


def _evidence_expectation(
    store: Store,
    *,
    run_id: str,
    role: str,
    source_url: str,
) -> RawEvidenceExpectation:
    row = store.query(
        """
        SELECT snapshot.*
        FROM ingest_raw_snapshots AS linked
        JOIN raw_snapshots AS snapshot USING (snapshot_id)
        WHERE linked.run_id = ? AND linked.role = ?
        """,
        (run_id, role),
    )[0]
    return RawEvidenceExpectation(
        run_id=run_id,
        role=role,
        snapshot_id=str(row["snapshot_id"]),
        source_url=source_url,
        provider=str(row["provider"]),
        dataset=str(row["dataset"]),
        artifact_kind=str(row["artifact_kind"]),
        parser_version=str(row["parser_version"]),
        request_fingerprint=str(row["request_fingerprint"]),
        adapter_name=str(row["adapter_name"]),
        adapter_version=str(row["adapter_version"]),
    )


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        relative = path.relative_to(root).as_posix().encode()
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _create_plan_case(
    root: Path,
    *,
    database_timezone: str | None = None,
    prior: str = "2026-07-22",
    boundary: str = "2026-07-23",
    target: str = "2026-07-23",
    release_date: str = "Jul 22, 2026",
    release_slug_date: str = "2026-07-22",
    effective_date: str = "July 23, 2026",
    expected_effective_date: date = date(2026, 7, 23),
    now: datetime | None = None,
    component_payload: bytes | None = None,
    include_future_detail: bool = False,
) -> tuple[Path, dict]:
    database = root / "data/aios.duckdb"
    store = Store(database)
    release_url = f"https://press.spglobal.com/{release_slug_date}-ferguson-change"
    future_release_url = (
        f"https://press.spglobal.com/{release_slug_date}-zz-future-change"
        if include_future_detail
        else None
    )
    checked_at = now or datetime(2026, 7, 23, 21, 0, tzinfo=UTC)
    try:
        if database_timezone is not None:
            store.execute(f"SET TimeZone = '{database_timezone}'")
        _seed_state(store, boundary=boundary, prior=prior)
        store.upsert_prices(
            [{"ticker": "SPY", "date": target, "close": 500.0, "source": "test"}]
        )
        payload_by_url = {
            OFFICIAL_ARCHIVE_URL: _press_archive_html(
                release_url,
                release_date=release_date,
                future_release_url=future_release_url,
            ),
            release_url: _change_detail_html(effective_date=effective_date),
            COMPONENT_SNAPSHOT_URL: (
                component_payload or _post_event_components()
            ),
        }
        if future_release_url is not None:
            payload_by_url[future_release_url] = _future_change_detail_html()
        result = roll_forward_sp500_coverage(
            store=store,
            now=checked_at,
            project_root=root,
            fetch_bytes=_producer_fetcher(payload_by_url, now=checked_at),
        )
        assert result.status == "review_required"
        assert result.run_id is not None and result.attestation_id is not None
        archive = (
            _evidence_expectation(
                store,
                run_id=result.run_id,
                role="official_release_archive_page_000",
                source_url=OFFICIAL_ARCHIVE_URL,
            ),
        )
        detail_urls = (
            [future_release_url, release_url]
            if future_release_url is not None
            else [release_url]
        )
        details = tuple(
            _evidence_expectation(
                store,
                run_id=result.run_id,
                role=f"candidate_release_detail_{position:03d}",
                source_url=source_url,
            )
            for position, source_url in enumerate(detail_urls)
        )
        component = _evidence_expectation(
            store,
            run_id=result.run_id,
            role="independent_component_snapshot",
            source_url=COMPONENT_SNAPSHOT_URL,
        )
        paper = root / "data/paper/account.json"
        paper.parent.mkdir(parents=True)
        paper.write_text('{"simulation_only":true}\n', encoding="utf-8")
    finally:
        store.close()
    return database, {
        "project_root": root,
        "universe_id": "sp500",
        "source_attestation_id": result.attestation_id,
        "official_release_url": release_url,
        "archive_evidence": archive,
        "detail_evidence": details,
        "component_evidence": component,
        "expected_effective_date": expected_effective_date,
        "expected_member_count": 2,
    }


def test_universe_change_plan_is_deterministic_read_only_and_non_executable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    database, arguments = _create_plan_case(tmp_path)

    before = _tree_sha256(tmp_path)
    read_only = Store(database, read_only=True)
    try:
        first = build_universe_change_plan(store=read_only, **arguments)
        second = build_universe_change_plan(store=read_only, **arguments)
    finally:
        read_only.close()

    assert first == second
    assert first.plan_sha256 == hashlib.sha256(
        first._canonical_payload_json.encode()
    ).hexdigest()
    assert first.event_id.startswith("uce-") and len(first.event_id) == 68
    assert first.activation_available is False
    assert first.requested_coverage_through == "2026-07-23"
    assert "target_coverage_through" not in first.payload
    assert first.payload["coverage_status"] == "requested_not_certified"
    assert "after_member_set_sha256" not in first.payload
    assert first.payload["planned_after_member_tickers"] == ["AAA", "FERG"]
    assert first.payload["paper_state"]["assurance"] == (
        "byte_identity_only_not_semantically_validated"
    )
    assert "source_attestation_blocked_review_required" in first.payload[
        "activation_blockers"
    ]
    assert "backup_restore_drill_not_bound_to_plan" in first.payload[
        "activation_blockers"
    ]
    assert "legacy_timestamp_clock_provenance_not_persisted" in first.payload[
        "activation_blockers"
    ]
    assert first.payload["incoming_component_evidence"] == [
        {"ticker": "FERG", "component_cik": "0000000003"}
    ]
    assert first.payload["ingest_outcome"]["status"] == "warning"
    assert first.payload["component_set_classification"] == "matches_after"
    assert first.payload["safety"] == {
        "network_used": False,
        "database_mutation": False,
        "paper_mutation": False,
        "broker_used": False,
        "activation_available": False,
    }
    assert _tree_sha256(tmp_path) == before


def test_universe_change_plan_rejects_writable_store_and_forged_request(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    database, arguments = _create_plan_case(tmp_path)
    store = Store(database)
    try:
        with pytest.raises(ValueError, match="read-only Store"):
            build_universe_change_plan(store=store, **arguments)
    finally:
        store.close()

    detail = arguments["detail_evidence"][0]
    forged_detail = RawEvidenceExpectation(
        **{
            **detail.__dict__,
            "request_fingerprint": "f" * 64,
        }
    )
    read_only = Store(database, read_only=True)
    try:
        with pytest.raises(ValueError, match="request identity"):
            build_universe_change_plan(
                store=read_only,
                **{**arguments, "detail_evidence": (forged_detail,)},
            )
    finally:
        read_only.close()


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            "UPDATE universe_coverage_attestations "
            "SET status = 'accepted_no_change'",
            "not blocked",
        ),
        (
            "UPDATE universe_coverage_attestations "
            "SET requested_coverage_through = DATE '2026-07-24'",
            "review clocks are inconsistent",
        ),
        (
            "INSERT INTO ingest_raw_snapshots "
            "SELECT run_id, snapshot_id, 'candidate_release_detail_001', linked_at "
            "FROM ingest_raw_snapshots "
            "WHERE role = 'candidate_release_detail_000'",
            "exactly cover the attestation run",
        ),
        (
            "UPDATE raw_snapshots SET parser_version = '1' "
            "WHERE dataset = 'sp500_change_announcement'",
            "parser_version mismatch",
        ),
        (
            "DELETE FROM ingest_log",
            "one exact ingest outcome",
        ),
        (
            "UPDATE universe_coverage_attestations SET identity_match_count = 0",
            "mismatch count is inconsistent",
        ),
        (
            "UPDATE universe_coverage_attestations SET detail = 'forged detail'",
            "ingest outcome is inconsistent",
        ),
    ),
)
def test_universe_change_plan_fails_closed_on_unsafe_source_state(
    monkeypatch,
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    database, arguments = _create_plan_case(tmp_path)
    writable = Store(database)
    try:
        writable.execute(mutation)
    finally:
        writable.close()

    read_only = Store(database, read_only=True)
    try:
        with pytest.raises(ValueError, match=message):
            build_universe_change_plan(store=read_only, **arguments)
    finally:
        read_only.close()


def test_universe_change_plan_accepts_timezone_neutral_producer_timeline(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    database, arguments = _create_plan_case(tmp_path, database_timezone="UTC")
    read_only = Store(database, read_only=True)
    try:
        plan = build_universe_change_plan(store=read_only, **arguments)
    finally:
        read_only.close()

    assert plan.requested_coverage_through == "2026-07-23"
    assert plan.payload["source_attestation"]["checked_at"].startswith(
        "2026-07-23T21:00:00"
    )
    assert plan.payload["source_attestation"]["completed_new_york_date"] == (
        "2026-07-23"
    )


def test_universe_attestation_quality_does_not_compare_cross_zone_dates(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    database, _arguments = _create_plan_case(tmp_path, database_timezone="UTC")
    store = Store(database)
    try:
        store.execute(
            "UPDATE universe_coverage_attestations SET component_count = 500"
        )
        attestation = store.universe_coverage_attestations(1)[0]
        assert attestation["checked_at"].date() == attestation[
            "completed_new_york_date"
        ]
        quality = {row["check"]: row for row in store.data_quality_report()}
    finally:
        store.close()

    assert quality["universe_attestation_invalid_rows"]["count"] == 0


def test_universe_change_plan_uses_prior_market_session_for_monday_event(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    database, arguments = _create_plan_case(
        tmp_path,
        prior="2026-07-31",
        boundary="2026-08-01",
        target="2026-08-03",
        release_date="Jul 31, 2026",
        release_slug_date="2026-07-31",
        effective_date="August 3, 2026",
        expected_effective_date=date(2026, 8, 3),
        now=datetime(2026, 8, 4, 0, 0, tzinfo=UTC),
    )
    read_only = Store(database, read_only=True)
    try:
        plan = build_universe_change_plan(store=read_only, **arguments)
    finally:
        read_only.close()

    assert plan.prior_coverage_through == "2026-07-31"
    assert plan.effective_date == "2026-08-03"


def test_universe_change_plan_accepts_bounded_unchanged_pre_event_gap(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    database, arguments = _create_plan_case(
        tmp_path,
        prior="2026-08-03",
        boundary="2026-08-04",
        target="2026-08-06",
        release_date="Jul 31, 2026",
        release_slug_date="2026-07-31",
        effective_date="August 5, 2026",
        expected_effective_date=date(2026, 8, 5),
        now=datetime(2026, 8, 7, 13, 0, tzinfo=UTC),
    )
    read_only = Store(database, read_only=True)
    try:
        plan = build_universe_change_plan(store=read_only, **arguments)
    finally:
        read_only.close()

    assert plan.prior_coverage_through == "2026-08-03"
    assert plan.effective_date == "2026-08-05"
    assert plan.requested_coverage_through == "2026-08-06"


def test_universe_change_plan_rejects_stale_pre_event_gap(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    database, arguments = _create_plan_case(
        tmp_path,
        prior="2026-07-27",
        boundary="2026-07-28",
        target="2026-08-06",
        release_date="Jul 31, 2026",
        release_slug_date="2026-07-31",
        effective_date="August 5, 2026",
        expected_effective_date=date(2026, 8, 5),
        now=datetime(2026, 8, 7, 13, 0, tzinfo=UTC),
    )
    read_only = Store(database, read_only=True)
    try:
        with pytest.raises(ValueError, match="too stale"):
            build_universe_change_plan(store=read_only, **arguments)
    finally:
        read_only.close()


def test_universe_change_plan_marks_pre_event_components_as_unverified(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    database, arguments = _create_plan_case(
        tmp_path,
        component_payload=_before_event_components(),
    )
    read_only = Store(database, read_only=True)
    try:
        plan = build_universe_change_plan(store=read_only, **arguments)
    finally:
        read_only.close()

    assert plan.payload["component_set_classification"] == "matches_before"
    assert plan.payload["incoming_component_evidence"] == []
    assert "post_event_component_reconciliation_not_observed" in plan.payload[
        "activation_blockers"
    ]


def test_universe_change_plan_replays_all_producer_candidate_details(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    database, arguments = _create_plan_case(
        tmp_path,
        include_future_detail=True,
    )
    read_only = Store(database, read_only=True)
    try:
        plan = build_universe_change_plan(store=read_only, **arguments)
    finally:
        read_only.close()

    details = [
        row for row in plan.payload["evidence"] if row["role"].startswith("candidate_release")
    ]
    assert [row["role"] for row in details] == [
        "candidate_release_detail_000",
        "candidate_release_detail_001",
    ]
    selected = next(
        row
        for row in details
        if row["source_url"] == arguments["official_release_url"]
    )
    assert selected["role"] == "candidate_release_detail_001"
    future = plan.payload["source_attestation"]["mismatch_detail"][
        "future_effective_releases"
    ]
    assert future[0]["changes"][0]["effective_date"] == "2026-07-24"


def test_universe_change_plan_rejects_cross_project_database(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    database, arguments = _create_plan_case(tmp_path / "project-a")
    other_root = tmp_path / "project-b"
    other_root.mkdir()
    read_only = Store(database, read_only=True)
    try:
        with pytest.raises(ValueError, match="not bound"):
            build_universe_change_plan(
                store=read_only,
                **{**arguments, "project_root": other_root},
            )
    finally:
        read_only.close()


def test_universe_change_plan_ignores_lock_residue_but_rejects_hardlinks(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    database, arguments = _create_plan_case(tmp_path)
    lock = tmp_path / "data/paper/account.json.lock"
    lock.write_text("operational residue", encoding="utf-8")
    read_only = Store(database, read_only=True)
    try:
        plan = build_universe_change_plan(store=read_only, **arguments)
    finally:
        read_only.close()
    assert all(not row["path"].endswith(".lock") for row in plan.payload["paper_state"]["files"])

    alias = tmp_path / "data/paper/account-alias.json"
    os.link(tmp_path / "data/paper/account.json", alias)
    read_only = Store(database, read_only=True)
    try:
        with pytest.raises(ValueError, match="regular unaliased"):
            build_universe_change_plan(store=read_only, **arguments)
    finally:
        read_only.close()


def test_universe_change_plan_rejects_symlinked_data_ancestor(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    database, arguments = _create_plan_case(tmp_path)
    actual_data = tmp_path / "actual-data"
    (tmp_path / "data").rename(actual_data)
    (tmp_path / "data").symlink_to(actual_data, target_is_directory=True)
    with pytest.raises(ValueError, match="cannot contain symbolic links"):
        Store(database, read_only=True)


def test_universe_change_plan_rejects_concurrent_paper_change(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    database, arguments = _create_plan_case(tmp_path)
    original_capture = universe_change_module._capture_paper_tree_once
    captures = 0

    def mutate_after_first(root: Path) -> list[dict]:
        nonlocal captures
        result = original_capture(root)
        captures += 1
        if captures == 1:
            (root / "data/paper/account.json").write_text(
                '{"simulation_only":false}\n',
                encoding="utf-8",
            )
        return result

    monkeypatch.setattr(
        universe_change_module,
        "_capture_paper_tree_once",
        mutate_after_first,
    )
    read_only = Store(database, read_only=True)
    try:
        with pytest.raises(ValueError, match="paper state changed"):
            build_universe_change_plan(store=read_only, **arguments)
    finally:
        read_only.close()


def test_universe_change_plan_binds_store_connection_database_identity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    database, arguments = _create_plan_case(tmp_path)
    read_only = Store(database, read_only=True)
    opened_database = database.with_name("opened-aios.duckdb")
    database.rename(opened_database)
    shutil.copy2(opened_database, database)
    try:
        with pytest.raises(ValueError, match="identity no longer matches"):
            build_universe_change_plan(store=read_only, **arguments)
    finally:
        read_only.close()


def test_universe_change_plan_rejects_implausible_future_lineage(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    database, arguments = _create_plan_case(tmp_path)
    writable = Store(database)
    try:
        writable.execute(
            "UPDATE universe_coverage_attestations "
            "SET created_at = TIMESTAMP '2099-01-01 00:00:00'"
        )
        writable.execute(
            "UPDATE ingest_log SET finished_at = TIMESTAMP '2099-01-01 00:00:01'"
        )
    finally:
        writable.close()
    read_only = Store(database, read_only=True)
    try:
        with pytest.raises(ValueError, match="clocks are inconsistent"):
            build_universe_change_plan(store=read_only, **arguments)
    finally:
        read_only.close()


def test_universe_change_plan_tolerates_legacy_reader_timezone_change(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    database, arguments = _create_plan_case(tmp_path)
    read_only = Store(database, read_only=True)
    try:
        read_only.execute("SET TimeZone = 'UTC'")
        plan = build_universe_change_plan(store=read_only, **arguments)
    finally:
        read_only.close()
    assert "legacy_timestamp_clock_provenance_not_persisted" in plan.payload[
        "activation_blockers"
    ]


def test_universe_change_plan_bounds_all_paper_entries(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    database, arguments = _create_plan_case(tmp_path)
    monkeypatch.setattr(universe_change_module, "_MAX_PAPER_ENTRIES", 2)
    (tmp_path / "data/paper/first.lock").touch()
    (tmp_path / "data/paper/second.lock").touch()
    read_only = Store(database, read_only=True)
    try:
        with pytest.raises(ValueError, match="entry limit"):
            build_universe_change_plan(store=read_only, **arguments)
    finally:
        read_only.close()

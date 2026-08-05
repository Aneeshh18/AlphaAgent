from __future__ import annotations

import json
from datetime import UTC, datetime

from aios.ingest.http_client import RawSnapshotContext
from aios.raw_snapshots import canonical_request_fingerprint, capture_raw_snapshot
from aios.storage.store import Store
from aios.universe_rollforward import (
    COMPONENT_SNAPSHOT_URL,
    OFFICIAL_ARCHIVE_URL,
    parse_component_snapshot,
    parse_press_archive,
    parse_sp500_constituent_changes,
    roll_forward_sp500_coverage,
)


def _press_html(
    title: str,
    *,
    url: str = "https://press.spglobal.com/2026-07-22-test-release",
) -> bytes:
    return f"""
    <html><body>
      <ul class="wd_layout-simple wd_item_list">
        <li class="wd_item"><div class="wd_item_wrapper">
          <div class="wd_date">Jul 22, 2026</div>
          <div class="wd_title">
            <a href="{url}">{title}</a>
          </div>
        </div></li>
        <li class="wd_item"><div class="wd_item_wrapper">
          <div class="wd_date">Jul 20, 2026</div>
          <div class="wd_title">
            <a href="https://press.spglobal.com/2026-07-20-older-release">
              S&amp;P Global publishes a market study
            </a>
          </div>
        </div></li>
      </ul>
    </body></html>
    """.encode()


def _component_csv() -> bytes:
    return (
        b"Symbol,Security,GICS Sector,GICS Sub-Industry,Headquarters Location,"
        b"Date added,CIK,Founded\n"
        b"AAA,Alpha,,,,2020-01-01,1,2000\n"
        b"BBB,Beta,,,,2020-01-01,2,2000\n"
    )


def _change_detail_html(*, effective_date: str = "July 25, 2026") -> bytes:
    return f"""
    <html><body><table>
      <tr><th>Effective Date</th><th>Index Name</th><th>Action</th>
          <th>Company Name</th><th>Ticker</th><th>GICS Sector</th></tr>
      <tr><td>{effective_date}</td><td>S&amp;P 500</td><td>Addition</td>
          <td>Gamma Incorporated</td><td>GAM</td><td>Industrials</td></tr>
      <tr><td>{effective_date}</td><td>S&amp;P 500</td><td>Deletion</td>
          <td>Beta Incorporated</td><td>BBB</td><td>Industrials</td></tr>
    </table></body></html>
    """.encode()


def _multi_index_change_detail_html() -> bytes:
    return b"""
    <html><body><table>
      <tr><th>Effective Date</th><th>Index Name</th><th>Action</th>
          <th>Company Name</th><th>Ticker</th><th>GICS Sector</th></tr>
      <tr><td>Mar 23, 2026</td><td>S&amp;P 100</td><td>Addition</td>
          <td>Other Large Company</td><td>OTHER</td><td>Industrials</td></tr>
      <tr><td>Mar 23, 2026</td><td>S&amp;P 500</td><td>Addition</td>
          <td>Gamma Incorporated</td><td>GAM</td><td>Industrials</td></tr>
      <tr><td>Mar 23, 2026</td><td>S&amp;P 500</td><td>Deletion</td>
          <td>Beta Incorporated</td><td>BBB</td><td>Industrials</td></tr>
      <tr><td>Mar 23, 2026</td><td>S&amp;P MidCap 400</td><td>Deletion</td>
          <td>Another Company</td><td>ANOT</td><td>Industrials</td></tr>
      <tr><td>Mar 23, 2026</td><td>S&amp;P SmallCap 600</td><td>Addition</td>
          <td>Small Company</td><td>SMAL</td><td>Industrials</td></tr>
    </table></body></html>
    """


def _seed_bounded_references(store: Store) -> None:
    memberships = [
        {
            "universe_id": "sp500",
            "ticker": ticker,
            "security_id": f"security:{ticker.lower()}",
            "effective_start": "2026-01-01",
            "effective_end": "2026-07-22",
            "known_date": "2025-12-15",
            "end_known_date": "2026-07-21",
            "source": "test|coverage-end:2026-07-21",
        }
        for ticker in ("AAA", "BBB")
    ]
    store.upsert_universe_membership(memberships)
    store.upsert_security_identities(
        [
            {
                **row,
                "identity_status": "bounded_ticker",
            }
            for row in memberships
        ]
    )
    store.upsert_reference_identities(
        issuers=[
            {
                "issuer_id": "issuer:aaa-old",
                "canonical_name": "Alpha predecessor",
                "canonical_ticker": "AAA",
                "source": "test",
            },
            {
                "issuer_id": "issuer:aaa",
                "canonical_name": "Alpha successor",
                "canonical_ticker": "AAA",
                "source": "test",
            },
            {
                "issuer_id": "issuer:bbb",
                "canonical_name": "Beta",
                "canonical_ticker": "BBB",
                "source": "test",
            },
        ],
        cik_history=[
            {
                "issuer_id": "issuer:aaa-old",
                "cik": "1",
                "effective_start": "2026-01-01",
                "effective_end": "2026-07-02",
                "verified_date": "2026-07-21",
                "source": "test",
            },
            {
                "issuer_id": "issuer:aaa",
                "cik": "11",
                "effective_start": "2026-07-02",
                "effective_end": "2026-07-22",
                "verified_date": "2026-07-21",
                "source": "test",
            },
            {
                "issuer_id": "issuer:bbb",
                "cik": "2",
                "effective_start": "2026-01-01",
                "effective_end": "2026-07-22",
                "verified_date": "2026-07-21",
                "source": "test",
            },
        ],
        security_issuers=[
            {
                "security_id": "security:aaa",
                "issuer_id": "issuer:aaa-old",
                "effective_start": "2026-01-01",
                "effective_end": "2026-07-02",
                "verified_date": "2026-07-21",
                "source": "test",
            },
            {
                "security_id": "security:aaa",
                "issuer_id": "issuer:aaa",
                "effective_start": "2026-07-02",
                "effective_end": "2026-07-22",
                "verified_date": "2026-07-21",
                "source": "test",
            },
            {
                "security_id": "security:bbb",
                "issuer_id": "issuer:bbb",
                "effective_start": "2026-01-01",
                "effective_end": "2026-07-22",
                "verified_date": "2026-07-21",
                "source": "test",
            },
        ],
        provider_symbols=[
            {
                "provider": "yfinance",
                "provider_symbol": ticker,
                "security_id": f"security:{ticker.lower()}",
                "data_start": "2026-01-01",
                "data_end": "2026-07-22",
                "mapping_status": "verified",
                "verified_date": "2026-07-21",
                "source": "test",
            }
            for ticker in ("AAA", "BBB")
        ],
    )
    store.upsert_prices(
        [
            {
                "ticker": "SPY",
                "date": observed,
                "close": 500.0,
                "source": "test",
            }
            for observed in ("2026-07-21", "2026-07-22", "2026-07-23")
        ]
    )


def _archiving_fetcher(
    payload_by_url: dict[str, bytes],
    *,
    now: datetime,
):
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


def test_public_source_parsers_keep_dates_symbols_and_ciks_exact(monkeypatch) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)

    releases = parse_press_archive(_press_html("Example Set to Join S&P 500"))
    components = parse_component_snapshot(_component_csv())

    assert releases[0].release_date.isoformat() == "2026-07-22"
    assert releases[0].title == "Example Set to Join S&P 500"
    assert [(row.ticker, row.cik) for row in components] == [
        ("AAA", "0000000001"),
        ("BBB", "0000000002"),
    ]
    changes = parse_sp500_constituent_changes(_change_detail_html())
    assert [change.to_dict() for change in changes] == [
        {
            "effective_date": "2026-07-25",
            "action": "addition",
            "company_name": "Gamma Incorporated",
            "ticker": "GAM",
        },
        {
            "effective_date": "2026-07-25",
            "action": "deletion",
            "company_name": "Beta Incorporated",
            "ticker": "BBB",
        },
    ]


def test_change_parser_scopes_multi_index_table_and_accepts_abbreviated_date() -> None:
    changes = parse_sp500_constituent_changes(_multi_index_change_detail_html())

    assert [change.to_dict() for change in changes] == [
        {
            "effective_date": "2026-03-23",
            "action": "addition",
            "company_name": "Gamma Incorporated",
            "ticker": "GAM",
        },
        {
            "effective_date": "2026-03-23",
            "action": "deletion",
            "company_name": "Beta Incorporated",
            "ticker": "BBB",
        },
    ]


def test_change_parser_rejects_action_row_without_named_sp_index() -> None:
    payload = b"""
    <table>
      <tr><td>August 5, 2026</td><td></td><td>Addition</td>
          <td>S&amp;P Global</td><td>SPGI</td></tr>
      <tr><td>August 5, 2026</td><td>S&amp;P 500</td><td>Deletion</td>
          <td>Beta Incorporated</td><td>BBB</td></tr>
    </table>
    """

    try:
        parse_sp500_constituent_changes(payload)
    except ValueError as exc:
        assert str(exc) == "S&P change table has an ambiguous index/action row"
    else:
        raise AssertionError("unlabeled action row must fail closed")


def test_percent_encoded_official_release_source_is_not_reopened(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    now = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
    literal_url = "https://press.spglobal.com/2026-07-22-Alpha,-Beta"
    encoded_url = "https://press.spglobal.com/2026-07-22-Alpha%2C-Beta"
    store = Store(tmp_path / "encoded-source-rollforward.duckdb")
    try:
        _seed_bounded_references(store)
        store.execute(
            "UPDATE universe_membership SET source = ? WHERE universe_id = 'sp500'",
            (f"start:{encoded_url}",),
        )
        result = roll_forward_sp500_coverage(
            store=store,
            now=now,
            project_root=tmp_path,
            fetch_bytes=_archiving_fetcher(
                {
                    OFFICIAL_ARCHIVE_URL: _press_html(
                        "Alpha Set to Join S&P 500",
                        url=literal_url,
                    ),
                    COMPONENT_SNAPSHOT_URL: _component_csv(),
                },
                now=now,
            ),
        )

        assert result.status == "extended"
        assert result.relevant_release_count == 0
        assert len(store.universe_membership_on("sp500", "2026-07-23")) == 2
    finally:
        store.close()


def test_no_change_attestation_extends_every_reference_window_atomically(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    now = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
    store = Store(tmp_path / "rollforward.duckdb")
    try:
        _seed_bounded_references(store)
        result = roll_forward_sp500_coverage(
            store=store,
            now=now,
            project_root=tmp_path,
            fetch_bytes=_archiving_fetcher(
                {
                    OFFICIAL_ARCHIVE_URL: _press_html(
                        "S&P Global publishes an unrelated market study"
                    ),
                    COMPONENT_SNAPSHOT_URL: _component_csv(),
                },
                now=now,
            ),
        )

        assert result.status == "extended"
        assert result.requested_coverage_through == "2026-07-23"
        assert result.rows_extended == {
            "membership_rows_extended": 2,
            "security_rows_extended": 2,
            "owner_rows_extended": 2,
            "cik_rows_extended": 2,
            "provider_rows_extended": 2,
        }
        assert len(store.universe_membership_on("sp500", "2026-07-23")) == 2
        attestation = store.universe_coverage_attestations(1)[0]
        assert attestation["status"] == "accepted_no_change"
        lineage_matches = json.loads(attestation["mismatch_detail_json"])[
            "reviewed_successor_lineage_matches"
        ]
        assert lineage_matches[0]["ticker"] == "AAA"
        assert store.query("SELECT COUNT(*) AS n FROM raw_snapshots")[0]["n"] == 2
        assert store.query(
            """
            SELECT
                COUNT(*) FILTER (WHERE effective_end = DATE '2026-07-24') AS identities
            FROM security_identity_assignments
            """
        )[0]["identities"] == 2
        assert store.query(
            """
            SELECT COUNT(*) FILTER (WHERE data_end = DATE '2026-07-24') AS mappings
            FROM provider_symbol_history
            """
        )[0]["mappings"] == 2
    finally:
        store.close()


def test_official_change_candidate_blocks_without_extending_dates(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    now = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
    store = Store(tmp_path / "blocked-rollforward.duckdb")
    try:
        _seed_bounded_references(store)
        result = roll_forward_sp500_coverage(
            store=store,
            now=now,
            project_root=tmp_path,
            fetch_bytes=_archiving_fetcher(
                {
                    OFFICIAL_ARCHIVE_URL: _press_html("Gamma Set to Join S&P 500"),
                    COMPONENT_SNAPSHOT_URL: _component_csv(),
                },
                now=now,
            ),
        )

        assert result.status == "review_required"
        assert result.relevant_release_count == 1
        assert store.universe_membership_on("sp500", "2026-07-23") == []
        attestation = store.universe_coverage_attestations(1)[0]
        assert attestation["status"] == "blocked_review_required"
        assert attestation["membership_rows_extended"] == 0
    finally:
        store.close()


def test_future_effective_official_change_allows_only_pre_effective_coverage(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    now = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
    detail_url = "https://press.spglobal.com/2026-07-22-test-release"
    store = Store(tmp_path / "future-rollforward.duckdb")
    try:
        _seed_bounded_references(store)
        result = roll_forward_sp500_coverage(
            store=store,
            now=now,
            project_root=tmp_path,
            fetch_bytes=_archiving_fetcher(
                {
                    OFFICIAL_ARCHIVE_URL: _press_html("Gamma Set to Join S&P 500"),
                    COMPONENT_SNAPSHOT_URL: _component_csv(),
                    detail_url: _change_detail_html(effective_date="July 25, 2026"),
                },
                now=now,
            ),
        )

        assert result.status == "extended"
        assert result.requested_coverage_through == "2026-07-23"
        assert result.relevant_release_count == 0
        assert len(store.universe_membership_on("sp500", "2026-07-23")) == 2
        attestation = store.universe_coverage_attestations(1)[0]
        future = json.loads(attestation["mismatch_detail_json"])[
            "future_effective_releases"
        ]
        assert future == [
            {
                "release_date": "2026-07-22",
                "title": "Gamma Set to Join S&P 500",
                "url": detail_url,
                "changes": [
                    {
                        "effective_date": "2026-07-25",
                        "action": "addition",
                        "company_name": "Gamma Incorporated",
                        "ticker": "GAM",
                    },
                    {
                        "effective_date": "2026-07-25",
                        "action": "deletion",
                        "company_name": "Beta Incorporated",
                        "ticker": "BBB",
                    },
                ],
            }
        ]
        assert store.query("SELECT COUNT(*) AS n FROM raw_snapshots")[0]["n"] == 3
    finally:
        store.close()


def test_change_blocks_when_effective_on_requested_close(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("aios.universe_rollforward.MINIMUM_MEMBERS", 2)
    monkeypatch.setattr("aios.universe_rollforward.MAXIMUM_MEMBERS", 3)
    now = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
    detail_url = "https://press.spglobal.com/2026-07-22-test-release"
    store = Store(tmp_path / "effective-rollforward.duckdb")
    try:
        _seed_bounded_references(store)
        result = roll_forward_sp500_coverage(
            store=store,
            now=now,
            project_root=tmp_path,
            fetch_bytes=_archiving_fetcher(
                {
                    OFFICIAL_ARCHIVE_URL: _press_html("Gamma Set to Join S&P 500"),
                    COMPONENT_SNAPSHOT_URL: _component_csv(),
                    detail_url: _change_detail_html(effective_date="July 23, 2026"),
                },
                now=now,
            ),
        )

        assert result.status == "review_required"
        assert result.relevant_release_count == 1
        assert store.universe_membership_on("sp500", "2026-07-23") == []
    finally:
        store.close()

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import duckdb
import pytest

from aios.ingest import edgar
from aios.raw_snapshots import canonical_parsed_rows_sha256
from aios.storage import store as store_module
from aios.storage.store import Store

ISSUER_ID = "aios:issuer:sec:1"
SECURITY_ID = "aios:security:test-common"


def _install_issuer_reference(store: Store) -> None:
    store.execute(
        """
        INSERT INTO security_master
        (security_id, canonical_ticker, identity_status, source)
        VALUES (?, 'NEW', 'verified_ticker_change', 'test')
        """,
        (SECURITY_ID,),
    )
    store.upsert_reference_identities(
        [
            {
                "issuer_id": ISSUER_ID,
                "canonical_name": "Test Corporation",
                "canonical_ticker": "NEW",
                "source": "test",
            }
        ],
        [
            {
                "issuer_id": ISSUER_ID,
                "cik": "1",
                "effective_start": "2024-01-01",
                "effective_end": None,
                "verified_date": "2024-01-01",
                "source": "test",
            }
        ],
        [
            {
                "security_id": SECURITY_ID,
                "issuer_id": ISSUER_ID,
                "effective_start": "2024-01-01",
                "effective_end": None,
                "verified_date": "2024-01-01",
                "source": "test",
            }
        ],
        [
            {
                "provider": "yfinance",
                "provider_symbol": "NEW",
                "security_id": SECURITY_ID,
                "data_start": "2024-01-01",
                "data_end": None,
                "mapping_status": "verified",
                "verified_date": "2024-01-01",
                "source": "test",
            }
        ],
    )


def _issuer_fundamental(
    value: float,
    *,
    ticker: str = "NEW",
    provenance: bool = False,
) -> dict:
    row = {
        "ticker": ticker,
        "issuer_id": ISSUER_ID,
        "security_id": SECURITY_ID,
        "period_end": "2025-12-31",
        "as_of_date": "2026-02-01",
        "fiscal_period": "FY2025",
        "statement": "income",
        "metric": "revenue",
        "value": value,
        "quarter_value": value,
        "unit": "USD",
        "source": "edgar",
    }
    if provenance:
        row.update(
            {
                "ingest_run_id": "accepted-run",
                "source_snapshot_id": "accepted-snapshot",
                "source_rowset_sha256": "a" * 64,
                "source_row_sha256": "b" * 64,
            }
        )
    return row


def _issuer_security_rows() -> list[dict]:
    return [
        {
            "ticker": "NEW",
            "cik": 1,
            "name": "Test Corporation",
            "exchange": "NYSE",
            "sector": "Test",
            "industry": "Test",
            "market_cap_bucket": None,
            "sic_code": "0000",
        }
    ]


def _issuer_submissions_row() -> dict:
    return {
        "cik": "0000000001",
        "name": "Test Corporation",
        "sic": "0000",
        "sic_description": "Test",
        "exchanges": ["NYSE"],
    }


def _issuer_commit_arguments(*, run_id: str) -> dict:
    return {
        "issuer_id": ISSUER_ID,
        "canonical_ticker": "NEW",
        "security_rows": _issuer_security_rows(),
        "submissions_row": _issuer_submissions_row(),
        "run_id": run_id,
        "source": "edgar:issuer-cik-history",
        "started_at": datetime(2026, 7, 30, 10, 0, tzinfo=UTC),
    }


def _record_lineaged_issuer_row(
    store: Store,
    *,
    run_id: str,
    snapshot_id: str,
    value: float = 100.0,
    rows_rejected: int = 0,
    rejection_codes: str | None = None,
) -> dict:
    return _record_lineaged_issuer_rows(
        store,
        run_id=run_id,
        snapshot_id=snapshot_id,
        rows=[_issuer_fundamental(value)],
        rows_rejected=rows_rejected,
        rejection_codes=rejection_codes,
    )[0]


def _provider_fundamental_row(row: dict) -> dict:
    provider_row = {
        "cik": "0000000001",
        "period_end": row["period_end"],
        "as_of_date": row["as_of_date"],
        "fiscal_period": row["fiscal_period"],
        "statement": row["statement"],
        "metric": row["metric"],
        "value": row["value"],
        "quarter_value": row["quarter_value"],
        "unit": row["unit"],
        "source": row["source"],
    }
    if row.get("source_fact_locator") is not None:
        provider_row["source_fact_locator"] = row["source_fact_locator"]
    return provider_row


def _record_lineaged_issuer_rows(
    store: Store,
    *,
    run_id: str,
    snapshot_id: str,
    rows: list[dict],
    rows_rejected: int = 0,
    rejection_codes: str | None = None,
) -> list[dict]:
    observed_at = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
    provider_rows = [_provider_fundamental_row(row) for row in rows]
    rowset_sha256 = canonical_parsed_rows_sha256(provider_rows)
    companyfacts_payload_sha256 = hashlib.sha256(
        f"companyfacts:{snapshot_id}".encode()
    ).hexdigest()
    submissions_payload_sha256 = hashlib.sha256(
        f"submissions:{snapshot_id}".encode()
    ).hexdigest()
    submissions_rowset_sha256 = canonical_parsed_rows_sha256(
        [_issuer_submissions_row()]
    )
    store.record_raw_snapshot(
        payload={
            "payload_sha256": companyfacts_payload_sha256,
            "relative_path": f"data/raw/sec/companyfacts/{snapshot_id}.json.gz",
            "original_bytes": 120,
            "stored_bytes": 80,
            "compression": "gzip",
        },
        snapshot={
            "snapshot_id": snapshot_id,
            "provider": "sec-edgar",
            "dataset": "companyfacts",
            "artifact_kind": "exact_response",
            "requested_at": observed_at,
            "received_at": observed_at,
            "http_status": 200,
            "content_type": "application/json",
            "request_fingerprint": "f" * 64,
            "payload_sha256": companyfacts_payload_sha256,
            "adapter_name": "edgar",
            "adapter_version": "v1",
            "parser_version": "sec-companyfacts-v2",
            "parsed_row_count": len(rows),
            "parsed_rows_sha256": rowset_sha256,
            "parsed_rows_rejected": rows_rejected,
            "parsed_rejection_codes": rejection_codes,
        },
        ingest_run_id=run_id,
        role="companyfacts",
    )
    store.record_raw_snapshot(
        payload={
            "payload_sha256": submissions_payload_sha256,
            "relative_path": f"data/raw/sec/submissions/{snapshot_id}.json.gz",
            "original_bytes": 60,
            "stored_bytes": 40,
            "compression": "gzip",
        },
        snapshot={
            "snapshot_id": f"{snapshot_id}-submissions",
            "provider": "sec-edgar",
            "dataset": "submissions",
            "artifact_kind": "exact_response",
            "requested_at": observed_at,
            "received_at": observed_at,
            "http_status": 200,
            "content_type": "application/json",
            "request_fingerprint": "c" * 64,
            "payload_sha256": submissions_payload_sha256,
            "adapter_name": "edgar",
            "adapter_version": "v1",
            "parser_version": "sec-submissions-v2",
            "parsed_row_count": 1,
            "parsed_rows_sha256": submissions_rowset_sha256,
            "parsed_rows_rejected": 0,
            "parsed_rejection_codes": None,
        },
        ingest_run_id=run_id,
        role="submissions",
    )
    lineaged: list[dict] = []
    for row, provider_row in zip(rows, provider_rows, strict=True):
        lineaged.append(
            {
                **row,
                "ingest_run_id": run_id,
                "source_snapshot_id": snapshot_id,
                "source_rowset_sha256": rowset_sha256,
                "source_row_sha256": edgar.canonical_sec_fundamental_row_sha256(
                    provider_row
                ),
            }
        )
    return lineaged


def _create_legacy_ingest_db(path, *, partial_subject: bool = False) -> None:
    con = duckdb.connect(str(path))
    subject_column = ", subject_type VARCHAR" if partial_subject else ""
    con.execute("CREATE SEQUENCE ingest_seq")
    con.execute(
        f"""
        CREATE TABLE ingest_log (
            id BIGINT PRIMARY KEY DEFAULT nextval('ingest_seq'),
            run_id VARCHAR NOT NULL,
            source VARCHAR NOT NULL,
            table_name VARCHAR NOT NULL,
            rows_inserted BIGINT,
            rows_rejected BIGINT,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            status VARCHAR,
            error TEXT
            {subject_column}
        )
        """
    )
    con.execute(
        """
        INSERT INTO ingest_log
        (run_id, source, table_name, rows_inserted, rows_rejected, status)
        VALUES ('legacy-run', 'edgar', 'fundamentals', 0, 0, 'warning')
        """
    )
    if not partial_subject:
        con.execute(
            """
            CREATE TABLE raw_payloads (
                payload_sha256 VARCHAR PRIMARY KEY,
                relative_path VARCHAR NOT NULL UNIQUE,
                original_bytes BIGINT NOT NULL,
                stored_bytes BIGINT NOT NULL,
                compression VARCHAR NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE raw_snapshots (
                snapshot_id VARCHAR PRIMARY KEY,
                provider VARCHAR NOT NULL,
                dataset VARCHAR NOT NULL,
                artifact_kind VARCHAR NOT NULL,
                requested_at TIMESTAMP NOT NULL,
                received_at TIMESTAMP NOT NULL,
                http_status INTEGER,
                content_type VARCHAR,
                request_fingerprint VARCHAR NOT NULL,
                payload_sha256 VARCHAR NOT NULL,
                adapter_name VARCHAR NOT NULL,
                adapter_version VARCHAR NOT NULL,
                parser_version VARCHAR NOT NULL,
                parsed_row_count BIGINT,
                parsed_rows_sha256 VARCHAR
            )
            """
        )
        con.execute(
            """
            CREATE TABLE ingest_raw_snapshots (
                run_id VARCHAR NOT NULL,
                snapshot_id VARCHAR NOT NULL,
                role VARCHAR NOT NULL,
                linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (run_id, snapshot_id, role)
            )
            """
        )
        con.execute(
            """
            INSERT INTO raw_payloads
            VALUES ('payload-hash', 'data/raw/sec/companyfacts/payload.json.gz',
                    120, 80, 'gzip')
            """
        )
        con.execute(
            """
            INSERT INTO raw_snapshots
            VALUES ('snapshot-1', 'sec', 'companyfacts', 'exact_response',
                    '2026-07-29 10:00:00', '2026-07-29 10:00:01', 200,
                    'application/json', 'request-hash', 'payload-hash',
                    'edgar', 'v1', 'companyfacts-v1', 0, 'rows-hash')
            """
        )
        con.execute(
            """
            INSERT INTO ingest_raw_snapshots
            (run_id, snapshot_id, role)
            VALUES ('legacy-run', 'snapshot-1', 'companyfacts')
            """
        )
    con.close()


def test_record_ingest_requires_an_all_or_none_nonblank_subject(tmp_path) -> None:
    store = Store(tmp_path / "subject-validation.duckdb")
    try:
        invalid = (
            {"subject_type": "issuer"},
            {"subject_id": "aios:issuer:sec:1"},
            {"subject_type": "", "subject_id": "aios:issuer:sec:1"},
            {"subject_type": "issuer", "subject_id": "  "},
        )
        for kwargs in invalid:
            with pytest.raises(ValueError, match="subject_type and subject_id"):
                store.record_ingest("edgar", "fundamentals", **kwargs)

        run_id = store.record_ingest(
            "edgar",
            "fundamentals",
            subject_type=" Issuer ",
            subject_id=" aios:issuer:sec:1 ",
        )
        evidence = store.ingest_evidence(run_id)

        assert evidence is not None
        assert evidence["subject_type"] == "issuer"
        assert evidence["subject_id"] == "aios:issuer:sec:1"
        assert evidence["snapshots"] == []
        assert store.query("SELECT COUNT(*) AS n FROM ingest_log")[0]["n"] == 1
    finally:
        store.close()


def test_record_ingest_persists_canonical_structured_rejection_codes(tmp_path) -> None:
    store = Store(tmp_path / "structured-rejections.duckdb")
    try:
        run_id = store.record_ingest(
            "edgar:issuer-cik-history",
            "fundamentals",
            rows_inserted=3,
            rows_rejected=2,
            status="warning",
            error="operator-facing detail",
            subject_type="issuer",
            subject_id=ISSUER_ID,
            rejection_codes=["unsupported_context", "future_period"],
        )

        evidence = store.ingest_evidence(run_id)
        assert evidence is not None
        assert evidence["rejection_codes"] == (
            '["future_period","unsupported_context"]'
        )
        assert store.ingest_history(1)[0]["rejection_codes"] == (
            '["future_period","unsupported_context"]'
        )

        with pytest.raises(ValueError, match="lowercase snake_case"):
            store.record_ingest(
                "edgar",
                "fundamentals",
                rejection_codes=["Free Form"],
            )
        assert store.query("SELECT COUNT(*) AS n FROM ingest_log") == [{"n": 1}]
    finally:
        store.close()


def test_issuer_commit_rejects_contradictory_outcome_metadata(tmp_path) -> None:
    store = Store(tmp_path / "issuer-outcome-contract.duckdb")
    security_rows = [
        {
            "ticker": "NEW",
            "cik": 1,
            "name": "Test Corporation",
            "exchange": "NYSE",
            "sector": "Test",
            "industry": "Test",
            "market_cap_bucket": None,
            "sic_code": "0000",
        }
    ]
    common = {
        "issuer_id": ISSUER_ID,
        "canonical_ticker": "NEW",
        "security_rows": security_rows,
        "submissions_row": _issuer_submissions_row(),
        "source": "edgar:issuer-cik-history",
        "started_at": datetime(2026, 7, 30, 10, 0, tzinfo=UTC),
    }
    invalid = [
        {
            "rows": [_issuer_fundamental(100.0)],
            "rows_rejected": 0,
            "status": "success",
            "error": "unexpected detail",
            "rejection_codes": [],
        },
        {
            "rows": [_issuer_fundamental(100.0)],
            "rows_rejected": 1,
            "status": "warning",
            "error": "withheld one row",
            "rejection_codes": ["unknown_policy"],
        },
        {
            "rows": [_issuer_fundamental(100.0)],
            "rows_rejected": 1,
            "status": "warning",
            "error": "withheld one row",
            "rejection_codes": [],
        },
        {
            "rows": [_issuer_fundamental(100.0)],
            "rows_rejected": 0,
            "status": "warning",
            "error": "SEC returned no fundamental rows",
            "rejection_codes": [],
        },
    ]
    try:
        _install_issuer_reference(store)
        for index, values in enumerate(invalid):
            with pytest.raises((TypeError, ValueError)):
                store.commit_issuer_fundamental_ingest(
                    **common,
                    **values,
                    run_id=f"invalid-{index}",
                )

        assert store.query("SELECT COUNT(*) AS n FROM fundamentals") == [{"n": 0}]
        assert store.query("SELECT COUNT(*) AS n FROM ingest_log") == [{"n": 0}]

        accepted_row = _record_lineaged_issuer_row(
            store,
            run_id="contract-run",
            snapshot_id="contract-snapshot",
            rows_rejected=1,
            rejection_codes='["future_period"]',
        )
        inserted, stale = store.commit_issuer_fundamental_ingest(
            **common,
            run_id="contract-run",
            rows=[accepted_row],
            rows_rejected=1,
            status="warning",
            error="withheld one future-period row",
            rejection_codes=["future_period"],
        )
        assert (inserted, stale) == (1, 0)
        assert store.ingest_history(1)[0]["rejection_codes"] == '["future_period"]'
    finally:
        store.close()


def test_zero_row_issuer_commit_requires_exact_source_evidence(tmp_path) -> None:
    store = Store(tmp_path / "zero-row-lineage.duckdb")
    try:
        _install_issuer_reference(store)
        with pytest.raises(
            ValueError,
            match="one linked Company Facts response",
        ):
            store.commit_issuer_fundamental_ingest(
                [],
                **_issuer_commit_arguments(run_id="missing-zero-evidence"),
                rows_rejected=0,
                status="warning",
                error="SEC returned no fundamental rows",
            )

        assert store.query("SELECT COUNT(*) AS n FROM ingest_log") == [{"n": 0}]
        _record_lineaged_issuer_rows(
            store,
            run_id="verified-zero-evidence",
            snapshot_id="verified-zero-snapshot",
            rows=[],
        )
        assert store.commit_issuer_fundamental_ingest(
            [],
            **_issuer_commit_arguments(run_id="verified-zero-evidence"),
            rows_rejected=0,
            status="warning",
            error="SEC returned no fundamental rows",
        ) == (0, 0)
        assert store.ingest_history(1)[0]["status"] == "warning"
    finally:
        store.close()


def test_issuer_commit_requires_linked_submissions_evidence(tmp_path) -> None:
    store = Store(tmp_path / "missing-submissions.duckdb")
    try:
        _install_issuer_reference(store)
        row = _record_lineaged_issuer_row(
            store,
            run_id="missing-submissions",
            snapshot_id="missing-submissions-snapshot",
        )
        store.execute(
            """
            DELETE FROM ingest_raw_snapshots
            WHERE run_id = 'missing-submissions' AND role = 'submissions'
            """
        )

        with pytest.raises(
            ValueError,
            match="one linked SEC Submissions response",
        ):
            store.commit_issuer_fundamental_ingest(
                [row],
                **_issuer_commit_arguments(run_id="missing-submissions"),
                rows_rejected=0,
                status="success",
                error=None,
            )
        assert store.query("SELECT COUNT(*) AS n FROM fundamentals") == [{"n": 0}]
    finally:
        store.close()


def test_issuer_commit_recomputes_each_row_and_the_complete_rowset(tmp_path) -> None:
    store = Store(tmp_path / "complete-rowset.duckdb")
    try:
        _install_issuer_reference(store)
        source_rows = [
            _issuer_fundamental(100.0),
            {
                **_issuer_fundamental(20.0),
                "metric": "net_income",
            },
        ]
        lineaged = _record_lineaged_issuer_rows(
            store,
            run_id="complete-rowset",
            snapshot_id="complete-rowset-snapshot",
            rows=source_rows,
        )

        bad_hash = {**lineaged[0], "source_row_sha256": "0" * 64}
        with pytest.raises(ValueError, match="row hash.*economic content"):
            store.commit_issuer_fundamental_ingest(
                [bad_hash, lineaged[1]],
                **_issuer_commit_arguments(run_id="complete-rowset"),
                rows_rejected=0,
                status="success",
                error=None,
            )

        forged_subset = [dict(lineaged[0]), dict(lineaged[0])]
        with pytest.raises(ValueError, match="exact Company Facts response"):
            store.commit_issuer_fundamental_ingest(
                forged_subset,
                **_issuer_commit_arguments(run_id="complete-rowset"),
                rows_rejected=0,
                status="success",
                error=None,
            )
        assert store.query("SELECT COUNT(*) AS n FROM fundamentals") == [{"n": 0}]
    finally:
        store.close()


def test_issuer_commit_binds_facts_to_reviewed_security_assignment(
    tmp_path,
) -> None:
    store = Store(tmp_path / "reviewed-security-route.duckdb")
    try:
        _install_issuer_reference(store)
        row = _record_lineaged_issuer_row(
            store,
            run_id="wrong-security-route",
            snapshot_id="wrong-security-route-snapshot",
        )
        row["security_id"] = "aios:security:unreviewed-victim"

        with pytest.raises(
            ValueError,
            match="security_id does not match reviewed assignments",
        ):
            store.commit_issuer_fundamental_ingest(
                [row],
                **_issuer_commit_arguments(run_id="wrong-security-route"),
                rows_rejected=0,
                status="success",
                error=None,
            )
        assert store.query("SELECT COUNT(*) AS n FROM fundamentals") == [{"n": 0}]
        assert store.query("SELECT COUNT(*) AS n FROM ingest_log") == [{"n": 0}]
    finally:
        store.close()


@pytest.mark.parametrize(
    "security_row",
    [
        {
            **_issuer_security_rows()[0],
            "ticker": "VICTIM",
        },
        {
            **_issuer_security_rows()[0],
            "cik": 999,
        },
    ],
)
def test_issuer_commit_cannot_mutate_unrelated_security_metadata(
    tmp_path,
    security_row,
) -> None:
    store = Store(tmp_path / f"security-scope-{security_row['ticker']}.duckdb")
    try:
        _install_issuer_reference(store)
        store.upsert_securities(
            [
                {
                    **_issuer_security_rows()[0],
                    "ticker": "VICTIM",
                    "cik": 777,
                    "name": "Original Victim",
                }
            ]
        )
        row = _record_lineaged_issuer_row(
            store,
            run_id="security-scope",
            snapshot_id="security-scope-snapshot",
        )
        arguments = _issuer_commit_arguments(run_id="security-scope")
        arguments["security_rows"] = [security_row]

        with pytest.raises(ValueError, match="security metadata is not derived"):
            store.commit_issuer_fundamental_ingest(
                [row],
                **arguments,
                rows_rejected=0,
                status="success",
                error=None,
            )
        assert store.query(
            "SELECT cik, name FROM securities WHERE ticker = 'VICTIM'"
        ) == [{"cik": 777, "name": "Original Victim"}]
        assert store.query("SELECT COUNT(*) AS n FROM fundamentals") == [{"n": 0}]
        assert store.query("SELECT COUNT(*) AS n FROM ingest_log") == [{"n": 0}]
    finally:
        store.close()


def test_issuer_commit_binds_security_metadata_to_submissions_rowset(
    tmp_path,
) -> None:
    store = Store(tmp_path / "submissions-metadata-binding.duckdb")
    try:
        _install_issuer_reference(store)
        row = _record_lineaged_issuer_row(
            store,
            run_id="submissions-metadata-binding",
            snapshot_id="submissions-metadata-binding-snapshot",
        )
        arguments = _issuer_commit_arguments(
            run_id="submissions-metadata-binding"
        )
        arguments["submissions_row"] = {
            **arguments["submissions_row"],
            "name": "Forged Name",
        }
        arguments["security_rows"] = [
            {
                **arguments["security_rows"][0],
                "name": "Forged Name",
            }
        ]

        with pytest.raises(
            ValueError,
            match="does not match exact Submissions evidence",
        ):
            store.commit_issuer_fundamental_ingest(
                [row],
                **arguments,
                rows_rejected=0,
                status="success",
                error=None,
            )
        assert store.query("SELECT COUNT(*) AS n FROM fundamentals") == [{"n": 0}]
        assert store.query("SELECT COUNT(*) AS n FROM securities") == [{"n": 0}]
        assert store.query("SELECT COUNT(*) AS n FROM ingest_log") == [{"n": 0}]
    finally:
        store.close()


def test_issuer_commit_binds_source_and_rejections_to_parser_evidence(
    tmp_path,
) -> None:
    store = Store(tmp_path / "parser-outcome-binding.duckdb")
    try:
        _install_issuer_reference(store)
        row = _record_lineaged_issuer_row(
            store,
            run_id="parser-outcome-binding",
            snapshot_id="parser-outcome-binding-snapshot",
        )
        arguments = _issuer_commit_arguments(run_id="parser-outcome-binding")

        with pytest.raises(
            ValueError,
            match="reviewed SEC source route",
        ):
            store.commit_issuer_fundamental_ingest(
                [row],
                **{**arguments, "source": "edgar:companyfacts-bulk"},
                rows_rejected=0,
                status="success",
                error=None,
            )
        with pytest.raises(
            ValueError,
            match="exact Company Facts response",
        ):
            store.commit_issuer_fundamental_ingest(
                [row],
                **arguments,
                rows_rejected=1,
                status="warning",
                error="withheld one future-period row",
                rejection_codes=["future_period"],
            )
        assert store.query("SELECT COUNT(*) AS n FROM fundamentals") == [{"n": 0}]
        assert store.query("SELECT COUNT(*) AS n FROM securities") == [{"n": 0}]
        assert store.query("SELECT COUNT(*) AS n FROM ingest_log") == [{"n": 0}]
    finally:
        store.close()


def test_issuer_commit_refuses_an_unconfirmed_relation_shrink(tmp_path) -> None:
    store = Store(tmp_path / "exact-issuer-relation.duckdb")
    try:
        _install_issuer_reference(store)
        first_rows = _record_lineaged_issuer_rows(
            store,
            run_id="full-relation",
            snapshot_id="full-relation-snapshot",
            rows=[
                _issuer_fundamental(100.0),
                {
                    **_issuer_fundamental(20.0),
                    "metric": "net_income",
                },
            ],
        )
        assert store.commit_issuer_fundamental_ingest(
            first_rows,
            **_issuer_commit_arguments(run_id="full-relation"),
            rows_rejected=0,
            status="success",
            error=None,
        ) == (2, 0)

        reduced_rows = _record_lineaged_issuer_rows(
            store,
            run_id="reduced-relation",
            snapshot_id="reduced-relation-snapshot",
            rows=[_issuer_fundamental(101.0)],
        )
        with pytest.raises(
            ValueError,
            match="relation would shrink.*confirmation is required",
        ):
            store.commit_issuer_fundamental_ingest(
                reduced_rows,
                **_issuer_commit_arguments(run_id="reduced-relation"),
                rows_rejected=0,
                status="success",
                error=None,
            )
        assert store.query(
            """
            SELECT metric, value, ingest_run_id
            FROM fundamentals
            WHERE issuer_id = ?
            ORDER BY metric
            """,
            (ISSUER_ID,),
        ) == [
            {
                "metric": "net_income",
                "value": 20.0,
                "ingest_run_id": "full-relation",
            },
            {
                "metric": "revenue",
                "value": 100.0,
                "ingest_run_id": "full-relation",
            }
        ]
        assert store.query("SELECT COUNT(*) AS n FROM ingest_log") == [{"n": 1}]
    finally:
        store.close()


def test_unlineaged_fundamental_cannot_overwrite_explicit_row_provenance(
    tmp_path,
) -> None:
    store = Store(tmp_path / "fundamental-provenance.duckdb")
    row = {
        "ticker": "TEST",
        "issuer_id": "issuer-test",
        "period_end": "2025-12-31",
        "as_of_date": "2026-02-01",
        "metric": "revenue",
        "value": 100.0,
        "quarter_value": 100.0,
        "source": "edgar",
    }
    try:
        with pytest.raises(ValueError, match="provenance requires"):
            store.upsert_fundamentals(
                [{**row, "ingest_run_id": "run-1"}]
            )

        provenanced = {
            **row,
            "ingest_run_id": "run-1",
            "source_snapshot_id": "raw-1",
            "source_rowset_sha256": "a" * 64,
            "source_row_sha256": "b" * 64,
        }
        assert store.upsert_fundamentals([provenanced]) == 1
        assert store.query(
            "SELECT ingest_run_id FROM fundamentals"
        )[0]["ingest_run_id"] == "run-1"

        with pytest.raises(
            ValueError,
            match="unlineaged fundamental cannot overwrite explicitly lineaged evidence",
        ):
            store.upsert_fundamentals([{**row, "value": 999.0}])
        stored = store.query(
            """
            SELECT value, ingest_run_id, source_snapshot_id,
                   source_rowset_sha256, source_row_sha256
            FROM fundamentals
            """
        )[0]
        assert stored == {
            "value": 100.0,
            "ingest_run_id": "run-1",
            "source_snapshot_id": "raw-1",
            "source_rowset_sha256": "a" * 64,
            "source_row_sha256": "b" * 64,
        }

        # The rejected bulk relation must be unregistered so a later reviewed
        # update can reuse the same write path.
        reviewed_update = {
            **provenanced,
            "value": 101.0,
            "ingest_run_id": "run-2",
            "source_snapshot_id": "raw-2",
            "source_rowset_sha256": "c" * 64,
            "source_row_sha256": "d" * 64,
        }
        assert store.upsert_fundamentals([reviewed_update]) == 1
        assert store.query(
            "SELECT value, ingest_run_id FROM fundamentals"
        )[0] == {"value": 101.0, "ingest_run_id": "run-2"}
    finally:
        store.close()


def test_ingest_evidence_joins_snapshot_and_payload_metadata(tmp_path) -> None:
    store = Store(tmp_path / "joined-evidence.duckdb")
    observed_at = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)
    try:
        run_id = store.record_ingest(
            "edgar:issuer-cik-history",
            "fundamentals",
            status="warning",
            error="SEC returned no fundamental rows",
            subject_type="issuer",
            subject_id="aios:issuer:sec:1",
        )
        store.record_raw_snapshot(
            payload={
                "payload_sha256": "a" * 64,
                "relative_path": "data/raw/sec/companyfacts/payload.json.gz",
                "original_bytes": 120,
                "stored_bytes": 80,
                "compression": "gzip",
            },
            snapshot={
                "snapshot_id": "snapshot-1",
                "provider": "sec",
                "dataset": "companyfacts",
                "artifact_kind": "exact_response",
                "requested_at": observed_at,
                "received_at": observed_at,
                "http_status": 200,
                "content_type": "application/json",
                "request_fingerprint": "b" * 64,
                "payload_sha256": "a" * 64,
                "adapter_name": "edgar",
                "adapter_version": "v1",
                "parser_version": "companyfacts-v1",
                "parsed_row_count": 0,
                "parsed_rows_sha256": "c" * 64,
            },
            ingest_run_id=run_id,
            role="companyfacts",
        )

        evidence = store.ingest_evidence(run_id)

        assert evidence is not None
        assert evidence["status"] == "warning"
        assert evidence["subject_type"] == "issuer"
        assert evidence["subject_id"] == "aios:issuer:sec:1"
        assert len(evidence["snapshots"]) == 1
        snapshot = evidence["snapshots"][0]
        assert snapshot["role"] == "companyfacts"
        assert snapshot["provider"] == "sec"
        assert snapshot["dataset"] == "companyfacts"
        assert snapshot["artifact_kind"] == "exact_response"
        assert snapshot["http_status"] == 200
        assert snapshot["adapter_name"] == "edgar"
        assert snapshot["adapter_version"] == "v1"
        assert snapshot["parser_version"] == "companyfacts-v1"
        assert snapshot["payload_sha256"] == "a" * 64
        assert snapshot["relative_path"] == (
            "data/raw/sec/companyfacts/payload.json.gz"
        )
        assert snapshot["original_bytes"] == 120
        assert snapshot["stored_bytes"] == 80
        assert snapshot["compression"] == "gzip"
    finally:
        store.close()


def test_writable_store_migrates_both_legacy_subject_columns_together(tmp_path) -> None:
    path = tmp_path / "legacy-writable.duckdb"
    _create_legacy_ingest_db(path)

    store = Store(path, allow_schema_upgrade=True)
    try:
        columns = {row["column_name"] for row in store.query("DESCRIBE ingest_log")}
        snapshot_columns = {
            row["column_name"] for row in store.query("DESCRIBE raw_snapshots")
        }
        assert {"subject_type", "subject_id"} <= columns
        assert "rejection_codes" in columns
        assert {
            "parsed_rows_rejected",
            "parsed_rejection_codes",
        } <= snapshot_columns
        assert store.ingest_evidence("legacy-run") is not None
        legacy = store.ingest_evidence("legacy-run")
        assert legacy is not None
        assert legacy["subject_type"] is None
        assert legacy["subject_id"] is None
        assert legacy["rejection_codes"] is None
        assert legacy["snapshots"][0]["parsed_row_count"] == 0
    finally:
        store.close()


def test_read_only_legacy_evidence_does_not_mutate_missing_subject_columns(
    tmp_path,
) -> None:
    path = tmp_path / "legacy-read-only.duckdb"
    _create_legacy_ingest_db(path)

    store = Store(path, read_only=True)
    try:
        evidence = store.ingest_evidence("legacy-run")
        columns = {row["column_name"] for row in store.query("DESCRIBE ingest_log")}

        assert evidence is not None
        assert evidence["subject_type"] is None
        assert evidence["subject_id"] is None
        assert evidence["snapshots"][0]["original_bytes"] == 120
        assert "subject_type" not in columns
        assert "subject_id" not in columns
        assert "rejection_codes" not in columns
        assert evidence["rejection_codes"] is None
        assert store.ingest_evidence("missing-run") is None
    finally:
        store.close()


def test_read_only_partial_subject_schema_fails_closed(tmp_path) -> None:
    path = tmp_path / "partial-read-only.duckdb"
    _create_legacy_ingest_db(path, partial_subject=True)

    store = Store(path, read_only=True)
    try:
        with pytest.raises(RuntimeError, match="must exist together"):
            store.ingest_evidence("legacy-run")
    finally:
        store.close()


def test_failed_issuer_ingest_retains_the_requested_subject(tmp_path) -> None:
    store = Store(tmp_path / "failed-issuer.duckdb")
    try:
        with pytest.raises(ValueError, match="No reviewed SEC CIK"):
            edgar.ingest_issuer("aios:issuer:sec:missing", store=store)

        latest = store.ingest_history(1)[0]
        evidence = store.ingest_evidence(latest["run_id"])
        assert evidence is not None
        assert evidence["status"] == "failed"
        assert evidence["subject_type"] == "issuer"
        assert evidence["subject_id"] == "aios:issuer:sec:missing"
    finally:
        store.close()


def test_legacy_ticker_ingest_refuses_unreviewed_ticker_map(
    monkeypatch,
    tmp_path,
) -> None:
    store = Store(tmp_path / "ticker-subject.duckdb")
    monkeypatch.setattr(store_module, "get_store", lambda: store)
    try:
        with pytest.raises(RuntimeError, match="ticker/CIK-map.*disabled"):
            edgar.ingest_ticker("test", {"TEST": 1})
        with pytest.raises(RuntimeError, match="exactly one reviewed active issuer"):
            edgar.ingest_ticker("test")
        assert store.query("SELECT COUNT(*) AS n FROM ingest_log") == [{"n": 0}]
    finally:
        store.close()


def test_legacy_ticker_ingest_delegates_to_one_reviewed_active_issuer(
    monkeypatch,
    tmp_path,
) -> None:
    store = Store(tmp_path / "ticker-delegation.duckdb")
    monkeypatch.setattr(store_module, "get_store", lambda: store)
    delegated: list[tuple[str, Store]] = []

    def fake_ingest_issuer(issuer_id, *, store):
        delegated.append((issuer_id, store))
        return 7

    monkeypatch.setattr(edgar, "ingest_issuer", fake_ingest_issuer)
    try:
        _install_issuer_reference(store)

        assert edgar.ingest_ticker(" new ") == 7
        assert delegated == [(ISSUER_ID, store)]
    finally:
        store.close()


@pytest.mark.parametrize("failure_point", ["security_metadata", "accepted_outcome"])
def test_issuer_ingest_rolls_back_every_accepted_write_before_failed_outcome(
    monkeypatch,
    tmp_path,
    failure_point,
) -> None:
    store = Store(tmp_path / f"issuer-atomic-{failure_point}.duckdb")
    try:
        _install_issuer_reference(store)
        store.upsert_fundamentals(
            [
                {
                    **_issuer_fundamental(50.0, ticker="STALE"),
                    "issuer_id": None,
                }
            ]
        )
        store.upsert_securities(
            [
                {
                    "ticker": "NEW",
                    "cik": 1,
                    "name": "Old Metadata",
                    "exchange": None,
                    "sector": None,
                    "industry": None,
                    "market_cap_bucket": None,
                    "sic_code": None,
                }
            ]
        )
        def fake_extract(*_args, **kwargs):
            observed_at = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
            row = _issuer_fundamental(100.0)
            provider_row = {
                "cik": "0000000001",
                "period_end": row["period_end"],
                "as_of_date": row["as_of_date"],
                "fiscal_period": row["fiscal_period"],
                "statement": row["statement"],
                "metric": row["metric"],
                "value": row["value"],
                "quarter_value": row["quarter_value"],
                "unit": row["unit"],
                "source": row["source"],
            }
            rowset_sha256 = canonical_parsed_rows_sha256([provider_row])
            submissions_row = _issuer_submissions_row()
            submissions_rowset_sha256 = canonical_parsed_rows_sha256(
                [submissions_row]
            )
            kwargs["snapshot_store"].record_raw_snapshot(
                payload={
                    "payload_sha256": "e" * 64,
                    "relative_path": (
                        f"data/raw/sec/companyfacts/{failure_point}.json.gz"
                    ),
                    "original_bytes": 120,
                    "stored_bytes": 80,
                    "compression": "gzip",
                },
                snapshot={
                    "snapshot_id": f"snapshot-{failure_point}",
                    "provider": "sec-edgar",
                    "dataset": "companyfacts",
                    "artifact_kind": "exact_response",
                    "requested_at": observed_at,
                    "received_at": observed_at,
                    "http_status": 200,
                    "content_type": "application/json",
                    "request_fingerprint": "f" * 64,
                    "payload_sha256": "e" * 64,
                    "adapter_name": "edgar",
                    "adapter_version": "v1",
                    "parser_version": "sec-companyfacts-v2",
                    "parsed_row_count": 1,
                    "parsed_rows_sha256": rowset_sha256,
                    "parsed_rows_rejected": 0,
                    "parsed_rejection_codes": None,
                },
                ingest_run_id=kwargs["ingest_run_id"],
                role="companyfacts",
            )
            kwargs["snapshot_store"].record_raw_snapshot(
                payload={
                    "payload_sha256": "d" * 64,
                    "relative_path": (
                        f"data/raw/sec/submissions/{failure_point}.json.gz"
                    ),
                    "original_bytes": 60,
                    "stored_bytes": 40,
                    "compression": "gzip",
                },
                snapshot={
                    "snapshot_id": f"submissions-{failure_point}",
                    "provider": "sec-edgar",
                    "dataset": "submissions",
                    "artifact_kind": "exact_response",
                    "requested_at": observed_at,
                    "received_at": observed_at,
                    "http_status": 200,
                    "content_type": "application/json",
                    "request_fingerprint": "c" * 64,
                    "payload_sha256": "d" * 64,
                    "adapter_name": "edgar",
                    "adapter_version": "v1",
                    "parser_version": "sec-submissions-v2",
                    "parsed_row_count": 1,
                    "parsed_rows_sha256": submissions_rowset_sha256,
                    "parsed_rows_rejected": 0,
                    "parsed_rejection_codes": None,
                },
                ingest_run_id=kwargs["ingest_run_id"],
                role="submissions",
            )
            row.update(
                {
                    "ingest_run_id": kwargs["ingest_run_id"],
                    "source_snapshot_id": f"snapshot-{failure_point}",
                    "source_rowset_sha256": rowset_sha256,
                    "source_row_sha256": edgar.canonical_sec_fundamental_row_sha256(
                        provider_row
                    ),
                }
            )
            return [row], {
                "name": "Test Corporation",
                "sic_code": "0000",
                "sic_description": "Test",
                "exchange": "NYSE",
                "submissions_row": submissions_row,
            }

        monkeypatch.setattr(edgar, "extract_fundamentals", fake_extract)
        if failure_point == "security_metadata":
            def fail_security_metadata(_rows):
                raise RuntimeError("security metadata write failed")

            monkeypatch.setattr(
                store,
                "upsert_securities",
                fail_security_metadata,
            )
            expected_error = "security metadata write failed"
        else:
            original_record = store.record_ingest

            def fail_accepted_outcome(*args, **kwargs):
                if kwargs.get("status") != "failed":
                    original_record(*args, **kwargs)
                    raise RuntimeError("accepted outcome write failed")
                return original_record(*args, **kwargs)

            monkeypatch.setattr(store, "record_ingest", fail_accepted_outcome)
            expected_error = "accepted outcome write failed"

        with pytest.raises(RuntimeError, match=expected_error):
            edgar.ingest_issuer(ISSUER_ID, store=store)

        assert store.query(
            """
            SELECT ticker, value
            FROM fundamentals
            ORDER BY ticker
            """
        ) == [{"ticker": "STALE", "value": 50.0}]
        assert store.query(
            "SELECT name FROM securities WHERE ticker = 'NEW'"
        ) == [{"name": "Old Metadata"}]
        outcomes = store.ingest_history(10)
        assert len(outcomes) == 1
        assert outcomes[0]["status"] == "failed"
        assert expected_error in outcomes[0]["error"]
        evidence = store.ingest_evidence(outcomes[0]["run_id"])
        assert evidence is not None
        assert [snapshot["snapshot_id"] for snapshot in evidence["snapshots"]] == [
            f"snapshot-{failure_point}",
            f"submissions-{failure_point}",
        ]
    finally:
        store.close()


def test_bulk_issuer_payload_cannot_replace_a_lineaged_fundamental(
    monkeypatch,
    tmp_path,
) -> None:
    store = Store(tmp_path / "bulk-lineage-overwrite.duckdb")
    payload = {"cik": 1, "facts": {}}
    try:
        _install_issuer_reference(store)
        store.upsert_fundamentals([_issuer_fundamental(100.0, provenance=True)])

        def fake_extract(*_args, **kwargs):
            assert kwargs["facts_payload"] is payload
            return [_issuer_fundamental(999.0)], {"name": "Changed Metadata"}

        monkeypatch.setattr(edgar, "extract_fundamentals", fake_extract)

        with pytest.raises(
            RuntimeError,
            match="Unlineaged Company Facts payload ingestion is unavailable",
        ):
            edgar.ingest_issuer(
                ISSUER_ID,
                store=store,
                facts_payload=payload,
            )

        stored = store.query(
            """
            SELECT value, ingest_run_id, source_snapshot_id,
                   source_rowset_sha256, source_row_sha256
            FROM fundamentals
            """
        )[0]
        assert stored == {
            "value": 100.0,
            "ingest_run_id": "accepted-run",
            "source_snapshot_id": "accepted-snapshot",
            "source_rowset_sha256": "a" * 64,
            "source_row_sha256": "b" * 64,
        }
        outcomes = store.ingest_history(10)
        assert len(outcomes) == 1
        assert outcomes[0]["status"] == "failed"
    finally:
        store.close()


def test_lineaged_fundamental_preserves_canonical_source_fact_locator(
    tmp_path,
) -> None:
    store = Store(tmp_path / "source-locator.duckdb")
    locator = json.dumps(
        [
            {
                "accession": "0000000001-26-000001",
                "concept": "Revenues",
                "end": "2025-12-31",
                "filed": "2026-02-01",
                "fiscal_period": "FY",
                "fiscal_year": 2025,
                "form": "10-K",
                "frame": None,
                "start": "2025-01-01",
                "taxonomy": "us-gaap",
            }
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        _install_issuer_reference(store)
        row = _issuer_fundamental(100.0, provenance=True)
        row["source_fact_locator"] = locator

        assert store.upsert_fundamentals([row]) == 1
        assert store.query(
            "SELECT source_fact_locator FROM fundamentals"
        ) == [{"source_fact_locator": locator}]
    finally:
        store.close()


def test_source_fact_locator_requires_complete_lineage_and_canonical_json(
    tmp_path,
) -> None:
    store = Store(tmp_path / "source-locator-refusal.duckdb")
    try:
        _install_issuer_reference(store)
        unlineaged = _issuer_fundamental(100.0)
        unlineaged["source_fact_locator"] = "[]"
        with pytest.raises(ValueError, match="requires complete immutable provenance"):
            store.upsert_fundamentals([unlineaged])

        noncanonical = _issuer_fundamental(100.0, provenance=True)
        noncanonical["source_fact_locator"] = '[{"taxonomy": "us-gaap"}]'
        with pytest.raises(ValueError, match="must be canonical JSON"):
            store.upsert_fundamentals([noncanonical])

        incomplete = _issuer_fundamental(100.0, provenance=True)
        incomplete["source_fact_locator"] = '{"taxonomy":"us-gaap"}'
        with pytest.raises(
            ValueError,
            match="must contain source locator objects",
        ):
            store.upsert_fundamentals([incomplete])

        assert store.query("SELECT COUNT(*) AS n FROM fundamentals") == [{"n": 0}]
    finally:
        store.close()

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from aios.ingest import edgar
from aios.raw_snapshots import (
    attach_parsed_rows_evidence,
    canonical_request_fingerprint,
    capture_raw_snapshot,
    verify_raw_snapshots,
)
from aios.storage.store import Store

ISSUER_ID = "aios:issuer:sec:0000000001"
SECURITY_ID = "aios:security:test-common"


def _companyfacts_payload(
    *,
    dei_rows: list[dict] | None = None,
    dei_units: dict[str, list[dict]] | None = None,
    us_gaap: dict | None = None,
) -> bytes:
    units = dei_units
    if units is None:
        units = {"shares": dei_rows or []}
    return json.dumps(
        {
            "cik": 1,
            "entityName": "Test Corporation",
            "facts": {
                "dei": {
                    edgar.DEI_ENTITY_SHARES_CONCEPT: {
                        "units": units,
                    }
                },
                "us-gaap": us_gaap or {},
            },
        },
        sort_keys=True,
    ).encode()


def _instant_share(
    *,
    accession: str = "0000000001-26-000001",
    end: str = "2025-12-31",
    filed: str = "2026-01-31",
    value: float = 100.0,
    **overrides,
) -> dict:
    row = {
        "accn": accession,
        "end": end,
        "filed": filed,
        "fp": "FY",
        "fy": 2025,
        "form": "10-K",
        "val": value,
        "frame": "CY2025Q4I",
    }
    row.update(overrides)
    return row


def _install_issuer_reference(store: Store) -> None:
    store.execute(
        """
        INSERT INTO security_master
        (security_id, canonical_ticker, identity_status, source)
        VALUES (?, 'TEST', 'verified_ticker_change', 'test')
        """,
        (SECURITY_ID,),
    )
    store.upsert_reference_identities(
        [
            {
                "issuer_id": ISSUER_ID,
                "canonical_name": "Test Corporation",
                "canonical_ticker": "TEST",
                "source": "test",
            }
        ],
        [
            {
                "issuer_id": ISSUER_ID,
                "cik": "0000000001",
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
                "provider_symbol": "TEST",
                "security_id": SECURITY_ID,
                "data_start": "2024-01-01",
                "data_end": None,
                "mapping_status": "verified",
                "verified_date": "2024-01-01",
                "source": "test",
            }
        ],
    )


def _capture_companyfacts(
    tmp_path,
    store: Store,
    payload: bytes,
    *,
    parser_version: str,
    parsed_rows: list[dict] | None,
    ingest_run_id: str | None = None,
):
    requested = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
    rejection_evidence = (
        edgar.replay_sec_companyfacts_response(
            payload,
            parser_version=parser_version,
        )[1]
        if parsed_rows is not None
        else None
    )
    return capture_raw_snapshot(
        payload,
        provider="sec-edgar",
        dataset="companyfacts",
        artifact_kind="exact_response",
        requested_at=requested,
        received_at=requested + timedelta(seconds=1),
        request_fingerprint=canonical_request_fingerprint(
            {
                "method": "GET",
                "url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
            }
        ),
        adapter_name="aios-sec-http",
        adapter_version="1",
        parser_version=parser_version,
        http_status=200,
        content_type="application/json",
        parsed_rows=parsed_rows,
        parsed_rows_rejected=(rejection_evidence["rows_rejected"] if rejection_evidence else None),
        parsed_rejection_codes=(
            rejection_evidence["rejection_codes"] if rejection_evidence else None
        ),
        ingest_run_id=ingest_run_id,
        role="companyfacts",
        store=store,
        project_root=tmp_path,
    )


def test_companyfacts_v3_reads_dei_entity_shares_with_instant_pit_semantics() -> None:
    payload = _companyfacts_payload(
        dei_rows=[
            _instant_share(),
            _instant_share(
                accession="0000000001-26-000002",
                filed="2026-02-15",
                value=110.0,
                form="10-K/A",
            ),
        ]
    )

    rows = edgar.parse_sec_companyfacts_response_v3(payload)

    assert edgar.COMPANYFACTS_NEXT_PARSER_VERSION == "sec-companyfacts-v3"
    assert [
        {key: value for key, value in row.items() if key != "source_fact_locator"} for row in rows
    ] == [
        {
            "cik": "0000000001",
            "period_end": "2025-12-31",
            "as_of_date": "2026-01-31",
            "fiscal_period": "FY2025",
            "statement": "balance",
            "metric": "shares_out",
            "value": 100.0,
            "quarter_value": 100.0,
            "unit": "shares",
            "source": "edgar",
        },
        {
            "cik": "0000000001",
            "period_end": "2025-12-31",
            "as_of_date": "2026-02-15",
            "fiscal_period": "FY2025",
            "statement": "balance",
            "metric": "shares_out",
            "value": 110.0,
            "quarter_value": 110.0,
            "unit": "shares",
            "source": "edgar",
        },
    ]


def test_companyfacts_v3_does_not_substitute_unsafe_share_concepts() -> None:
    unsafe_observation = _instant_share()
    payload = _companyfacts_payload(
        dei_rows=[],
        us_gaap={
            "WeightedAverageNumberOfDilutedSharesOutstanding": {
                "units": {"shares": [unsafe_observation]}
            },
            "WeightedAverageNumberOfSharesOutstandingBasic": {
                "units": {"shares": [unsafe_observation]}
            },
            "CommonStockSharesIssued": {"units": {"shares": [unsafe_observation]}},
            "SharesIssued": {"units": {"shares": [unsafe_observation]}},
        },
    )

    assert edgar.parse_sec_companyfacts_response_v3(payload) == []


def test_companyfacts_v3_ignores_noninstant_unbound_or_wrong_unit_dei_rows() -> None:
    payload = _companyfacts_payload(
        dei_units={
            "shares": [
                _instant_share(),
                _instant_share(
                    accession="0000000001-26-000002",
                    start="2025-01-01",
                ),
                _instant_share(accession=""),
                _instant_share(
                    accession="0000000001-26-000003",
                    end="2026-03-31",
                    filed="2026-02-15",
                ),
            ],
            "USD": [_instant_share(accession="0000000001-26-000004", value=999.0)],
        }
    )

    rows = edgar.parse_sec_companyfacts_response_v3(payload)

    assert [(row["metric"], row["value"]) for row in rows] == [("shares_out", 100.0)]


def test_companyfacts_v3_rejects_conflicting_entity_shares_in_one_accession() -> None:
    payload = _companyfacts_payload(
        dei_rows=[
            _instant_share(),
            _instant_share(value=101.0),
        ]
    )

    with pytest.raises(ValueError, match="conflict within one accession"):
        edgar.parse_sec_companyfacts_response_v3(payload)


def test_companyfacts_v3_live_issuer_ingest_is_blocked_before_fetch(
    monkeypatch,
    tmp_path,
) -> None:
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        _install_issuer_reference(store)
        monkeypatch.setattr(
            edgar,
            "fetch_facts",
            lambda _cik, **_kwargs: pytest.fail("v3 refusal must precede fetch"),
        )

        with pytest.raises(
            RuntimeError,
            match="v3 live mutation is unavailable",
        ):
            edgar.ingest_issuer(
                ISSUER_ID,
                store=store,
                companyfacts_parser_version=edgar.COMPANYFACTS_NEXT_PARSER_VERSION,
            )
        assert store.query("SELECT COUNT(*) AS n FROM fundamentals") == [{"n": 0}]
        outcome = store.ingest_history(1)[0]
        assert outcome["status"] == "failed"
        assert "v3 live mutation is unavailable" in outcome["error"]
    finally:
        store.close()


def test_companyfacts_v2_replay_remains_byte_contract_compatible(
    tmp_path,
) -> None:
    payload = _companyfacts_payload(
        dei_rows=[_instant_share()],
        us_gaap={
            "Revenues": {
                "units": {
                    "USD": [
                        {
                            "accn": "0000000001-26-000001",
                            "start": "2025-01-01",
                            "end": "2025-12-31",
                            "filed": "2026-01-31",
                            "fp": "FY",
                            "fy": 2025,
                            "form": "10-K",
                            "val": 500.0,
                        }
                    ]
                }
            }
        },
    )
    v2_rows = edgar.parse_sec_companyfacts_response_v2(payload)
    assert [row["metric"] for row in v2_rows] == ["revenue"]
    assert [row["metric"] for row in edgar.parse_sec_companyfacts_response_v3(payload)] == [
        "revenue",
        "shares_out",
    ]

    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        _capture_companyfacts(
            tmp_path,
            store,
            payload,
            parser_version="sec-companyfacts-v2",
            parsed_rows=v2_rows,
        )

        verification = verify_raw_snapshots(store=store, project_root=tmp_path)

        assert verification.replayed_snapshots == 1
    finally:
        store.close()


def test_companyfacts_capture_promotes_to_v3_and_replays(
    tmp_path,
) -> None:
    payload = _companyfacts_payload(dei_rows=[_instant_share()])
    parsed_rows = edgar.parse_sec_companyfacts_response_v3(payload)
    store = Store(tmp_path / "data" / "test.duckdb")
    try:
        captured = _capture_companyfacts(
            tmp_path,
            store,
            payload,
            parser_version=edgar.COMPANYFACTS_CAPTURE_PARSER_VERSION,
            parsed_rows=None,
            ingest_run_id="issuer-run",
        )

        promoted = attach_parsed_rows_evidence(
            store=store,
            ingest_run_id="issuer-run",
            role="companyfacts",
            capture_parser_version=edgar.COMPANYFACTS_CAPTURE_PARSER_VERSION,
            parser_version=edgar.COMPANYFACTS_NEXT_PARSER_VERSION,
            parsed_rows=parsed_rows,
        )
        evidence = store.query(
            """
            SELECT parser_version, parsed_row_count, parsed_rows_sha256
            FROM raw_snapshots
            WHERE snapshot_id = ?
            """,
            (captured.snapshot_id,),
        )[0]

        assert promoted == captured.snapshot_id
        assert evidence["parser_version"] == "sec-companyfacts-v3"
        assert evidence["parsed_row_count"] == 1
        assert evidence["parsed_rows_sha256"]
        assert verify_raw_snapshots(store=store, project_root=tmp_path).replayed_snapshots == 1
    finally:
        store.close()

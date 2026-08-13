from __future__ import annotations

import json
from typing import Any

import pytest

from aios.ingest import edgar
from aios.raw_snapshots import _replay_snapshot


def _fact(
    *,
    value: int,
    end: str = "2026-04-17",
    filed: str = "2026-05-01",
    accession: str = "0000000001-26-000001",
    form: str = "10-Q",
    frame: str | None = "CY2026Q2I",
) -> dict[str, Any]:
    return {
        "end": end,
        "filed": filed,
        "fp": "Q2",
        "fy": 2026,
        "accn": accession,
        "form": form,
        "frame": frame,
        "val": value,
    }


def _payload(
    *,
    us_gaap_rows: list[dict[str, Any]] | None = None,
    dei_rows: list[dict[str, Any]] | None = None,
) -> bytes:
    facts: dict[str, Any] = {}
    if us_gaap_rows is not None:
        facts["us-gaap"] = {
            "CommonStockSharesOutstanding": {
                "units": {"shares": us_gaap_rows},
            }
        }
    if dei_rows is not None:
        facts["dei"] = {
            "EntityCommonStockSharesOutstanding": {
                "units": {"shares": dei_rows},
            }
        }
    return json.dumps({"cik": 1, "entityName": "Test Corp", "facts": facts}).encode()


def _metric_payload(
    concept: str,
    rows_by_unit: dict[str, list[dict[str, Any]]],
) -> bytes:
    return json.dumps(
        {
            "cik": 1,
            "entityName": "Test Corp",
            "facts": {
                "us-gaap": {
                    concept: {
                        "units": rows_by_unit,
                    }
                }
            },
        }
    ).encode()


def _without_locator(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "source_fact_locator"}


def test_companyfacts_v3_reads_dei_entity_shares_with_exact_pit_date() -> None:
    rows = edgar.parse_sec_companyfacts_response_v3(_payload(dei_rows=[_fact(value=123_456_789)]))

    assert edgar.COMPANYFACTS_NEXT_PARSER_VERSION == "sec-companyfacts-v3"
    assert [_without_locator(row) for row in rows] == [
        {
            "cik": "0000000001",
            "period_end": "2026-04-17",
            "as_of_date": "2026-05-01",
            "fiscal_period": "Q2_2026",
            "statement": "balance",
            "metric": "shares_out",
            "value": 123_456_789.0,
            "quarter_value": 123_456_789.0,
            "unit": "shares",
            "source": "edgar",
        }
    ]
    assert json.loads(rows[0]["source_fact_locator"]) == [
        {
            "accession": "0000000001-26-000001",
            "concept": "EntityCommonStockSharesOutstanding",
            "end": "2026-04-17",
            "filed": "2026-05-01",
            "fiscal_period": "Q2",
            "fiscal_year": 2026,
            "form": "10-Q",
            "frame": "CY2026Q2I",
            "start": None,
            "taxonomy": "dei",
        }
    ]


def test_companyfacts_v3_remains_dormant_until_next_policy_activation() -> None:
    payload = _payload(dei_rows=[_fact(value=123_456_789)])

    assert edgar.COMPANYFACTS_LEGACY_PARSER_VERSION == "sec-companyfacts-v2"
    assert edgar.COMPANYFACTS_STORAGE_SAFE_V1_PARSER_VERSION == (
        "sec-companyfacts-v2-storage-safe-v1"
    )
    assert edgar.COMPANYFACTS_PARSER_VERSION == (
        "sec-companyfacts-v2-storage-safe-v2"
    )
    assert edgar.COMPANYFACTS_NEXT_PARSER_VERSION == "sec-companyfacts-v3"
    assert edgar.parse_sec_companyfacts_response(payload) == []
    assert len(edgar.parse_sec_companyfacts_response_v3(payload)) == 1


def test_companyfacts_v3_concept_names_are_namespace_qualified() -> None:
    wrong_namespace_payload = json.dumps(
        {
            "cik": 1,
            "facts": {
                "us-gaap": {
                    "EntityCommonStockSharesOutstanding": {"units": {"shares": [_fact(value=10)]}}
                },
                "dei": {"CommonStockSharesOutstanding": {"units": {"shares": [_fact(value=20)]}}},
            },
        }
    ).encode()

    assert edgar.parse_sec_companyfacts_response_v3(wrong_namespace_payload) == []


def test_companyfacts_v3_withholds_cross_taxonomy_conflict_for_same_context() -> None:
    shared_context = {
        "end": "2026-03-31",
        "filed": "2026-05-01",
        "accession": "0000000001-26-000002",
        "frame": "CY2026Q1I",
    }
    rows = edgar.parse_sec_companyfacts_response_v3(
        _payload(
            us_gaap_rows=[_fact(value=100, **shared_context)],
            dei_rows=[_fact(value=110, **shared_context)],
        )
    )

    assert rows == []


def test_companyfacts_v3_collapses_equal_cross_taxonomy_evidence_with_all_locators() -> None:
    shared_context = {
        "end": "2026-03-31",
        "filed": "2026-05-01",
        "accession": "0000000001-26-000002",
        "frame": "CY2026Q1I",
    }
    rows = edgar.parse_sec_companyfacts_response_v3(
        _payload(
            us_gaap_rows=[_fact(value=100, **shared_context)],
            dei_rows=[_fact(value=100, **shared_context)],
        )
    )

    assert len(rows) == 1
    locators = json.loads(rows[0]["source_fact_locator"])
    assert {(item["taxonomy"], item["concept"]) for item in locators} == {
        ("us-gaap", "CommonStockSharesOutstanding"),
        ("dei", "EntityCommonStockSharesOutstanding"),
    }


def test_companyfacts_v3_adds_non_overlapping_dei_contexts() -> None:
    rows = edgar.parse_sec_companyfacts_response_v3(
        _payload(
            us_gaap_rows=[
                _fact(
                    value=100,
                    end="2026-03-31",
                    filed="2026-05-01",
                    accession="0000000001-26-000002",
                    frame="CY2026Q1I",
                )
            ],
            dei_rows=[
                _fact(
                    value=105,
                    end="2026-04-17",
                    filed="2026-05-01",
                    accession="0000000001-26-000002",
                    frame="CY2026Q2I",
                )
            ],
        )
    )

    assert [(row["period_end"], row["value"]) for row in rows] == [
        ("2026-03-31", 100.0),
        ("2026-04-17", 105.0),
    ]


def test_companyfacts_v3_rejects_dei_period_end_after_filing_date(
    monkeypatch,
) -> None:
    payload = json.loads(
        _payload(
            dei_rows=[
                _fact(value=100),
                _fact(
                    value=200,
                    end="2026-06-30",
                    filed="2026-05-01",
                    accession="0000000001-26-000003",
                    frame="CY2026Q2I",
                ),
            ]
        )
    )
    monkeypatch.setattr(
        edgar,
        "fetch_submissions",
        lambda *_args, **_kwargs: {
            "cik": 1,
            "name": "Test Corp",
            "exchanges": ["NYSE"],
        },
    )

    rows, metadata = edgar.extract_fundamentals(
        "TEST",
        1,
        facts_payload=payload,
        companyfacts_parser_version=edgar.COMPANYFACTS_NEXT_PARSER_VERSION,
    )

    assert [row["value"] for row in rows] == [100.0]
    assert metadata["rows_rejected_future_period"] == 1


def test_issuer_extraction_fails_closed_when_submissions_evidence_is_missing(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        edgar,
        "fetch_submissions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("Submissions unavailable")
        ),
    )

    with pytest.raises(RuntimeError, match="Submissions unavailable"):
        edgar.extract_fundamentals(
            "TEST",
            1,
            facts_payload={"cik": 1, "facts": {}},
        )


def test_companyfacts_v3_never_uses_form_or_frame_as_the_pit_key() -> None:
    rows = edgar.parse_sec_companyfacts_response_v3(
        _payload(
            dei_rows=[
                _fact(
                    value=100,
                    end="2026-04-17",
                    filed="2026-05-03",
                    form="8-K",
                    frame="CY2026Q2I",
                )
            ]
        )
    )

    assert rows[0]["period_end"] == "2026-04-17"
    assert rows[0]["as_of_date"] == "2026-05-03"


def test_companyfacts_v2_replay_remains_byte_contract_compatible() -> None:
    us_gaap_payload = _payload(us_gaap_rows=[_fact(value=100)])
    dei_payload = _payload(dei_rows=[_fact(value=110)])

    v2_us_gaap = edgar.parse_sec_companyfacts_response_v2(us_gaap_payload)
    v2_dei = edgar.parse_sec_companyfacts_response_v2(dei_payload)

    assert v2_us_gaap[0]["unit"] == "USD"
    assert v2_dei == []


def test_raw_snapshot_replay_dispatches_companyfacts_by_parser_version() -> None:
    payload = _payload(dei_rows=[_fact(value=110)])
    base_snapshot = {
        "provider": "sec-edgar",
        "dataset": "companyfacts",
    }

    assert (
        _replay_snapshot(
            {**base_snapshot, "parser_version": "sec-companyfacts-v2"},
            payload,
        )
        == []
    )
    assert (
        len(
            _replay_snapshot(
                {**base_snapshot, "parser_version": "sec-companyfacts-v3"},
                payload,
            )
            or []
        )
        == 1
    )


def test_companyfacts_v3_selects_single_quarter_over_ytd_for_storage() -> None:
    shared = {
        "end": "2026-06-30",
        "filed": "2026-07-25",
        "fp": "Q2",
        "fy": 2026,
        "accn": "0000000001-26-000010",
        "form": "10-Q",
    }
    payload = _metric_payload(
        "Revenues",
        {
            "USD": [
                {**shared, "start": "2026-01-01", "val": 300.0},
                {**shared, "start": "2026-04-01", "val": 120.0},
            ]
        },
    )

    v2_rows = edgar.parse_sec_companyfacts_response_v2(payload)
    v3_rows = edgar.parse_sec_companyfacts_response_v3(payload)

    assert len(v2_rows) == 2
    assert len(v3_rows) == 1
    assert v3_rows[0]["metric"] == "revenue"
    assert v3_rows[0]["value"] == 120.0
    assert v3_rows[0]["quarter_value"] == 120.0


def test_companyfacts_v3_selects_shortest_period_for_eps() -> None:
    shared = {
        "end": "2026-06-30",
        "filed": "2026-07-25",
        "fp": "Q2",
        "fy": 2026,
        "accn": "0000000001-26-000010",
        "form": "10-Q",
    }
    payload = _metric_payload(
        "EarningsPerShareBasic",
        {
            "USD/shares": [
                {**shared, "start": "2026-01-01", "val": 3.0},
                {**shared, "start": "2026-04-01", "val": 1.2},
            ]
        },
    )

    rows = edgar.parse_sec_companyfacts_response_v3(payload)

    assert len(rows) == 1
    assert rows[0]["metric"] == "eps_basic"
    assert rows[0]["value"] == 1.2


def test_companyfacts_v3_requires_exact_metric_unit_and_instant_context() -> None:
    payload = _metric_payload(
        "Assets",
        {
            "USD": [
                {
                    **_fact(value=100),
                    "start": "2026-01-01",
                },
                _fact(value=200),
            ],
            "EUR": [_fact(value=999, accession="0000000001-26-000002")],
        },
    )

    rows = edgar.parse_sec_companyfacts_response_v3(payload)

    assert len(rows) == 1
    assert rows[0]["metric"] == "total_assets"
    assert rows[0]["value"] == 200.0
    assert rows[0]["unit"] == "USD"


def test_companyfacts_v3_wrong_unit_cannot_hide_same_context_usd_row() -> None:
    shared = _fact(value=999)
    payload = _metric_payload(
        "Assets",
        {
            "EUR": [shared],
            "USD": [{**shared, "val": 200}],
        },
    )

    rows = edgar.parse_sec_companyfacts_response_v3(payload)

    assert len(rows) == 1
    assert rows[0]["metric"] == "total_assets"
    assert rows[0]["value"] == 200.0
    assert rows[0]["unit"] == "USD"


def test_companyfacts_v3_collapses_identical_same_day_storage_evidence() -> None:
    shared = {
        "end": "2026-06-30",
        "filed": "2026-07-25",
        "fp": "Q2",
        "fy": 2026,
        "start": "2026-04-01",
        "val": 120.0,
    }
    payload = _metric_payload(
        "Revenues",
        {
            "USD": [
                {
                    **shared,
                    "accn": "0000000001-26-000010",
                    "form": "10-Q",
                },
                {
                    **shared,
                    "accn": "0000000001-26-000011",
                    "form": "8-K",
                },
            ]
        },
    )

    (
        rows,
        future_periods,
        context_rejections,
        storage_conflicts,
    ) = edgar._companyfacts_provider_rows(
        json.loads(payload),
        1,
        taxonomy_aware=True,
    )

    assert len(rows) == 1
    assert future_periods == 0
    assert context_rejections == 0
    assert storage_conflicts == 0
    safe_rows, safe_metadata = edgar.replay_sec_companyfacts_response(
        payload,
        parser_version=edgar.COMPANYFACTS_PARSER_VERSION,
    )
    v1_rows, v1_metadata = edgar.replay_sec_companyfacts_response(
        payload,
        parser_version=edgar.COMPANYFACTS_STORAGE_SAFE_V1_PARSER_VERSION,
    )
    assert len(safe_rows) == 1
    assert safe_metadata["rows_rejected_storage_conflict"] == 0
    assert len(v1_rows) == 1
    assert v1_metadata["rows_rejected_storage_conflict"] == 0


def test_companyfacts_v3_withholds_conflicting_same_day_storage_evidence() -> None:
    shared = {
        "end": "2026-06-30",
        "filed": "2026-07-25",
        "fp": "Q2",
        "fy": 2026,
        "start": "2026-04-01",
    }
    payload = _metric_payload(
        "Revenues",
        {
            "USD": [
                {
                    **shared,
                    "accn": "0000000001-26-000010",
                    "form": "10-Q",
                    "val": 120.0,
                },
                {
                    **shared,
                    "accn": "0000000001-26-000011",
                    "form": "8-K",
                    "val": 121.0,
                },
            ]
        },
    )

    (
        rows,
        future_periods,
        context_rejections,
        storage_conflicts,
    ) = edgar._companyfacts_provider_rows(
        json.loads(payload),
        1,
        taxonomy_aware=True,
    )

    assert rows == []
    assert future_periods == 0
    assert context_rejections == 0
    assert storage_conflicts == 1
    legacy_rows, legacy_metadata = edgar.replay_sec_companyfacts_response(
        payload,
        parser_version=edgar.COMPANYFACTS_LEGACY_PARSER_VERSION,
    )
    safe_rows, safe_metadata = edgar.replay_sec_companyfacts_response(
        payload,
        parser_version=edgar.COMPANYFACTS_PARSER_VERSION,
    )
    v1_rows, v1_metadata = edgar.replay_sec_companyfacts_response(
        payload,
        parser_version=edgar.COMPANYFACTS_STORAGE_SAFE_V1_PARSER_VERSION,
    )
    assert len(legacy_rows) == 2
    assert legacy_metadata["rows_rejected_storage_conflict"] == 0
    assert v1_rows == []
    assert v1_metadata["rows_rejected_storage_conflict"] == 1
    assert len(safe_rows) == 1
    assert safe_rows[0]["value"] == legacy_rows[0]["value"]
    assert safe_rows[0]["quarter_value"] == legacy_rows[0]["quarter_value"]
    assert safe_metadata["rows_rejected_storage_conflict"] == 1
    assert edgar.parse_sec_companyfacts_response_v3(payload) == []
    assert edgar._fundamental_rejection_summary(
        {
            "rows_rejected_future_period": 2,
            "rows_rejected_context": 0,
            "rows_rejected_storage_conflict": storage_conflicts,
        }
    ) == (
        3,
        "rejected 2 row(s) with period_end after filing date; "
        "withheld 1 ambiguous database storage key(s)",
    )


def test_companyfacts_v3_emits_unique_database_storage_keys() -> None:
    shared = {
        "end": "2026-06-30",
        "filed": "2026-07-25",
        "fp": "Q2",
        "fy": 2026,
    }
    payload = _metric_payload(
        "Revenues",
        {
            "USD": [
                {
                    **shared,
                    "accn": "0000000001-26-000010",
                    "form": "10-Q",
                    "start": "2026-01-01",
                    "val": 300.0,
                },
                {
                    **shared,
                    "accn": "0000000001-26-000010",
                    "form": "10-Q",
                    "start": "2026-04-01",
                    "val": 120.0,
                },
                {
                    **shared,
                    "accn": "0000000001-26-000011",
                    "form": "8-K",
                    "start": "2026-04-01",
                    "val": 120.0,
                },
            ]
        },
    )

    rows = edgar.parse_sec_companyfacts_response_v3(payload)
    keys = {
        (
            row["cik"],
            row["period_end"],
            row["as_of_date"],
            row["metric"],
        )
        for row in rows
    }

    assert len(rows) == len(keys) == 1


@pytest.mark.parametrize(
    ("updates", "expected_rejections"),
    [
        ({"accn": ""}, 1),
        ({"start": "2026-07-01"}, 1),
        ({"start": "2024-01-01"}, 1),
        ({"form": "S-1"}, 1),
        ({"fp": ""}, 1),
        ({"fy": None}, 1),
    ],
)
def test_companyfacts_v3_withholds_unsupported_period_contexts(
    updates: dict[str, Any],
    expected_rejections: int,
) -> None:
    row = {
        "end": "2026-06-30",
        "filed": "2026-07-25",
        "fp": "Q2",
        "fy": 2026,
        "accn": "0000000001-26-000010",
        "form": "10-Q",
        "start": "2026-04-01",
        "val": 120.0,
        **updates,
    }
    payload = _metric_payload("Revenues", {"USD": [row]})

    (
        rows,
        future_periods,
        context_rejections,
        storage_conflicts,
    ) = edgar._companyfacts_provider_rows(
        json.loads(payload),
        1,
        taxonomy_aware=True,
    )

    assert rows == []
    assert future_periods == 0
    assert context_rejections == expected_rejections
    assert storage_conflicts == 0


def test_companyfacts_v3_withholds_mixed_fiscal_semantics_at_one_storage_key() -> None:
    shared = {
        "end": "2026-06-30",
        "filed": "2026-07-25",
        "fy": 2026,
        "form": "10-Q",
        "start": "2026-04-01",
        "val": 120.0,
    }
    payload = _metric_payload(
        "Revenues",
        {
            "USD": [
                {
                    **shared,
                    "accn": "0000000001-26-000010",
                    "fp": "Q2",
                },
                {
                    **shared,
                    "accn": "0000000001-26-000011",
                    "fp": "Q3",
                },
            ]
        },
    )

    rows, future, contexts, conflicts = edgar._companyfacts_provider_rows(
        json.loads(payload),
        1,
        taxonomy_aware=True,
    )

    assert rows == []
    assert (future, contexts, conflicts) == (0, 0, 1)


def test_companyfacts_v3_rejects_mixed_fiscal_periods_within_one_accession() -> None:
    shared = {
        "end": "2026-06-30",
        "filed": "2026-07-25",
        "fy": 2026,
        "accn": "0000000001-26-000010",
        "form": "10-Q",
        "start": "2026-04-01",
        "val": 120.0,
    }
    payload = _metric_payload(
        "Revenues",
        {
            "USD": [
                {**shared, "fp": "Q2"},
                {**shared, "fp": "Q3"},
            ]
        },
    )

    rows, future, contexts, conflicts = edgar._companyfacts_provider_rows(
        json.loads(payload),
        1,
        taxonomy_aware=True,
    )

    assert rows == []
    assert (future, contexts, conflicts) == (0, 2, 0)

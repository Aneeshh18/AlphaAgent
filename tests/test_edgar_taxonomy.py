from __future__ import annotations

import json

import pytest

from aios.ingest import edgar
from aios.raw_snapshots import _replay_snapshot


def _shares_fact(
    value: float,
    *,
    end: str = "2026-06-30",
    filed: str = "2026-07-25",
    accession: str = "0000000001-26-000001",
) -> dict:
    return {
        "units": {
            "shares": [
                {
                    "end": end,
                    "filed": filed,
                    "fp": "Q2",
                    "fy": 2026,
                    "accn": accession,
                    "form": "10-Q",
                    "val": value,
                }
            ]
        }
    }


def _payload(
    *,
    us_gaap: dict | None = None,
    dei: object | None = None,
) -> dict:
    namespaces: dict[str, object] = {"us-gaap": us_gaap or {}}
    if dei is not None:
        namespaces["dei"] = dei
    return {"cik": 1, "entityName": "Test Corp", "facts": namespaces}


def _encoded(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def _without_locator(row: dict) -> dict:
    return {key: value for key, value in row.items() if key != "source_fact_locator"}


def test_v3_reads_exact_entity_shares_from_dei_and_preserves_pit_fields() -> None:
    payload = _payload(
        us_gaap={
            "WeightedAverageNumberOfSharesOutstandingBasic": _shares_fact(80),
            # The exact name in the wrong taxonomy must not be accepted by v3.
            "EntityCommonStockSharesOutstanding": _shares_fact(90),
        },
        dei={
            "EntityCommonStockSharesOutstanding": _shares_fact(100),
            "EntityCommonStockSharesOutstandingMember": _shares_fact(110),
        },
    )

    rows = edgar.parse_sec_companyfacts_response_v3(_encoded(payload))

    assert [_without_locator(row) for row in rows] == [
        {
            "cik": "0000000001",
            "period_end": "2026-06-30",
            "as_of_date": "2026-07-25",
            "fiscal_period": "Q2_2026",
            "statement": "balance",
            "metric": "shares_out",
            "value": 100.0,
            "quarter_value": 100.0,
            "unit": "shares",
            "source": "edgar",
        }
    ]


def test_v3_does_not_substitute_weighted_average_or_wrong_taxonomy() -> None:
    payload = _payload(
        us_gaap={
            "WeightedAverageNumberOfSharesOutstandingBasic": _shares_fact(80),
            "WeightedAverageNumberOfDilutedSharesOutstanding": _shares_fact(75),
            "EntityCommonStockSharesOutstanding": _shares_fact(90),
        },
        dei={"EntityCommonStockSharesOutstandingMember": _shares_fact(100)},
    )

    assert edgar.parse_sec_companyfacts_response_v3(_encoded(payload)) == []


@pytest.mark.parametrize(
    "dei",
    [
        [],
        {"EntityCommonStockSharesOutstanding": []},
    ],
)
def test_v3_fails_closed_for_malformed_dei_evidence(dei: object) -> None:
    with pytest.raises(ValueError, match="Company Facts dei"):
        edgar.parse_sec_companyfacts_response_v3(_encoded(_payload(dei=dei)))


def test_v3_rejects_entity_shares_filed_before_period_end() -> None:
    payload = _payload(
        dei={
            "EntityCommonStockSharesOutstanding": _shares_fact(
                100,
                end="2026-09-30",
                filed="2026-07-25",
            )
        }
    )

    rows, rejected, context_rejections, storage_conflicts = (
        edgar._companyfacts_provider_rows(
            payload,
            1,
            taxonomy_aware=True,
        )
    )

    assert rows == []
    assert rejected == 1
    assert context_rejections == 0
    assert storage_conflicts == 0


def test_v3_preserves_us_gaap_to_dei_taxonomy_transition() -> None:
    payload = _payload(
        us_gaap={
            "CommonStockSharesOutstanding": _shares_fact(
                90,
                end="2025-12-31",
                filed="2026-01-31",
                accession="0000000001-26-000001",
            )
        },
        dei={
            "EntityCommonStockSharesOutstanding": _shares_fact(
                100,
                accession="0000000001-26-000002",
            )
        },
    )

    rows = edgar.parse_sec_companyfacts_response_v3(_encoded(payload))

    assert [(row["period_end"], row["value"]) for row in rows] == [
        ("2025-12-31", 90.0),
        ("2026-06-30", 100.0),
    ]


def test_v3_withholds_cross_taxonomy_disagreement_for_same_filing_identity() -> None:
    payload = _payload(
        us_gaap={"CommonStockSharesOutstanding": _shares_fact(90)},
        dei={"EntityCommonStockSharesOutstanding": _shares_fact(100)},
    )

    rows = edgar.parse_sec_companyfacts_response_v3(_encoded(payload))

    assert rows == []


def test_v3_wrong_unit_us_gaap_fact_cannot_shadow_valid_dei_shares() -> None:
    us_gaap = _shares_fact(90)
    us_gaap["units"] = {"USD": us_gaap["units"]["shares"]}
    payload = _payload(
        us_gaap={"CommonStockSharesOutstanding": us_gaap},
        dei={"EntityCommonStockSharesOutstanding": _shares_fact(100)},
    )

    rows = edgar.parse_sec_companyfacts_response_v3(_encoded(payload))

    assert [(row["metric"], row["value"], row["unit"]) for row in rows] == [
        ("shares_out", 100.0, "shares")
    ]


def test_v3_rejects_duplicate_share_identity_with_conflicting_fiscal_metadata() -> None:
    first = _shares_fact(100)["units"]["shares"][0]
    second = {**first, "fp": "FY", "fy": 2025}
    payload = _payload(
        dei={"EntityCommonStockSharesOutstanding": {"units": {"shares": [first, second]}}}
    )

    with pytest.raises(ValueError, match="conflict within one accession"):
        edgar.parse_sec_companyfacts_response_v3(_encoded(payload))


def test_parser_version_dispatch_keeps_v2_replay_distinct_from_v3() -> None:
    payload = _encoded(_payload(dei={"EntityCommonStockSharesOutstanding": _shares_fact(100)}))
    snapshot = {"provider": "sec-edgar", "dataset": "companyfacts"}

    assert edgar.COMPANYFACTS_LEGACY_PARSER_VERSION == "sec-companyfacts-v2"
    assert edgar.COMPANYFACTS_PARSER_VERSION == "sec-companyfacts-v2"
    assert edgar.COMPANYFACTS_NEXT_PARSER_VERSION == "sec-companyfacts-v3"
    assert (
        _replay_snapshot(
            {**snapshot, "parser_version": edgar.COMPANYFACTS_LEGACY_PARSER_VERSION},
            payload,
        )
        == []
    )
    assert _replay_snapshot(
        {**snapshot, "parser_version": edgar.COMPANYFACTS_NEXT_PARSER_VERSION},
        payload,
    ) == edgar.parse_sec_companyfacts_response_v3(payload)

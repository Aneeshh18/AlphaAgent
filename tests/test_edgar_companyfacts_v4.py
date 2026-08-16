from __future__ import annotations

import json
from typing import Any

from aios.ingest import edgar


def _fact(value: int, *, accession: str = "0000000001-26-000001") -> dict[str, Any]:
    return {
        "start": "2026-01-01",
        "end": "2026-03-31",
        "filed": "2026-05-01",
        "fp": "Q1",
        "fy": 2026,
        "accn": accession,
        "form": "10-Q",
        "frame": "CY2026Q1",
        "val": value,
    }


def _payload(cik: int, concepts: dict[str, list[dict[str, Any]]]) -> bytes:
    return json.dumps(
        {
            "cik": cik,
            "entityName": "Revenue Policy Test",
            "facts": {
                "us-gaap": {concept: {"units": {"USD": rows}} for concept, rows in concepts.items()}
            },
        }
    ).encode()


def _revenue_rows(payload: bytes, parser_version: str) -> list[dict[str, Any]]:
    rows, _metadata = edgar.replay_sec_companyfacts_response(
        payload,
        parser_version=parser_version,
    )
    return [row for row in rows if row["metric"] == "revenue"]


def test_v4_resolves_existing_revenue_concepts_by_declared_precedence() -> None:
    payload = _payload(
        1,
        {
            "Revenues": [_fact(8_065_000_000)],
            "RevenueFromContractWithCustomerExcludingAssessedTax": [_fact(7_053_000_000)],
        },
    )

    v3_rows, v3_metadata = edgar.replay_sec_companyfacts_response(
        payload,
        parser_version=edgar.COMPANYFACTS_NEXT_PARSER_VERSION,
    )
    v4_rows, v4_metadata = edgar.replay_sec_companyfacts_response(
        payload,
        parser_version=edgar.COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION,
    )

    assert [row for row in v3_rows if row["metric"] == "revenue"] == []
    assert v3_metadata["rows_rejected_storage_conflict"] == 1
    revenue = [row for row in v4_rows if row["metric"] == "revenue"]
    assert len(revenue) == 1
    assert revenue[0]["value"] == 8_065_000_000.0
    assert {locator["concept"] for locator in json.loads(revenue[0]["source_fact_locator"])} == {
        "Revenues"
    }
    assert v4_metadata["revenue_policy"] == "base_concept_precedence"


def test_v4_keeps_non_revenue_concept_conflicts_fail_closed() -> None:
    payload = _payload(
        1,
        {
            "NetCashProvidedByUsedInOperatingActivities": [_fact(100)],
            "CashFlowFromContinuingOperatingActivities": [_fact(200)],
        },
    )

    v3_rows, v3_metadata = edgar.replay_sec_companyfacts_response(
        payload,
        parser_version=edgar.COMPANYFACTS_NEXT_PARSER_VERSION,
    )
    v4_rows, v4_metadata = edgar.replay_sec_companyfacts_response(
        payload,
        parser_version=edgar.COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION,
    )

    assert [row for row in v3_rows if row["metric"] == "cfo"] == []
    assert [row for row in v4_rows if row["metric"] == "cfo"] == []
    assert v3_metadata["rows_rejected_storage_conflict"] == 1
    assert v4_metadata["rows_rejected_storage_conflict"] == 1


def test_v4_assessed_tax_concept_is_limited_to_reviewed_issuers() -> None:
    concepts = {"RevenueFromContractWithCustomerIncludingAssessedTax": [_fact(56_400_000_000)]}

    assert (
        _revenue_rows(
            _payload(1, concepts),
            edgar.COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION,
        )
        == []
    )
    reviewed = _revenue_rows(
        _payload(109_198, concepts),
        edgar.COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION,
    )
    assert [row["value"] for row in reviewed] == [56_400_000_000.0]


def test_v4_utilities_prefer_reviewed_complete_operating_revenue() -> None:
    payload = _payload(
        753_308,
        {
            "RegulatedAndUnregulatedOperatingRevenue": [_fact(27_410_000_000)],
            "Revenues": [_fact(26_900_000_000)],
        },
    )

    rows = _revenue_rows(
        payload,
        edgar.COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION,
    )

    assert [row["value"] for row in rows] == [27_410_000_000.0]
    assert json.loads(rows[0]["source_fact_locator"])[0]["concept"] == (
        "RegulatedAndUnregulatedOperatingRevenue"
    )


def test_v4_uses_reit_lease_revenue_only_for_reviewed_eqr_identity() -> None:
    concepts = {"OperatingLeaseLeaseIncome": [_fact(3_094_000_000)]}

    assert (
        _revenue_rows(
            _payload(1, concepts),
            edgar.COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION,
        )
        == []
    )
    eqr = _revenue_rows(
        _payload(906_107, concepts),
        edgar.COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION,
    )
    assert [row["value"] for row in eqr] == [3_094_000_000.0]


def test_v4_derives_valero_revenue_net_of_excise_tax() -> None:
    payload = _payload(
        1_035_002,
        {
            "RevenueFromContractWithCustomerIncludingAssessedTax": [_fact(122_700_000_000)],
            "ExciseAndSalesTaxes": [_fact(6_700_000_000)],
        },
    )

    rows = _revenue_rows(
        payload,
        edgar.COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION,
    )

    assert [row["value"] for row in rows] == [116_000_000_000.0]
    locator = json.loads(rows[0]["source_fact_locator"])[0]
    assert locator["concept"] == (
        "RevenueFromContractWithCustomerIncludingAssessedTaxLessExciseAndSalesTaxes"
    )
    assert {item["concept"] for item in locator["inputs"]} == {
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "ExciseAndSalesTaxes",
    }


def test_v4_valero_derivation_outranks_generic_revenue() -> None:
    payload = _payload(
        1_035_002,
        {
            "Revenues": [_fact(110_000_000_000)],
            "RevenueFromContractWithCustomerIncludingAssessedTax": [
                _fact(122_700_000_000)
            ],
            "ExciseAndSalesTaxes": [_fact(6_700_000_000)],
        },
    )

    rows = _revenue_rows(
        payload,
        edgar.COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION,
    )

    assert [row["value"] for row in rows] == [116_000_000_000.0]
    assert json.loads(rows[0]["source_fact_locator"])[0]["concept"] == (
        "RevenueFromContractWithCustomerIncludingAssessedTaxLessExciseAndSalesTaxes"
    )


def test_v4_suppresses_incomparable_or_unavailable_revenue() -> None:
    payload = _payload(895_421, {"Revenues": [_fact(70_600_000_000)]})

    rows, metadata = edgar.replay_sec_companyfacts_response(
        payload,
        parser_version=edgar.COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION,
    )

    assert [row for row in rows if row["metric"] == "revenue"] == []
    assert metadata["revenue_policy"] == "suppressed_incomparable_or_unavailable"

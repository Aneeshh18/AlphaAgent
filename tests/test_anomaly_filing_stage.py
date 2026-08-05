from __future__ import annotations

from datetime import date

import pytest

from aios import anomalies


def _snapshot(*, received_at: str = "2026-07-28T12:00:00+00:00") -> dict:
    return {
        "snapshot_id": "raw-submissions-1",
        "payload_sha256": "a" * 64,
        "received_at": received_at,
    }


def test_submissions_filing_index_is_source_bound_and_pit_filtered() -> None:
    payload = {
        "cik": "0000000001",
        "filings": {
            "recent": {
                "form": [
                    "10-Q",
                    "10-K/A",
                    "S-4",
                    "S-4/A",
                    "8-K",
                    "20-F",
                ],
                "filingDate": [
                    "2026-07-20",
                    "2026-07-21",
                    "2026-07-22",
                    "2026-07-23",
                    "2026-07-24",
                    "2026-07-28",
                ],
            }
        },
    }

    result = anomalies._sec_submissions_filing_index(
        payload=payload,
        snapshot=_snapshot(),
        decision_date=date(2026, 7, 27),
    )

    assert result["availability"] == "exact_submissions_filing_index"
    assert result["source"] == {
        "snapshot_id": "raw-submissions-1",
        "payload_sha256": "a" * 64,
        "received_at": "2026-07-28T12:00:00+00:00",
    }
    assert result["decision_visible_filing_count"] == 5
    assert result["excluded_after_decision_count"] == 1
    assert result["periodic_form_count"] == 2
    assert [row["form"] for row in result["periodic_forms"]] == [
        "10-K/A",
        "10-Q",
    ]
    assert result["registration_form_count"] == 2
    assert [row["form"] for row in result["registration_forms"]] == [
        "S-4",
        "S-4/A",
    ]
    assert len(result["proof_sha256"]) == 64


def test_submissions_filing_index_marks_absent_legacy_index_unknown() -> None:
    result = anomalies._sec_submissions_filing_index(
        payload={"cik": "0000000001"},
        snapshot=_snapshot(),
        decision_date=date(2026, 7, 27),
    )

    assert result["availability"] == "filing_index_not_present"
    assert result["periodic_form_count"] is None
    assert result["registration_form_count"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {
            "filings": {
                "recent": {
                    "form": ["10-Q", "S-1"],
                    "filingDate": ["2026-07-20"],
                }
            }
        },
        {
            "filings": {
                "recent": {
                    "form": ["10-Q"],
                    "filingDate": "2026-07-20",
                }
            }
        },
    ],
)
def test_submissions_filing_index_rejects_unprovable_parallel_arrays(
    payload,
) -> None:
    with pytest.raises(ValueError, match="must be lists|not aligned"):
        anomalies._sec_submissions_filing_index(
            payload=payload,
            snapshot=_snapshot(),
            decision_date=date(2026, 7, 27),
        )


def test_submissions_filing_index_rejects_date_after_snapshot_boundary() -> None:
    payload = {
        "filings": {
            "recent": {
                "form": ["10-Q"],
                "filingDate": ["2026-07-29"],
            }
        }
    }

    with pytest.raises(ValueError, match="after its exact snapshot boundary"):
        anomalies._sec_submissions_filing_index(
            payload=payload,
            snapshot=_snapshot(),
            decision_date=date(2026, 7, 30),
        )


class _OwnerContextStore:
    def query(self, sql, params=None):
        if "anomaly_active_owner_context" in sql:
            assert params == (
                "security:xom",
                "2026-07-27",
                "2026-07-27",
                "2026-07-27",
            )
            return [
                {
                    "security_id": "security:xom",
                    "issuer_id": "issuer:new",
                    "effective_start": "2026-07-02",
                    "effective_end": None,
                    "verified_date": "2026-07-21",
                    "source": "reviewed-transition",
                    "canonical_name": "New reporting owner",
                    "canonical_ticker": "XOM",
                }
            ]
        if "anomaly_predecessor_owner_context" in sql:
            assert params == (
                "2026-07-27",
                "security:xom",
                "2026-07-27",
                "2026-07-02",
                "issuer:new",
            )
            return [
                {
                    "security_id": "security:xom",
                    "issuer_id": "issuer:old",
                    "effective_start": "2025-01-01",
                    "effective_end": "2026-07-02",
                    "verified_date": "2026-07-21",
                    "source": "reviewed-transition",
                    "canonical_name": "Old reporting owner",
                    "canonical_ticker": "XOM",
                    "cik": "34088",
                }
            ]
        raise AssertionError(sql)


def test_owner_context_keeps_predecessor_facts_independent(
    tmp_path,
    monkeypatch,
) -> None:
    predecessor_snapshot = {
        "snapshot_id": "raw-old-companyfacts",
        "role": "companyfacts",
        "provider": "sec-edgar",
        "dataset": "companyfacts",
        "artifact_kind": "exact_response",
        "http_status": 200,
        "received_at": "2026-07-25T12:00:00+00:00",
        "payload_sha256": "b" * 64,
        "parser_version": "sec-companyfacts-v2",
        "parsed_row_count": 10,
        "parsed_rows_sha256": "c" * 64,
        "relative_path": "data/raw/old.json.gz",
    }

    def verified_predecessor(*, store, root, issuer_members, decision_date):
        assert isinstance(store, _OwnerContextStore)
        assert root == tmp_path
        assert issuer_members == {
            "issuer:old": {
                "issuer_id": "issuer:old",
                "cik": "0000034088",
            }
        }
        assert decision_date == date(2026, 7, 27)
        return (
            {
                "issuer:old": {
                    "accepted_rows": 2_395,
                    "first_as_of_date": "2009-08-05",
                    "latest_as_of_date": "2026-05-04",
                    "lineage": {
                        "identity_binding": "legacy_exact_rowset_equality",
                        "companyfacts_snapshot": predecessor_snapshot,
                    },
                }
            },
            [predecessor_snapshot],
        )

    monkeypatch.setattr(
        anomalies,
        "_verified_sec_fundamental_coverage",
        verified_predecessor,
    )

    context, snapshots = anomalies._reviewed_owner_filing_context(
        store=_OwnerContextStore(),
        root=tmp_path,
        security_id="security:xom",
        active_issuer_id="issuer:new",
        decision_date=date(2026, 7, 27),
    )

    assert context["active_owner_start"] == "2026-07-02"
    assert context["predecessor_owner"]["issuer_id"] == "issuer:old"
    assert context["predecessor_owner"]["cik"] == "0000034088"
    assert context["transition_gap_days"] == 0
    coverage = context["predecessor_fact_coverage"]
    assert coverage["state"] == "covered_with_verified_source_replay"
    assert coverage["accepted_rows"] == 2_395
    assert coverage["facts_transfer_to_active_issuer"] is False
    assert snapshots == [predecessor_snapshot]
    assert len(context["proof_sha256"]) == 64

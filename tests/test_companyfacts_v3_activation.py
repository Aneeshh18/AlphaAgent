from __future__ import annotations

import gzip
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from aios.companyfacts_v3_activation import (
    activate_companyfacts_v3,
    prepare_companyfacts_v3_activation,
)
from aios.ingest.edgar import (
    COMPANYFACTS_PARSER_VERSION,
    canonical_sec_fundamental_row_sha256,
    replay_sec_companyfacts_response,
)
from aios.raw_snapshots import canonical_parsed_rows_sha256
from aios.sec_rejections import canonical_rejection_codes
from aios.storage.store import Store

_ISSUER_ID = "issuer:exm"
_TICKER = "EXM"
_CIK = 1
_AS_OF = "2026-08-10"


def _conflicting_payload() -> bytes:
    """One clean metric plus one genuinely conflicting storage key.

    Two facts share the same (period_end, as_of_date) but disagree on value.
    v2's `preserve_legacy_winner` keeps one arbitrarily; v3 withholds the
    whole key. This is real production behavior (see `_select_metric_storage_rows`
    in `edgar.py`), reproduced minimally so the test never depends on network
    or fixture data.
    """
    clean_fact = {
        "start": "2025-01-01",
        "end": "2025-12-31",
        "val": 500.0,
        "accn": "0000000001-26-000001",
        "fy": 2025,
        "fp": "FY",
        "form": "10-K",
        "filed": "2026-02-01",
        "frame": "CY2025",
    }
    conflicting_a = {
        "start": "2024-01-01",
        "end": "2024-12-31",
        "val": 1000.0,
        "accn": "0000000001-26-000002",
        "fy": 2024,
        "fp": "FY",
        "form": "10-K",
        "filed": "2026-01-15",
        "frame": "CY2024",
    }
    conflicting_b = {**conflicting_a, "val": 2000.0, "accn": "0000000001-26-000003"}
    return json.dumps(
        {
            "cik": _CIK,
            "entityName": "Example Corp",
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {"USD": [clean_fact, conflicting_a, conflicting_b]}
                    }
                }
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _install_payload(root: Path, payload: bytes) -> dict[str, Any]:
    digest = hashlib.sha256(payload).hexdigest()
    compressed = gzip.compress(payload, mtime=0)
    relative = Path("data") / "raw" / "sec-edgar" / "companyfacts" / f"{digest}.json.gz"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(compressed)
    return {
        "payload_sha256": digest,
        "relative_path": relative.as_posix(),
        "original_bytes": len(payload),
        "stored_bytes": len(compressed),
        "compression": "gzip",
    }


def _seed_issuer(store: Store, root: Path, *, payload: bytes) -> None:
    store.upsert_universe_membership(
        [
            {
                "universe_id": "sp500",
                "ticker": _TICKER,
                "security_id": "security:exm",
                "effective_start": "2020-01-01",
                "effective_end": "2027-01-01",
                "known_date": "2020-01-01",
                "end_known_date": None,
                "source": "reviewed:test",
            }
        ]
    )
    store.upsert_security_identities(
        [
            {
                "universe_id": "sp500",
                "ticker": _TICKER,
                "security_id": "security:exm",
                "effective_start": "2020-01-01",
                "effective_end": "2027-01-01",
                "known_date": "2020-01-01",
                "identity_status": "bounded_ticker",
                "source": "reviewed:test",
            }
        ]
    )
    store.upsert_reference_identities(
        issuers=[
            {
                "issuer_id": _ISSUER_ID,
                "canonical_name": "Example Corp",
                "canonical_ticker": _TICKER,
                "source": "sec:test",
            }
        ],
        cik_history=[
            {
                "issuer_id": _ISSUER_ID,
                "cik": f"{_CIK:010d}",
                "effective_start": "2020-01-01",
                "effective_end": None,
                "verified_date": "2026-08-01",
                "source": "sec:test",
            }
        ],
        security_issuers=[
            {
                "security_id": "security:exm",
                "issuer_id": _ISSUER_ID,
                "effective_start": "2020-01-01",
                "effective_end": None,
                "verified_date": "2026-08-01",
                "source": "sec:test",
            }
        ],
        provider_symbols=[
            {
                "provider": "yfinance",
                "provider_symbol": _TICKER,
                "security_id": "security:exm",
                "data_start": "2020-01-01",
                "data_end": "2026-12-31",
                "mapping_status": "verified",
                "verified_date": "2026-08-01",
                "source": "provider:test",
            }
        ],
    )

    payload_record = _install_payload(root, payload)
    v2_rows, v2_metadata = replay_sec_companyfacts_response(
        payload, parser_version=COMPANYFACTS_PARSER_VERSION
    )
    run_id = str(uuid4())
    snapshot_id = f"raw-{uuid4().hex}"
    received_at = "2026-08-01T10:00:00"
    v2_hash = canonical_parsed_rows_sha256(v2_rows)
    rejection_codes = canonical_rejection_codes(v2_metadata["rejection_codes"])

    store.record_raw_snapshot(
        payload=payload_record,
        snapshot={
            "snapshot_id": snapshot_id,
            "provider": "sec-edgar",
            "dataset": "companyfacts",
            "artifact_kind": "exact_response",
            "requested_at": received_at,
            "received_at": received_at,
            "http_status": 200,
            "content_type": "application/json",
            "request_fingerprint": hashlib.sha256(b"companyfacts:test-request").hexdigest(),
            "payload_sha256": payload_record["payload_sha256"],
            "adapter_name": "sec-http",
            "adapter_version": "1",
            "parser_version": COMPANYFACTS_PARSER_VERSION,
            "parsed_row_count": len(v2_rows),
            "parsed_rows_sha256": v2_hash,
            "parsed_rows_rejected": v2_metadata["rows_rejected"],
            "parsed_rejection_codes": rejection_codes,
        },
        ingest_run_id=run_id,
        role="companyfacts",
    )
    store.record_ingest(
        source="edgar:issuer-cik-history",
        table_name="fundamentals",
        rows_inserted=len(v2_rows),
        rows_rejected=v2_metadata["rows_rejected"],
        started_at=datetime.fromisoformat(received_at),
        finished_at=datetime.fromisoformat(received_at),
        status="warning" if v2_metadata["rows_rejected"] else "success",
        error=(
            f"withheld {v2_metadata['rows_rejected']} ambiguous storage key(s)"
            if v2_metadata["rows_rejected"]
            else None
        ),
        run_id=run_id,
        subject_type="issuer",
        subject_id=_ISSUER_ID,
        rejection_codes=v2_metadata["rejection_codes"],
    )
    store.upsert_fundamentals(
        [
            {
                "ticker": _TICKER,
                "issuer_id": _ISSUER_ID,
                "security_id": "security:exm",
                "ingest_run_id": run_id,
                "source_snapshot_id": snapshot_id,
                "source_rowset_sha256": v2_hash,
                "source_row_sha256": canonical_sec_fundamental_row_sha256(row),
                "period_end": row["period_end"],
                "as_of_date": row["as_of_date"],
                "fiscal_period": row.get("fiscal_period"),
                "statement": row.get("statement"),
                "metric": row["metric"],
                "value": row.get("value"),
                "quarter_value": row.get("quarter_value"),
                "unit": row.get("unit") or "USD",
                "source": row.get("source") or "edgar",
            }
            for row in v2_rows
        ]
    )


def _prepare(root: Path, database: Path, *, actor: str = "operator-1") -> Any:
    return prepare_companyfacts_v3_activation(
        project_root=root,
        database_path=database,
        application_version="0.3.0-test",
        as_of=_AS_OF,
        issuer_ids=[_ISSUER_ID],
        actor=actor,
    )


def test_activation_upserts_and_tombstones_a_conflicting_key(tmp_path: Path) -> None:
    database = tmp_path / "aios.duckdb"
    store = Store(database)
    _seed_issuer(store, tmp_path, payload=_conflicting_payload())
    store.close()

    preparation = _prepare(tmp_path, database)
    assert preparation.issuer_ids == (_ISSUER_ID,)

    result = activate_companyfacts_v3(
        project_root=tmp_path,
        database_path=database,
        plan_path=preparation.plan_path,
        expected_plan_sha256=preparation.plan_sha256,
        actor="operator-1",
        confirm=True,
    )
    assert result.counts["issuers"] == 1
    assert result.counts["removed"] == 1
    assert result.counts["added"] == 0
    assert result.counts["changed"] == 0

    reopened = Store(database, read_only=True)
    try:
        live = reopened.query(
            "SELECT period_end, as_of_date, metric FROM fundamentals WHERE ticker = ?",
            (_TICKER,),
        )
        assert len(live) == 1
        assert str(live[0]["period_end"]) == "2025-12-31"

        tombstones = reopened.query(
            "SELECT COUNT(*) AS n FROM fundamental_versions "
            "WHERE issuer_id = ? AND is_deleted",
            (_ISSUER_ID,),
        )
        assert tombstones[0]["n"] == 1

        generations = reopened.query(
            "SELECT purpose, decision_date FROM fundamental_evidence_generations "
            "WHERE generation_id = ?",
            (result.generation_id,),
        )
        assert generations[0]["purpose"] == "companyfacts_v3_activation"
        assert generations[0]["decision_date"] == date.fromisoformat(_AS_OF)

        receipts = reopened.query(
            "SELECT status, activation_plan_sha256 FROM companyfacts_v3_activations "
            "WHERE activation_id = ?",
            (result.activation_id,),
        )
        assert receipts[0]["status"] == "accepted"
        assert receipts[0]["activation_plan_sha256"] == preparation.plan_sha256
    finally:
        reopened.close()


def test_activation_requires_explicit_confirmation(tmp_path: Path) -> None:
    database = tmp_path / "aios.duckdb"
    store = Store(database)
    _seed_issuer(store, tmp_path, payload=_conflicting_payload())
    store.close()
    preparation = _prepare(tmp_path, database)

    try:
        activate_companyfacts_v3(
            project_root=tmp_path,
            database_path=database,
            plan_path=preparation.plan_path,
            expected_plan_sha256=preparation.plan_sha256,
            actor="operator-1",
            confirm=False,
        )
        raise AssertionError("expected confirm=False to be refused")
    except ValueError as exc:
        assert "confirm" in str(exc)


def test_activation_rejects_a_mismatched_actor(tmp_path: Path) -> None:
    database = tmp_path / "aios.duckdb"
    store = Store(database)
    _seed_issuer(store, tmp_path, payload=_conflicting_payload())
    store.close()
    preparation = _prepare(tmp_path, database, actor="operator-1")

    try:
        activate_companyfacts_v3(
            project_root=tmp_path,
            database_path=database,
            plan_path=preparation.plan_path,
            expected_plan_sha256=preparation.plan_sha256,
            actor="someone-else",
            confirm=True,
        )
        raise AssertionError("expected actor mismatch to be refused")
    except ValueError as exc:
        assert "actor" in str(exc)


def test_activation_rejects_a_drifted_backup(tmp_path: Path) -> None:
    database = tmp_path / "aios.duckdb"
    store = Store(database)
    _seed_issuer(store, tmp_path, payload=_conflicting_payload())
    store.close()
    preparation = _prepare(tmp_path, database)

    manifest_path = preparation.backup.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    (preparation.backup.path / manifest["database_file"]).write_bytes(b"corrupted")

    try:
        activate_companyfacts_v3(
            project_root=tmp_path,
            database_path=database,
            plan_path=preparation.plan_path,
            expected_plan_sha256=preparation.plan_sha256,
            actor="operator-1",
            confirm=True,
        )
        raise AssertionError("expected a corrupted backup to be refused")
    except ValueError:
        pass


def test_activation_rejects_live_drift_since_the_plan_was_reviewed(tmp_path: Path) -> None:
    database = tmp_path / "aios.duckdb"
    store = Store(database)
    _seed_issuer(store, tmp_path, payload=_conflicting_payload())
    store.close()
    preparation = _prepare(tmp_path, database)

    # Simulate the live relation changing after the plan was reviewed: touch
    # the one surviving fundamental row's value directly.
    drifting = Store(database)
    drifting.execute(
        "UPDATE fundamentals SET value = value + 1 WHERE ticker = ? AND metric = ?",
        (_TICKER, "revenue"),
    )
    drifting.close()

    try:
        activate_companyfacts_v3(
            project_root=tmp_path,
            database_path=database,
            plan_path=preparation.plan_path,
            expected_plan_sha256=preparation.plan_sha256,
            actor="operator-1",
            confirm=True,
        )
        raise AssertionError("expected live drift to be refused")
    except ValueError as exc:
        assert "CAS" in str(exc)


def test_prepare_refuses_an_ineligible_issuer(tmp_path: Path) -> None:
    database = tmp_path / "aios.duckdb"
    store = Store(database)
    store.close()

    try:
        _prepare(tmp_path, database)
        raise AssertionError("expected an issuer with no evidence to be refused")
    except ValueError as exc:
        assert "no evidence" in str(exc) or "eligible" in str(exc)

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import aios.companyfacts_replay as replay_module
from aios.companyfacts_replay import (
    COMPANYFACTS_REPLAY_PLAN_REPORT_DIRECTORY,
    persist_companyfacts_v3_plan,
    preview_companyfacts_v3_replay,
    read_companyfacts_v3_plan,
)
from aios.ingest.edgar import (
    COMPANYFACTS_CAPTURE_PARSER_VERSION,
    COMPANYFACTS_PARSER_VERSION,
    canonical_sec_fundamental_row_sha256,
    parse_sec_companyfacts_response_v2,
)
from aios.raw_snapshots import canonical_parsed_rows_sha256


class FakeStore:
    def __init__(
        self,
        evidence: list[dict[str, Any]],
        *,
        current: dict[str, list[dict[str, Any]]] | None = None,
        references: dict[str, dict[str, Any] | str] | None = None,
    ) -> None:
        self.evidence = evidence
        self.current = current or {}
        self.references = references or {}
        self.queries: list[tuple[str, tuple[Any, ...] | None]] = []

    def query(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
    ) -> list[dict[str, Any]]:
        self.queries.append((sql, params))
        normalized = " ".join(sql.split())
        if "FROM information_schema.columns" in normalized:
            assert params is not None
            table_name = str(params[0])
            if table_name == "ingest_log":
                columns = {
                    "subject_type",
                    "subject_id",
                    "rejection_codes",
                }
            elif table_name == "fundamentals":
                columns = {
                    "issuer_id",
                    "ingest_run_id",
                    "source_snapshot_id",
                    "source_rowset_sha256",
                    "source_row_sha256",
                }
            else:
                raise AssertionError(table_name)
            return [{"column_name": column} for column in sorted(columns)]
        if "FROM raw_snapshots AS snapshot" in normalized:
            return [dict(row) for row in self.evidence]
        if "FROM fundamentals AS fundamental" in normalized:
            assert params is not None
            return [dict(row) for row in self.current.get(str(params[0]), [])]
        raise AssertionError(f"unexpected planner query: {normalized}")

    def issuer_reference(
        self,
        issuer_id: str,
        *,
        as_of: str,
    ) -> dict[str, Any] | None:
        assert as_of == "2026-07-30"
        value = self.references.get(issuer_id)
        if value == "ambiguous":
            raise ValueError("ambiguous SEC CIK")
        return dict(value) if isinstance(value, dict) else None


def _companyfacts_payload(*, cik: int = 1, value: float = 100.0) -> bytes:
    return json.dumps(
        {
            "cik": cik,
            "entityName": "Example Corp",
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {
                            "USD": [
                                {
                                    "start": "2025-01-01",
                                    "end": "2025-12-31",
                                    "val": value,
                                    "accn": "0000000001-26-000001",
                                    "fy": 2025,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2026-02-01",
                                    "frame": "CY2025",
                                }
                            ]
                        }
                    }
                }
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _install_payload(
    root: Path,
    payload: bytes,
    *,
    suffix: str,
) -> dict[str, Any]:
    digest = hashlib.sha256(payload).hexdigest()
    compressed = gzip.compress(payload, mtime=0)
    relative = (
        Path("data")
        / "raw"
        / "sec-edgar"
        / "companyfacts"
        / "2026-07-30"
        / f"{digest}-{suffix}.json.gz"
    )
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


def _evidence(
    root: Path,
    *,
    issuer_id: str | None = "issuer-1",
    snapshot_id: str = "raw-1",
    run_id: str = "run-1",
    parser_version: str = COMPANYFACTS_PARSER_VERSION,
    status: str = "success",
    parsed_row_count: int | None = None,
    parsed_rows_sha256: str | None = None,
    rows_inserted: int | None = None,
    rows_rejected: int = 0,
    rejection_codes: str | None = None,
    error: str | None = None,
    received_at: str = "2026-07-30T10:00:00",
    payload: bytes | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    body = payload or _companyfacts_payload()
    provider_rows = parse_sec_companyfacts_response_v2(body)
    payload_record = _install_payload(root, body, suffix=snapshot_id)
    parsed_count = len(provider_rows) if parsed_row_count is None else parsed_row_count
    parsed_hash = (
        canonical_parsed_rows_sha256(provider_rows)
        if parsed_rows_sha256 is None
        else parsed_rows_sha256
    )
    inserted = (
        len({_storage_key(row) for row in provider_rows})
        if rows_inserted is None
        else rows_inserted
    )
    return (
        {
            "snapshot_id": snapshot_id,
            "provider": "sec-edgar",
            "dataset": "companyfacts",
            "artifact_kind": "exact_response",
            "received_at": received_at,
            "http_status": 200,
            "adapter_name": "sec-http",
            "adapter_version": "1",
            "parser_version": parser_version,
            "parsed_row_count": parsed_count,
            "parsed_rows_sha256": parsed_hash,
            "run_id": run_id,
            "role": "companyfacts",
            "ingest_id": int(snapshot_id.removeprefix("raw-") or "1"),
            "source": "edgar:issuer-cik-history",
            "table_name": "fundamentals",
            "subject_type": "issuer" if issuer_id is not None else None,
            "subject_id": issuer_id,
            "rows_inserted": inserted,
            "rows_rejected": rows_rejected,
            "rejection_codes": rejection_codes,
            "status": status,
            "error": error,
            "finished_at": "2026-07-30T10:00:01",
            **payload_record,
        },
        provider_rows,
    )


def _current_row(
    provider_row: dict[str, Any],
    evidence: dict[str, Any],
    *,
    issuer_id: str,
    value: float | None = None,
    lineaged: bool = True,
) -> dict[str, Any]:
    return {
        "ticker": "EXM",
        "issuer_id": issuer_id,
        "security_id": "security-1",
        "ingest_run_id": evidence["run_id"] if lineaged else None,
        "source_snapshot_id": evidence["snapshot_id"] if lineaged else None,
        "source_rowset_sha256": (evidence["parsed_rows_sha256"] if lineaged else None),
        "source_row_sha256": (
            canonical_sec_fundamental_row_sha256(provider_row) if lineaged else None
        ),
        "period_end": date.fromisoformat(provider_row["period_end"]),
        "as_of_date": date.fromisoformat(provider_row["as_of_date"]),
        "fiscal_period": provider_row["fiscal_period"],
        "statement": provider_row["statement"],
        "metric": provider_row["metric"],
        "value": provider_row["value"] if value is None else value,
        "quarter_value": provider_row["quarter_value"],
        "unit": provider_row["unit"],
        "source": provider_row["source"],
    }


def _reference() -> dict[str, Any]:
    return {
        "issuer_id": "issuer-1",
        "canonical_ticker": "EXM",
        "cik": "0000000001",
    }


def _tree_state(root: Path) -> list[tuple[str, int, str]]:
    state = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        payload = path.read_bytes()
        state.append(
            (
                path.relative_to(root).as_posix(),
                len(payload),
                hashlib.sha256(payload).hexdigest(),
            )
        )
    return state


def test_preview_is_read_only_deterministic_and_marks_exact_relation_eligible(
    tmp_path: Path,
) -> None:
    evidence, provider_rows = _evidence(tmp_path)
    store = FakeStore(
        [evidence],
        current={"issuer-1": [_current_row(provider_rows[0], evidence, issuer_id="issuer-1")]},
        references={"issuer-1": _reference()},
    )
    before = _tree_state(tmp_path)

    first = preview_companyfacts_v3_replay(
        tmp_path,
        store=store,
        as_of="2026-07-30",
    )
    second = preview_companyfacts_v3_replay(
        tmp_path,
        store=store,
        as_of=date(2026, 7, 30),
    )

    assert _tree_state(tmp_path) == before
    assert first.plan_sha256 == second.plan_sha256
    assert first.plan == second.plan
    assert first.eligible_issuers == 1
    assert first.activation_available is False
    issuer = first.plan["issuers"][0]
    assert issuer["classification"] == "eligible"
    assert issuer["reasons"] == []
    assert issuer["v2_replay"]["provider_row_count"] == 1
    assert issuer["v3_candidate"]["storage_row_count"] == 1
    assert issuer["delta"] == {
        "added_storage_keys": 0,
        "removed_storage_keys": 0,
        "changed_storage_keys": 0,
        "unchanged_storage_keys": 1,
    }
    assert all("SELECT" in sql for sql, _params in store.queries)


def test_preview_excludes_source_observations_received_after_as_of(
    tmp_path: Path,
) -> None:
    past, provider_rows = _evidence(
        tmp_path,
        snapshot_id="raw-9",
        run_id="run-past",
    )
    future, _future_rows = _evidence(
        tmp_path,
        snapshot_id="raw-10",
        run_id="run-future",
        received_at="2026-07-31T00:00:01",
    )
    store = FakeStore(
        [past, future],
        current={"issuer-1": [_current_row(provider_rows[0], past, issuer_id="issuer-1")]},
        references={"issuer-1": _reference()},
    )

    preview = preview_companyfacts_v3_replay(
        tmp_path,
        store=store,
        as_of="2026-07-30",
    )

    assert preview.plan["scope"]["as_of"] == "2026-07-30"
    assert preview.plan["issuers"][0]["source"]["snapshot_id"] == "raw-9"
    assert preview.plan["excluded_evidence"][0]["snapshot_id"] == "raw-10"
    assert preview.plan["excluded_evidence"][0]["reasons"] == ["received_after_as_of"]


def test_preview_mirrors_first_winner_for_duplicate_v2_storage_keys(
    tmp_path: Path,
) -> None:
    decoded = json.loads(_companyfacts_payload())
    facts = decoded["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"][
        "units"
    ]["USD"]
    facts.append(
        {
            **facts[0],
            "accn": "0000000001-26-000002",
            "val": 999.0,
        }
    )
    payload = json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode()
    evidence, provider_rows = _evidence(tmp_path, payload=payload)
    assert len(provider_rows) == 2
    store = FakeStore(
        [evidence],
        current={"issuer-1": [_current_row(provider_rows[0], evidence, issuer_id="issuer-1")]},
        references={"issuer-1": _reference()},
    )

    preview = preview_companyfacts_v3_replay(
        tmp_path,
        store=store,
        as_of="2026-07-30",
    )

    issuer = preview.plan["issuers"][0]
    assert issuer["classification"] == "eligible"
    assert issuer["v2_replay"]["provider_row_count"] == 2
    assert issuer["v2_replay"]["storage_row_count"] == 1


def test_preview_accepts_structured_v2_warning_only_when_replay_matches(
    tmp_path: Path,
) -> None:
    decoded = json.loads(_companyfacts_payload())
    facts = decoded["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"][
        "units"
    ]["USD"]
    facts.append(
        {
            **facts[0],
            "accn": "0000000001-26-000002",
            "start": "2026-07-01",
            "end": "2026-09-30",
            "filed": "2026-07-30",
            "fp": "Q3",
            "val": 999.0,
        }
    )
    payload = json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode()
    evidence, provider_rows = _evidence(
        tmp_path,
        payload=payload,
        status="warning",
    )
    evidence["rows_rejected"] = 1
    evidence["rejection_codes"] = '["future_period"]'
    evidence["error"] = "operator-facing text"
    store = FakeStore(
        [evidence],
        current={"issuer-1": [_current_row(provider_rows[0], evidence, issuer_id="issuer-1")]},
        references={"issuer-1": _reference()},
    )

    preview = preview_companyfacts_v3_replay(
        tmp_path,
        store=store,
        as_of="2026-07-30",
    )
    assert preview.eligible_issuers == 1

    evidence["rejection_codes"] = '["storage_conflict"]'
    refused = preview_companyfacts_v3_replay(
        tmp_path,
        store=store,
        as_of="2026-07-30",
    )
    assert refused.plan["issuers"][0]["reasons"] == ["source_rejection_codes_mismatch"]


def test_success_outcome_cannot_use_legacy_future_warning_compatibility(
    tmp_path: Path,
) -> None:
    decoded = json.loads(_companyfacts_payload())
    facts = decoded["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"][
        "units"
    ]["USD"]
    facts.append(
        {
            **facts[0],
            "accn": "0000000001-26-000002",
            "start": "2026-07-01",
            "end": "2026-09-30",
            "filed": "2026-07-30",
            "fp": "Q3",
            "val": 999.0,
        }
    )
    payload = json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode()
    evidence, provider_rows = _evidence(
        tmp_path,
        payload=payload,
        status="success",
        rows_rejected=1,
        rejection_codes=None,
        error="Rejected 1 row with period_end after filing date",
    )
    store = FakeStore(
        [evidence],
        current={"issuer-1": [_current_row(provider_rows[0], evidence, issuer_id="issuer-1")]},
        references={"issuer-1": _reference()},
    )

    preview = preview_companyfacts_v3_replay(
        tmp_path,
        store=store,
        as_of="2026-07-30",
    )

    assert preview.eligible_issuers == 0
    assert preview.plan["issuers"][0]["reasons"] == ["failed_ingest"]


def test_preview_fails_closed_for_every_unsupported_evidence_class(
    tmp_path: Path,
) -> None:
    rows: list[dict[str, Any]] = []
    current: dict[str, list[dict[str, Any]]] = {}
    references: dict[str, dict[str, Any] | str] = {}

    capture, _capture_rows = _evidence(
        tmp_path,
        issuer_id="issuer-capture",
        snapshot_id="raw-2",
        run_id="run-capture",
        parser_version=COMPANYFACTS_CAPTURE_PARSER_VERSION,
    )
    capture["parsed_row_count"] = None
    capture["parsed_rows_sha256"] = None
    rows.append(capture)

    failed, _failed_rows = _evidence(
        tmp_path,
        issuer_id="issuer-failed",
        snapshot_id="raw-3",
        run_id="run-failed",
        status="failed",
    )
    rows.append(failed)

    zero, _zero_rows = _evidence(
        tmp_path,
        issuer_id="issuer-zero",
        snapshot_id="raw-4",
        run_id="run-zero",
        parsed_row_count=0,
        parsed_rows_sha256=canonical_parsed_rows_sha256([]),
        rows_inserted=0,
    )
    rows.append(zero)

    unscoped, _unscoped_rows = _evidence(
        tmp_path,
        issuer_id=None,
        snapshot_id="raw-5",
        run_id="run-unscoped",
    )
    rows.append(unscoped)

    unlineaged, unlineaged_rows = _evidence(
        tmp_path,
        issuer_id="issuer-unlineaged",
        snapshot_id="raw-6",
        run_id="run-unlineaged",
    )
    rows.append(unlineaged)
    current["issuer-unlineaged"] = [
        _current_row(
            unlineaged_rows[0],
            unlineaged,
            issuer_id="issuer-unlineaged",
            lineaged=False,
        )
    ]
    references["issuer-unlineaged"] = _reference()

    mismatch, mismatch_rows = _evidence(
        tmp_path,
        issuer_id="issuer-mismatch",
        snapshot_id="raw-7",
        run_id="run-mismatch",
    )
    rows.append(mismatch)
    current["issuer-mismatch"] = [
        _current_row(
            mismatch_rows[0],
            mismatch,
            issuer_id="issuer-mismatch",
            value=999.0,
        )
    ]
    references["issuer-mismatch"] = _reference()

    ambiguous, ambiguous_rows = _evidence(
        tmp_path,
        issuer_id="issuer-ambiguous",
        snapshot_id="raw-8",
        run_id="run-ambiguous",
    )
    rows.append(ambiguous)
    current["issuer-ambiguous"] = [
        _current_row(
            ambiguous_rows[0],
            ambiguous,
            issuer_id="issuer-ambiguous",
        )
    ]
    references["issuer-ambiguous"] = "ambiguous"

    preview = preview_companyfacts_v3_replay(
        tmp_path,
        store=FakeStore(rows, current=current, references=references),
        as_of="2026-07-30",
    )
    reasons = {row["issuer_id"]: set(row["reasons"]) for row in preview.plan["issuers"]}

    assert "capture_only" in reasons["issuer-capture"]
    assert "failed_ingest" in reasons["issuer-failed"]
    assert "zero_rows" in reasons["issuer-zero"]
    assert "unlineaged_current_relation" in reasons["issuer-unlineaged"]
    assert "current_relation_mismatch" in reasons["issuer-mismatch"]
    assert "ambiguous_identity" in reasons["issuer-ambiguous"]
    assert preview.plan["excluded_evidence"][0]["reasons"] == ["unscoped_ingest"]
    assert preview.eligible_issuers == 0
    assert preview.activation_available is False


def test_plan_persistence_is_content_addressed_write_once_and_tamper_evident(
    tmp_path: Path,
) -> None:
    evidence, provider_rows = _evidence(tmp_path)
    store = FakeStore(
        [evidence],
        current={"issuer-1": [_current_row(provider_rows[0], evidence, issuer_id="issuer-1")]},
        references={"issuer-1": _reference()},
    )
    preview = preview_companyfacts_v3_replay(
        tmp_path,
        store=store,
        as_of="2026-07-30",
    )

    path = persist_companyfacts_v3_plan(tmp_path, preview)

    assert path == (
        tmp_path / COMPANYFACTS_REPLAY_PLAN_REPORT_DIRECTORY / f"{preview.plan_sha256}.json"
    )
    assert read_companyfacts_v3_plan(path) == preview.plan
    assert persist_companyfacts_v3_plan(tmp_path, preview) == path

    envelope = json.loads(path.read_text())
    envelope["payload"]["summary"]["eligible_issuers"] = 99
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(ValueError, match="checksum mismatch"):
        read_companyfacts_v3_plan(path)
    with pytest.raises(ValueError, match="artifact collision"):
        persist_companyfacts_v3_plan(tmp_path, preview)


@pytest.mark.parametrize(
    ("forgery", "error"),
    [
        ("unknown_policy_override", "schema"),
        ("missing_forbidden_actions", "activation contract"),
        ("inconsistent_summary", "summary"),
        ("scope_before_source", "source date"),
        ("superseded_count_inflation", "issuer"),
    ],
)
def test_reader_rejects_rehashed_semantic_forgery(
    tmp_path: Path,
    forgery: str,
    error: str,
) -> None:
    evidence, provider_rows = _evidence(tmp_path)
    preview = preview_companyfacts_v3_replay(
        tmp_path,
        store=FakeStore(
            [evidence],
            current={"issuer-1": [_current_row(provider_rows[0], evidence, issuer_id="issuer-1")]},
            references={"issuer-1": _reference()},
        ),
        as_of="2026-07-30",
    )
    path = persist_companyfacts_v3_plan(tmp_path, preview)
    envelope = json.loads(path.read_text())
    if forgery == "unknown_policy_override":
        envelope["payload"]["policy_override"] = {"parser": "force-v4"}
    elif forgery == "missing_forbidden_actions":
        del envelope["payload"]["activation_contract"]["forbidden_actions"]
    elif forgery == "inconsistent_summary":
        envelope["payload"]["summary"]["eligible_issuers"] = 99
    elif forgery == "scope_before_source":
        envelope["payload"]["scope"]["as_of"] = "2026-07-29"
    else:
        envelope["payload"]["issuers"][0]["superseded_source_observations"] = 999
    canonical_payload = json.dumps(
        envelope["payload"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    envelope["payload_sha256"] = hashlib.sha256(canonical_payload).hexdigest()
    path.write_text(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=error):
        read_companyfacts_v3_plan(path)


def test_reader_rejects_rehashed_duplicate_excluded_evidence(
    tmp_path: Path,
) -> None:
    unscoped, _provider_rows = _evidence(
        tmp_path,
        issuer_id=None,
        snapshot_id="raw-11",
        run_id="run-unscoped-11",
    )
    preview = preview_companyfacts_v3_replay(
        tmp_path,
        store=FakeStore([unscoped]),
        as_of="2026-07-30",
    )
    path = persist_companyfacts_v3_plan(tmp_path, preview)
    envelope = json.loads(path.read_text())
    duplicate = json.loads(json.dumps(envelope["payload"]["excluded_evidence"][0]))
    envelope["payload"]["excluded_evidence"].append(duplicate)
    envelope["payload"]["summary"]["excluded_source_observations"] = 2
    _write_rehashed_envelope(path, envelope)

    with pytest.raises(ValueError, match="identity is duplicated"):
        read_companyfacts_v3_plan(path)


def test_reader_rejects_rehashed_noncanonical_excluded_order(
    tmp_path: Path,
) -> None:
    first, _first_rows = _evidence(
        tmp_path,
        issuer_id=None,
        snapshot_id="raw-12",
        run_id="run-unscoped-12",
    )
    second, _second_rows = _evidence(
        tmp_path,
        issuer_id=None,
        snapshot_id="raw-13",
        run_id="run-unscoped-13",
    )
    preview = preview_companyfacts_v3_replay(
        tmp_path,
        store=FakeStore([second, first]),
        as_of="2026-07-30",
    )
    path = persist_companyfacts_v3_plan(tmp_path, preview)
    envelope = json.loads(path.read_text())
    envelope["payload"]["excluded_evidence"].reverse()
    _write_rehashed_envelope(path, envelope)

    with pytest.raises(ValueError, match="not canonically sorted"):
        read_companyfacts_v3_plan(path)


def test_preview_bounds_gzip_output_before_replay(tmp_path: Path) -> None:
    evidence, provider_rows = _evidence(tmp_path)
    expanded = b"x" * (1024 * 1024)
    compressed = gzip.compress(expanded, mtime=0)
    raw_path = tmp_path / evidence["relative_path"]
    raw_path.write_bytes(compressed)
    evidence["stored_bytes"] = len(compressed)
    evidence["original_bytes"] = 32
    evidence["payload_sha256"] = hashlib.sha256(expanded).hexdigest()
    store = FakeStore(
        [evidence],
        current={"issuer-1": [_current_row(provider_rows[0], evidence, issuer_id="issuer-1")]},
        references={"issuer-1": _reference()},
    )

    preview = preview_companyfacts_v3_replay(
        tmp_path,
        store=store,
        as_of="2026-07-30",
    )

    assert preview.eligible_issuers == 0
    assert preview.plan["issuers"][0]["reasons"] == ["raw_payload_mismatch"]


def test_preview_rejects_oversized_original_metadata_before_decompression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence, provider_rows = _evidence(tmp_path)
    evidence["original_bytes"] = replay_module.MAX_COMPANYFACTS_SNAPSHOT_ORIGINAL_BYTES + 1

    def unexpected_decompression(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("oversized evidence reached the decompressor")

    monkeypatch.setattr(replay_module.gzip, "GzipFile", unexpected_decompression)
    store = FakeStore(
        [evidence],
        current={"issuer-1": [_current_row(provider_rows[0], evidence, issuer_id="issuer-1")]},
        references={"issuer-1": _reference()},
    )

    preview = preview_companyfacts_v3_replay(
        tmp_path,
        store=store,
        as_of="2026-07-30",
    )

    assert preview.eligible_issuers == 0
    assert preview.plan["issuers"][0]["reasons"] == ["raw_payload_size_invalid"]


def test_plan_persistence_rejects_symlinked_report_namespace(tmp_path: Path) -> None:
    evidence, provider_rows = _evidence(tmp_path)
    preview = preview_companyfacts_v3_replay(
        tmp_path,
        store=FakeStore(
            [evidence],
            current={"issuer-1": [_current_row(provider_rows[0], evidence, issuer_id="issuer-1")]},
            references={"issuer-1": _reference()},
        ),
        as_of="2026-07-30",
    )
    unsafe_root = tmp_path / "unsafe-project"
    outside = tmp_path / "outside"
    (unsafe_root / "data").mkdir(parents=True)
    outside.mkdir()
    (unsafe_root / "data" / "reports").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes|symbolic"):
        persist_companyfacts_v3_plan(unsafe_root, preview)
    assert list(outside.rglob("*")) == []


def _write_rehashed_envelope(path: Path, envelope: dict[str, Any]) -> None:
    canonical_payload = json.dumps(
        envelope["payload"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    envelope["payload_sha256"] = hashlib.sha256(canonical_payload).hexdigest()
    path.write_text(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _storage_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row["period_end"]),
        str(row["as_of_date"]),
        str(row["metric"]),
    )

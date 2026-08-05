from __future__ import annotations

import ast
import hashlib
import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from importlib.resources import files
from pathlib import Path

import pytest

from aios.hosted_export import (
    HOSTED_RESEARCH_DOCUMENT_KIND,
    HOSTED_RESEARCH_SCHEMA_VERSION,
    MAX_HOSTED_SECURITIES,
    MAX_MISSING_INPUTS_PER_SECURITY,
    build_hosted_readiness_evidence,
    build_hosted_research_snapshot,
    validate_hosted_research_snapshot,
    validate_hosted_research_snapshot_for_serving,
)

DECISION_CLOSE = date(2026, 7, 29)
CREATED_AT = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
SERVING_EXPIRES_AT = datetime(2026, 7, 31, 13, 30, tzinfo=UTC)
FACTOR_POLICY_SHA256 = "a" * 64
SOURCE_POLICY_SHA256 = "b" * 64


READINESS_CHECKS = (
    "decision_date",
    "data_integrity",
    "universe_membership",
    "stable_security_identity",
    "fundamental_coverage",
    "price_history_coverage",
    "reviewed_price_freshness",
    "benchmark_freshness",
    "macro_pit_readiness",
)


def _readiness_report() -> dict:
    return {
        "as_of": DECISION_CLOSE.isoformat(),
        "purpose": "historical_research",
        "generated_on": "2026-07-30",
        "universe_id": "sp500",
        "benchmark_ticker": "SPY",
        "certified_research_from": "2023-08-01",
        "certified_research_through": DECISION_CLOSE.isoformat(),
        "raw_prices_through": DECISION_CLOSE.isoformat(),
        "fundamentals_through": "2026-07-28",
        "macro_releases_through": DECISION_CLOSE.isoformat(),
        "ready": True,
        "checks": [
            {
                "check": check,
                "label": check.replace("_", " ").title(),
                "status": "pass",
                "observed": "Reviewed evidence is present.",
                "required": "The v1 readiness gate must pass.",
                "detail": "Certified evidence supports the decision close.",
            }
            for check in READINESS_CHECKS
        ],
    }


def _readiness() -> dict:
    return build_hosted_readiness_evidence(_readiness_report())


def _memberships() -> list[dict]:
    return [
        {"security_id": "aios:security:aaa", "ticker": "AAA"},
        {"security_id": "aios:security:bbb", "ticker": "BBB"},
    ]


def _security_rows() -> list[dict]:
    return [
        {
            "security_id": "aios:security:aaa",
            "ticker": "AAA",
            "company_name": "Alpha Holdings, Inc.",
            "rank": 1,
            "grade": "A",
            "qv_score": 78.25,
            "quality_score": 85.5,
            "value_score": 67.375,
            "missing_inputs": ["q:ttm_capex", "v:shares_out"],
        },
        {
            "security_id": "aios:security:bbb",
            "ticker": "BBB",
            "company_name": None,
            "rank": None,
            "grade": "N/A",
            "qv_score": None,
            "quality_score": 52.0,
            "value_score": None,
            "missing_inputs": ["v:minimum_multiples:3"],
        },
    ]


def _build(**overrides):
    arguments = {
        "certified_decision_close": DECISION_CLOSE,
        "readiness_evidence": _readiness(),
        "universe_id": "sp500",
        "security_rows": _security_rows(),
        "factor_policy_sha256": FACTOR_POLICY_SHA256,
        "source_policy_sha256": SOURCE_POLICY_SHA256,
        "created_at": CREATED_AT,
        "serving_expires_at": SERVING_EXPIRES_AT,
        "memberships": _memberships(),
    }
    arguments.update(overrides)
    return build_hosted_research_snapshot(**arguments)


def _sha256_json(value) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _externally_rehash(document: dict, *, membership_changed: bool = False) -> dict:
    artifact = deepcopy(document)
    payload = artifact["payload"]
    if membership_changed:
        membership_basis = {
            "certified_decision_close": payload["certified_decision_close"],
            "universe_id": payload["universe_id"],
            "members": [
                {
                    "security_id": row["security_id"],
                    "ticker": row["ticker"],
                }
                for row in payload["securities"]
            ],
        }
        payload["universe_membership_sha256"] = _sha256_json(membership_basis)
    artifact["payload_sha256"] = _sha256_json(payload)
    artifact["snapshot_id"] = f"hrs-v1-{artifact['payload_sha256']}"
    return artifact


def test_explicit_export_is_deterministic_unsigned_and_allowlisted() -> None:
    first = _build()
    reordered_rows = list(reversed(_security_rows()))
    reordered_rows[1]["missing_inputs"] = list(reversed(reordered_rows[1]["missing_inputs"]))
    repeated = _build(
        memberships=list(reversed(_memberships())),
        security_rows=reordered_rows,
        readiness_evidence=dict(reversed(list(_readiness().items()))),
    )

    assert first.canonical_bytes() == repeated.canonical_bytes()
    document = first.to_dict()
    assert set(document) == {
        "document_kind",
        "schema_version",
        "unsigned",
        "canonicalization",
        "hosted_promotion_allowed",
        "promotion_blocker",
        "snapshot_id",
        "payload_sha256",
        "payload",
    }
    assert document["document_kind"] == HOSTED_RESEARCH_DOCUMENT_KIND
    assert document["schema_version"] == HOSTED_RESEARCH_SCHEMA_VERSION
    assert document["unsigned"] is True
    assert document["canonicalization"] == "python-json-v1-local-only"
    assert document["hosted_promotion_allowed"] is False
    assert document["promotion_blocker"] == (
        "interoperable-canonicalization-and-signature-required"
    )
    assert document["snapshot_id"] == f"hrs-v1-{document['payload_sha256']}"
    assert "tenant" not in first.canonical_json().lower()
    assert set(document["payload"]) == {
        "certified_decision_close",
        "readiness_sha256",
        "universe_id",
        "universe_membership_sha256",
        "factor_policy_sha256",
        "source_policy_sha256",
        "created_at",
        "serving_expires_at",
        "coverage",
        "securities",
    }
    assert document["payload"]["coverage"] == {
        "total": 2,
        "scored": 1,
        "withheld": 1,
    }
    assert [row["security_id"] for row in document["payload"]["securities"]] == [
        "aios:security:aaa",
        "aios:security:bbb",
    ]
    assert set(document["payload"]["securities"][0]) == {
        "security_id",
        "ticker",
        "company_name",
        "rank",
        "grade",
        "qv_score",
        "quality_score",
        "value_score",
        "missing_inputs",
    }


def test_export_uses_only_the_read_only_membership_store_surface() -> None:
    class FakeStore:
        read_only = True

        def __init__(self) -> None:
            self.calls: list[tuple[str, date | str]] = []

        def universe_membership_on(self, universe_id, as_of):
            self.calls.append((universe_id, as_of))
            return [
                {
                    **row,
                    "known_date": DECISION_CLOSE,
                    "source": "https://example.test/reviewed-membership",
                }
                for row in _memberships()
            ]

        def mutate(self):
            raise AssertionError("the hosted exporter must never call a writer")

    store = FakeStore()
    snapshot = _build(memberships=None, store=store)

    assert snapshot.to_dict()["payload"]["coverage"]["total"] == 2
    assert store.calls == [("sp500", DECISION_CLOSE)]


def test_export_refuses_a_writable_store_before_querying_it() -> None:
    class WritableStore:
        read_only = False

        def universe_membership_on(self, *_args):
            raise AssertionError("a writable Store must not be queried")

    with pytest.raises(ValueError, match="opened read-only"):
        _build(memberships=None, store=WritableStore())


@pytest.mark.parametrize(
    ("memberships", "rows", "message"),
    [
        (
            [
                {"security_id": "aios:security:aaa", "ticker": "AAA"},
                {"security_id": "aios:security:aaa", "ticker": "BBB"},
            ],
            _security_rows(),
            "duplicate membership security_id",
        ),
        (
            [{"ticker": "AAA"}],
            _security_rows(),
            "fields are invalid",
        ),
        (
            _memberships(),
            [_security_rows()[0], deepcopy(_security_rows()[0])],
            "duplicate security row security_id",
        ),
        (
            _memberships(),
            [_security_rows()[0]],
            "do not match certified membership",
        ),
        (
            _memberships(),
            [
                _security_rows()[0],
                {**_security_rows()[1], "ticker": "WRONG"},
            ],
            "do not match certified membership",
        ),
    ],
)
def test_export_rejects_missing_duplicate_or_mismatched_security_identity(
    memberships,
    rows,
    message,
) -> None:
    with pytest.raises(ValueError, match=message):
        _build(memberships=memberships, security_rows=rows)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["qv_score", "quality_score", "value_score"])
def test_export_rejects_non_finite_scores(field: str, value: float) -> None:
    rows = _security_rows()
    rows[0][field] = value

    with pytest.raises(ValueError, match="finite"):
        _build(security_rows=rows)


def test_export_rejects_non_allowlisted_fields_paths_and_secrets() -> None:
    rows_with_extra = _security_rows()
    rows_with_extra[0]["account_path"] = "data/paper/account.json"
    with pytest.raises(ValueError, match="extra=.*account_path"):
        _build(security_rows=rows_with_extra)

    rows_with_path = _security_rows()
    rows_with_path[0]["company_name"] = "/home/operator/private.json"
    with pytest.raises(ValueError, match="filesystem path"):
        _build(security_rows=rows_with_path)

    rows_with_secret = _security_rows()
    rows_with_secret[0]["company_name"] = "api_key=super-secret-value"
    with pytest.raises(ValueError, match="secret-shaped"):
        _build(security_rows=rows_with_secret)

    readiness_with_secret = _readiness()
    readiness_with_secret["api_key"] = "secret"
    with pytest.raises(ValueError, match="forbidden field"):
        _build(readiness_evidence=readiness_with_secret)


def test_withheld_and_rank_contracts_fail_closed() -> None:
    rows = _security_rows()
    rows[1]["missing_inputs"] = []
    with pytest.raises(ValueError, match="needs an evidence-gap code"):
        _build(security_rows=rows)

    rows = _security_rows()
    rows[0]["rank"] = 2
    with pytest.raises(ValueError, match="contiguous"):
        _build(security_rows=rows)

    rows = _security_rows()
    rows[0]["quality_score"] = None
    with pytest.raises(ValueError, match="needs Quality and Value"):
        _build(security_rows=rows)


def test_readiness_and_explicit_time_boundaries_are_required() -> None:
    blocked = _readiness()
    blocked["checks"][0]["status"] = "fail"
    blocked["ready"] = False
    with pytest.raises(ValueError, match="requires ready evidence"):
        _build(readiness_evidence=blocked)

    mismatched = _readiness()
    mismatched["as_of"] = "2026-07-28"
    with pytest.raises(ValueError, match="does not match certified decision close"):
        _build(readiness_evidence=mismatched)

    with pytest.raises(ValueError, match="include a UTC offset"):
        _build(created_at=datetime(2026, 7, 30, 9, 0))

    with pytest.raises(ValueError, match="later than created_at"):
        _build(serving_expires_at=CREATED_AT)

    with pytest.raises(ValueError, match="TTL cannot exceed 7 days"):
        _build(serving_expires_at=CREATED_AT + timedelta(days=7, seconds=1))


def test_readiness_requires_the_exact_versioned_certification_contract() -> None:
    minimal = {
        "ready": True,
        "as_of": DECISION_CLOSE.isoformat(),
        "checks": [],
    }
    with pytest.raises(ValueError, match="fields are invalid"):
        _build(readiness_evidence=minimal)

    wrong_purpose = _readiness()
    wrong_purpose["purpose"] = "paper"
    with pytest.raises(ValueError, match="requires historical_research"):
        _build(readiness_evidence=wrong_purpose)

    wrong_benchmark = _readiness()
    wrong_benchmark["benchmark_ticker"] = "QQQ"
    with pytest.raises(ValueError, match="requires the SPY benchmark"):
        _build(readiness_evidence=wrong_benchmark)

    with pytest.raises(ValueError, match="only the sp500 universe"):
        _build(universe_id="nasdaq100")

    incomplete_certification = _readiness()
    incomplete_certification["certified_research_through"] = "2026-07-28"
    with pytest.raises(ValueError, match="exact decision close"):
        _build(readiness_evidence=incomplete_certification)

    missing_check = _readiness()
    missing_check["checks"] = missing_check["checks"][:-1]
    with pytest.raises(ValueError, match="exactly 9 checks"):
        _build(readiness_evidence=missing_check)

    inconsistent = _readiness()
    inconsistent["checks"][0]["status"] = "fail"
    with pytest.raises(ValueError, match="does not reconcile"):
        _build(readiness_evidence=inconsistent)


def test_validator_rejects_payload_tampering_and_new_output_fields() -> None:
    original = _build().to_dict()
    tampered = deepcopy(original)
    tampered["payload"]["securities"][0]["qv_score"] = 12.0
    with pytest.raises(ValueError, match="payload checksum mismatch"):
        validate_hosted_research_snapshot(tampered)

    extra = deepcopy(original)
    extra["payload"]["tenant_id"] = "tenant-a"
    with pytest.raises(ValueError, match="extra=.*tenant_id"):
        validate_hosted_research_snapshot(extra)

    membership_tamper = deepcopy(original)
    membership_tamper["payload"]["universe_membership_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="membership checksum mismatch"):
        validate_hosted_research_snapshot(membership_tamper)


def test_validator_rejects_externally_rehashed_duplicate_ticker() -> None:
    external = _build().to_dict()
    external["payload"]["securities"][1]["ticker"] = "AAA"
    external = _externally_rehash(external, membership_changed=True)

    with pytest.raises(ValueError, match="duplicate security row ticker"):
        validate_hosted_research_snapshot(external)


@pytest.mark.parametrize("noncanonical_score", [78, -0.0])
def test_validator_rejects_externally_rehashed_noncanonical_score_spelling(
    noncanonical_score,
) -> None:
    external = _build().to_dict()
    external["payload"]["securities"][0]["qv_score"] = noncanonical_score
    external = _externally_rehash(external)

    with pytest.raises(ValueError, match="canonical builder form"):
        validate_hosted_research_snapshot(external)


def test_v1_collection_and_byte_bounds_fail_closed() -> None:
    too_many_memberships = [
        {
            "security_id": f"aios:security:s{index:03d}",
            "ticker": f"S{index:03d}",
        }
        for index in range(MAX_HOSTED_SECURITIES + 1)
    ]
    with pytest.raises(ValueError, match="cannot exceed 600"):
        _build(memberships=too_many_memberships)

    too_many_missing = _security_rows()
    too_many_missing[0]["missing_inputs"] = [
        f"gap:{index:02d}" for index in range(MAX_MISSING_INPUTS_PER_SECURITY + 1)
    ]
    with pytest.raises(ValueError, match="more than 32"):
        _build(security_rows=too_many_missing)

    oversized_readiness = _readiness()
    for check in oversized_readiness["checks"]:
        check["label"] = "L" * 128
        check["observed"] = "O" * 1_024
        check["required"] = "R" * 1_024
        check["detail"] = "D" * 2_048
    with pytest.raises(ValueError, match="32 KiB"):
        _build(readiness_evidence=oversized_readiness)

    missing_codes = [
        f"gap:{index:02d}:" + ("x" * 190) for index in range(MAX_MISSING_INPUTS_PER_SECURITY)
    ]
    memberships = [
        {
            "security_id": f"aios:security:s{index:03d}",
            "ticker": f"S{index:03d}",
        }
        for index in range(MAX_HOSTED_SECURITIES)
    ]
    rows = [
        {
            **membership,
            "company_name": "C" * 256,
            "rank": None,
            "grade": "N/A",
            "qv_score": None,
            "quality_score": None,
            "value_score": None,
            "missing_inputs": missing_codes,
        }
        for membership in memberships
    ]
    with pytest.raises(ValueError, match="2 MiB"):
        _build(memberships=memberships, security_rows=rows)


def test_serving_validation_requires_an_explicit_in_window_instant() -> None:
    artifact = _build().to_dict()

    assert (
        validate_hosted_research_snapshot_for_serving(
            artifact,
            now=CREATED_AT,
        ).snapshot_id
        == artifact["snapshot_id"]
    )
    with pytest.raises(ValueError, match="not yet valid"):
        validate_hosted_research_snapshot_for_serving(
            artifact,
            now=CREATED_AT - timedelta(microseconds=1),
        )
    with pytest.raises(ValueError, match="has expired"):
        validate_hosted_research_snapshot_for_serving(
            artifact,
            now=SERVING_EXPIRES_AT,
        )


def test_schema_is_strict_and_tracks_the_runtime_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    resource = files("aios").joinpath("schemas/hosted-research-snapshot.v1.json")
    schema = json.loads(resource.read_text(encoding="utf-8"))

    assert schema["additionalProperties"] is False
    assert schema["properties"]["document_kind"]["const"] == (HOSTED_RESEARCH_DOCUMENT_KIND)
    assert schema["properties"]["schema_version"]["const"] == (HOSTED_RESEARCH_SCHEMA_VERSION)
    assert schema["properties"]["canonicalization"]["const"] == "python-json-v1-local-only"
    assert schema["properties"]["hosted_promotion_allowed"]["const"] is False
    assert schema["properties"]["payload"]["additionalProperties"] is False
    assert schema["properties"]["payload"]["properties"]["securities"]["maxItems"] == 600
    assert schema["$defs"]["security"]["additionalProperties"] is False
    assert schema["$defs"]["security"]["properties"]["missing_inputs"]["maxItems"] == 32
    assert set(schema["$defs"]["security"]["required"]) == set(
        _build().to_dict()["payload"]["securities"][0]
    )
    assert not (root / "schemas/hosted-research-snapshot.v1.json").exists()


def test_hosted_export_import_boundary_excludes_mutation_and_runtime_modules() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "src/aios/hosted_export.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)

    forbidden = {
        "aios.alerts",
        "aios.cli",
        "aios.dashboard",
        "aios.forward",
        "aios.forward_rollover",
        "aios.operations",
        "aios.paper",
        "aios.scheduler",
        "aios.storage.store",
    }
    assert imports.isdisjoint(forbidden)

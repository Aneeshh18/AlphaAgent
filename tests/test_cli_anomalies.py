from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from aios import alerts as alerts_module
from aios import anomalies as anomalies_module
from aios import cli
from aios import maintenance as maintenance_module
from aios.alerts import (
    AnomalyCase,
    AnomalyObservation,
    AnomalyScan,
    canonical_anomaly_fingerprint,
)


def _observation() -> AnomalyObservation:
    rule_id = "sec_fundamentals_coverage_missing"
    rule_version = "1.0.0"
    scope = "us-equity-reference:sp500"
    subject_type = "issuer"
    subject_id = "aios:issuer:sec:0000000001"
    return AnomalyObservation(
        fingerprint=canonical_anomaly_fingerprint(
            rule_id=rule_id,
            rule_version=rule_version,
            scope=scope,
            subject_type=subject_type,
            subject_id=subject_id,
        ),
        rule_id=rule_id,
        rule_version=rule_version,
        scope=scope,
        subject_type=subject_type,
        subject_id=subject_id,
        severity="medium",
        confidence="medium",
        title="Reviewed issuer has no accepted SEC fundamentals",
        summary="The reviewed issuer maps to exact zero-row SEC evidence.",
        old_value={"minimum_accepted_rows": 1},
        new_value={"accepted_rows": 0},
        evidence={
            "issuer": {"canonical_ticker": "TEST"},
            "snapshot": {"sha256": "a" * 64},
        },
        suggested_checks=("Review the checksum-bound SEC source snapshot.",),
    )


def _scan() -> AnomalyScan:
    return AnomalyScan(
        scan_id="scan-test",
        rule_bundle_version="us-equity-data-quality.v1",
        scope="us-equity-reference:sp500",
        source_boundary_sha256="b" * 64,
        source_boundary_at="2026-07-29T08:00:00Z",
        executed_rules=("sec_fundamentals_coverage_missing@1.0.0",),
        observations=(_observation(),),
        evidence={
            "reviewed_members": 503,
            "reviewed_issuers": 500,
            "covered_issuers": 497,
            "missing_issuers": 3,
            "coverage_rate": 497 / 500,
        },
    )


def _case(**changes: object) -> AnomalyCase:
    case = AnomalyCase(
        case_id="case-1234567890",
        fingerprint="f" * 64,
        rule_id="sec_fundamentals_coverage_missing",
        rule_version="1.0.0",
        scope="us-equity-reference:sp500",
        subject_type="issuer",
        subject_id="aios:issuer:sec:0000000001",
        severity="medium",
        confidence="medium",
        title="Reviewed issuer has no accepted SEC fundamentals",
        summary="The reviewed issuer maps to exact zero-row SEC evidence.",
        state="open",
        owner=None,
        first_seen_at="2026-07-29T08:00:00Z",
        last_seen_at="2026-07-29T08:00:00Z",
        occurrence_count=1,
        old_value={"minimum_accepted_rows": 1},
        new_value={"accepted_rows": 0},
        evidence={"snapshot": {"sha256": "a" * 64}},
        suggested_checks=("Review the checksum-bound SEC source snapshot.",),
        evidence_sha256="c" * 64,
        last_scan_id="scan-test",
        acknowledged_at=None,
        resolution_outcome=None,
        resolution_note=None,
        resolved_at=None,
        next_review_at=None,
        verification_scan_id=None,
    )
    return replace(case, **changes)


def _settings(tmp_path):
    return SimpleNamespace(
        project_root=tmp_path,
        duckdb_path=tmp_path / "data" / "aios.duckdb",
        operations_db_path=tmp_path / "data" / "operations" / "alerts.sqlite3",
    )


@pytest.fixture
def isolated_scan(monkeypatch, tmp_path):
    research_store = object()
    scopes: list[dict[str, object]] = []
    detected: list[dict[str, object]] = []

    def store_scope(**kwargs):
        scopes.append(kwargs)
        return nullcontext(research_store)

    def detect(**kwargs):
        detected.append(kwargs)
        return _scan()

    monkeypatch.setattr(cli, "settings", _settings(tmp_path))
    monkeypatch.setattr(cli, "store_scope", store_scope)
    monkeypatch.setattr(anomalies_module, "scan_sec_fundamental_coverage", detect)
    monkeypatch.setattr(
        maintenance_module,
        "project_maintenance_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    return research_store, scopes, detected


def _assert_no_action_shortcut(output: str) -> None:
    lowered = output.lower()
    assert "aios paper-execute" not in lowered
    assert "--confirm-simulated" not in lowered
    assert "repair-data" not in lowered
    assert "place broker order" not in lowered
    assert "send broker order" not in lowered


def test_anomaly_scan_defaults_to_certified_close_and_preview_is_non_mutating(
    isolated_scan,
    monkeypatch,
    tmp_path,
) -> None:
    research_store, scopes, detected = isolated_scan
    monkeypatch.setattr(
        cli,
        "assess_us_readiness",
        lambda **kwargs: SimpleNamespace(certified_research_through="2026-07-27"),
    )
    monkeypatch.setattr(
        alerts_module,
        "get_alert_store",
        lambda *_args, **_kwargs: pytest.fail(
            "preview must not open or mutate the operations ledger"
        ),
    )
    monkeypatch.setattr(
        maintenance_module,
        "project_maintenance_lock",
        lambda *_args, **_kwargs: pytest.fail(
            "preview must not acquire the mutation lease"
        ),
    )

    result = CliRunner().invoke(cli.app, ["anomaly-scan", "--preview", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "preview"
    assert payload["recorded"] is False
    assert payload["cases"] == []
    assert payload["safety"] == {
        "research_data_changed": False,
        "readiness_overridden": False,
        "paper_state_changed": False,
        "broker_action": False,
    }
    assert scopes == [{"read_only": True}]
    assert detected == [
        {
            "store": research_store,
            "as_of": cli.date(2026, 7, 27),
            "project_root": tmp_path,
        }
    ]
    assert not (tmp_path / "data" / "operations" / "alerts.sqlite3").exists()
    assert not (tmp_path / "data" / "operations" / "maintenance.lock").exists()
    _assert_no_action_shortcut(result.output)


def test_anomaly_scan_records_only_with_explicit_record(
    isolated_scan,
    monkeypatch,
) -> None:
    _, scopes, detected = isolated_scan
    operations_store = SimpleNamespace()
    recorded: list[AnomalyScan] = []

    def record(scan):
        recorded.append(scan)
        return (_case(),)

    operations_store.record_anomaly_scan = record
    opened: list[dict[str, object]] = []

    def get_alert_store(*_args, **kwargs):
        opened.append(kwargs)
        return operations_store

    monkeypatch.setattr(alerts_module, "get_alert_store", get_alert_store)

    result = CliRunner().invoke(
        cli.app,
        [
            "anomaly-scan",
            "--as-of",
            "2026-07-28",
            "--record",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "recorded"
    assert payload["recorded"] is True
    assert payload["cases"][0]["case_id"] == "case-1234567890"
    assert recorded == [_scan()]
    assert opened == [{}]
    assert scopes == [{"read_only": True}]
    assert detected[0]["as_of"] == cli.date(2026, 7, 28)
    assert all(value is False for value in payload["safety"].values())
    _assert_no_action_shortcut(result.output)


def test_anomaly_scan_json_failure_is_structured_and_fail_closed(
    isolated_scan,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        anomalies_module,
        "scan_sec_fundamental_coverage",
        lambda **_kwargs: (_ for _ in ()).throw(
            ValueError("exact SEC evidence is unavailable")
        ),
    )
    monkeypatch.setattr(
        alerts_module,
        "get_alert_store",
        lambda *_args, **_kwargs: pytest.fail(
            "a failed scan must not open the operations ledger"
        ),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "anomaly-scan",
            "--as-of",
            "2026-07-28",
            "--record",
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "schema_version": "data-quality-anomaly-scan.v1",
        "status": "withheld",
        "recorded": False,
        "error": "exact SEC evidence is unavailable",
    }
    _assert_no_action_shortcut(result.output)


def test_anomaly_list_and_show_open_the_operations_ledger_read_only(
    monkeypatch,
) -> None:
    case = _case()

    class _ReadOnlyOperations:
        def anomaly_cases(self, **kwargs):
            assert kwargs == {"unresolved_only": True, "limit": 25}
            return [case]

        def anomaly_summary(self):
            return {"open": 1, "unresolved": 1, "resolved": 0, "total": 1}

        def anomaly_case(self, case_id):
            assert case_id == "case-1234"
            return case

        def anomaly_case_events(self, case_id):
            assert case_id == "case-1234"
            return [
                {
                    "event_id": "event-1",
                    "event_type": "opened",
                    "created_at": "2026-07-29T08:00:00Z",
                    "owner": None,
                    "note": None,
                }
            ]

    opened: list[dict[str, object]] = []
    store = _ReadOnlyOperations()

    def get_alert_store(*_args, **kwargs):
        opened.append(kwargs)
        return store

    monkeypatch.setattr(alerts_module, "get_alert_store", get_alert_store)

    listed = CliRunner().invoke(
        cli.app,
        ["anomalies", "--unresolved", "--limit", "25", "--json"],
    )
    shown = CliRunner().invoke(
        cli.app,
        ["anomaly-show", "case-1234", "--json"],
    )

    assert listed.exit_code == 0
    assert shown.exit_code == 0
    assert json.loads(listed.output)["cases"][0]["case_id"] == case.case_id
    assert json.loads(shown.output)["case"]["case_id"] == case.case_id
    assert opened == [{"read_only": True}, {"read_only": True}]
    _assert_no_action_shortcut(listed.output)
    _assert_no_action_shortcut(shown.output)


@pytest.mark.parametrize(
    "arguments,missing_option",
    [
        (["anomaly-ack", "case-1234", "--note", "Reviewed evidence."], "--owner"),
        (["anomaly-ack", "case-1234", "--owner", "Aneesh"], "--note"),
        (
            [
                "anomaly-resolve",
                "case-1234",
                "--outcome",
                "accepted",
                "--owner",
                "Aneesh",
            ],
            "--note",
        ),
    ],
)
def test_anomaly_mutations_require_owner_and_audit_note_before_opening_store(
    monkeypatch,
    arguments,
    missing_option,
) -> None:
    monkeypatch.setattr(
        alerts_module,
        "get_alert_store",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid CLI input must fail before opening the operations ledger"
        ),
    )

    result = CliRunner().invoke(cli.app, arguments)

    assert result.exit_code == 2
    assert missing_option in result.output
    _assert_no_action_shortcut(result.output)


def test_anomaly_ack_forwards_required_owner_and_note(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class _Operations:
        def acknowledge_anomaly(self, case_id, **kwargs):
            calls.append((case_id, kwargs))
            return _case(state="acknowledged", owner=kwargs["owner"])

    monkeypatch.setattr(
        alerts_module,
        "get_alert_store",
        lambda *_args, **_kwargs: _Operations(),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "anomaly-ack",
            "case-1234",
            "--owner",
            "Aneesh",
            "--note",
            "Checksum-bound SEC evidence reviewed.",
            "--evidence-sha256",
            "c" * 64,
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        (
            "case-1234",
            {
                "owner": "Aneesh",
                "note": "Checksum-bound SEC evidence reviewed.",
                "expected_evidence_sha256": "c" * 64,
            },
        )
    ]
    _assert_no_action_shortcut(result.output)


def test_anomaly_acceptance_check_is_read_only_and_emits_contract(
    monkeypatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class _Operations:
        def review_anomaly_acceptance(self, case_id, **kwargs):
            calls.append((case_id, kwargs))
            return {
                "contract_id": "sec-pre-periodic-issuer.v1",
                "case_evidence_sha256": kwargs["expected_evidence_sha256"],
                "analytical_effect": {
                    "coverage_changed": False,
                    "readiness_changed": False,
                    "score_created": False,
                    "facts_transferred": False,
                },
            }

    def get_store(*_args, **kwargs):
        calls.append(("read_only", kwargs.get("read_only")))
        return _Operations()

    monkeypatch.setattr(alerts_module, "get_alert_store", get_store)
    result = CliRunner().invoke(
        cli.app,
        [
            "anomaly-acceptance-check",
            "case-1234",
            "--evidence-sha256",
            "c" * 64,
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        ("read_only", True),
        (
            "case-1234",
            {"expected_evidence_sha256": "c" * 64},
        ),
    ]
    assert json.loads(result.output)["contract_id"] == (
        "sec-pre-periodic-issuer.v1"
    )


def test_anomaly_deferred_resolution_forwards_future_review_date(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class _Operations:
        def resolve_anomaly(self, case_id, **kwargs):
            calls.append((case_id, kwargs))
            return _case(
                state="deferred",
                owner=kwargs["owner"],
                resolution_outcome=kwargs["outcome"],
                next_review_at=str(kwargs["next_review_at"]),
            )

    monkeypatch.setattr(
        alerts_module,
        "get_alert_store",
        lambda *_args, **_kwargs: _Operations(),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "anomaly-resolve",
            "case-1234",
            "--outcome",
            "deferred",
            "--owner",
            "Aneesh",
            "--note",
            "Recheck after the next reviewed SEC ingest.",
            "--next-review",
            "2099-08-15",
            "--evidence-sha256",
            "c" * 64,
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        (
            "case-1234",
            {
                "outcome": "deferred",
                "note": "Recheck after the next reviewed SEC ingest.",
                "owner": "Aneesh",
                "expected_evidence_sha256": "c" * 64,
                "next_review_at": "2099-08-15T00:00:00Z",
                "verification_scan_id": None,
            },
        )
    ]
    assert "deferred" in result.output.lower()
    _assert_no_action_shortcut(result.output)


def test_anomaly_correction_forwards_verification_scan(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class _Operations:
        def resolve_anomaly(self, case_id, **kwargs):
            calls.append((case_id, kwargs))
            return _case(
                state="resolved",
                owner=kwargs["owner"],
                resolution_outcome=kwargs["outcome"],
                resolution_note=kwargs["note"],
                verification_scan_id=kwargs["verification_scan_id"],
            )

    monkeypatch.setattr(
        alerts_module,
        "get_alert_store",
        lambda *_args, **_kwargs: _Operations(),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "anomaly-resolve",
            "case-1234",
            "--outcome",
            "source_corrected",
            "--owner",
            "Aneesh",
            "--note",
            "A later complete scan proves the finding is absent.",
            "--verification-scan",
            "scan-proof",
            "--evidence-sha256",
            "c" * 64,
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        (
            "case-1234",
            {
                "outcome": "source_corrected",
                "note": "A later complete scan proves the finding is absent.",
                "owner": "Aneesh",
                "expected_evidence_sha256": "c" * 64,
                "next_review_at": None,
                "verification_scan_id": "scan-proof",
            },
        )
    ]
    assert "readiness gates were not changed" in result.output
    _assert_no_action_shortcut(result.output)


# ----------------------------------------------------------------------
# Multi-rule anomaly-scan (--rule / --all-rules)
# ----------------------------------------------------------------------
def _multi_scan(rule_id: str) -> AnomalyScan:
    return AnomalyScan(
        scan_id=f"scan-{rule_id}",
        rule_bundle_version="us-equity-data-quality.v1",
        scope=f"us-equity-test:{rule_id}",
        source_boundary_sha256="d" * 64,
        source_boundary_at="2026-08-07T08:00:00Z",
        executed_rules=(f"{rule_id}@1.0.0",),
        observations=(),
        evidence={"rule_id": rule_id},
    )


def test_anomaly_scan_rejects_an_unknown_rule(isolated_scan) -> None:
    result = CliRunner().invoke(
        cli.app, ["anomaly-scan", "--rule", "not_a_real_rule", "--preview"]
    )
    assert result.exit_code == 1
    assert "unknown anomaly rule" in result.output.lower()


def test_anomaly_scan_multi_rule_preview_runs_selected_rules(
    isolated_scan, monkeypatch
) -> None:
    _, _, _ = isolated_scan
    calls: list[tuple[str, ...]] = []

    def fake_run_detectors(*, rules, **_kwargs):
        calls.append(rules)
        return (_multi_scan(rules[0]),)

    monkeypatch.setattr(anomalies_module, "run_detectors", fake_run_detectors)
    monkeypatch.setattr(
        alerts_module,
        "get_alert_store",
        lambda *_a, **_k: pytest.fail("preview must not open the operations ledger"),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "anomaly-scan",
            "--as-of",
            "2026-08-07",
            "--rule",
            "price_action_mismatch",
            "--rule",
            "mapping_drift",
            "--preview",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "preview"
    assert payload["recorded"] is False
    assert [scan["rule_id"] for scan in payload["scans"]] == [
        "price_action_mismatch",
        "mapping_drift",
    ]
    assert calls == [("price_action_mismatch",), ("mapping_drift",)]
    _assert_no_action_shortcut(result.output)


def test_anomaly_scan_all_rules_expands_to_the_full_registry(
    isolated_scan, monkeypatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run_detectors(*, rules, **_kwargs):
        calls.append(rules)
        return (_multi_scan(rules[0]),)

    monkeypatch.setattr(anomalies_module, "run_detectors", fake_run_detectors)
    monkeypatch.setattr(
        anomalies_module,
        "measure_universe_factor_percentiles",
        lambda **_k: {"as_of": "2026-08-07", "factor_model": "qv", "scores": {}},
    )
    monkeypatch.setattr(
        anomalies_module, "latest_factor_percentile_baseline", lambda **_k: None
    )

    result = CliRunner().invoke(
        cli.app,
        ["anomaly-scan", "--as-of", "2026-08-07", "--all-rules", "--preview", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert len(payload["scans"]) == len(anomalies_module.registered_rule_ids())
    assert {call[0] for call in calls} == set(anomalies_module.registered_rule_ids())


def test_anomaly_scan_multi_rule_records_and_persists_factor_baseline(
    isolated_scan, monkeypatch
) -> None:
    recorded_scans: list[AnomalyScan] = []
    persisted_snapshots: list[dict] = []
    operations_store = SimpleNamespace()

    def record(scan):
        recorded_scans.append(scan)
        return ()

    operations_store.record_anomaly_scan = record
    operations_store.latest_anomaly_scan_evidence = lambda scope: None
    monkeypatch.setattr(alerts_module, "get_alert_store", lambda: operations_store)

    def fake_run_detectors(*, rules, **_kwargs):
        return (_multi_scan(rules[0]),)

    def fake_measure(*, store, as_of, factor_model):
        return {
            "as_of": "2026-08-07",
            "universe_id": "sp500",
            "factor_model": factor_model,
            "scores": {"AAA": 50.0},
        }

    monkeypatch.setattr(anomalies_module, "run_detectors", fake_run_detectors)
    monkeypatch.setattr(
        anomalies_module, "measure_universe_factor_percentiles", fake_measure
    )
    monkeypatch.setattr(
        anomalies_module, "latest_factor_percentile_baseline", lambda **_k: None
    )
    monkeypatch.setattr(
        anomalies_module,
        "record_factor_percentile_baseline",
        lambda snapshot: persisted_snapshots.append(snapshot),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "anomaly-scan",
            "--as-of",
            "2026-08-07",
            "--rule",
            "factor_percentile_jump",
            "--record",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert len(recorded_scans) == 1
    assert len(persisted_snapshots) == 1
    assert persisted_snapshots[0]["factor_model"] == "qv"
    _assert_no_action_shortcut(result.output)


def test_anomaly_scan_multi_rule_does_not_persist_baseline_on_preview(
    isolated_scan, monkeypatch
) -> None:
    persisted_snapshots: list[dict] = []

    def fake_run_detectors(*, rules, **_kwargs):
        return (_multi_scan(rules[0]),)

    monkeypatch.setattr(anomalies_module, "run_detectors", fake_run_detectors)
    monkeypatch.setattr(
        anomalies_module,
        "measure_universe_factor_percentiles",
        lambda **_k: {"as_of": "2026-08-07", "factor_model": "qv", "scores": {}},
    )
    monkeypatch.setattr(
        anomalies_module, "latest_factor_percentile_baseline", lambda **_k: None
    )
    monkeypatch.setattr(
        anomalies_module,
        "record_factor_percentile_baseline",
        lambda snapshot: persisted_snapshots.append(snapshot),
    )
    monkeypatch.setattr(
        alerts_module,
        "get_alert_store",
        lambda: pytest.fail("preview must not open the operations ledger"),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "anomaly-scan",
            "--as-of",
            "2026-08-07",
            "--rule",
            "factor_percentile_jump",
            "--preview",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert persisted_snapshots == []

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
from math import sqrt
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from aios import cli
from aios.backtest.costs import TaxPolicy, TransactionCostPolicy
from aios.backtest.portfolio import PortfolioBook
from aios.market_calendar import us_equity_sessions
from aios.paper import (
    ACCOUNT_DOCUMENT_KIND,
    PROPOSAL_DOCUMENT_KIND,
    PaperDocument,
    canonical_payload_sha256,
)
from aios.risk import stress as stress_module
from aios.risk.policy import PortfolioRiskPolicy
from aios.risk.stress import (
    ProposalStressInput,
    StressPosition,
    StressReviewReport,
    StressScenarioBundle,
    TargetStressEvidence,
    build_proposal_stress_input,
    build_stress_source_identity,
    collect_target_stress_evidence,
    default_stress_report_path,
    evaluate_proposal_stress,
    load_scenario_bundle,
    read_stress_report,
    require_stress_report_sources_unchanged,
    write_stress_report,
)
from aios.storage.store import Store


def _scenario(
    scenario_id: str,
    *,
    scenario_type: str = "fixed_return",
    assumptions: dict | None = None,
) -> dict:
    if assumptions is None:
        assumptions = {
            "selector": {"kind": "top_weighted", "count": 1},
            "selected_return": -0.5,
            "other_return": -0.2,
            "liquidity_adv_multiplier": 1.0,
            "liquidation_horizon_sessions": 5,
        }
    scenario = {
        "scenario_id": scenario_id,
        "scenario_type": scenario_type,
        "version": "1.0.0",
        "label": scenario_id.replace("_", " "),
        "historical_label": None,
        "description": "Deterministic test sensitivity.",
        "source_posture": "test_policy_sensitivity",
        "assumptions": assumptions,
        "calibration_sources": [
            {
                "title": "Test calibration",
                "reference": "tests/test_stress.py",
                "basis": "Exact arithmetic test fixture; not a forecast.",
            }
        ],
        "recovery_path": (
            []
            if scenario_type == "evidence_withholding_demonstration"
            else [
                {"session_offset": 0, "remaining_shock_fraction": 1.0},
                {"session_offset": 1, "remaining_shock_fraction": 0.0},
            ]
        ),
    }
    scenario["assumption_provenance"] = {key: "test_fixture_assumption" for key in assumptions}
    if scenario_type != "evidence_withholding_demonstration":
        scenario["assumption_provenance"]["recovery_path"] = (
            "test_fixture_assumption_not_used_in_loss_calculation"
        )
    return scenario


def _bundle(*scenarios: dict) -> StressScenarioBundle:
    payload = {
        "stress_scenario_bundle_schema_version": 1,
        "bundle_id": "test-us-equity",
        "bundle_version": "1.0.0",
        "market": "US",
        "currency": "USD",
        "notice": "Deterministic test evidence only.",
        "evidence_policy": {
            "maximum_revenue_fact_known_age_days": 180,
            "maximum_price_staleness_days": 7,
            "minimum_liquidity_observations": 20,
            "minimum_price_observations": 253,
        },
        "scenarios": list(scenarios),
    }
    return StressScenarioBundle(payload, canonical_payload_sha256(payload))


def _stress_input(*, policy: PortfolioRiskPolicy | None = None) -> ProposalStressInput:
    return ProposalStressInput(
        account_id="account",
        account_payload_sha256="a" * 64,
        proposal_id="proposal",
        proposal_payload_sha256="b" * 64,
        decision_date="2026-07-27",
        scheduled_simulation_date="2026-07-28",
        market="US",
        universe_id="sp500",
        strategy="quality_value",
        equity_basis=100.0,
        peak_equity=100.0,
        cash_weight=0.1,
        positions=(
            StressPosition("AAA", "SEC-AAA", "Technology", 0.6, 1, 1_000_000.0, 20),
            StressPosition("BBB", "SEC-BBB", "Financials", 0.3, 2, 1_000_000.0, 20),
        ),
        risk_policy=policy
        or PortfolioRiskPolicy(
            minimum_positions=2,
            maximum_positions=2,
            maximum_position_weight=0.7,
            maximum_sector_weight=0.9,
            maximum_drawdown=0.5,
        ),
        readiness_sha256="c" * 64,
        decision_evidence_sha256="d" * 64,
    )


def _evidence() -> tuple[TargetStressEvidence, ...]:
    return (
        TargetStressEvidence(
            ticker="AAA",
            security_id="SEC-AAA",
            sector="Technology",
            target_weight=0.6,
            factor_rank=1,
            average_daily_dollar_volume=1_000_000.0,
            liquidity_observations=20,
            reviewed_price=100.0,
            reviewed_price_date="2026-07-27",
            price_observations=253,
            annualized_volatility=0.2,
            latest_revenue_fact_known_date="2026-07-20",
            revenue_fact_known_age_days=7,
            price_window_sha256="1" * 64,
            revenue_fact_evidence_sha256="2" * 64,
            liquidity_window_start="2026-06-29",
            liquidity_window_end="2026-07-27",
            liquidity_window_sha256="5" * 64,
        ),
        TargetStressEvidence(
            ticker="BBB",
            security_id="SEC-BBB",
            sector="Financials",
            target_weight=0.3,
            factor_rank=2,
            average_daily_dollar_volume=1_000_000.0,
            liquidity_observations=20,
            reviewed_price=50.0,
            reviewed_price_date="2026-07-27",
            price_observations=253,
            annualized_volatility=0.3,
            latest_revenue_fact_known_date="2026-07-20",
            revenue_fact_known_age_days=7,
            price_window_sha256="3" * 64,
            revenue_fact_evidence_sha256="4" * 64,
            liquidity_window_start="2026-06-29",
            liquidity_window_end="2026-07-27",
            liquidity_window_sha256="6" * 64,
        ),
    )


def _evaluate(
    bundle: StressScenarioBundle,
    *,
    stress_input: ProposalStressInput | None = None,
    evidence: tuple[TargetStressEvidence, ...] | None = None,
    scenario_ids: list[str] | None = None,
) -> StressReviewReport:
    source_files = {"tests/test_stress.py": "7" * 64}
    return evaluate_proposal_stress(
        stress_input or _stress_input(),
        evidence or _evidence(),
        bundle,
        scenario_ids=scenario_ids,
        source_identity={
            "source_bundle_sha256": canonical_payload_sha256({"files": source_files}),
            "source_files": source_files,
        },
        governance_context={
            "trial_id": "trial",
            "trial_payload_sha256": "f" * 64,
            "policy_bundle_sha256": "0" * 64,
            "policy_unchanged": True,
            "proposal_registered": True,
        },
    )


def test_default_scenario_bundle_is_valid_and_content_addressed() -> None:
    first = load_scenario_bundle()
    second = load_scenario_bundle()

    assert first.payload_sha256 == second.payload_sha256
    assert len(first.scenarios) == 11
    assert first.payload["market"] == "US"
    assert first.payload["currency"] == "USD"
    assert all(scenario["calibration_sources"] for scenario in first.scenarios)
    detached = first.payload
    detached["scenarios"][0]["assumptions"]["selected_return"] = 0.0
    assert first.payload["scenarios"][0]["assumptions"]["selected_return"] == -0.37


def test_exact_mixed_shock_math_recomputes_weights_and_cash() -> None:
    report = _evaluate(_bundle(_scenario("mixed_shock")))
    result = report.payload["analysis"]["scenarios"][0]

    assert result["portfolio_loss"] == pytest.approx(36.0)
    assert result["portfolio_loss_pct"] == pytest.approx(0.36)
    assert result["stressed_equity"] == pytest.approx(64.0)
    assert result["stressed_drawdown"] == pytest.approx(-0.36)
    assert [row["stressed_weight"] for row in result["positions"]] == pytest.approx(
        [0.46875, 0.375]
    )
    assert result["reference_limit_findings"] == []


def test_unshocked_position_can_become_concentrated_after_another_position_falls() -> None:
    scenario = _scenario(
        "concentration_reversal",
        assumptions={
            "selector": {"kind": "top_weighted", "count": 1},
            "selected_return": -0.8,
            "other_return": 0.0,
            "liquidity_adv_multiplier": 1.0,
            "liquidation_horizon_sessions": 5,
        },
    )
    policy = PortfolioRiskPolicy(
        minimum_positions=2,
        maximum_positions=2,
        maximum_position_weight=0.5,
        maximum_sector_weight=0.9,
        maximum_drawdown=0.8,
    )
    concentration_input = replace(
        _stress_input(policy=policy),
        positions=(
            replace(_stress_input().positions[0], target_weight=0.5),
            replace(_stress_input().positions[1], target_weight=0.4),
        ),
    )
    concentration_evidence = (
        replace(_evidence()[0], target_weight=0.5),
        replace(_evidence()[1], target_weight=0.4),
    )
    result = _evaluate(
        _bundle(scenario),
        stress_input=concentration_input,
        evidence=concentration_evidence,
    ).payload["analysis"]["scenarios"][0]

    assert result["positions"][1]["shock_return"] == 0.0
    assert result["positions"][1]["stressed_weight"] == pytest.approx(40.0 / 60.0)
    assert {finding["finding_id"] for finding in result["reference_limit_findings"]} == {
        "post_stress_position_concentration"
    }
    finding = result["reference_limit_findings"][0]
    assert finding["observed"] == pytest.approx(2.0 / 3.0)
    assert finding["limit"] == pytest.approx(0.5)
    assert finding["advisory"] is True
    assert "generic 50.0% sandbox reference" in finding["message"]


def test_volatility_correlation_proxy_has_exact_portfolio_math() -> None:
    scenario = _scenario(
        "volatility_test",
        scenario_type="volatility_correlation",
        assumptions={
            "volatility_multiplier": 2.0,
            "constant_correlation_assumption": 0.85,
            "standard_deviation_multiple": 2.0,
            "horizon_sessions": 20,
        },
    )
    result = _evaluate(_bundle(scenario)).payload["analysis"]["scenarios"][0]
    variance = 0.6**2 * 0.4**2 + 0.3**2 * 0.6**2 + 2 * 0.6 * 0.3 * 0.4 * 0.6 * 0.85
    expected_volatility = sqrt(variance)
    expected_loss_fraction = 2.0 * expected_volatility * sqrt(20 / 252)

    assert result["stressed_annualized_volatility"] is None
    assert result["sensitivity_portfolio_annualized_volatility"] == pytest.approx(
        expected_volatility
    )
    assert result["volatility_loss_proxy_pct"] == pytest.approx(expected_loss_fraction)
    assert result["portfolio_loss_pct"] == pytest.approx(expected_loss_fraction)
    assert result["volatility_method"]["probability_claim"] is False
    assert result["stressed_equity"] is None
    assert result["portfolio_return"] is None
    assert result["stressed_drawdown"] is None
    assert result["post_stress_state"]["applicable"] is False
    assert "policy_breaches" not in result
    assert result["sector_contributions"] == []
    assert result["reference_limit_not_applicable"] == [
        "post_stress_position_concentration",
        "post_stress_sector_concentration",
        "liquidity_horizon",
    ]
    assert all("stressed_weight" not in row for row in result["positions"])
    assert sum(
        row["euler_loss_contribution_pct_of_starting_equity"] for row in result["positions"]
    ) == pytest.approx(expected_loss_fraction)


class _CapturedEvidenceStore:
    def __init__(self, *, liquidity_adv: float = 1_000_000.0) -> None:
        self.sessions = us_equity_sessions(date(2025, 1, 1), date(2026, 7, 28))[-253:]
        self.liquidity_sessions = self.sessions[-20:]
        self.liquidity_adv = liquidity_adv
        self.price_calls: list[str] = []
        self.liquidity_calls: list[str] = []

    def query(self, sql, params):
        security_id, as_of, observations = params
        self.liquidity_calls.append(security_id)
        assert as_of == "2026-07-27"
        assert observations == 20
        return [
            {
                "ticker": security_id.removeprefix("SEC-"),
                "security_id": security_id,
                "date": session,
                "close": 100.0,
                "volume": self.liquidity_adv / 100.0,
                "source": "test",
                "fetched_at": "2026-07-27T23:59:00+00:00",
            }
            for session in self.liquidity_sessions
        ]

    def security_id_for_ticker(self, ticker, as_of):
        return f"SEC-{ticker}"

    def pit_factor_price_history(self, ticker, as_of, *, observations):
        self.price_calls.append(ticker)
        assert observations == 253
        return [
            {
                "ticker": ticker,
                "security_id": f"SEC-{ticker}",
                "date": session,
                "close": 100.0 + index + (index % 3) * 0.25,
                "dividends": 0.0,
                "split_ratio": 1.0,
                "actions_complete": True,
                "close_split_adjusted": False,
                "split_normalization_factor": None,
                "split_normalization_through": None,
                "source": "test",
            }
            for index, session in enumerate(self.sessions)
        ]

    def pit_factor_fundamentals(self, ticker, as_of, metrics):
        assert metrics == ["revenue"]
        return [
            {
                "metric": "revenue",
                "period_end": date(2026, 6, 30),
                "as_of_date": date(2026, 7, 20),
                "fiscal_period": "Q2",
                "value": 1_000.0,
                "quarter_value": 250.0,
            }
        ]


def test_evidence_collection_binds_volatility_to_one_captured_price_read() -> None:
    store = _CapturedEvidenceStore()
    evidence = collect_target_stress_evidence(
        store,  # type: ignore[arg-type]
        _stress_input(),
        evidence_policy=load_scenario_bundle().evidence_policy,
    )

    assert store.price_calls == ["AAA", "BBB"]
    assert store.liquidity_calls == ["SEC-AAA", "SEC-BBB"]
    assert all(row.price_observations == 253 for row in evidence)
    assert all(row.annualized_volatility is not None for row in evidence)
    assert all(not any(item.startswith("price:") for item in row.blockers) for row in evidence)
    assert all(row.price_window_sha256 for row in evidence)
    assert all(
        row.liquidity_window_start == store.liquidity_sessions[0].isoformat()
        and row.liquidity_window_end == store.liquidity_sessions[-1].isoformat()
        and row.liquidity_window_sha256
        and not any(item.startswith("liquidity:") for item in row.blockers)
        for row in evidence
    )


def test_liquidity_lineage_mismatch_withholds_dependent_scenario() -> None:
    evidence = collect_target_stress_evidence(
        _CapturedEvidenceStore(liquidity_adv=900_000.0),  # type: ignore[arg-type]
        _stress_input(),
        evidence_policy=load_scenario_bundle().evidence_policy,
    )

    assert all("liquidity:proposal_evidence_mismatch" in row.blockers for row in evidence)
    assert all(row.liquidity_window_sha256 for row in evidence)

    report = _evaluate(
        _bundle(_scenario("liquidity_mismatch")),
        evidence=evidence,
    )
    result = report.payload["analysis"]["scenarios"][0]
    assert report.payload["report_generation_status"] == "blocked"
    assert result["status"] == "withheld_evidence"
    assert result["portfolio_loss"] is None
    assert result["blockers"] == [
        "AAA:liquidity:proposal_evidence_mismatch",
        "BBB:liquidity:proposal_evidence_mismatch",
    ]


@pytest.mark.parametrize(
    "broken_method",
    [
        "query",
        "security_id_for_ticker",
        "pit_factor_price_history",
        "pit_factor_fundamentals",
    ],
)
def test_evidence_collection_does_not_disguise_programming_failures(
    broken_method: str,
) -> None:
    store = _CapturedEvidenceStore()

    def fail(*args, **kwargs):
        raise RuntimeError(f"{broken_method} programming failure")

    setattr(store, broken_method, fail)

    with pytest.raises(RuntimeError, match="programming failure"):
        collect_target_stress_evidence(
            store,  # type: ignore[arg-type]
            _stress_input(),
            evidence_policy=load_scenario_bundle().evidence_policy,
        )


def test_malformed_price_row_withholds_price_dependent_results() -> None:
    class MalformedPriceStore(_CapturedEvidenceStore):
        def pit_factor_price_history(self, ticker, as_of, *, observations):
            rows = super().pit_factor_price_history(
                ticker,
                as_of,
                observations=observations,
            )
            rows[-1]["close"] = "not-a-number"
            return rows

    evidence = collect_target_stress_evidence(
        MalformedPriceStore(),  # type: ignore[arg-type]
        _stress_input(),
        evidence_policy=load_scenario_bundle().evidence_policy,
    )

    assert all(row.reviewed_price is None for row in evidence)
    assert all("price:invalid_reviewed_close" in row.blockers for row in evidence)
    assert all("price:invalid_close" in row.blockers for row in evidence)


def test_malformed_revenue_known_date_becomes_a_revenue_blocker() -> None:
    class MalformedRevenueStore(_CapturedEvidenceStore):
        def pit_factor_fundamentals(self, ticker, as_of, metrics):
            rows = super().pit_factor_fundamentals(ticker, as_of, metrics)
            rows[0]["as_of_date"] = "not-a-date"
            return rows

    evidence = collect_target_stress_evidence(
        MalformedRevenueStore(),  # type: ignore[arg-type]
        _stress_input(),
        evidence_policy=load_scenario_bundle().evidence_policy,
    )

    assert all(row.latest_revenue_fact_known_date is None for row in evidence)
    assert all(row.revenue_fact_known_age_days is None for row in evidence)
    assert all("revenue_fact:invalid_known_date" in row.blockers for row in evidence)


def test_missing_price_evidence_blocks_only_dependent_scenarios() -> None:
    fixed = _scenario("fixed")
    volatility = _scenario(
        "volatility",
        scenario_type="volatility_correlation",
        assumptions={
            "volatility_multiplier": 2.0,
            "constant_correlation_assumption": 0.85,
            "standard_deviation_multiple": 2.0,
            "horizon_sessions": 20,
        },
    )
    outage = _scenario(
        "price_policy_demo",
        scenario_type="evidence_withholding_demonstration",
        assumptions={"evidence_kind": "price"},
    )
    rows = list(_evidence())
    rows[1] = replace(rows[1], blockers=("price:window_unavailable",))
    report = _evaluate(_bundle(fixed, volatility, outage), evidence=tuple(rows))
    results = {row["scenario_id"]: row for row in report.payload["analysis"]["scenarios"]}

    assert report.payload["report_generation_status"] == "partial"
    safeguards = {
        row["safeguard_id"]: row for row in report.payload["analysis"]["fail_closed_safeguards"]
    }
    assert results["fixed"]["status"] == "calculated"
    assert results["volatility"]["status"] == "withheld_evidence"
    assert safeguards["price_policy_demo"]["status"] == "withholding_required"
    assert results["volatility"]["portfolio_loss"] is None
    assert report.payload["evidence"]["blockers"] == ["BBB:price:window_unavailable"]


def test_fail_closed_policy_demonstrations_do_not_claim_injected_outages() -> None:
    bundle = load_scenario_bundle()
    report = _evaluate(
        bundle,
        scenario_ids=[
            "price_withholding_demonstration",
            "revenue_fact_withholding_demonstration",
        ],
    )
    safeguards = report.payload["analysis"]["fail_closed_safeguards"]

    assert report.payload["report_generation_status"] == "complete"
    assert "status" not in report.payload
    assert report.payload["analysis"]["scenarios"] == []
    assert report.payload["analysis"]["calculation_coverage"] == "not_applicable"
    assert report.payload["analysis"]["summary"]["selected_numerical_policy_count"] == 0
    assert report.payload["analysis"]["summary"]["calculated_numerical_result_count"] == 0
    assert [row["status"] for row in safeguards] == [
        "policy_demonstration",
        "policy_demonstration",
    ]
    assert all(row["outage_injected"] is False for row in safeguards)
    assert all(row["calculation_rerun"] is False for row in safeguards)


def test_analysis_is_deterministic_across_input_and_requested_order() -> None:
    first = _scenario("first")
    second = _scenario(
        "second",
        assumptions={
            "selector": {"kind": "all"},
            "selected_return": -0.1,
            "other_return": 0.0,
            "liquidity_adv_multiplier": 1.0,
            "liquidation_horizon_sessions": 5,
        },
    )
    bundle = _bundle(first, second)
    ordered = _evaluate(bundle, scenario_ids=["first", "second"])
    reversed_inputs = _evaluate(
        bundle,
        evidence=tuple(reversed(_evidence())),
        scenario_ids=["second", "first"],
    )

    assert ordered.payload == reversed_inputs.payload
    assert ordered.payload["analysis_sha256"] == reversed_inputs.payload["analysis_sha256"]
    assert ordered.payload_sha256 == reversed_inputs.payload_sha256


def test_mismatched_evidence_identity_or_weight_is_rejected() -> None:
    rows = list(_evidence())
    rows[0] = replace(rows[0], security_id="SEC-WRONG")
    with pytest.raises(ValueError, match="different security ID"):
        _evaluate(_bundle(_scenario("one")), evidence=tuple(rows))

    rows = list(_evidence())
    rows[0] = replace(rows[0], target_weight=0.59)
    with pytest.raises(ValueError, match="different target weight"):
        _evaluate(_bundle(_scenario("one")), evidence=tuple(rows))


def test_public_evaluator_rejects_inconsistent_cash_and_noncanonical_targets() -> None:
    bundle = _bundle(_scenario("input_contract"))
    with pytest.raises(ValueError, match="weights and cash do not reconcile"):
        _evaluate(bundle, stress_input=replace(_stress_input(), cash_weight=0.2))

    wrong_count_policy = PortfolioRiskPolicy(
        minimum_positions=3,
        maximum_positions=3,
        maximum_position_weight=0.7,
        maximum_sector_weight=0.9,
        maximum_drawdown=0.5,
    )
    with pytest.raises(ValueError, match="target count conflicts"):
        _evaluate(
            bundle,
            stress_input=replace(
                _stress_input(),
                risk_policy=wrong_count_policy,
            ),
        )

    overweight_policy = PortfolioRiskPolicy(
        minimum_positions=2,
        maximum_positions=2,
        maximum_position_weight=0.5,
        maximum_sector_weight=0.9,
        maximum_drawdown=0.5,
    )
    with pytest.raises(ValueError, match="exceeds the proposal position limit"):
        _evaluate(
            bundle,
            stress_input=replace(
                _stress_input(),
                risk_policy=overweight_policy,
            ),
        )

    noncanonical = replace(
        _stress_input(),
        positions=tuple(reversed(_stress_input().positions)),
    )
    with pytest.raises(ValueError, match="canonical rank and identity order"):
        _evaluate(bundle, stress_input=noncanonical)


def test_public_evaluator_rejects_duplicate_evidence_rows() -> None:
    rows = _evidence()
    with pytest.raises(ValueError, match="duplicate target rows"):
        _evaluate(
            _bundle(_scenario("duplicate_evidence")),
            evidence=(rows[0], replace(rows[0])),
        )


def test_source_bundle_hash_must_match_file_manifest() -> None:
    source_files = {"src/aios/risk/stress.py": "7" * 64}
    with pytest.raises(ValueError, match="source bundle.*file manifest"):
        evaluate_proposal_stress(
            _stress_input(),
            _evidence(),
            _bundle(_scenario("source_identity")),
            source_identity={
                "source_bundle_sha256": "e" * 64,
                "source_files": source_files,
            },
        )


def test_selected_shock_cannot_be_less_severe_than_unselected_shock() -> None:
    scenario = _scenario(
        "reversed_shock",
        assumptions={
            "selector": {"kind": "top_weighted", "count": 1},
            "selected_return": -0.1,
            "other_return": -0.5,
            "liquidity_adv_multiplier": 1.0,
            "liquidation_horizon_sessions": 5,
        },
    )
    with pytest.raises(ValueError, match=r"selected.return"):
        _bundle(scenario)


def test_fully_invested_total_wipeout_is_calculated_not_withheld() -> None:
    stress_input = replace(
        _stress_input(),
        cash_weight=0.0,
        positions=(
            _stress_input().positions[0],
            replace(_stress_input().positions[1], target_weight=0.4),
        ),
    )
    evidence = (
        _evidence()[0],
        replace(_evidence()[1], target_weight=0.4),
    )
    scenario = _scenario(
        "total_wipeout",
        assumptions={
            "selector": {"kind": "all"},
            "selected_return": -1.0,
            "other_return": 0.0,
            "liquidity_adv_multiplier": 1.0,
            "liquidation_horizon_sessions": 5,
        },
    )

    report = _evaluate(
        _bundle(scenario),
        stress_input=stress_input,
        evidence=evidence,
    )
    result = report.payload["analysis"]["scenarios"][0]

    assert result["status"] == "calculated"
    assert result["stressed_equity"] == 0.0
    assert result["portfolio_return"] == -1.0
    assert result["portfolio_loss"] == 100.0
    assert result["portfolio_loss_pct"] == 1.0
    assert result["stressed_drawdown"] == -1.0
    assert all(row["stressed_weight"] is None for row in result["positions"])
    assert all(row["stressed_weight"] is None for row in result["sector_contributions"])
    assert "portfolio_equity_exhausted" in {
        finding["finding_id"] for finding in result["reference_limit_findings"]
    }
    assert (
        report.payload["analysis"]["summary"]["largest_fixed_shock_scenario_id"] == "total_wipeout"
    )


def test_volatility_proxy_rejects_liquidity_assumptions() -> None:
    scenario = _scenario(
        "volatility_schema",
        scenario_type="volatility_correlation",
        assumptions={
            "volatility_multiplier": 2.0,
            "constant_correlation_assumption": 0.85,
            "standard_deviation_multiple": 2.0,
            "horizon_sessions": 20,
        },
    )
    assert set(_bundle(scenario).scenarios[0]["assumptions"]) == {
        "volatility_multiplier",
        "constant_correlation_assumption",
        "standard_deviation_multiple",
        "horizon_sessions",
    }

    polluted = deepcopy(scenario)
    polluted["assumptions"]["liquidity_adv_multiplier"] = 0.5
    polluted["assumption_provenance"]["liquidity_adv_multiplier"] = "test_fixture_assumption"
    with pytest.raises(ValueError, match="volatility proxy assumptions must be exactly"):
        _bundle(polluted)


def test_bundle_rejects_duplicate_ids_and_non_monotonic_recovery(tmp_path) -> None:
    payload = deepcopy(load_scenario_bundle().payload)
    payload["scenarios"].append(deepcopy(payload["scenarios"][0]))
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate stress scenario ID"):
        load_scenario_bundle(duplicate)

    payload = deepcopy(load_scenario_bundle().payload)
    payload["scenarios"][0]["recovery_path"][1]["session_offset"] = 0
    recovery = tmp_path / "recovery.json"
    recovery.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="strictly increase"):
        load_scenario_bundle(recovery)


def test_bundle_and_report_reject_unsupported_schema_versions(tmp_path) -> None:
    bundle_payload = deepcopy(load_scenario_bundle().payload)
    bundle_payload["stress_scenario_bundle_schema_version"] = 2
    unsupported_bundle = tmp_path / "unsupported-bundle.json"
    unsupported_bundle.write_text(json.dumps(bundle_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported stress scenario bundle schema"):
        load_scenario_bundle(unsupported_bundle)

    report = _evaluate(_bundle(_scenario("report_schema")))
    raw = report.envelope()
    raw["payload"]["stress_report_schema_version"] = 2
    raw["payload_sha256"] = canonical_payload_sha256(raw["payload"])
    unsupported_report = tmp_path / "unsupported-report.json"
    unsupported_report.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported stress report schema"):
        read_stress_report(unsupported_report)


def test_unknown_or_blank_scenario_selection_is_rejected() -> None:
    bundle = _bundle(_scenario("known"))
    with pytest.raises(ValueError, match="unknown stress scenario"):
        _evaluate(bundle, scenario_ids=["unknown"])
    with pytest.raises(ValueError, match="non-empty stress scenario"):
        _evaluate(bundle, scenario_ids=[" "])


def test_sector_template_assigns_unique_ids_to_colliding_slugs() -> None:
    stress_input = replace(
        _stress_input(),
        positions=(
            replace(_stress_input().positions[0], sector="Health Care"),
            replace(_stress_input().positions[1], sector="Health-Care"),
        ),
    )
    evidence = (
        replace(_evidence()[0], sector="Health Care"),
        replace(_evidence()[1], sector="Health-Care"),
    )
    scenario = _scenario(
        "sector",
        scenario_type="sector_template",
        assumptions={
            "selector": {"kind": "each_sector"},
            "selected_return": -0.5,
            "other_return": 0.0,
            "liquidity_adv_multiplier": 1.0,
            "liquidation_horizon_sessions": 5,
        },
    )

    results = _evaluate(
        _bundle(scenario),
        stress_input=stress_input,
        evidence=evidence,
    ).payload["analysis"]["scenarios"]

    ids = [row["scenario_id"] for row in results]
    assert ids == [
        "sector:health-care:04b0a88a87070712",
        "sector:health-care:ce6cf52e8ca3ca37",
    ]
    assert {row["label"] for row in results} == {
        "sector — Health Care",
        "sector — Health-Care",
    }


def test_write_once_report_validates_tampering_and_concurrent_writers(tmp_path) -> None:
    report = _evaluate(_bundle(_scenario("write_once")))
    destination = tmp_path / "stress" / "report.json"

    assert write_stress_report(destination, report) == destination
    assert read_stress_report(destination) == report
    original = destination.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        write_stress_report(destination, report)
    assert destination.read_bytes() == original

    concurrent = tmp_path / "stress" / "concurrent.json"

    def attempt() -> str:
        try:
            write_stress_report(concurrent, report)
        except ValueError:
            return "refused"
        return "written"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(lambda _: attempt(), range(2)))
    assert outcomes == ["refused", "written"]
    assert read_stress_report(concurrent) == report
    assert not list(concurrent.parent.glob(".*.tmp"))

    refused = tmp_path / "stress" / "refused.json"
    with pytest.raises(RuntimeError, match="source changed"):
        write_stress_report(
            refused,
            report,
            before_publish=lambda: (_ for _ in ()).throw(RuntimeError("source changed")),
        )
    assert not refused.exists()
    assert not list(refused.parent.glob(".*.tmp"))

    raw = json.loads(destination.read_text(encoding="utf-8"))
    raw["payload"]["analysis"]["report_generation_status"] = "tampered"
    destination.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch"):
        read_stress_report(destination)


def test_write_once_report_rolls_back_when_directory_sync_fails(
    tmp_path,
    monkeypatch,
) -> None:
    report = _evaluate(_bundle(_scenario("directory_sync")))
    destination = tmp_path / "stress" / "report.json"
    real_fsync = stress_module.os.fsync
    calls = 0

    def fail_directory_sync(file_descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory sync failed")
        real_fsync(file_descriptor)

    monkeypatch.setattr(stress_module.os, "fsync", fail_directory_sync)

    with pytest.raises(OSError, match="directory sync failed"):
        write_stress_report(destination, report)

    assert not destination.exists()
    assert not list(destination.parent.glob(".*.tmp"))


def test_default_report_path_sanitizes_untrusted_proposal_id(tmp_path) -> None:
    stress_input = replace(_stress_input(), proposal_id="../../outside report")
    report = _evaluate(_bundle(_scenario("safe_path")), stress_input=stress_input)
    destination = default_stress_report_path(tmp_path, report)

    assert destination.is_relative_to((tmp_path / "data/stress/reports").resolve())
    assert ".." not in destination.name
    assert "/" not in destination.name


def test_report_source_cas_compares_full_account_and_proposal_hashes(
    tmp_path,
) -> None:
    def write_document(path, kind, payload):
        path.write_text(
            json.dumps(
                {
                    "document_schema_version": 1,
                    "document_kind": kind,
                    "payload_sha256": canonical_payload_sha256(payload),
                    "payload": payload,
                }
            ),
            encoding="utf-8",
        )

    account_path = tmp_path / "account.json"
    proposal_path = tmp_path / "proposal.json"
    account_payload = {"account_id": "account", "state": "before"}
    proposal_payload = {"proposal_id": "proposal", "state": "before"}
    write_document(account_path, ACCOUNT_DOCUMENT_KIND, account_payload)
    write_document(proposal_path, PROPOSAL_DOCUMENT_KIND, proposal_payload)
    payload = {
        "source": {
            "account_payload_sha256": canonical_payload_sha256(account_payload),
            "proposal_payload_sha256": canonical_payload_sha256(proposal_payload),
        },
        "scenario_bundle": {
            "bundle_sha256": load_scenario_bundle().payload_sha256,
        },
        "source_code": build_stress_source_identity(cli.settings.project_root),
    }
    report = StressReviewReport(payload, canonical_payload_sha256(payload))

    require_stress_report_sources_unchanged(
        report,
        account_path,
        proposal_path,
        project_root=cli.settings.project_root,
    )

    write_document(account_path, ACCOUNT_DOCUMENT_KIND, {**account_payload, "state": "after"})
    with pytest.raises(ValueError, match="paper account changed"):
        require_stress_report_sources_unchanged(
            report,
            account_path,
            proposal_path,
            project_root=cli.settings.project_root,
        )
    write_document(account_path, ACCOUNT_DOCUMENT_KIND, account_payload)
    write_document(
        proposal_path,
        PROPOSAL_DOCUMENT_KIND,
        {**proposal_payload, "state": "after"},
    )
    with pytest.raises(ValueError, match="paper proposal changed"):
        require_stress_report_sources_unchanged(
            report,
            account_path,
            proposal_path,
            project_root=cli.settings.project_root,
        )


def test_report_source_cas_refuses_changed_scenario_bundle(tmp_path) -> None:
    def write_document(path, kind, payload):
        path.write_text(
            json.dumps(
                {
                    "document_schema_version": 1,
                    "document_kind": kind,
                    "payload_sha256": canonical_payload_sha256(payload),
                    "payload": payload,
                }
            ),
            encoding="utf-8",
        )

    account_path = tmp_path / "account.json"
    proposal_path = tmp_path / "proposal.json"
    bundle_path = tmp_path / "scenarios.json"
    account_payload = {"account_id": "account"}
    proposal_payload = {"proposal_id": "proposal"}
    write_document(account_path, ACCOUNT_DOCUMENT_KIND, account_payload)
    write_document(proposal_path, PROPOSAL_DOCUMENT_KIND, proposal_payload)
    original_bundle = deepcopy(load_scenario_bundle().payload)
    bundle_path.write_text(
        json.dumps(original_bundle, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    payload = {
        "source": {
            "account_payload_sha256": canonical_payload_sha256(account_payload),
            "proposal_payload_sha256": canonical_payload_sha256(proposal_payload),
        },
        "scenario_bundle": {
            "bundle_sha256": StressScenarioBundle(original_bundle).payload_sha256,
        },
        "source_code": build_stress_source_identity(cli.settings.project_root),
    }
    report = StressReviewReport(payload, canonical_payload_sha256(payload))

    require_stress_report_sources_unchanged(
        report,
        account_path,
        proposal_path,
        project_root=cli.settings.project_root,
        bundle_path=bundle_path,
    )

    changed_bundle = deepcopy(original_bundle)
    changed_bundle["bundle_version"] = "1.1.1"
    bundle_path.write_text(
        json.dumps(changed_bundle, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="scenario bundle changed"):
        require_stress_report_sources_unchanged(
            report,
            account_path,
            proposal_path,
            project_root=cli.settings.project_root,
            bundle_path=bundle_path,
        )


def test_report_build_refuses_scenario_bundle_change_during_calculation(
    tmp_path,
    monkeypatch,
) -> None:
    bundle_path = tmp_path / "scenarios.json"
    original_bundle = deepcopy(load_scenario_bundle().payload)
    bundle_path.write_text(
        json.dumps(original_bundle, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    documents = iter([object(), object()])

    monkeypatch.setattr(
        stress_module,
        "read_paper_document",
        lambda *args, **kwargs: next(documents),
    )
    monkeypatch.setattr(
        stress_module,
        "build_proposal_stress_input",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        stress_module,
        "collect_target_stress_evidence",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        stress_module,
        "_require_source_documents_unchanged",
        lambda *args, **kwargs: None,
    )

    def evaluate(*args, **kwargs):
        changed_bundle = deepcopy(original_bundle)
        changed_bundle["bundle_version"] = "1.1.1"
        bundle_path.write_text(
            json.dumps(changed_bundle, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return _cli_report()

    monkeypatch.setattr(stress_module, "evaluate_proposal_stress", evaluate)

    with pytest.raises(ValueError, match="scenario bundle changed"):
        stress_module.review_paper_proposal_stress(
            tmp_path / "account.json",
            tmp_path / "proposal.json",
            object(),
            project_root=tmp_path,
            bundle_path=bundle_path,
            source_identity={},
        )


def _paper_documents(
    tmp_path,
    store: Store,
    *,
    account_changes: dict | None = None,
    proposal_changes: dict | None = None,
) -> tuple[PaperDocument, PaperDocument]:
    book = PortfolioBook(
        store,
        initial_capital=100_000.0,
        transaction_costs=TransactionCostPolicy(),
        tax_policy=TaxPolicy.zero(),
        calendar_ticker="SPY",
    )
    account_payload = {
        "account_schema_version": 1,
        "account_id": "account",
        "market": "US",
        "mode": "simulation_only",
        "broker_connected": False,
        "portfolio": book.to_state(),
        "executions": [],
        "audit_events": [],
    }
    account_payload.update(account_changes or {})
    account = PaperDocument(
        tmp_path / "account.json",
        ACCOUNT_DOCUMENT_KIND,
        account_payload,
        canonical_payload_sha256(account_payload),
    )
    targets = [
        {
            "ticker": f"T{index:02d}",
            "security_id": f"SEC-T{index:02d}",
            "sector": f"Sector {index % 4}",
            "target_weight": 0.1,
            "factor_rank": index + 1,
            "average_daily_dollar_volume": 10_000_000.0,
            "liquidity_observations": 20,
        }
        for index in range(10)
    ]
    risk_assessment = {"approved": True, "checks": []}
    proposal_payload = {
        "proposal_schema_version": 1,
        "proposal_id": "proposal",
        "account_id": "account",
        "account_payload_sha256": account.payload_sha256,
        "market": "US",
        "universe_id": "sp500",
        "strategy": "quality_value",
        "mode": "simulation_only",
        "status": "approved_for_supervised_simulation",
        "decision_date": "2026-07-27",
        "scheduled_simulation_date": "2026-07-28",
        "readiness": {"ready": True, "checks": []},
        "risk_policy": asdict(PortfolioRiskPolicy()),
        "risk_assessment": risk_assessment,
        "targets": targets,
        "factor_evidence_sha256": "1" * 64,
        "exit_liquidity_evidence": [],
        "selection_skips": [],
    }
    proposal_payload.update(proposal_changes or {})
    proposal_payload["decision_evidence_sha256"] = canonical_payload_sha256(
        {
            "factor_evidence_sha256": proposal_payload.get("factor_evidence_sha256"),
            "targets": proposal_payload.get("targets"),
            "exit_liquidity_evidence": proposal_payload.get("exit_liquidity_evidence"),
            "selection_skips": proposal_payload.get("selection_skips"),
            "risk_assessment": proposal_payload.get("risk_assessment"),
        }
    )
    proposal = PaperDocument(
        tmp_path / "proposal.json",
        PROPOSAL_DOCUMENT_KIND,
        proposal_payload,
        canonical_payload_sha256(proposal_payload),
    )
    return account, proposal


def test_proposal_input_accepts_only_governed_simulation_documents(tmp_path) -> None:
    store = Store(tmp_path / "input.duckdb")
    try:
        account, proposal = _paper_documents(tmp_path, store)
        stress_input = build_proposal_stress_input(account, proposal, store)
        assert stress_input.equity_basis == 100_000.0
        assert len(stress_input.positions) == 10
        assert stress_input.cash_weight == 0.0

        broker_account, broker_proposal = _paper_documents(
            tmp_path,
            store,
            account_changes={"broker_connected": True},
        )
        with pytest.raises(ValueError, match="broker connection"):
            build_proposal_stress_input(broker_account, broker_proposal, store)

        _, mismatched = _paper_documents(
            tmp_path,
            store,
            proposal_changes={"account_payload_sha256": "0" * 64},
        )
        with pytest.raises(ValueError, match="account changed after proposal"):
            build_proposal_stress_input(account, mismatched, store)
    finally:
        store.close()


def test_proposal_input_rejects_duplicate_security_and_non_integer_rank(tmp_path) -> None:
    store = Store(tmp_path / "target-input.duckdb")
    try:
        account, proposal = _paper_documents(tmp_path, store)
        duplicate_targets = deepcopy(proposal.payload["targets"])
        duplicate_targets[-1]["security_id"] = duplicate_targets[0]["security_id"]
        _, duplicate = _paper_documents(
            tmp_path,
            store,
            proposal_changes={"targets": duplicate_targets},
        )
        with pytest.raises(ValueError, match="duplicate stable security IDs"):
            build_proposal_stress_input(account, duplicate, store)

        non_integer_targets = deepcopy(proposal.payload["targets"])
        non_integer_targets[0]["factor_rank"] = 1.5
        _, non_integer = _paper_documents(
            tmp_path,
            store,
            proposal_changes={"targets": non_integer_targets},
        )
        with pytest.raises(ValueError, match="positive integer"):
            build_proposal_stress_input(account, non_integer, store)
    finally:
        store.close()


def _cli_report(*, status: str = "complete") -> StressReviewReport:
    payload = {
        "report_generation_status": status,
        "source": {"proposal_id": "proposal"},
        "analysis": {
            "scenarios": [],
            "fail_closed_safeguards": [],
            "calculation_coverage": "complete",
            "input_evidence": "sufficient",
            "summary": {
                "selected_numerical_policy_count": 0,
                "selected_safeguard_demonstration_count": 0,
                "generated_numerical_result_count": 0,
                "calculated_numerical_result_count": 0,
                "largest_fixed_shock_scenario_id": None,
                "largest_fixed_shock_loss": None,
                "largest_fixed_shock_loss_pct": None,
                "largest_statistical_proxy_scenario_id": None,
                "largest_statistical_proxy_loss": None,
                "largest_statistical_proxy_loss_pct": None,
            },
        },
        "evidence": {"blockers": []},
    }
    return StressReviewReport(payload, canonical_payload_sha256(payload))


def test_governed_service_checks_forward_before_database_access(
    tmp_path,
    monkeypatch,
) -> None:
    events: list[str] = []
    trial = SimpleNamespace(
        payload={"trial_id": "trial", "policy_bundle_sha256": "p" * 64},
        payload_sha256="t" * 64,
    )

    def read_trial(*args, **kwargs):
        events.append("trial")
        return trial

    def require(*args, **kwargs):
        events.append("forward")
        raise ValueError("missing forward trial")

    monkeypatch.setattr(stress_module, "read_forward_trial", read_trial)
    monkeypatch.setattr(
        stress_module,
        "require_registered_forward_proposal",
        require,
    )
    monkeypatch.setattr(
        stress_module,
        "store_scope",
        lambda *args, **kwargs: pytest.fail("database opened before forward governance"),
    )

    with pytest.raises(ValueError, match="missing forward trial"):
        stress_module.review_registered_paper_proposal_stress(
            tmp_path,
            tmp_path / "trial.json",
            tmp_path / "account.json",
            tmp_path / "proposal.json",
        )
    assert events == ["trial", "forward"]


def test_governed_service_uses_read_only_store_and_rechecks_final_sources(
    tmp_path,
    monkeypatch,
) -> None:
    events: list[str] = []
    status = SimpleNamespace(trial_id="trial", policy_unchanged=True)
    trial = SimpleNamespace(
        payload={"trial_id": "trial", "policy_bundle_sha256": "p" * 64},
        payload_sha256="t" * 64,
    )

    def read_trial(*args, **kwargs):
        events.append("trial")
        return trial

    def require(*args, **kwargs):
        events.append("forward")
        return status

    def scoped_store(*args, **kwargs):
        events.append("store")
        assert args == ()
        assert kwargs == {"read_only": True}
        return nullcontext(object())

    def review(*args, **kwargs):
        events.append("review")
        assert kwargs["governance_context"] == {
            "trial_id": "trial",
            "trial_payload_sha256": "t" * 64,
            "policy_bundle_sha256": "p" * 64,
            "policy_unchanged": True,
            "proposal_registered": True,
            "check_contract": ("validated before database access and again before output"),
        }
        return _cli_report()

    monkeypatch.setattr(stress_module, "read_forward_trial", read_trial)
    monkeypatch.setattr(
        stress_module,
        "require_registered_forward_proposal",
        require,
    )
    monkeypatch.setattr(stress_module, "store_scope", scoped_store)
    monkeypatch.setattr(stress_module, "review_paper_proposal_stress", review)

    def require_sources(*args, **kwargs):
        events.append("sources")
        assert kwargs["bundle_path"] == stress_module.DEFAULT_SCENARIO_BUNDLE

    monkeypatch.setattr(
        stress_module,
        "require_stress_report_sources_unchanged",
        require_sources,
    )

    governed = stress_module.review_registered_paper_proposal_stress(
        tmp_path,
        tmp_path / "trial.json",
        tmp_path / "account.json",
        tmp_path / "proposal.json",
    )

    assert governed.report == _cli_report()
    assert governed.artifact_path is None
    assert events == [
        "trial",
        "forward",
        "trial",
        "store",
        "review",
        "trial",
        "forward",
        "trial",
        "sources",
    ]


def test_governed_service_final_cas_refuses_artifact_when_trial_changes(
    tmp_path,
    monkeypatch,
) -> None:
    status = SimpleNamespace(trial_id="trial", policy_unchanged=True)
    trials = iter(
        [
            SimpleNamespace(
                payload={"trial_id": "trial", "policy_bundle_sha256": "p" * 64},
                payload_sha256="1" * 64,
            ),
            SimpleNamespace(
                payload={"trial_id": "trial", "policy_bundle_sha256": "p" * 64},
                payload_sha256="1" * 64,
            ),
            SimpleNamespace(
                payload={"trial_id": "trial", "policy_bundle_sha256": "p" * 64},
                payload_sha256="1" * 64,
            ),
            SimpleNamespace(
                payload={"trial_id": "trial", "policy_bundle_sha256": "p" * 64},
                payload_sha256="2" * 64,
            ),
        ]
    )
    monkeypatch.setattr(
        stress_module,
        "read_forward_trial",
        lambda *args, **kwargs: next(trials),
    )
    monkeypatch.setattr(
        stress_module,
        "require_registered_forward_proposal",
        lambda *args, **kwargs: status,
    )
    monkeypatch.setattr(
        stress_module,
        "store_scope",
        lambda *args, **kwargs: nullcontext(object()),
    )
    monkeypatch.setattr(
        stress_module,
        "review_paper_proposal_stress",
        lambda *args, **kwargs: _cli_report(),
    )
    monkeypatch.setattr(
        stress_module,
        "require_stress_report_sources_unchanged",
        lambda *args, **kwargs: pytest.fail("source CAS ran after trial CAS failed"),
    )
    monkeypatch.setattr(
        stress_module,
        "write_stress_report",
        lambda *args, **kwargs: pytest.fail("artifact written after final CAS failed"),
    )

    destination = tmp_path / "data/stress/reports/report.json"
    with pytest.raises(ValueError, match="forward trial changed"):
        stress_module.review_registered_paper_proposal_stress(
            tmp_path,
            tmp_path / "trial.json",
            tmp_path / "account.json",
            tmp_path / "proposal.json",
            output_path=destination,
        )
    assert not destination.exists()


def test_governed_service_rolls_back_artifact_when_post_publish_cas_fails(
    tmp_path,
    monkeypatch,
) -> None:
    status = SimpleNamespace(trial_id="trial", policy_unchanged=True)
    trial = SimpleNamespace(
        payload={"trial_id": "trial", "policy_bundle_sha256": "p" * 64},
        payload_sha256="t" * 64,
    )
    report = _evaluate(_bundle(_scenario("post_publish_cas")))
    source_checks = 0

    monkeypatch.setattr(
        stress_module,
        "read_forward_trial",
        lambda *args, **kwargs: trial,
    )
    monkeypatch.setattr(
        stress_module,
        "require_registered_forward_proposal",
        lambda *args, **kwargs: status,
    )
    monkeypatch.setattr(
        stress_module,
        "store_scope",
        lambda *args, **kwargs: nullcontext(object()),
    )
    monkeypatch.setattr(
        stress_module,
        "review_paper_proposal_stress",
        lambda *args, **kwargs: report,
    )

    def require_sources(*args, **kwargs):
        nonlocal source_checks
        source_checks += 1
        if source_checks == 3:
            raise ValueError("stress scenario bundle changed after analysis")

    monkeypatch.setattr(
        stress_module,
        "require_stress_report_sources_unchanged",
        require_sources,
    )

    def publish_then_change(path, selected_report, *, before_publish):
        before_publish()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(selected_report.envelope()),
            encoding="utf-8",
        )
        return path

    monkeypatch.setattr(stress_module, "write_stress_report", publish_then_change)

    destination = tmp_path / "data/stress/reports/report.json"
    with pytest.raises(ValueError, match="scenario bundle changed"):
        stress_module.review_registered_paper_proposal_stress(
            tmp_path,
            tmp_path / "trial.json",
            tmp_path / "account.json",
            tmp_path / "proposal.json",
            output_path=destination,
        )

    assert source_checks == 3
    assert not destination.exists()


def test_governed_service_refuses_output_outside_report_namespace_before_reads(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        stress_module,
        "read_forward_trial",
        lambda *args, **kwargs: pytest.fail(
            "governance evidence was read before output validation"
        ),
    )

    forbidden = tmp_path / "data/paper/proposals/looks-like-a-proposal.json"
    with pytest.raises(
        ValueError,
        match="must stay under data/stress/reports",
    ):
        stress_module.review_registered_paper_proposal_stress(
            tmp_path,
            tmp_path / "trial.json",
            tmp_path / "account.json",
            tmp_path / "proposal.json",
            output_path=forbidden,
        )

    assert not forbidden.exists()


def test_governed_service_refuses_symlinked_report_directory_before_reads(
    tmp_path,
    monkeypatch,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    report_directory = tmp_path / "data/stress/reports"
    report_directory.parent.mkdir(parents=True)
    report_directory.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        stress_module,
        "read_forward_trial",
        lambda *args, **kwargs: pytest.fail(
            "governance evidence was read before output validation"
        ),
    )

    with pytest.raises(
        ValueError,
        match="report directory cannot contain symbolic links",
    ):
        stress_module.review_registered_paper_proposal_stress(
            tmp_path,
            tmp_path / "trial.json",
            tmp_path / "account.json",
            tmp_path / "proposal.json",
            output_path=report_directory / "report.json",
        )

    assert list(outside.iterdir()) == []


def test_stress_cli_delegates_to_governed_service_and_renders_json(
    tmp_path,
    monkeypatch,
) -> None:
    events: list[str] = []

    def governed_review(*args, **kwargs):
        events.append("governed_service")
        assert args == (
            tmp_path,
            tmp_path / "data/paper/us_qv_forward_trial.json",
            tmp_path / "data/paper/us_qv_sandbox.json",
            tmp_path / "proposal.json",
        )
        assert kwargs == {
            "scenario_ids": None,
            "output_path": None,
        }
        return stress_module.GovernedStressReview(report=_cli_report())

    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(
        stress_module,
        "review_registered_paper_proposal_stress",
        governed_review,
    )

    result = CliRunner().invoke(
        cli.app,
        ["stress-review", "--proposal", "proposal.json", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["document_kind"] == "aios.stress-report"
    assert events == ["governed_service"]


def test_stress_cli_surfaces_governed_service_refusal(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(
        stress_module,
        "review_registered_paper_proposal_stress",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("missing forward trial")),
    )

    result = CliRunner().invoke(
        cli.app,
        ["stress-review", "--proposal", "proposal.json"],
    )

    assert result.exit_code == 1
    assert "missing forward trial" in result.output


def test_stress_cli_does_not_create_output_after_governed_service_cas_refusal(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "settings", SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(
        stress_module,
        "review_registered_paper_proposal_stress",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError("forward trial changed while stress evidence was being reviewed")
        ),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "stress-review",
            "--proposal",
            "proposal.json",
            "--output",
            "data/stress/reports/report.json",
        ],
    )

    assert result.exit_code == 1
    assert "forward trial changed" in result.output
    assert not (tmp_path / "data/stress/reports/report.json").exists()

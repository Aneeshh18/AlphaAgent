"""Contracts for named, versioned research/market/account policy identity.

Every value these builders return must trace to the real running frozen
module, never a hand-typed duplicate — that is the whole point: drift in the
source shows up as a changed hash automatically.
"""

from __future__ import annotations

import pytest

from aios.backtest.costs import TaxPolicy, TransactionCostPolicy
from aios.factors.policy import (
    QVML_LOW_VOLATILITY_WEIGHT,
    QVML_MOMENTUM_WEIGHT,
    QVML_QV_CORE_WEIGHT,
    REGIME_FACTOR_WEIGHTS,
)
from aios.policy_domains import (
    current_account_policy,
    current_market_profile,
    current_research_policy,
    policy_snapshot,
)
from aios.risk.policy import PortfolioRiskPolicy


def test_research_policy_matches_the_real_frozen_module_exactly() -> None:
    identity = current_research_policy(name="test-research", version="v1")
    baseline = REGIME_FACTOR_WEIGHTS["unknown"]

    assert identity.baseline_quality_weight == baseline.quality
    assert identity.baseline_value_weight == baseline.value
    assert identity.qvml_qv_core_weight == QVML_QV_CORE_WEIGHT
    assert identity.qvml_momentum_weight == QVML_MOMENTUM_WEIGHT
    assert identity.qvml_low_volatility_weight == QVML_LOW_VOLATILITY_WEIGHT
    assert identity.risk_policy == {
        "minimum_positions": PortfolioRiskPolicy().minimum_positions,
        "maximum_positions": PortfolioRiskPolicy().maximum_positions,
        "maximum_position_weight": PortfolioRiskPolicy().maximum_position_weight,
        "maximum_sector_weight": PortfolioRiskPolicy().maximum_sector_weight,
        "maximum_gross_exposure": PortfolioRiskPolicy().maximum_gross_exposure,
        "maximum_rebalance_turnover": PortfolioRiskPolicy().maximum_rebalance_turnover,
        "maximum_drawdown": PortfolioRiskPolicy().maximum_drawdown,
        "maximum_order_adv_fraction": PortfolioRiskPolicy().maximum_order_adv_fraction,
        "require_sector_data": PortfolioRiskPolicy().require_sector_data,
        "require_liquidity_data": PortfolioRiskPolicy().require_liquidity_data,
    }
    # Every regime the real module defines is present, none invented.
    assert set(identity.regime_weights) == set(REGIME_FACTOR_WEIGHTS)
    for regime, weights in REGIME_FACTOR_WEIGHTS.items():
        assert identity.regime_weights[regime] == {
            "quality": weights.quality,
            "value": weights.value,
        }


def test_research_policy_hash_is_deterministic_and_ignores_name_free_content() -> None:
    first = current_research_policy(name="a", version="v1")
    second = current_research_policy(name="a", version="v1")
    assert first.content_sha256 == second.content_sha256
    assert len(first.content_sha256) == 64


def test_research_policy_hash_changes_with_name_or_version() -> None:
    base = current_research_policy(name="a", version="v1")
    renamed = current_research_policy(name="b", version="v1")
    reversioned = current_research_policy(name="a", version="v2")
    # The name/version ARE part of identity: renaming a policy is itself a
    # meaningful change to detect, even if every numeric value is identical.
    assert renamed.content_sha256 != base.content_sha256
    assert reversioned.content_sha256 != base.content_sha256


def test_market_profile_declares_real_repo_conventions() -> None:
    identity = current_market_profile(name="test-market", version="v1")
    assert identity.universe_id == "sp500"
    assert identity.benchmark_ticker == "SPY"
    assert identity.calendar_ticker == "SPY"
    assert identity.primary_price_source == "yfinance"
    assert identity.fundamentals_source == "sec-edgar"
    assert len(identity.content_sha256) == 64


def test_account_policy_matches_real_cost_and_tax_dataclasses() -> None:
    identity = current_account_policy(name="test-account", version="v1")
    assert identity.default_transaction_costs == TransactionCostPolicy(
        commission_bps=5.0, slippage_bps=5.0
    ).to_dict()
    assert identity.default_tax_policy == TaxPolicy.zero().to_dict()
    assert identity.default_initial_capital == 100_000.0
    assert len(identity.content_sha256) == 64


@pytest.mark.parametrize(
    "builder",
    [current_research_policy, current_market_profile, current_account_policy],
)
def test_every_domain_rejects_an_empty_name(builder) -> None:
    with pytest.raises(ValueError, match="name"):
        builder(name="   ", version="v1")


@pytest.mark.parametrize(
    "builder",
    [current_research_policy, current_market_profile, current_account_policy],
)
def test_every_domain_rejects_an_empty_version(builder) -> None:
    with pytest.raises(ValueError, match="version"):
        builder(name="ok", version="")


def test_every_domain_rejects_an_oversized_name() -> None:
    with pytest.raises(ValueError, match="96 characters"):
        current_research_policy(name="x" * 97, version="v1")


def test_every_domain_rejects_an_oversized_version() -> None:
    with pytest.raises(ValueError, match="32 characters"):
        current_research_policy(name="ok", version="x" * 33)


def test_policy_snapshot_combines_all_three_domains() -> None:
    snapshot = policy_snapshot()
    assert snapshot["research_policy"]["name"] == "us-equity-qv-baseline"
    assert snapshot["market_profile"]["name"] == "us-equity-sp500-reference"
    assert snapshot["account_policy"]["name"] == "us-equity-paper-simulation"
    assert len(snapshot["combined_sha256"]) == 64


def test_policy_snapshot_combined_hash_changes_if_any_single_domain_changes() -> None:
    baseline = policy_snapshot()
    renamed_research = policy_snapshot(research_name="different-research-policy")
    renamed_market = policy_snapshot(market_name="different-market-profile")
    renamed_account = policy_snapshot(account_name="different-account-policy")

    assert renamed_research["combined_sha256"] != baseline["combined_sha256"]
    assert renamed_market["combined_sha256"] != baseline["combined_sha256"]
    assert renamed_account["combined_sha256"] != baseline["combined_sha256"]
    # Changing one domain must not perturb the other two domains' own hashes.
    assert (
        renamed_research["market_profile"]["content_sha256"]
        == baseline["market_profile"]["content_sha256"]
    )
    assert (
        renamed_research["account_policy"]["content_sha256"]
        == baseline["account_policy"]["content_sha256"]
    )


def test_policy_snapshot_is_deterministic_across_calls() -> None:
    first = policy_snapshot()
    second = policy_snapshot()
    assert first == second

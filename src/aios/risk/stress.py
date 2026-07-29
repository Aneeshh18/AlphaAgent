"""Deterministic, advisory stress review for checksum-protected paper proposals.

The stress engine is deliberately separate from proposal approval and paper
execution.  It reads a checksum-valid simulation proposal, account state, and
point-in-time evidence; it never changes investment policy, account state,
forward-trial state, incidents, or DuckDB.

Scenario coefficients live in a versioned JSON bundle.  Historical labels are
calibration descriptions only.  Every result states whether a value is
source-anchored, an explicit AIOS sensitivity, or unavailable because evidence
failed closed.
"""

from __future__ import annotations

import hmac
import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from math import isfinite, sqrt
from pathlib import Path
from statistics import stdev
from typing import Any
from uuid import uuid4

from aios.backtest.portfolio import PortfolioBook
from aios.factors.common import factor_price_from_row
from aios.factors.market_factors import (
    REQUIRED_PRICE_OBSERVATIONS,
    TRADING_SESSIONS_PER_YEAR,
    _daily_total_returns,
)
from aios.forward import read_forward_trial, require_registered_forward_proposal
from aios.market_calendar import us_equity_sessions
from aios.paper import (
    ACCOUNT_DOCUMENT_KIND,
    ACCOUNT_SCHEMA_VERSION,
    PROPOSAL_DOCUMENT_KIND,
    PROPOSAL_SCHEMA_VERSION,
    PaperDocument,
    canonical_payload_sha256,
    read_paper_document,
)
from aios.risk.policy import PortfolioRiskPolicy
from aios.storage.store import Store, store_scope

STRESS_DOCUMENT_KIND = "aios.stress-report"
STRESS_DOCUMENT_SCHEMA_VERSION = 1
STRESS_REPORT_SCHEMA_VERSION = 1
STRESS_SCENARIO_BUNDLE_SCHEMA_VERSION = 1
DEFAULT_SCENARIO_BUNDLE = Path(__file__).with_name("scenarios") / "us_equity_reference_v1.json"
DEFAULT_STRESS_REPORT_DIRECTORY = Path("data/stress/reports")
_EPSILON = 1e-12
_SCENARIO_TYPES = {
    "fixed_return",
    "sector_template",
    "volatility_correlation",
    "evidence_withholding_demonstration",
}
_EVIDENCE_CATEGORIES = ("identity", "sector", "liquidity", "price", "revenue_fact")
_STRESS_SOURCE_FILES = (
    "src/aios/risk/stress.py",
    "src/aios/factors/common.py",
    "src/aios/factors/market_factors.py",
    "src/aios/market_calendar.py",
    "src/aios/paper.py",
    "src/aios/risk/policy.py",
    "src/aios/backtest/portfolio.py",
    "src/aios/storage/store.py",
    "src/aios/forward.py",
)


@dataclass(frozen=True, init=False)
class StressScenarioBundle:
    """Validated immutable scenario policy and its canonical identity."""

    _canonical_json: str
    payload_sha256: str

    def __init__(self, payload: dict[str, Any], payload_sha256: str | None = None) -> None:
        _validate_scenario_bundle(payload)
        canonical_json = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        frozen_payload = json.loads(canonical_json)
        actual_sha256 = canonical_payload_sha256(frozen_payload)
        if payload_sha256 is not None and not hmac.compare_digest(payload_sha256, actual_sha256):
            raise ValueError("stress scenario bundle checksum mismatch")
        object.__setattr__(self, "_canonical_json", canonical_json)
        object.__setattr__(self, "payload_sha256", actual_sha256)

    @property
    def payload(self) -> dict[str, Any]:
        """Return a detached copy so callers cannot mutate the hashed bundle."""
        return json.loads(self._canonical_json)

    @property
    def scenarios(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.payload["scenarios"])

    @property
    def evidence_policy(self) -> dict[str, int]:
        return dict(self.payload["evidence_policy"])


@dataclass(frozen=True)
class StressPosition:
    """One proposal target, explicitly not an executed holding."""

    ticker: str
    security_id: str
    sector: str | None
    target_weight: float
    factor_rank: int
    average_daily_dollar_volume: float | None
    liquidity_observations: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProposalStressInput:
    """Validated account/proposal inputs used by every scenario."""

    account_id: str
    account_payload_sha256: str
    proposal_id: str
    proposal_payload_sha256: str
    decision_date: str
    scheduled_simulation_date: str
    market: str
    universe_id: str
    strategy: str
    equity_basis: float
    peak_equity: float
    cash_weight: float
    positions: tuple[StressPosition, ...]
    risk_policy: PortfolioRiskPolicy
    readiness_sha256: str
    decision_evidence_sha256: str

    def source_dict(self) -> dict[str, Any]:
        return {
            "kind": "proposal_targets",
            "account_id": self.account_id,
            "account_payload_sha256": self.account_payload_sha256,
            "proposal_id": self.proposal_id,
            "proposal_payload_sha256": self.proposal_payload_sha256,
            "decision_date": self.decision_date,
            "scheduled_simulation_date": self.scheduled_simulation_date,
            "market": self.market,
            "universe_id": self.universe_id,
            "strategy": self.strategy,
            "mode": "simulation_only",
            "positions_are_holdings": False,
            "notice": (
                "Targets are a checksum-protected proposal. They are not holdings, fills, "
                "orders, or a personal investment recommendation."
            ),
        }


@dataclass(frozen=True)
class TargetStressEvidence:
    """Bounded evidence supporting one proposal target."""

    ticker: str
    security_id: str
    sector: str | None
    target_weight: float
    factor_rank: int
    average_daily_dollar_volume: float | None
    liquidity_observations: int
    reviewed_price: float | None
    reviewed_price_date: str | None
    price_observations: int
    annualized_volatility: float | None
    latest_revenue_fact_known_date: str | None
    revenue_fact_known_age_days: int | None
    price_window_sha256: str | None
    revenue_fact_evidence_sha256: str | None
    liquidity_window_start: str | None = None
    liquidity_window_end: str | None = None
    liquidity_window_sha256: str | None = None
    blockers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["blockers"] = list(self.blockers)
        return result


@dataclass(frozen=True)
class StressReviewReport:
    """Canonical deterministic stress payload and envelope checksum."""

    payload: dict[str, Any]
    payload_sha256: str

    def envelope(self) -> dict[str, Any]:
        return {
            "document_schema_version": STRESS_DOCUMENT_SCHEMA_VERSION,
            "document_kind": STRESS_DOCUMENT_KIND,
            "payload_sha256": self.payload_sha256,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class GovernedStressReview:
    """One registered-proposal review plus its optional immutable export."""

    report: StressReviewReport
    artifact_path: Path | None = None


def load_scenario_bundle(path: Path | None = None) -> StressScenarioBundle:
    """Load, validate, and hash one immutable scenario bundle."""
    source = Path(path or DEFAULT_SCENARIO_BUNDLE)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"stress scenario bundle does not exist: {source}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"stress scenario bundle is unreadable: {source}") from exc
    return StressScenarioBundle(payload)


def _resolve_governed_stress_output_path(
    project_root: Path,
    output_path: Path,
) -> Path:
    """Keep governed exports inside the dedicated immutable-report namespace."""
    root = Path(project_root).resolve()
    allowed_directory = root / DEFAULT_STRESS_REPORT_DIRECTORY
    current = root
    for component in DEFAULT_STRESS_REPORT_DIRECTORY.parts:
        current /= component
        if current.is_symlink():
            raise ValueError(
                "stress report directory cannot contain symbolic links: "
                f"{allowed_directory}"
            )

    raw_destination = Path(output_path)
    destination = (
        raw_destination.resolve()
        if raw_destination.is_absolute()
        else (root / raw_destination).resolve()
    )
    resolved_allowed_directory = allowed_directory.resolve()
    try:
        relative_destination = destination.relative_to(resolved_allowed_directory)
    except ValueError as exc:
        raise ValueError(
            "stress report output must stay under "
            f"{DEFAULT_STRESS_REPORT_DIRECTORY.as_posix()}"
        ) from exc
    if relative_destination == Path(".") or destination.suffix.lower() != ".json":
        raise ValueError(
            "stress report output must be a .json file under "
            f"{DEFAULT_STRESS_REPORT_DIRECTORY.as_posix()}"
        )
    return destination


def review_registered_paper_proposal_stress(
    project_root: Path,
    trial_path: Path,
    account_path: Path,
    proposal_path: Path,
    *,
    scenario_ids: Sequence[str] | None = None,
    bundle_path: Path | None = None,
    output_path: Path | None = None,
    db_path: Path | None = None,
) -> GovernedStressReview:
    """Review one registered proposal under a stable forward and source snapshot.

    This is the production application boundary shared by the CLI and dashboard.
    It validates forward registration before opening DuckDB, uses a read-only
    connection, and repeats the forward/account/proposal/source checks before
    returning or publishing an optional write-once report.
    """
    root = Path(project_root).resolve()
    trial_source = Path(trial_path)
    account_source = Path(account_path)
    proposal_source = Path(proposal_path)
    scenario_bundle_source = Path(bundle_path or DEFAULT_SCENARIO_BUNDLE)
    output_destination = (
        _resolve_governed_stress_output_path(root, Path(output_path))
        if output_path is not None
        else None
    )

    trial_before_initial_check = read_forward_trial(trial_source)
    initial_status = require_registered_forward_proposal(
        root,
        trial_source,
        account_source,
        proposal_source,
    )
    initial_trial = read_forward_trial(trial_source)
    if (
        not hmac.compare_digest(
            trial_before_initial_check.payload_sha256,
            initial_trial.payload_sha256,
        )
        or initial_trial.payload.get("trial_id") != initial_status.trial_id
    ):
        raise ValueError("forward trial changed during the initial governance check")

    governance_context = {
        "trial_id": initial_status.trial_id,
        "trial_payload_sha256": initial_trial.payload_sha256,
        "policy_bundle_sha256": initial_trial.payload.get("policy_bundle_sha256"),
        "policy_unchanged": initial_status.policy_unchanged,
        "proposal_registered": True,
        "check_contract": "validated before database access and again before output",
    }
    if db_path is None:
        scoped_store = store_scope(read_only=True)
    else:
        scoped_store = store_scope(Path(db_path), read_only=True)
    with scoped_store as store:
        report = review_paper_proposal_stress(
            account_source,
            proposal_source,
            store,
            project_root=root,
            scenario_ids=scenario_ids,
            bundle_path=bundle_path,
            governance_context=governance_context,
        )

    def require_final_sources_unchanged() -> None:
        trial_before_final_check = read_forward_trial(trial_source)
        final_status = require_registered_forward_proposal(
            root,
            trial_source,
            account_source,
            proposal_source,
        )
        final_trial = read_forward_trial(trial_source)
        if (
            final_status.trial_id != initial_status.trial_id
            or not final_status.policy_unchanged
            or final_trial.payload.get("trial_id") != initial_status.trial_id
            or final_trial.payload.get("policy_bundle_sha256")
            != initial_trial.payload.get("policy_bundle_sha256")
            or not hmac.compare_digest(
                trial_before_final_check.payload_sha256,
                final_trial.payload_sha256,
            )
            or not hmac.compare_digest(
                final_trial.payload_sha256,
                initial_trial.payload_sha256,
            )
        ):
            raise ValueError("forward trial changed while stress evidence was being reviewed")
        require_stress_report_sources_unchanged(
            report,
            account_source,
            proposal_source,
            project_root=root,
            bundle_path=scenario_bundle_source,
        )

    require_final_sources_unchanged()
    artifact_path = None
    if output_destination is not None:
        artifact_path = write_stress_report(
            output_destination,
            report,
            before_publish=require_final_sources_unchanged,
        )
        try:
            require_final_sources_unchanged()
        except Exception:
            try:
                _rollback_stress_report_publication(artifact_path, report)
            except Exception as rollback_error:
                raise RuntimeError(
                    "stress sources changed after publication and the new artifact "
                    "could not be safely rolled back; quarantine it for review"
                ) from rollback_error
            raise
    return GovernedStressReview(report=report, artifact_path=artifact_path)


def review_paper_proposal_stress(
    account_path: Path,
    proposal_path: Path,
    store: Store,
    *,
    project_root: Path,
    scenario_ids: Sequence[str] | None = None,
    bundle_path: Path | None = None,
    source_identity: Mapping[str, Any] | None = None,
    governance_context: Mapping[str, Any] | None = None,
) -> StressReviewReport:
    """Build one read-only deterministic report from a paper proposal.

    Account and proposal checksums are verified both before and after evidence
    collection.  This preserves the paper workflow's compare-and-swap posture:
    concurrent source changes cause a refusal rather than a report over mixed
    inputs.
    """
    account = read_paper_document(Path(account_path), expected_kind=ACCOUNT_DOCUMENT_KIND)
    proposal = read_paper_document(Path(proposal_path), expected_kind=PROPOSAL_DOCUMENT_KIND)
    bundle = load_scenario_bundle(bundle_path)
    live_source_identity = source_identity is None
    identity = (
        dict(source_identity or {})
        if not live_source_identity
        else build_stress_source_identity(Path(project_root))
    )
    stress_input = build_proposal_stress_input(account, proposal, store)
    evidence = collect_target_stress_evidence(
        store,
        stress_input,
        evidence_policy=bundle.evidence_policy,
    )
    _require_source_documents_unchanged(account, proposal)
    report = evaluate_proposal_stress(
        stress_input,
        evidence,
        bundle,
        scenario_ids=scenario_ids,
        source_identity=identity,
        governance_context=governance_context,
    )
    _require_source_documents_unchanged(account, proposal)
    current_bundle = load_scenario_bundle(bundle_path)
    if not hmac.compare_digest(
        bundle.payload_sha256,
        current_bundle.payload_sha256,
    ):
        raise ValueError("stress scenario bundle changed while the report was being built")
    if live_source_identity:
        current_identity = build_stress_source_identity(Path(project_root))
        if not hmac.compare_digest(
            str(identity["source_bundle_sha256"]),
            str(current_identity["source_bundle_sha256"]),
        ):
            raise ValueError("stress calculation source changed while the report was being built")
    return report


def build_proposal_stress_input(
    account: PaperDocument,
    proposal: PaperDocument,
    store: Store,
) -> ProposalStressInput:
    """Validate proposal/account governance and construct immutable stress input."""
    if account.kind != ACCOUNT_DOCUMENT_KIND:
        raise ValueError("stress review requires a paper account document")
    if proposal.kind != PROPOSAL_DOCUMENT_KIND:
        raise ValueError("stress review requires a paper proposal document")

    account_payload = account.payload
    proposal_payload = proposal.payload
    if account_payload.get("account_schema_version") != ACCOUNT_SCHEMA_VERSION:
        raise ValueError("unsupported paper account schema")
    if not isinstance(account_payload.get("portfolio"), dict):
        raise ValueError("paper account is missing portfolio state")
    if not isinstance(account_payload.get("executions"), list) or not isinstance(
        account_payload.get("audit_events"), list
    ):
        raise ValueError("paper account audit state is invalid")
    if proposal_payload.get("proposal_schema_version") != PROPOSAL_SCHEMA_VERSION:
        raise ValueError("unsupported paper proposal schema")
    if not isinstance(proposal_payload.get("targets"), list):
        raise ValueError("paper proposal payload is incomplete")
    if account_payload.get("mode") != "simulation_only":
        raise ValueError("stress review accepts simulation-only accounts")
    if account_payload.get("broker_connected") is not False:
        raise ValueError("stress review refuses an account with a broker connection")
    if proposal_payload.get("mode") != "simulation_only":
        raise ValueError("stress review accepts simulation-only proposals")
    if proposal_payload.get("status") != "approved_for_supervised_simulation":
        raise ValueError("stress review requires an approved supervised-simulation proposal")
    if proposal_payload.get("account_id") != account_payload.get("account_id"):
        raise ValueError("proposal belongs to a different paper account")
    if not hmac.compare_digest(
        str(proposal_payload.get("account_payload_sha256", "")),
        account.payload_sha256,
    ):
        raise ValueError("paper account changed after proposal; create a new proposal")
    if proposal_payload.get("market") != "US" or account_payload.get("market") != "US":
        raise ValueError("stress-review v1 supports the governed U.S. paper sandbox only")
    readiness = proposal_payload.get("readiness")
    if not isinstance(readiness, dict) or readiness.get("ready") is not True:
        raise ValueError("proposal readiness is not approved")
    risk_assessment = proposal_payload.get("risk_assessment")
    if not isinstance(risk_assessment, dict) or risk_assessment.get("approved") is not True:
        raise ValueError("proposal risk assessment is not approved")
    risk_policy_payload = proposal_payload.get("risk_policy")
    if not isinstance(risk_policy_payload, dict):
        raise ValueError("proposal is missing its risk policy")
    risk_policy = PortfolioRiskPolicy(**risk_policy_payload)

    expected_decision_hash = canonical_payload_sha256(
        {
            "factor_evidence_sha256": proposal_payload.get("factor_evidence_sha256"),
            "targets": proposal_payload.get("targets"),
            "exit_liquidity_evidence": proposal_payload.get("exit_liquidity_evidence"),
            "selection_skips": proposal_payload.get("selection_skips"),
            "risk_assessment": risk_assessment,
        }
    )
    if not hmac.compare_digest(
        expected_decision_hash,
        str(proposal_payload.get("decision_evidence_sha256", "")),
    ):
        raise ValueError("proposal decision-evidence checksum is inconsistent")

    raw_targets = proposal_payload.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("proposal has no stressable targets")
    positions = tuple(_stress_position(raw, index) for index, raw in enumerate(raw_targets, 1))
    tickers = [position.ticker for position in positions]
    security_ids = [position.security_id for position in positions]
    if len(tickers) != len(set(tickers)):
        raise ValueError("proposal stress targets contain duplicate tickers")
    if len(security_ids) != len(set(security_ids)):
        raise ValueError("proposal stress targets contain duplicate stable security IDs")
    total_weight = sum(position.target_weight for position in positions)
    if total_weight > 1.0 + _EPSILON:
        raise ValueError("proposal target weights exceed 100 percent")
    if total_weight <= _EPSILON:
        raise ValueError("proposal has no positive target exposure")

    portfolio = account_payload.get("portfolio")
    if not isinstance(portfolio, dict):
        raise ValueError("paper account is missing portfolio state")
    book = PortfolioBook.from_state(store, portfolio)
    equity_basis = _finite_positive(book.equity, "paper account equity")
    peak_equity = _finite_positive(book.peak_equity, "paper account peak equity")
    if peak_equity + _EPSILON < equity_basis:
        raise ValueError("paper account peak equity cannot be below current equity")

    return ProposalStressInput(
        account_id=str(account_payload["account_id"]),
        account_payload_sha256=account.payload_sha256,
        proposal_id=str(proposal_payload["proposal_id"]),
        proposal_payload_sha256=proposal.payload_sha256,
        decision_date=str(proposal_payload["decision_date"]),
        scheduled_simulation_date=str(proposal_payload["scheduled_simulation_date"]),
        market=str(proposal_payload["market"]),
        universe_id=str(proposal_payload["universe_id"]),
        strategy=str(proposal_payload["strategy"]),
        equity_basis=equity_basis,
        peak_equity=peak_equity,
        cash_weight=_clean_float(max(0.0, 1.0 - total_weight)),
        positions=tuple(
            sorted(
                positions,
                key=lambda position: (
                    position.factor_rank,
                    position.ticker,
                    position.security_id,
                ),
            )
        ),
        risk_policy=risk_policy,
        readiness_sha256=canonical_payload_sha256(readiness),
        decision_evidence_sha256=expected_decision_hash,
    )


def _proposal_liquidity_evidence_rows(
    store: Store,
    security_id: str,
    as_of: str,
    *,
    observations: int,
) -> list[dict[str, Any]]:
    """Rebuild the proposal's action-safe ADV window with row-level lineage."""
    return store.query(
        """
        WITH deduplicated AS (
            SELECT ticker, security_id, date, close, volume, source, fetched_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY security_id, date
                       ORDER BY fetched_at DESC, ticker DESC
                   ) AS duplicate_rank
            FROM prices
            WHERE security_id = ?
              AND date <= CAST(? AS DATE)
              AND actions_complete = TRUE
              AND close_split_adjusted IS NOT NULL
              AND close > 0
              AND volume > 0
        ), recent AS (
            SELECT ticker, security_id, date, close, volume, source, fetched_at,
                   ROW_NUMBER() OVER (ORDER BY date DESC) AS observation_rank
            FROM deduplicated
            WHERE duplicate_rank = 1
        )
        SELECT ticker, security_id, date, close, volume, source, fetched_at
        FROM recent
        WHERE observation_rank <= ?
        ORDER BY date
        """,
        (security_id, as_of, observations),
    )


def _validated_liquidity_evidence(
    rows: Sequence[Mapping[str, Any]],
    *,
    security_id: str,
    decision_date: date,
    required_observations: int,
) -> tuple[float | None, str | None, str | None, str | None, tuple[str, ...]]:
    """Validate and hash the exact PIT rows supporting stressed exit capacity."""
    if not rows:
        return None, None, None, None, ("window_unavailable",)

    canonical_rows = [
        {
            "ticker": row.get("ticker"),
            "security_id": row.get("security_id"),
            "date": _json_value(row.get("date")),
            "close": row.get("close"),
            "volume": row.get("volume"),
            "source": row.get("source"),
            "fetched_at": _json_value(row.get("fetched_at")),
        }
        for row in rows
    ]
    window_sha256 = canonical_payload_sha256({"rows": canonical_rows})
    blockers: list[str] = []
    if len(rows) != required_observations:
        blockers.append(f"minimum_observations:{required_observations}")

    try:
        row_dates = [_as_date(row.get("date")) for row in rows]
    except (TypeError, ValueError):
        return None, None, None, window_sha256, ("invalid_dates",)
    window_start = min(row_dates).isoformat()
    window_end = max(row_dates).isoformat()
    if row_dates != sorted(row_dates) or len(set(row_dates)) != len(row_dates):
        blockers.append("noncanonical_dates")
    expected_dates = us_equity_sessions(row_dates[0], decision_date + timedelta(days=1))
    if row_dates != expected_dates:
        blockers.append("noncontiguous_sessions")
    if row_dates[-1] != decision_date:
        blockers.append("latest_session_mismatch")

    notionals: list[float] = []
    for row in rows:
        if str(row.get("security_id", "")).strip() != security_id:
            blockers.append("security_identity_mismatch")
        try:
            close = _finite_positive(row.get("close"), "liquidity close")
            volume = _finite_positive(row.get("volume"), "liquidity volume")
        except ValueError:
            blockers.append("invalid_close_or_volume")
            continue
        notionals.append(close * volume)

    blockers = sorted(set(blockers))
    verified_adv = (
        sum(notionals) / len(notionals)
        if not blockers and len(notionals) == required_observations
        else None
    )
    return (
        float(verified_adv) if verified_adv is not None else None,
        window_start,
        window_end,
        window_sha256,
        tuple(blockers),
    )


def collect_target_stress_evidence(
    store: Store,
    stress_input: ProposalStressInput,
    *,
    evidence_policy: Mapping[str, int],
) -> tuple[TargetStressEvidence, ...]:
    """Collect bounded PIT evidence, keeping every failure explicit."""
    minimum_price_observations = int(evidence_policy["minimum_price_observations"])
    if minimum_price_observations != REQUIRED_PRICE_OBSERVATIONS:
        raise ValueError(
            "stress price policy must match the certified market-factor observation window"
        )
    maximum_price_staleness = int(evidence_policy["maximum_price_staleness_days"])
    maximum_revenue_fact_age = int(
        evidence_policy["maximum_revenue_fact_known_age_days"]
    )
    minimum_liquidity_observations = int(evidence_policy["minimum_liquidity_observations"])
    decision_date = date.fromisoformat(stress_input.decision_date)
    evidence: list[TargetStressEvidence] = []

    for position in stress_input.positions:
        blockers: list[str] = []
        liquidity_window_start: str | None = None
        liquidity_window_end: str | None = None
        liquidity_window_sha256: str | None = None
        if not position.sector:
            blockers.append("sector:missing_classification")
        if (
            position.average_daily_dollar_volume is None
            or not isfinite(position.average_daily_dollar_volume)
            or position.average_daily_dollar_volume <= 0
        ):
            blockers.append("liquidity:missing_average_daily_dollar_volume")
        if position.liquidity_observations < minimum_liquidity_observations:
            blockers.append(
                f"liquidity:minimum_observations:{minimum_liquidity_observations}"
            )
        liquidity_rows = _proposal_liquidity_evidence_rows(
            store,
            position.security_id,
            stress_input.decision_date,
            observations=minimum_liquidity_observations,
        )
        (
            verified_adv,
            liquidity_window_start,
            liquidity_window_end,
            liquidity_window_sha256,
            liquidity_blockers,
        ) = _validated_liquidity_evidence(
            liquidity_rows,
            security_id=position.security_id,
            decision_date=decision_date,
            required_observations=minimum_liquidity_observations,
        )
        blockers.extend(f"liquidity:{item}" for item in liquidity_blockers)
        if (
            verified_adv is not None
            and position.average_daily_dollar_volume is not None
            and abs(verified_adv - position.average_daily_dollar_volume)
            > max(_EPSILON, abs(position.average_daily_dollar_volume) * 1e-12)
        ):
            blockers.append("liquidity:proposal_evidence_mismatch")

        resolved_security_id = store.security_id_for_ticker(
            position.ticker,
            stress_input.decision_date,
        )
        if resolved_security_id is None:
            blockers.append("identity:missing_stable_security")
        elif resolved_security_id != position.security_id:
            blockers.append("identity:proposal_security_mismatch")

        price_rows: list[dict[str, Any]] = []
        price_window_sha256: str | None = None
        reviewed_price: float | None = None
        reviewed_price_date: str | None = None
        annualized_volatility: float | None = None
        price_observations = 0
        price_rows = store.pit_factor_price_history(
            position.ticker,
            stress_input.decision_date,
            observations=minimum_price_observations,
        )
        price_observations = len(price_rows)
        price_window_sha256 = canonical_payload_sha256(
            {"rows": [_canonical_price_evidence_row(row) for row in price_rows]}
        )
        (
            annualized_volatility,
            reviewed_price_date,
            price_validation_blockers,
        ) = _validated_volatility_from_rows(
            price_rows,
            security_id=position.security_id,
            decision_date=decision_date,
            required_observations=minimum_price_observations,
            maximum_staleness_days=maximum_price_staleness,
        )
        blockers.extend(f"price:{item}" for item in price_validation_blockers)

        if price_rows:
            try:
                reviewed_price = factor_price_from_row(price_rows[-1])
            except (TypeError, ValueError, OverflowError):
                reviewed_price = None
                blockers.append("price:invalid_reviewed_close")
            else:
                if (
                    reviewed_price is None
                    or not isfinite(reviewed_price)
                    or reviewed_price <= 0
                ):
                    reviewed_price = None
                    blockers.append("price:reviewed_close_unavailable")
        else:
            blockers.append("price:window_unavailable")

        revenue_fact_rows: list[dict[str, Any]] = []
        revenue_fact_evidence_sha256: str | None = None
        latest_revenue_fact_known_date: str | None = None
        revenue_fact_known_age_days: int | None = None
        revenue_fact_rows = store.pit_factor_fundamentals(
            position.ticker,
            stress_input.decision_date,
            ["revenue"],
        )
        if revenue_fact_rows:
            revenue_fact_evidence_sha256 = canonical_payload_sha256(
                {
                    "rows": [
                        _canonical_revenue_fact_evidence_row(row)
                        for row in revenue_fact_rows
                    ]
                }
            )
            try:
                known_dates = [
                    _as_date(row["as_of_date"]) for row in revenue_fact_rows
                ]
            except (KeyError, TypeError, ValueError):
                blockers.append("revenue_fact:invalid_known_date")
            else:
                latest_known = max(known_dates)
                latest_revenue_fact_known_date = latest_known.isoformat()
                revenue_fact_known_age_days = (decision_date - latest_known).days
                if (
                    revenue_fact_known_age_days < 0
                    or revenue_fact_known_age_days > maximum_revenue_fact_age
                ):
                    blockers.append(
                        f"revenue_fact:stale_latest_known:{revenue_fact_known_age_days}"
                    )
        else:
            blockers.append("revenue_fact:pit_revenue_fact_unavailable")

        evidence.append(
            TargetStressEvidence(
                ticker=position.ticker,
                security_id=position.security_id,
                sector=position.sector,
                target_weight=position.target_weight,
                factor_rank=position.factor_rank,
                average_daily_dollar_volume=position.average_daily_dollar_volume,
                liquidity_observations=position.liquidity_observations,
                reviewed_price=reviewed_price,
                reviewed_price_date=reviewed_price_date,
                price_observations=price_observations,
                annualized_volatility=annualized_volatility,
                latest_revenue_fact_known_date=latest_revenue_fact_known_date,
                revenue_fact_known_age_days=revenue_fact_known_age_days,
                price_window_sha256=price_window_sha256,
                revenue_fact_evidence_sha256=revenue_fact_evidence_sha256,
                liquidity_window_start=liquidity_window_start,
                liquidity_window_end=liquidity_window_end,
                liquidity_window_sha256=liquidity_window_sha256,
                blockers=tuple(sorted(set(blockers))),
            )
        )

    return tuple(sorted(evidence, key=lambda row: (row.factor_rank, row.ticker, row.security_id)))


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a SHA-256 hex digest")
    candidate = value.strip().lower()
    if len(candidate) != 64 or any(
        character not in "0123456789abcdef" for character in candidate
    ):
        raise ValueError(f"{label} must be a SHA-256 hex digest")
    return candidate


def _validate_proposal_stress_input(
    stress_input: ProposalStressInput,
    *,
    evidence_policy: Mapping[str, int],
) -> None:
    """Refuse internally inconsistent public evaluator inputs."""
    if not isinstance(stress_input, ProposalStressInput):
        raise ValueError("stress input must be a ProposalStressInput")
    for field in ("account_id", "proposal_id", "universe_id", "strategy"):
        value = getattr(stress_input, field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"stress input is missing {field}")
    for field in (
        "account_payload_sha256",
        "proposal_payload_sha256",
        "readiness_sha256",
        "decision_evidence_sha256",
    ):
        _require_sha256(getattr(stress_input, field), f"stress input {field}")

    decision_date = _as_date(stress_input.decision_date)
    scheduled_date = _as_date(stress_input.scheduled_simulation_date)
    if scheduled_date <= decision_date:
        raise ValueError("scheduled simulation date must follow the decision date")
    if stress_input.market != "US":
        raise ValueError("proposal stress v1 requires the U.S. paper sandbox")

    equity = _finite_positive(stress_input.equity_basis, "stress equity basis")
    peak_equity = _finite_positive(stress_input.peak_equity, "stress peak equity")
    if peak_equity + _EPSILON < equity:
        raise ValueError("stress peak equity cannot be below current equity")
    cash_weight = _finite_number(stress_input.cash_weight, "stress cash weight")
    if cash_weight < -_EPSILON or cash_weight > 1.0 + _EPSILON:
        raise ValueError("stress cash weight must be between zero and one")
    if not isinstance(stress_input.risk_policy, PortfolioRiskPolicy):
        raise ValueError("stress input requires a validated portfolio risk policy")
    if not stress_input.positions:
        raise ValueError("stress input has no proposal targets")

    canonical_positions = tuple(
        sorted(
            stress_input.positions,
            key=lambda position: (
                position.factor_rank,
                position.ticker,
                position.security_id,
            ),
        )
    )
    if stress_input.positions != canonical_positions:
        raise ValueError("stress proposal targets must use canonical rank and identity order")

    tickers: set[str] = set()
    security_ids: set[str] = set()
    factor_ranks: set[int] = set()
    total_weight = 0.0
    sector_weights: dict[str, float] = {}
    minimum_liquidity_observations = int(evidence_policy["minimum_liquidity_observations"])
    for position in stress_input.positions:
        if not isinstance(position, StressPosition):
            raise ValueError("stress proposal target must be a StressPosition")
        ticker = position.ticker
        security_id = position.security_id
        if not ticker or ticker != ticker.strip().upper():
            raise ValueError("stress proposal target ticker must be canonical uppercase")
        if not security_id or security_id != security_id.strip():
            raise ValueError(f"{ticker} is missing a stable security ID")
        if ticker in tickers or security_id in security_ids:
            raise ValueError("stress proposal targets contain duplicate identities")
        tickers.add(ticker)
        security_ids.add(security_id)
        if (
            isinstance(position.factor_rank, bool)
            or not isinstance(position.factor_rank, int)
            or position.factor_rank < 1
            or position.factor_rank in factor_ranks
        ):
            raise ValueError(f"{ticker} factor rank must be a unique positive integer")
        factor_ranks.add(position.factor_rank)
        weight = _finite_positive(position.target_weight, f"{ticker} target weight")
        if weight > stress_input.risk_policy.maximum_position_weight + _EPSILON:
            raise ValueError(f"{ticker} exceeds the proposal position limit")
        total_weight += weight
        if not isinstance(position.sector, str) or not position.sector.strip():
            raise ValueError(f"{ticker} is missing its proposal sector")
        sector_weights[position.sector] = sector_weights.get(position.sector, 0.0) + weight
        _finite_positive(
            position.average_daily_dollar_volume,
            f"{ticker} average daily dollar volume",
        )
        if (
            isinstance(position.liquidity_observations, bool)
            or not isinstance(position.liquidity_observations, int)
            or position.liquidity_observations < minimum_liquidity_observations
        ):
            raise ValueError(
                f"{ticker} needs at least {minimum_liquidity_observations} liquidity observations"
            )

    rules = stress_input.risk_policy
    current_drawdown = max(0.0, 1.0 - equity / peak_equity)
    if current_drawdown > rules.maximum_drawdown + _EPSILON:
        raise ValueError("stress account drawdown conflicts with its approved risk policy")
    if not rules.minimum_positions <= len(stress_input.positions) <= rules.maximum_positions:
        raise ValueError("stress proposal target count conflicts with its risk policy")
    if total_weight > rules.maximum_gross_exposure + _EPSILON:
        raise ValueError("stress proposal exposure exceeds its risk policy")
    if any(weight > rules.maximum_sector_weight + _EPSILON for weight in sector_weights.values()):
        raise ValueError("stress proposal sector exposure exceeds its risk policy")
    if abs((total_weight + cash_weight) - 1.0) > 1e-9:
        raise ValueError("stress proposal weights and cash do not reconcile to equity")


def _validate_target_stress_evidence(
    stress_input: ProposalStressInput,
    evidence: Sequence[TargetStressEvidence],
    *,
    evidence_policy: Mapping[str, int],
) -> None:
    """Validate evidence field relationships before any loss calculation."""
    if len(evidence) != len(stress_input.positions):
        raise ValueError("stress evidence must contain exactly one row per proposal target")
    tickers = [row.ticker for row in evidence]
    if len(tickers) != len(set(tickers)):
        raise ValueError("stress evidence contains duplicate target rows")

    decision_date = _as_date(stress_input.decision_date)
    minimum_price_observations = int(evidence_policy["minimum_price_observations"])
    minimum_liquidity_observations = int(evidence_policy["minimum_liquidity_observations"])
    maximum_price_staleness = int(evidence_policy["maximum_price_staleness_days"])
    maximum_revenue_age = int(evidence_policy["maximum_revenue_fact_known_age_days"])
    for row in evidence:
        if not isinstance(row, TargetStressEvidence):
            raise ValueError("stress evidence row must be TargetStressEvidence")
        if not row.ticker or row.ticker != row.ticker.strip().upper():
            raise ValueError("stress evidence ticker must be canonical uppercase")
        if not row.security_id or row.security_id != row.security_id.strip():
            raise ValueError(f"{row.ticker} stress evidence is missing a stable security ID")
        _finite_positive(row.target_weight, f"{row.ticker} evidence target weight")
        if (
            isinstance(row.factor_rank, bool)
            or not isinstance(row.factor_rank, int)
            or row.factor_rank < 1
        ):
            raise ValueError(f"{row.ticker} evidence factor rank must be positive")
        if (
            isinstance(row.liquidity_observations, bool)
            or not isinstance(row.liquidity_observations, int)
            or row.liquidity_observations < 0
        ):
            raise ValueError(f"{row.ticker} liquidity observations must be non-negative")
        if (
            isinstance(row.price_observations, bool)
            or not isinstance(row.price_observations, int)
            or row.price_observations < 0
        ):
            raise ValueError(f"{row.ticker} price observations must be non-negative")
        if tuple(row.blockers) != tuple(sorted(set(row.blockers))) or any(
            not isinstance(blocker, str) or not blocker.strip() for blocker in row.blockers
        ):
            raise ValueError(f"{row.ticker} stress evidence blockers are not canonical")

        liquidity_blocked = any(
            blocker.startswith("liquidity:") for blocker in row.blockers
        )
        if not liquidity_blocked:
            _finite_positive(
                row.average_daily_dollar_volume,
                f"{row.ticker} evidence liquidity",
            )
            if row.liquidity_observations < minimum_liquidity_observations:
                raise ValueError(f"{row.ticker} liquidity evidence is below policy coverage")
            if not row.liquidity_window_start or not row.liquidity_window_end:
                raise ValueError(f"{row.ticker} liquidity evidence is missing its date window")
            liquidity_start = _as_date(row.liquidity_window_start)
            liquidity_end = _as_date(row.liquidity_window_end)
            if liquidity_start > liquidity_end or liquidity_end != decision_date:
                raise ValueError(f"{row.ticker} liquidity evidence window is not decision-scoped")
            _require_sha256(
                row.liquidity_window_sha256,
                f"{row.ticker} liquidity window",
            )

        price_blocked = any(blocker.startswith("price:") for blocker in row.blockers)
        price_complete = all(
            value is not None
            for value in (
                row.reviewed_price,
                row.reviewed_price_date,
                row.annualized_volatility,
                row.price_window_sha256,
            )
        ) and row.price_observations >= minimum_price_observations
        if not price_complete and not price_blocked:
            raise ValueError(f"{row.ticker} incomplete price evidence is not blocked")
        if price_complete:
            _finite_positive(row.reviewed_price, f"{row.ticker} reviewed price")
            _finite_positive(row.annualized_volatility, f"{row.ticker} annualized volatility")
            price_date = _as_date(row.reviewed_price_date)
            staleness = (decision_date - price_date).days
            if staleness < 0 or (staleness > maximum_price_staleness and not price_blocked):
                raise ValueError(f"{row.ticker} price evidence is outside the decision window")
            _require_sha256(row.price_window_sha256, f"{row.ticker} price window")

        revenue_blocked = any(
            blocker.startswith("revenue_fact:") for blocker in row.blockers
        )
        revenue_values = (
            row.latest_revenue_fact_known_date,
            row.revenue_fact_known_age_days,
            row.revenue_fact_evidence_sha256,
        )
        revenue_complete = all(value is not None for value in revenue_values)
        if not revenue_complete and not revenue_blocked:
            raise ValueError(f"{row.ticker} incomplete revenue evidence is not blocked")
        if revenue_complete:
            revenue_date = _as_date(row.latest_revenue_fact_known_date)
            expected_age = (decision_date - revenue_date).days
            if (
                isinstance(row.revenue_fact_known_age_days, bool)
                or not isinstance(row.revenue_fact_known_age_days, int)
                or row.revenue_fact_known_age_days != expected_age
                or expected_age < 0
            ):
                raise ValueError(f"{row.ticker} revenue evidence age is inconsistent")
            if expected_age > maximum_revenue_age and not revenue_blocked:
                raise ValueError(f"{row.ticker} stale revenue evidence is not blocked")
            _require_sha256(
                row.revenue_fact_evidence_sha256,
                f"{row.ticker} revenue evidence",
            )


def _validate_stress_source_identity(source_identity: Mapping[str, Any]) -> None:
    if not isinstance(source_identity, Mapping):
        raise ValueError("stress source identity must be a mapping")
    _require_sha256(source_identity.get("source_bundle_sha256"), "stress source bundle")
    source_files = source_identity.get("source_files")
    if not isinstance(source_files, Mapping) or not source_files:
        raise ValueError("stress source identity is missing its file manifest")
    for path, digest in source_files.items():
        if not isinstance(path, str) or not path.strip():
            raise ValueError("stress source identity contains an invalid path")
        _require_sha256(digest, f"stress source file {path}")
    expected_bundle_sha256 = canonical_payload_sha256({"files": dict(source_files)})
    if not hmac.compare_digest(
        source_identity["source_bundle_sha256"],
        expected_bundle_sha256,
    ):
        raise ValueError("stress source bundle does not match its file manifest")


def _validate_governance_context(governance_context: Mapping[str, Any] | None) -> None:
    if governance_context is None:
        return
    if not isinstance(governance_context, Mapping):
        raise ValueError("stress governance context must be a mapping")
    if not isinstance(governance_context.get("trial_id"), str) or not str(
        governance_context["trial_id"]
    ).strip():
        raise ValueError("stress governance context is missing its trial ID")
    _require_sha256(
        governance_context.get("trial_payload_sha256"),
        "stress forward trial",
    )
    _require_sha256(
        governance_context.get("policy_bundle_sha256"),
        "stress forward policy bundle",
    )
    if governance_context.get("policy_unchanged") is not True:
        raise ValueError("stress review requires unchanged forward policy")
    if governance_context.get("proposal_registered") is not True:
        raise ValueError("stress review requires a registered proposal")


def evaluate_proposal_stress(
    stress_input: ProposalStressInput,
    evidence: Sequence[TargetStressEvidence],
    bundle: StressScenarioBundle,
    *,
    scenario_ids: Sequence[str] | None = None,
    source_identity: Mapping[str, Any],
    governance_context: Mapping[str, Any] | None = None,
) -> StressReviewReport:
    """Evaluate selected scenarios with deterministic ordering and hashes."""
    bundle_payload = bundle.payload
    _validate_scenario_bundle(bundle_payload)
    evidence_policy = bundle_payload["evidence_policy"]
    _validate_proposal_stress_input(stress_input, evidence_policy=evidence_policy)
    _validate_target_stress_evidence(
        stress_input,
        evidence,
        evidence_policy=evidence_policy,
    )
    _validate_stress_source_identity(source_identity)
    _validate_governance_context(governance_context)
    if not hmac.compare_digest(
        bundle.payload_sha256,
        canonical_payload_sha256(bundle_payload),
    ):
        raise ValueError("stress scenario bundle changed after validation")
    evidence_by_ticker = {row.ticker: row for row in evidence}
    if len(evidence_by_ticker) != len(stress_input.positions):
        raise ValueError("stress evidence does not cover unique proposal targets")
    if set(evidence_by_ticker) != {position.ticker for position in stress_input.positions}:
        raise ValueError("stress evidence does not match the proposal target set")
    for position in stress_input.positions:
        row = evidence_by_ticker[position.ticker]
        if row.security_id != position.security_id:
            raise ValueError(f"{position.ticker} stress evidence has a different security ID")
        if abs(row.target_weight - position.target_weight) > _EPSILON:
            raise ValueError(f"{position.ticker} stress evidence has a different target weight")
        if row.sector != position.sector:
            raise ValueError(f"{position.ticker} stress evidence has a different sector")
        if row.factor_rank != position.factor_rank:
            raise ValueError(f"{position.ticker} stress evidence has a different factor rank")
        if row.liquidity_observations != position.liquidity_observations:
            raise ValueError(
                f"{position.ticker} stress evidence has a different liquidity observation count"
            )
        proposal_adv = position.average_daily_dollar_volume
        evidence_adv = row.average_daily_dollar_volume
        if (proposal_adv is None) != (evidence_adv is None) or (
            proposal_adv is not None
            and evidence_adv is not None
            and abs(proposal_adv - evidence_adv) > _EPSILON
        ):
            raise ValueError(f"{position.ticker} stress evidence has a different liquidity value")

    selected = _select_scenarios(tuple(bundle_payload["scenarios"]), scenario_ids)
    scenario_results: list[dict[str, Any]] = []
    safeguard_results: list[dict[str, Any]] = []
    for scenario in selected:
        scenario_type = scenario["scenario_type"]
        if scenario_type == "sector_template":
            sectors = sorted(
                {
                    row.sector
                    for row in evidence_by_ticker.values()
                    if row.sector is not None and row.sector.strip()
                }
            )
            if not sectors:
                scenario_results.append(
                    _blocked_scenario_result(
                        scenario,
                        ("sector:no_represented_sector",),
                        result_scenario_id=scenario["scenario_id"],
                    )
                )
            result_ids = _sector_result_ids(str(scenario["scenario_id"]), sectors)
            for sector in sectors:
                scenario_results.append(
                    _evaluate_fixed_return_scenario(
                        stress_input,
                        evidence_by_ticker,
                        scenario,
                        selected_sector=sector,
                        result_scenario_id=result_ids[sector],
                    )
                )
        elif scenario_type == "fixed_return":
            scenario_results.append(
                _evaluate_fixed_return_scenario(
                    stress_input,
                    evidence_by_ticker,
                    scenario,
                )
            )
        elif scenario_type == "volatility_correlation":
            scenario_results.append(
                _evaluate_volatility_correlation_scenario(
                    stress_input,
                    evidence_by_ticker,
                    scenario,
                )
            )
        else:
            safeguard_results.append(
                _evaluate_evidence_withholding_demonstration(
                    evidence_by_ticker,
                    scenario,
                )
            )

    scenario_result_ids = [str(result["scenario_id"]) for result in scenario_results]
    if len(scenario_result_ids) != len(set(scenario_result_ids)):
        raise ValueError("stress scenario expansion produced duplicate result IDs")

    controls = _evidence_controls(evidence_by_ticker)
    actual_blockers = sorted(
        {
            f"{row.ticker}:{blocker}"
            for row in evidence_by_ticker.values()
            for blocker in row.blockers
        }
    )
    numerical_results = [
        result
        for result in scenario_results
        if result["status"] == "calculated" and result.get("portfolio_loss_pct") is not None
    ]
    blocked_results = [
        result for result in scenario_results if result["status"] == "withheld_evidence"
    ]
    selected_numerical_policies = [
        scenario
        for scenario in selected
        if scenario["scenario_type"] != "evidence_withholding_demonstration"
    ]
    if blocked_results and not numerical_results:
        report_generation_status = "blocked"
    elif actual_blockers or blocked_results:
        report_generation_status = "partial"
    else:
        report_generation_status = "complete"
    if not selected_numerical_policies:
        calculation_coverage = "not_applicable"
    elif blocked_results and not numerical_results:
        calculation_coverage = "blocked"
    elif blocked_results:
        calculation_coverage = "partial"
    else:
        calculation_coverage = "complete"
    fixed_mark_results = [
        result
        for result in numerical_results
        if result["result_kind"] == "deterministic_mark_shock"
    ]
    statistical_proxy_results = [
        result
        for result in numerical_results
        if result["result_kind"] == "statistical_loss_proxy"
    ]
    largest_fixed = max(
        fixed_mark_results,
        key=lambda result: (result["portfolio_loss_pct"], result["scenario_id"]),
        default=None,
    )
    largest_statistical_proxy = max(
        statistical_proxy_results,
        key=lambda result: (result["portfolio_loss_pct"], result["scenario_id"]),
        default=None,
    )
    evidence_rows = [evidence_by_ticker[ticker].to_dict() for ticker in sorted(evidence_by_ticker)]
    evidence_payload = {
        "equity_basis": stress_input.equity_basis,
        "peak_equity": stress_input.peak_equity,
        "cash_weight": stress_input.cash_weight,
        "currency": bundle_payload["currency"],
        "position_count": len(stress_input.positions),
        "sandbox_reference_limits": asdict(stress_input.risk_policy),
        "positions_sha256": canonical_payload_sha256(
            {
                "positions": [
                    position.to_dict()
                    for position in sorted(
                        stress_input.positions,
                        key=lambda row: (row.security_id, row.ticker),
                    )
                ]
            }
        ),
        "target_evidence": evidence_rows,
        "target_evidence_sha256": canonical_payload_sha256({"targets": evidence_rows}),
        "controls": controls,
        "blockers": actual_blockers,
        "source_posture": {
            "proposal": "checksum_validated_local_document",
            "identity": "dated_reviewed_security_mapping",
            "prices": "identity_safe_action_checked_pit_window",
            "revenue_fact_availability": (
                "latest known date among PIT revenue facts; not a filing-freshness measure"
            ),
            "liquidity": (
                "row_hash_bound_action_safe_pit_window_recomputed_and_compared_to_proposal_adv"
            ),
        },
    }
    scenario_summary = {
        "selected_policy_count": len(selected),
        "selected_numerical_policy_count": len(selected_numerical_policies),
        "selected_safeguard_demonstration_count": len(safeguard_results),
        "generated_numerical_result_count": len(scenario_results),
        "calculated_numerical_result_count": len(numerical_results),
        "withheld_numerical_result_count": len(blocked_results),
        "largest_fixed_shock_scenario_id": (
            largest_fixed["scenario_id"] if largest_fixed else None
        ),
        "largest_fixed_shock_loss": (
            largest_fixed["portfolio_loss"] if largest_fixed else None
        ),
        "largest_fixed_shock_loss_pct": (
            largest_fixed["portfolio_loss_pct"] if largest_fixed else None
        ),
        "largest_statistical_proxy_scenario_id": (
            largest_statistical_proxy["scenario_id"]
            if largest_statistical_proxy
            else None
        ),
        "largest_statistical_proxy_loss": (
            largest_statistical_proxy["portfolio_loss"]
            if largest_statistical_proxy
            else None
        ),
        "largest_statistical_proxy_loss_pct": (
            largest_statistical_proxy["portfolio_loss_pct"]
            if largest_statistical_proxy
            else None
        ),
        "reference_finding_scenarios": sorted(
            result["scenario_id"]
            for result in numerical_results
            if result["reference_limit_findings"]
        ),
    }
    analysis = {
        "report_generation_status": report_generation_status,
        "calculation_coverage": calculation_coverage,
        "input_evidence": "incomplete" if actual_blockers else "sufficient",
        "reference_limit_findings": (
            "advisory_findings_present"
            if any(result["reference_limit_findings"] for result in numerical_results)
            else "no_advisory_findings_in_calculated_results"
        ),
        "summary": scenario_summary,
        "scenarios": scenario_results,
        "fail_closed_safeguards": safeguard_results,
        "calculation_contract": {
            "loss_sign": "positive values are losses; scenario returns are negative for declines",
            "starting_position_notional": "account equity basis multiplied by target weight",
            "fixed_mark_cash": "unshocked",
            "fixed_mark_post_stress_weights": (
                "stressed position value divided by stressed portfolio equity"
            ),
            "fixed_mark_liquidation_days": (
                "stressed position value divided by (stressed ADV multiplied by the proposal "
                "maximum order ADV fraction)"
            ),
            "fixed_mark_drawdown": "stressed equity divided by account peak equity minus one",
            "statistical_proxy_volatility": (
                "verified annualized sample volatility, multiplied by the scenario policy, "
                "with a constant correlation-convergence matrix and Euler loss contributions"
            ),
            "statistical_proxy_state": (
                "Euler contributions are risk allocations only; no stressed holdings, "
                "post-shock weights, drawdown, or liquidation path is inferred"
            ),
        },
        "advisory_posture": (
            "Scenario comparisons to generic sandbox reference limits are advisory only. "
            "They do not approve, reject, execute, or alter the paper proposal."
        ),
    }
    source_payload = stress_input.source_dict()
    if governance_context is not None:
        source_payload["forward_governance"] = dict(governance_context)
    scenario_bundle_payload = {
        "bundle_id": bundle_payload["bundle_id"],
        "bundle_version": bundle_payload["bundle_version"],
        "bundle_sha256": bundle.payload_sha256,
        "market": bundle_payload["market"],
        "currency": bundle_payload["currency"],
        "selected_scenario_ids": [scenario["scenario_id"] for scenario in selected],
    }
    analysis_sha256 = canonical_payload_sha256(
        {
            "source": source_payload,
            "scenario_bundle": scenario_bundle_payload,
            "evidence": evidence_payload,
            "analysis": analysis,
            "source_code": dict(source_identity),
        }
    )
    payload = {
        "stress_report_schema_version": STRESS_REPORT_SCHEMA_VERSION,
        "report_id": (
            f"stress-{_slug(stress_input.proposal_id) or 'proposal'}-"
            f"{analysis_sha256[:12]}"
        ),
        "report_generation_status": report_generation_status,
        "source": source_payload,
        "scenario_bundle": scenario_bundle_payload,
        "evidence": evidence_payload,
        "analysis": analysis,
        "analysis_sha256": analysis_sha256,
        "source_code": dict(source_identity),
        "reliance": {
            "reproducibility": "deterministic_given_identical_validated_inputs",
            "calibration_status": (
                "per-assumption provenance distinguishes source anchors from explicit "
                "AIOS policy sensitivities; no probabilities"
            ),
            "use": "advisory proposal risk review only",
            "not_for": [
                "personal investment advice",
                "expected-return claims",
                "broker execution",
                "automatic proposal approval or rejection",
            ],
        },
        "notice": (
            "Read-only reproducible stress evidence. The account, proposal, forward trial, "
            "incident ledger, and database were not changed, and no order was sent to a broker."
        ),
    }
    return StressReviewReport(
        payload=payload,
        payload_sha256=canonical_payload_sha256(payload),
    )


def default_stress_report_path(project_root: Path, report: StressReviewReport) -> Path:
    """Return a deterministic, content-addressed output path."""
    directory = (Path(project_root).resolve() / DEFAULT_STRESS_REPORT_DIRECTORY).resolve()
    destination = (directory / f"{_slug(str(report.payload['report_id']))}.json").resolve()
    try:
        destination.relative_to(directory)
    except ValueError as exc:
        raise ValueError("default stress report path escapes its report directory") from exc
    return destination


def require_stress_report_sources_unchanged(
    report: StressReviewReport,
    account_path: Path,
    proposal_path: Path,
    *,
    project_root: Path,
    bundle_path: Path | None = None,
) -> None:
    """Compare exact document, scenario-policy, and calculation-source hashes."""
    source = report.payload.get("source")
    if not isinstance(source, dict):
        raise ValueError("stress report is missing its source identity")
    account = read_paper_document(Path(account_path), expected_kind=ACCOUNT_DOCUMENT_KIND)
    proposal = read_paper_document(Path(proposal_path), expected_kind=PROPOSAL_DOCUMENT_KIND)
    if not hmac.compare_digest(
        account.payload_sha256,
        str(source.get("account_payload_sha256", "")),
    ):
        raise ValueError("paper account changed after stress analysis")
    if not hmac.compare_digest(
        proposal.payload_sha256,
        str(source.get("proposal_payload_sha256", "")),
    ):
        raise ValueError("paper proposal changed after stress analysis")
    expected_bundle = report.payload.get("scenario_bundle")
    if not isinstance(expected_bundle, dict) or not isinstance(
        expected_bundle.get("bundle_sha256"),
        str,
    ):
        raise ValueError("stress report is missing its scenario-bundle identity")
    current_bundle = load_scenario_bundle(bundle_path)
    if not hmac.compare_digest(
        str(expected_bundle["bundle_sha256"]),
        current_bundle.payload_sha256,
    ):
        raise ValueError("stress scenario bundle changed after analysis")
    expected_code = report.payload.get("source_code")
    if not isinstance(expected_code, dict) or not isinstance(
        expected_code.get("source_bundle_sha256"),
        str,
    ):
        raise ValueError("stress report is missing its calculation-source identity")
    current_code = build_stress_source_identity(Path(project_root))
    if not hmac.compare_digest(
        str(expected_code["source_bundle_sha256"]),
        str(current_code["source_bundle_sha256"]),
    ):
        raise ValueError("stress calculation source changed after analysis")


def write_stress_report(
    path: Path,
    report: StressReviewReport,
    *,
    before_publish: Callable[[], None] | None = None,
) -> Path:
    """Atomically write one immutable stress artifact without overwriting."""
    if not hmac.compare_digest(
        report.payload_sha256,
        canonical_payload_sha256(report.payload),
    ):
        raise ValueError("stress report payload changed after analysis")
    expected_analysis_hash = canonical_payload_sha256(
        {
            "source": report.payload.get("source"),
            "scenario_bundle": report.payload.get("scenario_bundle"),
            "evidence": report.payload.get("evidence"),
            "analysis": report.payload.get("analysis"),
            "source_code": report.payload.get("source_code"),
        }
    )
    if not hmac.compare_digest(
        str(report.payload.get("analysis_sha256", "")),
        expected_analysis_hash,
    ):
        raise ValueError("stress report analysis changed after calculation")
    destination = Path(path)
    for ancestor in (destination.parent, *destination.parent.parents):
        if ancestor.is_symlink():
            raise ValueError(f"stress report parent cannot be a symlink: {ancestor}")
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"stress report already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    published = False
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(report.envelope(), handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if before_publish is not None:
            before_publish()
        if destination.exists() or destination.is_symlink():
            raise ValueError(f"stress report already exists: {destination}")
        try:
            os.link(temporary, destination)
        except FileExistsError:
            raise ValueError(f"stress report already exists: {destination}") from None
        published = True
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if published:
            try:
                same_artifact = os.path.samestat(temporary.stat(), destination.stat())
            except (FileNotFoundError, OSError):
                same_artifact = False
            if same_artifact:
                destination.unlink(missing_ok=True)
        raise
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def _rollback_stress_report_publication(
    path: Path,
    report: StressReviewReport,
) -> None:
    """Remove only the just-published matching artifact after a failed final CAS."""
    destination = Path(path)
    published = read_stress_report(destination)
    if not hmac.compare_digest(
        published.payload_sha256,
        report.payload_sha256,
    ):
        raise ValueError("published stress artifact identity changed before rollback")
    destination.unlink()
    directory_fd = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def read_stress_report(path: Path) -> StressReviewReport:
    """Read and checksum-validate one stored stress artifact."""
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"stress report does not exist: {source}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"stress report is unreadable: {source}") from exc
    if (
        not isinstance(raw, dict)
        or raw.get("document_schema_version") != STRESS_DOCUMENT_SCHEMA_VERSION
        or raw.get("document_kind") != STRESS_DOCUMENT_KIND
        or not isinstance(raw.get("payload"), dict)
        or not isinstance(raw.get("payload_sha256"), str)
    ):
        raise ValueError("invalid stress report envelope")
    payload = raw["payload"]
    actual = canonical_payload_sha256(payload)
    if not hmac.compare_digest(raw["payload_sha256"], actual):
        raise ValueError("stress report checksum mismatch")
    if payload.get("stress_report_schema_version") != STRESS_REPORT_SCHEMA_VERSION:
        raise ValueError("unsupported stress report schema")
    expected_analysis_hash = canonical_payload_sha256(
        {
            "source": payload.get("source"),
            "scenario_bundle": payload.get("scenario_bundle"),
            "evidence": payload.get("evidence"),
            "analysis": payload.get("analysis"),
            "source_code": payload.get("source_code"),
        }
    )
    if not hmac.compare_digest(
        str(payload.get("analysis_sha256", "")),
        expected_analysis_hash,
    ):
        raise ValueError("stress report analysis checksum mismatch")
    return StressReviewReport(payload=payload, payload_sha256=actual)


def build_stress_source_identity(project_root: Path) -> dict[str, Any]:
    """Hash every source file that can change stress calculations."""
    root = Path(project_root).resolve()
    file_hashes: dict[str, str] = {}
    for relative in _STRESS_SOURCE_FILES:
        source = (root / relative).resolve()
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"stress source file escapes project root: {relative}") from exc
        if not source.is_file():
            raise ValueError(f"stress source file is missing: {relative}")
        file_hashes[relative] = _sha256_bytes(source.read_bytes())
    git_commit: str | None = None
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if completed is not None and completed.returncode == 0:
        candidate = completed.stdout.strip()
        if candidate:
            git_commit = candidate
    return {
        "git_commit": git_commit,
        "source_bundle_sha256": canonical_payload_sha256({"files": file_hashes}),
        "source_files": file_hashes,
        "identity_note": (
            "The source bundle hash is authoritative even when the repository working tree "
            "does not match the recorded commit."
        ),
    }


def _evaluate_fixed_return_scenario(
    stress_input: ProposalStressInput,
    evidence_by_ticker: Mapping[str, TargetStressEvidence],
    scenario: Mapping[str, Any],
    *,
    selected_sector: str | None = None,
    result_scenario_id: str | None = None,
) -> dict[str, Any]:
    blockers = _scenario_evidence_blockers(
        evidence_by_ticker,
        required_categories=("identity", "sector", "liquidity"),
    )
    result_id = result_scenario_id or str(scenario["scenario_id"])
    label = str(scenario["label"])
    if selected_sector is not None:
        if result_scenario_id is None:
            result_id = f"{result_id}:{_slug(selected_sector)}"
        label = f"{label} — {selected_sector}"
    if blockers:
        return _blocked_scenario_result(
            scenario,
            blockers,
            result_scenario_id=result_id,
            result_label=label,
        )

    assumptions = scenario["assumptions"]
    selector = assumptions.get("selector", {})
    selected_tickers: set[str]
    if selected_sector is not None:
        selected_tickers = {
            ticker
            for ticker, row in evidence_by_ticker.items()
            if row.sector == selected_sector
        }
    elif selector.get("kind") == "top_weighted":
        count = int(selector["count"])
        ranked = sorted(
            stress_input.positions,
            key=lambda row: (
                -row.target_weight,
                row.factor_rank,
                row.security_id,
                row.ticker,
            ),
        )
        selected_tickers = {position.ticker for position in ranked[:count]}
    else:
        selected_tickers = set(evidence_by_ticker)

    selected_return = float(assumptions["selected_return"])
    other_return = float(assumptions["other_return"])
    shocks = {
        ticker: selected_return if ticker in selected_tickers else other_return
        for ticker in evidence_by_ticker
    }
    return _assemble_scenario_result(
        stress_input,
        evidence_by_ticker,
        scenario,
        shocks=shocks,
        result_scenario_id=result_id,
        result_label=label,
        selected_sector=selected_sector,
    )


def _evaluate_volatility_correlation_scenario(
    stress_input: ProposalStressInput,
    evidence_by_ticker: Mapping[str, TargetStressEvidence],
    scenario: Mapping[str, Any],
) -> dict[str, Any]:
    blockers = _scenario_evidence_blockers(
        evidence_by_ticker,
        required_categories=("identity", "price"),
    )
    if blockers:
        return _blocked_scenario_result(scenario, blockers)

    assumptions = scenario["assumptions"]
    volatility_multiplier = float(assumptions["volatility_multiplier"])
    correlation = float(assumptions["constant_correlation_assumption"])
    sigma_multiple = float(assumptions["standard_deviation_multiple"])
    horizon_sessions = int(assumptions["horizon_sessions"])
    positions = stress_input.positions
    weights = [position.target_weight for position in positions]
    volatilities = [
        float(evidence_by_ticker[position.ticker].annualized_volatility or 0.0)
        * volatility_multiplier
        for position in positions
    ]
    covariance: list[list[float]] = []
    for row_index, row_volatility in enumerate(volatilities):
        covariance.append(
            [
                row_volatility
                * column_volatility
                * (1.0 if row_index == column_index else correlation)
                for column_index, column_volatility in enumerate(volatilities)
            ]
        )
    variance = sum(
        weights[row_index]
        * weights[column_index]
        * covariance[row_index][column_index]
        for row_index in range(len(weights))
        for column_index in range(len(weights))
    )
    if not isfinite(variance) or variance <= 0:
        return _blocked_scenario_result(
            scenario,
            ("price:invalid_stressed_covariance",),
        )
    portfolio_volatility = sqrt(variance)
    horizon_scale = sqrt(horizon_sessions / TRADING_SESSIONS_PER_YEAR)
    portfolio_loss_fraction = sigma_multiple * portfolio_volatility * horizon_scale
    if not isfinite(portfolio_loss_fraction) or portfolio_loss_fraction < 0:
        return _blocked_scenario_result(
            scenario,
            ("price:invalid_volatility_loss_proxy",),
        )

    position_rows: list[dict[str, Any]] = []
    for row_index, position in enumerate(positions):
        marginal_covariance = sum(
            covariance[row_index][column_index] * weights[column_index]
            for column_index in range(len(weights))
        )
        contribution_fraction = (
            sigma_multiple
            * horizon_scale
            * weights[row_index]
            * marginal_covariance
            / portfolio_volatility
        )
        if contribution_fraction < 0 or not isfinite(contribution_fraction):
            return _blocked_scenario_result(
                scenario,
                (f"price:{position.ticker}:invalid_euler_loss_contribution",),
            )
        position_rows.append(
            {
                "ticker": position.ticker,
                "security_id": position.security_id,
                "sector": position.sector,
                "factor_rank": position.factor_rank,
                "target_weight": position.target_weight,
                "verified_annualized_volatility": evidence_by_ticker[
                    position.ticker
                ].annualized_volatility,
                "sensitivity_annualized_volatility": _clean_float(
                    volatilities[row_index]
                ),
                "euler_loss_contribution": _clean_float(
                    stress_input.equity_basis * contribution_fraction
                ),
                "euler_loss_contribution_pct_of_starting_equity": _clean_float(
                    contribution_fraction
                ),
                "euler_share_of_proxy_loss": _clean_float(
                    contribution_fraction / portfolio_loss_fraction
                ),
            }
        )

    sector_proxy_contributions: list[dict[str, Any]] = []
    sector_labels = {
        str(row["sector"]).strip() if row["sector"] else "Unclassified"
        for row in position_rows
    }
    for sector in sorted(sector_labels):
        members = [
            row
            for row in position_rows
            if (str(row["sector"]).strip() if row["sector"] else "Unclassified")
            == sector
        ]
        contribution = sum(
            float(row["euler_loss_contribution_pct_of_starting_equity"])
            for row in members
        )
        sector_proxy_contributions.append(
            {
                "sector": sector,
                "euler_loss_contribution": _clean_float(
                    stress_input.equity_basis * contribution
                ),
                "euler_loss_contribution_pct_of_starting_equity": _clean_float(
                    contribution
                ),
                "euler_share_of_proxy_loss": _clean_float(
                    contribution / portfolio_loss_fraction
                ),
            }
        )

    reference_findings: list[dict[str, Any]] = []
    if (
        portfolio_loss_fraction
        > stress_input.risk_policy.maximum_drawdown + _EPSILON
    ):
        reference_findings.append(
            _reference_limit_finding(
                finding_id="loss_proxy_above_drawdown_reference",
                metric="portfolio_loss_proxy_pct",
                observed=portfolio_loss_fraction,
                limit=stress_input.risk_policy.maximum_drawdown,
                unit="fraction_of_starting_equity",
                message=(
                    f"Loss proxy {portfolio_loss_fraction:.1%} is above the generic "
                    f"{stress_input.risk_policy.maximum_drawdown:.1%} sandbox drawdown "
                    "reference. This is not a deterministic drawdown or an approval gate."
                ),
            )
        )

    portfolio_loss = stress_input.equity_basis * portfolio_loss_fraction
    return {
        "scenario_id": scenario["scenario_id"],
        "scenario_policy_id": scenario["scenario_id"],
        "scenario_version": scenario["version"],
        "scenario_sha256": canonical_payload_sha256(dict(scenario)),
        "scenario_type": scenario["scenario_type"],
        "result_kind": "statistical_loss_proxy",
        "loss_basis": "two-standard-deviation constant-correlation loss proxy",
        "required_evidence_categories": ["identity", "price"],
        "label": scenario["label"],
        "historical_label": scenario.get("historical_label"),
        "status": "calculated",
        "source_posture": scenario["source_posture"],
        "description": scenario["description"],
        "selected_sector": None,
        "starting_equity": stress_input.equity_basis,
        "stressed_equity": None,
        "portfolio_return": None,
        "portfolio_loss": _clean_float(portfolio_loss),
        "portfolio_loss_pct": _clean_float(portfolio_loss_fraction),
        "stressed_drawdown": None,
        "stressed_annualized_volatility": None,
        "sensitivity_portfolio_annualized_volatility": _clean_float(
            portfolio_volatility
        ),
        "volatility_loss_proxy_pct": _clean_float(portfolio_loss_fraction),
        "positions": position_rows,
        "sector_contributions": [],
        "sector_proxy_contributions": sector_proxy_contributions,
        "post_stress_state": {
            "applicable": False,
            "reason": (
                "Euler risk contributions are not position returns, stressed holdings, "
                "or liquidation paths."
            ),
        },
        "reference_limit_findings": reference_findings,
        "reference_limit_not_applicable": [
            "post_stress_position_concentration",
            "post_stress_sector_concentration",
            "liquidity_horizon",
        ],
        "blockers": [],
        "assumptions": dict(assumptions),
        "assumption_provenance": dict(scenario["assumption_provenance"]),
        "calibration_sources": list(scenario["calibration_sources"]),
        "illustrative_recovery_assumption": {
            "path": list(scenario["recovery_path"]),
            "used_in_loss_calculation": False,
            "forecast": False,
        },
        "volatility_method": {
            "volatility_multiplier": volatility_multiplier,
            "constant_correlation_assumption": correlation,
            "standard_deviation_multiple": sigma_multiple,
            "horizon_sessions": horizon_sessions,
            "contribution_method": "Euler contribution to sensitivity portfolio volatility",
            "deterministic_post_stress_state": False,
            "probability_claim": False,
        },
        "probability_claim": False,
        "execution_effect": "none_advisory_only",
    }


def _assemble_scenario_result(
    stress_input: ProposalStressInput,
    evidence_by_ticker: Mapping[str, TargetStressEvidence],
    scenario: Mapping[str, Any],
    *,
    shocks: Mapping[str, float],
    result_scenario_id: str | None = None,
    result_label: str | None = None,
    selected_sector: str | None = None,
) -> dict[str, Any]:
    equity = stress_input.equity_basis
    cash_value = equity * stress_input.cash_weight
    assumptions = scenario["assumptions"]
    adv_multiplier = float(assumptions["liquidity_adv_multiplier"])
    liquidation_horizon = int(assumptions["liquidation_horizon_sessions"])
    position_rows: list[dict[str, Any]] = []
    stressed_invested_value = 0.0
    portfolio_loss = 0.0

    for position in stress_input.positions:
        row = evidence_by_ticker[position.ticker]
        shock_return = float(shocks[position.ticker])
        starting_notional = equity * position.target_weight
        stressed_notional = max(0.0, starting_notional * (1.0 + shock_return))
        loss = starting_notional - stressed_notional
        portfolio_loss += loss
        stressed_invested_value += stressed_notional
        baseline_adv = float(row.average_daily_dollar_volume or 0.0)
        stressed_adv = baseline_adv * adv_multiplier
        daily_capacity = stressed_adv * stress_input.risk_policy.maximum_order_adv_fraction
        exit_days = stressed_notional / daily_capacity if daily_capacity > 0 else None
        position_rows.append(
            {
                "ticker": position.ticker,
                "security_id": position.security_id,
                "sector": position.sector,
                "factor_rank": position.factor_rank,
                "target_weight": position.target_weight,
                "starting_notional": _clean_float(starting_notional),
                "shock_return": _clean_float(shock_return),
                "stressed_notional": _clean_float(stressed_notional),
                "loss": _clean_float(loss),
                "loss_pct_of_starting_equity": _clean_float(loss / equity),
                "reviewed_price": row.reviewed_price,
                "reviewed_price_date": row.reviewed_price_date,
                "baseline_adv": row.average_daily_dollar_volume,
                "stressed_adv": _clean_float(stressed_adv),
                "sessions_to_liquidate_at_policy_limit": (
                    _clean_float(exit_days) if exit_days is not None else None
                ),
                "liquidation_horizon_sessions": liquidation_horizon,
                "liquidity_horizon_breach": (
                    exit_days is None or exit_days > liquidation_horizon + _EPSILON
                ),
            }
        )

    stressed_equity = cash_value + stressed_invested_value
    expected_stressed_equity = equity - portfolio_loss
    accounting_tolerance = max(_EPSILON, abs(equity) * 1e-12)
    if (
        not isfinite(stressed_equity)
        or not isfinite(expected_stressed_equity)
        or abs(stressed_equity - expected_stressed_equity) > accounting_tolerance
    ):
        return _blocked_scenario_result(
            scenario,
            ("calculation:portfolio_accounting_identity_failed",),
            result_scenario_id=result_scenario_id,
            result_label=result_label,
        )
    if stressed_equity < -accounting_tolerance:
        return _blocked_scenario_result(
            scenario,
            ("calculation:negative_stressed_equity",),
            result_scenario_id=result_scenario_id,
            result_label=result_label,
        )
    if abs(stressed_equity) <= accounting_tolerance:
        stressed_equity = 0.0
    equity_exhausted = stressed_equity == 0.0
    for row in position_rows:
        row["stressed_weight"] = (
            None
            if equity_exhausted
            else _clean_float(row["stressed_notional"] / stressed_equity)
        )

    sector_rows: list[dict[str, Any]] = []
    sector_labels = {
        str(row["sector"]).strip() if row["sector"] else "Unclassified"
        for row in position_rows
    }
    for sector in sorted(sector_labels):
        members = [
            row
            for row in position_rows
            if (str(row["sector"]).strip() if row["sector"] else "Unclassified")
            == sector
        ]
        starting_notional = sum(float(row["starting_notional"]) for row in members)
        stressed_notional = sum(float(row["stressed_notional"]) for row in members)
        loss = sum(float(row["loss"]) for row in members)
        sector_rows.append(
            {
                "sector": sector,
                "starting_weight": _clean_float(starting_notional / equity),
                "stressed_weight": (
                    None
                    if equity_exhausted
                    else _clean_float(stressed_notional / stressed_equity)
                ),
                "loss": _clean_float(loss),
                "loss_pct_of_starting_equity": _clean_float(loss / equity),
            }
        )

    stressed_drawdown = stressed_equity / stress_input.peak_equity - 1.0
    largest_position_weight = max(
        (
            float(row["stressed_weight"])
            for row in position_rows
            if row["stressed_weight"] is not None
        ),
        default=0.0,
    )
    largest_sector_weight = max(
        (
            float(row["stressed_weight"])
            for row in sector_rows
            if row["stressed_weight"] is not None
        ),
        default=0.0,
    )
    reference_findings: list[dict[str, Any]] = []
    if equity_exhausted:
        reference_findings.append(
            _reference_limit_finding(
                finding_id="portfolio_equity_exhausted",
                metric="portfolio_loss_pct",
                observed=1.0,
                limit=stress_input.risk_policy.maximum_drawdown,
                unit="fraction_of_starting_equity",
                message=(
                    "The deterministic mark shock exhausts modeled portfolio equity. "
                    "Post-stress concentration weights are undefined."
                ),
            )
        )
    if stressed_drawdown < -stress_input.risk_policy.maximum_drawdown - _EPSILON:
        reference_findings.append(
            _reference_limit_finding(
                finding_id="drawdown_above_reference",
                metric="stressed_drawdown_magnitude",
                observed=-stressed_drawdown,
                limit=stress_input.risk_policy.maximum_drawdown,
                unit="fraction_of_peak_equity",
                message=(
                    f"Modeled drawdown {-stressed_drawdown:.1%} exceeds the generic "
                    f"{stress_input.risk_policy.maximum_drawdown:.1%} sandbox reference."
                ),
            )
        )
    if largest_position_weight > stress_input.risk_policy.maximum_position_weight + _EPSILON:
        reference_findings.append(
            _reference_limit_finding(
                finding_id="post_stress_position_concentration",
                metric="largest_post_stress_position_weight",
                observed=largest_position_weight,
                limit=stress_input.risk_policy.maximum_position_weight,
                unit="fraction_of_stressed_equity",
                message=(
                    f"Largest modeled position weight {largest_position_weight:.1%} "
                    f"exceeds the generic "
                    f"{stress_input.risk_policy.maximum_position_weight:.1%} sandbox "
                    "reference."
                ),
            )
        )
    if largest_sector_weight > stress_input.risk_policy.maximum_sector_weight + _EPSILON:
        reference_findings.append(
            _reference_limit_finding(
                finding_id="post_stress_sector_concentration",
                metric="largest_post_stress_sector_weight",
                observed=largest_sector_weight,
                limit=stress_input.risk_policy.maximum_sector_weight,
                unit="fraction_of_stressed_equity",
                message=(
                    f"Largest modeled sector weight {largest_sector_weight:.1%} "
                    f"exceeds the generic "
                    f"{stress_input.risk_policy.maximum_sector_weight:.1%} sandbox "
                    "reference."
                ),
            )
        )
    liquidity_breaches = [
        row for row in position_rows if row["liquidity_horizon_breach"]
    ]
    if liquidity_breaches:
        max_exit_days = max(
            float(row["sessions_to_liquidate_at_policy_limit"])
            for row in liquidity_breaches
            if row["sessions_to_liquidate_at_policy_limit"] is not None
        )
        affected_tickers = sorted(str(row["ticker"]) for row in liquidity_breaches)
        finding = _reference_limit_finding(
            finding_id="liquidity_horizon",
            metric="maximum_sessions_to_liquidate_at_policy_limit",
            observed=max_exit_days,
            limit=float(liquidation_horizon),
            unit="sessions",
            message=(
                f"Modeled exit time reaches {max_exit_days:.2f} sessions for "
                f"{', '.join(affected_tickers)}, above the generic "
                f"{liquidation_horizon}-session sandbox horizon."
            ),
        )
        finding["affected_tickers"] = affected_tickers
        reference_findings.append(finding)

    return {
        "scenario_id": result_scenario_id or scenario["scenario_id"],
        "scenario_policy_id": scenario["scenario_id"],
        "scenario_version": scenario["version"],
        "scenario_sha256": canonical_payload_sha256(dict(scenario)),
        "scenario_type": scenario["scenario_type"],
        "result_kind": (
            "statistical_loss_proxy"
            if scenario["scenario_type"] == "volatility_correlation"
            else "deterministic_mark_shock"
        ),
        "loss_basis": (
            "two-standard-deviation constant-correlation loss proxy"
            if scenario["scenario_type"] == "volatility_correlation"
            else "deterministic proposal-target mark shock"
        ),
        "required_evidence_categories": ["identity", "sector", "liquidity"],
        "label": result_label or scenario["label"],
        "historical_label": scenario.get("historical_label"),
        "status": "calculated",
        "source_posture": scenario["source_posture"],
        "description": scenario["description"],
        "selected_sector": selected_sector,
        "starting_equity": equity,
        "stressed_equity": _clean_float(stressed_equity),
        "portfolio_return": _clean_float(-portfolio_loss / equity),
        "portfolio_loss": _clean_float(portfolio_loss),
        "portfolio_loss_pct": _clean_float(portfolio_loss / equity),
        "stressed_drawdown": _clean_float(stressed_drawdown),
        "stressed_annualized_volatility": None,
        "sensitivity_portfolio_annualized_volatility": None,
        "positions": position_rows,
        "sector_contributions": sector_rows,
        "sector_proxy_contributions": [],
        "post_stress_state": {
            "applicable": True,
            "reason": "Position marks are transformed directly by deterministic returns.",
            "portfolio_equity_exhausted": equity_exhausted,
        },
        "reference_limit_findings": reference_findings,
        "reference_limit_not_applicable": (
            [
                "post_stress_position_concentration",
                "post_stress_sector_concentration",
            ]
            if equity_exhausted
            else []
        ),
        "blockers": [],
        "assumptions": dict(assumptions),
        "assumption_provenance": dict(scenario["assumption_provenance"]),
        "calibration_sources": list(scenario["calibration_sources"]),
        "illustrative_recovery_assumption": {
            "path": list(scenario["recovery_path"]),
            "used_in_loss_calculation": False,
            "forecast": False,
        },
        "probability_claim": False,
        "execution_effect": "none_advisory_only",
    }


def _reference_limit_finding(
    *,
    finding_id: str,
    metric: str,
    observed: float,
    limit: float,
    unit: str,
    message: str,
) -> dict[str, Any]:
    """Build one explicit, non-gating comparison to the proposal's sandbox limits."""
    return {
        "finding_id": finding_id,
        "metric": metric,
        "observed": _clean_float(observed),
        "comparison": "above",
        "limit": _clean_float(limit),
        "unit": unit,
        "advisory": True,
        "message": message,
    }


def _evaluate_evidence_withholding_demonstration(
    evidence_by_ticker: Mapping[str, TargetStressEvidence],
    scenario: Mapping[str, Any],
) -> dict[str, Any]:
    evidence_kind = str(scenario["assumptions"]["evidence_kind"])
    actual = sorted(
        {
            f"{row.ticker}:{blocker}"
            for row in evidence_by_ticker.values()
            for blocker in row.blockers
            if blocker.startswith(f"{evidence_kind}:")
        }
    )
    return {
        "safeguard_id": scenario["scenario_id"],
        "safeguard_version": scenario["version"],
        "safeguard_sha256": canonical_payload_sha256(dict(scenario)),
        "label": scenario["label"],
        "status": "withholding_required" if actual else "policy_demonstration",
        "source_posture": scenario["source_posture"],
        "description": scenario["description"],
        "evidence_kind": evidence_kind,
        "live_evidence_gap": bool(actual),
        "live_blockers": actual,
        "outage_injected": False,
        "calculation_rerun": False,
        "numerical_output": None,
        "assumption_provenance": dict(scenario["assumption_provenance"]),
        "calibration_sources": list(scenario["calibration_sources"]),
        "probability_claim": False,
        "execution_effect": "none_advisory_only",
        "demonstration_outcome": (
            "live evidence is missing; dependent calculations must withhold output"
            if actual
            else (
                "policy documented only; current evidence is present and no outage was "
                "injected"
            )
        ),
    }


def _blocked_scenario_result(
    scenario: Mapping[str, Any],
    blockers: Sequence[str],
    *,
    result_scenario_id: str | None = None,
    result_label: str | None = None,
) -> dict[str, Any]:
    return {
        "scenario_id": result_scenario_id or scenario["scenario_id"],
        "scenario_policy_id": scenario["scenario_id"],
        "scenario_version": scenario["version"],
        "scenario_sha256": canonical_payload_sha256(dict(scenario)),
        "scenario_type": scenario["scenario_type"],
        "result_kind": (
            "statistical_loss_proxy"
            if scenario["scenario_type"] == "volatility_correlation"
            else "deterministic_mark_shock"
        ),
        "loss_basis": (
            "two-standard-deviation constant-correlation loss proxy"
            if scenario["scenario_type"] == "volatility_correlation"
            else "deterministic proposal-target mark shock"
        ),
        "required_evidence_categories": (
            ["identity", "price"]
            if scenario["scenario_type"] == "volatility_correlation"
            else ["identity", "sector", "liquidity"]
        ),
        "label": result_label or scenario["label"],
        "historical_label": scenario.get("historical_label"),
        "status": "withheld_evidence",
        "source_posture": scenario["source_posture"],
        "description": scenario["description"],
        "starting_equity": None,
        "stressed_equity": None,
        "portfolio_return": None,
        "portfolio_loss": None,
        "portfolio_loss_pct": None,
        "stressed_drawdown": None,
        "stressed_annualized_volatility": None,
        "sensitivity_portfolio_annualized_volatility": None,
        "positions": [],
        "sector_contributions": [],
        "sector_proxy_contributions": [],
        "post_stress_state": {
            "applicable": False,
            "reason": "Required evidence was unavailable, so no numerical state was produced.",
        },
        "reference_limit_findings": [],
        "reference_limit_not_applicable": [],
        "blockers": sorted(set(blockers)),
        "assumptions": dict(scenario["assumptions"]),
        "assumption_provenance": dict(scenario["assumption_provenance"]),
        "calibration_sources": list(scenario["calibration_sources"]),
        "illustrative_recovery_assumption": {
            "path": list(scenario["recovery_path"]),
            "used_in_loss_calculation": False,
            "forecast": False,
        },
        "probability_claim": False,
        "execution_effect": "none_advisory_only",
    }


def _evidence_controls(
    evidence_by_ticker: Mapping[str, TargetStressEvidence],
) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []
    for category in _EVIDENCE_CATEGORIES:
        blockers = sorted(
            {
                f"{row.ticker}:{blocker}"
                for row in evidence_by_ticker.values()
                for blocker in row.blockers
                if blocker.startswith(f"{category}:")
            }
        )
        controls.append(
            {
                "evidence": category,
                "status": "blocked" if blockers else "pass",
                "blockers": blockers,
                "policy": "No imputation; dependent scenarios withhold numerical output.",
            }
        )
    return controls


def _scenario_evidence_blockers(
    evidence_by_ticker: Mapping[str, TargetStressEvidence],
    *,
    required_categories: Sequence[str],
) -> tuple[str, ...]:
    prefixes = tuple(f"{category}:" for category in required_categories)
    return tuple(
        sorted(
            {
                f"{row.ticker}:{blocker}"
                for row in evidence_by_ticker.values()
                for blocker in row.blockers
                if blocker.startswith(prefixes)
            }
        )
    )


def _select_scenarios(
    scenarios: Sequence[dict[str, Any]],
    scenario_ids: Sequence[str] | None,
) -> tuple[dict[str, Any], ...]:
    if not scenario_ids:
        return tuple(scenarios)
    requested = {str(value).strip() for value in scenario_ids if str(value).strip()}
    if not requested:
        raise ValueError("at least one non-empty stress scenario ID is required")
    known = {scenario["scenario_id"] for scenario in scenarios}
    unknown = sorted(requested - known)
    if unknown:
        raise ValueError("unknown stress scenario(s): " + ", ".join(unknown))
    return tuple(scenario for scenario in scenarios if scenario["scenario_id"] in requested)


def _stress_position(raw: Any, fallback_rank: int) -> StressPosition:
    if not isinstance(raw, dict):
        raise ValueError("proposal target is not an object")
    ticker = str(raw.get("ticker", "")).strip().upper()
    security_id = str(raw.get("security_id", "")).strip()
    if not ticker or not security_id:
        raise ValueError("proposal target is missing ticker or stable security ID")
    target_weight = _finite_positive(raw.get("target_weight"), f"{ticker} target weight")
    factor_rank_raw = raw.get("factor_rank", fallback_rank)
    if isinstance(factor_rank_raw, bool) or not isinstance(factor_rank_raw, int):
        raise ValueError(f"{ticker} factor rank must be a positive integer")
    factor_rank = factor_rank_raw
    if factor_rank < 1:
        raise ValueError(f"{ticker} factor rank must be positive")
    sector_raw = raw.get("sector")
    sector = str(sector_raw).strip() if sector_raw is not None else None
    sector = sector or None
    adv_raw = raw.get("average_daily_dollar_volume")
    average_daily_dollar_volume = (
        None
        if adv_raw is None
        else _finite_number(adv_raw, f"{ticker} average daily dollar volume")
    )
    observations_raw = raw.get("liquidity_observations", 0)
    if isinstance(observations_raw, bool) or not isinstance(observations_raw, int):
        raise ValueError(f"{ticker} liquidity observations must be a non-negative integer")
    observations = observations_raw
    if observations < 0:
        raise ValueError(f"{ticker} liquidity observations cannot be negative")
    return StressPosition(
        ticker=ticker,
        security_id=security_id,
        sector=sector,
        target_weight=target_weight,
        factor_rank=factor_rank,
        average_daily_dollar_volume=average_daily_dollar_volume,
        liquidity_observations=observations,
    )


def _validate_scenario_bundle(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("stress scenario bundle must be an object")
    if (
        payload.get("stress_scenario_bundle_schema_version")
        != STRESS_SCENARIO_BUNDLE_SCHEMA_VERSION
    ):
        raise ValueError("unsupported stress scenario bundle schema")
    for field in ("bundle_id", "bundle_version", "market", "currency", "notice"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ValueError(f"stress scenario bundle is missing {field}")
    if payload["market"] != "US" or payload["currency"] != "USD":
        raise ValueError("stress scenario bundle v1 must be US/USD")
    evidence_policy = payload.get("evidence_policy")
    if not isinstance(evidence_policy, dict):
        raise ValueError("stress scenario bundle is missing evidence policy")
    for field in (
        "maximum_revenue_fact_known_age_days",
        "maximum_price_staleness_days",
        "minimum_liquidity_observations",
        "minimum_price_observations",
    ):
        value = evidence_policy.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"stress evidence policy {field} must be a positive integer")
    if evidence_policy["minimum_price_observations"] != REQUIRED_PRICE_OBSERVATIONS:
        raise ValueError("stress bundle price window must match certified market-factor policy")

    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("stress scenario bundle has no scenarios")
    scenario_ids: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise ValueError("stress scenario must be an object")
        scenario_id = str(scenario.get("scenario_id", "")).strip()
        if not scenario_id:
            raise ValueError("stress scenario is missing scenario_id")
        if scenario_id in scenario_ids:
            raise ValueError(f"duplicate stress scenario ID: {scenario_id}")
        scenario_ids.add(scenario_id)
        scenario_type = scenario.get("scenario_type")
        if scenario_type not in _SCENARIO_TYPES:
            raise ValueError(f"unsupported stress scenario type: {scenario_type}")
        for field in ("version", "label", "description", "source_posture"):
            if not isinstance(scenario.get(field), str) or not scenario[field].strip():
                raise ValueError(f"{scenario_id} is missing {field}")
        sources = scenario.get("calibration_sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError(f"{scenario_id} needs at least one calibration reference")
        for source in sources:
            if not isinstance(source, dict) or any(
                not isinstance(source.get(field), str) or not source[field].strip()
                for field in ("title", "reference", "basis")
            ):
                raise ValueError(f"{scenario_id} has an invalid calibration reference")
        assumptions = scenario.get("assumptions")
        if not isinstance(assumptions, dict):
            raise ValueError(f"{scenario_id} is missing assumptions")
        provenance = scenario.get("assumption_provenance")
        required_provenance = set(assumptions)
        if scenario_type != "evidence_withholding_demonstration":
            required_provenance.add("recovery_path")
        if (
            not isinstance(provenance, dict)
            or set(provenance) != required_provenance
            or any(not isinstance(value, str) or not value.strip() for value in provenance.values())
        ):
            raise ValueError(
                f"{scenario_id} needs explicit provenance for every assumption"
            )
        recovery = scenario.get("recovery_path")
        if not isinstance(recovery, list):
            raise ValueError(f"{scenario_id} recovery path must be a list")
        _validate_recovery_path(
            scenario_id,
            recovery,
            allow_empty=scenario_type == "evidence_withholding_demonstration",
        )
        if scenario_type in {"fixed_return", "sector_template"}:
            _validate_fixed_return_assumptions(scenario_id, assumptions, scenario_type)
        elif scenario_type == "volatility_correlation":
            _validate_volatility_assumptions(scenario_id, assumptions)
        else:
            if assumptions.get("evidence_kind") not in {"price", "revenue_fact"}:
                raise ValueError(f"{scenario_id} has an unsupported evidence outage")


def _validate_fixed_return_assumptions(
    scenario_id: str,
    assumptions: Mapping[str, Any],
    scenario_type: str,
) -> None:
    expected_fields = {
        "selector",
        "selected_return",
        "other_return",
        "liquidity_adv_multiplier",
        "liquidation_horizon_sessions",
    }
    if set(assumptions) != expected_fields:
        raise ValueError(
            f"{scenario_id} fixed-return assumptions must be exactly "
            + ", ".join(sorted(expected_fields))
        )
    selector = assumptions.get("selector")
    if not isinstance(selector, dict):
        raise ValueError(f"{scenario_id} is missing a selector")
    kind = selector.get("kind")
    allowed = {"each_sector"} if scenario_type == "sector_template" else {"all", "top_weighted"}
    if kind not in allowed:
        raise ValueError(f"{scenario_id} has an unsupported selector")
    if kind == "top_weighted":
        if set(selector) != {"kind", "count"}:
            raise ValueError(f"{scenario_id} top-weighted selector has unknown fields")
        count = selector.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"{scenario_id} top-weighted count must be positive")
    elif set(selector) != {"kind"}:
        raise ValueError(f"{scenario_id} selector has unknown fields")
    selected_return = _finite_number(
        assumptions.get("selected_return"),
        f"{scenario_id} selected_return",
    )
    other_return = _finite_number(
        assumptions.get("other_return"),
        f"{scenario_id} other_return",
    )
    for field, value in (
        ("selected_return", selected_return),
        ("other_return", other_return),
    ):
        if value < -1.0 or value > 0.0:
            raise ValueError(f"{scenario_id} {field} must be between -1 and 0")
    if selected_return > other_return + _EPSILON:
        raise ValueError(
            f"{scenario_id} selected_return must be no greater than other_return"
        )
    _validate_liquidity_assumptions(scenario_id, assumptions)


def _validate_volatility_assumptions(
    scenario_id: str,
    assumptions: Mapping[str, Any],
) -> None:
    expected_fields = {
        "volatility_multiplier",
        "constant_correlation_assumption",
        "standard_deviation_multiple",
        "horizon_sessions",
    }
    if set(assumptions) != expected_fields:
        raise ValueError(
            f"{scenario_id} volatility proxy assumptions must be exactly "
            + ", ".join(sorted(expected_fields))
        )
    for field in ("volatility_multiplier", "standard_deviation_multiple"):
        if _finite_number(assumptions.get(field), f"{scenario_id} {field}") <= 0:
            raise ValueError(f"{scenario_id} {field} must be positive")
    correlation = _finite_number(
        assumptions.get("constant_correlation_assumption"),
        f"{scenario_id} constant_correlation_assumption",
    )
    if correlation < 0 or correlation >= 1:
        raise ValueError(
            f"{scenario_id} constant correlation assumption must be in [0, 1)"
        )
    horizon = assumptions.get("horizon_sessions")
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1:
        raise ValueError(f"{scenario_id} horizon sessions must be positive")


def _validate_liquidity_assumptions(
    scenario_id: str,
    assumptions: Mapping[str, Any],
) -> None:
    multiplier = _finite_number(
        assumptions.get("liquidity_adv_multiplier"),
        f"{scenario_id} liquidity_adv_multiplier",
    )
    if multiplier <= 0 or multiplier > 1:
        raise ValueError(f"{scenario_id} liquidity ADV multiplier must be in (0, 1]")
    horizon = assumptions.get("liquidation_horizon_sessions")
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1:
        raise ValueError(f"{scenario_id} liquidation horizon must be positive")


def _validate_recovery_path(
    scenario_id: str,
    recovery: Sequence[Any],
    *,
    allow_empty: bool,
) -> None:
    if not recovery:
        if allow_empty:
            return
        raise ValueError(f"{scenario_id} recovery path cannot be empty")
    previous_offset = -1
    previous_fraction = float("inf")
    for index, row in enumerate(recovery):
        if not isinstance(row, dict):
            raise ValueError(f"{scenario_id} recovery row must be an object")
        offset = row.get("session_offset")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError(f"{scenario_id} recovery offset must be non-negative")
        fraction = _finite_number(
            row.get("remaining_shock_fraction"),
            f"{scenario_id} recovery fraction",
        )
        if fraction < 0 or fraction > 1:
            raise ValueError(f"{scenario_id} recovery fraction must be in [0, 1]")
        if offset <= previous_offset:
            raise ValueError(f"{scenario_id} recovery offsets must strictly increase")
        if fraction > previous_fraction + _EPSILON:
            raise ValueError(f"{scenario_id} recovery shock cannot increase")
        if index == 0 and (offset != 0 or abs(fraction - 1.0) > _EPSILON):
            raise ValueError(f"{scenario_id} recovery path must start at session 0 and 100 percent")
        previous_offset = offset
        previous_fraction = fraction


def _require_source_documents_unchanged(
    account: PaperDocument,
    proposal: PaperDocument,
) -> None:
    current_account = read_paper_document(account.path, expected_kind=ACCOUNT_DOCUMENT_KIND)
    current_proposal = read_paper_document(proposal.path, expected_kind=PROPOSAL_DOCUMENT_KIND)
    if not hmac.compare_digest(current_account.payload_sha256, account.payload_sha256):
        raise ValueError("paper account changed while stress evidence was being collected")
    if not hmac.compare_digest(current_proposal.payload_sha256, proposal.payload_sha256):
        raise ValueError("paper proposal changed while stress evidence was being collected")


def _validated_volatility_from_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    security_id: str,
    decision_date: date,
    required_observations: int,
    maximum_staleness_days: int,
) -> tuple[float | None, str | None, tuple[str, ...]]:
    """Validate and calculate volatility from the exact rows bound into the report."""
    blockers: list[str] = []
    if len(rows) < required_observations:
        blockers.append(f"minimum_price_observations:{required_observations}")
        return None, None, tuple(blockers)
    if any(row.get("actions_complete") is not True for row in rows):
        blockers.append("corporate_actions_unverified")
    if any(str(row.get("security_id", "")).strip() != security_id for row in rows):
        blockers.append("security_identity_mismatch")
    split_bases = {row.get("close_split_adjusted") for row in rows}
    if None in split_bases:
        blockers.append("split_adjustment_basis_unknown")
    if len(split_bases) != 1:
        blockers.append("mixed_split_adjustment_basis")

    try:
        row_dates = [_as_date(row["date"]) for row in rows]
    except (KeyError, TypeError, ValueError):
        blockers.append("invalid_price_date")
        return None, None, tuple(sorted(set(blockers)))
    latest_date = row_dates[-1]
    staleness = (decision_date - latest_date).days
    if staleness < 0 or staleness > maximum_staleness_days:
        blockers.append(f"stale_latest_price:{staleness}")
    expected_dates = us_equity_sessions(row_dates[0], decision_date + timedelta(days=1))
    if row_dates != expected_dates:
        blockers.append("noncontiguous_price_sessions")
    if blockers:
        return None, latest_date.isoformat(), tuple(sorted(set(blockers)))

    daily_returns, invalid = _daily_total_returns(list(rows))
    blockers.extend(invalid)
    if len(daily_returns) != TRADING_SESSIONS_PER_YEAR:
        blockers.append(f"minimum_daily_returns:{TRADING_SESSIONS_PER_YEAR}")
    if blockers:
        return None, latest_date.isoformat(), tuple(sorted(set(blockers)))
    annualized = stdev(daily_returns) * sqrt(TRADING_SESSIONS_PER_YEAR)
    if not isfinite(annualized) or annualized <= 0:
        blockers.append("annualized_volatility_unavailable")
        return None, latest_date.isoformat(), tuple(blockers)
    return float(annualized), latest_date.isoformat(), ()


def _canonical_price_evidence_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ticker": row.get("ticker"),
        "security_id": row.get("security_id"),
        "date": _json_value(row.get("date")),
        "close": row.get("close"),
        "dividends": row.get("dividends"),
        "split_ratio": row.get("split_ratio"),
        "actions_complete": row.get("actions_complete"),
        "close_split_adjusted": row.get("close_split_adjusted"),
        "split_normalization_factor": row.get("split_normalization_factor"),
        "split_normalization_through": _json_value(row.get("split_normalization_through")),
        "source": row.get("source"),
    }


def _canonical_revenue_fact_evidence_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "metric": row.get("metric"),
        "period_end": _json_value(row.get("period_end")),
        "as_of_date": _json_value(row.get("as_of_date")),
        "fiscal_period": row.get("fiscal_period"),
        "value": row.get("value"),
        "quarter_value": row.get("quarter_value"),
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _finite_nonnegative(value: Any, label: str) -> float:
    result = _finite_number(value, label)
    if result < 0:
        raise ValueError(f"{label} must be non-negative")
    return result


def _finite_positive(value: Any, label: str) -> float:
    result = _finite_number(value, label)
    if result <= 0:
        raise ValueError(f"{label} must be positive")
    return result


def _clean_float(value: float) -> float:
    result = float(value)
    return 0.0 if abs(result) <= _EPSILON else round(result, 12)


def _slug(value: str) -> str:
    return "-".join(
        part
        for part in "".join(
            character.lower() if character.isalnum() else " " for character in value
        ).split()
        if part
    )


def _sector_result_ids(base_scenario_id: str, sectors: Sequence[str]) -> dict[str, str]:
    """Create stable readable IDs without collapsing distinct sector labels."""
    sectors_by_slug: dict[str, list[str]] = {}
    for sector in sectors:
        sectors_by_slug.setdefault(_slug(sector), []).append(sector)

    result: dict[str, str] = {}
    for sector in sectors:
        slug = _slug(sector)
        result_id = f"{base_scenario_id}:{slug}"
        if len(sectors_by_slug[slug]) > 1:
            identity = _sha256_bytes(sector.encode("utf-8"))[:16]
            result_id = f"{result_id}:{identity}"
        result[sector] = result_id
    if len(result.values()) != len(set(result.values())):
        raise ValueError("sector labels could not be assigned unique stress result IDs")
    return result


def _sha256_bytes(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "DEFAULT_SCENARIO_BUNDLE",
    "DEFAULT_STRESS_REPORT_DIRECTORY",
    "GovernedStressReview",
    "ProposalStressInput",
    "STRESS_DOCUMENT_KIND",
    "StressPosition",
    "StressReviewReport",
    "StressScenarioBundle",
    "TargetStressEvidence",
    "build_proposal_stress_input",
    "build_stress_source_identity",
    "collect_target_stress_evidence",
    "default_stress_report_path",
    "evaluate_proposal_stress",
    "load_scenario_bundle",
    "read_stress_report",
    "review_paper_proposal_stress",
    "review_registered_paper_proposal_stress",
    "require_stress_report_sources_unchanged",
    "write_stress_report",
]

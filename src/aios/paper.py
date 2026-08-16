"""Durable, local-only U.S. paper portfolio workflow.

This module turns reviewed factor evidence into a supervised simulation.  It
does not contain broker credentials, network order APIs, or an unattended
execution path.  Every proposed rebalance is tied to a hash-checked account
snapshot, revalidated against the database, and applied only after the caller
explicitly confirms a simulated close-price execution.

The JSON envelope hash detects accidental or partial local edits.  It is an
integrity checksum, not a cryptographic signature against a malicious user who
can rewrite both the payload and its checksum.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from math import floor
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from aios.backtest.costs import TaxPolicy, TransactionCostPolicy
from aios.backtest.portfolio import PortfolioBook, PortfolioPeriodResult
from aios.factors.composite import compute_composite
from aios.market_calendar import us_equity_sessions
from aios.readiness import USReadinessPolicy, assess_us_readiness
from aios.risk import PortfolioRiskPolicy, TargetPosition, assess_portfolio_risk
from aios.storage.store import Store

ACCOUNT_DOCUMENT_KIND = "aios.paper-account"
PROPOSAL_DOCUMENT_KIND = "aios.paper-proposal"
DOCUMENT_SCHEMA_VERSION = 1
ACCOUNT_SCHEMA_VERSION = 1
PROPOSAL_SCHEMA_VERSION = 1
DEFAULT_ACCOUNT_RELATIVE_PATH = Path("data/paper/us_qv_sandbox.json")
DEFAULT_PROPOSAL_DIRECTORY = Path("data/paper/proposals")
SIMULATION_MODE = "simulation_only"
SECTOR_CLASSIFICATION = "SEC SIC division"
_MINIMUM_LIQUIDITY_OBSERVATIONS = 20
_NEW_YORK = ZoneInfo("America/New_York")
_CONSERVATIVE_REGULAR_CLOSE = time(16, 0)
_REGULAR_OPEN = time(9, 30)


@dataclass(frozen=True)
class PaperDocument:
    """A validated local JSON document and its canonical payload checksum."""

    path: Path
    kind: str
    payload: dict[str, Any]
    payload_sha256: str


class PaperExecutionTimingError(ValueError):
    """A valid proposal that is not currently inside its simulation window."""

    def __init__(
        self,
        status: str,
        detail: str,
        *,
        decision_date: date,
        execution_date: date,
        executable_after: datetime,
        expires_at: datetime,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.decision_date = decision_date
        self.execution_date = execution_date
        self.executable_after = executable_after
        self.expires_at = expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": False,
            "status": self.status,
            "detail": self.detail,
            "decision_date": self.decision_date.isoformat(),
            "execution_date": self.execution_date.isoformat(),
            "executable_after": _utc_timestamp(self.executable_after),
            "expires_at": _utc_timestamp(self.expires_at),
            "missing": [],
            "missing_count": 0,
        }


@dataclass(frozen=True)
class _PaperExecutionPreparation:
    account: PaperDocument
    proposal: PaperDocument
    book: PortfolioBook
    decision_date: date
    entry_date: date
    executable_after: datetime
    expires_at: datetime
    result: PortfolioPeriodResult


def canonical_payload_sha256(payload: dict[str, Any]) -> str:
    """Hash one JSON payload using stable key ordering and no NaN values."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_paper_document(path: Path, *, expected_kind: str | None = None) -> PaperDocument:
    """Read and checksum-validate one account or proposal document."""
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"paper document does not exist: {source}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"paper document is unreadable: {source}") from exc
    if not isinstance(raw, dict) or raw.get("document_schema_version") != 1:
        raise ValueError("unsupported paper document schema")
    kind = raw.get("document_kind")
    payload = raw.get("payload")
    stored_digest = raw.get("payload_sha256")
    if (
        not isinstance(kind, str)
        or not isinstance(payload, dict)
        or not isinstance(stored_digest, str)
    ):
        raise ValueError("invalid paper document envelope")
    if expected_kind is not None and kind != expected_kind:
        raise ValueError(f"expected {expected_kind!r}, found {kind!r}")
    actual_digest = canonical_payload_sha256(payload)
    if not hmac.compare_digest(stored_digest, actual_digest):
        raise ValueError("paper document checksum mismatch; restore or recreate it")
    return PaperDocument(source, kind, payload, actual_digest)


def initialize_paper_account(
    path: Path,
    store: Store,
    *,
    initial_capital: float = 100_000.0,
    commission_bps: float = 5.0,
    slippage_bps: float = 5.0,
    now: datetime | None = None,
) -> PaperDocument:
    """Create a new pre-tax U.S. simulation account without overwriting state."""
    destination = Path(path)
    if destination.exists():
        raise ValueError(f"paper account already exists: {destination}")
    timestamp = _utc_timestamp(now)
    book = PortfolioBook(
        store,
        initial_capital=initial_capital,
        transaction_costs=TransactionCostPolicy(
            commission_bps=commission_bps,
            slippage_bps=slippage_bps,
        ),
        tax_policy=TaxPolicy.zero(),
        calendar_ticker="SPY",
    )
    payload: dict[str, Any] = {
        "account_schema_version": ACCOUNT_SCHEMA_VERSION,
        "account_id": f"paper-account-{uuid4().hex}",
        "market": "US",
        "universe_id": "sp500",
        "strategy": "qv",
        "mode": SIMULATION_MODE,
        "created_at": timestamp,
        "updated_at": timestamp,
        "assumption_label": "generic pre-tax research sandbox",
        "jurisdiction_configured": False,
        "broker_connected": False,
        "portfolio": book.to_state(),
        "executions": [],
        "audit_events": [
            {
                "event": "account_initialized",
                "at": timestamp,
                "detail": "No broker connection; tax rates remain zero until configured.",
            }
        ],
    }
    with _paper_account_write_lock(destination):
        if destination.exists():
            raise ValueError(f"paper account already exists: {destination}")
        return _write_paper_document(
            destination,
            kind=ACCOUNT_DOCUMENT_KIND,
            payload=payload,
            replace=False,
        )


def default_proposal_path(project_root: Path, as_of: date) -> Path:
    """Return the conventional dated proposal path for the local sandbox."""
    return Path(project_root) / DEFAULT_PROPOSAL_DIRECTORY / f"us-qv-{as_of.isoformat()}.json"


def latest_reviewed_market_close(store: Store, *, today: date | None = None) -> date:
    """Return the newest action-safe SPY close available for valuation."""
    upper_bound = today or date.today()
    rows = store.query(
        """
        SELECT MAX(date) AS latest
        FROM prices
        WHERE ticker = 'SPY'
          AND date <= CAST(? AS DATE)
          AND actions_complete = TRUE
          AND close_split_adjusted IS NOT NULL
          AND close > 0
        """,
        (upper_bound.isoformat(),),
    )
    latest = rows[0]["latest"] if rows else None
    if not isinstance(latest, date):
        raise ValueError("no reviewed SPY market close is available")
    return latest


def latest_paper_decision_date(
    store: Store,
    *,
    today: date | None = None,
    policy: USReadinessPolicy | None = None,
) -> date:
    """Return the newest reviewed close covered by a dated investable universe.

    Market prices may arrive before the constituent universe has been independently
    certified through the same date. Valuation may use those newer prices, but a new
    portfolio decision must remain on the newest date where both clocks overlap.
    The full readiness assessor still validates identities, filings, prices, macro,
    and data integrity for the selected date.
    """
    upper_bound = today or date.today()
    rules = policy or USReadinessPolicy()
    rows = store.query(
        """
        WITH candidate_dates AS (
            SELECT DISTINCT price.date
            FROM prices AS price
            WHERE price.ticker = ?
              AND price.date <= CAST(? AS DATE)
              AND price.actions_complete IS TRUE
              AND price.close_split_adjusted IS NOT NULL
              AND price.split_normalization_factor IS NOT NULL
              AND price.close IS NOT NULL
              AND price.close > 0
        ), member_counts AS (
            SELECT candidate.date, COUNT(DISTINCT membership.ticker) AS members
            FROM candidate_dates AS candidate
            JOIN universe_membership AS membership
              ON membership.universe_id = ?
             AND membership.known_date <= candidate.date
             AND membership.effective_start <= candidate.date
             AND (
                 membership.effective_end IS NULL
                 OR membership.effective_end > candidate.date
                 OR membership.end_known_date > candidate.date
             )
            GROUP BY candidate.date
        )
        SELECT date
        FROM member_counts
        WHERE members BETWEEN ? AND ?
        ORDER BY date DESC
        LIMIT 1
        """,
        (
            rules.benchmark_ticker,
            upper_bound.isoformat(),
            rules.universe_id,
            rules.minimum_universe_members,
            rules.maximum_universe_members,
        ),
    )
    latest = rows[0]["date"] if rows else None
    if not isinstance(latest, date):
        raise ValueError("no reviewed market close overlaps a certified investable-universe date")
    return latest


def create_paper_proposal(
    account_path: Path,
    proposal_path: Path,
    as_of: date,
    store: Store,
    *,
    top_n: int = 10,
    risk_policy: PortfolioRiskPolicy | None = None,
    replace: bool = False,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    readiness_assessor: Callable[..., Any] = assess_us_readiness,
    composite_computer: Callable[..., Any] = compute_composite,
) -> PaperDocument:
    """Build one supervised rebalance proposal and persist its audit evidence."""
    account = read_paper_document(account_path, expected_kind=ACCOUNT_DOCUMENT_KIND)
    _validate_account_payload(account.payload)
    book = PortfolioBook.from_state(store, account.payload["portfolio"])
    if book.last_date is not None and book.last_date != as_of:
        raise ValueError(
            f"paper account is marked through {book.last_date}; run paper-mark through "
            f"{as_of} before proposing"
        )
    rules = risk_policy or PortfolioRiskPolicy()
    if not rules.minimum_positions <= top_n <= rules.maximum_positions:
        raise ValueError(
            f"top_n must be between {rules.minimum_positions} and {rules.maximum_positions}"
        )
    target_weight = 1.0 / top_n
    if target_weight > rules.maximum_position_weight + 1e-12:
        raise ValueError("top_n would breach the single-position risk limit")

    next_session = _next_us_session(as_of)
    generation_deadline = _proposal_generation_deadline(next_session)
    generated_moment = _current_moment(now, clock)
    _require_prospective_generation(generated_moment, generation_deadline)
    generated_at = _utc_timestamp(generated_moment)
    readiness = readiness_assessor(as_of, purpose="paper", store=store)
    payload: dict[str, Any] = {
        "proposal_schema_version": PROPOSAL_SCHEMA_VERSION,
        "proposal_id": f"paper-{as_of.isoformat()}-{uuid4().hex[:12]}",
        "account_id": account.payload["account_id"],
        "account_payload_sha256": account.payload_sha256,
        "market": "US",
        "universe_id": "sp500",
        "strategy": "qv",
        "mode": SIMULATION_MODE,
        "decision_date": as_of.isoformat(),
        "scheduled_simulation_date": next_session.isoformat(),
        "generated_at": generated_at,
        "readiness": readiness.to_dict(),
        "risk_policy": asdict(rules),
        "sector_classification": SECTOR_CLASSIFICATION,
        "targets": [],
        "exit_liquidity_evidence": [],
        "selection_skips": [],
        "factor_eligible_count": 0,
        "factor_evidence_sha256": None,
        "decision_evidence_sha256": None,
        "risk_assessment": None,
        "status": "blocked_readiness",
        "notice": (
            "Simulation only. This is a research portfolio proposal, not a personal buy or "
            "sell recommendation and not a broker order."
        ),
    }
    if readiness.ready:
        evidence = _build_decision_evidence(
            as_of,
            store,
            book,
            top_n=top_n,
            risk_policy=rules,
            composite_computer=composite_computer,
        )
        payload.update(evidence)
        payload["status"] = (
            "approved_for_supervised_simulation"
            if evidence["risk_assessment"]["approved"]
            else "blocked_risk"
        )
    completed_moment = _current_moment(now, clock)
    _require_prospective_generation(completed_moment, generation_deadline)
    payload["generated_at"] = _utc_timestamp(completed_moment)

    def proposal_write_guard() -> None:
        _require_prospective_generation(
            _current_moment(now, clock),
            generation_deadline,
        )
        _require_document_unchanged(account)

    with _paper_document_write_lock(proposal_path):
        return _write_paper_document(
            Path(proposal_path),
            kind=PROPOSAL_DOCUMENT_KIND,
            payload=payload,
            replace=replace,
            before_replace=proposal_write_guard,
        )


def execute_paper_proposal(
    account_path: Path,
    proposal_path: Path,
    store: Store,
    *,
    confirm_simulated: bool,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    readiness_assessor: Callable[..., Any] = assess_us_readiness,
    composite_computer: Callable[..., Any] = compute_composite,
) -> dict[str, Any]:
    """Apply an approved proposal after its next-session close is available."""
    if not confirm_simulated:
        raise ValueError("explicit --confirm-simulated approval is required")
    with _paper_account_write_lock(account_path):
        return _execute_paper_proposal_locked(
            account_path,
            proposal_path,
            store,
            now=now,
            clock=clock,
            readiness_assessor=readiness_assessor,
            composite_computer=composite_computer,
        )


def _execute_paper_proposal_locked(
    account_path: Path,
    proposal_path: Path,
    store: Store,
    *,
    now: datetime | None,
    clock: Callable[[], datetime] | None,
    readiness_assessor: Callable[..., Any],
    composite_computer: Callable[..., Any],
) -> dict[str, Any]:
    """Execute while the caller holds the account's cross-process write lock."""
    prepared = _prepare_paper_execution(
        account_path,
        proposal_path,
        store,
        now=now,
        clock=clock,
        readiness_assessor=readiness_assessor,
        composite_computer=composite_computer,
    )
    account = prepared.account
    proposal = prepared.proposal
    book = prepared.book
    decision_date = prepared.decision_date
    entry_date = prepared.entry_date
    result = prepared.result
    if result.ending_equity is None or result.missing:
        detail = _summarize_missing_execution_evidence(result.missing)
        raise ValueError(f"simulated execution refused: {detail}")

    proposal_id = str(proposal.payload["proposal_id"])
    executed_moment = _current_moment(now, clock)
    _validate_execution_timing(
        proposal.payload,
        decision_date=decision_date,
        entry_date=entry_date,
        now=executed_moment,
    )
    executed_at = _utc_timestamp(executed_moment)
    execution = {
        "proposal_id": proposal_id,
        "proposal_payload_sha256": proposal.payload_sha256,
        "decision_date": decision_date.isoformat(),
        "execution_date": entry_date.isoformat(),
        "executed_at": executed_at,
        "mode": SIMULATION_MODE,
        "starting_equity": result.starting_equity,
        "ending_equity": result.ending_equity,
        "turnover": result.turnover,
        "transaction_costs": result.transaction_costs,
        "taxes": result.taxes,
        "trades": [trade.to_dict() for trade in result.trades],
        "ending_holdings": list(result.ending_holdings),
    }
    updated = dict(account.payload)
    updated["portfolio"] = book.to_state()
    updated["executions"] = [*account.payload["executions"], execution]
    updated["updated_at"] = executed_at
    updated["audit_events"] = [
        *account.payload["audit_events"],
        {
            "event": "proposal_simulated",
            "at": executed_at,
            "proposal_id": proposal_id,
            "execution_date": entry_date.isoformat(),
        },
    ]

    def execution_write_guard() -> None:
        _validate_execution_timing(
            proposal.payload,
            decision_date=decision_date,
            entry_date=entry_date,
            now=_current_moment(now, clock),
        )
        _require_document_unchanged(account)
        _require_document_unchanged(proposal)

    updated_document = _write_paper_document(
        Path(account_path),
        kind=ACCOUNT_DOCUMENT_KIND,
        payload=updated,
        replace=True,
        before_replace=execution_write_guard,
    )
    return {"account": updated_document, "execution": execution}


def review_paper_proposal_execution(
    account_path: Path,
    proposal_path: Path,
    store: Store,
    *,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    readiness_assessor: Callable[..., Any] = assess_us_readiness,
    composite_computer: Callable[..., Any] = compute_composite,
) -> dict[str, Any]:
    """Preflight one proposal without writing the account or simulating a fill."""
    try:
        prepared = _prepare_paper_execution(
            account_path,
            proposal_path,
            store,
            now=now,
            clock=clock,
            readiness_assessor=readiness_assessor,
            composite_computer=composite_computer,
        )
    except PaperExecutionTimingError as exc:
        return exc.to_dict()

    result = prepared.result
    common = {
        "decision_date": prepared.decision_date.isoformat(),
        "execution_date": prepared.entry_date.isoformat(),
        "executable_after": _utc_timestamp(prepared.executable_after),
        "expires_at": _utc_timestamp(prepared.expires_at),
        "proposal_id": str(prepared.proposal.payload["proposal_id"]),
        "proposal_payload_sha256": prepared.proposal.payload_sha256,
        "account_payload_sha256": prepared.account.payload_sha256,
    }
    if result.ending_equity is None or result.missing:
        return {
            **common,
            "ready": False,
            "status": "waiting_for_execution_data",
            "detail": _summarize_missing_execution_evidence(result.missing),
            "missing": list(result.missing),
            "missing_count": len(result.missing),
        }
    return {
        **common,
        "ready": True,
        "status": "ready_for_confirmed_simulation",
        "detail": (
            "All checks and reviewed close-price evidence pass. Explicit simulated "
            "confirmation is still required."
        ),
        "missing": [],
        "missing_count": 0,
        "projected_trade_count": len(result.trades),
        "projected_transaction_costs": result.transaction_costs,
        "projected_ending_equity": result.ending_equity,
    }


def paper_proposal_timing_status(
    proposal: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    """Return a lightweight plain-language timing state without using market data."""
    decision_date = date.fromisoformat(str(proposal["decision_date"]))
    entry_date = date.fromisoformat(str(proposal["scheduled_simulation_date"]))
    if entry_date != _next_us_session(decision_date):
        raise ValueError("proposal execution date is not the next U.S. market session")
    executable_after, expires_at = _proposal_execution_window(entry_date)
    generation_deadline = _proposal_generation_deadline(entry_date)
    generated_at = _parse_utc_timestamp(proposal.get("generated_at"))
    if generated_at >= generation_deadline:
        raise ValueError(
            "proposal was not created prospectively before its scheduled simulation "
            "session opened; create a new proposal from a newer reviewed decision close"
        )

    current = _as_utc_datetime(now)
    if current < executable_after:
        status = "waiting_for_scheduled_close"
        detail = "Waiting for the scheduled U.S. close. The proposal cannot be simulated yet."
    elif current >= expires_at:
        status = "expired"
        detail = (
            "The simulation window expired when the following U.S. session opened. "
            "Retrospective fills are blocked."
        )
    else:
        status = "execution_window_open"
        detail = (
            "The scheduled close has passed. Run the read-only paper review before "
            "explicitly recording the local simulation."
        )
    return {
        "status": status,
        "detail": detail,
        "decision_date": decision_date.isoformat(),
        "execution_date": entry_date.isoformat(),
        "must_be_generated_before": _utc_timestamp(generation_deadline),
        "executable_after": _utc_timestamp(executable_after),
        "expires_at": _utc_timestamp(expires_at),
    }


def _prepare_paper_execution(
    account_path: Path,
    proposal_path: Path,
    store: Store,
    *,
    now: datetime | None,
    clock: Callable[[], datetime] | None,
    readiness_assessor: Callable[..., Any],
    composite_computer: Callable[..., Any],
) -> _PaperExecutionPreparation:
    account = read_paper_document(account_path, expected_kind=ACCOUNT_DOCUMENT_KIND)
    proposal = read_paper_document(proposal_path, expected_kind=PROPOSAL_DOCUMENT_KIND)
    _validate_account_payload(account.payload)
    _validate_proposal_payload(proposal.payload)
    if proposal.payload["mode"] != SIMULATION_MODE or account.payload["mode"] != SIMULATION_MODE:
        raise ValueError("only simulation-only paper documents are supported")
    if proposal.payload["status"] != "approved_for_supervised_simulation":
        raise ValueError(f"proposal is not approved: {proposal.payload['status']}")
    if proposal.payload["account_id"] != account.payload["account_id"]:
        raise ValueError("proposal belongs to a different paper account")
    if proposal.payload["account_payload_sha256"] != account.payload_sha256:
        raise ValueError("paper account changed after proposal; create a new proposal")
    proposal_id = str(proposal.payload["proposal_id"])
    if any(row.get("proposal_id") == proposal_id for row in account.payload["executions"]):
        raise ValueError("this proposal was already simulated")

    decision_date = date.fromisoformat(str(proposal.payload["decision_date"]))
    entry_date = date.fromisoformat(str(proposal.payload["scheduled_simulation_date"]))
    if entry_date != _next_us_session(decision_date):
        raise ValueError("proposal execution date is not the next U.S. market session")
    executable_after, expires_at = _validate_execution_timing(
        proposal.payload,
        decision_date=decision_date,
        entry_date=entry_date,
        now=_current_moment(now, clock),
    )

    readiness = readiness_assessor(decision_date, purpose="paper", store=store)
    if not readiness.ready:
        blockers = ", ".join(check.check for check in readiness.blockers)
        raise ValueError(f"current readiness recheck blocked simulation: {blockers}")

    book = PortfolioBook.from_state(store, account.payload["portfolio"])
    rules = PortfolioRiskPolicy(**proposal.payload["risk_policy"])
    rebuilt = _build_decision_evidence(
        decision_date,
        store,
        book,
        top_n=len(proposal.payload["targets"]),
        risk_policy=rules,
        composite_computer=composite_computer,
    )
    if rebuilt["decision_evidence_sha256"] != proposal.payload["decision_evidence_sha256"]:
        raise ValueError("decision evidence changed after proposal; create and review a new one")
    if not rebuilt["risk_assessment"]["approved"]:
        raise ValueError("risk recheck rejected the proposal")

    target_tickers = tuple(str(row["ticker"]) for row in proposal.payload["targets"])
    result = book.advance_period(target_tickers, decision_date, entry_date, entry_date)
    executable_after, expires_at = _validate_execution_timing(
        proposal.payload,
        decision_date=decision_date,
        entry_date=entry_date,
        now=_current_moment(now, clock),
    )
    _require_document_unchanged(account)
    _require_document_unchanged(proposal)
    return _PaperExecutionPreparation(
        account=account,
        proposal=proposal,
        book=book,
        decision_date=decision_date,
        entry_date=entry_date,
        executable_after=executable_after,
        expires_at=expires_at,
        result=result,
    )


def _validate_execution_timing(
    proposal: dict[str, Any],
    *,
    decision_date: date,
    entry_date: date,
    now: datetime | None,
) -> tuple[datetime, datetime]:
    timing = paper_proposal_timing_status(proposal, now=now)
    executable_after = _parse_utc_timestamp(timing["executable_after"])
    expires_at = _parse_utc_timestamp(timing["expires_at"])
    if timing["status"] == "waiting_for_scheduled_close":
        raise PaperExecutionTimingError(
            "waiting_for_scheduled_close",
            (
                "The scheduled U.S. session has not reached the conservative 4:00 p.m. "
                "New York close. No simulation can be recorded yet."
            ),
            decision_date=decision_date,
            execution_date=entry_date,
            executable_after=executable_after,
            expires_at=expires_at,
        )
    if timing["status"] == "expired":
        raise PaperExecutionTimingError(
            "expired",
            (
                "The proposal simulation window expired when the following U.S. "
                "session opened. Create a new prospective proposal; retrospective "
                "fills are refused."
            ),
            decision_date=decision_date,
            execution_date=entry_date,
            executable_after=executable_after,
            expires_at=expires_at,
        )
    return executable_after, expires_at


def _proposal_execution_window(entry_date: date) -> tuple[datetime, datetime]:
    """Return a conservative close-to-next-open supervised simulation window."""
    executable_after = datetime.combine(
        entry_date,
        _CONSERVATIVE_REGULAR_CLOSE,
        tzinfo=_NEW_YORK,
    )
    next_session = _next_us_session(entry_date)
    expires_at = datetime.combine(next_session, _REGULAR_OPEN, tzinfo=_NEW_YORK)
    return executable_after, expires_at


def _proposal_generation_deadline(entry_date: date) -> datetime:
    """Freeze target selection before any regular-session movement is observable."""
    return datetime.combine(entry_date, _REGULAR_OPEN, tzinfo=_NEW_YORK)


def _summarize_missing_execution_evidence(
    missing: tuple[str, ...],
    *,
    limit: int = 6,
) -> str:
    if not missing:
        return "unknown execution evidence failure"
    visible = ", ".join(missing[:limit])
    remainder = len(missing) - limit
    if remainder > 0:
        return f"{visible} (+{remainder} more)"
    return visible


def mark_paper_account(
    account_path: Path,
    through: date,
    store: Store,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Advance an invested simulation through one reviewed market close."""
    with _paper_account_write_lock(account_path):
        return _mark_paper_account_locked(account_path, through, store, now=now)


def _mark_paper_account_locked(
    account_path: Path,
    through: date,
    store: Store,
    *,
    now: datetime | None,
) -> dict[str, Any]:
    """Mark holdings while the caller owns the account write lock."""
    account = read_paper_document(account_path, expected_kind=ACCOUNT_DOCUMENT_KIND)
    _validate_account_payload(account.payload)
    book = PortfolioBook.from_state(store, account.payload["portfolio"])
    if book.last_date is None:
        raise ValueError("paper account has not simulated its first rebalance")
    points = book.mark_through(through)
    marked_at = _utc_timestamp(now)
    updated = dict(account.payload)
    updated["portfolio"] = book.to_state()
    updated["updated_at"] = marked_at
    updated["audit_events"] = [
        *account.payload["audit_events"],
        {
            "event": "account_marked",
            "at": marked_at,
            "through": through.isoformat(),
            "new_equity_points": len(points),
        },
    ]
    document = _write_paper_document(
        Path(account_path),
        kind=ACCOUNT_DOCUMENT_KIND,
        payload=updated,
        replace=True,
        before_replace=lambda: _require_document_unchanged(account),
    )
    return {"account": document, "points": [point.to_dict() for point in points]}


def paper_account_summary(account_path: Path, store: Store) -> dict[str, Any]:
    """Return a compact, plain-language-safe status payload for CLI and UI."""
    account = read_paper_document(account_path, expected_kind=ACCOUNT_DOCUMENT_KIND)
    _validate_account_payload(account.payload)
    book = PortfolioBook.from_state(store, account.payload["portfolio"])
    curve = book.curve
    return {
        "account_id": account.payload["account_id"],
        "mode": account.payload["mode"],
        "broker_connected": account.payload["broker_connected"],
        "jurisdiction_configured": account.payload["jurisdiction_configured"],
        "last_market_date": book.last_date.isoformat() if book.last_date else None,
        "equity": book.equity,
        "cash": book.cash,
        "holdings": [
            {"ticker": ticker, "weight": weight}
            for ticker, weight in sorted(book.current_weights().items())
        ],
        "drawdown": curve[-1].drawdown if curve else 0.0,
        "transaction_costs": book.transaction_costs,
        "accrued_taxes": book.accrued_taxes,
        "transaction_cost_policy": book.transaction_cost_policy.to_dict(),
        "tax_policy": book.tax_policy.to_dict(),
        "execution_count": len(account.payload["executions"]),
        "curve": [point.to_dict() for point in curve],
        "payload_sha256": account.payload_sha256,
        "notice": "Local simulation only; no broker orders can be sent.",
    }


def _build_decision_evidence(
    as_of: date,
    store: Store,
    book: PortfolioBook,
    *,
    top_n: int,
    risk_policy: PortfolioRiskPolicy,
    composite_computer: Callable[..., Any],
) -> dict[str, Any]:
    members = store.universe_membership_on("sp500", as_of)
    member_ids = {
        str(row["ticker"]).upper(): str(row["security_id"])
        for row in members
        if row.get("security_id")
    }
    tickers = sorted(str(row["ticker"]).upper() for row in members)
    factor_rows = composite_computer(tickers, as_of, store, include_market_factors=False)
    eligible = sorted(
        (row for row in factor_rows if row.qv_score is not None),
        key=lambda row: (-float(row.qv_score), str(row.ticker).upper()),
    )
    factor_evidence = [
        {
            "ticker": str(row.ticker).upper(),
            "qv_score": float(row.qv_score),
            "qv_rank": int(row.qv_rank) if row.qv_rank is not None else None,
            "quality_score": (float(row.quality_score) if row.quality_score is not None else None),
            "value_score": float(row.value_score) if row.value_score is not None else None,
            "quality_inputs": int(row.quality_components_available),
            "value_inputs": int(row.value_multiples_available),
            "macro_regime": str(row.macro_regime),
            "quality_weight": float(row.quality_weight),
            "value_weight": float(row.value_weight),
            "regime_pit_ready": bool(row.regime_pit_ready),
        }
        for row in eligible
    ]
    factor_digest = canonical_payload_sha256({"eligible": factor_evidence})

    metadata = {
        str(row["ticker"]).upper(): row
        for row in store.query("SELECT ticker, sic_code FROM securities")
    }
    position_ids = {
        str(row["ticker"]).upper(): str(row["security_id"])
        for row in book.to_state()["positions"]
        if row.get("security_id")
    }
    all_ids = sorted(set(member_ids.values()) | set(position_ids.values()))
    liquidity = _liquidity_by_security(store, all_ids, as_of)
    target_weight = 1.0 / top_n
    max_per_sector = floor((risk_policy.maximum_sector_weight + 1e-12) / target_weight)
    sector_counts: dict[str, int] = {}
    selected: list[dict[str, Any]] = []
    skips: list[dict[str, str]] = []
    for rank_index, row in enumerate(eligible, 1):
        ticker = str(row.ticker).upper()
        security_id = member_ids.get(ticker)
        sector = _sic_division((metadata.get(ticker) or {}).get("sic_code"))
        liquidity_row = liquidity.get(security_id or "")
        reason: str | None = None
        if security_id is None:
            reason = "missing stable security identity"
        elif sector is None:
            reason = "missing SEC SIC division"
        elif liquidity_row is None:
            reason = f"fewer than {_MINIMUM_LIQUIDITY_OBSERVATIONS} reviewed liquidity days"
        elif sector_counts.get(sector, 0) >= max_per_sector:
            reason = f"{sector} allocation limit reached"
        if reason is not None:
            if len(selected) < top_n:
                skips.append({"ticker": ticker, "reason": reason})
            continue
        selected.append(
            {
                "ticker": ticker,
                "security_id": security_id,
                "factor_rank": int(row.qv_rank or rank_index),
                "qv_score": float(row.qv_score),
                "quality_score": (
                    float(row.quality_score) if row.quality_score is not None else None
                ),
                "value_score": float(row.value_score) if row.value_score is not None else None,
                "target_weight": target_weight,
                "sector": sector,
                "average_daily_dollar_volume": liquidity_row["adv"],
                "liquidity_observations": liquidity_row["observations"],
            }
        )
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        if len(selected) == top_n:
            break

    selected_tickers = {row["ticker"] for row in selected}
    current_weights = book.current_weights()
    exit_evidence: list[dict[str, Any]] = []
    risk_targets = [
        TargetPosition(
            ticker=row["ticker"],
            weight=row["target_weight"],
            sector=row["sector"],
            average_daily_dollar_volume=row["average_daily_dollar_volume"],
        )
        for row in selected
    ]
    for ticker in sorted(set(current_weights) - selected_tickers):
        security_id = position_ids.get(ticker)
        sector = _sic_division((metadata.get(ticker) or {}).get("sic_code"))
        liquidity_row = liquidity.get(security_id or "")
        row = {
            "ticker": ticker,
            "security_id": security_id,
            "sector": sector,
            "average_daily_dollar_volume": liquidity_row["adv"] if liquidity_row else None,
            "liquidity_observations": liquidity_row["observations"] if liquidity_row else 0,
        }
        exit_evidence.append(row)
        risk_targets.append(
            TargetPosition(
                ticker=ticker,
                weight=0.0,
                sector=sector,
                average_daily_dollar_volume=row["average_daily_dollar_volume"],
            )
        )
    assessment = assess_portfolio_risk(
        risk_targets,
        equity=book.equity,
        peak_equity=book.peak_equity,
        current_weights=current_weights,
        policy=risk_policy,
    )
    assessment_payload = assessment.to_dict()
    decision_payload = {
        "factor_evidence_sha256": factor_digest,
        "targets": selected,
        "exit_liquidity_evidence": exit_evidence,
        "selection_skips": skips,
        "risk_assessment": assessment_payload,
    }
    return {
        **decision_payload,
        "factor_eligible_count": len(eligible),
        "factor_evidence_sha256": factor_digest,
        "decision_evidence_sha256": canonical_payload_sha256(decision_payload),
    }


def _liquidity_by_security(
    store: Store,
    security_ids: list[str],
    as_of: date,
) -> dict[str, dict[str, float | int]]:
    if not security_ids:
        return {}
    placeholders = ",".join("?" for _ in security_ids)
    rows = store.query(
        f"""
        WITH deduplicated AS (
            SELECT security_id, date, close, volume,
                   ROW_NUMBER() OVER (
                       PARTITION BY security_id, date
                       ORDER BY fetched_at DESC, ticker DESC
                   ) AS duplicate_rank
            FROM prices
            WHERE security_id IN ({placeholders})
              AND date <= CAST(? AS DATE)
              AND actions_complete = TRUE
              AND close_split_adjusted IS NOT NULL
              AND close > 0
              AND volume > 0
        ), recent AS (
            SELECT security_id, close, volume,
                   ROW_NUMBER() OVER (
                       PARTITION BY security_id ORDER BY date DESC
                   ) AS observation_rank
            FROM deduplicated
            WHERE duplicate_rank = 1
        )
        SELECT security_id,
               AVG(close * volume) AS adv,
               COUNT(*) AS observations
        FROM recent
        WHERE observation_rank <= {_MINIMUM_LIQUIDITY_OBSERVATIONS}
        GROUP BY security_id
        HAVING COUNT(*) >= {_MINIMUM_LIQUIDITY_OBSERVATIONS}
        """,
        (*security_ids, as_of.isoformat()),
    )
    return {
        str(row["security_id"]): {
            "adv": float(row["adv"]),
            "observations": int(row["observations"]),
        }
        for row in rows
    }


def _sic_division(value: object) -> str | None:
    """Map an SEC SIC code to its conservative, broad top-level division."""
    match = re.search(r"\d{3,4}", str(value or ""))
    if match is None:
        return None
    code = int(match.group())
    if 100 <= code <= 999:
        return "Agriculture, forestry and fishing"
    if 1000 <= code <= 1499:
        return "Mining"
    if 1500 <= code <= 1799:
        return "Construction"
    if 2000 <= code <= 3999:
        return "Manufacturing"
    if 4000 <= code <= 4999:
        return "Transport, communications and utilities"
    if 5000 <= code <= 5199:
        return "Wholesale trade"
    if 5200 <= code <= 5999:
        return "Retail trade"
    if 6000 <= code <= 6799:
        return "Finance, insurance and real estate"
    if 7000 <= code <= 8999:
        return "Services"
    if 9100 <= code <= 9729:
        return "Public administration"
    if 9900 <= code <= 9999:
        return "Nonclassifiable establishments"
    return None


def _next_us_session(as_of: date) -> date:
    sessions = us_equity_sessions(as_of + timedelta(days=1), as_of + timedelta(days=15))
    if not sessions:  # pragma: no cover - bounded calendar defensive guard
        raise ValueError(f"no U.S. market session found after {as_of}")
    return sessions[0]


def _validate_account_payload(payload: dict[str, Any]) -> None:
    if payload.get("account_schema_version") != ACCOUNT_SCHEMA_VERSION:
        raise ValueError("unsupported paper account schema")
    if payload.get("mode") != SIMULATION_MODE:
        raise ValueError("paper account is not simulation-only")
    if payload.get("broker_connected") is not False:
        raise ValueError("paper account must not have a broker connection")
    if not isinstance(payload.get("portfolio"), dict):
        raise ValueError("paper account is missing portfolio state")
    if not isinstance(payload.get("executions"), list) or not isinstance(
        payload.get("audit_events"), list
    ):
        raise ValueError("paper account audit state is invalid")


def _validate_proposal_payload(payload: dict[str, Any]) -> None:
    if payload.get("proposal_schema_version") != PROPOSAL_SCHEMA_VERSION:
        raise ValueError("unsupported paper proposal schema")
    if not isinstance(payload.get("targets"), list) or not payload.get("proposal_id"):
        raise ValueError("paper proposal payload is incomplete")


def _require_document_unchanged(expected: PaperDocument) -> None:
    current = read_paper_document(expected.path, expected_kind=expected.kind)
    if not hmac.compare_digest(current.payload_sha256, expected.payload_sha256):
        label = "paper account" if expected.kind == ACCOUNT_DOCUMENT_KIND else "paper proposal"
        raise ValueError(f"{label} changed while checks were running; retry from fresh evidence")


@contextmanager
def _paper_document_write_lock(document_path: Path) -> Iterator[None]:
    """Refuse concurrent document mutations instead of losing one update."""
    lock_path = Path(f"{Path(document_path)}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "another paper-document update is already in progress; wait for it to finish"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _paper_account_write_lock(account_path: Path) -> Iterator[None]:
    """Use the common document lock for account state transitions."""
    with _paper_document_write_lock(account_path):
        yield


@contextmanager
def document_write_lock(document_path: Path) -> Iterator[None]:
    """Public export of the write-once document lock for other frozen modules.

    `forward.py`'s trial persistence needs the exact same cross-process
    mutual exclusion this module already uses for account/proposal writes,
    rather than a second, potentially-divergent locking implementation.
    """
    with _paper_document_write_lock(document_path):
        yield


def _write_paper_document(
    path: Path,
    *,
    kind: str,
    payload: dict[str, Any],
    replace: bool,
    before_replace: Callable[[], None] | None = None,
) -> PaperDocument:
    destination = Path(path)
    if destination.exists() and not replace:
        raise ValueError(f"paper document already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = canonical_payload_sha256(payload)
    envelope = {
        "document_schema_version": DOCUMENT_SCHEMA_VERSION,
        "document_kind": kind,
        "payload_sha256": digest,
        "payload": payload,
    }
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(envelope, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace()
        if destination.exists() and not replace:
            raise ValueError(f"paper document already exists: {destination}")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return PaperDocument(destination, kind, payload, digest)


def _utc_timestamp(value: datetime | None) -> str:
    current = _as_utc_datetime(value)
    return current.isoformat().replace("+00:00", "Z")


def _as_utc_datetime(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("paper workflow timestamps must include an explicit timezone")
    return current.astimezone(UTC)


def _current_moment(
    now: datetime | None,
    clock: Callable[[], datetime] | None,
) -> datetime:
    if now is not None:
        return _as_utc_datetime(now)
    return _as_utc_datetime(clock() if clock is not None else None)


def _require_prospective_generation(
    current: datetime,
    generation_deadline: datetime,
) -> None:
    if current >= generation_deadline:
        raise ValueError(
            "paper proposal is no longer prospective: its scheduled simulation "
            f"session opened at {_utc_timestamp(generation_deadline)}; create a proposal "
            "from a newer reviewed decision close"
        )


def _parse_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("paper proposal is missing its generated timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("paper proposal generated timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("paper proposal generated timestamp must include a timezone")
    return parsed.astimezone(UTC)

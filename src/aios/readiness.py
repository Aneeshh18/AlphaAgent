"""Fail-closed operational readiness checks for the U.S. reference market.

Data can be recently downloaded without being safe for a portfolio decision.
This module keeps those concepts separate: raw source freshness is reported,
while readiness requires a dated universe, stable identities, action-safe
prices, PIT fundamentals, release-aware macro evidence, and a benchmark.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Literal

from aios.macro.regime import compute_regime
from aios.storage.store import Store, get_store

ReadinessPurpose = Literal["historical_research", "paper"]
ReadinessStatus = Literal["pass", "warn", "fail"]


@dataclass(frozen=True)
class USReadinessPolicy:
    """Explicit operating thresholds for the U.S. S&P 500 reference build."""

    universe_id: str = "sp500"
    benchmark_ticker: str = "SPY"
    minimum_universe_members: int = 450
    maximum_universe_members: int = 550
    minimum_member_coverage: float = 0.95
    maximum_market_data_age_days: int = 7
    maximum_paper_decision_age_days: int = 7

    def __post_init__(self) -> None:
        if not self.universe_id.strip():
            raise ValueError("readiness universe_id is required")
        if not self.benchmark_ticker.strip():
            raise ValueError("readiness benchmark_ticker is required")
        if self.minimum_universe_members < 1:
            raise ValueError("minimum_universe_members must be positive")
        if self.maximum_universe_members < self.minimum_universe_members:
            raise ValueError("maximum_universe_members cannot be below the minimum")
        if not 0 < self.minimum_member_coverage <= 1:
            raise ValueError("minimum_member_coverage must be in (0, 1]")
        if self.maximum_market_data_age_days < 0:
            raise ValueError("maximum_market_data_age_days cannot be negative")
        if self.maximum_paper_decision_age_days < 0:
            raise ValueError("maximum_paper_decision_age_days cannot be negative")


@dataclass(frozen=True)
class ReadinessCheck:
    """One human-readable operating gate."""

    check: str
    label: str
    status: ReadinessStatus
    observed: str
    required: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class USReadinessReport:
    """Evidence-backed readiness result for one decision date and purpose."""

    as_of: str
    purpose: ReadinessPurpose
    generated_on: str
    universe_id: str
    benchmark_ticker: str
    certified_research_from: str | None
    certified_research_through: str | None
    raw_prices_through: str | None
    fundamentals_through: str | None
    macro_releases_through: str | None
    checks: tuple[ReadinessCheck, ...]

    @property
    def ready(self) -> bool:
        return all(check.status != "fail" for check in self.checks)

    @property
    def blockers(self) -> tuple[ReadinessCheck, ...]:
        return tuple(check for check in self.checks if check.status == "fail")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["ready"] = self.ready
        result["checks"] = [check.to_dict() for check in self.checks]
        return result


def assess_us_readiness(
    as_of: date | str | None = None,
    *,
    purpose: ReadinessPurpose = "paper",
    policy: USReadinessPolicy | None = None,
    store: Store | None = None,
    today: date | None = None,
) -> USReadinessReport:
    """Assess whether the U.S. data is safe for research or paper decisions.

    ``paper`` additionally requires a decision date close to the actual current
    date. Historical research can be ready on an older certified date without
    pretending that the same evidence is current.
    """
    if purpose not in {"historical_research", "paper"}:
        raise ValueError("purpose must be 'historical_research' or 'paper'")
    db = store or get_store()
    rules = policy or USReadinessPolicy()
    current_date = today or date.today()
    decision_date = _as_date(as_of or current_date)
    checks: list[ReadinessCheck] = []

    decision_age = (current_date - decision_date).days
    if decision_age < 0:
        decision_status: ReadinessStatus = "fail"
        decision_detail = "A decision cannot use a future date."
    elif purpose == "paper" and decision_age > rules.maximum_paper_decision_age_days:
        decision_status = "fail"
        decision_detail = "Paper decisions must use a recent reviewed market date."
    else:
        decision_status = "pass"
        decision_detail = (
            "The decision date is current enough for paper monitoring."
            if purpose == "paper"
            else "The date is valid for a bounded historical research run."
        )
    checks.append(
        ReadinessCheck(
            "decision_date",
            "Decision date",
            decision_status,
            f"{decision_date.isoformat()} ({decision_age} days old)",
            (
                f"no more than {rules.maximum_paper_decision_age_days} days old"
                if purpose == "paper"
                else "not in the future"
            ),
            decision_detail,
        )
    )

    quality = db.data_quality_report()
    failures = [row["check"] for row in quality if row["status"] == "fail"]
    warnings = [row["check"] for row in quality if row["status"] == "warn"]
    quality_status: ReadinessStatus = "fail" if failures else ("warn" if warnings else "pass")
    checks.append(
        ReadinessCheck(
            "data_integrity",
            "Database integrity",
            quality_status,
            f"{len(failures)} failures, {len(warnings)} warnings",
            "zero integrity failures",
            (
                "Blocking checks: " + ", ".join(failures)
                if failures
                else "Warnings remain visible, but no fail-closed integrity check is broken."
            ),
        )
    )

    members = db.universe_membership_on(rules.universe_id, decision_date)
    member_count = len(members)
    member_count_ok = (
        rules.minimum_universe_members <= member_count <= rules.maximum_universe_members
    )
    checks.append(
        ReadinessCheck(
            "universe_membership",
            "Dated investable universe",
            "pass" if member_count_ok else "fail",
            f"{member_count} members",
            (
                f"{rules.minimum_universe_members}–{rules.maximum_universe_members} "
                "publicly known members"
            ),
            (
                "The S&P 500 membership snapshot is available for this date."
                if member_count_ok
                else "Extend and independently review membership announcements through this date."
            ),
        )
    )

    identified_count = sum(bool(row.get("security_id")) for row in members)
    checks.append(
        ReadinessCheck(
            "stable_security_identity",
            "Stable security identities",
            "pass" if member_count > 0 and identified_count == member_count else "fail",
            f"{identified_count}/{member_count} members",
            "every member mapped to one reviewed security ID",
            (
                "Every member has a stable listed-security identity."
                if member_count > 0 and identified_count == member_count
                else "Review additions, deletions, ticker changes, and provider symbols first."
            ),
        )
    )

    coverage = (
        db.universe_data_coverage(rules.universe_id, decision_date) if member_count else []
    )
    price_covered = sum(bool(row["has_price_history"]) for row in coverage)
    fundamental_covered = sum(bool(row["has_pit_fundamentals"]) for row in coverage)
    _append_coverage_check(
        checks,
        check="fundamental_coverage",
        label="Point-in-time company filings",
        covered=fundamental_covered,
        total=member_count,
        minimum=rules.minimum_member_coverage,
        success_detail="Enough members have company filings that were public by this date.",
        failure_detail="Refresh and identity-link the missing issuer filings before use.",
    )
    _append_coverage_check(
        checks,
        check="price_history_coverage",
        label="Price-history coverage",
        covered=price_covered,
        total=member_count,
        minimum=rules.minimum_member_coverage,
        success_detail="Enough members have identity-safe historical prices.",
        failure_detail="Refresh and identity-link the missing market histories before use.",
    )

    latest_reviewed_by_security = _latest_reviewed_prices_by_security(
        db,
        [str(row["security_id"]) for row in members if row.get("security_id")],
        decision_date,
    )
    fresh_reviewed = sum(
        0 <= (decision_date - observed).days <= rules.maximum_market_data_age_days
        for observed in latest_reviewed_by_security.values()
    )
    _append_coverage_check(
        checks,
        check="reviewed_price_freshness",
        label="Reviewed current prices and corporate actions",
        covered=fresh_reviewed,
        total=member_count,
        minimum=rules.minimum_member_coverage,
        success_detail="Recent member prices have reviewed split and dividend fields.",
        failure_detail=(
            "Action-safe member prices do not reach this decision date; raw downloads do not "
            "satisfy this gate."
        ),
    )

    benchmark_latest = _latest_action_safe_ticker_date(
        db, rules.benchmark_ticker, decision_date
    )
    benchmark_age = (
        (decision_date - benchmark_latest).days if benchmark_latest is not None else None
    )
    benchmark_ok = (
        benchmark_age is not None
        and 0 <= benchmark_age <= rules.maximum_market_data_age_days
    )
    checks.append(
        ReadinessCheck(
            "benchmark_freshness",
            "Benchmark and market calendar",
            "pass" if benchmark_ok else "fail",
            (
                f"{rules.benchmark_ticker} through {benchmark_latest.isoformat()}"
                if benchmark_latest
                else f"no action-safe {rules.benchmark_ticker} observation"
            ),
            f"within {rules.maximum_market_data_age_days} days of the decision",
            (
                "The benchmark can define sessions and comparison returns."
                if benchmark_ok
                else "Refresh the action-safe benchmark series before paper monitoring."
            ),
        )
    )

    try:
        macro = compute_regime(decision_date, db)
        macro_ready = macro.is_pit_ready
        macro_observed = macro.regime if macro_ready else ", ".join(macro.missing) or "missing"
    except Exception as exc:  # pragma: no cover - defensive external-state boundary
        macro_ready = False
        macro_observed = type(exc).__name__
    checks.append(
        ReadinessCheck(
            "macro_pit_readiness",
            "Release-dated economic evidence",
            "pass" if macro_ready else "fail",
            macro_observed,
            "all mandatory inputs public by the decision date",
            (
                "The economic regime uses only release-dated evidence."
                if macro_ready
                else "Refresh or repair the missing release-aware macro series."
            ),
        )
    )

    reviewed_from, reviewed_through = _broad_reviewed_price_bounds(
        db, decision_date, rules
    )
    universe_from = _earliest_universe_date(db, rules)
    universe_through = _latest_universe_coverage_date(db, decision_date, rules)
    certified_candidates = [
        value for value in (reviewed_through, universe_through) if value is not None
    ]
    certified_through = min(certified_candidates) if len(certified_candidates) == 2 else None
    certified_start_candidates = [
        value for value in (reviewed_from, universe_from) if value is not None
    ]
    certified_from = (
        max(certified_start_candidates) if len(certified_start_candidates) == 2 else None
    )

    return USReadinessReport(
        as_of=decision_date.isoformat(),
        purpose=purpose,
        generated_on=current_date.isoformat(),
        universe_id=rules.universe_id,
        benchmark_ticker=rules.benchmark_ticker,
        certified_research_from=_iso(certified_from),
        certified_research_through=_iso(certified_through),
        raw_prices_through=_iso(_max_date(db, "prices", "date")),
        fundamentals_through=_iso(_max_date(db, "fundamentals", "as_of_date")),
        macro_releases_through=_iso(
            _max_date(db, "macro", "release_date", "release_date IS NOT NULL")
        ),
        checks=tuple(checks),
    )


def _append_coverage_check(
    checks: list[ReadinessCheck],
    *,
    check: str,
    label: str,
    covered: int,
    total: int,
    minimum: float,
    success_detail: str,
    failure_detail: str,
) -> None:
    ratio = covered / total if total else 0.0
    ok = total > 0 and ratio >= minimum
    checks.append(
        ReadinessCheck(
            check,
            label,
            "pass" if ok else "fail",
            f"{covered}/{total} ({ratio:.1%})",
            f"at least {minimum:.0%}",
            success_detail if ok else failure_detail,
        )
    )


def _latest_reviewed_prices_by_security(
    store: Store,
    security_ids: list[str],
    as_of: date,
) -> dict[str, date]:
    if not security_ids:
        return {}
    placeholders = ", ".join("?" for _ in security_ids)
    rows = store.query(
        f"""
        SELECT security_id, MAX(date) AS latest
        FROM prices
        WHERE security_id IN ({placeholders})
          AND date <= CAST(? AS DATE)
          AND close IS NOT NULL
          AND actions_complete IS TRUE
          AND close_split_adjusted IS NOT NULL
          AND split_normalization_factor IS NOT NULL
        GROUP BY security_id
        """,
        (*security_ids, as_of.isoformat()),
    )
    return {str(row["security_id"]): row["latest"] for row in rows if row["latest"]}


def _latest_action_safe_ticker_date(store: Store, ticker: str, as_of: date) -> date | None:
    row = store.query(
        """
        SELECT MAX(date) AS latest
        FROM prices
        WHERE ticker = ?
          AND date <= CAST(? AS DATE)
          AND close IS NOT NULL
          AND actions_complete IS TRUE
          AND close_split_adjusted IS NOT NULL
          AND split_normalization_factor IS NOT NULL
        """,
        (ticker.upper(), as_of.isoformat()),
    )[0]
    return row["latest"]


def _broad_reviewed_price_bounds(
    store: Store,
    as_of: date,
    policy: USReadinessPolicy,
) -> tuple[date | None, date | None]:
    row = store.query(
        """
        WITH sessions AS (
            SELECT DISTINCT date
            FROM prices
            WHERE ticker = ?
              AND date <= CAST(? AS DATE)
              AND close IS NOT NULL
              AND actions_complete IS TRUE
              AND close_split_adjusted IS NOT NULL
              AND split_normalization_factor IS NOT NULL
        ), coverage AS (
            SELECT session.date,
                   COUNT(DISTINCT membership.ticker) AS members,
                   COUNT(DISTINCT CASE WHEN price.security_id IS NOT NULL
                                       THEN membership.ticker END) AS covered
            FROM sessions AS session
            JOIN universe_membership AS membership
              ON membership.universe_id = ?
             AND membership.known_date <= session.date
             AND membership.effective_start <= session.date
             AND (
                 membership.effective_end IS NULL
                 OR membership.effective_end > session.date
                 OR membership.end_known_date > session.date
             )
            LEFT JOIN prices AS price
              ON price.security_id = membership.security_id
             AND price.date = session.date
             AND price.close IS NOT NULL
             AND price.actions_complete IS TRUE
             AND price.close_split_adjusted IS NOT NULL
             AND price.split_normalization_factor IS NOT NULL
            GROUP BY session.date
        )
        SELECT MIN(date) AS earliest, MAX(date) AS latest
        FROM coverage
        WHERE members BETWEEN ? AND ?
          AND covered >= members * ?
        """,
        (
            policy.benchmark_ticker,
            as_of.isoformat(),
            policy.universe_id,
            policy.minimum_universe_members,
            policy.maximum_universe_members,
            policy.minimum_member_coverage,
        ),
    )[0]
    return row["earliest"], row["latest"]


def _earliest_universe_date(store: Store, policy: USReadinessPolicy) -> date | None:
    row = store.query(
        """
        SELECT MIN(effective_start) AS earliest
        FROM universe_membership
        WHERE universe_id = ?
        """,
        (policy.universe_id,),
    )[0]
    return row["earliest"]


def _latest_universe_coverage_date(
    store: Store,
    as_of: date,
    policy: USReadinessPolicy,
) -> date | None:
    current_count = len(store.universe_membership_on(policy.universe_id, as_of))
    if policy.minimum_universe_members <= current_count <= policy.maximum_universe_members:
        return as_of
    candidates = store.query(
        """
        SELECT DISTINCT CAST(effective_end - INTERVAL 1 DAY AS DATE) AS candidate
        FROM universe_membership
        WHERE universe_id = ?
          AND effective_end IS NOT NULL
          AND effective_end - INTERVAL 1 DAY <= CAST(? AS DATE)
        ORDER BY candidate DESC
        """,
        (policy.universe_id, as_of.isoformat()),
    )
    for row in candidates:
        candidate = row["candidate"]
        count = len(store.universe_membership_on(policy.universe_id, candidate))
        if policy.minimum_universe_members <= count <= policy.maximum_universe_members:
            return candidate
    return None


def _max_date(store: Store, table: str, column: str, where: str | None = None) -> date | None:
    sql = f"SELECT MAX({column}) AS latest FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return store.query(sql)[0]["latest"]


def _as_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None

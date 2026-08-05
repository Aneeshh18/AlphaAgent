"""Fail-visible refresh orchestration for the reviewed U.S. reference market.

This module refreshes only identities already present in the audited local
universe. It deliberately does not discover or approve index membership
changes; announcement/effective-date provenance remains a separate review
workflow.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from functools import partial
from typing import Any

import httpx

from aios.config import settings
from aios.storage.store import Store, get_store

ProgressCallback = Callable[[str, str, int, int], None]
DEFAULT_CONSECUTIVE_TRANSPORT_FAILURE_LIMIT = 3


@dataclass(frozen=True)
class RefreshFailure:
    """One source or identity that failed without hiding successful peers."""

    kind: str
    identity: str
    error: str


@dataclass(frozen=True)
class USRefreshResult:
    """Auditable summary of one sequential U.S. current-data refresh."""

    as_of: str
    universe_id: str
    members: int
    issuers_attempted: int
    securities_attempted: int
    macro_rows: int
    fundamental_rows: int
    price_rows: int
    benchmark_rows: int
    failures: tuple[RefreshFailure, ...]
    warnings: tuple[RefreshFailure, ...] = ()
    membership_as_of: str | None = None

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ok"] = self.ok
        return payload


def refresh_us_current(
    as_of: date | str | None = None,
    *,
    today: date | None = None,
    universe_id: str = "sp500",
    benchmark_ticker: str = "SPY",
    include_prices: bool = True,
    include_benchmark: bool | None = None,
    include_fundamentals: bool = True,
    include_macro: bool = True,
    minimum_members: int = 450,
    maximum_members: int = 550,
    maximum_membership_age_days: int = 7,
    consecutive_transport_failure_limit: int = (
        DEFAULT_CONSECUTIVE_TRANSPORT_FAILURE_LIMIT
    ),
    store: Store | None = None,
    macro_ingester: Callable[[], int] | None = None,
    issuer_ingester: Callable[[str], int] | None = None,
    security_price_ingester: Callable[[str], int] | None = None,
    benchmark_ingester: Callable[[str], int] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    progress: ProgressCallback | None = None,
) -> USRefreshResult:
    """Refresh current reviewed U.S. identities and retain every failure.

    Network work is sequential because DuckDB is an embedded single-writer
    store and the free providers require polite pacing. Re-running is safe:
    price adapters fetch a short overlap and all storage writes are idempotent.
    """
    refresh_benchmark = include_prices if include_benchmark is None else include_benchmark
    if not any((include_prices, refresh_benchmark, include_fundamentals, include_macro)):
        raise ValueError("at least one refresh area must be enabled")
    if minimum_members < 1 or maximum_members < minimum_members:
        raise ValueError("invalid universe member bounds")
    if maximum_membership_age_days < 0:
        raise ValueError("maximum_membership_age_days cannot be negative")
    if consecutive_transport_failure_limit < 1:
        raise ValueError("consecutive transport failure limit must be positive")
    current_date = today or date.today()
    db = store or get_store()

    requested_membership_date = _as_date(as_of) if as_of is not None else None
    if requested_membership_date is not None:
        membership_age = (current_date - requested_membership_date).days
        if membership_age < 0:
            raise ValueError("reviewed membership date cannot be in the future")
        if membership_age > maximum_membership_age_days:
            raise ValueError(
                "reviewed membership date is too old for current refresh: "
                f"{membership_age} days; maximum is {maximum_membership_age_days}"
            )
        membership_candidates = [requested_membership_date]
    else:
        membership_candidates = [
            current_date - timedelta(days=days_ago)
            for days_ago in range(maximum_membership_age_days + 1)
        ]

    membership_date: date | None = None
    members: list[dict] = []
    for candidate in membership_candidates:
        candidate_members = db.universe_membership_on(universe_id, candidate)
        if minimum_members <= len(candidate_members) <= maximum_members:
            membership_date = candidate
            members = candidate_members
            break
    if membership_date is None:
        raise ValueError(
            f"{universe_id} has no reviewed {minimum_members}-{maximum_members} member "
            f"snapshot within {maximum_membership_age_days} days of {current_date}; "
            "review membership announcements before extending the decision date"
        )

    missing_security = sorted(
        str(member.get("ticker") or "unknown")
        for member in members
        if not str(member.get("security_id") or "").strip()
    )
    if missing_security:
        raise ValueError(
            "current universe has members without reviewed security IDs: "
            + ", ".join(missing_security[:5])
        )
    security_ids = sorted({str(member["security_id"]).strip() for member in members})

    failures: list[RefreshFailure] = []
    warnings: list[RefreshFailure] = []
    macro_rows = 0
    fundamental_rows = 0
    price_rows = 0
    benchmark_rows = 0

    if macro_ingester is None:
        from aios.ingest.fred import ingest_macro

        macro_ingester = partial(ingest_macro, store=db)
    if issuer_ingester is None:
        from aios.ingest.edgar import ingest_issuer

        issuer_ingester = partial(ingest_issuer, store=db)
    if security_price_ingester is None:
        from aios.ingest.prices import ingest_security_prices

        security_price_ingester = partial(ingest_security_prices, store=db)
    if benchmark_ingester is None:
        from aios.ingest.prices import ingest_prices

        benchmark_ingester = partial(ingest_prices, store=db)

    if include_macro:
        _progress(progress, "macro", "release-aware series", 1, 1)
        try:
            macro_rows = _positive_rows(macro_ingester(), "macro sources")
        except Exception as exc:
            failures.append(RefreshFailure("macro", "required-series", str(exc)))

    issuer_ids: list[str] = []
    issuers_attempted = 0
    if include_fundamentals:
        for security_id in security_ids:
            try:
                issuer_id = db.issuer_id_for_security(security_id, membership_date)
            except Exception as exc:
                failures.append(RefreshFailure("issuer_identity", security_id, str(exc)))
                continue
            if not issuer_id:
                failures.append(
                    RefreshFailure(
                        "issuer_identity",
                        security_id,
                        f"no reviewed issuer on {membership_date}",
                    )
                )
                continue
            issuer_ids.append(issuer_id)
        issuer_ids = sorted(set(issuer_ids))
        consecutive_transport_failures = 0
        for index, issuer_id in enumerate(issuer_ids, 1):
            issuers_attempted = index
            _progress(progress, "fundamentals", issuer_id, index, len(issuer_ids))
            try:
                rows = int(issuer_ingester(issuer_id))
                consecutive_transport_failures = 0
                if rows > 0:
                    fundamental_rows += rows
                elif db.issuer_has_fundamentals(issuer_id):
                    failures.append(
                        RefreshFailure(
                            "fundamentals",
                            issuer_id,
                            "established issuer unexpectedly returned no rows",
                        )
                    )
                else:
                    warnings.append(
                        RefreshFailure(
                            "fundamentals_pending",
                            issuer_id,
                            "reviewed issuer has not published accepted Company Facts yet",
                        )
                    )
            except Exception as exc:
                failures.append(RefreshFailure("fundamentals", issuer_id, str(exc)))
                consecutive_transport_failures = (
                    consecutive_transport_failures + 1
                    if isinstance(exc, httpx.RequestError)
                    else 0
                )
                if (
                    consecutive_transport_failures
                    >= consecutive_transport_failure_limit
                ):
                    remaining = len(issuer_ids) - index
                    if remaining:
                        failures.append(
                            RefreshFailure(
                                "fundamentals_transport_circuit",
                                f"{remaining} issuer(s) not attempted",
                                "systemic provider transport failures reached the "
                                "bounded circuit-breaker limit",
                            )
                        )
                    break

    price_candidates: list[str] = []
    securities_attempted = 0
    if include_prices:
        window_end = membership_date + timedelta(days=1)
        for security_id in security_ids:
            try:
                mappings = db.provider_symbol_mappings(
                    security_id,
                    start=membership_date,
                    end=window_end,
                )
            except Exception as exc:
                failures.append(RefreshFailure("provider_identity", security_id, str(exc)))
                continue
            if not mappings:
                failures.append(
                    RefreshFailure(
                        "provider_identity",
                        security_id,
                        f"no reviewed provider on {membership_date}",
                    )
                )
                continue
            price_candidates.append(security_id)

        consecutive_transport_failures = 0
        for index, security_id in enumerate(price_candidates, 1):
            securities_attempted = index
            _progress(progress, "prices", security_id, index, len(price_candidates))
            try:
                price_rows += _positive_rows(
                    security_price_ingester(security_id),
                    f"security {security_id}",
                )
                consecutive_transport_failures = 0
            except Exception as exc:
                failures.append(RefreshFailure("prices", security_id, str(exc)))
                consecutive_transport_failures = (
                    consecutive_transport_failures + 1
                    if isinstance(exc, httpx.RequestError)
                    else 0
                )
                if (
                    consecutive_transport_failures
                    >= consecutive_transport_failure_limit
                ):
                    remaining = len(price_candidates) - index
                    if remaining:
                        failures.append(
                            RefreshFailure(
                                "prices_transport_circuit",
                                f"{remaining} security(s) not attempted",
                                "systemic provider transport failures reached the "
                                "bounded circuit-breaker limit",
                            )
                        )
                    break
            if index < len(price_candidates):
                sleeper(settings.yfinance_sleep_sec)

    if refresh_benchmark:
        _progress(progress, "benchmark", benchmark_ticker.upper(), 1, 1)
        try:
            benchmark_rows = _positive_rows(
                benchmark_ingester(benchmark_ticker.upper()),
                f"benchmark {benchmark_ticker.upper()}",
            )
        except Exception as exc:
            failures.append(
                RefreshFailure("benchmark", benchmark_ticker.upper(), str(exc))
            )

    return USRefreshResult(
        as_of=current_date.isoformat(),
        universe_id=universe_id,
        members=len(members),
        issuers_attempted=issuers_attempted,
        securities_attempted=securities_attempted,
        macro_rows=macro_rows,
        fundamental_rows=fundamental_rows,
        price_rows=price_rows,
        benchmark_rows=benchmark_rows,
        failures=tuple(failures),
        warnings=tuple(warnings),
        membership_as_of=membership_date.isoformat(),
    )


def _positive_rows(value: int, label: str) -> int:
    rows = int(value)
    if rows <= 0:
        raise ValueError(f"{label} returned no rows")
    return rows


def _as_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _progress(
    callback: ProgressCallback | None,
    kind: str,
    identity: str,
    index: int,
    total: int,
) -> None:
    if callback is not None:
        callback(kind, identity, index, total)

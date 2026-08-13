"""Market, venue, and listing contracts (INDIA_BUILD_PLAN.md phase I1).

The schema tables alone are not a usable contract: raw SQL lets a caller
insert an invalid timezone, a made-up currency, or a listing whose
`known_at` postdates the date it claims to cover. This module is the
validated entry point, and it enforces the identity discipline the rest of
the system already depends on:

* a market is a country + base currency + timezone + default venue;
* a venue is one exchange inside exactly one market;
* a listing binds an existing, stable `security_id` to one venue's symbol,
  series and ISIN over a half-open `[listed_start, listed_end)` interval;
* `known_at` records when that listing fact became *publicly knowable*, and
  is checked separately from the interval itself.

That last point is the whole reason this file exists rather than raw
inserts. `active_listing()` takes both `as_of` (which interval covers this
date) and `known_as_of` (what a decision made on this date could have
known), exactly like `universe_membership`'s `effective_*` / `known_date`
split. Collapsing the two would silently reintroduce look-ahead bias
through the identity layer — the same class of bug that made every
historical price lookup fail earlier in this project's history.

Nothing here is India-specific. `us_equity` uses the identical calls; see
`tests/test_market_contracts.py` for the parity proof.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from aios.storage.store import Store

# ISO 3166-1 alpha-2, ISO 4217, ISO 10383 MIC. Shape checks only: this
# deliberately does not ship a bundled copy of each registry, because a stale
# embedded list would reject a valid new code and be worse than no check.
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_MIC_RE = re.compile(r"^[A-Z0-9]{4}$")
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
# ISO 6166: 2-letter country prefix, 9 alphanumeric, 1 check digit.
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


@dataclass(frozen=True)
class Market:
    """One country-level equity market."""

    market_id: str
    country: str
    base_currency: str
    timezone: str
    default_venue_id: str
    source: str


@dataclass(frozen=True)
class Venue:
    """One exchange inside exactly one market."""

    venue_id: str
    market_id: str
    mic: str | None
    name: str
    source: str


@dataclass(frozen=True)
class SecurityListing:
    """One security's dated listing on one venue."""

    security_id: str
    venue_id: str
    symbol: str
    series: str | None
    isin: str | None
    security_type: str
    currency: str
    listed_start: date
    listed_end: date | None
    known_at: date
    source: str


def _identifier(value: Any, *, field: str) -> str:
    text = str(value or "").strip().lower()
    if not _IDENTIFIER_RE.match(text):
        raise ValueError(
            f"{field} must be lowercase alphanumeric/underscore, 1-64 chars: {value!r}"
        )
    return text


def _required_text(value: Any, *, field: str, maximum: int = 200) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    if len(text) > maximum:
        raise ValueError(f"{field} must not exceed {maximum} characters")
    return text


def _country(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not _COUNTRY_RE.match(text):
        raise ValueError(f"country must be an ISO 3166-1 alpha-2 code: {value!r}")
    return text


def _currency(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not _CURRENCY_RE.match(text):
        raise ValueError(f"currency must be an ISO 4217 alpha-3 code: {value!r}")
    return text


def _timezone(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("timezone is required")
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"timezone must be a valid IANA name: {value!r}") from exc
    return text


def _optional_mic(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip().upper()
    if not _MIC_RE.match(text):
        raise ValueError(f"mic must be a 4-character ISO 10383 code: {value!r}")
    return text


def _optional_isin(value: Any) -> str | None:
    """Validate ISIN shape and check digit, or return None.

    The check digit is verified because a transposed ISIN is otherwise a
    silent identity error — it looks structurally fine and routes data to the
    wrong company, which is exactly the failure this system exists to prevent.
    """
    if value is None or not str(value).strip():
        return None
    text = str(value).strip().upper()
    if not _ISIN_RE.match(text):
        raise ValueError(f"isin must match ISO 6166 shape: {value!r}")
    digits = "".join(
        str(int(char, 36)) if char.isalpha() else char for char in text[:-1]
    )
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = int(char)
        if index % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    if (10 - total % 10) % 10 != int(text[-1]):
        raise ValueError(f"isin check digit is invalid: {value!r}")
    return text


def _as_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def register_market(
    store: Store,
    *,
    market_id: str,
    country: str,
    base_currency: str,
    timezone: str,
    default_venue_id: str,
    source: str,
) -> Market:
    """Register or replace one market's identity."""
    market = Market(
        market_id=_identifier(market_id, field="market_id"),
        country=_country(country),
        base_currency=_currency(base_currency),
        timezone=_timezone(timezone),
        default_venue_id=_identifier(default_venue_id, field="default_venue_id"),
        source=_required_text(source, field="source"),
    )
    store.execute(
        """
        INSERT OR REPLACE INTO markets
            (market_id, country, base_currency, timezone, default_venue_id, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            market.market_id,
            market.country,
            market.base_currency,
            market.timezone,
            market.default_venue_id,
            market.source,
        ),
    )
    return market


def register_venue(
    store: Store,
    *,
    venue_id: str,
    market_id: str,
    name: str,
    mic: str | None = None,
    source: str,
) -> Venue:
    """Register or replace one venue, requiring its market to already exist."""
    normalized_market = _identifier(market_id, field="market_id")
    if not store.query("SELECT 1 FROM markets WHERE market_id = ?", (normalized_market,)):
        raise ValueError(f"venue references an unregistered market: {market_id!r}")
    venue = Venue(
        venue_id=_identifier(venue_id, field="venue_id"),
        market_id=normalized_market,
        mic=_optional_mic(mic),
        name=_required_text(name, field="name"),
        source=_required_text(source, field="source"),
    )
    store.execute(
        """
        INSERT OR REPLACE INTO venues (venue_id, market_id, mic, name, source)
        VALUES (?, ?, ?, ?, ?)
        """,
        (venue.venue_id, venue.market_id, venue.mic, venue.name, venue.source),
    )
    return venue


def register_security_listing(
    store: Store,
    *,
    security_id: str,
    venue_id: str,
    symbol: str,
    currency: str,
    listed_start: date | str,
    known_at: date | str,
    source: str,
    series: str | None = None,
    isin: str | None = None,
    security_type: str = "common_stock",
    listed_end: date | str | None = None,
) -> SecurityListing:
    """Register one dated venue listing for an existing stable security.

    ``known_at`` may legitimately postdate ``listed_start`` — a listing is
    often reviewed after it began — but it may never postdate ``listed_end``,
    because a listing that ended before anyone could know it existed cannot
    have informed any decision.
    """
    normalized_venue = _identifier(venue_id, field="venue_id")
    if not store.query("SELECT 1 FROM venues WHERE venue_id = ?", (normalized_venue,)):
        raise ValueError(f"listing references an unregistered venue: {venue_id!r}")
    normalized_security = _required_text(security_id, field="security_id")
    if not store.query(
        "SELECT 1 FROM security_master WHERE security_id = ?", (normalized_security,)
    ):
        raise ValueError(
            f"listing references an unregistered security: {security_id!r}"
        )

    start = _as_date(listed_start)
    end = _as_date(listed_end) if listed_end else None
    known = _as_date(known_at)
    if end is not None and end <= start:
        raise ValueError("listed_end must be after listed_start")
    if end is not None and known > end:
        raise ValueError("known_at cannot postdate listed_end")

    listing = SecurityListing(
        security_id=normalized_security,
        venue_id=normalized_venue,
        symbol=_required_text(symbol, field="symbol", maximum=32).upper(),
        series=(str(series).strip().upper() or None) if series else None,
        isin=_optional_isin(isin),
        security_type=_required_text(security_type, field="security_type"),
        currency=_currency(currency),
        listed_start=start,
        listed_end=end,
        known_at=known,
        source=_required_text(source, field="source"),
    )
    store.execute(
        """
        INSERT OR REPLACE INTO security_listings
            (security_id, venue_id, symbol, series, isin, security_type, currency,
             listed_start, listed_end, known_at, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            listing.security_id,
            listing.venue_id,
            listing.symbol,
            listing.series,
            listing.isin,
            listing.security_type,
            listing.currency,
            listing.listed_start,
            listing.listed_end,
            listing.known_at,
            listing.source,
        ),
    )
    return listing


def market(store: Store, market_id: str) -> Market | None:
    """Return one registered market, or None."""
    rows = store.query(
        "SELECT * FROM markets WHERE market_id = ?",
        (_identifier(market_id, field="market_id"),),
    )
    if not rows:
        return None
    row = rows[0]
    return Market(
        market_id=str(row["market_id"]),
        country=str(row["country"]),
        base_currency=str(row["base_currency"]),
        timezone=str(row["timezone"]),
        default_venue_id=str(row["default_venue_id"]),
        source=str(row["source"]),
    )


def venues_for_market(store: Store, market_id: str) -> list[Venue]:
    """Return every venue registered under one market, ordered by venue_id."""
    rows = store.query(
        "SELECT * FROM venues WHERE market_id = ? ORDER BY venue_id",
        (_identifier(market_id, field="market_id"),),
    )
    return [
        Venue(
            venue_id=str(row["venue_id"]),
            market_id=str(row["market_id"]),
            mic=str(row["mic"]) if row["mic"] else None,
            name=str(row["name"]),
            source=str(row["source"]),
        )
        for row in rows
    ]


def active_listing(
    store: Store,
    *,
    security_id: str,
    venue_id: str,
    as_of: date | str,
    known_as_of: date | str | None = None,
) -> SecurityListing | None:
    """Return the listing covering ``as_of`` that was knowable by ``known_as_of``.

    ``known_as_of`` defaults to ``as_of``. Passing them separately is what
    makes a historical query honest: a listing reviewed after the decision
    date must not inform that decision, even though its interval covers it.
    Returns None when no listing satisfies both conditions — an absence, not
    an error, because "this security had no known listing here" is a real and
    useful answer.
    """
    moment = _as_date(as_of)
    known_moment = _as_date(known_as_of) if known_as_of is not None else moment
    rows = store.query(
        """
        SELECT * FROM security_listings
        WHERE security_id = ?
          AND venue_id = ?
          AND listed_start <= CAST(? AS DATE)
          AND (listed_end IS NULL OR listed_end > CAST(? AS DATE))
          AND known_at <= CAST(? AS DATE)
        ORDER BY listed_start DESC
        LIMIT 1
        """,
        (
            _required_text(security_id, field="security_id"),
            _identifier(venue_id, field="venue_id"),
            moment.isoformat(),
            moment.isoformat(),
            known_moment.isoformat(),
        ),
    )
    if not rows:
        return None
    row = rows[0]
    return SecurityListing(
        security_id=str(row["security_id"]),
        venue_id=str(row["venue_id"]),
        symbol=str(row["symbol"]),
        series=str(row["series"]) if row["series"] else None,
        isin=str(row["isin"]) if row["isin"] else None,
        security_type=str(row["security_type"]),
        currency=str(row["currency"]),
        listed_start=_as_date(row["listed_start"]),
        listed_end=_as_date(row["listed_end"]) if row["listed_end"] else None,
        known_at=_as_date(row["known_at"]),
        source=str(row["source"]),
    )

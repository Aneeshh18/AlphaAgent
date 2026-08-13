"""Contracts for the market/venue/listing registration layer.

The point of this layer over raw SQL is validation and point-in-time
honesty. These tests pin both: garbage identity data is refused at the
boundary, and `active_listing` respects `known_at` separately from the
listing interval so a later-reviewed listing cannot inform an earlier
decision.
"""

from __future__ import annotations

from datetime import date

import pytest

from aios.markets import (
    active_listing,
    market,
    register_market,
    register_security_listing,
    register_venue,
    venues_for_market,
)
from aios.storage.store import Store

SECURITY_ID = "aios:test:in:reliance"
# Reliance Industries' real ISIN, used because a fabricated one would fail
# the check-digit validation this module deliberately enforces.
ISIN = "INE002A01018"


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "markets.duckdb")
    db.execute(
        "INSERT INTO security_master "
        "(security_id, canonical_ticker, security_type, identity_status, source) "
        "VALUES (?, 'RELIANCE', 'common_stock', 'verified_ticker_change', 'test')",
        (SECURITY_ID,),
    )
    try:
        yield db
    finally:
        db.close()


def _register_india(store: Store) -> None:
    register_market(
        store,
        market_id="in_equity",
        country="IN",
        base_currency="INR",
        timezone="Asia/Kolkata",
        default_venue_id="xnse",
        source="test",
    )
    register_venue(
        store,
        venue_id="xnse",
        market_id="in_equity",
        name="National Stock Exchange of India",
        mic="XNSE",
        source="test",
    )


# ----------------------------------------------------------------------
# Registration and validation
# ----------------------------------------------------------------------
def test_market_round_trips(store) -> None:
    _register_india(store)
    loaded = market(store, "in_equity")
    assert loaded is not None
    assert loaded.country == "IN"
    assert loaded.base_currency == "INR"
    assert loaded.timezone == "Asia/Kolkata"


def test_market_is_none_when_unregistered(store) -> None:
    assert market(store, "does_not_exist") is None


def test_market_rejects_an_invalid_timezone(store) -> None:
    with pytest.raises(ValueError, match="IANA"):
        register_market(
            store,
            market_id="in_equity",
            country="IN",
            base_currency="INR",
            timezone="Asia/Bengaluru",  # not a real IANA zone
            default_venue_id="xnse",
            source="test",
        )


def test_market_rejects_a_non_iso_country_or_currency(store) -> None:
    with pytest.raises(ValueError, match="ISO 3166"):
        register_market(
            store,
            market_id="in_equity",
            country="India",
            base_currency="INR",
            timezone="Asia/Kolkata",
            default_venue_id="xnse",
            source="test",
        )
    with pytest.raises(ValueError, match="ISO 4217"):
        register_market(
            store,
            market_id="in_equity",
            country="IN",
            base_currency="Rupee",
            timezone="Asia/Kolkata",
            default_venue_id="xnse",
            source="test",
        )


def test_venue_requires_a_registered_market(store) -> None:
    with pytest.raises(ValueError, match="unregistered market"):
        register_venue(
            store,
            venue_id="xnse",
            market_id="in_equity",
            name="NSE",
            source="test",
        )


def test_venues_for_market_lists_both_indian_venues(store) -> None:
    _register_india(store)
    register_venue(
        store,
        venue_id="xbom",
        market_id="in_equity",
        name="BSE Limited",
        mic="XBOM",
        source="test",
    )
    venues = venues_for_market(store, "in_equity")
    assert [v.venue_id for v in venues] == ["xbom", "xnse"]
    assert {v.mic for v in venues} == {"XBOM", "XNSE"}


def test_listing_requires_registered_venue_and_security(store) -> None:
    _register_india(store)
    with pytest.raises(ValueError, match="unregistered venue"):
        register_security_listing(
            store,
            security_id=SECURITY_ID,
            venue_id="xnowhere",
            symbol="RELIANCE",
            currency="INR",
            listed_start="2025-01-01",
            known_at="2025-01-01",
            source="test",
        )
    with pytest.raises(ValueError, match="unregistered security"):
        register_security_listing(
            store,
            security_id="aios:test:in:ghost",
            venue_id="xnse",
            symbol="GHOST",
            currency="INR",
            listed_start="2025-01-01",
            known_at="2025-01-01",
            source="test",
        )


def test_listing_validates_the_isin_check_digit(store) -> None:
    """A transposed ISIN routes data to the wrong company silently."""
    _register_india(store)
    with pytest.raises(ValueError, match="check digit"):
        register_security_listing(
            store,
            security_id=SECURITY_ID,
            venue_id="xnse",
            symbol="RELIANCE",
            isin="INE002A01019",  # real shape, wrong check digit
            currency="INR",
            listed_start="2025-01-01",
            known_at="2025-01-01",
            source="test",
        )


def test_listing_rejects_an_inverted_interval(store) -> None:
    _register_india(store)
    with pytest.raises(ValueError, match="listed_end must be after"):
        register_security_listing(
            store,
            security_id=SECURITY_ID,
            venue_id="xnse",
            symbol="RELIANCE",
            currency="INR",
            listed_start="2025-06-01",
            listed_end="2025-01-01",
            known_at="2025-01-01",
            source="test",
        )


def test_listing_rejects_known_at_after_listed_end(store) -> None:
    _register_india(store)
    with pytest.raises(ValueError, match="known_at cannot postdate"):
        register_security_listing(
            store,
            security_id=SECURITY_ID,
            venue_id="xnse",
            symbol="RELIANCE",
            currency="INR",
            listed_start="2025-01-01",
            listed_end="2025-06-01",
            known_at="2025-09-01",
            source="test",
        )


# ----------------------------------------------------------------------
# Point-in-time resolution
# ----------------------------------------------------------------------
def test_active_listing_resolves_inside_its_interval(store) -> None:
    _register_india(store)
    register_security_listing(
        store,
        security_id=SECURITY_ID,
        venue_id="xnse",
        symbol="RELIANCE",
        series="EQ",
        isin=ISIN,
        currency="INR",
        listed_start="2025-01-01",
        known_at="2025-01-01",
        source="test",
    )
    found = active_listing(
        store, security_id=SECURITY_ID, venue_id="xnse", as_of="2025-06-30"
    )
    assert found is not None
    assert found.symbol == "RELIANCE"
    assert found.isin == ISIN
    assert found.series == "EQ"
    assert found.currency == "INR"


def test_active_listing_excludes_dates_before_the_interval(store) -> None:
    _register_india(store)
    register_security_listing(
        store,
        security_id=SECURITY_ID,
        venue_id="xnse",
        symbol="RELIANCE",
        currency="INR",
        listed_start="2025-01-01",
        known_at="2025-01-01",
        source="test",
    )
    assert (
        active_listing(
            store, security_id=SECURITY_ID, venue_id="xnse", as_of="2024-12-31"
        )
        is None
    )


def test_active_listing_treats_listed_end_as_half_open(store) -> None:
    _register_india(store)
    register_security_listing(
        store,
        security_id=SECURITY_ID,
        venue_id="xnse",
        symbol="RELIANCE",
        currency="INR",
        listed_start="2025-01-01",
        listed_end="2025-07-01",
        known_at="2025-01-01",
        source="test",
    )
    assert (
        active_listing(
            store, security_id=SECURITY_ID, venue_id="xnse", as_of="2025-06-30"
        )
        is not None
    )
    # The end date itself is excluded: [start, end).
    assert (
        active_listing(
            store, security_id=SECURITY_ID, venue_id="xnse", as_of="2025-07-01"
        )
        is None
    )


def test_active_listing_hides_a_listing_reviewed_after_the_decision(store) -> None:
    """The core point-in-time guarantee of this module.

    The listing interval covers 2025-03-01, but nobody knew the fact until
    2025-08-01. A decision made in March must not see it; the same query
    made in September must.
    """
    _register_india(store)
    register_security_listing(
        store,
        security_id=SECURITY_ID,
        venue_id="xnse",
        symbol="RELIANCE",
        currency="INR",
        listed_start="2025-01-01",
        known_at="2025-08-01",
        source="test",
    )

    assert (
        active_listing(
            store,
            security_id=SECURITY_ID,
            venue_id="xnse",
            as_of="2025-03-01",
            known_as_of="2025-03-01",
        )
        is None
    )
    assert (
        active_listing(
            store,
            security_id=SECURITY_ID,
            venue_id="xnse",
            as_of="2025-03-01",
            known_as_of="2025-09-01",
        )
        is not None
    )


def test_active_listing_defaults_known_as_of_to_as_of(store) -> None:
    _register_india(store)
    register_security_listing(
        store,
        security_id=SECURITY_ID,
        venue_id="xnse",
        symbol="RELIANCE",
        currency="INR",
        listed_start="2025-01-01",
        known_at="2025-08-01",
        source="test",
    )
    # Without an explicit known_as_of, the strict same-date reading applies.
    assert (
        active_listing(
            store, security_id=SECURITY_ID, venue_id="xnse", as_of="2025-03-01"
        )
        is None
    )


def test_active_listing_picks_the_latest_covering_interval(store) -> None:
    """A re-listing under a new symbol supersedes the older interval."""
    _register_india(store)
    register_security_listing(
        store,
        security_id=SECURITY_ID,
        venue_id="xnse",
        symbol="OLDNAME",
        currency="INR",
        listed_start="2024-01-01",
        listed_end="2025-01-01",
        known_at="2024-01-01",
        source="test",
    )
    register_security_listing(
        store,
        security_id=SECURITY_ID,
        venue_id="xnse",
        symbol="RELIANCE",
        currency="INR",
        listed_start="2025-01-01",
        known_at="2025-01-01",
        source="test",
    )
    earlier = active_listing(
        store, security_id=SECURITY_ID, venue_id="xnse", as_of="2024-06-30"
    )
    later = active_listing(
        store, security_id=SECURITY_ID, venue_id="xnse", as_of="2025-06-30"
    )
    assert earlier is not None and earlier.symbol == "OLDNAME"
    assert later is not None and later.symbol == "RELIANCE"


def test_listing_dates_round_trip_as_dates(store) -> None:
    _register_india(store)
    listing = register_security_listing(
        store,
        security_id=SECURITY_ID,
        venue_id="xnse",
        symbol="RELIANCE",
        currency="INR",
        listed_start="2025-01-01",
        known_at="2025-01-01",
        source="test",
    )
    assert listing.listed_start == date(2025, 1, 1)
    assert listing.listed_end is None
    found = active_listing(
        store, security_id=SECURITY_ID, venue_id="xnse", as_of="2025-06-30"
    )
    assert found is not None
    assert found.listed_start == date(2025, 1, 1)

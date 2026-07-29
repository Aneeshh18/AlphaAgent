"""Deterministic U.S. equity sessions for bounded provider QA.

This is deliberately a small calendar, not an execution venue simulator.  It
models full-day NYSE/Nasdaq closures needed to distinguish missing price rows
from weekends and exchange holidays.  Early closes remain trading sessions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_NEW_YORK = ZoneInfo("America/New_York")
_REGULAR_CLOSE = time(16, 0)
_EOD_FINALIZATION_DELAY = timedelta(minutes=30)

# Full-market exceptional closures since the beginning of AIOS price history.
# Scheduled holidays are generated below.  Keep this set source-reviewed when
# extending certification into a new one-off closure.
_EXCEPTIONAL_CLOSURES = {
    date(2001, 9, 11),
    date(2001, 9, 12),
    date(2001, 9, 13),
    date(2001, 9, 14),
    date(2004, 6, 11),  # President Reagan national day of mourning
    date(2007, 1, 2),  # President Ford national day of mourning
    date(2012, 10, 29),
    date(2012, 10, 30),  # Hurricane Sandy
    date(2018, 12, 5),  # President George H. W. Bush national day of mourning
    date(2025, 1, 9),  # President Carter national day of mourning
}


def us_equity_sessions(start: date, end: date) -> list[date]:
    """Return scheduled U.S. equity sessions in the half-open range."""
    if end <= start:
        return []
    holidays: set[date] = set(_EXCEPTIONAL_CLOSURES)
    for year in range(start.year, end.year + 1):
        holidays.update(_scheduled_holidays(year))
    output: list[date] = []
    current = start
    while current < end:
        if current.weekday() < 5 and current not in holidays:
            output.append(current)
        current += timedelta(days=1)
    return output


def latest_completed_us_equity_session(now: datetime | None = None) -> date:
    """Return the newest session whose conservative EOD-ready time has elapsed.

    Early-close sessions remain conservatively incomplete until 16:00 ET. This
    function then waits another 30 minutes for free providers to finalize the
    daily candle. This avoids treating a just-closed partial bar as final.
    """
    checked_at = now or datetime.now(UTC)
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("now must include a timezone")
    checked_at = checked_at.astimezone(UTC)
    new_york_date = checked_at.astimezone(_NEW_YORK).date()
    candidates = us_equity_sessions(
        new_york_date - timedelta(days=14),
        new_york_date + timedelta(days=1),
    )
    completed = [
        session
        for session in candidates
        if (
            datetime.combine(session, _REGULAR_CLOSE, tzinfo=_NEW_YORK).astimezone(UTC)
            + _EOD_FINALIZATION_DELAY
        )
        <= checked_at
    ]
    if not completed:
        raise ValueError("no completed U.S. equity session found in the calendar window")
    return completed[-1]


def _scheduled_holidays(year: int) -> set[date]:
    holidays = {
        _new_years_day(year),
        _nth_weekday(year, 2, weekday=0, occurrence=3),  # Washington's Birthday
        _good_friday(year),
        _last_weekday(year, 5, weekday=0),  # Memorial Day
        _observed_fixed_holiday(year, 7, 4),
        _nth_weekday(year, 9, weekday=0, occurrence=1),  # Labor Day
        _nth_weekday(year, 11, weekday=3, occurrence=4),  # Thanksgiving
        _observed_fixed_holiday(year, 12, 25),
    }
    if year >= 1998:
        holidays.add(_nth_weekday(year, 1, weekday=0, occurrence=3))
    if year >= 2022:
        holidays.add(_observed_fixed_holiday(year, 6, 19))
    return holidays


def _new_years_day(year: int) -> date:
    holiday = date(year, 1, 1)
    # NYSE does not move a Saturday January 1 closure to the preceding Friday.
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def _observed_fixed_holiday(year: int, month: int, day: int) -> date:
    holiday = date(year, month, day)
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def _nth_weekday(
    year: int,
    month: int,
    *,
    weekday: int,
    occurrence: int,
) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, *, weekday: int) -> date:
    following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = following - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _good_friday(year: int) -> date:
    """Gregorian computus followed by the Friday before Easter Sunday."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day) - timedelta(days=2)

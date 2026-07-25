from datetime import date, datetime
from zoneinfo import ZoneInfo

from aios.market_calendar import latest_completed_us_equity_session, us_equity_sessions


def test_us_equity_calendar_matches_reviewed_2025_2026_windows() -> None:
    assert len(us_equity_sessions(date(2025, 1, 1), date(2026, 7, 21))) == 386
    assert len(us_equity_sessions(date(2025, 12, 11), date(2026, 7, 21))) == 150
    assert len(us_equity_sessions(date(2026, 5, 21), date(2026, 7, 21))) == 40
    assert len(us_equity_sessions(date(2026, 6, 29), date(2026, 7, 21))) == 15


def test_us_equity_calendar_excludes_official_closures_but_keeps_early_closes() -> None:
    sessions_2025 = set(us_equity_sessions(date(2025, 1, 1), date(2026, 1, 1)))
    assert date(2025, 1, 9) not in sessions_2025
    assert date(2025, 11, 28) in sessions_2025

    sessions_2026 = set(us_equity_sessions(date(2026, 1, 1), date(2027, 1, 1)))
    for holiday in (
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    ):
        assert holiday not in sessions_2026
    assert date(2026, 11, 27) in sessions_2026
    assert date(2026, 12, 24) in sessions_2026


def test_latest_completed_session_uses_new_york_close_not_india_midnight() -> None:
    india = ZoneInfo("Asia/Kolkata")

    assert latest_completed_us_equity_session(
        datetime(2026, 7, 24, 1, 5, tzinfo=india)
    ) == date(2026, 7, 22)
    assert latest_completed_us_equity_session(
        datetime(2026, 7, 24, 2, 0, tzinfo=india)
    ) == date(2026, 7, 23)

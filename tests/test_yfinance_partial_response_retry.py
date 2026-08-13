"""Regression tests for throttled/partial Yahoo responses.

A throttled Yahoo response is *non-empty* but carries no usable close on its
newest row. Before this contract existed, that first partial response won
immediately: the retry loop broke on attempt 1, and the missing close surfaced
downstream as a hard "Close must be positive" failure that failed the entire
daily cycle. Different tickers failed on each run while every one of them
returned a valid close when requested individually moments later.

Retrying must not weaken the fail-closed guarantee. Genuinely absent provider
data (observed live on a real 2:1 split date, where Yahoo served
Open/High/Low but a NaN Close) must still exhaust every attempt and still be
rejected rather than stored.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pandas as pd
import pytest

from aios.ingest.prices import _has_usable_latest_close


def _frame(closes: list[float | None], *, multiindex: bool = False) -> pd.DataFrame:
    # OHLC must stay internally consistent or the storage validator rejects the
    # row for that reason instead of the missing close under test.
    reference = [value if value is not None else 100.0 for value in closes]
    rows = {
        "Open": reference,
        "High": [value + 1.0 for value in reference],
        "Low": [value - 1.0 for value in reference],
        "Close": closes,
        "Adj Close": closes,
        "Volume": [1000] * len(closes),
        "Dividends": [0.0] * len(closes),
        "Stock Splits": [0.0] * len(closes),
    }
    frame = pd.DataFrame(
        rows,
        index=pd.to_datetime(
            [f"2026-08-{4 + offset:02d}" for offset in range(len(closes))]
        ),
    )
    if multiindex:
        frame.columns = pd.MultiIndex.from_product([frame.columns, ["TEST"]])
    return frame


def test_usable_latest_close_accepts_a_complete_response() -> None:
    assert _has_usable_latest_close(_frame([100.0, 101.0, 102.0])) is True


def test_usable_latest_close_rejects_a_nan_newest_close() -> None:
    """The exact shape a throttled response takes: rows present, close missing."""
    assert _has_usable_latest_close(_frame([100.0, 101.0, None])) is False


def test_usable_latest_close_rejects_a_nonpositive_newest_close() -> None:
    assert _has_usable_latest_close(_frame([100.0, 0.0])) is False


def test_usable_latest_close_rejects_an_empty_response() -> None:
    assert _has_usable_latest_close(pd.DataFrame()) is False


def test_usable_latest_close_handles_multiindex_columns() -> None:
    """Newer yfinance returns MultiIndex columns even for a single ticker."""
    assert _has_usable_latest_close(_frame([100.0, 101.0], multiindex=True)) is True
    assert _has_usable_latest_close(_frame([100.0, None], multiindex=True)) is False


def test_usable_latest_close_ignores_an_earlier_missing_close() -> None:
    """Only the newest row gates a retry.

    An older gap is a data-quality question for the validation layer, not a
    reason to re-request the whole window.
    """
    assert _has_usable_latest_close(_frame([None, 101.0])) is True


@pytest.fixture
def fake_yfinance(monkeypatch: pytest.MonkeyPatch):
    """Install a stub `yfinance` module the adapter imports at call time."""
    calls: list[dict[str, Any]] = []
    module = types.ModuleType("yfinance")

    def install(responses: list[pd.DataFrame]) -> list[dict[str, Any]]:
        queue = list(responses)

        def download(ticker: str, **kwargs: Any) -> pd.DataFrame:
            calls.append({"ticker": ticker, **kwargs})
            return queue.pop(0) if queue else responses[-1]

        module.download = download  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "yfinance", module)
        monkeypatch.setattr("time.sleep", lambda _seconds: None)
        return calls

    return install


def test_a_partial_response_is_retried_instead_of_accepted(fake_yfinance) -> None:
    """The core regression: a partial first response must not win outright."""
    from aios.ingest.prices import fetch_yfinance

    calls = fake_yfinance(
        [_frame([100.0, 101.0, None]), _frame([100.0, 101.0, 102.0])]
    )
    fetch_yfinance("TEST", start="2026-08-04", end="2026-08-07")

    assert len(calls) == 2, "a partial response must trigger exactly one retry here"


def test_a_complete_response_is_not_retried(fake_yfinance) -> None:
    """A healthy response must cost exactly one request, not several."""
    from aios.ingest.prices import fetch_yfinance

    calls = fake_yfinance([_frame([100.0, 101.0, 102.0])])
    fetch_yfinance("TEST", start="2026-08-04", end="2026-08-07")

    assert len(calls) == 1


def test_a_persistently_partial_response_still_fails_closed(fake_yfinance) -> None:
    """Genuinely absent data must exhaust retries and never be stored.

    This is the live MNST split-date case: Yahoo served Open/High/Low with a
    NaN Close and kept serving it. Retrying must not turn that into an
    accepted row.
    """
    from aios.ingest.prices import fetch_yfinance

    calls = fake_yfinance([_frame([100.0, 101.0, None])])
    with pytest.raises(ValueError, match="Close"):
        fetch_yfinance("TEST", start="2026-08-04", end="2026-08-07")

    assert len(calls) > 1, "every attempt must be spent before failing"

"""Plain-language copy helpers for the Streamlit research dashboard.

Keep these helpers free of Streamlit imports so labels and explanations can be
tested without starting the UI. Internal factor/provenance codes remain intact
in storage and audit artifacts; this module only translates their display.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta

from aios.market_calendar import (
    latest_completed_us_equity_session,
    us_equity_sessions,
)

VIEW_OVERVIEW = "Overview"
VIEW_STOCK_RANKINGS = "Research Explorer"
VIEW_COMPANY_DETAILS = "Company Lens"
VIEW_PAPER_MONITOR = "Portfolio Monitor"
VIEW_SYSTEM_CONTROL = "System Control"
VIEW_HOW_IT_WORKS = "Methodology & Data"
VIEW_OPTIONS = (
    VIEW_OVERVIEW,
    VIEW_STOCK_RANKINGS,
    VIEW_COMPANY_DETAILS,
    VIEW_PAPER_MONITOR,
    VIEW_SYSTEM_CONTROL,
    VIEW_HOW_IT_WORKS,
)

# Short aliases keep the Streamlit page conditions readable.
VIEW_HOME = VIEW_OVERVIEW
VIEW_RANKINGS = VIEW_STOCK_RANKINGS
VIEW_DETAILS = VIEW_COMPANY_DETAILS
VIEW_PAPER = VIEW_PAPER_MONITOR
VIEW_SYSTEM = VIEW_SYSTEM_CONTROL
VIEW_METHOD = VIEW_HOW_IT_WORKS

MODEL_QV_LABEL = "Quality + Value (baseline)"
MODEL_QVML_LABEL = "Quality + Value + Trend + Stability (experimental)"
MODEL_OPTIONS = (MODEL_QV_LABEL, MODEL_QVML_LABEL)

RESEARCH_ONLY_NOTICE = (
    "Research only — not a buy or sell recommendation. A high score means a stock "
    "compares well on the selected historical measures; it does not predict its next return."
)


def display_date(value: object) -> str:
    """Format an ISO date for people while preserving unknown values."""
    if value in {None, ""}:
        return "Not available"
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return parsed.strftime("%b %d, %Y").replace(" 0", " ")


def us_eod_freshness_message(
    raw_prices_through: object,
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Explain EOD freshness using the U.S. session clock, not local midnight."""
    try:
        stored_date = date.fromisoformat(str(raw_prices_through))
    except ValueError:
        return False, "The latest completed U.S. market date is not available."
    expected_date = latest_completed_us_equity_session(now)
    expected_label = display_date(expected_date)
    if stored_date >= expected_date:
        return True, f"Up to date for the latest completed U.S. session ({expected_label})."
    return (
        False,
        f"The {expected_label} U.S. session is complete and awaits the next automatic refresh.",
    )


def us_certification_freshness_message(
    certified_through: object,
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Explain whether new decisions reach the latest completed U.S. session."""
    expected_date = latest_completed_us_equity_session(now)
    expected_label = display_date(expected_date)
    try:
        certified_date = date.fromisoformat(str(certified_through))
    except ValueError:
        return (
            False,
            f"The latest completed U.S. session is {expected_label}, but no fully "
            "certified decision date is available.",
        )
    if certified_date >= expected_date:
        return (
            True,
            f"New supervised research is current through the latest completed U.S. "
            f"session ({expected_label}).",
        )
    lag = len(
        us_equity_sessions(
            certified_date + timedelta(days=1),
            expected_date + timedelta(days=1),
        )
    )
    session_word = "session" if lag == 1 else "sessions"
    return (
        False,
        f"The latest completed U.S. session is {expected_label}. Safe decision data "
        f"currently ends on {display_date(certified_date)} ({lag} market {session_word} "
        "behind), so new paper decisions remain paused until catch-up completes.",
    )


def company_symbol_label(company_name: object, ticker: object) -> str:
    """Return one human-readable security label without inventing a company name."""
    symbol = str(ticker or "").strip().upper()
    company = str(company_name or "").strip()
    if not company or company.upper() == symbol:
        return symbol or "Unknown security"
    return f"{company} ({symbol})" if symbol else company


def coverage_value(observed: object) -> tuple[int, int, float] | None:
    """Parse a readiness observation such as ``500/503 (99.4%)``."""
    match = re.search(r"(?<!\d)(\d+)\s*/\s*(\d+)(?!\d)", str(observed or ""))
    if not match:
        return None
    covered, total = (int(value) for value in match.groups())
    if total <= 0 or covered > total:
        return None
    return covered, total, covered / total * 100

_MODEL_KEYS = {
    MODEL_QV_LABEL: "qv",
    MODEL_QVML_LABEL: "qvml",
}

_REGIME_LABELS = {
    "goldilocks": "steady growth with easing inflation",
    "reflation": "growth with higher inflation",
    "stagflation": "slower growth with high inflation",
    "deflationary": "weak growth with falling inflation",
    "risk_off": "stressed or risk-averse markets",
    "unknown": "unclear because some economic data is missing",
}

_EXACT_MISSING_LABELS = {
    "explicit_policy_exclusion": "Excluded by the current research policy",
    "missing_pit_fundamentals": "No eligible company filing was public by this date",
    "missing_price_history": "No verified price history was available",
    "quality_score_unavailable": "Not enough information to score business quality",
    "value_score_unavailable": "Not enough information to score relative value",
    "momentum_score_unavailable": "Not enough price history to score the price trend",
    "low_volatility_score_unavailable": "Not enough price history to score price stability",
    "macro_regime_pit_unavailable": "The economic backdrop was not fully known on this date",
    "market:factor_snapshot_unavailable": "The verified price-history snapshot was unavailable",
    "market:corporate_actions_unverified": (
        "Dividend and stock-split history has not been fully verified"
    ),
    "market:split_adjustment_basis_unknown": "The stock-split price treatment is unknown",
    "market:mixed_split_adjustment_basis": "The price history mixes split treatments",
    "market:momentum_window_unavailable": "The price-trend window is incomplete",
    "market:noncontiguous_price_sessions": (
        "The verified price history has a missing trading-session gap"
    ),
    "market:momentum_unavailable": "The price trend could not be calculated",
    "market:low_volatility_unavailable": "Price stability could not be calculated",
}

_FIELD_LABELS = {
    "stockholders_equity": "shareholders' equity",
    "total_assets": "total assets",
    "ttm_capex": "capital spending for the last 12 months",
    "ttm_cfo": "operating cash flow for the last 12 months",
    "ttm_net_income": "net income for the last 12 months",
    "ttm_operating_income": "operating income for the last 12 months",
    "ttm_revenue": "revenue for the last 12 months",
    "cash": "cash",
    "debt_total": "total debt",
    "eps_nonpositive": "positive earnings per share",
    "price": "a usable share price",
    "shares_out": "shares outstanding",
    "invalid_close": "a valid closing price",
    "invalid_split_ratio": "a valid stock-split ratio",
    "invalid_dividend": "a valid dividend amount",
    "invalid_total_return": "a valid daily return",
}


def model_key(label: str) -> str:
    """Return the internal factor-model key for a visible model label."""
    try:
        return _MODEL_KEYS[label]
    except KeyError as exc:
        raise ValueError(f"unknown dashboard model label: {label}") from exc


def friendly_regime(value: object) -> str:
    """Translate an internal macro-regime code into normal language."""
    code = str(value or "unknown").strip().lower()
    return _REGIME_LABELS.get(code, code.replace("_", " ") or _REGIME_LABELS["unknown"])


def friendly_missing_reason(reason: str) -> str:
    """Translate one internal missing-input code while retaining its meaning."""
    if reason in _EXACT_MISSING_LABELS:
        return _EXACT_MISSING_LABELS[reason]

    parts = reason.split(":")
    if parts[0].startswith("minimum_"):
        prefix = ""
        detail = parts[0]
        threshold = parts[1] if len(parts) > 1 else None
    else:
        prefix = parts[0]
        detail = parts[1] if len(parts) > 1 else reason
        threshold = parts[2] if len(parts) > 2 else None

    if detail == "minimum_quality_components":
        return f"Fewer than {threshold or 'the required'} business-quality measures were available"
    if detail == "minimum_value_multiples":
        return f"Fewer than {threshold or 'the required'} valuation measures were available"
    if detail == "minimum_price_observations":
        return f"Fewer than {threshold or 'the required'} usable trading days were available"
    if detail == "minimum_daily_returns":
        return f"Fewer than {threshold or 'the required'} daily returns were available"
    if detail == "stale_latest_price":
        return f"The latest verified price was {threshold or 'too many'} days old"

    field = _FIELD_LABELS.get(detail, detail.replace("_", " "))
    area = {
        "q": "Missing business-quality data",
        "v": "Missing valuation data",
        "market": "Missing price-history data",
        "macro": "Missing economic data",
    }.get(prefix, "Missing input")
    return f"{area}: {field}"


def friendly_missing_reasons(reasons: Iterable[str]) -> list[str]:
    """Translate and de-duplicate missing-input reasons in source order."""
    translated: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        label = friendly_missing_reason(str(reason))
        if label not in seen:
            translated.append(label)
            seen.add(label)
    return translated


def friendly_missing_summary(reasons: Iterable[str], *, limit: int = 3) -> str:
    """Create a bounded table-friendly summary of missing evidence."""
    translated = friendly_missing_reasons(reasons)
    if not translated:
        return ""
    shown = translated[:limit]
    remainder = len(translated) - len(shown)
    suffix = f"; +{remainder} more" if remainder else ""
    return "; ".join(shown) + suffix

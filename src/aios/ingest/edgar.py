"""SEC EDGAR XBRL fundamentals fetcher — our PRIMARY fundamentals source.

ENDPOINTS
---------
  - Company ticker → CIK map:   https://www.sec.gov/files/company_tickers.json
  - Submissions (filings list): https://data.sec.gov/submissions/CIK{cik:010d}.json
  - Company Facts (all XBRL):   https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json

WHY EDGAR
---------
This is the source of truth. Machine-readable, free, covers every SEC filer.
No vendor normalization lag. The `facts` blob contains every reported XBRL
concept with full history — we extract a curated metric set and store it with
the FILING DATE as `as_of_date` (the point-in-time key).

POINT-IN-TIME
-------------
For each (concept, period) the SEC gives us `end` (period end) and `fp`/`fy`
(fiscal period/year) and, via the filings index, the filing date. The true
"knowable" date is the filing date. We attach the company-level accepted/filing
date from the submissions index as as_of_date per filing period.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from structlog import get_logger

from aios.ingest.http_client import get_http

log = get_logger(__name__)

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# Curated metric set we care about for factor computation.
# Maps our canonical metric name → list of XBRL concepts to try (in priority order;
# first non-null wins — companies report under us-gaap or dei; some use alternate tags).
METRIC_CONCEPTS: dict[str, list[str]] = {
    # Income statement
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
    ],
    "net_income": ["NetIncomeLoss"],
    "operating_income": ["OperatingIncomeLoss"],
    "gross_profit": ["GrossProfit"],
    # EBITDA is deliberately derived downstream from operating income + D&A.
    # Net income is not EBITDA and must never be used as a proxy for it.
    "depreciation": [
        "DepreciationDepletionAndAmortization",
        "DepreciationDepletionAndAmortizationPropertyPlantAndEquipment",
        "Depreciation",
    ],
    "eps_basic": ["EarningsPerShareBasic"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "rd_expense": ["ResearchAndDevelopmentExpense"],
    "interest_expense": ["InterestExpense"],
    "shares_out": [
        "CommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
    ],
    # Balance sheet
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "stockholders_equity": ["StockholdersEquity"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "debt_total": [
        "LongTermDebt",
        "LongTermDebtNoncurrent",
    ],
    # Cash flow
    "cfo": [
        "NetCashProvidedByUsedInOperatingActivities",
        "CashFlowFromContinuingOperatingActivities",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForCapitalImprovements",
    ],
    "dividends_paid": [
        "PaymentsForDividends",
        "PaymentsOfDividends",
    ],
}


def _cik_zero_padded(cik: int) -> str:
    """SEC's data.sec.gov endpoints want the CIK zero-padded to 10 digits."""
    return f"{cik:010d}"


def load_ticker_cik_map() -> dict[str, int]:
    """Fetch SEC's master ticker→CIK map. Returns {TICKER_UPPER: cik}."""
    http = get_http()
    raw: dict[str, Any] = http.get_json(TICKER_MAP_URL)
    out: dict[str, int] = {}
    # Format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    for entry in raw.values():
        ticker = str(entry["ticker"]).upper()
        out[ticker] = int(entry["cik_str"])
    log.info("edgar.ticker_map_loaded", count=len(out))
    return out


def fetch_submissions(cik: int) -> dict[str, Any]:
    """Fetch the submissions index (filings list + metadata) for a CIK."""
    url = SUBMISSIONS_URL.format(cik=_cik_zero_padded(cik))
    return get_http().get_json(url)


def fetch_facts(cik: int) -> dict[str, Any]:
    """Fetch the full XBRL Company Facts blob for a CIK."""
    url = FACTS_URL.format(cik=_cik_zero_padded(cik))
    return get_http().get_json(url)


def _filing_dates_by_period(submissions: dict[str, Any]) -> dict[str, date]:
    """Build {period_end_str: filing_date} from the submissions index.

    The recent filings block has parallel arrays: form, filingDate, periodOfReport.
    periodOfReport is the fiscal period end; filingDate is when it became public.
    We map the LATEST filing date per period (restate/ammend → take newest).
    """
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    filed = recent.get("filingDate", [])
    periods = recent.get("periodOfReport", [])

    out: dict[str, date] = {}
    for form, fdate, period in zip(forms, filed, periods, strict=False):
        # Accept the main periodic filings that carry financials.
        if form not in ("10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "40-F"):
            continue
        if not period or not fdate:
            continue
        out[period] = date.fromisoformat(fdate)  # last write wins = newest
    return out


def _resolve_concept(units_field: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Given a concept's 'units' field, return the flattened raw rows.

    EDGAR's structure is: concept['units'] = { '<UNIT>': [ {row}, {row}, ... ] }
    e.g. {'USD': [...], 'USD/shares': [...]}. We concatenate across all units.
    """
    rows: list[dict[str, Any]] = []
    if isinstance(units_field, dict):
        for _unit, data_rows in units_field.items():
            if isinstance(data_rows, list):
                rows.extend(data_rows)
    elif isinstance(units_field, list):
        # Defensive fallback for the alternate list-of-buckets shape.
        for bucket in units_field:
            if isinstance(bucket, dict):
                rows.extend(bucket.get("data", []) or [])
    return rows


def _merge_concepts(
    us_gaap: dict[str, Any], concepts: list[str]
) -> tuple[str | None, list[dict[str, Any]]]:
    """Merge ALL candidate concepts into one de-duplicated row list.

    WHY NOT 'pick densest': companies rename XBRL tags over time. Apple reported
    revenue as `SalesRevenueNet` through FY2018, then switched to
    `RevenueFromContractWithCustomerExcludingAssessedTax` under ASC 606. Picking
    the single densest tag (= SalesRevenueNet, 210 rows) silently drops every
    year after the rename (2019-2026 gone). Merging all candidates preserves
    full coverage and handles renames transparently.

    A company never reports the SAME (period_end, filed, value) under two
    different concepts in one filing, so merging by (accn, end, start) dedupes
    cleanly without double-counting.
    """
    seen: set[tuple[str | None, str | None, str | None]] = set()
    merged: list[dict[str, Any]] = []
    used_concept: str | None = None
    for concept in concepts:
        node = us_gaap.get(concept)
        if not node:
            continue
        if used_concept is None:
            used_concept = concept
        for r in _resolve_concept(node.get("units", {})):
            key = (r.get("accn"), r.get("end"), r.get("start"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(r)
    return used_concept, merged


# Metrics that represent a FLOW over a period (YTD-cumulative in EDGAR).
# These need quarter_value derivation. Balance-sheet / instant metrics and
# per-share metrics are point-in-time or already per-period → no differencing.
FLOW_METRICS = {
    "revenue", "net_income", "operating_income", "gross_profit",
    "rd_expense", "interest_expense", "depreciation", "cfo", "capex",
    "dividends_paid",
}


def _span_days(r: dict[str, Any]) -> int | None:
    """Duration in days a flow row covers, or None if not a span row."""
    s, e = r.get("start"), r.get("end")
    if not s or not e:
        return None
    try:
        return (date.fromisoformat(e) - date.fromisoformat(s)).days
    except (TypeError, ValueError):
        return None


def _single_period_value(raw_rows: list[dict[str, Any]]) -> dict[int, float | None]:
    """Map each row to its true single-period value via SPAN SELECTION.

    KEY INSIGHT (verified on live EDGAR data): for every flow metric, EDGAR
    stores BOTH the YTD-cumulative row AND the single-quarter row in the same
    filing, distinguished by the `start` date:
        Q3 filing → row1: start=FY_start, end=Q3_end, val=YTD  (9-month)
                  → row2: start=Q3_start, end=Q3_end, val=Q3   (3-month)
    So the TRUE single-period value is the row with the SHORTEST span for a
    given (accn, end). For FY (annual), the longest span IS the full year.

    Returns {id(row): single_period_value}.
    """
    # Group by (accn, end): rows reporting the same period-end in one filing.
    groups: dict[tuple[str | None, str | None], list[dict[str, Any]]] = {}
    for r in raw_rows:
        if r.get("val") is None or r.get("end") is None:
            continue
        groups.setdefault((r.get("accn"), r.get("end")), []).append(r)

    out: dict[int, float | None] = {}
    for (_accn, _end), grp in groups.items():
        if len(grp) == 1:
            # Only one row for this period — it is what it is.
            out[id(grp[0])] = float(grp[0]["val"])
            continue
        # Multiple rows reporting the same period-end in one filing.
        # Determine the fiscal period: if this is an annual (FY) report, the
        # "single period" IS the full year → pick the LONGEST span. For a
        # quarter, pick the SHORTEST span (the single quarter, not the YTD).
        fp_values = {r.get("fp") for r in grp}
        is_annual = "FY" in fp_values

        with_span = [(_span_days(r), r) for r in grp]
        with_span = [t for t in with_span if t[0] is not None]
        if not with_span:
            continue
        with_span.sort(key=lambda t: t[0])
        # FY → longest span (last after ascending sort); Q → shortest (first).
        picked = with_span[-1][1] if is_annual else with_span[0][1]
        single_val = float(picked["val"])
        for r in grp:
            out[id(r)] = single_val
    return out


def extract_fundamentals(ticker: str, cik: int) -> list[dict]:
    """Full extract: return a list of fundamental row dicts (PIT-tagged).

    Each row: {ticker, period_end, as_of_date, fiscal_period, statement,
               metric, value, quarter_value, unit, source}

    Point-in-time key: as_of_date = the per-row 'filed' date from EDGAR.
    This is the date the report became public — exactly when the market could
    first know the number. No estimation, no submissions lookup needed.

    value         = raw EDGAR value (YTD-cumulative for flow metrics).
    quarter_value = single-period value via span selection (flow metrics: the
                    shortest-span row within a filing for a given period-end;
                    instant/EPS metrics: equals value).
    """
    facts = fetch_facts(cik)
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    rows: list[dict] = []

    # Fetch submissions for company metadata (name, SIC, exchange) — cached so
    # the single-filing-date path doesn't refetch. Stash on facts for _extract_company_meta.
    try:
        submissions = fetch_submissions(cik)
        facts["_meta"] = {
            "name": submissions.get("name"),
            "sic": submissions.get("sic"),
            "sicDescription": submissions.get("sicDescription"),
            "exchanges": submissions.get("exchanges"),
        }
    except Exception as e:
        log.warning("edgar.submissions_fetch_failed", ticker=ticker, error=str(e))

    for metric, concepts in METRIC_CONCEPTS.items():
        _concept, raw_rows = _merge_concepts(us_gaap, concepts)
        if not raw_rows:
            continue

        # Map each raw row → its single-period value (flow metrics only).
        qmap: dict[int, float | None] = {}
        if metric in FLOW_METRICS:
            qmap = _single_period_value(raw_rows)

        for r in raw_rows:
            end = r.get("end")
            val = r.get("val")
            filed = r.get("filed")  # per-row filing date — THE pit key
            fp = r.get("fp")        # 'FY','Q1'... or None for instant concepts
            fy = r.get("fy")

            if val is None or end is None or filed is None:
                continue

            period_end = date.fromisoformat(end)
            as_of = date.fromisoformat(filed)

            # Fiscal period label.
            if fp == "FY" and fy:
                fiscal_period = f"FY{fy}"
            elif fp and fy:
                fiscal_period = f"{fp}_{fy}"
            elif fp:
                fiscal_period = fp
            else:
                fiscal_period = "INST"

            # quarter_value: span-selected for flows, equals value for instant/EPS.
            quarter_value = qmap.get(id(r))
            if quarter_value is None and metric not in FLOW_METRICS:
                quarter_value = float(val)

            rows.append({
                "ticker": ticker,
                "period_end": period_end.isoformat(),
                "as_of_date": as_of.isoformat(),
                "fiscal_period": fiscal_period,
                "statement": _statement_for(metric),
                "metric": metric,
                "value": float(val),
                "quarter_value": quarter_value,
                "unit": r.get("unit", "USD"),
                "source": "edgar",
            })

    log.info("edgar.fundamentals_extracted", ticker=ticker, rows=len(rows))
    return rows, _extract_company_meta(facts, ticker)


def _statement_for(metric: str) -> str:
    income = {
        "revenue", "net_income", "operating_income", "gross_profit",
        "eps_basic", "eps_diluted", "rd_expense", "interest_expense", "depreciation",
    }
    balance = {
        "total_assets", "total_liabilities", "stockholders_equity",
        "current_assets", "current_liabilities", "cash", "debt_total", "shares_out",
    }
    cashflow = {"cfo", "capex", "dividends_paid"}
    if metric in income:
        return "income"
    if metric in balance:
        return "balance"
    if metric in cashflow:
        return "cashflow"
    return "other"


def _extract_company_meta(facts_blob: dict[str, Any], ticker: str) -> dict[str, Any]:
    """Pull company metadata (name, SIC, exchange) from the facts/submissions blob.

    The 'facts' blob has entityName at top level, but SIC + exchange live in the
    submissions index. We fetch submissions inside extract_fundamentals and
    stash the metadata on the facts blob under '_meta' for this helper to read.
    """
    meta = facts_blob.get("_meta", {})
    return {
        "name": meta.get("name"),
        "sic_code": meta.get("sic"),
        "sic_description": meta.get("sicDescription"),
        "exchange": (meta.get("exchanges") or [None])[0],
    }


def ingest_ticker(ticker: str, cik_map: dict[str, int] | None = None) -> int:
    """Fetch + store fundamentals for one ticker. Returns rows stored.

    Convenience entrypoint used by the CLI.
    """
    from aios.storage.store import get_store

    if cik_map is None:
        cik_map = load_ticker_cik_map()
    ticker_up = ticker.upper()
    if ticker_up not in cik_map:
        raise ValueError(f"Ticker {ticker_up} not found in SEC ticker map.")
    cik = cik_map[ticker_up]
    rows, meta = extract_fundamentals(ticker_up, cik)
    n = get_store().upsert_fundamentals(rows)

    # Upsert the security row WITH metadata (SIC code enables sector detection).
    get_store().upsert_securities([{
        "ticker": ticker_up,
        "cik": cik,
        "name": meta.get("name"),
        "exchange": meta.get("exchange"),
        "sector": meta.get("sic_description"),
        "industry": meta.get("sic_description"),
        "market_cap_bucket": None,
        "sic_code": meta.get("sic_code"),
    }])
    log.info("edgar.ingest_ticker_done", ticker=ticker_up, rows=n)
    return n

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

import json
import re
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zipfile import BadZipFile, ZipFile, ZipInfo

from structlog import get_logger

from aios.ingest.http_client import get_http

if TYPE_CHECKING:
    from aios.storage.store import Store

log = get_logger(__name__)

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SUBMISSIONS_FILE_URL = "https://data.sec.gov/submissions/{name}"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
COMPANYFACTS_BULK_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"

# The SEC's current ticker file maps XOM to ExxonMobil Holdings Corp, which has
# no public-company XBRL facts. The listed issuer is Exxon Mobil Corp (CIK
# 0000034088). Keep source anomalies explicit and reviewable rather than
# silently accepting an empty facts response.
CIK_OVERRIDES: dict[str, int] = {"XOM": 34088}

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
    out.update(CIK_OVERRIDES)
    log.info("edgar.ticker_map_loaded", count=len(out))
    return out


def fetch_submissions(cik: int) -> dict[str, Any]:
    """Fetch the submissions index (filings list + metadata) for a CIK."""
    url = SUBMISSIONS_URL.format(cik=_cik_zero_padded(cik))
    return get_http().get_json(url)


def fetch_submission_file(name: str) -> dict[str, Any]:
    """Fetch one older filing-history shard named by a submissions payload."""
    if not re.fullmatch(r"CIK\d{10}-submissions-\d{3}\.json", name):
        raise ValueError(f"invalid SEC submissions history filename {name!r}")
    return get_http().get_json(SUBMISSIONS_FILE_URL.format(name=name))


def fetch_facts(cik: int) -> dict[str, Any]:
    """Fetch the full XBRL Company Facts blob for a CIK."""
    url = FACTS_URL.format(cik=_cik_zero_padded(cik))
    return get_http().get_json(url)


class CompanyFactsArchive:
    """Read selected CIK payloads from one local official Company Facts ZIP.

    The archive is intentionally supplied by path instead of downloaded by the
    ingest command: it is large, nightly rather than real-time, and belongs in
    gitignored data storage. Members are selected only by the reviewed CIK and
    their embedded CIK is checked before any facts reach the database.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._archive: ZipFile | None = None
        self._members: dict[str, list[ZipInfo]] = {}

    def __enter__(self) -> CompanyFactsArchive:
        if not self.path.is_file():
            raise ValueError(f"Company Facts ZIP is not a file: {self.path}")
        try:
            archive = ZipFile(self.path)
        except (BadZipFile, OSError) as exc:
            raise ValueError(f"invalid Company Facts ZIP: {self.path}") from exc

        members: dict[str, list[ZipInfo]] = {}
        for info in archive.infolist():
            if info.is_dir():
                continue
            basename = PurePosixPath(info.filename).name
            if basename.startswith("CIK") and basename.endswith(".json"):
                members.setdefault(basename, []).append(info)
        self._archive = archive
        self._members = members
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._archive is not None:
            self._archive.close()
        self._archive = None
        self._members = {}

    def validate_ciks(self, ciks: list[int]) -> None:
        """Fail before reference import when a requested CIK member is absent."""
        for cik in ciks:
            self._member_for(cik)

    def read(self, cik: int) -> dict[str, Any]:
        """Return one validated Company Facts payload by reviewed CIK."""
        archive = self._require_open()
        info = self._member_for(cik)
        try:
            payload = json.loads(archive.read(info))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(
                f"Company Facts ZIP member for CIK {cik:010d} is invalid JSON"
            ) from exc
        return _validate_companyfacts_payload(payload, cik)

    def _require_open(self) -> ZipFile:
        if self._archive is None:
            raise RuntimeError("Company Facts ZIP is not open")
        return self._archive

    def _member_for(self, cik: int) -> ZipInfo:
        self._require_open()
        name = f"CIK{_cik_zero_padded(cik)}.json"
        matches = self._members.get(name, [])
        if not matches:
            raise ValueError(f"Company Facts ZIP has no member for CIK {cik:010d}")
        if len(matches) != 1:
            raise ValueError(f"Company Facts ZIP has duplicate members for CIK {cik:010d}")
        return matches[0]


def _validate_companyfacts_payload(payload: Any, cik: int) -> dict[str, Any]:
    """Reject malformed or cross-issuer Company Facts payloads."""
    if not isinstance(payload, dict):
        raise ValueError("Company Facts payload is not an object")
    try:
        payload_cik = int(payload["cik"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Company Facts payload has no valid CIK") from exc
    if payload_cik != cik:
        raise ValueError(
            f"Company Facts payload CIK {payload_cik:010d} does not match reviewed CIK {cik:010d}"
        )
    if not isinstance(payload.get("facts"), dict):
        raise ValueError("Company Facts payload has no facts object")
    return dict(payload)


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
    "revenue",
    "net_income",
    "operating_income",
    "gross_profit",
    "rd_expense",
    "interest_expense",
    "depreciation",
    "cfo",
    "capex",
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


def extract_fundamentals(
    ticker: str,
    cik: int,
    *,
    issuer_id: str | None = None,
    security_id: str | None = None,
    facts_payload: dict[str, Any] | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    """Full extract: return a list of fundamental row dicts (PIT-tagged).

    Each row: {ticker, issuer_id, security_id, period_end, as_of_date,
               fiscal_period, statement, metric, value, quarter_value, unit,
               source}

    Point-in-time key: as_of_date = the per-row 'filed' date from EDGAR.
    This is the date the report became public — exactly when the market could
    first know the number. No estimation, no submissions lookup needed.

    value         = raw EDGAR value (YTD-cumulative for flow metrics).
    quarter_value = single-period value via span selection (flow metrics: the
                    shortest-span row within a filing for a given period-end;
                    instant/EPS metrics: equals value).
    """
    facts = _validate_companyfacts_payload(
        fetch_facts(cik) if facts_payload is None else facts_payload,
        cik,
    )
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    rows: list[dict] = []
    rejected_future_periods = 0

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
            fp = r.get("fp")  # 'FY','Q1'... or None for instant concepts
            fy = r.get("fy")

            if val is None or end is None or filed is None:
                continue

            period_end = date.fromisoformat(end)
            as_of = date.fromisoformat(filed)
            if period_end > as_of:
                rejected_future_periods += 1
                continue

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

            rows.append(
                {
                    "ticker": ticker,
                    "issuer_id": issuer_id,
                    "security_id": security_id,
                    "period_end": period_end.isoformat(),
                    "as_of_date": as_of.isoformat(),
                    "fiscal_period": fiscal_period,
                    "statement": _statement_for(metric),
                    "metric": metric,
                    "value": float(val),
                    "quarter_value": quarter_value,
                    "unit": r.get("unit", "USD"),
                    "source": "edgar",
                }
            )

    meta = _extract_company_meta(facts, ticker)
    meta["rows_rejected_future_period"] = rejected_future_periods
    if rejected_future_periods:
        log.warning(
            "edgar.future_period_rows_rejected",
            ticker=ticker,
            rows=rejected_future_periods,
        )
    log.info("edgar.fundamentals_extracted", ticker=ticker, rows=len(rows))
    return rows, meta


def ingest_issuer(
    issuer_id: str,
    *,
    store: Store | None = None,
    facts_payload: dict[str, Any] | None = None,
) -> int:
    """Fetch SEC facts by reviewed issuer/CIK identity, not a current ticker map."""
    from aios.storage.store import get_store

    db = store or get_store()
    started_at = datetime.now()
    run_id = str(uuid4())
    ingest_source = (
        "edgar:companyfacts-bulk" if facts_payload is not None else "edgar:issuer-cik-history"
    )
    try:
        reference = db.issuer_reference(issuer_id)
        if reference is None:
            raise ValueError(f"No reviewed SEC CIK for issuer {issuer_id!r}.")
        securities = db.query(
            """
            SELECT DISTINCT security_id
            FROM security_issuer_assignments
            WHERE issuer_id = ?
            """,
            (issuer_id,),
        )
        security_id = securities[0]["security_id"] if len(securities) == 1 else None
        ticker = str(reference["canonical_ticker"]).upper()
        cik = int(reference["cik"])
        extract_kwargs: dict[str, Any] = {
            "issuer_id": issuer_id,
            "security_id": security_id,
        }
        if facts_payload is not None:
            extract_kwargs["facts_payload"] = facts_payload
        rows, meta = extract_fundamentals(ticker, cik, **extract_kwargs)
        rejected = int(meta.get("rows_rejected_future_period") or 0)
        inserted, stale_labels_removed = db.refresh_issuer_fundamentals(
            rows,
            issuer_id=issuer_id,
            canonical_ticker=ticker,
        )
        db.upsert_securities(
            [
                {
                    "ticker": ticker,
                    "cik": cik,
                    "name": meta.get("name") or reference["canonical_name"],
                    "exchange": meta.get("exchange"),
                    "sector": meta.get("sic_description"),
                    "industry": meta.get("sic_description"),
                    "market_cap_bucket": None,
                    "sic_code": meta.get("sic_code"),
                }
            ]
        )
        db.record_ingest(
            run_id=run_id,
            source=ingest_source,
            table_name="fundamentals",
            rows_inserted=inserted,
            rows_rejected=rejected,
            started_at=started_at,
            status="success" if inserted and not rejected else "warning",
            error=(
                f"Rejected {rejected} rows with period_end after filing date"
                if rejected
                else (None if inserted else "SEC returned no fundamental rows")
            ),
        )
        log.info(
            "edgar.ingest_issuer_done",
            issuer_id=issuer_id,
            ticker=ticker,
            rows=inserted,
            stale_labels_removed=stale_labels_removed,
            run_id=run_id,
        )
        return inserted
    except Exception as exc:
        db.record_ingest(
            run_id=run_id,
            source=ingest_source,
            table_name="fundamentals",
            started_at=started_at,
            status="failed",
            error=str(exc),
        )
        raise


def _statement_for(metric: str) -> str:
    income = {
        "revenue",
        "net_income",
        "operating_income",
        "gross_profit",
        "eps_basic",
        "eps_diluted",
        "rd_expense",
        "interest_expense",
        "depreciation",
    }
    balance = {
        "total_assets",
        "total_liabilities",
        "stockholders_equity",
        "current_assets",
        "current_liabilities",
        "cash",
        "debt_total",
        "shares_out",
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

    store = get_store()
    started_at = datetime.now()
    run_id = str(uuid4())
    ticker_up = ticker.upper()
    try:
        if cik_map is None:
            cik_map = load_ticker_cik_map()
        if ticker_up not in cik_map:
            raise ValueError(f"Ticker {ticker_up} not found in SEC ticker map.")
        cik = cik_map[ticker_up]
        rows, meta = extract_fundamentals(ticker_up, cik)
        rejected = int(meta.get("rows_rejected_future_period") or 0)
        n = store.upsert_fundamentals(rows)

        # Upsert the security row WITH metadata (SIC enables sector detection).
        store.upsert_securities(
            [
                {
                    "ticker": ticker_up,
                    "cik": cik,
                    "name": meta.get("name"),
                    "exchange": meta.get("exchange"),
                    "sector": meta.get("sic_description"),
                    "industry": meta.get("sic_description"),
                    "market_cap_bucket": None,
                    "sic_code": meta.get("sic_code"),
                }
            ]
        )
        store.record_ingest(
            run_id=run_id,
            source="edgar",
            table_name="fundamentals",
            rows_inserted=n,
            rows_rejected=rejected,
            started_at=started_at,
            status="success" if n and not rejected else "warning",
            error=(
                f"Rejected {rejected} rows with period_end after filing date"
                if rejected
                else (None if n else "SEC returned no fundamental rows")
            ),
        )
        log.info("edgar.ingest_ticker_done", ticker=ticker_up, rows=n, run_id=run_id)
        return n
    except Exception as e:
        store.record_ingest(
            run_id=run_id,
            source="edgar",
            table_name="fundamentals",
            started_at=started_at,
            status="failed",
            error=str(e),
        )
        raise

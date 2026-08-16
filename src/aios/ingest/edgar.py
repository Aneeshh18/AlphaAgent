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

from aios.ingest.http_client import RawSnapshotContext, get_http
from aios.sec_rejections import SEC_FUNDAMENTAL_REJECTION_CODES

if TYPE_CHECKING:
    from aios.storage.store import Store

log = get_logger(__name__)

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SUBMISSIONS_FILE_URL = "https://data.sec.gov/submissions/{name}"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
COMPANYFACTS_BULK_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
COMPANYFACTS_CAPTURE_PARSER_VERSION = "sec-companyfacts-capture-v1"
COMPANYFACTS_LEGACY_PARSER_VERSION = "sec-companyfacts-v2"
COMPANYFACTS_STORAGE_SAFE_V1_PARSER_VERSION = "sec-companyfacts-v2-storage-safe-v1"
COMPANYFACTS_PARSER_VERSION = "sec-companyfacts-v2-storage-safe-v2"
COMPANYFACTS_NEXT_PARSER_VERSION = "sec-companyfacts-v3"
COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION = "sec-companyfacts-v4"
DEI_ENTITY_SHARES_CONCEPT = "EntityCommonStockSharesOutstanding"
SUBMISSIONS_CAPTURE_PARSER_VERSION = "sec-submissions-capture-v1"
SUBMISSIONS_PARSER_VERSION = "sec-submissions-v2"
COMPANY_TICKERS_CAPTURE_PARSER_VERSION = "sec-company-tickers-capture-v1"
COMPANY_TICKERS_PARSER_VERSION = "sec-company-tickers-v2"

# The SEC's current ticker file maps XOM to ExxonMobil Holdings Corp, which has
# no public-company XBRL facts. The listed issuer is Exxon Mobil Corp (CIK
# 0000034088). Keep source anomalies explicit and reviewable rather than
# silently accepting an empty facts response.
CIK_OVERRIDES: dict[str, int] = {"XOM": 34088}

# Curated metric set we care about for factor computation.
# Maps our canonical metric name → list of exact XBRL concepts to try in priority order.
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

# Revenue v4 is intentionally issuer-scoped. These identities are the reviewed
# cohort from the retained 2026-08-13 Company Facts replay. Broadly appending
# any of these concepts would silently change unrelated issuers and would make
# the historical v2/v3 contracts unreplayable.
_REVENUE_BASE_CONCEPTS = tuple(METRIC_CONCEPTS["revenue"])
_REVENUE_ASSESSED_TAX_CIKS = frozenset(
    {
        900_075,  # CPRT
        9_389,  # BALL
        1_035_443,  # ARE
        1_086_222,  # AKAM
        1_174_922,  # WYNN
        1_374_310,  # CBOE
        1_513_761,  # NCLH
        1_535_527,  # CRWD
        1_637_459,  # KHC
        45_012,  # HAL
        48_465,  # HRL
        52_988,  # J
        728_535,  # JBHT
        878_927,  # ODFL
        91_419,  # SJM
        109_198,  # TJX
    }
)
_REVENUE_UTILITY_CIKS = frozenset({72_903, 753_308, 936_340, 1_326_160})
_REVENUE_EQUITY_RESIDENTIAL_CIK = 906_107
_REVENUE_VALERO_CIK = 1_035_002
_REVENUE_SUPPRESSED_CIKS = frozenset(
    {
        35_527,  # FITB: components only
        72_971,  # WFC: net-of-interest basis
        886_982,  # GS: net-of-interest basis
        895_421,  # MS: net-of-interest basis
        92_230,  # TFC: components only
        1_281_761,  # RF: components only
        1_601_712,  # SYF: components only
        1_841_666,  # APA: extension taxonomy only
    }
)
_ASSESSED_TAX_REVENUE_CONCEPT = "RevenueFromContractWithCustomerIncludingAssessedTax"
_UTILITY_REVENUE_CONCEPT = "RegulatedAndUnregulatedOperatingRevenue"
_REIT_LEASE_REVENUE_CONCEPT = "OperatingLeaseLeaseIncome"
_VLO_EXCISE_TAX_CONCEPT = "ExciseAndSalesTaxes"
_VLO_DERIVED_REVENUE_CONCEPT = (
    "RevenueFromContractWithCustomerIncludingAssessedTaxLessExciseAndSalesTaxes"
)

# Company Facts groups concepts by taxonomy. Concepts default to `us-gaap`;
# this deliberately narrow override is the only cross-taxonomy selection.
# Do not add weighted-average share concepts here: they represent a period
# average and are not a substitute for point-in-time shares outstanding.
_METRIC_CONCEPT_TAXONOMIES: dict[tuple[str, str], str] = {
    ("shares_out", DEI_ENTITY_SHARES_CONCEPT): "dei",
}


def _v4_metric_concepts(metric: str, cik: int) -> list[str]:
    """Return the reviewed v4 concept policy without mutating older parsers."""

    if metric != "revenue":
        return list(METRIC_CONCEPTS[metric])
    if cik in _REVENUE_SUPPRESSED_CIKS:
        return []
    if cik in _REVENUE_UTILITY_CIKS:
        return [_UTILITY_REVENUE_CONCEPT, *_REVENUE_BASE_CONCEPTS]
    if cik in _REVENUE_ASSESSED_TAX_CIKS:
        return [*_REVENUE_BASE_CONCEPTS, _ASSESSED_TAX_REVENUE_CONCEPT]
    if cik == _REVENUE_EQUITY_RESIDENTIAL_CIK:
        return [*_REVENUE_BASE_CONCEPTS, _REIT_LEASE_REVENUE_CONCEPT]
    return list(_REVENUE_BASE_CONCEPTS)


def _v4_revenue_policy_label(cik: int) -> str:
    if cik in _REVENUE_SUPPRESSED_CIKS:
        return "suppressed_incomparable_or_unavailable"
    if cik in _REVENUE_UTILITY_CIKS:
        return "reviewed_utility_total"
    if cik in _REVENUE_ASSESSED_TAX_CIKS:
        return "reviewed_assessed_tax_total"
    if cik == _REVENUE_EQUITY_RESIDENTIAL_CIK:
        return "reviewed_reit_lease_total"
    if cik == _REVENUE_VALERO_CIK:
        return "reviewed_assessed_tax_less_excise"
    return "base_concept_precedence"


def _cik_zero_padded(cik: int) -> str:
    """SEC's data.sec.gov endpoints want the CIK zero-padded to 10 digits."""
    return f"{cik:010d}"


def load_ticker_cik_map(
    *,
    store: Store | None = None,
    ingest_run_id: str | None = None,
    project_root: Path | None = None,
) -> dict[str, int]:
    """Fetch SEC's master ticker→CIK map. Returns {TICKER_UPPER: cik}."""
    raw: dict[str, Any] = _get_sec_json(
        TICKER_MAP_URL,
        snapshot=_sec_snapshot(
            store,
            ingest_run_id,
            dataset="company-tickers",
            role="ticker-map",
            parser_version=COMPANY_TICKERS_CAPTURE_PARSER_VERSION,
            project_root=project_root,
        ),
    )
    rows = _canonical_company_ticker_rows(raw)
    if store is not None and ingest_run_id is not None:
        from aios.raw_snapshots import attach_parsed_rows_evidence

        attach_parsed_rows_evidence(
            store=store,
            ingest_run_id=ingest_run_id,
            role="ticker-map",
            capture_parser_version=COMPANY_TICKERS_CAPTURE_PARSER_VERSION,
            parser_version=COMPANY_TICKERS_PARSER_VERSION,
            parsed_rows=rows,
        )
    out: dict[str, int] = {}
    for row in rows:
        ticker = str(row["ticker"])
        cik = int(row["cik"])
        existing = out.get(ticker)
        if existing is not None and existing != cik:
            raise ValueError(f"SEC ticker map is ambiguous for {ticker}")
        out[ticker] = cik
    out.update(CIK_OVERRIDES)
    log.info("edgar.ticker_map_loaded", count=len(out))
    return out


def fetch_company_ticker_records(
    *,
    store: Store | None = None,
    ingest_run_id: str | None = None,
    project_root: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return all SEC ticker records without discarding duplicate symbols."""
    raw: dict[str, Any] = _get_sec_json(
        TICKER_MAP_URL,
        snapshot=_sec_snapshot(
            store,
            ingest_run_id,
            dataset="company-tickers",
            role="ticker-map",
            parser_version=COMPANY_TICKERS_CAPTURE_PARSER_VERSION,
            project_root=project_root,
        ),
    )
    rows = _canonical_company_ticker_rows(raw)
    if store is not None and ingest_run_id is not None:
        from aios.raw_snapshots import attach_parsed_rows_evidence

        attach_parsed_rows_evidence(
            store=store,
            ingest_run_id=ingest_run_id,
            role="ticker-map",
            capture_parser_version=COMPANY_TICKERS_CAPTURE_PARSER_VERSION,
            parser_version=COMPANY_TICKERS_PARSER_VERSION,
            parsed_rows=rows,
        )
    output: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        output.setdefault(str(row["ticker"]), []).append(dict(row))
    return output


def parse_sec_company_tickers_response(payload: bytes) -> list[dict[str, Any]]:
    """Replay the exact SEC company-ticker map into canonical identity rows."""
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("SEC company ticker response is not valid JSON") from exc
    return _canonical_company_ticker_rows(raw)


def _canonical_company_ticker_rows(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        raise ValueError("SEC company ticker response is not an object")
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            raise ValueError(f"SEC company ticker entry {key!r} is not an object")
        ticker = str(entry.get("ticker") or "").strip().upper()
        title = str(entry.get("title") or "").strip()
        cik_value = entry.get("cik_str")
        if not ticker or not title or cik_value is None:
            raise ValueError(f"SEC company ticker entry {key!r} lacks identity fields")
        try:
            cik = int(cik_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"SEC company ticker entry {key!r} has an invalid CIK") from exc
        if cik <= 0:
            raise ValueError(f"SEC company ticker entry {key!r} has an invalid CIK")
        identity = (ticker, cik, title)
        if identity in seen:
            raise ValueError(f"SEC company ticker response duplicates {ticker}/{cik}")
        seen.add(identity)
        rows.append({"ticker": ticker, "title": title, "cik": cik})
    return sorted(rows, key=lambda row: (row["ticker"], row["cik"], row["title"]))


def fetch_submissions(
    cik: int,
    *,
    store: Store | None = None,
    ingest_run_id: str | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Fetch the submissions index (filings list + metadata) for a CIK."""
    url = SUBMISSIONS_URL.format(cik=_cik_zero_padded(cik))
    return _get_sec_json(
        url,
        snapshot=_sec_snapshot(
            store,
            ingest_run_id,
            dataset="submissions",
            role="submissions",
            parser_version=SUBMISSIONS_CAPTURE_PARSER_VERSION,
            project_root=project_root,
        ),
    )


def fetch_submission_file(
    name: str,
    *,
    store: Store | None = None,
    ingest_run_id: str | None = None,
) -> dict[str, Any]:
    """Fetch one older filing-history shard named by a submissions payload."""
    if not re.fullmatch(r"CIK\d{10}-submissions-\d{3}\.json", name):
        raise ValueError(f"invalid SEC submissions history filename {name!r}")
    return _get_sec_json(
        SUBMISSIONS_FILE_URL.format(name=name),
        snapshot=_sec_snapshot(
            store,
            ingest_run_id,
            dataset="submissions-history",
            role="submissions-history",
            parser_version="sec-submissions-history-v1",
        ),
    )


def fetch_facts(
    cik: int,
    *,
    store: Store | None = None,
    ingest_run_id: str | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Fetch the full XBRL Company Facts blob for a CIK."""
    url = FACTS_URL.format(cik=_cik_zero_padded(cik))
    return _get_sec_json(
        url,
        snapshot=_sec_snapshot(
            store,
            ingest_run_id,
            dataset="companyfacts",
            role="companyfacts",
            parser_version=COMPANYFACTS_CAPTURE_PARSER_VERSION,
            project_root=project_root,
        ),
    )


def _get_sec_json(
    url: str,
    *,
    snapshot: RawSnapshotContext | None,
) -> Any:
    """Keep uninstrumented test/discovery callers backward compatible."""
    http = get_http()
    if snapshot is None:
        return http.get_json(url)
    return http.get_json(url, raw_snapshot=snapshot)


def _sec_snapshot(
    store: Store | None,
    ingest_run_id: str | None,
    *,
    dataset: str,
    role: str,
    parser_version: str,
    project_root: Path | None = None,
) -> RawSnapshotContext | None:
    if store is None:
        return None
    return RawSnapshotContext(
        provider="sec-edgar",
        dataset=dataset,
        store=store,
        ingest_run_id=ingest_run_id,
        role=role,
        adapter_name="aios-sec-http",
        adapter_version="1",
        parser_version=parser_version,
        project_root=project_root,
    )


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


def _validate_submissions_payload(payload: Any, cik: int) -> dict[str, Any]:
    """Reject malformed or cross-issuer SEC Submissions metadata."""
    if not isinstance(payload, dict):
        raise ValueError("SEC Submissions payload is not an object")
    try:
        payload_cik = int(payload["cik"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("SEC Submissions payload has no valid CIK") from exc
    if payload_cik != cik:
        raise ValueError(
            f"SEC Submissions payload CIK {payload_cik:010d} does not match reviewed CIK {cik:010d}"
        )
    exchanges = payload.get("exchanges")
    if exchanges is not None and not isinstance(exchanges, list):
        raise ValueError("SEC Submissions exchanges field is not a list")
    return dict(payload)


def _decode_json_object(payload: bytes, label: str) -> dict[str, Any]:
    """Decode one archived JSON response without accepting scalar roots."""
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{label} response is invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} response is not an object")
    return decoded


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _submissions_provider_rows(
    payload: Any,
    cik: int,
) -> list[dict[str, Any]]:
    """Return the canonical provider metadata consumed by issuer ingestion."""
    submissions = _validate_submissions_payload(payload, cik)
    exchanges = [
        normalized
        for value in submissions.get("exchanges") or []
        if (normalized := _optional_text(value)) is not None
    ]
    return [
        {
            "cik": _cik_zero_padded(cik),
            "name": _optional_text(submissions.get("name")),
            "sic": _optional_text(submissions.get("sic")),
            "sic_description": _optional_text(submissions.get("sicDescription")),
            "exchanges": exchanges,
        }
    ]


def parse_sec_submissions_response(payload: bytes) -> list[dict[str, Any]]:
    """Replay the reviewed canonical rows from exact SEC Submissions bytes."""
    decoded = _decode_json_object(payload, "SEC Submissions")
    try:
        cik = int(decoded["cik"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("SEC Submissions payload has no valid CIK") from exc
    return _submissions_provider_rows(decoded, cik)


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


def _resolve_concept(
    units_field: dict[str, Any] | list[dict[str, Any]],
    *,
    preserve_bucket_unit: bool = False,
) -> list[dict[str, Any]]:
    """Given a concept's 'units' field, return the flattened raw rows.

    EDGAR's structure is: concept['units'] = { '<UNIT>': [ {row}, {row}, ... ] }
    e.g. {'USD': [...], 'USD/shares': [...]}. We concatenate across all units.
    """
    rows: list[dict[str, Any]] = []
    if isinstance(units_field, dict):
        for unit, data_rows in units_field.items():
            if isinstance(data_rows, list):
                for raw_row in data_rows:
                    if not isinstance(raw_row, dict):
                        continue
                    row = dict(raw_row)
                    if preserve_bucket_unit:
                        row["unit"] = str(unit)
                    rows.append(row)
    elif isinstance(units_field, list):
        # Defensive fallback for the alternate list-of-buckets shape.
        for bucket in units_field:
            if isinstance(bucket, dict):
                data_rows = bucket.get("data", []) or []
                if not isinstance(data_rows, list):
                    continue
                for raw_row in data_rows:
                    if not isinstance(raw_row, dict):
                        continue
                    row = dict(raw_row)
                    if preserve_bucket_unit and bucket.get("unit") is not None:
                        row["unit"] = str(bucket["unit"])
                    rows.append(row)
    return rows


def _exact_instant_share_rows(
    node: Any,
    *,
    taxonomy: str,
    concept: str,
) -> list[dict[str, Any]]:
    """Return exact, accession-bound instant-share observations.

    Both supported outstanding-share concepts are instant facts. Period
    averages, currency buckets, span rows, and observations without filing
    identity are unsafe substitutes for the point-in-time market-cap
    denominator. This strict path is v3-only; v2 replay remains byte-contract
    compatible.
    """
    if not isinstance(node, dict):
        raise ValueError(f"Company Facts {taxonomy}:{concept} is not an object")
    units = node.get("units", {})
    if not isinstance(units, dict):
        raise ValueError(f"Company Facts {taxonomy}:{concept} units are not an object")
    provider_rows = units.get("shares", [])
    if not isinstance(provider_rows, list):
        raise ValueError(f"Company Facts {taxonomy}:{concept} shares unit is not a row list")

    rows: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for provider_row in provider_rows:
        if not isinstance(provider_row, dict):
            raise ValueError(f"Company Facts {taxonomy}:{concept} row is not an object")
        accession = str(provider_row.get("accn") or "").strip()
        period_end = str(provider_row.get("end") or "").strip()
        filed = str(provider_row.get("filed") or "").strip()
        if (
            not accession
            or not period_end
            or not filed
            or provider_row.get("val") is None
            or provider_row.get("start") not in (None, "")
        ):
            continue

        normalized = {**provider_row, "unit": "shares"}
        key = (accession, period_end)
        prior = seen.get(key)
        if prior is not None:
            comparison_fields = ("filed", "val", "fp", "fy", "form", "frame")
            if any(prior.get(field) != normalized.get(field) for field in comparison_fields):
                raise ValueError(
                    f"Company Facts {taxonomy}:{concept} conflict within one accession"
                )
            continue
        seen[key] = normalized
        rows.append(normalized)
    return rows


def _merge_concepts(
    namespaces: dict[str, dict[str, Any]],
    metric: str,
    concepts: list[str],
    *,
    taxonomy_aware: bool,
    annotate_concept_priority: bool = False,
    priority_offset: int = 0,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Merge ALL candidate concepts into one provider-row list.

    WHY NOT 'pick densest': companies rename XBRL tags over time. Apple reported
    revenue as `SalesRevenueNet` through FY2018, then switched to
    `RevenueFromContractWithCustomerExcludingAssessedTax` under ASC 606. Picking
    the single densest tag (= SalesRevenueNet, 210 rows) silently drops every
    year after the rename (2019-2026 gone). Merging all candidates preserves
    full coverage and handles renames transparently.

    The active v2 contract retains explicit candidate-concept precedence for the
    same filing identity. V3 keeps every canonical-unit candidate, annotates its
    exact taxonomy/concept/accession locator, and lets the storage selector
    collapse only evidence that agrees on both economics and fiscal semantics.
    """
    seen: set[tuple[str | None, str | None, str | None]] = set()
    merged: list[dict[str, Any]] = []
    used_concept: str | None = None
    for concept_index, concept in enumerate(concepts):
        taxonomy = (
            _METRIC_CONCEPT_TAXONOMIES.get((metric, concept), "us-gaap")
            if taxonomy_aware
            else "us-gaap"
        )
        node = namespaces[taxonomy].get(concept)
        if node is None:
            continue
        if not isinstance(node, dict):
            raise ValueError(f"Company Facts {taxonomy}:{concept} concept is not an object")
        if used_concept is None:
            used_concept = concept
        if taxonomy_aware and metric == "shares_out":
            concept_rows = _exact_instant_share_rows(
                node,
                taxonomy=taxonomy,
                concept=concept,
            )
        else:
            concept_rows = _resolve_concept(
                node.get("units", {}),
                preserve_bucket_unit=taxonomy_aware,
            )
            if taxonomy_aware:
                expected_unit = _expected_metric_unit(metric)
                concept_rows = [
                    row for row in concept_rows if row.get("unit") == expected_unit
                ]
        for r in concept_rows:
            if taxonomy_aware:
                merged_row = {
                    **r,
                    _SOURCE_FACT_LOCATOR_KEY: {
                        "taxonomy": taxonomy,
                        "concept": concept,
                        "accession": str(r.get("accn") or "").strip(),
                        "form": str(r.get("form") or "").strip(),
                        "start": r.get("start"),
                        "end": r.get("end"),
                        "filed": r.get("filed"),
                        "fiscal_period": r.get("fp"),
                        "fiscal_year": r.get("fy"),
                        "frame": r.get("frame"),
                    },
                }
                if annotate_concept_priority:
                    merged_row[_CONCEPT_PRIORITY_KEY] = priority_offset + concept_index
                merged.append(merged_row)
                continue
            key = (r.get("accn"), r.get("end"), r.get("start"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(r)
    return used_concept, merged


def _v4_valero_revenue_rows(
    namespaces: dict[str, dict[str, Any]],
    *,
    priority: int,
) -> list[dict[str, Any]]:
    """Derive reviewed VLO revenue net of assessed excise and sales taxes."""

    _gross_concept, gross_rows = _merge_concepts(
        namespaces,
        "revenue",
        [_ASSESSED_TAX_REVENUE_CONCEPT],
        taxonomy_aware=True,
    )
    _tax_concept, tax_rows = _merge_concepts(
        namespaces,
        "revenue",
        [_VLO_EXCISE_TAX_CONCEPT],
        taxonomy_aware=True,
    )

    def context_key(row: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(
            row.get(field)
            for field in (
                "accn",
                "start",
                "end",
                "filed",
                "form",
                "fp",
                "fy",
                "unit",
            )
        )

    def unique_economics(
        rows: list[dict[str, Any]],
    ) -> dict[tuple[Any, ...], dict[str, Any]]:
        grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(context_key(row), []).append(row)
        selected: dict[tuple[Any, ...], dict[str, Any]] = {}
        for key, candidates in grouped.items():
            values = {float(candidate["val"]) for candidate in candidates}
            if len(values) != 1:
                continue
            selected[key] = min(
                candidates,
                key=lambda candidate: json.dumps(
                    candidate,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ),
            )
        return selected

    gross_by_context = unique_economics(gross_rows)
    tax_by_context = unique_economics(tax_rows)
    derived: list[dict[str, Any]] = []
    for key in sorted(
        gross_by_context.keys() & tax_by_context.keys(),
        key=lambda item: tuple(str(value) for value in item),
    ):
        gross = gross_by_context[key]
        tax = tax_by_context[key]
        net_revenue = float(gross["val"]) - float(tax["val"])
        if net_revenue < 0:
            continue
        derived.append(
            {
                **gross,
                "val": net_revenue,
                _CONCEPT_PRIORITY_KEY: priority,
                _SOURCE_FACT_LOCATOR_KEY: {
                    "taxonomy": "derived:us-gaap",
                    "concept": _VLO_DERIVED_REVENUE_CONCEPT,
                    "accession": str(gross.get("accn") or "").strip(),
                    "form": str(gross.get("form") or "").strip(),
                    "start": gross.get("start"),
                    "end": gross.get("end"),
                    "filed": gross.get("filed"),
                    "fiscal_period": gross.get("fp"),
                    "fiscal_year": gross.get("fy"),
                    "frame": gross.get("frame"),
                    "inputs": [
                        gross[_SOURCE_FACT_LOCATOR_KEY],
                        tax[_SOURCE_FACT_LOCATOR_KEY],
                    ],
                },
            }
        )
    return derived


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

PER_SHARE_METRICS = {"eps_basic", "eps_diluted"}
PERIOD_METRICS = FLOW_METRICS | PER_SHARE_METRICS
MAX_PERIOD_CONTEXT_DAYS = 400
_ANNUAL_FACT_FORMS = {
    "10-K",
    "10-K/A",
    "20-F",
    "20-F/A",
    "40-F",
    "40-F/A",
}
_INTERIM_FACT_FORMS = {
    "10-Q",
    "10-Q/A",
    "6-K",
    "6-K/A",
    "8-K",
    "8-K/A",
}
_SOURCE_FACT_LOCATOR_KEY = "_source_fact_locator"
_CONCEPT_PRIORITY_KEY = "_concept_priority"
_FUNDAMENTAL_STORAGE_KEY_FIELDS = (
    "cik",
    "period_end",
    "as_of_date",
    "metric",
)


def _expected_metric_unit(metric: str) -> str:
    """Return the only v3 unit that can feed one canonical factor metric."""

    if metric == "shares_out":
        return "shares"
    if metric in PER_SHARE_METRICS:
        return "USD/shares"
    return "USD"


def _metric_context_rows(
    metric: str,
    raw_rows: list[dict[str, Any]],
    *,
    prefer_concept_precedence: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Select exact v3 contexts before they can collide in storage.

    Period metrics use the shortest span for a quarterly filing and the longest
    span for an annual filing, independently for every accession and period
    end. Instant metrics require an observation without a start date. Every
    accepted context is accession-bound, uses an explicit supported SEC form,
    and carries the exact canonical unit. Mixed fiscal metadata and invalid or
    unreasonable spans are withheld as a complete accession/end group.

    The active v2 replay deliberately bypasses this selector so archived
    parsed-row checksums remain byte-compatible.
    """

    expected_unit = _expected_metric_unit(metric)
    exact_unit = [row for row in raw_rows if row.get("unit") == expected_unit]
    required = [
        row
        for row in exact_unit
        if row.get("val") is not None
        and str(row.get("accn") or "").strip()
        and str(row.get("end") or "").strip()
        and str(row.get("filed") or "").strip()
        and str(row.get("form") or "").strip()
    ]
    rejected = len(exact_unit) - len(required)
    if metric not in PERIOD_METRICS:
        selected = [
            row
            for row in required
            if row.get("start") in (None, "")
            and str(row["form"]).strip() in (_ANNUAL_FACT_FORMS | _INTERIM_FACT_FORMS)
        ]
        return selected, rejected + len(required) - len(selected)

    groups: dict[tuple[str | None, str | None], list[dict[str, Any]]] = {}
    for row in required:
        if not str(row.get("fp") or "").strip() or row.get("fy") is None:
            rejected += 1
            continue
        span = _span_days(row)
        if span is None or span <= 0 or span > MAX_PERIOD_CONTEXT_DAYS:
            rejected += 1
            continue
        groups.setdefault((row.get("accn"), row.get("end")), []).append(row)

    selected: list[dict[str, Any]] = []
    for group in groups.values():
        fiscal_contexts = {
            (
                str(row.get("fp") or "").strip(),
                str(row.get("fy")),
            )
            for row in group
        }
        if len(fiscal_contexts) != 1:
            rejected += len(group)
            continue
        fiscal_period, _fiscal_year = next(iter(fiscal_contexts))
        annual = fiscal_period == "FY"
        allowed_forms = _ANNUAL_FACT_FORMS if annual else _INTERIM_FACT_FORMS
        authoritative = [
            row
            for row in group
            if str(row.get("form") or "").strip() in allowed_forms
        ]
        rejected += len(group) - len(authoritative)
        if not authoritative:
            continue
        spanned = [(_span_days(row), row) for row in authoritative]
        target_span = (
            max(int(span) for span, _row in spanned)
            if annual
            else min(int(span) for span, _row in spanned)
        )
        context_rows = [row for span, row in spanned if span == target_span]
        if prefer_concept_precedence:
            priorities = [
                int(row[_CONCEPT_PRIORITY_KEY])
                for row in context_rows
                if _CONCEPT_PRIORITY_KEY in row
            ]
            if priorities:
                preferred = min(priorities)
                preferred_rows = [
                    row
                    for row in context_rows
                    if int(row.get(_CONCEPT_PRIORITY_KEY, preferred)) == preferred
                ]
                rejected += len(context_rows) - len(preferred_rows)
                context_rows = preferred_rows
        selected.extend(context_rows)
    return selected, rejected


def _fundamental_storage_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row[field] for field in _FUNDAMENTAL_STORAGE_KEY_FIELDS)


def _select_metric_storage_rows(
    rows: list[dict[str, Any]],
    *,
    preserve_legacy_winner: bool = False,
    prefer_concept_precedence: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Produce at most one unambiguous row for every database key.

    Repeated filings on one day commonly restate the same fact under identical
    economics and fiscal semantics. Those rows collapse deterministically while
    retaining every exact source locator. If the same PIT key disagrees on
    fiscal period, statement, value, quarter value, or unit, v3 withholds the
    entire key. The storage-safe v2 compatibility parser instead preserves the
    first provider row's economics explicitly, matching the frozen legacy
    projection while preventing duplicate database keys. Conflicting peers are
    still counted as rejected evidence.
    """

    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(_fundamental_storage_key(row), []).append(row)

    selected: list[dict[str, Any]] = []
    rejected_conflicts = 0
    for key in sorted(grouped, key=lambda item: tuple(str(value) for value in item)):
        candidates = grouped[key]
        if prefer_concept_precedence:
            priorities = [
                int(candidate[_CONCEPT_PRIORITY_KEY])
                for candidate in candidates
                if _CONCEPT_PRIORITY_KEY in candidate
            ]
            if priorities:
                preferred = min(priorities)
                candidates = [
                    candidate
                    for candidate in candidates
                    if int(candidate.get(_CONCEPT_PRIORITY_KEY, preferred)) == preferred
                ]

        def economic_value(candidate: dict[str, Any]) -> tuple[Any, ...]:
            return (
                candidate.get("fiscal_period"),
                candidate.get("statement"),
                candidate.get("value"),
                candidate.get("quarter_value"),
                candidate.get("unit"),
            )

        economic_values = {economic_value(candidate) for candidate in candidates}
        if len(economic_values) != 1:
            rejected_conflicts += 1
            if not preserve_legacy_winner:
                continue
            first_economics = economic_value(candidates[0])
            candidates = [
                candidate
                for candidate in candidates
                if economic_value(candidate) == first_economics
            ]
        source_locators = sorted(
            {
                json.dumps(
                    candidate[_SOURCE_FACT_LOCATOR_KEY],
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                for candidate in candidates
                if _SOURCE_FACT_LOCATOR_KEY in candidate
            }
        )
        selected_row = min(
            candidates,
            key=lambda candidate: json.dumps(
                candidate,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ),
        ).copy()
        selected_row.pop(_SOURCE_FACT_LOCATOR_KEY, None)
        selected_row.pop(_CONCEPT_PRIORITY_KEY, None)
        if source_locators:
            selected_row["source_fact_locator"] = json.dumps(
                [json.loads(locator) for locator in source_locators],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        selected.append(selected_row)
    return selected, rejected_conflicts


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


def _companyfacts_provider_rows(
    payload: Any,
    cik: int,
    *,
    taxonomy_aware: bool = False,
    storage_safe: bool = False,
    preserve_legacy_winner: bool = False,
    revenue_policy_v4: bool = False,
    prefer_concept_precedence: bool = False,
) -> tuple[list[dict[str, Any]], int, int, int]:
    """Parse the identity-neutral, PIT-safe rows consumed from Company Facts."""
    facts = _validate_companyfacts_payload(payload, cik)
    fact_namespaces = facts["facts"]
    us_gaap = fact_namespaces.get("us-gaap", {})
    if not isinstance(us_gaap, dict):
        raise ValueError("Company Facts us-gaap namespace is not an object")
    namespaces = {"us-gaap": us_gaap}
    if taxonomy_aware:
        dei = fact_namespaces.get("dei", {})
        if not isinstance(dei, dict):
            raise ValueError("Company Facts dei namespace is not an object")
        namespaces["dei"] = dei
    rows: list[dict[str, Any]] = []
    rejected_future_periods = 0
    rejected_contexts = 0

    for metric, legacy_concepts in METRIC_CONCEPTS.items():
        prefer_metric_concept_precedence = (
            prefer_concept_precedence and metric == "revenue"
        )
        concepts = _v4_metric_concepts(metric, cik) if revenue_policy_v4 else legacy_concepts
        if not concepts:
            continue
        _concept, raw_rows = _merge_concepts(
            namespaces,
            metric,
            concepts,
            taxonomy_aware=taxonomy_aware,
            annotate_concept_priority=prefer_metric_concept_precedence,
        )
        if revenue_policy_v4 and metric == "revenue" and cik == _REVENUE_VALERO_CIK:
            raw_rows.extend(
                _v4_valero_revenue_rows(
                    namespaces,
                    priority=-1,
                )
            )
        if not raw_rows:
            continue

        if taxonomy_aware:
            raw_rows, context_rejections = _metric_context_rows(
                metric,
                raw_rows,
                prefer_concept_precedence=prefer_metric_concept_precedence,
            )
            rejected_contexts += context_rejections
            if not raw_rows:
                continue

        qmap: dict[int, float | None] = {}
        if metric in FLOW_METRICS:
            qmap = _single_period_value(raw_rows)

        for raw_row in raw_rows:
            end = raw_row.get("end")
            value = raw_row.get("val")
            filed = raw_row.get("filed")
            fiscal_period_code = raw_row.get("fp")
            fiscal_year = raw_row.get("fy")
            if value is None or end is None or filed is None:
                continue

            period_end = date.fromisoformat(end)
            as_of = date.fromisoformat(filed)
            if period_end > as_of:
                rejected_future_periods += 1
                continue

            if fiscal_period_code == "FY" and fiscal_year:
                fiscal_period = f"FY{fiscal_year}"
            elif fiscal_period_code and fiscal_year:
                fiscal_period = f"{fiscal_period_code}_{fiscal_year}"
            elif fiscal_period_code:
                fiscal_period = fiscal_period_code
            else:
                fiscal_period = "INST"

            quarter_value = qmap.get(id(raw_row))
            if quarter_value is None and metric not in FLOW_METRICS:
                quarter_value = float(value)

            provider_row = {
                "cik": _cik_zero_padded(cik),
                "period_end": period_end.isoformat(),
                "as_of_date": as_of.isoformat(),
                "fiscal_period": fiscal_period,
                "statement": _statement_for(metric),
                "metric": metric,
                "value": float(value),
                "quarter_value": quarter_value,
                "unit": raw_row.get("unit", "USD"),
                "source": "edgar",
            }
            if taxonomy_aware:
                provider_row[_SOURCE_FACT_LOCATOR_KEY] = raw_row[
                    _SOURCE_FACT_LOCATOR_KEY
                ]
            if prefer_metric_concept_precedence:
                provider_row[_CONCEPT_PRIORITY_KEY] = raw_row[_CONCEPT_PRIORITY_KEY]
            rows.append(provider_row)

    rejected_storage_conflicts = 0
    if taxonomy_aware or storage_safe:
        rows, rejected_storage_conflicts = _select_metric_storage_rows(
            rows,
            preserve_legacy_winner=preserve_legacy_winner,
            prefer_concept_precedence=prefer_concept_precedence,
        )
    return (
        rows,
        rejected_future_periods,
        rejected_contexts,
        rejected_storage_conflicts,
    )


def parse_sec_companyfacts_response(payload: bytes) -> list[dict[str, Any]]:
    """Replay the currently active v2 Company Facts contract."""
    rows, _metadata = replay_sec_companyfacts_response(
        payload,
        parser_version=COMPANYFACTS_PARSER_VERSION,
    )
    return rows


def replay_sec_companyfacts_response(
    payload: bytes,
    *,
    parser_version: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Replay one explicit parser and return its structured withholding proof."""

    decoded = _decode_json_object(payload, "SEC Company Facts")
    try:
        cik = int(decoded["cik"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Company Facts payload has no valid CIK") from exc
    if parser_version == COMPANYFACTS_LEGACY_PARSER_VERSION:
        taxonomy_aware = False
        storage_safe = False
        preserve_legacy_winner = False
        revenue_policy_v4 = False
        prefer_concept_precedence = False
    elif parser_version == COMPANYFACTS_STORAGE_SAFE_V1_PARSER_VERSION:
        taxonomy_aware = False
        storage_safe = True
        preserve_legacy_winner = False
        revenue_policy_v4 = False
        prefer_concept_precedence = False
    elif parser_version == COMPANYFACTS_PARSER_VERSION:
        taxonomy_aware = False
        storage_safe = True
        preserve_legacy_winner = True
        revenue_policy_v4 = False
        prefer_concept_precedence = False
    elif parser_version == COMPANYFACTS_NEXT_PARSER_VERSION:
        taxonomy_aware = True
        storage_safe = True
        preserve_legacy_winner = False
        revenue_policy_v4 = False
        prefer_concept_precedence = False
    elif parser_version == COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION:
        taxonomy_aware = True
        storage_safe = True
        preserve_legacy_winner = False
        revenue_policy_v4 = True
        prefer_concept_precedence = True
    else:
        raise ValueError(
            f"unsupported SEC Company Facts parser version: {parser_version}"
        )
    (
        rows,
        rejected_future_periods,
        rejected_contexts,
        rejected_storage_conflicts,
    ) = _companyfacts_provider_rows(
        decoded,
        cik,
        taxonomy_aware=taxonomy_aware,
        storage_safe=storage_safe,
        preserve_legacy_winner=preserve_legacy_winner,
        revenue_policy_v4=revenue_policy_v4,
        prefer_concept_precedence=prefer_concept_precedence,
    )
    metadata = {
        "parser_version": parser_version,
        "rows_rejected_future_period": rejected_future_periods,
        "rows_rejected_context": rejected_contexts,
        "rows_rejected_storage_conflict": rejected_storage_conflicts,
    }
    if revenue_policy_v4:
        metadata["revenue_policy"] = _v4_revenue_policy_label(cik)
    metadata["rejection_codes"] = _fundamental_rejection_codes(metadata)
    metadata["rows_rejected"] = (
        rejected_future_periods
        + rejected_contexts
        + rejected_storage_conflicts
    )
    return rows, metadata


def parse_sec_companyfacts_response_v2(payload: bytes) -> list[dict[str, Any]]:
    """Replay archived v2 rows without applying the dormant v3 repair."""
    rows, _metadata = replay_sec_companyfacts_response(
        payload,
        parser_version=COMPANYFACTS_LEGACY_PARSER_VERSION,
    )
    return rows


def parse_sec_companyfacts_response_storage_safe_v1(
    payload: bytes,
) -> list[dict[str, Any]]:
    """Replay archived v1 storage-safe withholding semantics exactly."""
    rows, _metadata = replay_sec_companyfacts_response(
        payload,
        parser_version=COMPANYFACTS_STORAGE_SAFE_V1_PARSER_VERSION,
    )
    return rows


def parse_sec_companyfacts_response_storage_safe(payload: bytes) -> list[dict[str, Any]]:
    """Replay the active v2 metric policy with one deterministic row per key."""
    return parse_sec_companyfacts_response(payload)


def parse_sec_companyfacts_response_v3(payload: bytes) -> list[dict[str, Any]]:
    """Replay the reviewed next-policy taxonomy and storage-key policy."""
    rows, _metadata = replay_sec_companyfacts_response(
        payload,
        parser_version=COMPANYFACTS_NEXT_PARSER_VERSION,
    )
    return rows


def parse_sec_companyfacts_response_v4(payload: bytes) -> list[dict[str, Any]]:
    """Replay the reviewed issuer-scoped revenue and precedence policy."""
    rows, _metadata = replay_sec_companyfacts_response(
        payload,
        parser_version=COMPANYFACTS_REVENUE_POLICY_PARSER_VERSION,
    )
    return rows


def canonical_sec_fundamental_row_sha256(row: dict[str, Any]) -> str:
    """Hash one identity-neutral row exactly as the Company Facts parser emits it."""
    from aios.raw_snapshots import canonical_parsed_rows_sha256

    canonical = {
        "cik": _cik_zero_padded(int(row["cik"])),
        "period_end": str(row["period_end"]),
        "as_of_date": str(row["as_of_date"]),
        "fiscal_period": row.get("fiscal_period"),
        "statement": row.get("statement"),
        "metric": row["metric"],
        "value": row.get("value"),
        "quarter_value": row.get("quarter_value"),
        "unit": row.get("unit", "USD"),
        "source": row.get("source", "edgar"),
    }
    if row.get("source_fact_locator") is not None:
        canonical["source_fact_locator"] = row["source_fact_locator"]
    return canonical_parsed_rows_sha256([canonical])


def _fundamental_rejection_summary(metadata: dict[str, Any]) -> tuple[int, str | None]:
    """Describe every fail-closed Company Facts rejection category."""

    future_periods = int(metadata.get("rows_rejected_future_period") or 0)
    contexts = int(metadata.get("rows_rejected_context") or 0)
    storage_conflicts = int(metadata.get("rows_rejected_storage_conflict") or 0)
    details: list[str] = []
    if future_periods:
        details.append(
            f"rejected {future_periods} row(s) with period_end after filing date"
        )
    if contexts:
        details.append(f"withheld {contexts} unsupported SEC fact context row(s)")
    if storage_conflicts:
        details.append(
            f"withheld {storage_conflicts} ambiguous database storage key(s)"
        )
    return future_periods + contexts + storage_conflicts, "; ".join(details) or None


def _fundamental_rejection_codes(metadata: dict[str, Any]) -> tuple[str, ...]:
    """Return the stable machine contract for Company Facts withholding."""

    codes: list[str] = []
    if int(metadata.get("rows_rejected_future_period") or 0):
        codes.append("future_period")
    if int(metadata.get("rows_rejected_storage_conflict") or 0):
        codes.append("storage_conflict")
    if int(metadata.get("rows_rejected_context") or 0):
        codes.append("unsupported_context")
    result = tuple(sorted(codes))
    if not set(result) <= SEC_FUNDAMENTAL_REJECTION_CODES:
        raise AssertionError("unknown SEC fundamental rejection code")
    return result


def extract_fundamentals(
    ticker: str,
    cik: int,
    *,
    issuer_id: str | None = None,
    security_id: str | None = None,
    facts_payload: dict[str, Any] | None = None,
    snapshot_store: Store | None = None,
    ingest_run_id: str | None = None,
    snapshot_project_root: Path | None = None,
    companyfacts_parser_version: str = COMPANYFACTS_PARSER_VERSION,
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
    fetched_facts = facts_payload is None
    facts = _validate_companyfacts_payload(
        (
            fetch_facts(
                cik,
                store=snapshot_store,
                ingest_run_id=ingest_run_id,
                project_root=snapshot_project_root,
            )
            if fetched_facts
            else facts_payload
        ),
        cik,
    )
    if companyfacts_parser_version == COMPANYFACTS_LEGACY_PARSER_VERSION:
        taxonomy_aware = False
        storage_safe = False
        preserve_legacy_winner = False
    elif companyfacts_parser_version == COMPANYFACTS_STORAGE_SAFE_V1_PARSER_VERSION:
        taxonomy_aware = False
        storage_safe = True
        preserve_legacy_winner = False
    elif companyfacts_parser_version == COMPANYFACTS_PARSER_VERSION:
        taxonomy_aware = False
        storage_safe = True
        preserve_legacy_winner = True
    elif companyfacts_parser_version == COMPANYFACTS_NEXT_PARSER_VERSION:
        taxonomy_aware = True
        storage_safe = True
        preserve_legacy_winner = False
    else:
        raise ValueError(
            f"unsupported SEC Company Facts parser version: {companyfacts_parser_version}"
        )
    (
        provider_rows,
        rejected_future_periods,
        rejected_contexts,
        rejected_storage_conflicts,
    ) = _companyfacts_provider_rows(
        facts,
        cik,
        taxonomy_aware=taxonomy_aware,
        storage_safe=storage_safe,
        preserve_legacy_winner=preserve_legacy_winner,
    )
    companyfacts_snapshot_id: str | None = None
    companyfacts_rowset_sha256: str | None = None
    if fetched_facts and snapshot_store is not None and ingest_run_id is not None:
        from aios.raw_snapshots import (
            attach_parsed_rows_evidence,
            canonical_parsed_rows_sha256,
        )

        companyfacts_snapshot_id = attach_parsed_rows_evidence(
            store=snapshot_store,
            ingest_run_id=ingest_run_id,
            role="companyfacts",
            capture_parser_version=COMPANYFACTS_CAPTURE_PARSER_VERSION,
            parser_version=companyfacts_parser_version,
            parsed_rows=provider_rows,
            rows_rejected=(
                rejected_future_periods
                + rejected_contexts
                + rejected_storage_conflicts
            ),
            rejection_codes=_fundamental_rejection_codes(
                {
                    "rows_rejected_future_period": rejected_future_periods,
                    "rows_rejected_context": rejected_contexts,
                    "rows_rejected_storage_conflict": rejected_storage_conflicts,
                }
            ),
        )
        companyfacts_rowset_sha256 = canonical_parsed_rows_sha256(provider_rows)

    # Fetch Submissions for company metadata and attach its independently
    # replayable canonical row only after the identity validation succeeds.
    submissions = fetch_submissions(
        cik,
        store=snapshot_store,
        ingest_run_id=ingest_run_id,
        project_root=snapshot_project_root,
    )
    submissions_rows = _submissions_provider_rows(submissions, cik)
    if snapshot_store is not None and ingest_run_id is not None:
        from aios.raw_snapshots import attach_parsed_rows_evidence

        attach_parsed_rows_evidence(
            store=snapshot_store,
            ingest_run_id=ingest_run_id,
            role="submissions",
            capture_parser_version=SUBMISSIONS_CAPTURE_PARSER_VERSION,
            parser_version=SUBMISSIONS_PARSER_VERSION,
            parsed_rows=submissions_rows,
        )
    submissions_meta = submissions_rows[0]
    facts["_meta"] = {
        "name": submissions_meta["name"],
        "sic": submissions_meta["sic"],
        "sicDescription": submissions_meta["sic_description"],
        "exchanges": submissions_meta["exchanges"],
    }

    rows: list[dict] = []
    for row in provider_rows:
        stored_row = {
            "ticker": ticker,
            "issuer_id": issuer_id,
            "security_id": security_id,
            "period_end": row["period_end"],
            "as_of_date": row["as_of_date"],
            "fiscal_period": row["fiscal_period"],
            "statement": row["statement"],
            "metric": row["metric"],
            "value": row["value"],
            "quarter_value": row["quarter_value"],
            "unit": row["unit"],
            "source": row["source"],
            "source_fact_locator": row.get("source_fact_locator"),
        }
        if companyfacts_snapshot_id is not None and issuer_id is not None:
            stored_row.update(
                {
                    "ingest_run_id": ingest_run_id,
                    "source_snapshot_id": companyfacts_snapshot_id,
                    "source_rowset_sha256": companyfacts_rowset_sha256,
                    "source_row_sha256": canonical_sec_fundamental_row_sha256(row),
                }
            )
        rows.append(stored_row)

    meta = _extract_company_meta(facts, ticker)
    meta["rows_rejected_future_period"] = rejected_future_periods
    meta["rows_rejected_context"] = rejected_contexts
    meta["rows_rejected_storage_conflict"] = rejected_storage_conflicts
    meta["submissions_row"] = submissions_meta
    if rejected_future_periods:
        log.warning(
            "edgar.future_period_rows_rejected",
            ticker=ticker,
            rows=rejected_future_periods,
        )
    if rejected_storage_conflicts:
        log.warning(
            "edgar.ambiguous_storage_keys_rejected",
            ticker=ticker,
            rows=rejected_storage_conflicts,
        )
    if rejected_contexts:
        log.warning(
            "edgar.unsupported_fact_contexts_rejected",
            ticker=ticker,
            rows=rejected_contexts,
        )
    log.info("edgar.fundamentals_extracted", ticker=ticker, rows=len(rows))
    return rows, meta


def ingest_issuer(
    issuer_id: str,
    *,
    store: Store | None = None,
    facts_payload: dict[str, Any] | None = None,
    companyfacts_parser_version: str = COMPANYFACTS_PARSER_VERSION,
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
        if facts_payload is not None:
            raise RuntimeError(
                "Unlineaged Company Facts payload ingestion is unavailable; "
                "capture and verify immutable source evidence before issuer commit."
            )
        if companyfacts_parser_version == COMPANYFACTS_NEXT_PARSER_VERSION:
            raise RuntimeError(
                "SEC Company Facts v3 live mutation is unavailable in this build; "
                "use the read-only governed replay planner."
            )
        if companyfacts_parser_version not in {
            COMPANYFACTS_LEGACY_PARSER_VERSION,
            COMPANYFACTS_PARSER_VERSION,
        }:
            raise ValueError(
                "unsupported SEC Company Facts parser version: "
                f"{companyfacts_parser_version}"
            )
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
            "snapshot_store": db,
            "ingest_run_id": run_id,
        }
        if companyfacts_parser_version != COMPANYFACTS_PARSER_VERSION:
            extract_kwargs["companyfacts_parser_version"] = companyfacts_parser_version
        if facts_payload is not None:
            extract_kwargs["facts_payload"] = facts_payload
        rows, meta = extract_fundamentals(ticker, cik, **extract_kwargs)
        rejected, rejection_error = _fundamental_rejection_summary(meta)
        rejection_codes = _fundamental_rejection_codes(meta)
        accepted_status = "success" if rows and not rejected else "warning"
        accepted_error = rejection_error or (
            None if rows else "SEC returned no fundamental rows"
        )
        inserted, stale_relation_rows_removed = db.commit_issuer_fundamental_ingest(
            rows,
            issuer_id=issuer_id,
            canonical_ticker=ticker,
            security_rows=[
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
            ],
            submissions_row=meta["submissions_row"],
            run_id=run_id,
            source=ingest_source,
            rows_rejected=rejected,
            started_at=started_at,
            status=accepted_status,
            error=accepted_error,
            rejection_codes=rejection_codes,
        )
    except Exception as exc:
        try:
            db.record_ingest(
                run_id=run_id,
                source=ingest_source,
                table_name="fundamentals",
                subject_type="issuer",
                subject_id=issuer_id,
                started_at=started_at,
                status="failed",
                error=str(exc),
            )
        except Exception as outcome_exc:
            log.error(
                "edgar.failed_outcome_record_failed",
                issuer_id=issuer_id,
                run_id=run_id,
                error=str(outcome_exc),
            )
        raise
    log.info(
        "edgar.ingest_issuer_done",
        issuer_id=issuer_id,
        ticker=ticker,
        rows=inserted,
        stale_relation_rows_removed=stale_relation_rows_removed,
        run_id=run_id,
    )
    return inserted


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
    """Route a legacy ticker request through one reviewed issuer identity."""
    from aios.storage.store import get_store

    store = get_store()
    ticker_up = str(ticker).strip().upper()
    if not ticker_up:
        raise ValueError("ticker cannot be blank")
    if cik_map is not None:
        raise RuntimeError(
            "Direct ticker/CIK-map fundamental ingestion is disabled; "
            "use a reviewed issuer identity."
        )
    matches = store.query(
        """
        SELECT DISTINCT issuer.issuer_id
        FROM issuer_master AS issuer
        JOIN issuer_cik_history AS cik USING (issuer_id)
        WHERE upper(issuer.canonical_ticker) = ?
          AND cik.effective_end IS NULL
        ORDER BY issuer.issuer_id
        """,
        (ticker_up,),
    )
    if len(matches) != 1:
        raise RuntimeError(
            f"Ticker {ticker_up} does not resolve to exactly one reviewed active issuer."
        )
    return ingest_issuer(str(matches[0]["issuer_id"]), store=store)

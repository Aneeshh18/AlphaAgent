"""Fail-closed contracts for the v2 anomaly rule families.

These rules are additive: each writes into its own ledger scope so its source
boundary advances independently of the SEC coverage rule, and neither the
shipped ``rule_bundle_version`` nor the single-rule ``executed_rules`` contract
changes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from aios import anomalies
from aios.alerts import (
    ANOMALY_CONFIDENCES,
    ANOMALY_SEVERITIES,
)
from aios.alerts import (
    _prepare_anomaly_scan as prepare_anomaly_scan,
)
from aios.anomalies import (
    COVERAGE_RULE_ID,
    COVERAGE_RULE_VERSION,
    FACTOR_RULE_ID,
    FACTOR_RULE_VERSION,
    FILINGS_RULE_ID,
    MAPPING_RULE_ID,
    PRICE_RULE_ID,
    PRICE_RULE_VERSION,
    RULE_BUNDLE_VERSION,
    RULE_REGISTRY,
    SEC_RULE_ID,
    SHARES_RULE_ID,
    anomaly_fingerprint,
    measure_universe_coverage,
    registered_rule_ids,
    rule_scope,
    run_detectors,
    scan_conflicting_filings,
    scan_coverage_deterioration,
    scan_factor_percentile_jump,
    scan_mapping_drift,
    scan_price_action_mismatch,
    scan_share_count_jump,
)

DECISION = date(2026, 8, 7)
BOUNDARY = datetime(2026, 8, 7, 21, 30, tzinfo=UTC)


def _member(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "security_id": f"sec-{ticker.lower()}",
        "issuer_id": f"iss-{ticker.lower()}",
    }


def _price_row(
    day: date,
    close: float,
    *,
    split_ratio: float = 1.0,
    dividends: float = 0.0,
    actions_complete: bool = True,
) -> dict:
    return {
        "date": day,
        "close": close,
        "dividends": dividends,
        "split_ratio": split_ratio,
        "actions_complete": actions_complete,
        "close_split_adjusted": True,
        "provider_symbol": "SYM",
        "source": "yfinance",
    }


class _PriceStore:
    """Minimal Store surface used by the price rule."""

    def __init__(
        self,
        prices: dict[str, list[dict]],
        *,
        members: int = 500,
        boundary: datetime | None = BOUNDARY,
    ) -> None:
        self._prices = prices
        self._members = [_member(f"T{index:03d}") for index in range(members)]
        # Real securities replace the leading synthetic placeholders.
        for offset, security_id in enumerate(sorted(prices)):
            ticker = security_id.removeprefix("sec-").upper()
            self._members[offset] = {
                "ticker": ticker,
                "security_id": security_id,
                "issuer_id": f"iss-{ticker.lower()}",
            }
        self._boundary = boundary

    def universe_identity_labels(self, universe_id, as_of):
        assert universe_id == "sp500"
        assert as_of == DECISION
        return self._members

    def query(self, sql, params=None):
        if "MAX(received_at)" in sql:
            self.boundary_dataset = params[0]
            return [{"latest": self._boundary}]
        assert "FROM prices" in sql
        security_id = params[0]
        rows = self._prices.get(security_id, [])
        limit = params[2]
        ordered = sorted(rows, key=lambda row: row["date"], reverse=True)
        return ordered[:limit]


class _CoverageStore:
    def __init__(
        self,
        coverage: list[dict],
        *,
        boundary: datetime | None = BOUNDARY,
    ) -> None:
        self._coverage = coverage
        self._boundary = boundary

    def universe_data_coverage(self, universe_id, as_of):
        assert universe_id == "sp500"
        assert as_of == DECISION
        return self._coverage

    def query(self, sql, params=None):
        assert "MAX(received_at)" in sql
        self.boundary_dataset = params[0]
        return [{"latest": self._boundary}]


def _coverage_rows(*, members: int, priced: int, filed: int) -> list[dict]:
    rows = []
    for index in range(members):
        rows.append(
            {
                "ticker": f"T{index:03d}",
                "security_id": f"sec-{index}",
                "has_price_history": index < priced,
                "has_pit_fundamentals": index < filed,
            }
        )
    return rows


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------
def test_registry_exposes_every_rule_in_its_own_scope() -> None:
    assert registered_rule_ids() == (
        SEC_RULE_ID,
        PRICE_RULE_ID,
        COVERAGE_RULE_ID,
        MAPPING_RULE_ID,
        SHARES_RULE_ID,
        FILINGS_RULE_ID,
        FACTOR_RULE_ID,
    )
    scopes = {rule_scope(rule_id) for rule_id in registered_rule_ids()}
    assert scopes == {
        "us-equity-reference:sp500",
        "us-equity-prices:sp500",
        "us-equity-coverage:sp500",
        "us-equity-mappings:sp500",
        "us-equity-shares:sp500",
        "us-equity-filings:sp500",
        "us-equity-factors:sp500",
    }
    # Distinct scopes are what keep each rule's monotonic boundary independent.
    assert len(scopes) == len(RULE_REGISTRY)


def test_unknown_rule_scope_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown anomaly rule"):
        rule_scope("not_a_rule")


# ----------------------------------------------------------------------
# price_action_mismatch
# ----------------------------------------------------------------------
def test_price_rule_flags_a_jump_with_no_declared_action() -> None:
    store = _PriceStore(
        {
            "sec-aaa": [
                _price_row(date(2026, 8, 5), 400.0),
                _price_row(date(2026, 8, 6), 100.0),
            ]
        }
    )
    scan = scan_price_action_mismatch(store=store, as_of=DECISION)

    assert scan.rule_bundle_version == RULE_BUNDLE_VERSION
    assert scan.scope == "us-equity-prices:sp500"
    assert scan.executed_rules == (f"{PRICE_RULE_ID}@{PRICE_RULE_VERSION}",)
    assert len(scan.observations) == 1

    finding = scan.observations[0]
    assert finding.subject_id == "sec-aaa@2026-08-06"
    assert finding.new_value["declared_split_ratio"] == 1.0
    assert finding.new_value["declared_dividends"] == 0.0
    # A 4:1 split is the classic explanation for this exact shape.
    assert finding.new_value["implied_split_ratio_if_unrecorded"] == 4.0
    assert scan.evidence["safety"] == {
        "data_repairs": 0,
        "readiness_overrides": 0,
        "paper_actions": 0,
        "broker_actions": 0,
    }


def test_price_rule_accepts_a_jump_explained_by_a_declared_split() -> None:
    store = _PriceStore(
        {
            "sec-aaa": [
                _price_row(date(2026, 8, 5), 400.0),
                _price_row(date(2026, 8, 6), 100.0, split_ratio=4.0),
            ]
        }
    )
    scan = scan_price_action_mismatch(store=store, as_of=DECISION)
    assert scan.observations == ()


def test_price_rule_ignores_moves_below_the_threshold() -> None:
    store = _PriceStore(
        {
            "sec-aaa": [
                _price_row(date(2026, 8, 5), 100.0),
                _price_row(date(2026, 8, 6), 90.0),
            ]
        }
    )
    scan = scan_price_action_mismatch(store=store, as_of=DECISION)
    assert scan.observations == ()


def test_price_rule_withholds_the_whole_scan_on_incomplete_actions() -> None:
    store = _PriceStore(
        {
            "sec-aaa": [
                _price_row(date(2026, 8, 5), 400.0),
                _price_row(date(2026, 8, 6), 100.0, actions_complete=False),
            ]
        }
    )
    with pytest.raises(ValueError, match="action-complete"):
        scan_price_action_mismatch(store=store, as_of=DECISION)


def test_price_rule_withholds_when_action_fields_are_absent() -> None:
    row = _price_row(date(2026, 8, 6), 100.0)
    row["split_ratio"] = None
    store = _PriceStore(
        {"sec-aaa": [_price_row(date(2026, 8, 5), 400.0), row]}
    )
    with pytest.raises(ValueError, match="split and dividend evidence"):
        scan_price_action_mismatch(store=store, as_of=DECISION)


def test_price_rule_requires_retained_fetch_evidence_for_its_boundary() -> None:
    store = _PriceStore(
        {
            "sec-aaa": [
                _price_row(date(2026, 8, 5), 400.0),
                _price_row(date(2026, 8, 6), 100.0),
            ]
        },
        boundary=None,
    )
    with pytest.raises(ValueError, match="no retained price-fetch evidence"):
        scan_price_action_mismatch(store=store, as_of=DECISION)


def test_price_rule_refuses_an_out_of_bounds_universe() -> None:
    store = _PriceStore({"sec-aaa": []}, members=10)
    with pytest.raises(ValueError, match="expected 450-550"):
        scan_price_action_mismatch(store=store, as_of=DECISION)


def test_price_scan_is_stable_for_identical_evidence() -> None:
    prices = {
        "sec-aaa": [
            _price_row(date(2026, 8, 5), 400.0),
            _price_row(date(2026, 8, 6), 100.0),
        ]
    }
    first = scan_price_action_mismatch(store=_PriceStore(prices), as_of=DECISION)
    second = scan_price_action_mismatch(store=_PriceStore(prices), as_of=DECISION)
    assert first.scan_id == second.scan_id
    assert first.source_boundary_sha256 == second.source_boundary_sha256


def test_price_fingerprint_is_scoped_to_its_own_rule() -> None:
    price_fingerprint = anomaly_fingerprint(
        rule_id=PRICE_RULE_ID,
        rule_version=PRICE_RULE_VERSION,
        scope="us-equity-prices:sp500",
        subject_type="security_session",
        subject_id="sec-aaa@2026-08-06",
    )
    sec_fingerprint = anomaly_fingerprint(
        rule_id=SEC_RULE_ID,
        rule_version="1.0.0",
        scope="us-equity-reference:sp500",
        subject_type="security_session",
        subject_id="sec-aaa@2026-08-06",
    )
    # Same subject, different rule/scope: the ledger must not merge these cases.
    assert price_fingerprint != sec_fingerprint


# ----------------------------------------------------------------------
# coverage_deterioration
# ----------------------------------------------------------------------
def test_coverage_measurement_reports_exact_counts() -> None:
    store = _CoverageStore(_coverage_rows(members=503, priced=503, filed=502))
    measured = measure_universe_coverage(store=store, as_of=DECISION)
    assert measured["members"] == 503
    assert measured["filed_members"] == 502
    assert measured["priced_members"] == 503


def test_first_run_without_a_baseline_records_no_finding() -> None:
    store = _CoverageStore(_coverage_rows(members=503, priced=503, filed=502))
    scan = scan_coverage_deterioration(store=store, as_of=DECISION, baseline=None)
    assert scan.observations == ()
    assert scan.evidence["comparable_baseline"] is False


def test_coverage_drop_creates_one_case_per_metric() -> None:
    store = _CoverageStore(_coverage_rows(members=503, priced=500, filed=495))
    baseline = {
        "as_of": "2026-08-06",
        "universe_id": "sp500",
        "members": 503,
        "identified_members": 503,
        "priced_members": 503,
        "filed_members": 502,
    }
    scan = scan_coverage_deterioration(
        store=store,
        as_of=DECISION,
        baseline=baseline,
    )
    subjects = {row.subject_id for row in scan.observations}
    assert subjects == {"sp500:filed_members", "sp500:priced_members"}
    filed = next(
        row for row in scan.observations if row.subject_id == "sp500:filed_members"
    )
    assert filed.new_value["dropped"] == 7
    assert filed.rule_version == COVERAGE_RULE_VERSION
    assert scan.scope == "us-equity-coverage:sp500"


def test_improved_coverage_creates_no_finding() -> None:
    store = _CoverageStore(_coverage_rows(members=503, priced=503, filed=503))
    baseline = {
        "as_of": "2026-08-06",
        "universe_id": "sp500",
        "members": 503,
        "identified_members": 503,
        "priced_members": 500,
        "filed_members": 495,
    }
    scan = scan_coverage_deterioration(
        store=store,
        as_of=DECISION,
        baseline=baseline,
    )
    assert scan.observations == ()


def test_coverage_refuses_a_baseline_from_the_future() -> None:
    store = _CoverageStore(_coverage_rows(members=503, priced=503, filed=503))
    baseline = {
        "as_of": "2026-09-01",
        "universe_id": "sp500",
        "members": 503,
        "identified_members": 503,
        "priced_members": 503,
        "filed_members": 503,
    }
    with pytest.raises(ValueError, match="later than the scanned decision date"):
        scan_coverage_deterioration(
            store=store,
            as_of=DECISION,
            baseline=baseline,
        )


def test_coverage_refuses_a_baseline_for_another_universe() -> None:
    store = _CoverageStore(_coverage_rows(members=503, priced=503, filed=503))
    baseline = {
        "as_of": "2026-08-06",
        "universe_id": "nifty50",
        "members": 50,
        "identified_members": 50,
        "priced_members": 50,
        "filed_members": 50,
    }
    with pytest.raises(ValueError, match="different universe"):
        scan_coverage_deterioration(
            store=store,
            as_of=DECISION,
            baseline=baseline,
        )


def test_coverage_refuses_a_malformed_baseline_count() -> None:
    """A coerced string or None must not manufacture or hide a finding."""
    store = _CoverageStore(_coverage_rows(members=503, priced=503, filed=495))
    for bad in ("502", None, 2.5, True, -1):
        baseline = {
            "as_of": "2026-08-06",
            "universe_id": "sp500",
            "members": 503,
            "identified_members": 503,
            "priced_members": 503,
            "filed_members": bad,
        }
        with pytest.raises(ValueError, match="non-negative integer"):
            scan_coverage_deterioration(
                store=store,
                as_of=DECISION,
                baseline=baseline,
            )


def test_coverage_scan_refuses_an_empty_universe() -> None:
    with pytest.raises(ValueError, match="no active members"):
        measure_universe_coverage(store=_CoverageStore([]), as_of=DECISION)


class _CombinedStore(_PriceStore):
    """Serves both the price rule and the coverage rule from one fake."""

    def __init__(self, prices: dict[str, list[dict]], coverage: list[dict]) -> None:
        super().__init__(prices)
        self._coverage = coverage

    def universe_data_coverage(self, universe_id, as_of):
        assert universe_id == "sp500"
        assert as_of == DECISION
        return self._coverage


def _combined_store() -> _CombinedStore:
    return _CombinedStore(
        {
            "sec-aaa": [
                _price_row(date(2026, 8, 5), 400.0),
                _price_row(date(2026, 8, 6), 100.0),
            ]
        },
        _coverage_rows(members=503, priced=503, filed=502),
    )


def test_run_detectors_returns_one_independent_scan_per_rule() -> None:
    scans = run_detectors(
        store=_combined_store(),
        as_of=DECISION,
        rules=(PRICE_RULE_ID, COVERAGE_RULE_ID),
        coverage_baseline=None,
    )
    assert len(scans) == 2
    # Distinct scopes are what keep each family's boundary independent.
    assert {scan.scope for scan in scans} == {
        "us-equity-prices:sp500",
        "us-equity-coverage:sp500",
    }
    for scan in scans:
        assert len(scan.executed_rules) == 1
        assert scan.rule_bundle_version == RULE_BUNDLE_VERSION


def test_run_detectors_passes_the_coverage_baseline_through() -> None:
    baseline = {
        "as_of": "2026-08-06",
        "universe_id": "sp500",
        "members": 503,
        "identified_members": 503,
        "priced_members": 503,
        "filed_members": 503,
    }
    scans = run_detectors(
        store=_combined_store(),
        as_of=DECISION,
        rules=(COVERAGE_RULE_ID,),
        coverage_baseline=baseline,
    )
    assert len(scans) == 1
    assert scans[0].evidence["comparable_baseline"] is True
    # 503 -> 502 filed members is a real one-member drop.
    assert len(scans[0].observations) == 1


def test_run_detectors_rejects_an_unknown_rule() -> None:
    with pytest.raises(ValueError, match="unknown anomaly rule"):
        run_detectors(
            store=_combined_store(),
            as_of=DECISION,
            rules=("not_a_rule",),
        )


def test_run_detectors_propagates_a_withheld_scan() -> None:
    """One rule's missing evidence must not be swallowed into a partial result."""
    store = _CombinedStore(
        {
            "sec-aaa": [
                _price_row(date(2026, 8, 5), 400.0),
                _price_row(date(2026, 8, 6), 100.0, actions_complete=False),
            ]
        },
        _coverage_rows(members=503, priced=503, filed=502),
    )
    with pytest.raises(ValueError, match="action-complete"):
        run_detectors(store=store, as_of=DECISION, rules=(PRICE_RULE_ID,))


# ----------------------------------------------------------------------
# mapping_drift
# ----------------------------------------------------------------------
def _mapping(
    provider: str,
    symbol: str,
    start: date,
    end: date | None = None,
) -> dict:
    return {
        "provider": provider,
        "provider_symbol": symbol,
        "data_start": start,
        "data_end": end,
        "mapping_status": "verified",
        "verified_date": date(2026, 8, 1),
        "source": "reviewed-manifest",
    }


class _MappingStore:
    """Minimal Store surface used by the mapping rule."""

    def __init__(
        self,
        mappings: dict[str, list[dict]],
        *,
        members: int = 500,
        boundary: datetime | None = BOUNDARY,
    ) -> None:
        self._mappings = mappings
        self._boundary = boundary
        self._members = [_member(f"T{index:03d}") for index in range(members)]
        for offset, security_id in enumerate(sorted(mappings)):
            ticker = security_id.removeprefix("sec-").upper()
            self._members[offset] = {
                "ticker": ticker,
                "security_id": security_id,
                "issuer_id": f"iss-{ticker.lower()}",
            }

    def universe_identity_labels(self, universe_id, as_of):
        assert universe_id == "sp500"
        assert as_of == DECISION
        return self._members

    def query(self, sql, params=None):
        assert "MAX(received_at)" in sql
        self.boundary_dataset = params[0]
        return [{"latest": self._boundary}]

    def provider_symbol_mappings(self, security_id, **kwargs):
        return list(self._mappings.get(security_id, []))


def test_mapping_rule_accepts_one_active_window_per_provider() -> None:
    store = _MappingStore(
        {"sec-aaa": [_mapping("yfinance", "AAA", date(2020, 1, 1))]}
    )
    scan = scan_mapping_drift(store=store, as_of=DECISION)
    assert scan.observations == ()
    assert scan.scope == "us-equity-mappings:sp500"


def test_mapping_rule_flags_overlapping_windows_for_one_security() -> None:
    store = _MappingStore(
        {
            "sec-aaa": [
                _mapping("yfinance", "OLD", date(2020, 1, 1)),
                _mapping("yfinance", "NEW", date(2024, 1, 1)),
            ]
        }
    )
    scan = scan_mapping_drift(store=store, as_of=DECISION)
    assert len(scan.observations) == 1
    finding = scan.observations[0]
    assert finding.subject_id == "sec-aaa:yfinance"
    assert finding.new_value["active_mappings"] == 2
    assert finding.new_value["provider_symbols"] == ["NEW", "OLD"]
    assert finding.severity == "high"


def test_mapping_rule_ignores_a_closed_predecessor_window() -> None:
    store = _MappingStore(
        {
            "sec-aaa": [
                _mapping("yfinance", "OLD", date(2020, 1, 1), date(2024, 1, 1)),
                _mapping("yfinance", "NEW", date(2024, 1, 1)),
            ]
        }
    )
    scan = scan_mapping_drift(store=store, as_of=DECISION)
    assert scan.observations == ()


def test_mapping_rule_flags_a_symbol_claimed_by_two_securities() -> None:
    store = _MappingStore(
        {
            "sec-aaa": [_mapping("yfinance", "DOC", date(2020, 1, 1))],
            "sec-bbb": [_mapping("yfinance", "DOC", date(2023, 1, 1))],
        }
    )
    scan = scan_mapping_drift(store=store, as_of=DECISION)
    assert len(scan.observations) == 1
    finding = scan.observations[0]
    assert finding.subject_id == "yfinance:DOC"
    assert finding.new_value["securities"] == 2
    assert finding.new_value["security_ids"] == ["sec-aaa", "sec-bbb"]


def test_mapping_rule_separates_providers() -> None:
    """The same symbol at two providers for one security is not a conflict."""
    store = _MappingStore(
        {
            "sec-aaa": [
                _mapping("yfinance", "AAA", date(2020, 1, 1)),
                _mapping("tiingo", "AAA", date(2020, 1, 1)),
            ]
        }
    )
    scan = scan_mapping_drift(store=store, as_of=DECISION)
    assert scan.observations == ()


def test_mapping_rule_withholds_when_a_window_lacks_a_start() -> None:
    broken = _mapping("yfinance", "AAA", date(2020, 1, 1))
    broken["data_start"] = None
    store = _MappingStore({"sec-aaa": [broken]})
    with pytest.raises(ValueError, match="data_start"):
        scan_mapping_drift(store=store, as_of=DECISION)


def test_mapping_rule_refuses_an_out_of_bounds_universe() -> None:
    store = _MappingStore({"sec-aaa": []}, members=10)
    with pytest.raises(ValueError, match="expected 450-550"):
        scan_mapping_drift(store=store, as_of=DECISION)


def test_mapping_scan_is_stable_for_identical_evidence() -> None:
    mappings = {
        "sec-aaa": [
            _mapping("yfinance", "OLD", date(2020, 1, 1)),
            _mapping("yfinance", "NEW", date(2024, 1, 1)),
        ]
    }
    first = scan_mapping_drift(store=_MappingStore(mappings), as_of=DECISION)
    second = scan_mapping_drift(store=_MappingStore(mappings), as_of=DECISION)
    assert first.source_boundary_sha256 == second.source_boundary_sha256
    # Identical evidence must be idempotent in the ledger, so the id repeats.
    assert first.scan_id == second.scan_id


def test_mapping_rule_withholds_without_retained_boundary_evidence() -> None:
    """A scan may not be bounded by wall-clock time when evidence is absent."""
    store = _MappingStore(
        {"sec-aaa": [_mapping("yfinance", "AAA", date(2020, 1, 1))]},
        boundary=None,
    )
    with pytest.raises(ValueError, match="no retained companyfacts evidence"):
        scan_mapping_drift(store=store, as_of=DECISION)


def test_coverage_scan_is_stable_for_identical_evidence() -> None:
    rows = _coverage_rows(members=503, priced=503, filed=502)
    first = scan_coverage_deterioration(
        store=_CoverageStore(rows), as_of=DECISION, baseline=None
    )
    second = scan_coverage_deterioration(
        store=_CoverageStore(rows), as_of=DECISION, baseline=None
    )
    assert first.scan_id == second.scan_id


def test_share_and_filing_scans_are_stable_for_identical_evidence() -> None:
    shares = {
        "iss-aaa": [
            _shares_row(date(2025, 12, 31), 1_000_000.0),
            _shares_row(date(2026, 3, 31), 4_000_000.0),
        ]
    }
    first = scan_share_count_jump(
        store=_FundamentalStore(shares=shares), as_of=DECISION
    )
    second = scan_share_count_jump(
        store=_FundamentalStore(shares=shares), as_of=DECISION
    )
    assert first.scan_id == second.scan_id

    third = scan_conflicting_filings(
        store=_FundamentalStore(conflicts={}), as_of=DECISION
    )
    fourth = scan_conflicting_filings(
        store=_FundamentalStore(conflicts={}), as_of=DECISION
    )
    assert third.scan_id == fourth.scan_id


def test_fundamental_rules_withhold_without_retained_boundary_evidence() -> None:
    store = _FundamentalStore(
        shares={
            "iss-aaa": [
                _shares_row(date(2025, 12, 31), 1_000_000.0),
                _shares_row(date(2026, 3, 31), 1_010_000.0),
            ]
        },
        boundary=None,
    )
    with pytest.raises(ValueError, match="no retained companyfacts evidence"):
        scan_share_count_jump(store=store, as_of=DECISION)
    with pytest.raises(ValueError, match="no retained companyfacts evidence"):
        scan_conflicting_filings(store=store, as_of=DECISION)


# ----------------------------------------------------------------------
# share_count_jump and conflicting_filings
# ----------------------------------------------------------------------
class _FundamentalStore:
    """Serves the share-count and conflicting-filing rules."""

    def __init__(
        self,
        *,
        shares: dict[str, list[dict]] | None = None,
        conflicts: dict[str, list[dict]] | None = None,
        members: int = 500,
        boundary: datetime | None = BOUNDARY,
    ) -> None:
        self._shares = shares or {}
        self._conflicts = conflicts or {}
        self._boundary = boundary
        self.queried_issuers: list[str] = []
        self._members = [_member(f"T{index:03d}") for index in range(members)]
        named = sorted(set(self._shares) | set(self._conflicts))
        for offset, issuer_id in enumerate(named):
            ticker = issuer_id.removeprefix("iss-").upper()
            self._members[offset] = {
                "ticker": ticker,
                "security_id": f"sec-{ticker.lower()}",
                "issuer_id": issuer_id,
            }

    def universe_identity_labels(self, universe_id, as_of):
        assert universe_id == "sp500"
        assert as_of == DECISION
        return self._members

    def share_a_single_issuer(self, tickers: tuple[str, str], issuer_id: str) -> None:
        """Point two member securities at one issuer, as dual-class listings do."""
        for offset, ticker in enumerate(tickers):
            self._members[offset] = {
                "ticker": ticker,
                "security_id": f"sec-{ticker.lower()}",
                "issuer_id": issuer_id,
            }

    def query(self, sql, params=None):
        if "MAX(received_at)" in sql:
            self.boundary_dataset = params[0]
            return [{"latest": self._boundary}]
        issuer_id = params[0]
        self.queried_issuers.append(issuer_id)
        if "COUNT(DISTINCT value)" in sql:
            return list(self._conflicts.get(issuer_id, []))
        assert "FROM fundamentals" in sql
        # Mirror the real query: newest filings first, bounded by the lookback,
        # so the rule's own reversal is exercised rather than bypassed.
        assert "ORDER BY period_end DESC, as_of_date DESC" in sql
        rows = sorted(
            self._shares.get(issuer_id, []),
            key=lambda row: (row["period_end"], row["as_of_date"]),
            reverse=True,
        )
        return rows[: params[3]]


def _shares_row(period_end: date, value: float, known: date | None = None) -> dict:
    return {
        "period_end": period_end,
        "as_of_date": known or period_end,
        "value": value,
    }


def test_share_rule_ignores_ordinary_drift() -> None:
    store = _FundamentalStore(
        shares={
            "iss-aaa": [
                _shares_row(date(2025, 12, 31), 1_000_000.0),
                _shares_row(date(2026, 3, 31), 1_020_000.0),
            ]
        }
    )
    scan = scan_share_count_jump(store=store, as_of=DECISION)
    assert scan.observations == ()
    assert scan.scope == "us-equity-shares:sp500"


def test_share_rule_flags_a_large_jump() -> None:
    store = _FundamentalStore(
        shares={
            "iss-aaa": [
                _shares_row(date(2025, 12, 31), 1_000_000.0),
                _shares_row(date(2026, 3, 31), 4_000_000.0),
            ]
        }
    )
    scan = scan_share_count_jump(store=store, as_of=DECISION)
    assert len(scan.observations) == 1
    finding = scan.observations[0]
    # The subject carries the knowable date as well as the fiscal period: one
    # period restated on several dates must not collapse into one fingerprint.
    assert finding.subject_id == "iss-aaa@2026-03-31#2026-03-31"
    assert finding.new_value["implied_ratio"] == 4.0
    assert finding.severity == "high"


def test_share_rule_separates_restatements_of_one_period() -> None:
    """Two jumps inside one fiscal period stay two distinct review cases.

    A share count is restated across several knowable dates, so keying a case
    on the fiscal period alone repeated a fingerprint and the ledger rejects
    any scan that does.
    """
    store = _FundamentalStore(
        shares={
            "iss-aaa": [
                _shares_row(date(2026, 3, 31), 1_000_000.0, date(2026, 4, 30)),
                _shares_row(date(2026, 3, 31), 4_000_000.0, date(2026, 5, 30)),
                _shares_row(date(2026, 3, 31), 1_000_000.0, date(2026, 6, 30)),
            ]
        }
    )
    scan = scan_share_count_jump(store=store, as_of=DECISION)
    assert len(scan.observations) == 2
    assert len({row.fingerprint for row in scan.observations}) == 2
    assert [row.subject_id for row in scan.observations] == [
        "iss-aaa@2026-03-31#2026-05-30",
        "iss-aaa@2026-03-31#2026-06-30",
    ]


def test_share_rule_needs_two_observations() -> None:
    store = _FundamentalStore(
        shares={"iss-aaa": [_shares_row(date(2026, 3, 31), 1_000_000.0)]}
    )
    scan = scan_share_count_jump(store=store, as_of=DECISION)
    assert scan.observations == ()
    assert scan.evidence["examined_issuers"] == 0


def test_share_rule_reports_a_non_positive_count_instead_of_withholding() -> None:
    """A stored zero is the anomaly, not a reason to withhold the universe.

    The live archive holds 80 historical ``shares_out`` rows stored as zero.
    Raising on them withheld every scan for all 503 members and hid the very
    finding the rule exists to surface, so they are reported instead.
    """
    store = _FundamentalStore(
        shares={
            "iss-aaa": [
                _shares_row(date(2025, 12, 31), 0.0),
                _shares_row(date(2026, 3, 31), 1_000_000.0),
            ]
        }
    )
    scan = scan_share_count_jump(store=store, as_of=DECISION)
    invalid = [
        row for row in scan.observations if row.subject_type == "issuer_share_count"
    ]
    assert len(invalid) == 1
    assert invalid[0].new_value["shares_out"] == 0.0
    assert invalid[0].severity == "high"
    # The zero is excluded from the ratio comparison rather than dividing by it.
    assert all(row.subject_type == "issuer_share_count" for row in scan.observations)


def test_share_rule_still_withholds_on_a_missing_value() -> None:
    """A missing value is a contract violation and still fails closed."""
    store = _FundamentalStore(
        shares={
            "iss-aaa": [
                {"period_end": date(2025, 12, 31), "as_of_date": date(2025, 12, 31)},
                _shares_row(date(2026, 3, 31), 1_000_000.0),
            ]
        }
    )
    with pytest.raises(ValueError, match="lacks a value"):
        scan_share_count_jump(store=store, as_of=DECISION)


def test_share_rule_visits_a_dual_class_issuer_once() -> None:
    """Two member securities under one issuer must not double-report.

    The live S&P 500 holds three such issuers (Alphabet GOOG/GOOGL, Fox
    FOX/FOXA, News Corp NWS/NWSA). Iterating members rather than issuers
    queried each twice and emitted the same finding twice, and the ledger
    rejects any scan containing a duplicate fingerprint.
    """
    store = _FundamentalStore(
        shares={
            "iss-alphabet": [
                _shares_row(date(2025, 12, 31), 1_000_000.0),
                _shares_row(date(2026, 3, 31), 4_000_000.0),
            ]
        }
    )
    store.share_a_single_issuer(("GOOG", "GOOGL"), "iss-alphabet")

    scan = scan_share_count_jump(store=store, as_of=DECISION)

    assert store.queried_issuers.count("iss-alphabet") == 1
    assert len(scan.observations) == 1
    assert len({row.fingerprint for row in scan.observations}) == 1
    # Both listed tickers stay visible as evidence; neither is invented away.
    assert scan.observations[0].evidence["tickers"] == ["GOOG", "GOOGL"]
    assert scan.evidence["examined_issuers"] == 1


def test_filing_rule_visits_a_dual_class_issuer_once() -> None:
    store = _FundamentalStore(
        conflicts={
            "iss-alphabet": [
                {
                    "period_end": date(2026, 3, 31),
                    "as_of_date": date(2026, 5, 1),
                    "metric": "revenue",
                    "distinct_values": 2,
                    "minimum_value": 100.0,
                    "maximum_value": 110.0,
                }
            ]
        }
    )
    store.share_a_single_issuer(("GOOG", "GOOGL"), "iss-alphabet")

    scan = scan_conflicting_filings(store=store, as_of=DECISION)

    assert store.queried_issuers.count("iss-alphabet") == 1
    assert len(scan.observations) == 1
    assert scan.observations[0].evidence["tickers"] == ["GOOG", "GOOGL"]


def test_share_rule_bounds_its_review_window_to_recent_filings() -> None:
    """Only the most recent filings are compared.

    The retained history reaches back to 2008. Scanning all of it produced 750
    findings against the live database, which the ledger cannot store: scan
    evidence is capped at 64 KiB and `alerts.py` is inside the frozen policy
    bundle, so the cap cannot be raised. An old jump outside the window is
    therefore not reported, exactly as an old price move is not.
    """
    rows = [
        _shares_row(date(2020, 3, 31), 1_000_000.0),
        _shares_row(date(2020, 6, 30), 9_000_000.0),  # old jump, outside window
    ]
    rows += [
        _shares_row(date(2025, 3, 31), 9_000_000.0),
        _shares_row(date(2025, 6, 30), 9_100_000.0),
        _shares_row(date(2025, 9, 30), 9_200_000.0),
        _shares_row(date(2025, 12, 31), 9_300_000.0),
    ]
    store = _FundamentalStore(shares={"iss-aaa": rows})

    bounded = scan_share_count_jump(store=store, as_of=DECISION, lookback_filings=4)
    assert bounded.observations == ()
    assert bounded.evidence["lookback_filings"] == 4

    # The same evidence, reviewed over a deliberately wider window, finds it.
    swept = scan_share_count_jump(store=store, as_of=DECISION, lookback_filings=6)
    assert len(swept.observations) == 1
    assert swept.observations[0].subject_id.startswith("iss-aaa@2020-06-30")


def test_share_rule_rejects_a_degenerate_lookback() -> None:
    store = _FundamentalStore(shares={"iss-aaa": []})
    with pytest.raises(ValueError, match="at least two filings"):
        scan_share_count_jump(store=store, as_of=DECISION, lookback_filings=1)


def test_share_rule_refuses_an_out_of_bounds_universe() -> None:
    store = _FundamentalStore(shares={"iss-aaa": []}, members=10)
    with pytest.raises(ValueError, match="expected 450-550"):
        scan_share_count_jump(store=store, as_of=DECISION)


def test_filing_rule_is_silent_without_conflicts() -> None:
    store = _FundamentalStore(conflicts={})
    scan = scan_conflicting_filings(store=store, as_of=DECISION)
    assert scan.observations == ()
    assert scan.scope == "us-equity-filings:sp500"


def test_filing_rule_flags_one_key_with_two_values() -> None:
    store = _FundamentalStore(
        conflicts={
            "iss-aaa": [
                {
                    "period_end": date(2026, 3, 31),
                    "as_of_date": date(2026, 5, 1),
                    "metric": "revenue",
                    "distinct_values": 2,
                    "minimum_value": 100.0,
                    "maximum_value": 110.0,
                }
            ]
        }
    )
    scan = scan_conflicting_filings(store=store, as_of=DECISION)
    assert len(scan.observations) == 1
    finding = scan.observations[0]
    assert finding.subject_id == "iss-aaa:revenue@2026-03-31#2026-05-01"
    assert finding.new_value["distinct_values"] == 2
    assert finding.severity == "high"


def test_filing_rule_withholds_on_a_malformed_count() -> None:
    store = _FundamentalStore(
        conflicts={
            "iss-aaa": [
                {
                    "period_end": date(2026, 3, 31),
                    "as_of_date": date(2026, 5, 1),
                    "metric": "revenue",
                    "distinct_values": "2",
                    "minimum_value": 100.0,
                    "maximum_value": 110.0,
                }
            ]
        }
    )
    with pytest.raises(ValueError, match="non-negative integer"):
        scan_conflicting_filings(store=store, as_of=DECISION)


# ----------------------------------------------------------------------
# Shared safety contract
# ----------------------------------------------------------------------
def _clean_scan(rule_id: str):
    """Build one finding-free scan per rule from minimal healthy evidence."""
    if rule_id == PRICE_RULE_ID:
        return scan_price_action_mismatch(
            store=_PriceStore(
                {
                    "sec-aaa": [
                        _price_row(date(2026, 8, 5), 100.0),
                        _price_row(date(2026, 8, 6), 101.0),
                    ]
                }
            ),
            as_of=DECISION,
        )
    if rule_id == COVERAGE_RULE_ID:
        return scan_coverage_deterioration(
            store=_CoverageStore(_coverage_rows(members=503, priced=503, filed=503)),
            as_of=DECISION,
            baseline=None,
        )
    if rule_id == MAPPING_RULE_ID:
        return scan_mapping_drift(
            store=_MappingStore(
                {"sec-aaa": [_mapping("yfinance", "AAA", date(2020, 1, 1))]}
            ),
            as_of=DECISION,
        )
    if rule_id == SHARES_RULE_ID:
        return scan_share_count_jump(
            store=_FundamentalStore(
                shares={
                    "iss-aaa": [
                        _shares_row(date(2025, 12, 31), 1_000_000.0),
                        _shares_row(date(2026, 3, 31), 1_010_000.0),
                    ]
                }
            ),
            as_of=DECISION,
        )
    if rule_id == FILINGS_RULE_ID:
        return scan_conflicting_filings(
            store=_FundamentalStore(conflicts={}),
            as_of=DECISION,
        )
    if rule_id == FACTOR_RULE_ID:
        return scan_factor_percentile_jump(
            store=_FactorStore(),
            as_of=DECISION,
            baseline=None,
            current=_factor_snapshot(DECISION, {"AAA": 50.0}),
        )
    raise AssertionError(f"no clean-scan builder for {rule_id}")


NEW_RULE_IDS = [
    PRICE_RULE_ID,
    COVERAGE_RULE_ID,
    MAPPING_RULE_ID,
    SHARES_RULE_ID,
    FILINGS_RULE_ID,
    FACTOR_RULE_ID,
]


@pytest.mark.parametrize("rule_id", NEW_RULE_IDS)
def test_new_rules_declare_the_zero_mutation_safety_contract(rule_id) -> None:
    scan = _clean_scan(rule_id)
    assert scan.evidence["safety"] == {
        "data_repairs": 0,
        "readiness_overrides": 0,
        "paper_actions": 0,
        "broker_actions": 0,
    }
    assert scan.evidence["temporal_mode"] == "retrospective_review_no_backfill"
    assert scan.rule_bundle_version == RULE_BUNDLE_VERSION


@pytest.mark.parametrize("rule_id", NEW_RULE_IDS)
def test_each_rule_executes_exactly_one_rule_in_its_own_scope(rule_id) -> None:
    """The shipped ledger pins a single-rule executed_rules tuple."""
    scan = _clean_scan(rule_id)
    assert len(scan.executed_rules) == 1
    assert scan.executed_rules[0].startswith(f"{rule_id}@")
    assert scan.scope == rule_scope(rule_id)


@pytest.mark.parametrize("rule_id", NEW_RULE_IDS)
def test_healthy_evidence_produces_no_findings(rule_id) -> None:
    assert _clean_scan(rule_id).observations == ()


def test_every_registered_rule_has_a_dispatch_entry() -> None:
    """Registry and run_detectors dispatch must not drift apart."""
    dispatchable = set(NEW_RULE_IDS) | {SEC_RULE_ID}
    assert set(registered_rule_ids()) == dispatchable


# ----------------------------------------------------------------------
# Source-boundary dataset labels
# ----------------------------------------------------------------------
# The retained archive labels a fetch by its DATASET, which is not the storage
# table name: every price response (yfinance and Tiingo alike) is stored under
# `daily-prices`, never `prices`. A rule that bounds itself with the table name
# matches no retained evidence and withholds every scan against the real
# archive, which is exactly what `prices` did. These labels are verified
# against `SELECT DISTINCT dataset FROM raw_snapshots` in the live database.
BOUNDARY_DATASETS = {
    PRICE_RULE_ID: "daily-prices",
    COVERAGE_RULE_ID: "companyfacts",
    MAPPING_RULE_ID: "companyfacts",
    SHARES_RULE_ID: "companyfacts",
    FILINGS_RULE_ID: "companyfacts",
    FACTOR_RULE_ID: "companyfacts",
}


def _boundary_store(rule_id: str):
    """Run one rule and hand back the store it bounded itself with."""
    if rule_id == PRICE_RULE_ID:
        store = _PriceStore(
            {
                "sec-aaa": [
                    _price_row(date(2026, 8, 5), 100.0),
                    _price_row(date(2026, 8, 6), 101.0),
                ]
            }
        )
        scan_price_action_mismatch(store=store, as_of=DECISION)
    elif rule_id == COVERAGE_RULE_ID:
        store = _CoverageStore(_coverage_rows(members=503, priced=503, filed=503))
        scan_coverage_deterioration(store=store, as_of=DECISION, baseline=None)
    elif rule_id == MAPPING_RULE_ID:
        store = _MappingStore({"sec-aaa": [_mapping("yfinance", "AAA", date(2020, 1, 1))]})
        scan_mapping_drift(store=store, as_of=DECISION)
    elif rule_id == SHARES_RULE_ID:
        store = _FundamentalStore(
            shares={"iss-aaa": [_shares_row(date(2026, 3, 31), 1_000_000.0)]}
        )
        scan_share_count_jump(store=store, as_of=DECISION)
    elif rule_id == FILINGS_RULE_ID:
        store = _FundamentalStore(conflicts={})
        scan_conflicting_filings(store=store, as_of=DECISION)
    elif rule_id == FACTOR_RULE_ID:
        store = _FactorStore()
        scan_factor_percentile_jump(
            store=store,
            as_of=DECISION,
            baseline=None,
            current=_factor_snapshot(DECISION, {"AAA": 50.0}),
        )
    else:  # pragma: no cover - the parametrization covers every rule
        raise AssertionError(f"no boundary-store builder for {rule_id}")
    return store


@pytest.mark.parametrize("rule_id", NEW_RULE_IDS)
def test_each_rule_bounds_itself_with_a_real_archive_dataset(rule_id) -> None:
    assert _boundary_store(rule_id).boundary_dataset == BOUNDARY_DATASETS[rule_id]


# ----------------------------------------------------------------------
# Ledger acceptance
# ----------------------------------------------------------------------
def _finding_scan(rule_id: str):
    """Build one scan per rule that actually contains a finding.

    A finding-free scan validates trivially: every per-observation contract is
    skipped when there are no observations. Only a scan WITH findings proves a
    rule can reach the ledger.
    """
    if rule_id == PRICE_RULE_ID:
        return scan_price_action_mismatch(
            store=_PriceStore(
                {
                    "sec-aaa": [
                        _price_row(date(2026, 8, 5), 100.0),
                        _price_row(date(2026, 8, 6), 40.0),
                    ]
                }
            ),
            as_of=DECISION,
        )
    if rule_id == COVERAGE_RULE_ID:
        return scan_coverage_deterioration(
            store=_CoverageStore(_coverage_rows(members=503, priced=503, filed=490)),
            as_of=DECISION,
            baseline={
                "as_of": "2026-08-06",
                "universe_id": "sp500",
                "members": 503,
                "identified_members": 503,
                "priced_members": 503,
                "filed_members": 503,
            },
        )
    if rule_id == MAPPING_RULE_ID:
        return scan_mapping_drift(
            store=_MappingStore(
                {
                    "sec-aaa": [
                        _mapping("yfinance", "AAA", date(2020, 1, 1)),
                        _mapping("yfinance", "AAB", date(2021, 1, 1)),
                    ]
                }
            ),
            as_of=DECISION,
        )
    if rule_id == SHARES_RULE_ID:
        return scan_share_count_jump(
            store=_FundamentalStore(
                shares={
                    "iss-aaa": [
                        _shares_row(date(2025, 12, 31), 1_000_000.0),
                        _shares_row(date(2026, 3, 31), 4_000_000.0),
                        _shares_row(date(2026, 6, 30), 0.0),
                    ]
                }
            ),
            as_of=DECISION,
        )
    if rule_id == FILINGS_RULE_ID:
        return scan_conflicting_filings(
            store=_FundamentalStore(
                conflicts={
                    "iss-aaa": [
                        {
                            "period_end": date(2026, 3, 31),
                            "as_of_date": date(2026, 5, 1),
                            "metric": "revenue",
                            "distinct_values": 2,
                            "minimum_value": 100.0,
                            "maximum_value": 110.0,
                        }
                    ]
                }
            ),
            as_of=DECISION,
        )
    if rule_id == FACTOR_RULE_ID:
        return scan_factor_percentile_jump(
            store=_FactorStore(),
            as_of=DECISION,
            baseline=_factor_snapshot(date(2026, 8, 6), {"AAA": 20.0}),
            current=_factor_snapshot(DECISION, {"AAA": 85.0}),
        )
    raise AssertionError(f"no finding-scan builder for {rule_id}")


@pytest.mark.parametrize("rule_id", NEW_RULE_IDS)
def test_a_scan_with_findings_is_accepted_by_the_ledger(rule_id) -> None:
    """Every rule must survive the real ledger contract, not just run.

    `_prepare_anomaly_scan` is the pure validator `record_anomaly_scan` applies
    before writing anything. It rejects an unsupported severity or confidence
    and any repeated fingerprint, so a rule that produces findings the ledger
    refuses is unusable no matter how correct its detection logic is.
    """
    scan = _finding_scan(rule_id)
    assert scan.observations, "this scan is meant to contain findings"
    prepare_anomaly_scan(scan)


@pytest.mark.parametrize("rule_id", NEW_RULE_IDS)
def test_every_finding_uses_a_ledger_supported_confidence(rule_id) -> None:
    for observation in _finding_scan(rule_id).observations:
        assert observation.confidence in ANOMALY_CONFIDENCES
        assert observation.severity in ANOMALY_SEVERITIES


# ----------------------------------------------------------------------
# factor_percentile_jump
# ----------------------------------------------------------------------
class _FactorStore:
    """Only the source-boundary surface: the snapshot is passed in directly."""

    def __init__(self, *, boundary: datetime | None = BOUNDARY) -> None:
        self._boundary = boundary

    def query(self, sql, params=None):
        assert "MAX(received_at)" in sql
        self.boundary_dataset = params[0]
        return [{"latest": self._boundary}]


def _factor_snapshot(
    as_of: date,
    scores: dict[str, float],
    *,
    universe_id: str = "sp500",
    factor_model: str = "qv",
    withheld: list[str] | None = None,
) -> dict:
    return {
        "as_of": as_of.isoformat(),
        "universe_id": universe_id,
        "factor_model": factor_model,
        "members": len(scores) + len(withheld or []),
        "scored_members": len(scores),
        "withheld_members": sorted(withheld or []),
        "scores": dict(scores),
    }


def test_factor_rule_is_silent_without_a_baseline() -> None:
    """A first run must never invent a jump."""
    scan = scan_factor_percentile_jump(
        store=_FactorStore(),
        as_of=DECISION,
        baseline=None,
        current=_factor_snapshot(DECISION, {"AAA": 50.0, "BBB": 10.0}),
    )
    assert scan.observations == ()
    assert scan.scope == "us-equity-factors:sp500"
    assert scan.executed_rules == (f"{FACTOR_RULE_ID}@{FACTOR_RULE_VERSION}",)
    assert scan.evidence["comparable_baseline"] is False


def test_factor_rule_flags_a_large_percentile_move() -> None:
    scan = scan_factor_percentile_jump(
        store=_FactorStore(),
        as_of=DECISION,
        baseline=_factor_snapshot(date(2026, 8, 6), {"AAA": 20.0, "BBB": 50.0}),
        current=_factor_snapshot(DECISION, {"AAA": 85.0, "BBB": 52.0}),
    )
    assert len(scan.observations) == 1
    finding = scan.observations[0]
    assert finding.subject_id == "sp500:qv:AAA"
    assert finding.new_value["move_points"] == 65.0
    assert finding.severity == "high"
    assert scan.evidence["compared_members"] == 2


def test_factor_rule_ignores_ordinary_peer_drift() -> None:
    scan = scan_factor_percentile_jump(
        store=_FactorStore(),
        as_of=DECISION,
        baseline=_factor_snapshot(date(2026, 8, 6), {"AAA": 50.0}),
        current=_factor_snapshot(DECISION, {"AAA": 62.0}),
    )
    assert scan.observations == ()


def test_factor_rule_compares_only_members_present_on_both_sides() -> None:
    """A newly scored or newly withheld member is not a jump.

    An added member has no previous percentile and a dropped one has no current
    percentile; treating either as a move from zero would manufacture a finding
    out of a coverage change, which is a different rule's subject.
    """
    scan = scan_factor_percentile_jump(
        store=_FactorStore(),
        as_of=DECISION,
        baseline=_factor_snapshot(date(2026, 8, 6), {"AAA": 50.0, "GONE": 90.0}),
        current=_factor_snapshot(DECISION, {"AAA": 51.0, "NEW": 5.0}),
    )
    assert scan.observations == ()
    assert scan.evidence["compared_members"] == 1


def test_factor_rule_refuses_a_baseline_from_another_model() -> None:
    """Comparing QV against QVML would manufacture jumps from a definition change."""
    with pytest.raises(ValueError, match="different factor model"):
        scan_factor_percentile_jump(
            store=_FactorStore(),
            as_of=DECISION,
            baseline=_factor_snapshot(
                date(2026, 8, 6), {"AAA": 20.0}, factor_model="qvml"
            ),
            current=_factor_snapshot(DECISION, {"AAA": 85.0}),
        )


def test_factor_rule_refuses_a_baseline_from_another_universe() -> None:
    with pytest.raises(ValueError, match="different universe"):
        scan_factor_percentile_jump(
            store=_FactorStore(),
            as_of=DECISION,
            baseline=_factor_snapshot(
                date(2026, 8, 6), {"AAA": 20.0}, universe_id="nifty50"
            ),
            current=_factor_snapshot(DECISION, {"AAA": 85.0}),
        )


def test_factor_rule_refuses_a_later_baseline() -> None:
    with pytest.raises(ValueError, match="later than the scanned decision date"):
        scan_factor_percentile_jump(
            store=_FactorStore(),
            as_of=DECISION,
            baseline=_factor_snapshot(date(2026, 8, 8), {"AAA": 20.0}),
            current=_factor_snapshot(DECISION, {"AAA": 85.0}),
        )


def test_factor_rule_refuses_a_snapshot_for_another_date() -> None:
    with pytest.raises(ValueError, match="different decision date"):
        scan_factor_percentile_jump(
            store=_FactorStore(),
            as_of=DECISION,
            baseline=None,
            current=_factor_snapshot(date(2026, 8, 6), {"AAA": 50.0}),
        )


@pytest.mark.parametrize("bad", ["50", None, True, -1.0, 101.0])
def test_factor_rule_refuses_a_malformed_percentile(bad) -> None:
    with pytest.raises(ValueError):
        scan_factor_percentile_jump(
            store=_FactorStore(),
            as_of=DECISION,
            baseline=_factor_snapshot(date(2026, 8, 6), {"AAA": bad}),
            current=_factor_snapshot(DECISION, {"AAA": 50.0}),
        )


def test_factor_rule_refuses_a_degenerate_threshold() -> None:
    for bad in (0.0, -5.0, 101.0):
        with pytest.raises(ValueError, match="percentile points"):
            scan_factor_percentile_jump(
                store=_FactorStore(),
                as_of=DECISION,
                baseline=None,
                current=_factor_snapshot(DECISION, {"AAA": 50.0}),
                jump_threshold=bad,
            )


def test_factor_rule_keeps_score_maps_out_of_scan_evidence() -> None:
    """503 scores on both sides would not fit the ledger's 64 KiB limit."""
    scores = {f"T{index:03d}": float(index % 100) for index in range(503)}
    scan = scan_factor_percentile_jump(
        store=_FactorStore(),
        as_of=DECISION,
        baseline=None,
        current=_factor_snapshot(DECISION, scores),
    )
    assert "scores" not in scan.evidence
    assert scan.evidence["scored_members"] == 503
    assert len(scan.evidence["factor_set_sha256"]) == 64
    prepare_anomaly_scan(scan)


# ----------------------------------------------------------------------
# Factor-percentile baseline persistence (outside the ledger)
# ----------------------------------------------------------------------
def test_factor_baseline_round_trips_through_disk(tmp_path) -> None:
    snapshot = _factor_snapshot(date(2026, 8, 6), {"AAA": 50.0, "BBB": 10.0})
    path = anomalies.record_factor_percentile_baseline(
        snapshot, baseline_dir=tmp_path
    )
    assert path.is_file()

    reloaded = anomalies.latest_factor_percentile_baseline(
        universe_id="sp500", factor_model="qv", baseline_dir=tmp_path
    )
    assert reloaded == snapshot


def test_factor_baseline_returns_none_when_nothing_recorded(tmp_path) -> None:
    assert (
        anomalies.latest_factor_percentile_baseline(baseline_dir=tmp_path) is None
    )


def test_factor_baseline_picks_the_most_recent_snapshot(tmp_path) -> None:
    older = _factor_snapshot(date(2026, 7, 1), {"AAA": 20.0})
    newer = _factor_snapshot(date(2026, 8, 1), {"AAA": 60.0})
    anomalies.record_factor_percentile_baseline(older, baseline_dir=tmp_path)
    anomalies.record_factor_percentile_baseline(newer, baseline_dir=tmp_path)

    latest = anomalies.latest_factor_percentile_baseline(baseline_dir=tmp_path)
    assert latest == newer


def test_factor_baseline_before_excludes_same_and_later_snapshots(tmp_path) -> None:
    early = _factor_snapshot(date(2026, 7, 1), {"AAA": 20.0})
    same_day = _factor_snapshot(date(2026, 8, 1), {"AAA": 60.0})
    anomalies.record_factor_percentile_baseline(early, baseline_dir=tmp_path)
    anomalies.record_factor_percentile_baseline(same_day, baseline_dir=tmp_path)

    result = anomalies.latest_factor_percentile_baseline(
        before=date(2026, 8, 1), baseline_dir=tmp_path
    )
    assert result == early


def test_factor_baseline_is_write_once(tmp_path) -> None:
    snapshot = _factor_snapshot(date(2026, 8, 6), {"AAA": 50.0})
    anomalies.record_factor_percentile_baseline(snapshot, baseline_dir=tmp_path)
    with pytest.raises(FileExistsError):
        anomalies.record_factor_percentile_baseline(snapshot, baseline_dir=tmp_path)


def test_factor_baseline_separates_universe_and_model_scopes(tmp_path) -> None:
    qv_snapshot = _factor_snapshot(
        date(2026, 8, 1), {"AAA": 50.0}, universe_id="sp500", factor_model="qv"
    )
    qvml_snapshot = _factor_snapshot(
        date(2026, 8, 1), {"AAA": 90.0}, universe_id="sp500", factor_model="qvml"
    )
    anomalies.record_factor_percentile_baseline(qv_snapshot, baseline_dir=tmp_path)
    anomalies.record_factor_percentile_baseline(qvml_snapshot, baseline_dir=tmp_path)

    assert (
        anomalies.latest_factor_percentile_baseline(
            universe_id="sp500", factor_model="qv", baseline_dir=tmp_path
        )
        == qv_snapshot
    )
    assert (
        anomalies.latest_factor_percentile_baseline(
            universe_id="sp500", factor_model="qvml", baseline_dir=tmp_path
        )
        == qvml_snapshot
    )

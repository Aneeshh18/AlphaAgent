from __future__ import annotations

import json
import sys
from datetime import date
from types import SimpleNamespace

import pandas as pd

from aios import cli
from aios.corporate_actions import (
    CORPORATE_ACTION_POLICY_VERSION,
    project_yfinance_action,
    reviewed_corporate_actions,
)
from aios.ingest import prices as prices_module
from aios.ingest.prices import (
    YFINANCE_CORPORATE_ACTION_PARSER_VERSION,
    fetch_yfinance,
    parse_yfinance_normalized_export,
    parse_yfinance_normalized_export_v4,
    relabel_provider_price_rows,
)
from aios.raw_snapshots import verify_raw_snapshots
from aios.storage.store import Store

HON_SECURITY_ID = "aios:bounded:sp500:hon"


def _payload(*, symbol: str = "HON", stock_splits: float = 0.9535) -> bytes:
    return json.dumps(
        {
            "export_schema_version": 1,
            "provider": "yfinance",
            "symbol": symbol,
            "requested_start": "2026-06-29",
            "requested_end_exclusive": "2026-06-30",
            "normalization_through": "2026-06-30",
            "provider_rows": [
                {
                    "date": "2026-06-29",
                    "open": 226.0,
                    "high": 230.0,
                    "low": 225.0,
                    "close": 227.8,
                    "adj_close": 227.8,
                    "volume": 10_000,
                    "dividends": 0.0,
                    "stock_splits": stock_splits,
                }
            ],
        },
        sort_keys=True,
    ).encode()


def _mapping(*, security_id: str = HON_SECURITY_ID) -> dict:
    return {
        "mapping_status": "verified",
        "security_id": security_id,
        "provider": "yfinance",
        "provider_symbol": "HON",
        "data_start": "2026-01-01",
        "data_end": "2027-01-01",
    }


def _assignments() -> list[dict]:
    return [
        {
            "ticker": "HON",
            "effective_start": "2026-01-01",
            "effective_end": "2027-01-01",
        }
    ]


def test_yfinance_v4_keeps_v3_replay_immutable_and_separates_price_factor() -> None:
    payload = _payload()

    v3 = parse_yfinance_normalized_export(payload)[0]
    v4 = parse_yfinance_normalized_export_v4(payload)[0]

    assert v3["split_ratio"] == 0.9535
    assert v3["close_split_adjusted"] is True
    assert v4["provider_price_continuity_factor"] == 0.9535
    assert v4["split_ratio"] == 1.0
    assert v4["actions_complete"] is False
    assert v4["close_split_adjusted"] is False
    assert v4["split_normalization_factor"] == 1.0


def test_hon_review_records_legal_ratio_but_blocks_incomplete_distribution() -> None:
    raw_row = parse_yfinance_normalized_export_v4(_payload())[0]

    reviewed = project_yfinance_action(
        raw_row,
        security_id=HON_SECURITY_ID,
        provider_symbol="HON",
    )

    assert reviewed["provider_price_continuity_factor"] == 0.9535
    assert reviewed["split_ratio"] == 0.5
    assert reviewed["actions_complete"] is False
    assert reviewed["corporate_action_review_status"] == "reviewed_incomplete"
    assert reviewed["corporate_action_review_id"] == "hon-2026-06-29-reverse-split-hona-v1"
    assert reviewed["corporate_action_policy_version"] == CORPORATE_ACTION_POLICY_VERSION


def test_action_review_requires_exact_security_identity_and_provider_factor() -> None:
    raw_row = parse_yfinance_normalized_export_v4(_payload())[0]
    wrong_security = project_yfinance_action(
        raw_row,
        security_id="aios:security:not-hon",
        provider_symbol="HON",
    )
    changed_factor = project_yfinance_action(
        {**raw_row, "provider_price_continuity_factor": 0.95},
        security_id=HON_SECURITY_ID,
        provider_symbol="HON",
    )

    assert wrong_security["split_ratio"] == 1.0
    assert wrong_security["corporate_action_review_status"] == "unreviewed_provider_action"
    assert changed_factor["split_ratio"] == 1.0
    assert changed_factor["corporate_action_review_status"] == "provider_factor_mismatch"


def test_action_review_rejects_non_finite_provider_factors() -> None:
    raw_row = parse_yfinance_normalized_export_v4(_payload())[0]

    for provider_factor in (float("nan"), float("inf"), float("-inf")):
        try:
            project_yfinance_action(
                {**raw_row, "provider_price_continuity_factor": provider_factor},
                security_id=HON_SECURITY_ID,
                provider_symbol="HON",
            )
            raise AssertionError("expected a non-finite provider factor to be refused")
        except ValueError as exc:
            assert "finite and positive" in str(exc)


def test_no_action_provider_row_is_complete_without_event_review() -> None:
    row = parse_yfinance_normalized_export_v4(_payload(stock_splits=0.0))[0]
    projected = project_yfinance_action(
        row,
        security_id=HON_SECURITY_ID,
        provider_symbol="HON",
    )

    assert projected["provider_price_continuity_factor"] == 1.0
    assert projected["split_ratio"] == 1.0
    assert projected["actions_complete"] is True
    assert projected["corporate_action_review_status"] == "provider_reported_none"


def test_identity_relabeling_applies_reviewed_action_policy() -> None:
    rows = parse_yfinance_normalized_export_v4(_payload())

    relabeled = relabel_provider_price_rows(rows, _mapping(), _assignments())

    assert relabeled[0]["security_id"] == HON_SECURITY_ID
    assert relabeled[0]["split_ratio"] == 0.5
    assert relabeled[0]["actions_complete"] is False


def test_review_policy_has_unique_exact_keys_and_auditable_sources() -> None:
    actions = reviewed_corporate_actions()
    keys = {
        (
            action.security_id,
            action.provider,
            action.provider_symbol,
            action.effective_date,
        )
        for action in actions
    }

    assert len(keys) == len(actions)
    assert all(action.source_urls for action in actions)
    assert all(action.known_date <= action.effective_date for action in actions)


def test_explicit_provider_fetch_threads_v4_parser(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_fetch_yfinance(*_args, parser_version, **_kwargs):
        captured["parser_version"] = parser_version
        return []

    monkeypatch.setattr(prices_module, "fetch_yfinance", fake_fetch_yfinance)

    prices_module.fetch_provider_prices(
        "yfinance",
        "HON",
        "2026-06-29",
        "2026-06-30",
        yfinance_parser_version=YFINANCE_CORPORATE_ACTION_PARSER_VERSION,
    )

    assert captured["parser_version"] == YFINANCE_CORPORATE_ACTION_PARSER_VERSION


def test_price_action_refresh_uses_v4_parser(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class FakeStore:
        @staticmethod
        def price_action_refresh_candidates(*_args):
            return [HON_SECURITY_ID]

        @staticmethod
        def unverified_price_action_count(*_args):
            return 0

    def fake_ingest_security_prices(*_args, yfinance_parser_version, **_kwargs):
        captured["parser_version"] = yfinance_parser_version
        return 1

    monkeypatch.setattr(cli, "get_store", lambda: FakeStore())
    monkeypatch.setattr(
        prices_module,
        "ingest_security_prices",
        fake_ingest_security_prices,
    )

    cli.refresh_price_actions.__wrapped__(
        start="2026-06-29",
        end="2026-06-30",
        provider="yfinance",
        limit=None,
        tickers=None,
    )

    assert captured["parser_version"] == YFINANCE_CORPORATE_ACTION_PARSER_VERSION


def test_v4_fresh_capture_is_explicit_and_replayable(monkeypatch, tmp_path) -> None:
    monkeypatch.setitem(
        sys.modules,
        "yfinance",
        SimpleNamespace(
            download=lambda *_args, **_kwargs: pd.DataFrame(
                {
                    "Open": [226.0],
                    "High": [230.0],
                    "Low": [225.0],
                    "Close": [227.8],
                    "Adj Close": [227.8],
                    "Volume": [10_000],
                    "Dividends": [0.0],
                    "Stock Splits": [0.9535],
                },
                index=[pd.Timestamp("2026-06-29")],
            )
        ),
    )
    monkeypatch.setattr(
        "aios.ingest.prices.latest_completed_us_equity_session",
        lambda: date(2026, 6, 30),
    )
    store = Store(tmp_path / "corporate-actions.duckdb")
    try:
        rows = fetch_yfinance(
            "HON",
            start="2026-06-29",
            end="2026-06-30",
            parser_version=YFINANCE_CORPORATE_ACTION_PARSER_VERSION,
            store=store,
            ingest_run_id="hon-v4-capture",
            project_root=tmp_path,
        )

        snapshot = store.query(
            "SELECT parser_version FROM raw_snapshots"
        )[0]
        assert snapshot["parser_version"] == YFINANCE_CORPORATE_ACTION_PARSER_VERSION
        assert rows[0]["provider_price_continuity_factor"] == 0.9535
        assert rows[0]["split_ratio"] == 1.0
        assert rows[0]["actions_complete"] is False
        assert verify_raw_snapshots(
            store=store,
            project_root=tmp_path,
        ).replayed_snapshots == 1
    finally:
        store.close()

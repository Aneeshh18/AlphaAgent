"""Reviewed post-membership price paths for orderly portfolio liquidation.

A security can leave an index between quarterly decisions while remaining
listed. A survivorship-safe book must keep pricing the already-held security
until the next rebalance, without making it factor-eligible again. These short
extensions require exact prior identity/provider anchors and complete session
coverage; delisted securities must instead use a reviewed conversion or cash
event.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from aios.ingest.prices import fetch_provider_prices
from aios.ingest.security_identity import SECURITY_ID_PATTERN
from aios.market_calendar import us_equity_sessions
from aios.price_provenance import canonical_price_payload_hash
from aios.storage.store import Store, get_store

REVIEW_POLICY = "adjacent_identity_provider_v1"
PURPOSE = "portfolio_liquidation"
TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.-]*$")
MANIFEST_COLUMNS = {
    "universe_id",
    "security_id",
    "ticker",
    "provider",
    "provider_symbol",
    "data_start",
    "data_end",
    "verified_date",
    "identity_source",
    "provider_source",
    "purpose",
}


def load_liquidation_extension_csv(path: str | Path) -> list[dict]:
    """Load a strict operator-reviewed liquidation-extension manifest."""
    csv_path = Path(path)
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = [str(field or "").strip().lower() for field in reader.fieldnames or []]
        if len(fields) != len(set(fields)):
            raise ValueError("liquidation extension CSV has duplicate columns")
        actual = set(fields)
        if actual != MANIFEST_COLUMNS:
            missing = MANIFEST_COLUMNS - actual
            unknown = actual - MANIFEST_COLUMNS
            details: list[str] = []
            if missing:
                details.append(f"missing {', '.join(sorted(missing))}")
            if unknown:
                details.append(f"unsupported {', '.join(sorted(unknown))}")
            raise ValueError(
                f"liquidation extension CSV columns invalid: {'; '.join(details)}"
            )
        output: list[dict] = []
        seen: set[str] = set()
        for row_number, raw in enumerate(reader, start=2):
            if None in raw:
                raise ValueError(f"liquidation extension row {row_number} has extra fields")
            row = {
                str(key).strip().lower(): str(value or "").strip()
                for key, value in raw.items()
            }
            if not any(row.values()):
                continue
            security_id = _required(row, "security_id", row_number)
            if not SECURITY_ID_PATTERN.fullmatch(security_id):
                raise ValueError(
                    f"liquidation extension row {row_number}: invalid security_id"
                )
            if security_id in seen:
                raise ValueError(
                    f"liquidation extension row {row_number}: duplicate security_id"
                )
            seen.add(security_id)
            ticker = _required(row, "ticker", row_number).upper()
            if not TICKER_PATTERN.fullmatch(ticker):
                raise ValueError(f"liquidation extension row {row_number}: invalid ticker")
            provider = _required(row, "provider", row_number).lower()
            if provider not in {"yfinance", "tiingo"}:
                raise ValueError(
                    f"liquidation extension row {row_number}: unsupported provider"
                )
            start = _date(row, "data_start", row_number)
            end = _date(row, "data_end", row_number)
            if end <= start or (end - start).days > 45:
                raise ValueError(
                    f"liquidation extension row {row_number}: invalid bounded window"
                )
            verified_date = _date(row, "verified_date", row_number)
            if verified_date > date.today() or end > verified_date + timedelta(days=1):
                raise ValueError(
                    f"liquidation extension row {row_number}: window exceeds review date"
                )
            if row["purpose"] != PURPOSE:
                raise ValueError(
                    f"liquidation extension row {row_number}: unsupported purpose"
                )
            output.append(
                {
                    "universe_id": _required(row, "universe_id", row_number),
                    "security_id": security_id,
                    "ticker": ticker,
                    "provider": provider,
                    "provider_symbol": _required(
                        row, "provider_symbol", row_number
                    ).upper(),
                    "data_start": start,
                    "data_end": end,
                    "verified_date": verified_date,
                    "identity_source": _https(row, "identity_source", row_number),
                    "provider_source": _https(row, "provider_source", row_number),
                    "purpose": PURPOSE,
                }
            )
    if not output:
        raise ValueError("liquidation extension CSV has no data rows")
    return output


def ingest_liquidation_extension_csv(
    path: str | Path,
    *,
    store: Store | None = None,
    fetcher: Callable[[str, str, str, str], list[dict[str, Any]]] = (
        fetch_provider_prices
    ),
) -> dict[str, int]:
    """Fetch, session-check, hash, and atomically import reviewed extensions."""
    db = store or get_store()
    manifest_rows = load_liquidation_extension_csv(path)
    provenance_rows: list[dict] = []
    all_prices: list[dict] = []
    for manifest in manifest_rows:
        start = manifest["data_start"]
        end = manifest["data_end"]
        fetched = fetcher(
            manifest["provider"],
            manifest["provider_symbol"],
            start.isoformat(),
            end.isoformat(),
        )
        expected_sessions = us_equity_sessions(start, end)
        actual_dates = {date.fromisoformat(str(row["date"])[:10]) for row in fetched}
        if not expected_sessions:
            raise ValueError(f"{manifest['ticker']} liquidation window has no sessions")
        if actual_dates != set(expected_sessions):
            missing = sorted(set(expected_sessions) - actual_dates)
            extra = sorted(actual_dates - set(expected_sessions))
            detail = (
                f"missing {missing[0]}" if missing else f"unexpected {extra[0]}"
            )
            raise ValueError(
                f"{manifest['ticker']} liquidation price history is incomplete: {detail}"
            )
        provenance_id = (
            f"liquidation:{manifest['security_id']}:"
            f"{start.isoformat()}:{end.isoformat()}"
        )
        prices = [
            {
                **row,
                "provenance_id": provenance_id,
                "ticker": manifest["ticker"],
                "security_id": manifest["security_id"],
                "provider_symbol": manifest["provider_symbol"],
                "source": manifest["provider"],
            }
            for row in fetched
        ]
        payload_sha256 = canonical_price_payload_hash(prices)
        provenance_rows.append(
            {
                **manifest,
                "provenance_id": provenance_id,
                "payload_sha256": payload_sha256,
                "review_policy": REVIEW_POLICY,
            }
        )
        all_prices.extend(prices)

    started_at = datetime.now()
    run_id = str(uuid4())
    try:
        counts = db.upsert_liquidation_price_extensions(
            provenance_rows,
            all_prices,
        )
        db.record_ingest(
            run_id=run_id,
            source=REVIEW_POLICY,
            table_name="security_ticker_extensions",
            rows_inserted=counts["prices"],
            started_at=started_at,
        )
        return counts
    except Exception as exc:
        db.record_ingest(
            run_id=run_id,
            source=REVIEW_POLICY,
            table_name="security_ticker_extensions",
            status="failed",
            error=str(exc),
            started_at=started_at,
        )
        raise


def _required(row: dict[str, str], field: str, row_number: int) -> str:
    value = row[field].strip()
    if not value:
        raise ValueError(f"liquidation extension row {row_number}: missing {field}")
    return value


def _date(row: dict[str, str], field: str, row_number: int) -> date:
    value = _required(row, field, row_number)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"liquidation extension row {row_number}: invalid {field}"
        ) from exc


def _https(row: dict[str, str], field: str, row_number: int) -> str:
    value = _required(row, field, row_number)
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(
            f"liquidation extension row {row_number}: {field} must be HTTPS"
        )
    return value

"""Reviewed identity-changing security events.

Ticker changes stay inside one ``security_id``. A stock-for-stock merger does
not: a held source security becomes a different target security at a sourced
ratio. This module imports only that narrow, auditable event type. Cash and
mixed-consideration deals remain unsupported until their tax/basis treatment
is modeled explicitly.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from math import isfinite
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from aios.ingest.security_identity import SECURITY_ID_PATTERN
from aios.storage.store import Store, get_store

SECURITY_CONVERSION_COLUMNS = {
    "source_security_id",
    "target_security_id",
    "effective_date",
    "known_date",
    "share_ratio",
    "basis_policy",
    "review_status",
    "verified_date",
    "source",
    "basis_source",
}


def load_security_conversion_csv(path: str | Path) -> list[dict]:
    """Load and strictly validate a reviewed share-conversion manifest."""
    csv_path = Path(path)
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = [str(field or "").strip().lower() for field in reader.fieldnames or []]
        if len(fields) != len(set(fields)):
            raise ValueError("security conversion CSV has duplicate columns")
        actual = set(fields)
        if actual != SECURITY_CONVERSION_COLUMNS:
            missing = SECURITY_CONVERSION_COLUMNS - actual
            unknown = actual - SECURITY_CONVERSION_COLUMNS
            details: list[str] = []
            if missing:
                details.append(f"missing {', '.join(sorted(missing))}")
            if unknown:
                details.append(f"unsupported {', '.join(sorted(unknown))}")
            raise ValueError(
                f"security conversion CSV columns invalid: {'; '.join(details)}"
            )

        output: list[dict] = []
        seen_sources: set[str] = set()
        for row_number, raw in enumerate(reader, start=2):
            if None in raw:
                raise ValueError(
                    f"security conversion row {row_number} has extra fields"
                )
            row = {
                str(key).strip().lower(): str(value or "").strip()
                for key, value in raw.items()
            }
            if not any(row.values()):
                continue
            source_security_id = _security_id(
                row["source_security_id"], "source_security_id", row_number
            )
            target_security_id = _security_id(
                row["target_security_id"], "target_security_id", row_number
            )
            if source_security_id == target_security_id:
                raise ValueError(
                    f"security conversion row {row_number}: source and target must differ"
                )
            if source_security_id in seen_sources:
                raise ValueError(
                    f"security conversion row {row_number}: duplicate source security"
                )
            seen_sources.add(source_security_id)
            effective_date = _date(row["effective_date"], "effective_date", row_number)
            known_date = _date(row["known_date"], "known_date", row_number)
            if known_date > effective_date:
                raise ValueError(
                    f"security conversion row {row_number}: known_date follows effective_date"
                )
            verified_date = _date(row["verified_date"], "verified_date", row_number)
            if verified_date > date.today():
                raise ValueError(
                    f"security conversion row {row_number}: verified_date is in the future"
                )
            try:
                share_ratio = float(row["share_ratio"])
            except ValueError as exc:
                raise ValueError(
                    f"security conversion row {row_number}: invalid share_ratio"
                ) from exc
            if not isfinite(share_ratio) or share_ratio <= 0:
                raise ValueError(
                    f"security conversion row {row_number}: share_ratio must be positive"
                )
            if row["basis_policy"] != "carryover":
                raise ValueError(
                    f"security conversion row {row_number}: unsupported basis_policy"
                )
            if row["review_status"] != "verified":
                raise ValueError(
                    f"security conversion row {row_number}: event must be verified"
                )
            source = _https_source(row["source"], row_number)
            basis_source = _https_source(row["basis_source"], row_number)
            output.append(
                {
                    "source_security_id": source_security_id,
                    "target_security_id": target_security_id,
                    "effective_date": effective_date,
                    "known_date": known_date,
                    "share_ratio": share_ratio,
                    "basis_policy": "carryover",
                    "review_status": "verified",
                    "verified_date": verified_date,
                    "source": source,
                    "basis_source": basis_source,
                }
            )
    if not output:
        raise ValueError("security conversion CSV has no data rows")
    return output


def ingest_security_conversion_csv(
    path: str | Path,
    *,
    store: Store | None = None,
) -> int:
    """Validate and atomically import a reviewed conversion manifest."""
    db = store or get_store()
    started_at = datetime.now()
    run_id = str(uuid4())
    csv_path = Path(path)
    source = f"csv:{csv_path.name}"
    try:
        count = db.upsert_security_conversions(load_security_conversion_csv(csv_path))
        db.record_ingest(
            run_id=run_id,
            source=source,
            table_name="security_conversions",
            rows_inserted=count,
            started_at=started_at,
        )
        return count
    except Exception as exc:
        db.record_ingest(
            run_id=run_id,
            source=source,
            table_name="security_conversions",
            status="failed",
            error=str(exc),
            started_at=started_at,
        )
        raise


def _security_id(value: str, field: str, row_number: int) -> str:
    normalized = value.strip()
    if not normalized or not SECURITY_ID_PATTERN.fullmatch(normalized):
        raise ValueError(f"security conversion row {row_number}: invalid {field}")
    return normalized


def _date(value: str, field: str, row_number: int) -> date:
    if not value:
        raise ValueError(f"security conversion row {row_number}: missing {field}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"security conversion row {row_number}: invalid {field}"
        ) from exc


def _https_source(value: str, row_number: int) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(
            f"security conversion row {row_number}: source must be an HTTPS URL"
        )
    return value

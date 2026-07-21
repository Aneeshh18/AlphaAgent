"""Reviewed, identity-safe price warm-up batches for market factors.

Provider history may predate this project's certified ticker intervals. We do
not backdate those market labels. Instead, each accepted snapshot is keyed by
``security_id`` and must match at least five sessions of the already stored,
reviewed provider series at its exact mapping boundary. Payload and overlap
hashes make the local batch reproducible and tamper-evident.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from aios.ingest.prices import fetch_provider_prices
from aios.storage.store import Store, get_store

REVIEW_POLICY = "identity_provider_overlap_v3"
COMPATIBLE_REVIEW_POLICIES = {
    "identity_provider_overlap_v1",  # v1 also required unstable Adj Close equality.
    # v2 also treated the Yahoo split scan date as an exact, daily identity field.
    "identity_provider_overlap_v2",
    REVIEW_POLICY,
}
CACHE_SCHEMA_VERSION = 1
DEFAULT_START = date(2022, 9, 1)
DEFAULT_OVERLAP_DAYS = 21
DEFAULT_MINIMUM_OVERLAP_SESSIONS = 5
DEFAULT_MINIMUM_WARMUP_SESSIONS = 210

REVIEW_FIELDS = (
    "universe_id",
    "canonical_ticker",
    "security_id",
    "provider",
    "provider_symbol",
    "data_start",
    "provider_anchor",
    "overlap_end",
    "warmup_rows",
    "overlap_rows",
    "first_date",
    "last_date",
    "payload_sha256",
    "overlap_sha256",
    "provenance_id",
    "review_status",
    "reason",
    "verified_date",
    "source",
    "cache_file",
    "cache_reused",
)

PROVENANCE_FIELDS = (
    "provenance_id",
    "universe_id",
    "security_id",
    "provider",
    "provider_symbol",
    "data_start",
    "data_end",
    "overlap_start",
    "overlap_end",
    "verified_date",
    "source",
    "payload_sha256",
    "overlap_sha256",
    "review_policy",
)

PRICE_HASH_FIELDS = (
    "security_id",
    "date",
    "provider",
    "provider_symbol",
    "close",
    "adj_close",
    "dividends",
    "split_ratio",
    "actions_complete",
    "close_split_adjusted",
    "split_normalization_factor",
    "split_normalization_through",
)


def build_factor_price_warmup(
    output_dir: str | Path,
    *,
    universe_id: str = "sp500",
    start: date | str = DEFAULT_START,
    as_of: date | str | None = None,
    security_ids: Iterable[str] | None = None,
    only_missing: bool = False,
    overlap_days: int = DEFAULT_OVERLAP_DAYS,
    minimum_overlap_sessions: int = DEFAULT_MINIMUM_OVERLAP_SESSIONS,
    minimum_warmup_sessions: int = DEFAULT_MINIMUM_WARMUP_SESSIONS,
    refresh: bool = False,
    store: Store | None = None,
    fetcher: Callable[[str, str, str, str], list[dict]] = fetch_provider_prices,
    checked_on: date | None = None,
    progress: Callable[[int, int, dict[str, Any]], None] | None = None,
    rejections_reviewed: bool = False,
) -> dict[str, Any]:
    """Build a resumable batch of accepted snapshots and a complete review CSV."""
    if overlap_days < 7:
        raise ValueError("overlap_days must be at least 7")
    if minimum_overlap_sessions < 2:
        raise ValueError("minimum_overlap_sessions must be at least 2")
    if minimum_warmup_sessions < 2:
        raise ValueError("minimum_warmup_sessions must be at least 2")
    normalized_start = _as_date(start, "warm-up start")
    checked_on = checked_on or date.today()
    selection_date = _as_date(as_of, "as_of") if as_of is not None else None
    if selection_date is not None and selection_date > checked_on:
        raise ValueError("warm-up as_of cannot be after the review date")
    if only_missing and selection_date is None:
        raise ValueError("only_missing requires an as_of date")
    requested_security_ids = {
        str(security_id).strip()
        for security_id in security_ids or ()
        if str(security_id).strip()
    }
    db = store or get_store()
    root = Path(output_dir)
    snapshot_dir = root / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    candidates = _candidate_mappings(
        db,
        universe_id,
        as_of=selection_date,
        security_ids=requested_security_ids or None,
    )
    skipped_complete = 0
    if only_missing and selection_date is not None:
        from aios.factors.market_factors import compute_market_factors_raw

        incomplete: list[dict[str, Any]] = []
        for candidate in candidates:
            snapshot = compute_market_factors_raw(
                str(candidate["canonical_ticker"]), selection_date, db
            )
            if snapshot.momentum_12_1 is None or snapshot.annualized_volatility is None:
                incomplete.append(candidate)
            else:
                skipped_complete += 1
        candidates = incomplete
    review_rows: list[dict[str, Any]] = []
    provenance_rows: list[dict[str, Any]] = []
    accepted = 0
    rejected = 0
    reused = 0

    for index, candidate in enumerate(candidates, start=1):
        if progress is not None:
            progress(index, len(candidates), candidate)
        anchor = _as_date(candidate["data_start"], "provider anchor")
        mapping_end = (
            _as_date(candidate["data_end"], "provider end")
            if candidate.get("data_end")
            else None
        )
        overlap_end = anchor + timedelta(days=overlap_days)
        if mapping_end is not None:
            overlap_end = min(overlap_end, mapping_end)
        review = _blank_review(
            candidate,
            universe_id=universe_id,
            start=normalized_start,
            anchor=anchor,
            overlap_end=overlap_end,
            checked_on=checked_on,
        )
        cache_path = snapshot_dir / _snapshot_name(candidate)
        review["cache_file"] = str(cache_path.relative_to(root))

        try:
            cache_reused = False
            if anchor <= normalized_start:
                raise ValueError("verified provider mapping begins before warm-up start")
            if overlap_end <= anchor:
                raise ValueError("verified provider mapping has no overlap review window")
            if _has_blocking_provider_history(
                db,
                candidate["security_id"],
                candidate["provider"],
                normalized_start,
                anchor,
            ):
                raise ValueError(
                    "earlier provider history is blocked or unavailable for this security"
                )

            envelope: dict[str, Any] | None = None
            if cache_path.exists() and not refresh:
                cached = _read_snapshot(cache_path)
                _validate_snapshot_envelope(
                    cached,
                    candidate=candidate,
                    universe_id=universe_id,
                    start=normalized_start,
                    anchor=anchor,
                    overlap_end=overlap_end,
                )
                _validate_cached_overlap(
                    db,
                    cached,
                    candidate=candidate,
                    anchor=anchor,
                    overlap_end=overlap_end,
                    minimum_overlap_sessions=minimum_overlap_sessions,
                )
                envelope = cached
                cache_reused = True
                reused += 1

            if envelope is None:
                fetched = fetcher(
                    str(candidate["provider"]),
                    str(candidate["provider_symbol"]),
                    normalized_start.isoformat(),
                    overlap_end.isoformat(),
                )
                envelope = _review_fetched_rows(
                    db,
                    candidate,
                    fetched,
                    universe_id=universe_id,
                    start=normalized_start,
                    anchor=anchor,
                    overlap_end=overlap_end,
                    minimum_overlap_sessions=minimum_overlap_sessions,
                    minimum_warmup_sessions=minimum_warmup_sessions,
                    checked_on=checked_on,
                    cache_file=review["cache_file"],
                )
                _write_snapshot(cache_path, envelope)

            accepted_review = dict(envelope["review"])
            accepted_review["cache_reused"] = cache_reused
            review_rows.append(accepted_review)
            provenance_rows.append(dict(envelope["provenance"]))
            accepted += 1
        except Exception as exc:
            review["reason"] = str(exc)
            review_rows.append(review)
            rejected += 1

    review_path = root / "factor_price_warmup_review.csv"
    provenance_path = root / "factor_price_warmup_provenance.csv"
    _write_csv(review_path, REVIEW_FIELDS, review_rows)
    _write_csv(provenance_path, PROVENANCE_FIELDS, provenance_rows)
    accepted_cache_files = sorted(
        str(row["cache_file"])
        for row in review_rows
        if row["review_status"] == "accepted"
    )
    manifest_path = root / "factor_price_warmup_manifest.json"
    _write_json(
        manifest_path,
        {
            "schema_version": CACHE_SCHEMA_VERSION,
            "universe_id": universe_id,
            "data_start": normalized_start.isoformat(),
            "selection_as_of": selection_date.isoformat() if selection_date else None,
            "only_missing": bool(only_missing),
            "requested_security_ids": sorted(requested_security_ids),
            "review_policy": REVIEW_POLICY,
            "accepted": accepted,
            "rejected": rejected,
            "rejections_reviewed": bool(rejections_reviewed or rejected == 0),
            "review_sha256": _file_hash(review_path),
            "provenance_sha256": _file_hash(provenance_path),
            "accepted_cache_files": accepted_cache_files,
            "generated_on": checked_on.isoformat(),
        },
    )
    return {
        "candidates": len(candidates),
        "skipped_complete": skipped_complete,
        "accepted": accepted,
        "rejected": rejected,
        "reused": reused,
        "review_path": review_path,
        "provenance_path": provenance_path,
        "manifest_path": manifest_path,
        "snapshot_dir": snapshot_dir,
        "review_rows": review_rows,
    }


def ingest_factor_price_warmup(
    batch_dir: str | Path,
    *,
    store: Store | None = None,
) -> dict[str, int]:
    """Validate every accepted cache and atomically import the complete batch."""
    db = store or get_store()
    root = Path(batch_dir)
    manifest_path = root / "factor_price_warmup_manifest.json"
    review_path = root / "factor_price_warmup_review.csv"
    provenance_path = root / "factor_price_warmup_provenance.csv"
    if not manifest_path.exists():
        raise ValueError("factor-price warm-up batch lacks its review manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != CACHE_SCHEMA_VERSION:
        raise ValueError("unsupported factor-price warm-up manifest")
    if manifest.get("rejected") and manifest.get("rejections_reviewed") is not True:
        raise ValueError("factor-price warm-up rejections have not been explicitly reviewed")
    if _file_hash(review_path) != manifest.get("review_sha256"):
        raise ValueError("factor-price warm-up review CSV hash mismatch")
    if _file_hash(provenance_path) != manifest.get("provenance_sha256"):
        raise ValueError("factor-price warm-up provenance CSV hash mismatch")

    expected_relative = manifest.get("accepted_cache_files")
    if not isinstance(expected_relative, list) or not all(
        isinstance(value, str) for value in expected_relative
    ):
        raise ValueError("factor-price warm-up manifest has an invalid snapshot list")
    snapshot_root = (root / "snapshots").resolve()
    snapshot_paths = [root / value for value in expected_relative]
    for path in snapshot_paths:
        try:
            path.resolve().relative_to(snapshot_root)
        except ValueError as exc:
            raise ValueError("warm-up manifest snapshot escapes the batch directory") from exc
        if not path.name.endswith(".json.gz"):
            raise ValueError("warm-up manifest contains a non-snapshot file")
    actual_paths = sorted((root / "snapshots").glob("*.json.gz"))
    if {path.resolve() for path in snapshot_paths} != {path.resolve() for path in actual_paths}:
        raise ValueError("factor-price warm-up snapshot set disagrees with the review manifest")
    if not snapshot_paths:
        raise ValueError("factor-price warm-up batch has no accepted snapshots")

    provenance_rows: list[dict] = []
    price_rows: list[dict] = []
    seen_security_ids: set[str] = set()
    for path in snapshot_paths:
        envelope = _read_snapshot(path)
        _validate_snapshot_hashes(envelope)
        provenance = dict(envelope["provenance"])
        security_id = str(provenance["security_id"])
        if security_id in seen_security_ids:
            raise ValueError(f"duplicate warm-up snapshot for {security_id}")
        seen_security_ids.add(security_id)
        provenance_rows.append(provenance)
        price_rows.extend(dict(row) for row in envelope["rows"])

    started_at = datetime.now()
    run_id = str(uuid4())
    try:
        counts = db.upsert_factor_price_warmup(provenance_rows, price_rows)
        db.record_ingest(
            run_id=run_id,
            source=REVIEW_POLICY,
            table_name="factor_prices",
            rows_inserted=counts["factor_prices"],
            started_at=started_at,
            status="success",
        )
        return counts | {"snapshots": len(provenance_rows)}
    except Exception as exc:
        db.record_ingest(
            run_id=run_id,
            source=REVIEW_POLICY,
            table_name="factor_prices",
            started_at=started_at,
            status="failed",
            error=str(exc),
        )
        raise


def mark_factor_price_warmup_rejections_reviewed(
    batch_dir: str | Path,
    *,
    reviewed_on: date | None = None,
) -> int:
    """Approve preserved rejection evidence without repeating provider calls.

    The first build intentionally exits non-zero when exclusions exist. Manual
    review must not require a second network fetch, because a transient empty
    response could overwrite the more informative original rejection.
    """
    root = Path(batch_dir)
    manifest_path = root / "factor_price_warmup_manifest.json"
    review_path = root / "factor_price_warmup_review.csv"
    provenance_path = root / "factor_price_warmup_provenance.csv"
    if not manifest_path.exists():
        raise ValueError("factor-price warm-up batch lacks its review manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != CACHE_SCHEMA_VERSION:
        raise ValueError("unsupported factor-price warm-up manifest")
    if _file_hash(review_path) != manifest.get("review_sha256"):
        raise ValueError("factor-price warm-up review CSV hash mismatch")
    if _file_hash(provenance_path) != manifest.get("provenance_sha256"):
        raise ValueError("factor-price warm-up provenance CSV hash mismatch")

    with review_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != REVIEW_FIELDS:
            raise ValueError("factor-price warm-up review has an invalid header")
        rejected = [row for row in reader if row.get("review_status") == "rejected"]
    if len(rejected) != int(manifest.get("rejected", -1)):
        raise ValueError("factor-price warm-up rejection count disagrees with its manifest")
    if not rejected:
        raise ValueError("factor-price warm-up batch has no rejections to review")
    if any(not str(row.get("reason") or "").strip() for row in rejected):
        raise ValueError("every factor-price rejection requires a preserved reason")

    manifest["rejections_reviewed"] = True
    manifest["rejections_reviewed_on"] = (reviewed_on or date.today()).isoformat()
    _write_json(manifest_path, manifest)
    return len(rejected)


def _candidate_mappings(
    store: Store,
    universe_id: str,
    *,
    as_of: date | None = None,
    security_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    if as_of is None:
        rows = store.query(
            """
            SELECT mapping.provider, mapping.provider_symbol, mapping.security_id,
                   mapping.data_start, mapping.data_end, mapping.source,
                   security.canonical_ticker
            FROM provider_symbol_history AS mapping
            JOIN security_master AS security USING (security_id)
            WHERE mapping.mapping_status = 'verified'
              AND EXISTS (
                  SELECT 1 FROM universe_membership AS membership
                  WHERE membership.universe_id = ?
                    AND membership.security_id = mapping.security_id
              )
            ORDER BY mapping.security_id, mapping.data_start, mapping.provider
            """,
            (universe_id,),
        )
    else:
        rows = store.query(
            """
            SELECT mapping.provider, mapping.provider_symbol, mapping.security_id,
                   mapping.data_start, mapping.data_end, mapping.source,
                   membership.ticker AS canonical_ticker
            FROM universe_membership AS membership
            JOIN provider_symbol_history AS mapping
              ON mapping.security_id = membership.security_id
             AND mapping.mapping_status = 'verified'
             AND mapping.data_start <= CAST(? AS DATE)
             AND (mapping.data_end IS NULL OR mapping.data_end > CAST(? AS DATE))
            WHERE membership.universe_id = ?
              AND membership.known_date <= CAST(? AS DATE)
              AND membership.effective_start <= CAST(? AS DATE)
              AND (
                  membership.effective_end IS NULL
                  OR membership.effective_end > CAST(? AS DATE)
                  OR membership.end_known_date > CAST(? AS DATE)
              )
            ORDER BY mapping.security_id, mapping.data_start, mapping.provider
            """,
            (as_of, as_of, universe_id, as_of, as_of, as_of, as_of),
        )
    if security_ids is not None:
        rows = [row for row in rows if str(row["security_id"]) in security_ids]
        found = {str(row["security_id"]) for row in rows}
        missing = sorted(security_ids - found)
        if missing:
            raise ValueError(
                "requested security has no unique eligible provider mapping: " + missing[0]
            )
    by_security: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_security.setdefault(str(row["security_id"]), []).append(row)
    duplicates = [
        security_id for security_id, mappings in by_security.items() if len(mappings) != 1
    ]
    if duplicates:
        raise ValueError(
            "warm-up builder requires exactly one verified anchor per security; "
            f"first ambiguous security is {duplicates[0]}"
        )
    return [mappings[0] for mappings in by_security.values()]


def _has_blocking_provider_history(
    store: Store,
    security_id: str,
    provider: str,
    start: date,
    end: date,
) -> bool:
    return bool(
        store.query(
            """
            SELECT 1
            FROM provider_symbol_history
            WHERE security_id = ?
              AND provider = ?
              AND mapping_status <> 'verified'
              AND data_start < CAST(? AS DATE)
              AND (data_end IS NULL OR data_end > CAST(? AS DATE))
            LIMIT 1
            """,
            (security_id, provider, end.isoformat(), start.isoformat()),
        )
    )


def _review_fetched_rows(
    store: Store,
    candidate: dict[str, Any],
    fetched: list[dict],
    *,
    universe_id: str,
    start: date,
    anchor: date,
    overlap_end: date,
    minimum_overlap_sessions: int,
    minimum_warmup_sessions: int,
    checked_on: date,
    cache_file: str,
) -> dict[str, Any]:
    provider = str(candidate["provider"]).lower()
    provider_symbol = str(candidate["provider_symbol"]).upper()
    security_id = str(candidate["security_id"])
    normalized = [
        _normalize_fetched_row(row, security_id, provider, provider_symbol)
        for row in fetched
    ]
    normalized.sort(key=lambda row: row["date"])
    dates = [row["date"] for row in normalized]
    if len(dates) != len(set(dates)):
        raise ValueError("provider returned duplicate dates")

    warmup_rows = [
        row
        for row in normalized
        if start <= _as_date(row["date"], "price date") < anchor
    ]
    overlap_rows = [
        row for row in normalized if anchor <= _as_date(row["date"], "price date") < overlap_end
    ]
    if len(warmup_rows) < minimum_warmup_sessions:
        raise ValueError(
            f"insufficient warm-up sessions: {len(warmup_rows)} < {minimum_warmup_sessions}"
        )
    first_date = _as_date(warmup_rows[0]["date"], "first warm-up date")
    last_date = _as_date(warmup_rows[-1]["date"], "last warm-up date")
    if first_date > start + timedelta(days=7):
        raise ValueError(f"warm-up history begins too late: {first_date}")
    if last_date < anchor - timedelta(days=7):
        raise ValueError(f"warm-up history ends too early: {last_date}")

    stored_rows = store.query(
        """
        SELECT date, close, adj_close, dividends, split_ratio, actions_complete,
               close_split_adjusted, split_normalization_factor,
               split_normalization_through
        FROM prices
        WHERE security_id = ?
          AND source = ?
          AND provider_symbol = ?
          AND date >= CAST(? AS DATE)
          AND date < CAST(? AS DATE)
        ORDER BY date
        """,
        (
            security_id,
            provider,
            provider_symbol,
            anchor.isoformat(),
            overlap_end.isoformat(),
        ),
    )
    fetched_by_date = {row["date"]: row for row in overlap_rows}
    stored_by_date = {str(row["date"]): row for row in stored_rows}
    common_dates = sorted(set(fetched_by_date) & set(stored_by_date))
    if len(common_dates) < minimum_overlap_sessions:
        raise ValueError(
            f"insufficient reviewed overlap: {len(common_dates)} < {minimum_overlap_sessions}"
        )

    overlap_evidence: list[dict[str, Any]] = []
    for row_date in common_dates:
        fetched_row = fetched_by_date[row_date]
        stored_row = stored_by_date[row_date]
        _assert_overlap_match(row_date, fetched_row, stored_row)
        overlap_evidence.append(
            {
                "date": row_date,
                "close": fetched_row["close"],
                "adj_close": fetched_row["adj_close"],
                "dividends": fetched_row["dividends"],
                "split_ratio": fetched_row["split_ratio"],
                "actions_complete": fetched_row["actions_complete"],
                "close_split_adjusted": fetched_row["close_split_adjusted"],
                "split_normalization_factor": fetched_row[
                    "split_normalization_factor"
                ],
                "split_normalization_through": fetched_row[
                    "split_normalization_through"
                ],
            }
        )

    payload_sha256 = _payload_hash(_canonical_price_payload(warmup_rows))
    overlap_sha256 = _payload_hash(overlap_evidence)
    provenance_seed = {
        "universe_id": universe_id,
        "security_id": security_id,
        "provider": provider,
        "provider_symbol": provider_symbol,
        "data_start": start.isoformat(),
        "data_end": anchor.isoformat(),
        "overlap_end": overlap_end.isoformat(),
        "payload_sha256": payload_sha256,
        "overlap_sha256": overlap_sha256,
        "review_policy": REVIEW_POLICY,
    }
    provenance_id = f"fpw:{_payload_hash(provenance_seed)}"
    for row in warmup_rows:
        row["provenance_id"] = provenance_id

    source = _provider_source(provider, provider_symbol)
    provenance = {
        "provenance_id": provenance_id,
        "universe_id": universe_id,
        "security_id": security_id,
        "provider": provider,
        "provider_symbol": provider_symbol,
        "data_start": start.isoformat(),
        "data_end": anchor.isoformat(),
        "overlap_start": anchor.isoformat(),
        "overlap_end": overlap_end.isoformat(),
        "verified_date": checked_on.isoformat(),
        "source": source,
        "payload_sha256": payload_sha256,
        "overlap_sha256": overlap_sha256,
        "review_policy": REVIEW_POLICY,
    }
    review = {
        "universe_id": universe_id,
        "canonical_ticker": candidate["canonical_ticker"],
        "security_id": security_id,
        "provider": provider,
        "provider_symbol": provider_symbol,
        "data_start": start.isoformat(),
        "provider_anchor": anchor.isoformat(),
        "overlap_end": overlap_end.isoformat(),
        "warmup_rows": len(warmup_rows),
        "overlap_rows": len(common_dates),
        "first_date": first_date.isoformat(),
        "last_date": last_date.isoformat(),
        "payload_sha256": payload_sha256,
        "overlap_sha256": overlap_sha256,
        "provenance_id": provenance_id,
        "review_status": "accepted",
        "reason": "",
        "verified_date": checked_on.isoformat(),
        "source": source,
        "cache_file": cache_file,
        "cache_reused": False,
    }
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "written_at": checked_on.isoformat(),
        "review": review,
        "provenance": provenance,
        "overlap_evidence": overlap_evidence,
        "rows": warmup_rows,
    }


def _normalize_fetched_row(
    row: dict,
    security_id: str,
    provider: str,
    provider_symbol: str,
) -> dict[str, Any]:
    row_date = _as_date(row.get("date"), "provider price date")
    close = _positive_number(row.get("close"), "close")
    dividends = _non_negative_number(row.get("dividends", 0), "dividends")
    split_ratio = _positive_number(row.get("split_ratio", 1), "split ratio")
    factor = _positive_number(
        row.get("split_normalization_factor"), "split normalization factor"
    )
    if row.get("actions_complete") is not True:
        raise ValueError(f"corporate actions are incomplete on {row_date}")
    if row.get("close_split_adjusted") not in {True, False}:
        raise ValueError(f"split-adjustment basis is unknown on {row_date}")
    through = row.get("split_normalization_through")
    return {
        "security_id": security_id,
        "date": row_date.isoformat(),
        "provider": provider,
        "provider_symbol": provider_symbol,
        "close": close,
        "adj_close": _optional_number(row.get("adj_close"), "adjusted close"),
        "dividends": dividends,
        "split_ratio": split_ratio,
        "actions_complete": True,
        "close_split_adjusted": row["close_split_adjusted"],
        "split_normalization_factor": factor,
        "split_normalization_through": (
            _as_date(through, "split normalization through").isoformat() if through else None
        ),
    }


def _assert_overlap_match(row_date: str, fetched: dict, stored: dict) -> None:
    # Adj Close is deliberately excluded: it is retrospectively recomputed for
    # later dividends and is neither an identity anchor nor an input to QVML.
    # Raw close plus explicit actions and split basis are the stable return path.
    for field in ("close", "dividends", "split_ratio", "split_normalization_factor"):
        if not _numbers_match(fetched.get(field), stored.get(field)):
            raise ValueError(f"reviewed overlap mismatch for {field} on {row_date}")
    for field in ("actions_complete", "close_split_adjusted"):
        if fetched.get(field) is not stored.get(field):
            raise ValueError(f"reviewed overlap mismatch for {field} on {row_date}")
    # Yahoo's scan-through date advances every day even when no price or split
    # basis changes. It is provenance, not an identity field. The economic
    # basis above must still agree, and each scan must cover the compared row.
    for label, row in (("fetched", fetched), ("stored", stored)):
        if row.get("close_split_adjusted") is not True:
            continue
        through = row.get("split_normalization_through")
        if through is None or _as_date(
            through, f"{label} split normalization through"
        ) < _as_date(row_date, "overlap date"):
            raise ValueError(
                f"{label} split-normalization scan does not cover {row_date}"
            )


def _validate_cached_overlap(
    store: Store,
    envelope: dict[str, Any],
    *,
    candidate: dict[str, Any],
    anchor: date,
    overlap_end: date,
    minimum_overlap_sessions: int,
) -> None:
    """Re-anchor a cached snapshot to the current reviewed price rows."""
    evidence = envelope["overlap_evidence"]
    evidence_by_date: dict[str, dict[str, Any]] = {}
    for row in evidence:
        row_date = _as_date(row.get("date"), "cached overlap date")
        if not anchor <= row_date < overlap_end:
            raise ValueError("cached warm-up snapshot has overlap outside review window")
        key = row_date.isoformat()
        if key in evidence_by_date:
            raise ValueError("cached warm-up snapshot has duplicate overlap dates")
        evidence_by_date[key] = row

    stored_rows = store.query(
        """
        SELECT date, close, adj_close, dividends, split_ratio, actions_complete,
               close_split_adjusted, split_normalization_factor,
               split_normalization_through
        FROM prices
        WHERE security_id = ?
          AND source = ?
          AND provider_symbol = ?
          AND date >= CAST(? AS DATE)
          AND date < CAST(? AS DATE)
        ORDER BY date
        """,
        (
            str(candidate["security_id"]),
            str(candidate["provider"]).lower(),
            str(candidate["provider_symbol"]).upper(),
            anchor.isoformat(),
            overlap_end.isoformat(),
        ),
    )
    stored_by_date = {str(row["date"]): row for row in stored_rows}
    common_dates = sorted(set(evidence_by_date) & set(stored_by_date))
    if len(common_dates) < minimum_overlap_sessions:
        raise ValueError(
            "cached warm-up snapshot has insufficient current reviewed overlap: "
            f"{len(common_dates)} < {minimum_overlap_sessions}"
        )
    for row_date in common_dates:
        _assert_overlap_match(
            row_date,
            evidence_by_date[row_date],
            stored_by_date[row_date],
        )


def _validate_snapshot_envelope(
    envelope: dict[str, Any],
    *,
    candidate: dict[str, Any],
    universe_id: str,
    start: date,
    anchor: date,
    overlap_end: date,
) -> None:
    _validate_snapshot_hashes(envelope)
    provenance = envelope["provenance"]
    expected = {
        "universe_id": universe_id,
        "security_id": str(candidate["security_id"]),
        "provider": str(candidate["provider"]).lower(),
        "provider_symbol": str(candidate["provider_symbol"]).upper(),
        "data_start": start.isoformat(),
        "data_end": anchor.isoformat(),
        "overlap_start": anchor.isoformat(),
        "overlap_end": overlap_end.isoformat(),
    }
    for field, value in expected.items():
        if provenance.get(field) != value:
            raise ValueError(f"cached warm-up snapshot has stale {field}")
    if provenance.get("review_policy") not in COMPATIBLE_REVIEW_POLICIES:
        raise ValueError("cached warm-up snapshot has stale review_policy")
    if provenance["provider"] == "yfinance":
        through_dates = {
            row.get("split_normalization_through")
            for row in envelope["rows"] + envelope["overlap_evidence"]
        }
        if None in through_dates or len(through_dates) != 1:
            raise ValueError("cached Yahoo split-normalization scan is inconsistent")
        verified_date = _as_date(provenance.get("verified_date"), "verified date")
        through_date = _as_date(through_dates.pop(), "cached normalization date")
        if through_date < verified_date:
            raise ValueError("cached Yahoo split-normalization scan predates its review")


def _validate_snapshot_hashes(envelope: dict[str, Any]) -> None:
    if envelope.get("schema_version") != CACHE_SCHEMA_VERSION:
        raise ValueError("unsupported factor-price warm-up cache schema")
    if not isinstance(envelope.get("rows"), list) or not envelope["rows"]:
        raise ValueError("warm-up cache has no rows")
    if not isinstance(envelope.get("overlap_evidence"), list):
        raise ValueError("warm-up cache lacks overlap evidence")
    provenance = envelope.get("provenance")
    review = envelope.get("review")
    if not isinstance(provenance, dict) or not isinstance(review, dict):
        raise ValueError("warm-up cache lacks review provenance")
    payload_hash = _payload_hash(_canonical_price_payload(envelope["rows"]))
    overlap_hash = _payload_hash(envelope["overlap_evidence"])
    if payload_hash != provenance.get("payload_sha256"):
        raise ValueError("warm-up payload hash mismatch")
    if overlap_hash != provenance.get("overlap_sha256"):
        raise ValueError("warm-up overlap hash mismatch")
    if review.get("review_status") != "accepted":
        raise ValueError("warm-up cache was not accepted")
    if review.get("provenance_id") != provenance.get("provenance_id"):
        raise ValueError("warm-up cache provenance IDs disagree")
    for row in envelope["rows"]:
        if row.get("provenance_id") != provenance.get("provenance_id"):
            raise ValueError("warm-up row provenance ID mismatch")


def _canonical_price_payload(rows: list[dict]) -> list[dict[str, Any]]:
    return [
        {field: row.get(field) for field in PRICE_HASH_FIELDS}
        for row in sorted(rows, key=lambda item: str(item.get("date")))
    ]


def _blank_review(
    candidate: dict[str, Any],
    *,
    universe_id: str,
    start: date,
    anchor: date,
    overlap_end: date,
    checked_on: date,
) -> dict[str, Any]:
    return {field: "" for field in REVIEW_FIELDS} | {
        "universe_id": universe_id,
        "canonical_ticker": candidate["canonical_ticker"],
        "security_id": candidate["security_id"],
        "provider": str(candidate["provider"]).lower(),
        "provider_symbol": str(candidate["provider_symbol"]).upper(),
        "data_start": start.isoformat(),
        "provider_anchor": anchor.isoformat(),
        "overlap_end": overlap_end.isoformat(),
        "review_status": "rejected",
        "verified_date": checked_on.isoformat(),
        "source": _provider_source(
            str(candidate["provider"]).lower(), str(candidate["provider_symbol"])
        ),
        "cache_reused": False,
    }


def _provider_source(provider: str, symbol: str) -> str:
    if provider == "yfinance":
        return f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}"
    if provider == "tiingo":
        return f"https://api.tiingo.com/tiingo/daily/{quote(symbol)}"
    if provider == "stooq":
        return f"https://stooq.com/q/d/l/?s={quote(symbol.lower())}"
    raise ValueError(f"unsupported warm-up provider {provider!r}")


def _snapshot_name(candidate: dict[str, Any]) -> str:
    identity = "|".join(
        (
            str(candidate["security_id"]),
            str(candidate["provider"]),
            str(candidate["provider_symbol"]),
            str(candidate["data_start"]),
        )
    )
    return f"{hashlib.sha256(identity.encode()).hexdigest()[:24]}.json.gz"


def _read_snapshot(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"invalid warm-up cache {path}")
    return payload


def _write_snapshot(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
    temporary.replace(path)


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _payload_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _numbers_match(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    left_number = float(left)
    right_number = float(right)
    tolerance = max(1e-8, abs(right_number) * 1e-7)
    return abs(left_number - right_number) <= tolerance


def _positive_number(value: Any, field: str) -> float:
    number = _optional_number(value, field)
    if number is None or number <= 0:
        raise ValueError(f"{field} must be positive and finite")
    return number


def _non_negative_number(value: Any, field: str) -> float:
    number = _optional_number(value, field)
    if number is None or number < 0:
        raise ValueError(f"{field} must be non-negative and finite")
    return number


def _optional_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _as_date(value: date | str | Any, field: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc

"""DuckDB connection + storage operations.

A thin wrapper that:
  - opens a single-process connection to the local .duckdb file
  - initializes the point-in-time schema on first run
  - provides typed insert helpers that enforce the PIT invariant
  - exposes simple query helpers for the factor/test layers

This is the ONLY module allowed to open a DuckDB connection. Everything else
goes through these functions. Keeps connection management in one place.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import duckdb
from structlog import get_logger

from aios.config import settings
from aios.storage.schema import MACRO_TABLE_SQL, SCHEMA_SQL

if TYPE_CHECKING:
    import pandas as pd

log = get_logger(__name__)
MACRO_LEGACY_PURGED_MIGRATION = "macro_legacy_active_copies_purged"


class Store:
    """Thin wrapper around a DuckDB connection."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or _resolve(settings.duckdb_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(self.db_path))
        self._init_schema()

    def _init_schema(self) -> None:
        """Run all CREATE TABLE IF NOT EXISTS statements."""
        self._con.execute(SCHEMA_SQL)
        self._migrate_macro_schema()
        self._migrate_security_identity_schema()
        self._migrate_reference_identity_columns()
        log.info("schema.initialized", db=str(self.db_path))

    def _migrate_security_identity_schema(self) -> None:
        """Add the stable identity link to databases created before this layer."""
        columns = {row["column_name"] for row in self.query("DESCRIBE universe_membership")}
        if "security_id" not in columns:
            self.execute("ALTER TABLE universe_membership ADD COLUMN security_id VARCHAR")

    def _migrate_reference_identity_columns(self) -> None:
        """Add nullable issuer/provider links without rewriting legacy rows."""
        additions = {
            "prices": {
                "security_id": "VARCHAR",
                "provider_symbol": "VARCHAR",
            },
            "fundamentals": {
                "issuer_id": "VARCHAR",
                "security_id": "VARCHAR",
            },
        }
        for table, expected in additions.items():
            columns = {row["column_name"] for row in self.query(f"DESCRIBE {table}")}
            for column, data_type in expected.items():
                if column not in columns:
                    self.execute(f"ALTER TABLE {table} ADD COLUMN {column} {data_type}")

    def _migrate_macro_schema(self) -> None:
        """Upgrade the pre-vintage macro table without silently losing data.

        The old schema keyed macro rows only by ``(series_id, date)``. It is
        not safe for PIT analysis because revisions overwrite history. Existing
        rows are copied into a legacy marker source with a NULL release date;
        they remain available for inspection but are deliberately excluded by
        the PIT query helpers until a release-aware re-ingest replaces them.
        """
        tables = {
            row["table_name"]
            for row in self.query(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'main'
                  AND table_name IN ('macro', 'macro_legacy')
                """
            )
        }
        if "macro_legacy" in tables:
            columns = {row["column_name"] for row in self.query("DESCRIBE macro")}
            if "release_date" not in columns:
                raise RuntimeError(
                    "Macro migration is ambiguous: both old macro and macro_legacy exist. "
                    "Restore the database backup and retry the migration."
                )
            was_intentionally_purged = self.query(
                "SELECT COUNT(*) AS n FROM schema_migrations WHERE name = ?",
                (MACRO_LEGACY_PURGED_MIGRATION,),
            )[0]["n"]
            if was_intentionally_purged:
                return
            legacy_count = self.query("SELECT COUNT(*) AS n FROM macro_legacy")[0]["n"]
            copied_count = self.query(
                """
                SELECT COUNT(*) AS n
                FROM macro
                WHERE source = 'legacy_unversioned' AND release_date IS NULL
                """
            )[0]["n"]
            if copied_count == legacy_count:
                return
            # This is idempotent if a previous process stopped after the rename
            # or table creation but before copying the legacy rows.
            self.execute("DELETE FROM macro WHERE source = 'legacy_unversioned'")
            self.execute(
                """
                INSERT INTO macro
                (series_id, date, release_date, value, unit, source, fetched_at)
                SELECT series_id, date, NULL, value, unit, 'legacy_unversioned', fetched_at
                FROM macro_legacy
                """
            )
            return

        columns = {row["column_name"] for row in self.query("DESCRIBE macro")}
        if "release_date" in columns:
            return

        self.execute("ALTER TABLE macro RENAME TO macro_legacy")
        self.execute(MACRO_TABLE_SQL)
        self.execute(
            """
            INSERT INTO macro
            (series_id, date, release_date, value, unit, source, fetched_at)
            SELECT series_id, date, NULL, value, unit, 'legacy_unversioned', fetched_at
            FROM macro_legacy
            """
        )

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        return self._con

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------
    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> Any:
        if params is None:
            return self._con.execute(sql)
        return self._con.execute(sql, params)

    def query(self, sql: str, params: tuple[Any, ...] | None = None) -> list[dict]:
        """Run a SELECT and return rows as list of dicts."""
        cur = self.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Insert helpers (typed, idempotent, PIT-aware)
    # ------------------------------------------------------------------
    def upsert_securities(self, rows: list[dict]) -> int:
        """Insert/update securities. Returns number of rows affected."""
        if not rows:
            return 0
        self._con.register("_tmp_sec", _rows_to_arrowable(rows))
        n = self._con.execute(
            """
            INSERT OR REPLACE INTO securities
            (ticker, cik, name, exchange, sector, industry, market_cap_bucket,
             sic_code, is_active, first_seen, last_updated)
            SELECT
                ticker, cik, name, exchange, sector, industry, market_cap_bucket,
                sic_code, TRUE, now(), now() FROM _tmp_sec
            """.strip()
        ).fetchone()[0]
        self._con.unregister("_tmp_sec")
        return int(n)

    def upsert_universe_membership(self, rows: list[dict]) -> int:
        """Insert point-in-time universe intervals.

        Intervals are half-open: ``[effective_start, effective_end)``. The
        membership's ``known_date`` must be on or before its effective start;
        this lets a future announced change exist in storage without making it
        visible before the event becomes effective.
        """
        if not rows:
            return 0
        normalized: list[dict] = []
        for row in rows:
            universe_id = str(row.get("universe_id") or "").strip()
            ticker = str(row.get("ticker") or "").strip().upper()
            source = str(row.get("source") or "").strip()
            if not universe_id or not ticker or not source:
                raise ValueError("universe membership requires universe_id, ticker, and source")
            if not row.get("effective_start") or not row.get("known_date"):
                raise ValueError("universe membership requires effective_start and known_date")
            effective_start = _as_date(row["effective_start"])
            known_date = _as_date(row["known_date"])
            effective_end = _as_date(row["effective_end"]) if row.get("effective_end") else None
            if known_date > effective_start:
                raise ValueError("universe membership known_date cannot follow effective_start")
            if effective_end is not None and effective_end <= effective_start:
                raise ValueError("universe membership effective_end must follow its start")
            normalized.append(
                {
                    "universe_id": universe_id,
                    "ticker": ticker,
                    "security_id": (
                        str(row["security_id"]).strip() if row.get("security_id") else None
                    ),
                    "effective_start": effective_start,
                    "effective_end": effective_end,
                    "known_date": known_date,
                    "source": source,
                }
            )

        self._con.register("_tmp_universe", _rows_to_arrowable(normalized))
        self._con.execute("BEGIN TRANSACTION")
        try:
            n = self._con.execute(
                """
                INSERT INTO universe_membership
                (universe_id, ticker, security_id, effective_start, effective_end,
                 known_date, source, fetched_at)
                SELECT universe_id, ticker, security_id, CAST(effective_start AS DATE),
                       CAST(effective_end AS DATE), CAST(known_date AS DATE), source, now()
                FROM _tmp_universe
                ON CONFLICT (universe_id, ticker, effective_start) DO UPDATE
                SET security_id = COALESCE(
                        EXCLUDED.security_id, universe_membership.security_id
                    ),
                    effective_end = EXCLUDED.effective_end,
                    known_date = EXCLUDED.known_date,
                    source = EXCLUDED.source,
                    fetched_at = EXCLUDED.fetched_at
                """
            ).fetchone()[0]
            overlap = self.query(
                """
                WITH ordered AS (
                    SELECT universe_id, ticker, effective_start, effective_end,
                           ROW_NUMBER() OVER (
                               PARTITION BY universe_id, ticker ORDER BY effective_start
                           ) AS interval_number,
                           LAG(effective_end) OVER (
                               PARTITION BY universe_id, ticker ORDER BY effective_start
                           ) AS previous_end
                    FROM universe_membership
                )
                SELECT universe_id, ticker, effective_start, previous_end
                FROM ordered
                WHERE interval_number > 1
                  AND (previous_end IS NULL OR effective_start < previous_end)
                LIMIT 1
                """
            )
            if overlap:
                sample = overlap[0]
                raise ValueError(
                    "overlapping universe membership intervals for "
                    f"{sample['universe_id']}:{sample['ticker']}"
                )
            self._con.execute("COMMIT")
            return int(n)
        except Exception:
            self._con.execute("ROLLBACK")
            raise
        finally:
            self._con.unregister("_tmp_universe")

    def upsert_security_identities(self, rows: list[dict]) -> int:
        """Link certified universe intervals to immutable internal security IDs.

        The import is all-or-nothing. Every assignment must match one existing
        membership interval exactly, and one security cannot have overlapping
        tickers inside the same universe. This prevents an alias correction
        from silently stitching unrelated price or fundamental histories.
        """
        if not rows:
            return 0
        allowed_statuses = {
            "bounded_ticker",
            "verified_ticker_change",
            "verified_surviving_security_ticker_change",
        }
        normalized: list[dict] = []
        keys: set[tuple[str, str, date]] = set()
        for row in rows:
            universe_id = str(row.get("universe_id") or "").strip()
            ticker = str(row.get("ticker") or "").strip().upper()
            security_id = str(row.get("security_id") or "").strip()
            status = str(row.get("identity_status") or "").strip()
            source = str(row.get("source") or "").strip()
            if not universe_id or not ticker or not security_id or not source:
                raise ValueError(
                    "security identity requires universe_id, ticker, security_id, and source"
                )
            if status not in allowed_statuses:
                raise ValueError(f"unsupported security identity status {status!r}")
            if not row.get("effective_start") or not row.get("known_date"):
                raise ValueError("security identity requires effective_start and known_date")
            effective_start = _as_date(row["effective_start"])
            effective_end = _as_date(row["effective_end"]) if row.get("effective_end") else None
            known_date = _as_date(row["known_date"])
            if known_date > effective_start:
                raise ValueError("security identity known_date cannot follow effective_start")
            if effective_end is not None and effective_end <= effective_start:
                raise ValueError("security identity effective_end must follow its start")
            key = (universe_id, ticker, effective_start)
            if key in keys:
                raise ValueError(
                    "duplicate security identity assignment for "
                    f"{universe_id}:{ticker}@{effective_start}"
                )
            keys.add(key)
            normalized.append(
                {
                    "universe_id": universe_id,
                    "ticker": ticker,
                    "effective_start": effective_start,
                    "effective_end": effective_end,
                    "security_id": security_id,
                    "known_date": known_date,
                    "identity_status": status,
                    "source": source,
                }
            )

        masters: list[dict] = []
        by_security: dict[str, list[dict]] = {}
        for row in normalized:
            by_security.setdefault(row["security_id"], []).append(row)
        for security_id, assignments in by_security.items():
            statuses = {row["identity_status"] for row in assignments}
            if len(statuses) != 1:
                raise ValueError(f"security identity {security_id!r} has inconsistent statuses")
            canonical = max(
                assignments,
                key=lambda row: (row["effective_start"], row["ticker"]),
            )
            masters.append(
                {
                    "security_id": security_id,
                    "canonical_ticker": canonical["ticker"],
                    "security_type": "common_stock",
                    "identity_status": canonical["identity_status"],
                    "source": canonical["source"],
                }
            )

        self._con.register("_tmp_identity", _rows_to_arrowable(normalized))
        self._con.register("_tmp_security_master", _rows_to_arrowable(masters))
        self._con.execute("BEGIN TRANSACTION")
        try:
            missing = self.query(
                """
                SELECT identity.universe_id, identity.ticker,
                       identity.effective_start
                FROM _tmp_identity AS identity
                LEFT JOIN universe_membership AS membership
                  ON membership.universe_id = identity.universe_id
                 AND membership.ticker = identity.ticker
                 AND membership.effective_start = CAST(identity.effective_start AS DATE)
                 AND membership.effective_end IS NOT DISTINCT FROM
                     CAST(identity.effective_end AS DATE)
                WHERE membership.universe_id IS NULL
                LIMIT 1
                """
            )
            if missing:
                sample = missing[0]
                raise ValueError(
                    "security identity does not exactly match membership interval "
                    f"{sample['universe_id']}:{sample['ticker']}@"
                    f"{sample['effective_start']}"
                )

            conflicts = self.query(
                """
                SELECT membership.universe_id, membership.ticker,
                       membership.effective_start, membership.security_id AS existing,
                       identity.security_id AS incoming
                FROM universe_membership AS membership
                JOIN _tmp_identity AS identity
                  ON membership.universe_id = identity.universe_id
                 AND membership.ticker = identity.ticker
                 AND membership.effective_start = CAST(identity.effective_start AS DATE)
                WHERE membership.security_id IS NOT NULL
                  AND membership.security_id <> identity.security_id
                LIMIT 1
                """
            )
            if conflicts:
                sample = conflicts[0]
                raise ValueError(
                    "security identity conflicts with existing mapping for "
                    f"{sample['universe_id']}:{sample['ticker']}@"
                    f"{sample['effective_start']}"
                )

            self.execute(
                """
                INSERT INTO security_master
                (security_id, canonical_ticker, security_type, identity_status,
                 source, created_at, last_updated)
                SELECT security_id, canonical_ticker, security_type,
                       identity_status, source, now(), now()
                FROM _tmp_security_master
                ON CONFLICT (security_id) DO UPDATE
                SET canonical_ticker = EXCLUDED.canonical_ticker,
                    security_type = EXCLUDED.security_type,
                    identity_status = EXCLUDED.identity_status,
                    source = EXCLUDED.source,
                    last_updated = EXCLUDED.last_updated
                """
            )
            n = self.execute(
                """
                INSERT INTO security_identity_assignments
                (universe_id, ticker, effective_start, effective_end, security_id,
                 known_date, identity_status, source, fetched_at)
                SELECT universe_id, ticker, CAST(effective_start AS DATE),
                       CAST(effective_end AS DATE), security_id,
                       CAST(known_date AS DATE), identity_status, source, now()
                FROM _tmp_identity
                ON CONFLICT (universe_id, ticker, effective_start) DO UPDATE
                SET effective_end = EXCLUDED.effective_end,
                    security_id = EXCLUDED.security_id,
                    known_date = EXCLUDED.known_date,
                    identity_status = EXCLUDED.identity_status,
                    source = EXCLUDED.source,
                    fetched_at = EXCLUDED.fetched_at
                """
            ).fetchone()[0]
            self.execute(
                """
                UPDATE universe_membership AS membership
                SET security_id = identity.security_id
                FROM _tmp_identity AS identity
                WHERE membership.universe_id = identity.universe_id
                  AND membership.ticker = identity.ticker
                  AND membership.effective_start = CAST(identity.effective_start AS DATE)
                """
            )

            overlap = self.query(
                """
                WITH ordered AS (
                    SELECT universe_id, security_id, ticker, effective_start,
                           ROW_NUMBER() OVER (
                               PARTITION BY universe_id, security_id
                               ORDER BY effective_start, ticker
                           ) AS interval_number,
                           LAG(effective_end) OVER (
                               PARTITION BY universe_id, security_id
                               ORDER BY effective_start, ticker
                           ) AS previous_end
                    FROM security_identity_assignments
                )
                SELECT universe_id, security_id, ticker, effective_start,
                       previous_end
                FROM ordered
                WHERE interval_number > 1
                  AND (previous_end IS NULL OR effective_start < previous_end)
                LIMIT 1
                """
            )
            if overlap:
                sample = overlap[0]
                raise ValueError(
                    "overlapping ticker identities for "
                    f"{sample['universe_id']}:{sample['security_id']}"
                )
            self._con.execute("COMMIT")
            return int(n)
        except Exception:
            self._con.execute("ROLLBACK")
            raise
        finally:
            self._con.unregister("_tmp_identity")
            self._con.unregister("_tmp_security_master")

    def upsert_reference_identities(
        self,
        issuers: list[dict],
        cik_history: list[dict],
        security_issuers: list[dict],
        provider_symbols: list[dict],
    ) -> dict[str, int]:
        """Atomically import issuer, CIK, security-owner, and provider mappings.

        These are separate identity domains on purpose: an SEC CIK identifies
        a reporting entity, while ``security_id`` identifies a listed security.
        Provider symbols are accepted only inside explicit half-open data
        intervals, which is the guard against ticker reuse such as old/new DOC.
        """
        if not issuers or not cik_history or not security_issuers or not provider_symbols:
            raise ValueError(
                "reference identity import requires issuer, CIK, security-owner, "
                "and provider-symbol rows"
            )

        issuer_rows: list[dict] = []
        issuer_ids: set[str] = set()
        for row in issuers:
            issuer_id = _required_text(row, "issuer_id", "issuer")
            if issuer_id in issuer_ids:
                raise ValueError(f"duplicate issuer {issuer_id!r}")
            issuer_ids.add(issuer_id)
            issuer_rows.append(
                {
                    "issuer_id": issuer_id,
                    "canonical_name": _required_text(row, "canonical_name", "issuer"),
                    "canonical_ticker": _required_text(row, "canonical_ticker", "issuer").upper(),
                    "source": _required_text(row, "source", "issuer"),
                }
            )

        cik_rows: list[dict] = []
        cik_keys: set[tuple[str, date]] = set()
        for row in cik_history:
            issuer_id = _required_text(row, "issuer_id", "CIK history")
            raw_cik = _required_text(row, "cik", "CIK history")
            if not raw_cik.isdigit() or len(raw_cik) > 10:
                raise ValueError(f"invalid SEC CIK {raw_cik!r}")
            start, end = _half_open_dates(row, "effective_start", "effective_end", "CIK history")
            key = (issuer_id, start)
            if key in cik_keys:
                raise ValueError(f"duplicate CIK history interval for {issuer_id!r}")
            cik_keys.add(key)
            cik_rows.append(
                {
                    "issuer_id": issuer_id,
                    "cik": raw_cik.zfill(10),
                    "effective_start": start,
                    "effective_end": end,
                    "verified_date": _verified_date(row, "CIK history"),
                    "source": _required_text(row, "source", "CIK history"),
                }
            )

        owner_rows: list[dict] = []
        owner_keys: set[tuple[str, date]] = set()
        for row in security_issuers:
            security_id = _required_text(row, "security_id", "security issuer")
            issuer_id = _required_text(row, "issuer_id", "security issuer")
            start, end = _half_open_dates(
                row, "effective_start", "effective_end", "security issuer"
            )
            key = (security_id, start)
            if key in owner_keys:
                raise ValueError(f"duplicate security issuer interval for {security_id!r}")
            owner_keys.add(key)
            owner_rows.append(
                {
                    "security_id": security_id,
                    "issuer_id": issuer_id,
                    "effective_start": start,
                    "effective_end": end,
                    "verified_date": _verified_date(row, "security issuer"),
                    "source": _required_text(row, "source", "security issuer"),
                }
            )

        provider_rows: list[dict] = []
        provider_keys: set[tuple[str, str, date]] = set()
        for row in provider_symbols:
            provider = _required_text(row, "provider", "provider symbol").lower()
            security_id = _required_text(row, "security_id", "provider symbol")
            start, end = _half_open_dates(row, "data_start", "data_end", "provider symbol")
            status = _required_text(row, "mapping_status", "provider symbol")
            if status not in {"verified", "unavailable", "blocked_wrong_security"}:
                raise ValueError(f"unsupported provider mapping status {status!r}")
            key = (provider, security_id, start)
            if key in provider_keys:
                raise ValueError(f"duplicate provider interval for {provider}:{security_id}")
            provider_keys.add(key)
            provider_rows.append(
                {
                    "provider": provider,
                    "provider_symbol": _required_text(
                        row, "provider_symbol", "provider symbol"
                    ).upper(),
                    "security_id": security_id,
                    "data_start": start,
                    "data_end": end,
                    "mapping_status": status,
                    "verified_date": _verified_date(row, "provider symbol"),
                    "source": _required_text(row, "source", "provider symbol"),
                }
            )

        self._con.register("_tmp_issuer", _rows_to_arrowable(issuer_rows))
        self._con.register("_tmp_cik", _rows_to_arrowable(cik_rows))
        self._con.register("_tmp_owner", _rows_to_arrowable(owner_rows))
        self._con.register("_tmp_provider", _rows_to_arrowable(provider_rows))
        self.execute("BEGIN TRANSACTION")
        try:
            missing_security = self.query(
                """
                WITH referenced AS (
                    SELECT security_id FROM _tmp_owner
                    UNION
                    SELECT security_id FROM _tmp_provider
                )
                SELECT referenced.security_id
                FROM referenced
                LEFT JOIN security_master AS security USING (security_id)
                WHERE security.security_id IS NULL
                LIMIT 1
                """
            )
            if missing_security:
                raise ValueError(
                    "reference identity uses unknown security_id "
                    f"{missing_security[0]['security_id']!r}"
                )

            incoming_issuers = {row["issuer_id"] for row in issuer_rows}
            referenced_issuers = {row["issuer_id"] for row in cik_rows + owner_rows}
            existing_issuers = {
                row["issuer_id"] for row in self.query("SELECT issuer_id FROM issuer_master")
            }
            unknown_issuers = referenced_issuers - incoming_issuers - existing_issuers
            if unknown_issuers:
                raise ValueError(
                    f"reference identity uses unknown issuer_id {sorted(unknown_issuers)[0]!r}"
                )

            conflict_checks = (
                (
                    """
                    SELECT existing.issuer_id
                    FROM issuer_cik_history AS existing
                    JOIN _tmp_cik AS incoming
                      ON incoming.issuer_id = existing.issuer_id
                     AND CAST(incoming.effective_start AS DATE) = existing.effective_start
                    WHERE existing.cik <> incoming.cik
                    LIMIT 1
                    """,
                    "CIK history remap",
                ),
                (
                    """
                    SELECT existing.security_id
                    FROM security_issuer_assignments AS existing
                    JOIN _tmp_owner AS incoming
                      ON incoming.security_id = existing.security_id
                     AND CAST(incoming.effective_start AS DATE) = existing.effective_start
                    WHERE existing.issuer_id <> incoming.issuer_id
                    LIMIT 1
                    """,
                    "security issuer remap",
                ),
                (
                    """
                    SELECT existing.security_id
                    FROM provider_symbol_history AS existing
                    JOIN _tmp_provider AS incoming
                      ON incoming.provider = existing.provider
                     AND incoming.security_id = existing.security_id
                     AND CAST(incoming.data_start AS DATE) = existing.data_start
                    WHERE existing.provider_symbol <> incoming.provider_symbol
                       OR existing.mapping_status <> incoming.mapping_status
                    LIMIT 1
                    """,
                    "provider symbol remap",
                ),
            )
            for sql, label in conflict_checks:
                conflict = self.query(sql)
                if conflict:
                    raise ValueError(f"{label} conflicts with existing provenance")

            issuer_count = self.execute(
                """
                INSERT INTO issuer_master
                (issuer_id, canonical_name, canonical_ticker, source,
                 created_at, last_updated)
                SELECT issuer_id, canonical_name, canonical_ticker, source, now(), now()
                FROM _tmp_issuer
                ON CONFLICT (issuer_id) DO UPDATE
                SET canonical_name = EXCLUDED.canonical_name,
                    canonical_ticker = EXCLUDED.canonical_ticker,
                    source = EXCLUDED.source,
                    last_updated = EXCLUDED.last_updated
                """
            ).fetchone()[0]
            cik_count = self.execute(
                """
                INSERT INTO issuer_cik_history
                (issuer_id, cik, effective_start, effective_end, verified_date,
                 source, fetched_at)
                SELECT issuer_id, cik, CAST(effective_start AS DATE),
                       CAST(effective_end AS DATE), CAST(verified_date AS DATE),
                       source, now()
                FROM _tmp_cik
                ON CONFLICT (issuer_id, effective_start) DO UPDATE
                SET effective_end = EXCLUDED.effective_end,
                    verified_date = EXCLUDED.verified_date,
                    source = EXCLUDED.source,
                    fetched_at = EXCLUDED.fetched_at
                """
            ).fetchone()[0]
            owner_count = self.execute(
                """
                INSERT INTO security_issuer_assignments
                (security_id, issuer_id, effective_start, effective_end,
                 verified_date, source, fetched_at)
                SELECT security_id, issuer_id, CAST(effective_start AS DATE),
                       CAST(effective_end AS DATE), CAST(verified_date AS DATE),
                       source, now()
                FROM _tmp_owner
                ON CONFLICT (security_id, effective_start) DO UPDATE
                SET effective_end = EXCLUDED.effective_end,
                    verified_date = EXCLUDED.verified_date,
                    source = EXCLUDED.source,
                    fetched_at = EXCLUDED.fetched_at
                """
            ).fetchone()[0]
            provider_count = 0
            if provider_rows:
                provider_count = self.execute(
                    """
                    INSERT INTO provider_symbol_history
                    (provider, provider_symbol, security_id, data_start, data_end,
                     mapping_status, verified_date, source, fetched_at)
                    SELECT provider, provider_symbol, security_id,
                           CAST(data_start AS DATE), CAST(data_end AS DATE),
                           mapping_status, CAST(verified_date AS DATE), source, now()
                    FROM _tmp_provider
                    ON CONFLICT (provider, security_id, data_start) DO UPDATE
                    SET data_end = EXCLUDED.data_end,
                        verified_date = EXCLUDED.verified_date,
                        source = EXCLUDED.source,
                        fetched_at = EXCLUDED.fetched_at
                    """
                ).fetchone()[0]

            overlap_checks = (
                (
                    "issuer_cik_history",
                    "issuer_id",
                    "effective_start",
                    "effective_end",
                    "overlapping CIK history",
                ),
                (
                    "security_issuer_assignments",
                    "security_id",
                    "effective_start",
                    "effective_end",
                    "overlapping security issuer assignments",
                ),
            )
            for table, partition, start_col, end_col, label in overlap_checks:
                overlap = self.query(
                    f"""
                    WITH ordered AS (
                        SELECT {partition}, {start_col}, {end_col},
                               ROW_NUMBER() OVER (
                                   PARTITION BY {partition} ORDER BY {start_col}
                               ) AS interval_number,
                               LAG({end_col}) OVER (
                                   PARTITION BY {partition} ORDER BY {start_col}
                               ) AS previous_end
                        FROM {table}
                    )
                    SELECT {partition} FROM ordered
                    WHERE interval_number > 1
                      AND (previous_end IS NULL OR {start_col} < previous_end)
                    LIMIT 1
                    """
                )
                if overlap:
                    raise ValueError(label)

            provider_overlap = self.query(
                """
                WITH ordered AS (
                    SELECT provider, security_id, data_start, data_end,
                           ROW_NUMBER() OVER (
                               PARTITION BY provider, security_id ORDER BY data_start
                           ) AS interval_number,
                           LAG(data_end) OVER (
                               PARTITION BY provider, security_id ORDER BY data_start
                           ) AS previous_end
                    FROM provider_symbol_history
                )
                SELECT provider, security_id FROM ordered
                WHERE interval_number > 1
                  AND (previous_end IS NULL OR data_start < previous_end)
                LIMIT 1
                """
            )
            if provider_overlap:
                raise ValueError("overlapping provider symbol assignments")

            reused_symbol = self.query(
                """
                SELECT left_map.provider, left_map.provider_symbol
                FROM provider_symbol_history AS left_map
                JOIN provider_symbol_history AS right_map
                  ON right_map.provider = left_map.provider
                 AND right_map.provider_symbol = left_map.provider_symbol
                 AND right_map.security_id <> left_map.security_id
                 AND right_map.mapping_status = 'verified'
                 AND left_map.mapping_status = 'verified'
                 AND COALESCE(left_map.data_end, DATE '9999-12-31') > right_map.data_start
                 AND COALESCE(right_map.data_end, DATE '9999-12-31') > left_map.data_start
                LIMIT 1
                """
            )
            if reused_symbol:
                raise ValueError("one provider symbol maps to overlapping securities")

            self.execute("COMMIT")
            return {
                "issuers": int(issuer_count),
                "cik_history": int(cik_count),
                "security_issuers": int(owner_count),
                "provider_symbols": int(provider_count),
            }
        except Exception:
            self.execute("ROLLBACK")
            raise
        finally:
            self._con.unregister("_tmp_issuer")
            self._con.unregister("_tmp_cik")
            self._con.unregister("_tmp_owner")
            self._con.unregister("_tmp_provider")

    def upsert_prices(self, rows: list[dict]) -> int:
        """Upsert daily prices. Idempotent on (ticker, date)."""
        if not rows:
            return 0
        normalized = [
            {
                "ticker": str(r.get("ticker") or "").strip().upper(),
                "security_id": r.get("security_id"),
                "provider_symbol": r.get("provider_symbol"),
                "date": r.get("date"),
                "open": r.get("open"),
                "high": r.get("high"),
                "low": r.get("low"),
                "close": r.get("close"),
                "adj_close": r.get("adj_close"),
                "volume": r.get("volume"),
                "dividends": r.get("dividends", 0),
                "split_ratio": r.get("split_ratio", 1),
                "source": r.get("source", "yfinance"),
            }
            for r in rows
        ]
        self._con.register("_tmp_px", _rows_to_arrowable(normalized))
        n = self._con.execute(
            """
            INSERT INTO prices
            (ticker, security_id, provider_symbol, date, open, high, low, close,
             adj_close, volume, dividends, split_ratio, source, fetched_at)
            SELECT
                ticker, security_id, provider_symbol, CAST(date AS DATE),
                open, high, low, close, adj_close, volume,
                COALESCE(dividends, 0), COALESCE(split_ratio, 1),
                COALESCE(source, 'yfinance'), now()
            FROM _tmp_px
            ON CONFLICT (ticker, date) DO UPDATE
            SET security_id = COALESCE(EXCLUDED.security_id, prices.security_id),
                provider_symbol = COALESCE(
                    EXCLUDED.provider_symbol, prices.provider_symbol
                ),
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                adj_close = EXCLUDED.adj_close,
                volume = EXCLUDED.volume,
                dividends = EXCLUDED.dividends,
                split_ratio = EXCLUDED.split_ratio,
                source = EXCLUDED.source,
                fetched_at = EXCLUDED.fetched_at
            """.strip()
        ).fetchone()[0]
        self._con.unregister("_tmp_px")
        return int(n)

    def upsert_fundamentals(self, rows: list[dict]) -> int:
        """Upsert fundamentals. CRITICAL: as_of_date must be set per row.

        This is the point-in-time-critical insert. Never call with a default
        as_of_date; the caller must supply the *filing date* from the source.
        """
        if not rows:
            return 0
        # Defensive: refuse to insert fundamentals without as_of_date.
        missing = [r for r in rows if not r.get("as_of_date")]
        if missing:
            raise ValueError(
                f"upsert_fundamentals: {len(missing)} rows lack as_of_date. "
                "Point-in-time correctness requires a knowable-date per row."
            )
        missing_period_end = [r for r in rows if not r.get("period_end")]
        if missing_period_end:
            raise ValueError(
                f"upsert_fundamentals: {len(missing_period_end)} rows lack period_end."
            )

        normalized: list[dict] = []
        invalid_periods = 0
        for row in rows:
            period_end = _as_date(row["period_end"])
            as_of_date = _as_date(row["as_of_date"])
            if period_end > as_of_date:
                invalid_periods += 1
            normalized.append(
                {
                    "ticker": str(row.get("ticker") or "").strip().upper(),
                    "issuer_id": row.get("issuer_id"),
                    "security_id": row.get("security_id"),
                    "period_end": period_end,
                    "as_of_date": as_of_date,
                    "fiscal_period": row.get("fiscal_period"),
                    "statement": row.get("statement"),
                    "metric": row.get("metric"),
                    "value": row.get("value"),
                    "quarter_value": row.get("quarter_value"),
                    "unit": row.get("unit", "USD"),
                    "source": row.get("source", "edgar"),
                }
            )
        if invalid_periods:
            raise ValueError(
                f"upsert_fundamentals: {invalid_periods} rows have period_end later "
                "than as_of_date. A filing cannot report a future fiscal period."
            )
        self._con.register("_tmp_fund", _rows_to_arrowable(normalized))
        n = self._con.execute(
            """
            INSERT INTO fundamentals
            (ticker, issuer_id, security_id, period_end, as_of_date, fiscal_period,
             statement, metric, value, quarter_value, unit, source, fetched_at)
            SELECT
                ticker, issuer_id, security_id, CAST(period_end AS DATE),
                CAST(as_of_date AS DATE), fiscal_period, statement, metric,
                value, quarter_value,
                COALESCE(unit, 'USD'), source, now()
            FROM _tmp_fund
            ON CONFLICT (ticker, period_end, as_of_date, metric) DO UPDATE
            SET issuer_id = COALESCE(EXCLUDED.issuer_id, fundamentals.issuer_id),
                security_id = COALESCE(
                    EXCLUDED.security_id, fundamentals.security_id
                ),
                fiscal_period = EXCLUDED.fiscal_period,
                statement = EXCLUDED.statement,
                value = EXCLUDED.value,
                quarter_value = EXCLUDED.quarter_value,
                unit = EXCLUDED.unit,
                source = EXCLUDED.source,
                fetched_at = EXCLUDED.fetched_at
            """.strip()
        ).fetchone()[0]
        self._con.unregister("_tmp_fund")
        return int(n)

    def refresh_issuer_fundamentals(
        self,
        rows: list[dict],
        *,
        issuer_id: str,
        canonical_ticker: str,
    ) -> tuple[int, int]:
        """Upsert one issuer snapshot and remove only stale canonical labels.

        SEC Company Facts is fetched as a complete issuer history. Once that
        complete payload has been extracted successfully, keeping rows for the
        same ``issuer_id`` under an older canonical display ticker would create
        duplicate PIT facts. The upsert and narrow cleanup are one transaction.
        Untagged legacy rows and rows belonging to other issuers are untouched.
        """
        if not rows:
            return 0, 0
        issuer_id = issuer_id.strip()
        canonical_ticker = canonical_ticker.strip().upper()
        if not issuer_id or not canonical_ticker:
            raise ValueError("issuer refresh requires issuer_id and canonical_ticker")
        if any(str(row.get("issuer_id") or "").strip() != issuer_id for row in rows):
            raise ValueError("issuer refresh contains a different issuer_id")
        if any(str(row.get("ticker") or "").strip().upper() != canonical_ticker for row in rows):
            raise ValueError("issuer refresh contains a non-canonical ticker")

        self.execute("BEGIN TRANSACTION")
        try:
            inserted = self.upsert_fundamentals(rows)
            stale = self.query(
                """
                SELECT COUNT(*) AS n
                FROM fundamentals
                WHERE issuer_id = ? AND ticker <> ?
                """,
                (issuer_id, canonical_ticker),
            )[0]["n"]
            if stale:
                self.execute(
                    "DELETE FROM fundamentals WHERE issuer_id = ? AND ticker <> ?",
                    (issuer_id, canonical_ticker),
                )
            self.execute("COMMIT")
            return inserted, int(stale)
        except Exception:
            self.execute("ROLLBACK")
            raise

    def upsert_macro(self, rows: list[dict]) -> int:
        """Upsert release-aware macro vintages.

        New rows must carry the date on which that vintage became public. This
        prevents callers from accidentally putting revised macro data into a
        table that the regime layer may later query point-in-time.
        """
        if not rows:
            return 0
        missing = [r for r in rows if not r.get("release_date")]
        if missing:
            raise ValueError(
                f"upsert_macro: {len(missing)} rows lack release_date. "
                "Point-in-time macro analysis requires the public vintage date."
            )
        self._con.register("_tmp_macro", _rows_to_arrowable(self._coerce_macro(rows)))
        try:
            n = self._con.execute(
                """
                INSERT INTO macro
                (series_id, date, release_date, value, unit, source, fetched_at)
                SELECT
                    series_id, CAST(date AS DATE), CAST(release_date AS DATE),
                    value, unit, source, now()
                FROM _tmp_macro
                ON CONFLICT (series_id, date, release_date, source) DO UPDATE
                SET value = EXCLUDED.value,
                    unit = EXCLUDED.unit,
                    fetched_at = EXCLUDED.fetched_at
                """.strip()
            ).fetchone()[0]
            return int(n)
        finally:
            self._con.unregister("_tmp_macro")

    def record_ingest(
        self,
        source: str,
        table_name: str,
        rows_inserted: int = 0,
        rows_rejected: int = 0,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        status: str = "success",
        error: str | None = None,
        run_id: str | None = None,
    ) -> str:
        """Write one auditable ingest outcome and return its run id.

        This is intentionally explicit instead of hidden inside every upsert:
        one ingest operation may fetch several source payloads before it writes
        one table, and the caller knows the operation's true boundary.
        """
        run_id = run_id or str(uuid4())
        started_at = started_at or datetime.now()
        finished_at = finished_at or datetime.now()
        self._con.execute(
            """
            INSERT INTO ingest_log
            (run_id, source, table_name, rows_inserted, rows_rejected,
             started_at, finished_at, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                source,
                table_name,
                rows_inserted,
                rows_rejected,
                started_at,
                finished_at,
                status,
                error,
            ),
        )
        return run_id

    def ingest_history(self, limit: int = 20) -> list[dict]:
        """Return the most recent ingest outcomes for operators and agents."""
        if limit < 1:
            return []
        return self.query(
            """
            SELECT id, run_id, source, table_name, rows_inserted, rows_rejected,
                   started_at, finished_at, status, error
            FROM ingest_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )

    def data_quality_report(self) -> list[dict]:
        """Run read-only checks on the stored data.

        A warning is actionable but does not make the database unusable. A
        failure means a required field is missing and downstream calculations
        should stop until the ingest is repaired.
        """
        checks: list[dict] = []

        def add(name: str, count: int, severity: str, detail: str) -> None:
            checks.append(
                {
                    "check": name,
                    "status": "ok" if count == 0 else severity,
                    "count": count,
                    "detail": detail,
                }
            )

        add(
            "fundamentals_missing_as_of_date",
            self.query("SELECT COUNT(*) AS n FROM fundamentals WHERE as_of_date IS NULL")[0]["n"],
            "fail",
            "Every fundamental must have a knowable date for PIT analysis.",
        )
        add(
            "fundamentals_future_as_of_date",
            self.query("SELECT COUNT(*) AS n FROM fundamentals WHERE as_of_date > CURRENT_DATE")[0][
                "n"
            ],
            "fail",
            "Future availability dates indicate a malformed or premature ingest.",
        )
        add(
            "fundamentals_period_end_after_as_of_date",
            self.query("SELECT COUNT(*) AS n FROM fundamentals WHERE period_end > as_of_date")[0][
                "n"
            ],
            "fail",
            "A fiscal period cannot end after the filing became publicly knowable.",
        )
        add(
            "fundamentals_missing_quarter_value",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM fundamentals
                WHERE metric IN (
                    'revenue', 'net_income', 'operating_income', 'gross_profit',
                    'rd_expense', 'interest_expense', 'depreciation', 'cfo',
                    'capex', 'dividends_paid'
                )
                AND quarter_value IS NULL
                """
            )[0]["n"],
            "warn",
            "Flow metrics need a single-period value for TTM calculations.",
        )
        add(
            "legacy_mislabeled_ebitda",
            self.query("SELECT COUNT(*) AS n FROM fundamentals WHERE metric = 'ebitda'")[0]["n"],
            "warn",
            "Legacy rows used net income as EBITDA; clean re-ingest is required.",
        )
        add(
            "prices_missing_close",
            self.query("SELECT COUNT(*) AS n FROM prices WHERE close IS NULL")[0]["n"],
            "fail",
            "A price row without close cannot support valuation or returns.",
        )
        add(
            "macro_missing_value",
            self.query("SELECT COUNT(*) AS n FROM macro WHERE value IS NULL")[0]["n"],
            "warn",
            "Missing observations should not be used in regime calculations.",
        )
        add(
            "macro_unversioned_rows",
            self.query("SELECT COUNT(*) AS n FROM macro WHERE release_date IS NULL")[0]["n"],
            "fail",
            "Macro regime/backtest inputs lack public vintage dates and must be re-ingested.",
        )
        add(
            "macro_future_release_date",
            self.query("SELECT COUNT(*) AS n FROM macro WHERE release_date > CURRENT_DATE")[0]["n"],
            "fail",
            "A future release date is malformed and cannot be used for analysis.",
        )
        add(
            "universe_invalid_intervals",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM universe_membership
                WHERE effective_end IS NOT NULL AND effective_end <= effective_start
                """
            )[0]["n"],
            "fail",
            "Historical universe intervals must have a positive duration.",
        )
        add(
            "universe_future_known_dates",
            self.query(
                "SELECT COUNT(*) AS n FROM universe_membership WHERE known_date > CURRENT_DATE"
            )[0]["n"],
            "fail",
            "Future membership knowledge dates cannot be used in a backtest.",
        )
        add(
            "universe_overlapping_intervals",
            self.query(
                """
                WITH ordered AS (
                    SELECT universe_id, ticker, effective_start, effective_end,
                           ROW_NUMBER() OVER (
                               PARTITION BY universe_id, ticker ORDER BY effective_start
                           ) AS interval_number,
                           LAG(effective_end) OVER (
                               PARTITION BY universe_id, ticker ORDER BY effective_start
                           ) AS previous_end
                    FROM universe_membership
                )
                SELECT COUNT(*) AS n
                FROM ordered
                WHERE interval_number > 1
                  AND (previous_end IS NULL OR effective_start < previous_end)
                """
            )[0]["n"],
            "fail",
            "A ticker may have only one active interval per universe/date.",
        )
        add(
            "universe_missing_security_ids",
            self.query("SELECT COUNT(*) AS n FROM universe_membership WHERE security_id IS NULL")[
                0
            ]["n"],
            "fail",
            "Every certified membership interval needs a stable security identity.",
        )
        add(
            "universe_orphan_security_ids",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM universe_membership AS membership
                LEFT JOIN security_master AS security
                  ON security.security_id = membership.security_id
                WHERE membership.security_id IS NOT NULL
                  AND security.security_id IS NULL
                """
            )[0]["n"],
            "fail",
            "Membership security IDs must exist in the security master.",
        )
        add(
            "security_identity_membership_mismatches",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM universe_membership AS membership
                LEFT JOIN security_identity_assignments AS identity
                  ON identity.universe_id = membership.universe_id
                 AND identity.ticker = membership.ticker
                 AND identity.effective_start = membership.effective_start
                WHERE membership.security_id IS NOT NULL
                  AND (
                      identity.security_id IS NULL
                      OR identity.security_id <> membership.security_id
                      OR identity.effective_end IS DISTINCT FROM membership.effective_end
                  )
                """
            )[0]["n"],
            "fail",
            "Every populated membership identity must match its audited assignment.",
        )
        add(
            "security_identity_future_known_dates",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM security_identity_assignments
                WHERE known_date > CURRENT_DATE
                """
            )[0]["n"],
            "fail",
            "Future identity knowledge dates cannot be used in a backtest.",
        )
        add(
            "security_identity_overlapping_tickers",
            self.query(
                """
                WITH ordered AS (
                    SELECT universe_id, security_id, effective_start, effective_end,
                           ROW_NUMBER() OVER (
                               PARTITION BY universe_id, security_id
                               ORDER BY effective_start, ticker
                           ) AS interval_number,
                           LAG(effective_end) OVER (
                               PARTITION BY universe_id, security_id
                               ORDER BY effective_start, ticker
                           ) AS previous_end
                    FROM security_identity_assignments
                )
                SELECT COUNT(*) AS n
                FROM ordered
                WHERE interval_number > 1
                  AND (previous_end IS NULL OR effective_start < previous_end)
                """
            )[0]["n"],
            "fail",
            "One security cannot have overlapping ticker assignments in a universe.",
        )
        add(
            "issuer_cik_invalid_intervals",
            self.query(
                """
                SELECT COUNT(*) AS n FROM issuer_cik_history
                WHERE effective_end IS NOT NULL AND effective_end <= effective_start
                """
            )[0]["n"],
            "fail",
            "SEC CIK assignments use positive half-open intervals.",
        )
        add(
            "reference_identity_future_verification_dates",
            self.query(
                """
                SELECT (
                    (SELECT COUNT(*) FROM issuer_cik_history
                     WHERE verified_date > CURRENT_DATE)
                    + (SELECT COUNT(*) FROM security_issuer_assignments
                       WHERE verified_date > CURRENT_DATE)
                    + (SELECT COUNT(*) FROM provider_symbol_history
                       WHERE verified_date > CURRENT_DATE)
                ) AS n
                """
            )[0]["n"],
            "fail",
            "Identity evidence cannot be marked verified in the future.",
        )
        add(
            "reference_identity_orphans",
            self.query(
                """
                SELECT (
                    (SELECT COUNT(*)
                     FROM issuer_cik_history AS cik
                     LEFT JOIN issuer_master AS issuer USING (issuer_id)
                     WHERE issuer.issuer_id IS NULL)
                    + (SELECT COUNT(*)
                       FROM security_issuer_assignments AS owner
                       LEFT JOIN security_master AS security USING (security_id)
                       LEFT JOIN issuer_master AS issuer USING (issuer_id)
                       WHERE security.security_id IS NULL OR issuer.issuer_id IS NULL)
                    + (SELECT COUNT(*)
                       FROM provider_symbol_history AS mapping
                       LEFT JOIN security_master AS security USING (security_id)
                       WHERE security.security_id IS NULL)
                ) AS n
                """
            )[0]["n"],
            "fail",
            "CIK, owner, and provider mappings must reference their master IDs.",
        )
        add(
            "reference_identity_overlapping_intervals",
            self.query(
                """
                WITH cik_ordered AS (
                    SELECT issuer_id, effective_start,
                           ROW_NUMBER() OVER (
                               PARTITION BY issuer_id ORDER BY effective_start
                           ) AS interval_number,
                           LAG(effective_end) OVER (
                               PARTITION BY issuer_id ORDER BY effective_start
                           ) AS previous_end
                    FROM issuer_cik_history
                ), owner_ordered AS (
                    SELECT security_id, effective_start,
                           ROW_NUMBER() OVER (
                               PARTITION BY security_id ORDER BY effective_start
                           ) AS interval_number,
                           LAG(effective_end) OVER (
                               PARTITION BY security_id ORDER BY effective_start
                           ) AS previous_end
                    FROM security_issuer_assignments
                ), provider_ordered AS (
                    SELECT provider, security_id, data_start,
                           ROW_NUMBER() OVER (
                               PARTITION BY provider, security_id ORDER BY data_start
                           ) AS interval_number,
                           LAG(data_end) OVER (
                               PARTITION BY provider, security_id ORDER BY data_start
                           ) AS previous_end
                    FROM provider_symbol_history
                )
                SELECT (
                    (SELECT COUNT(*) FROM cik_ordered
                     WHERE interval_number > 1
                       AND (previous_end IS NULL OR effective_start < previous_end))
                    + (SELECT COUNT(*) FROM owner_ordered
                       WHERE interval_number > 1
                         AND (previous_end IS NULL OR effective_start < previous_end))
                    + (SELECT COUNT(*) FROM provider_ordered
                       WHERE interval_number > 1
                         AND (previous_end IS NULL OR data_start < previous_end))
                ) AS n
                """
            )[0]["n"],
            "fail",
            "Reference identity intervals must not overlap within one identity.",
        )
        add(
            "provider_symbol_overlapping_reuse",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM provider_symbol_history AS left_map
                JOIN provider_symbol_history AS right_map
                  ON right_map.provider = left_map.provider
                 AND right_map.provider_symbol = left_map.provider_symbol
                 AND right_map.security_id > left_map.security_id
                 AND right_map.mapping_status = 'verified'
                 AND left_map.mapping_status = 'verified'
                 AND COALESCE(left_map.data_end, DATE '9999-12-31') > right_map.data_start
                 AND COALESCE(right_map.data_end, DATE '9999-12-31') > left_map.data_start
                """
            )[0]["n"],
            "fail",
            "A provider symbol cannot identify two securities on the same date.",
        )
        add(
            "tagged_prices_outside_provider_provenance",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM prices AS price
                WHERE price.security_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM provider_symbol_history AS mapping
                      WHERE mapping.security_id = price.security_id
                        AND mapping.provider = price.source
                        AND mapping.provider_symbol = price.provider_symbol
                        AND mapping.mapping_status = 'verified'
                        AND mapping.data_start <= price.date
                        AND (mapping.data_end IS NULL OR mapping.data_end > price.date)
                  )
                """
            )[0]["n"],
            "fail",
            "Identity-tagged prices must remain inside a reviewed provider window.",
        )
        add(
            "tagged_prices_wrong_dated_ticker",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM prices AS price
                WHERE price.security_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM security_identity_assignments AS identity
                      WHERE identity.security_id = price.security_id
                        AND identity.ticker = price.ticker
                        AND identity.effective_start <= price.date
                        AND (
                            identity.effective_end IS NULL
                            OR identity.effective_end > price.date
                        )
                  )
                """
            )[0]["n"],
            "fail",
            "Stored market tickers must match the security identity on each price date.",
        )
        add(
            "tagged_fundamentals_orphan_identities",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM fundamentals AS fundamental
                WHERE (
                    fundamental.issuer_id IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM issuer_master AS issuer
                        WHERE issuer.issuer_id = fundamental.issuer_id
                    )
                ) OR (
                    fundamental.security_id IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM security_issuer_assignments AS owner
                        WHERE owner.security_id = fundamental.security_id
                          AND (
                              fundamental.issuer_id IS NULL
                              OR owner.issuer_id = fundamental.issuer_id
                          )
                    )
                )
                """
            )[0]["n"],
            "fail",
            "Identity-tagged fundamentals must reference a reviewed issuer/security link.",
        )
        add(
            "failed_ingests",
            self.query("SELECT COUNT(*) AS n FROM ingest_log WHERE status = 'failed'")[0]["n"],
            "warn",
            "Inspect `aios audit` for source errors and retry only failed work.",
        )
        add(
            "zero_row_ingests",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM ingest_log
                WHERE table_name = 'fundamentals'
                  AND COALESCE(rows_inserted, 0) = 0
                  AND status IN ('success', 'warning')
                """
            )[0]["n"],
            "warn",
            "A fundamentals source returned no rows; inspect the run and issuer mapping.",
        )
        return checks

    def purge_legacy_ebitda(self, ticker: str | None = None) -> int:
        """Delete the known-invalid pre-D&A EBITDA metric rows.

        The current factor engine never reads these rows. This operation is
        deliberately narrow and exists only to remove the legacy metric that
        was populated from net income in the pre-correction database.
        """
        where = "metric = 'ebitda'"
        params: tuple[Any, ...] = ()
        if ticker:
            where += " AND ticker = ?"
            params = (ticker.upper(),)
        count = self.query(f"SELECT COUNT(*) AS n FROM fundamentals WHERE {where}", params)[0]["n"]
        self.execute(f"DELETE FROM fundamentals WHERE {where}", params)
        return int(count)

    def quarantine_invalid_fundamental_periods(self) -> int:
        """Move impossible future-period facts out of the active PIT table."""
        count = self.query("SELECT COUNT(*) AS n FROM fundamentals WHERE period_end > as_of_date")[
            0
        ]["n"]
        if not count:
            return 0
        self.execute("BEGIN TRANSACTION")
        try:
            self.execute(
                """
                INSERT INTO fundamentals_quarantine
                (ticker, issuer_id, security_id, period_end, as_of_date,
                 fiscal_period, statement, metric, value, quarter_value, unit,
                 source, fetched_at, quarantine_reason, quarantined_at)
                SELECT ticker, issuer_id, security_id, period_end, as_of_date,
                       fiscal_period, statement, metric, value, quarter_value,
                       unit, source, fetched_at,
                       'period_end_after_as_of_date', now()
                FROM fundamentals
                WHERE period_end > as_of_date
                """
            )
            self.execute("DELETE FROM fundamentals WHERE period_end > as_of_date")
            self.execute("COMMIT")
        except Exception:
            self.execute("ROLLBACK")
            raise
        return int(count)

    def purge_legacy_macro(self, series_ids: list[str] | None = None) -> int:
        """Remove only quarantined macro copies after replacement coverage.

        The original rows remain in ``macro_legacy``. This deletes their copies
        from the active table only when every selected ``(series_id, date)``
        already has at least one release-aware replacement. Refusing partial
        cleanup prevents a seemingly healthy database from silently losing
        historical macro coverage.
        """
        where = "release_date IS NULL AND source = 'legacy_unversioned'"
        params: tuple[Any, ...] = ()
        if series_ids:
            normalized = [series_id.upper() for series_id in series_ids]
            placeholders = ",".join("?" for _ in normalized)
            where += f" AND series_id IN ({placeholders})"
            params = tuple(normalized)

        uncovered = self.query(
            f"""
            SELECT DISTINCT legacy.series_id
            FROM macro AS legacy
            WHERE {where}
              AND NOT EXISTS (
                  SELECT 1
                  FROM macro AS replacement
                  WHERE replacement.series_id = legacy.series_id
                    AND replacement.date = legacy.date
                    AND replacement.release_date IS NOT NULL
              )
            ORDER BY legacy.series_id
            """,
            params,
        )
        if uncovered:
            series = ", ".join(row["series_id"] for row in uncovered)
            raise ValueError(
                "Cannot purge legacy macro rows; release-aware replacements are missing for: "
                f"{series}"
            )

        count = self.query(f"SELECT COUNT(*) AS n FROM macro WHERE {where}", params)[0]["n"]
        self.execute("BEGIN TRANSACTION")
        try:
            self.execute(f"DELETE FROM macro WHERE {where}", params)
            self.execute(
                """
                INSERT OR REPLACE INTO schema_migrations (name, applied_at)
                VALUES (?, now())
                """,
                (MACRO_LEGACY_PURGED_MIGRATION,),
            )
            self.execute("COMMIT")
        except Exception:
            self.execute("ROLLBACK")
            raise
        return int(count)

    @staticmethod
    def _coerce_macro(rows: list[dict]) -> list[dict]:
        clean = []
        for r in rows:
            clean.append(
                {
                    "series_id": r["series_id"],
                    "date": r.get("date"),
                    "release_date": r.get("release_date"),
                    "value": _to_float(r.get("value")),
                    "unit": r.get("unit"),
                    "source": r.get("source", "fred"),
                    "fetched_at": r.get("fetched_at"),
                }
            )
        return clean

    # ------------------------------------------------------------------
    # Point-in-time query helpers (used by factor + backtest layers)
    # ------------------------------------------------------------------
    def security_id_for_ticker(
        self,
        ticker: str,
        as_of: date | str,
        *,
        universe_id: str | None = None,
    ) -> str | None:
        """Resolve a dated market ticker without treating it as permanent."""
        sql = """
            SELECT DISTINCT security_id
            FROM security_identity_assignments
            WHERE ticker = ?
              AND known_date <= CAST(? AS DATE)
              AND effective_start <= CAST(? AS DATE)
              AND (effective_end IS NULL OR effective_end > CAST(? AS DATE))
        """
        params: list[Any] = [ticker.upper(), str(as_of), str(as_of), str(as_of)]
        if universe_id is not None:
            sql += " AND universe_id = ?"
            params.append(universe_id)
        rows = self.query(sql, tuple(params))
        if len(rows) > 1:
            raise ValueError(f"ambiguous security identity for {ticker}@{as_of}")
        return rows[0]["security_id"] if rows else None

    def issuer_id_for_security(
        self,
        security_id: str,
        as_of: date | str,
    ) -> str | None:
        """Resolve the reporting issuer that owns a security on one date."""
        rows = self.query(
            """
            SELECT DISTINCT issuer_id
            FROM security_issuer_assignments
            WHERE security_id = ?
              AND effective_start <= CAST(? AS DATE)
              AND (effective_end IS NULL OR effective_end > CAST(? AS DATE))
            """,
            (security_id, str(as_of), str(as_of)),
        )
        if len(rows) > 1:
            raise ValueError(f"ambiguous issuer identity for {security_id}@{as_of}")
        return rows[0]["issuer_id"] if rows else None

    def issuer_id_for_ticker(self, ticker: str, as_of: date | str) -> str | None:
        security_id = self.security_id_for_ticker(ticker, as_of)
        if security_id is None:
            return None
        return self.issuer_id_for_security(security_id, as_of)

    def _fundamental_identity_filter(
        self,
        ticker: str,
        as_of: date | str,
    ) -> tuple[str, str] | None:
        """Resolve a PIT fundamental identity without crossing reviewed gaps.

        Ticker fallback is retained only for legacy securities that have never
        received a reviewed owner assignment. Once owner history exists, a date
        without an active owner is an explicit data gap and must fail closed.
        """
        normalized_ticker = ticker.upper()
        security_id = self.security_id_for_ticker(normalized_ticker, as_of)
        if security_id is None:
            return "ticker = ?", normalized_ticker

        issuer_id = self.issuer_id_for_security(security_id, as_of)
        if issuer_id is not None:
            return "issuer_id = ?", issuer_id

        has_reviewed_owner = bool(
            self.query(
                """
                SELECT 1
                FROM security_issuer_assignments
                WHERE security_id = ?
                LIMIT 1
                """,
                (security_id,),
            )
        )
        if has_reviewed_owner:
            return None
        return "ticker = ?", normalized_ticker

    def issuer_reference(self, issuer_id: str) -> dict | None:
        """Return canonical issuer metadata and its latest verified SEC CIK."""
        rows = self.query(
            """
            SELECT issuer.issuer_id, issuer.canonical_name,
                   issuer.canonical_ticker, cik.cik,
                   cik.effective_start, cik.effective_end,
                   cik.verified_date, cik.source AS cik_source,
                   issuer.source AS issuer_source
            FROM issuer_master AS issuer
            JOIN issuer_cik_history AS cik USING (issuer_id)
            WHERE issuer.issuer_id = ?
            ORDER BY cik.effective_start DESC
            LIMIT 1
            """,
            (issuer_id,),
        )
        return rows[0] if rows else None

    def provider_symbol_mappings(
        self,
        security_id: str,
        *,
        provider: str | None = None,
        start: date | str | None = None,
        end: date | str | None = None,
        status: str = "verified",
    ) -> list[dict]:
        """Return provider mappings whose half-open data windows overlap a range."""
        sql = """
            SELECT provider, provider_symbol, security_id, data_start, data_end,
                   mapping_status, verified_date, source
            FROM provider_symbol_history
            WHERE security_id = ? AND mapping_status = ?
        """
        params: list[Any] = [security_id, status]
        if provider is not None:
            sql += " AND provider = ?"
            params.append(provider.lower())
        if start is not None:
            sql += " AND (data_end IS NULL OR data_end > CAST(? AS DATE))"
            params.append(str(start))
        if end is not None:
            sql += " AND data_start < CAST(? AS DATE)"
            params.append(str(end))
        sql += " ORDER BY provider, data_start"
        return self.query(sql, tuple(params))

    def security_ticker_assignments(
        self,
        security_id: str,
        *,
        start: date | str | None = None,
        end: date | str | None = None,
    ) -> list[dict]:
        """Return dated market labels used to relabel provider history."""
        sql = """
            SELECT DISTINCT ticker, effective_start, effective_end
            FROM security_identity_assignments
            WHERE security_id = ?
        """
        params: list[Any] = [security_id]
        if start is not None:
            sql += " AND (effective_end IS NULL OR effective_end > CAST(? AS DATE))"
            params.append(str(start))
        if end is not None:
            sql += " AND effective_start < CAST(? AS DATE)"
            params.append(str(end))
        sql += " ORDER BY effective_start, ticker"
        return self.query(sql, tuple(params))

    def pit_fundamentals(
        self,
        ticker: str,
        as_of: date | str,
        metrics: list[str] | None = None,
    ) -> list[dict]:
        """Get the latest fundamentals known as of `as_of` for a ticker.

        This is THE point-in-time read. It returns the most recent filing for
        each metric whose as_of_date <= as_of. No look-ahead possible.
        """
        identity = self._fundamental_identity_filter(ticker, as_of)
        if identity is None:
            return []
        identity_filter, identity_value = identity
        sql = f"""
            WITH ranked AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY metric
                           ORDER BY as_of_date DESC, period_end DESC
                       ) AS rn
                FROM fundamentals
                WHERE {identity_filter}
                  AND as_of_date <= CAST(? AS DATE)
                  AND period_end <= as_of_date
            )
            SELECT ticker, issuer_id, security_id, period_end, as_of_date, fiscal_period,
                   statement, metric, value, quarter_value, unit, source
            FROM ranked
            WHERE rn = 1
        """
        params: tuple[Any, ...] = (identity_value, str(as_of))
        if metrics:
            placeholders = ",".join("?" for _ in metrics)
            sql += f" AND metric IN ({placeholders})"
            params = (identity_value, str(as_of), *metrics)
        return self.query(sql, params)

    def fundamental_history(
        self,
        ticker: str,
        as_of: date | str,
        metric: str,
    ) -> list[dict]:
        """Return PIT-deduped metric history using issuer identity when verified."""
        identity = self._fundamental_identity_filter(ticker, as_of)
        if identity is None:
            return []
        identity_filter, identity_value = identity
        return self.query(
            f"""
            WITH ranked AS (
                SELECT period_end, fiscal_period, quarter_value,
                       ROW_NUMBER() OVER (
                           PARTITION BY period_end
                           ORDER BY as_of_date DESC
                       ) AS rn
                FROM fundamentals
                WHERE {identity_filter}
                  AND metric = ?
                  AND as_of_date <= CAST(? AS DATE)
                  AND period_end <= as_of_date
                  AND quarter_value IS NOT NULL
            )
            SELECT period_end, fiscal_period, quarter_value
            FROM ranked
            WHERE rn = 1
            ORDER BY period_end ASC
            """,
            (identity_value, metric, str(as_of)),
        )

    def pit_factor_fundamentals(
        self,
        ticker: str,
        as_of: date | str,
        metrics: list[str],
    ) -> list[dict]:
        """Return one PIT-deduped history snapshot for several factor metrics.

        This is the batched equivalent of repeatedly calling
        :meth:`pit_fundamentals` and :meth:`fundamental_history`. It preserves
        the same reviewed issuer/security routing and fail-closed identity-gap
        behavior, while collapsing all requested metrics for one ticker/date
        into a single DuckDB query. Callers must keep the result scoped to one
        immutable decision snapshot; this method does not persist a cache.
        """
        normalized_metrics = sorted({metric.strip() for metric in metrics if metric.strip()})
        if not normalized_metrics:
            return []
        identity = self._fundamental_identity_filter(ticker, as_of)
        if identity is None:
            return []
        identity_filter, identity_value = identity
        placeholders = ",".join("?" for _ in normalized_metrics)
        return self.query(
            f"""
            WITH ranked AS (
                SELECT metric, period_end, as_of_date, fiscal_period,
                       value, quarter_value,
                       ROW_NUMBER() OVER (
                           PARTITION BY metric, period_end
                           ORDER BY as_of_date DESC
                       ) AS period_rn
                FROM fundamentals
                WHERE {identity_filter}
                  AND metric IN ({placeholders})
                  AND as_of_date <= CAST(? AS DATE)
                  AND period_end <= as_of_date
            )
            SELECT metric, period_end, as_of_date, fiscal_period,
                   value, quarter_value
            FROM ranked
            WHERE period_rn = 1
            ORDER BY metric, period_end
            """,
            (identity_value, *normalized_metrics, str(as_of)),
        )

    def pit_macro_history(self, series_id: str, as_of: date | str) -> list[dict]:
        """Return the latest known vintage for every observation up to `as_of`.

        The observation date is also capped at `as_of`, so a macro release
        cannot introduce an observation from a future economic period into an
        earlier decision date. Rows migrated from the old schema have no
        release date and are intentionally ignored.
        """
        return self.query(
            """
            WITH ranked AS (
                SELECT series_id, date, release_date, value, unit, source,
                       ROW_NUMBER() OVER (
                           PARTITION BY series_id, date
                           ORDER BY release_date DESC, fetched_at DESC
                       ) AS rn
                FROM macro
                WHERE series_id = ?
                  AND release_date IS NOT NULL
                  AND release_date <= CAST(? AS DATE)
                  AND date <= CAST(? AS DATE)
            )
            SELECT series_id, date, release_date, value, unit, source
            FROM ranked
            WHERE rn = 1
            ORDER BY date
            """,
            (series_id.upper(), str(as_of), str(as_of)),
        )

    def pit_macro_latest(self, series_ids: list[str], as_of: date | str) -> list[dict]:
        """Return the latest known observation for each requested series."""
        normalized = [sid.upper() for sid in series_ids]
        if not normalized:
            return []
        placeholders = ",".join("?" for _ in normalized)
        return self.query(
            f"""
            WITH vintages AS (
                SELECT series_id, date, release_date, value, unit, source,
                       ROW_NUMBER() OVER (
                           PARTITION BY series_id, date
                           ORDER BY release_date DESC, fetched_at DESC
                       ) AS vintage_rn
                FROM macro
                WHERE series_id IN ({placeholders})
                  AND release_date IS NOT NULL
                  AND release_date <= CAST(? AS DATE)
                  AND date <= CAST(? AS DATE)
            ), latest AS (
                SELECT series_id, date, release_date, value, unit, source,
                       ROW_NUMBER() OVER (
                           PARTITION BY series_id
                           ORDER BY date DESC
                       ) AS observation_rn
                FROM vintages
                WHERE vintage_rn = 1
            )
            SELECT series_id, date, release_date, value, unit, source
            FROM latest
            WHERE observation_rn = 1
            ORDER BY series_id
            """,
            (*normalized, str(as_of), str(as_of)),
        )

    def latest_macro_release_date(self, series_id: str, source: str | None = None) -> date | None:
        """Return the newest stored release date for a macro series/source."""
        sql = "SELECT MAX(release_date) AS latest FROM macro WHERE series_id = ?"
        params: tuple[Any, ...] = (series_id.upper(),)
        if source:
            sql += " AND source = ?"
            params = (series_id.upper(), source)
        rows = self.query(sql, params)
        latest = rows[0]["latest"] if rows else None
        return latest if isinstance(latest, date) else None

    def price_on(self, ticker: str, on_date: date | str) -> dict | None:
        row = self.query(
            "SELECT * FROM prices WHERE ticker=? AND date=?",
            (ticker, str(on_date)),
        )
        return row[0] if row else None

    def latest_price_date(self, ticker: str) -> date | None:
        """Return the newest stored price date for a ticker, if any."""
        rows = self.query(
            "SELECT MAX(date) AS latest FROM prices WHERE ticker = ?",
            (ticker.upper(),),
        )
        latest = rows[0]["latest"] if rows else None
        return latest if isinstance(latest, date) else None

    def latest_security_price_date(self, security_id: str) -> date | None:
        """Return the newest identity-tagged price for one listed security."""
        rows = self.query(
            "SELECT MAX(date) AS latest FROM prices WHERE security_id = ?",
            (security_id,),
        )
        latest = rows[0]["latest"] if rows else None
        return latest if isinstance(latest, date) else None

    def latest_price(self, ticker: str, as_of: date | str) -> dict | None:
        """Return a PIT price, preferring verified security identity when available.

        Legacy ticker lookup remains the compatibility path for securities that
        have never had a reviewed provider-symbol mapping. Once any reviewed
        mapping exists, dates without an active verified mapping fail closed;
        when one is active, untagged ticker rows are intentionally ignored.
        """
        security_id = self.security_id_for_ticker(ticker, as_of)
        has_reviewed_mapping = False
        has_active_mapping = False
        if security_id is not None:
            mapping_state = self.query(
                """
                    SELECT COUNT(*) AS reviewed_count,
                           COUNT(*) FILTER (
                               WHERE mapping_status = 'verified'
                                 AND data_start <= CAST(? AS DATE)
                                 AND (data_end IS NULL OR data_end > CAST(? AS DATE))
                           ) AS active_count
                    FROM provider_symbol_history
                    WHERE security_id = ?
                """,
                (str(as_of), str(as_of), security_id),
            )[0]
            has_reviewed_mapping = mapping_state["reviewed_count"] > 0
            has_active_mapping = mapping_state["active_count"] > 0
        if has_reviewed_mapping and not has_active_mapping:
            return None
        if has_active_mapping:
            rows = self.query(
                """
                SELECT * FROM prices
                WHERE security_id = ? AND date <= CAST(? AS DATE)
                ORDER BY date DESC LIMIT 1
                """,
                (security_id, str(as_of)),
            )
        else:
            rows = self.query(
                """
                SELECT * FROM prices
                WHERE ticker = ? AND date <= CAST(? AS DATE)
                ORDER BY date DESC LIMIT 1
                """,
                (ticker.upper(), str(as_of)),
            )
        return rows[0] if rows else None

    def price_history(
        self,
        ticker: str,
        start: date | str | None = None,
        end: date | str | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM prices WHERE ticker = ?"
        params: list[Any] = [ticker]
        if start:
            sql += " AND date >= CAST(? AS DATE)"
            params.append(str(start))
        if end:
            sql += " AND date <= CAST(? AS DATE)"
            params.append(str(end))
        sql += " ORDER BY date"
        return self.query(sql, tuple(params))

    def universe_membership_on(self, universe_id: str, as_of: date | str) -> list[dict]:
        """Return members active and publicly known on the decision date."""
        return self.query(
            """
            SELECT universe_id, ticker, effective_start, effective_end,
                   security_id, known_date, source
            FROM universe_membership
            WHERE universe_id = ?
              AND known_date <= CAST(? AS DATE)
              AND effective_start <= CAST(? AS DATE)
              AND (effective_end IS NULL OR effective_end > CAST(? AS DATE))
            ORDER BY ticker
            """,
            (universe_id, str(as_of), str(as_of), str(as_of)),
        )

    def universe_data_coverage(self, universe_id: str, as_of: date | str) -> list[dict]:
        """Report PIT fundamentals and price availability for each active member.

        Reviewed identities use issuer-tagged fundamentals and security-tagged
        prices. Unreviewed names retain the legacy dated-ticker path. This
        avoids both false gaps after a real ticker change and false coverage
        from an unrelated company that previously reused the same symbol.
        """
        return self.query(
            """
            WITH decision AS (
                SELECT CAST(? AS DATE) AS as_of
            ), members AS (
                SELECT membership.universe_id, membership.ticker,
                       membership.security_id, decision.as_of
                FROM universe_membership AS membership
                CROSS JOIN decision
                WHERE membership.universe_id = ?
                  AND membership.known_date <= decision.as_of
                  AND membership.effective_start <= decision.as_of
                  AND (
                      membership.effective_end IS NULL
                      OR membership.effective_end > decision.as_of
                  )
            ), identified AS (
                SELECT members.*,
                       (
                           SELECT issuer_id
                           FROM security_issuer_assignments AS owner
                           WHERE owner.security_id = members.security_id
                             AND owner.effective_start <= members.as_of
                             AND (
                                 owner.effective_end IS NULL
                                 OR owner.effective_end > members.as_of
                             )
                           LIMIT 1
                       ) AS issuer_id,
                       EXISTS (
                           SELECT 1
                           FROM security_issuer_assignments AS owner
                           WHERE owner.security_id = members.security_id
                       ) AS has_reviewed_owner,
                       EXISTS (
                           SELECT 1
                           FROM provider_symbol_history AS mapping
                           WHERE mapping.security_id = members.security_id
                             AND mapping.mapping_status = 'verified'
                             AND mapping.data_start <= members.as_of
                             AND (
                                 mapping.data_end IS NULL
                                 OR mapping.data_end > members.as_of
                             )
                       ) AS has_provider_mapping,
                       EXISTS (
                           SELECT 1
                           FROM provider_symbol_history AS mapping
                           WHERE mapping.security_id = members.security_id
                       ) AS has_reviewed_provider_mapping
                FROM members
            )
            SELECT identified.universe_id, identified.ticker,
                   identified.security_id, identified.issuer_id,
                   security.identity_status,
                   CASE WHEN identified.has_provider_mapping THEN EXISTS (
                       SELECT 1 FROM prices
                       WHERE prices.security_id = identified.security_id
                         AND prices.date <= identified.as_of
                         AND prices.close IS NOT NULL
                   ) WHEN identified.has_reviewed_provider_mapping THEN FALSE
                   ELSE EXISTS (
                       SELECT 1 FROM prices
                       WHERE prices.ticker = identified.ticker
                         AND prices.date <= identified.as_of
                         AND prices.close IS NOT NULL
                   ) END AS has_price_history,
                   CASE WHEN identified.issuer_id IS NOT NULL THEN EXISTS (
                       SELECT 1 FROM fundamentals
                       WHERE fundamentals.issuer_id = identified.issuer_id
                         AND fundamentals.as_of_date <= identified.as_of
                         AND fundamentals.period_end <= fundamentals.as_of_date
                   ) WHEN identified.has_reviewed_owner THEN FALSE
                   ELSE EXISTS (
                       SELECT 1 FROM fundamentals
                       WHERE fundamentals.ticker = identified.ticker
                         AND fundamentals.as_of_date <= identified.as_of
                         AND fundamentals.period_end <= fundamentals.as_of_date
                   ) END AS has_pit_fundamentals,
                   CASE WHEN identified.has_provider_mapping THEN (
                       SELECT MAX(prices.date) FROM prices
                       WHERE prices.security_id = identified.security_id
                         AND prices.date <= identified.as_of
                   ) WHEN identified.has_reviewed_provider_mapping THEN CAST(NULL AS DATE)
                   ELSE (
                       SELECT MAX(prices.date) FROM prices
                       WHERE prices.ticker = identified.ticker
                         AND prices.date <= identified.as_of
                   ) END AS latest_price_date,
                   CASE WHEN identified.issuer_id IS NOT NULL THEN (
                       SELECT MAX(fundamentals.as_of_date) FROM fundamentals
                       WHERE fundamentals.issuer_id = identified.issuer_id
                         AND fundamentals.as_of_date <= identified.as_of
                         AND fundamentals.period_end <= fundamentals.as_of_date
                   ) WHEN identified.has_reviewed_owner THEN CAST(NULL AS DATE)
                   ELSE (
                       SELECT MAX(fundamentals.as_of_date) FROM fundamentals
                       WHERE fundamentals.ticker = identified.ticker
                         AND fundamentals.as_of_date <= identified.as_of
                         AND fundamentals.period_end <= fundamentals.as_of_date
                   ) END AS latest_fundamental_date
            FROM identified
            LEFT JOIN security_master AS security
              ON security.security_id = identified.security_id
            ORDER BY identified.ticker
            """,
            (str(as_of), universe_id),
        )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def table_rowcounts(self) -> dict[str, int]:
        out = {}
        for t in (
            "securities",
            "security_master",
            "security_identity_assignments",
            "issuer_master",
            "issuer_cik_history",
            "security_issuer_assignments",
            "provider_symbol_history",
            "prices",
            "fundamentals",
            "fundamentals_quarantine",
            "macro",
            "universe_membership",
        ):
            out[t] = self.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
        return out

    def close(self) -> None:
        self._con.close()


# ----------------------------------------------------------------------
# Module helpers
# ----------------------------------------------------------------------
def _resolve(p: Path | str) -> Path:
    p = Path(p)
    return p if p.is_absolute() else settings.project_root / p


def _as_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _required_text(row: dict, key: str, label: str) -> str:
    value = str(row.get(key) or "").strip()
    if not value:
        raise ValueError(f"{label} requires {key}")
    return value


def _half_open_dates(
    row: dict,
    start_key: str,
    end_key: str,
    label: str,
) -> tuple[date, date | None]:
    if not row.get(start_key):
        raise ValueError(f"{label} requires {start_key}")
    start = _as_date(row[start_key])
    end = _as_date(row[end_key]) if row.get(end_key) else None
    if end is not None and end <= start:
        raise ValueError(f"{label} {end_key} must follow {start_key}")
    return start, end


def _verified_date(row: dict, label: str) -> date:
    if not row.get("verified_date"):
        raise ValueError(f"{label} requires verified_date")
    verified = _as_date(row["verified_date"])
    if verified > date.today():
        raise ValueError(f"{label} verified_date cannot be in the future")
    return verified


def _rows_to_arrowable(rows: list[dict]) -> pd.DataFrame:
    """Normalize row dicts into a pandas DataFrame for DuckDB registration.

    DuckDB's replacement scan accepts DataFrames natively. We coerce
    datetime/date values to ISO strings so DuckDB can CAST them to DATE/
    TIMESTAMP in the INSERT statements. None values are preserved as NaN/None.
    """
    import pandas as pd

    out: list[dict] = []
    for r in rows:
        clean: dict[str, Any] = {}
        for k, v in r.items():
            if isinstance(v, datetime):
                clean[k] = v.isoformat(sep=" ")
            elif isinstance(v, date):
                clean[k] = v.isoformat()
            else:
                clean[k] = v
        out.append(clean)
    return pd.DataFrame(out) if out else pd.DataFrame()


def _to_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


@contextmanager
def store_scope() -> Iterator[Store]:
    """Context manager that yields a fresh Store and closes it after."""
    s = Store()
    try:
        yield s
    finally:
        s.close()

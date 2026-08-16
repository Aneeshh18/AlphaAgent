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

import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from pathlib import Path
from time import monotonic, sleep
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import duckdb
from structlog import get_logger

from aios.config import settings
from aios.price_provenance import (
    canonical_price_payload_hash,
    normalize_extension_price_row,
)
from aios.sec_rejections import (
    SEC_FUNDAMENTAL_REJECTION_CODES,
    accepted_sec_fundamental_outcome,
    canonical_rejection_codes,
    decode_rejection_codes,
)
from aios.storage.schema import MACRO_TABLE_SQL, SCHEMA_SQL

if TYPE_CHECKING:
    import pandas as pd

log = get_logger(__name__)
MACRO_LEGACY_PURGED_MIGRATION = "macro_legacy_active_copies_purged"
RAW_SNAPSHOT_REJECTION_EVIDENCE_MIGRATION = "raw_snapshot_rejection_evidence_v1"
FUNDAMENTAL_EVIDENCE_VERSIONS_MIGRATION = "fundamental_evidence_versions_v1"
UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION = "universe_constituent_change_activations_v1"
_RAW_SNAPSHOT_MAX_ORIGINAL_BYTES = 256 * 1024 * 1024
_RAW_SNAPSHOT_MAX_STORED_BYTES = 64 * 1024 * 1024

DatabaseFileIdentity = tuple[int, int, int, int, int, int]


def stable_database_file_identity(database_path: Path) -> DatabaseFileIdentity:
    """Return one no-follow identity for a regular, singly linked database."""

    database = Path(os.path.abspath(Path(database_path).expanduser()))
    for candidate in (database, *database.parents):
        if candidate.is_symlink():
            raise ValueError(f"database path cannot contain symbolic links: {database}")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(database, flags)
    try:
        opened = os.fstat(descriptor)
        current = database.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ValueError(f"database must be one regular unaliased file: {database}")
        return (
            opened.st_dev,
            opened.st_ino,
            opened.st_nlink,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class FundamentalEvidenceGeneration:
    """Named immutable system-time boundary for fundamental factor reads."""

    generation_id: str
    version_sequence: int
    purpose: str
    decision_date: str | None
    captured_at: str


def checkpoint_database_for_backup(database_path: Path) -> None:
    """Checkpoint an existing DuckDB without running application migrations.

    Backup is a preservation boundary. Opening the database through ``Store``
    would run additive schema migrations before the pre-migration snapshot was
    captured, defeating that boundary. This direct engine connection performs
    only the checkpoint needed for a consistent file copy.
    """
    requested = Path(database_path).expanduser()
    database = Path(os.path.abspath(requested))
    for candidate in (database, *database.parents):
        if candidate.is_symlink():
            raise ValueError(f"database path cannot contain symbolic links: {database}")
    if not database.is_file():
        raise ValueError(f"database does not exist or is unsafe: {database}")
    before = database.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError(f"database must be one regular, unaliased file: {database}")
    connection = duckdb.connect(str(database), read_only=False)
    try:
        connection.execute("CHECKPOINT")
    finally:
        connection.close()
    after = database.stat()
    if (
        not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise RuntimeError("database identity changed during backup checkpoint")


class Store:
    """Thin wrapper around a DuckDB connection."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        read_only: bool = False,
        lock_wait_seconds: float | None = None,
        allow_schema_upgrade: bool = False,
    ) -> None:
        self.db_path = db_path or _resolve(settings.duckdb_path)
        self.read_only = read_only
        self.database_file_identity: DatabaseFileIdentity | None = None
        self.allow_schema_upgrade = bool(allow_schema_upgrade)
        if read_only and self.allow_schema_upgrade:
            raise ValueError("a read-only Store cannot allow schema upgrades")
        if read_only:
            if not self.db_path.is_file():
                raise FileNotFoundError(f"DuckDB database does not exist: {self.db_path}")
            connection_identity = stable_database_file_identity(self.db_path)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        wait_seconds = (
            settings.duckdb_lock_wait_seconds
            if lock_wait_seconds is None
            else float(lock_wait_seconds)
        )
        if wait_seconds < 0:
            raise ValueError("lock_wait_seconds cannot be negative")
        self._con = _connect_with_lock_wait(
            self.db_path,
            read_only=read_only,
            wait_seconds=wait_seconds,
        )
        # Every persisted TIMESTAMP is interpreted as a UTC wall time. Pin the
        # session so SQL defaults and timezone-aware Python parameters cannot
        # drift with the host's local timezone.
        self._con.execute("SET TimeZone = 'UTC'")
        if read_only:
            try:
                opened_identity = stable_database_file_identity(self.db_path)
                if opened_identity != connection_identity:
                    raise RuntimeError("DuckDB database identity changed while it was opened")
                self.database_file_identity = opened_identity
            except Exception:
                self._con.close()
                raise
        else:
            try:
                self._init_schema()
            except Exception:
                self._con.close()
                raise

    def _init_schema(self) -> None:
        """Run all CREATE TABLE IF NOT EXISTS statements."""
        self._preflight_universe_constituent_change_activation_schema()
        self._con.execute(SCHEMA_SQL)
        self._migrate_ingest_subject_schema()
        self._migrate_ingest_rejection_schema()
        self._migrate_raw_snapshot_rejection_schema()
        self._migrate_macro_schema()
        self._migrate_security_identity_schema()
        self._migrate_reference_identity_columns()
        self._migrate_fundamental_provenance_schema()
        self._migrate_fundamental_evidence_versions()
        self._migrate_price_action_schema()
        self._migrate_universe_constituent_change_activation_schema(
            allow_marker_insert=(
                self.allow_schema_upgrade or self._universe_change_bootstrap_allowed
            )
        )
        log.info("schema.initialized", db=str(self.db_path))

    def _preflight_universe_constituent_change_activation_schema(self) -> None:
        """Reject receipt loss or unmarked rows before additive DDL can hide it."""

        tables = {
            str(row["table_name"])
            for row in self.query(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'main'
                """
            )
        }
        self._universe_change_bootstrap_allowed = not tables
        table_exists = "universe_constituent_change_activations" in tables
        marker_exists = False
        if "schema_migrations" in tables:
            marker_exists = bool(
                self.query(
                    "SELECT name FROM schema_migrations WHERE name = ?",
                    (UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,),
                )
            )
        if marker_exists and not table_exists:
            raise RuntimeError(
                "Universe constituent-change activation receipt table is missing "
                "after its migration marker was recorded."
            )
        if tables and not table_exists and not marker_exists and not self.allow_schema_upgrade:
            raise RuntimeError(
                "Universe constituent-change activation capability requires the "
                "backup-first upgrade-local-state workflow."
            )
        if table_exists and not marker_exists:
            row_count = int(
                self.query("SELECT COUNT(*) AS n FROM universe_constituent_change_activations")[0][
                    "n"
                ]
            )
            if row_count:
                raise RuntimeError(
                    "Universe constituent-change activation rows exist without a migration marker."
                )

    def require_universe_change_activation_schema(self) -> None:
        """Verify this database can safely plan or apply constituent changes.

        This check is intentionally side-effect-free so a read-only planning
        process cannot auto-migrate a database behind the operator's back.
        """

        tables = {
            str(row["table_name"])
            for row in self.query(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'main'
                """
            )
        }
        if not {
            "schema_migrations",
            "universe_constituent_change_activations",
        }.issubset(tables):
            raise RuntimeError(
                "Universe constituent-change activation capability is not installed; "
                "run the backup-first upgrade-local-state workflow."
            )
        self._preflight_universe_constituent_change_activation_schema()
        self._migrate_universe_constituent_change_activation_schema(allow_marker_insert=False)

    def _migrate_universe_constituent_change_activation_schema(
        self,
        *,
        allow_marker_insert: bool = True,
    ) -> None:
        """Certify the append-only constituent-change receipt capability.

        ``CREATE TABLE IF NOT EXISTS`` cannot distinguish our reviewed table
        from a partial table left by an interrupted or foreign migration.  A
        constituent activation is too sensitive to accept that ambiguity, so
        writable startup validates the complete shape and constraints before
        recording one durable capability marker.
        """

        expected_columns = (
            ("activation_id", "VARCHAR", "NO"),
            ("event_id", "VARCHAR", "NO"),
            ("plan_sha256", "VARCHAR", "NO"),
            ("activation_payload_sha256", "VARCHAR", "NO"),
            ("activation_run_id", "VARCHAR", "NO"),
            ("fundamental_run_id", "VARCHAR", "NO"),
            ("price_run_id", "VARCHAR", "NO"),
            ("source_attestation_id", "VARCHAR", "NO"),
            ("schema_version", "INTEGER", "NO"),
            ("universe_id", "VARCHAR", "NO"),
            ("announcement_date", "DATE", "NO"),
            ("effective_date", "DATE", "NO"),
            ("prior_coverage_through", "DATE", "NO"),
            ("target_coverage_through", "DATE", "NO"),
            ("official_detail_snapshot_id", "VARCHAR", "NO"),
            ("component_snapshot_id", "VARCHAR", "NO"),
            ("before_member_set_sha256", "VARCHAR", "NO"),
            ("after_member_set_sha256", "VARCHAR", "NO"),
            ("before_state_sha256", "VARCHAR", "NO"),
            ("after_state_sha256", "VARCHAR", "NO"),
            ("change_rows_sha256", "VARCHAR", "NO"),
            ("activation_payload_json", "VARCHAR", "NO"),
            ("backup_manifest_sha256", "VARCHAR", "NO"),
            ("actor", "VARCHAR", "NO"),
            ("policy_version", "VARCHAR", "NO"),
            ("counts_json", "VARCHAR", "NO"),
            ("activated_at", "TIMESTAMP", "NO"),
            ("status", "VARCHAR", "NO"),
            ("created_at", "TIMESTAMP", "YES"),
        )
        described = tuple(
            (str(row["column_name"]), str(row["column_type"]), str(row["null"]))
            for row in self.query("DESCRIBE universe_constituent_change_activations")
        )
        if described != expected_columns:
            raise RuntimeError(
                "Universe constituent-change activation schema is incomplete or unsupported."
            )

        constraints = self.query(
            """
            SELECT constraint_type, constraint_column_names, expression
            FROM duckdb_constraints()
            WHERE schema_name = 'main'
              AND table_name = 'universe_constituent_change_activations'
            """
        )
        key_constraints = {
            (
                str(row["constraint_type"]),
                tuple(str(column) for column in row["constraint_column_names"]),
            )
            for row in constraints
            if row["constraint_type"] in {"PRIMARY KEY", "UNIQUE"}
        }
        required_keys = {
            ("PRIMARY KEY", ("activation_id",)),
            ("UNIQUE", ("event_id",)),
            ("UNIQUE", ("plan_sha256",)),
            ("UNIQUE", ("activation_payload_sha256",)),
            ("UNIQUE", ("activation_run_id",)),
            ("UNIQUE", ("fundamental_run_id",)),
            ("UNIQUE", ("price_run_id",)),
        }
        check_expressions = {
            "".join(str(row["expression"] or "").split()).casefold()
            for row in constraints
            if row["constraint_type"] == "CHECK"
        }
        required_checks = {
            "(schema_version=1)",
            "(status='accepted')",
            "(announcement_date<=effective_date)",
            "(prior_coverage_through<effective_date)",
            "(effective_date<=target_coverage_through)",
            "regexp_full_match(plan_sha256,'^[0-9a-f]{64}$')",
            "regexp_full_match(activation_payload_sha256,'^[0-9a-f]{64}$')",
            "regexp_full_match(before_member_set_sha256,'^[0-9a-f]{64}$')",
            "regexp_full_match(after_member_set_sha256,'^[0-9a-f]{64}$')",
            "regexp_full_match(before_state_sha256,'^[0-9a-f]{64}$')",
            "regexp_full_match(after_state_sha256,'^[0-9a-f]{64}$')",
            "regexp_full_match(change_rows_sha256,'^[0-9a-f]{64}$')",
            "regexp_full_match(backup_manifest_sha256,'^[0-9a-f]{64}$')",
            '(length(main."trim"(actor))>0)',
            '(length(main."trim"(policy_version))>0)',
        }
        if key_constraints != required_keys or check_expressions != required_checks:
            raise RuntimeError("Universe constituent-change activation constraints are incomplete.")

        marker = self.query(
            """
            SELECT applied_at
            FROM schema_migrations WHERE name = ?
            """,
            (UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,),
        )
        row_count = int(
            self.query("SELECT COUNT(*) AS n FROM universe_constituent_change_activations")[0]["n"]
        )
        if marker:
            if (
                len(marker) != 1
                or not isinstance(marker[0]["applied_at"], datetime)
                or marker[0]["applied_at"]
                > datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=18)
            ):
                raise RuntimeError(
                    "Universe constituent-change activation migration marker is invalid."
                )
            if row_count:
                # Import lazily so the storage boundary remains the only module
                # that opens DuckDB while the activation policy can still
                # validate every append-only receipt at startup.
                from aios.universe_change_activation import (
                    verify_universe_change_activation_receipts,
                )

                verify_universe_change_activation_receipts(self)
            return
        if row_count:
            raise RuntimeError(
                "Universe constituent-change activation rows exist without a migration marker."
            )
        if not allow_marker_insert:
            raise RuntimeError(
                "Universe constituent-change activation capability is not certified; "
                "run the backup-first upgrade-local-state workflow."
            )

        self.execute("BEGIN TRANSACTION")
        try:
            self.execute(
                "INSERT INTO schema_migrations (name) VALUES (?)",
                (UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,),
            )
            self.execute("COMMIT")
        except Exception:
            self.execute("ROLLBACK")
            raise

    def _migrate_ingest_subject_schema(self) -> None:
        """Add optional subject identity without rewriting legacy ingest rows."""
        columns = {row["column_name"] for row in self.query("DESCRIBE ingest_log")}
        present = {"subject_type", "subject_id"} & columns
        if present == {"subject_type", "subject_id"}:
            return
        if present:
            raise RuntimeError(
                "Ingest subject migration is incomplete: subject_type and "
                "subject_id must be added together."
            )
        self.execute("BEGIN TRANSACTION")
        try:
            self.execute("ALTER TABLE ingest_log ADD COLUMN subject_type VARCHAR")
            self.execute("ALTER TABLE ingest_log ADD COLUMN subject_id VARCHAR")
            self.execute("COMMIT")
        except Exception:
            self.execute("ROLLBACK")
            raise

    def _migrate_ingest_rejection_schema(self) -> None:
        """Add structured rejection evidence without rewriting old outcomes."""
        columns = {row["column_name"] for row in self.query("DESCRIBE ingest_log")}
        if "rejection_codes" not in columns:
            self.execute("ALTER TABLE ingest_log ADD COLUMN rejection_codes VARCHAR")

    def _migrate_raw_snapshot_rejection_schema(self) -> None:
        """Add and replay-backfill parser rejection evidence."""

        columns = {row["column_name"] for row in self.query("DESCRIBE raw_snapshots")}
        expected = {
            "parsed_rows_rejected": "BIGINT",
            "parsed_rejection_codes": "VARCHAR",
        }
        present = set(expected) & columns
        if present and present != set(expected):
            raise RuntimeError(
                "Raw snapshot rejection migration is incomplete: rejection "
                "count and codes must be added together."
            )
        self.execute("BEGIN TRANSACTION")
        try:
            if not present:
                for column, data_type in expected.items():
                    self.execute(f"ALTER TABLE raw_snapshots ADD COLUMN {column} {data_type}")
            self._backfill_raw_snapshot_rejection_evidence()
            self.execute(
                """
                INSERT INTO schema_migrations (name)
                VALUES (?)
                ON CONFLICT (name) DO NOTHING
                """,
                (RAW_SNAPSHOT_REJECTION_EVIDENCE_MIGRATION,),
            )
            self.execute("COMMIT")
        except Exception:
            self.execute("ROLLBACK")
            raise

    def _backfill_raw_snapshot_rejection_evidence(self) -> None:
        """Replay exact historical Company Facts bytes before certifying migration."""

        candidates = self.query(
            """
            SELECT snapshot.snapshot_id, snapshot.parser_version,
                   snapshot.parsed_row_count, snapshot.parsed_rows_sha256,
                   snapshot.parsed_rows_rejected,
                   snapshot.parsed_rejection_codes,
                   payload.payload_sha256, payload.relative_path,
                   payload.original_bytes, payload.stored_bytes,
                   payload.compression
            FROM raw_snapshots AS snapshot
            JOIN raw_payloads AS payload USING (payload_sha256)
            WHERE snapshot.provider = 'sec-edgar'
              AND snapshot.dataset = 'companyfacts'
              AND snapshot.parser_version IN (
                  'sec-companyfacts-v2',
                  'sec-companyfacts-v2-storage-safe-v1',
                  'sec-companyfacts-v2-storage-safe-v2',
                  'sec-companyfacts-v3',
                  'sec-companyfacts-v4'
              )
              AND snapshot.parsed_row_count IS NOT NULL
            ORDER BY snapshot.snapshot_id
            """
        )
        missing = [
            row
            for row in candidates
            if row["parsed_rows_rejected"] is None
            or (int(row["parsed_rows_rejected"]) > 0 and row["parsed_rejection_codes"] is None)
            or (int(row["parsed_rows_rejected"]) == 0 and row["parsed_rejection_codes"] is not None)
        ]
        if not missing:
            return

        project_root = self._raw_snapshot_project_root()
        if project_root is None:
            raise RuntimeError(
                "Historical SEC rejection evidence requires a database under "
                "the project data directory or the configured live database."
            )
        from aios.ingest.edgar import replay_sec_companyfacts_response
        from aios.raw_snapshots import (
            _read_verified_payload,
            _resolve_raw_root,
            canonical_parsed_rows_sha256,
        )

        raw_root = _resolve_raw_root(project_root)
        for row in missing:
            payload, _stored_bytes = _read_verified_payload(
                project_root,
                raw_root,
                row,
            )
            parsed_rows, metadata = replay_sec_companyfacts_response(
                payload,
                parser_version=str(row["parser_version"]),
            )
            if (
                len(parsed_rows) != int(row["parsed_row_count"])
                or canonical_parsed_rows_sha256(parsed_rows) != row["parsed_rows_sha256"]
            ):
                raise RuntimeError(
                    "Historical SEC snapshot replay does not match its stored "
                    f"parsed evidence: {row['snapshot_id']}"
                )
            rejected = int(metadata["rows_rejected"])
            rejection_codes = canonical_rejection_codes(metadata["rejection_codes"])
            self.execute(
                """
                UPDATE raw_snapshots
                SET parsed_rows_rejected = ?,
                    parsed_rejection_codes = ?
                WHERE snapshot_id = ?
                """,
                (rejected, rejection_codes, row["snapshot_id"]),
            )

    def _raw_snapshot_project_root(self) -> Path | None:
        configured_db = _resolve(settings.duckdb_path).resolve()
        database = self.db_path.resolve()
        if database == configured_db:
            return settings.project_root.resolve()
        if database.parent.name == "data":
            return database.parent.parent
        return None

    def _migrate_security_identity_schema(self) -> None:
        """Add the stable identity link to databases created before this layer."""
        columns = {row["column_name"] for row in self.query("DESCRIBE universe_membership")}
        if "security_id" not in columns:
            self.execute("ALTER TABLE universe_membership ADD COLUMN security_id VARCHAR")
        if "end_known_date" not in columns:
            self.execute("ALTER TABLE universe_membership ADD COLUMN end_known_date DATE")

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

    def _migrate_fundamental_provenance_schema(self) -> None:
        """Add nullable SEC row lineage without claiming provenance for old data."""
        expected = {
            "ingest_run_id": "VARCHAR",
            "source_snapshot_id": "VARCHAR",
            "source_rowset_sha256": "VARCHAR",
            "source_row_sha256": "VARCHAR",
            "source_fact_locator": "VARCHAR",
        }
        for table in ("fundamentals", "fundamentals_quarantine"):
            columns = {row["column_name"] for row in self.query(f"DESCRIBE {table}")}
            for column, data_type in expected.items():
                if column not in columns:
                    self.execute(f"ALTER TABLE {table} ADD COLUMN {column} {data_type}")

    def _migrate_fundamental_evidence_versions(self) -> None:
        """Seed one append-only system-time version for every current fact."""

        expected = {
            "version_sequence",
            "ticker",
            "issuer_id",
            "security_id",
            "ingest_run_id",
            "source_snapshot_id",
            "source_rowset_sha256",
            "source_row_sha256",
            "source_fact_locator",
            "period_end",
            "as_of_date",
            "fiscal_period",
            "statement",
            "metric",
            "value",
            "quarter_value",
            "unit",
            "source",
            "recorded_at",
            "is_deleted",
        }
        columns = {row["column_name"] for row in self.query("DESCRIBE fundamental_versions")}
        if not expected <= columns:
            missing = sorted(expected - columns)
            raise RuntimeError(
                "Fundamental evidence version schema is incomplete: " + ", ".join(missing)
            )
        marked = bool(
            self.query(
                "SELECT COUNT(*) AS n FROM schema_migrations WHERE name = ?",
                (FUNDAMENTAL_EVIDENCE_VERSIONS_MIGRATION,),
            )[0]["n"]
        )
        version_count = int(self.query("SELECT COUNT(*) AS n FROM fundamental_versions")[0]["n"])
        if not marked:
            if version_count:
                raise RuntimeError(
                    "Fundamental evidence version migration is unmarked but non-empty"
                )
            self.execute("BEGIN TRANSACTION")
            try:
                self.execute(
                    """
                    INSERT INTO fundamental_versions
                    (ticker, issuer_id, security_id, ingest_run_id,
                     source_snapshot_id, source_rowset_sha256,
                     source_row_sha256, source_fact_locator, period_end,
                     as_of_date, fiscal_period, statement, metric, value,
                     quarter_value, unit, source, recorded_at, is_deleted)
                    SELECT ticker, issuer_id, security_id, ingest_run_id,
                           source_snapshot_id, source_rowset_sha256,
                           source_row_sha256, source_fact_locator, period_end,
                           as_of_date, fiscal_period, statement, metric, value,
                           quarter_value, unit, source, fetched_at, FALSE
                    FROM fundamentals
                    ORDER BY ticker, period_end, as_of_date, metric
                    """
                )
                self.execute(
                    """
                    INSERT INTO schema_migrations (name)
                    VALUES (?)
                    ON CONFLICT (name) DO NOTHING
                    """,
                    (FUNDAMENTAL_EVIDENCE_VERSIONS_MIGRATION,),
                )
                self.execute("COMMIT")
            except Exception:
                self.execute("ROLLBACK")
                raise
        unmatched = self._fundamental_projection_version_mismatch_count()
        if unmatched:
            raise RuntimeError(
                "Latest fundamental evidence versions do not reconstruct the current projection"
            )

    def _fundamental_projection_version_mismatch_count(self) -> int:
        """Count differences between latest versions and the current projection.

        The latest version for each economic key is authoritative.  A deleted
        latest version must have no current row; a non-deleted latest version
        must reproduce the current row exactly.  This symmetric check catches
        missing tombstones, stale versions, and unversioned current rows.
        """

        return int(
            self.query(
                """
                WITH ranked_versions AS (
                    SELECT version.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY ticker, period_end, as_of_date, metric
                               ORDER BY version_sequence DESC
                           ) AS projection_rank
                    FROM fundamental_versions AS version
                ), latest_versions AS (
                    SELECT * EXCLUDE (projection_rank)
                    FROM ranked_versions
                    WHERE projection_rank = 1
                )
                SELECT COUNT(*) AS n
                FROM latest_versions AS latest
                FULL OUTER JOIN fundamentals AS current
                  ON latest.ticker = current.ticker
                 AND latest.period_end = current.period_end
                 AND latest.as_of_date = current.as_of_date
                 AND latest.metric = current.metric
                WHERE latest.version_sequence IS NULL
                   OR (current.ticker IS NULL AND latest.is_deleted = FALSE)
                   OR (current.ticker IS NOT NULL AND latest.is_deleted = TRUE)
                   OR (
                       current.ticker IS NOT NULL
                       AND latest.is_deleted = FALSE
                       AND (
                           latest.issuer_id IS DISTINCT FROM current.issuer_id
                           OR latest.security_id IS DISTINCT FROM current.security_id
                           OR latest.ingest_run_id IS DISTINCT FROM current.ingest_run_id
                           OR latest.source_snapshot_id IS DISTINCT FROM
                               current.source_snapshot_id
                           OR latest.source_rowset_sha256 IS DISTINCT FROM
                               current.source_rowset_sha256
                           OR latest.source_row_sha256 IS DISTINCT FROM
                               current.source_row_sha256
                           OR latest.source_fact_locator IS DISTINCT FROM
                               current.source_fact_locator
                           OR latest.fiscal_period IS DISTINCT FROM current.fiscal_period
                           OR latest.statement IS DISTINCT FROM current.statement
                           OR latest.value IS DISTINCT FROM current.value
                           OR latest.quarter_value IS DISTINCT FROM current.quarter_value
                           OR latest.unit IS DISTINCT FROM current.unit
                           OR latest.source IS DISTINCT FROM current.source
                       )
                   )
                """
            )[0]["n"]
        )

    def _migrate_price_action_schema(self) -> None:
        """Track whether a provider response actually included action fields.

        Older yfinance downloads used its ``actions=False`` default, so their
        zero dividends and unit split ratios are unknown rather than verified
        zeroes. Tiingo has always returned explicit action fields in this
        project and can be safely marked complete during the additive upgrade.
        """
        columns = {row["column_name"] for row in self.query("DESCRIBE prices")}
        if "actions_complete" not in columns:
            self.execute("ALTER TABLE prices ADD COLUMN actions_complete BOOLEAN DEFAULT FALSE")
            self.execute("UPDATE prices SET actions_complete = TRUE WHERE source = 'tiingo'")
        if "close_split_adjusted" not in columns:
            self.execute("ALTER TABLE prices ADD COLUMN close_split_adjusted BOOLEAN")
            self.execute(
                """
                UPDATE prices
                SET close_split_adjusted = CASE
                    WHEN source = 'yfinance' THEN TRUE
                    WHEN source IN ('tiingo', 'stooq', 'test') THEN FALSE
                    ELSE NULL
                END
                """
            )
        columns = {row["column_name"] for row in self.query("DESCRIBE prices")}
        if "split_normalization_factor" not in columns:
            self.execute("ALTER TABLE prices ADD COLUMN split_normalization_factor DOUBLE")
            self.execute(
                """
                UPDATE prices
                SET split_normalization_factor = 1.0
                WHERE close_split_adjusted IS FALSE
                """
            )
        if "split_normalization_through" not in columns:
            self.execute("ALTER TABLE prices ADD COLUMN split_normalization_through DATE")

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
        try:
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
            return int(n)
        finally:
            self._con.unregister("_tmp_sec")

    def upsert_universe_membership(self, rows: list[dict]) -> int:
        """Insert point-in-time universe intervals.

        Intervals are half-open: ``[effective_start, effective_end)``. The
        ``known_date`` protects the start and ``end_known_date`` independently
        protects a finite end. Legacy callers that omit ``end_known_date`` are
        declaring that the whole supplied interval was known at ``known_date``.
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
            end_known_date = (
                _as_date(row["end_known_date"])
                if row.get("end_known_date")
                else (known_date if effective_end is not None else None)
            )
            if known_date > effective_start:
                raise ValueError("universe membership known_date cannot follow effective_start")
            if effective_end is not None and effective_end <= effective_start:
                raise ValueError("universe membership effective_end must follow its start")
            if end_known_date is not None and effective_end is None:
                raise ValueError("open universe membership cannot have end_known_date")
            if end_known_date is not None and (
                end_known_date < known_date or end_known_date > effective_end
            ):
                raise ValueError(
                    "universe membership end_known_date must fall between its "
                    "start known_date and effective_end"
                )
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
                    "end_known_date": end_known_date,
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
                 known_date, end_known_date, source, fetched_at)
                SELECT universe_id, ticker, security_id, CAST(effective_start AS DATE),
                       CAST(effective_end AS DATE), CAST(known_date AS DATE),
                       CAST(end_known_date AS DATE), source, now()
                FROM _tmp_universe
                ON CONFLICT (universe_id, ticker, effective_start) DO UPDATE
                SET security_id = COALESCE(
                        EXCLUDED.security_id, universe_membership.security_id
                    ),
                    effective_end = EXCLUDED.effective_end,
                    known_date = EXCLUDED.known_date,
                    end_known_date = EXCLUDED.end_known_date,
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

    def upsert_security_conversions(self, rows: list[dict]) -> int:
        """Atomically import reviewed share-for-share security conversions.

        A conversion is not a ticker alias. It terminates one immutable
        security position and creates another at an explicitly sourced share
        ratio. Only reviewed carry-over-basis events are supported here; cash
        mergers require a separate, jurisdiction-aware accounting policy.
        """
        if not rows:
            raise ValueError("security conversion import requires at least one row")

        clean: list[dict] = []
        seen_sources: set[str] = set()
        for row in rows:
            source_security_id = _required_text(row, "source_security_id", "security conversion")
            target_security_id = _required_text(row, "target_security_id", "security conversion")
            if source_security_id == target_security_id:
                raise ValueError("security conversion cannot target the source security")
            if source_security_id in seen_sources:
                raise ValueError(f"duplicate security conversion for {source_security_id!r}")
            seen_sources.add(source_security_id)
            if not row.get("effective_date") or not row.get("known_date"):
                raise ValueError("security conversion requires effective_date and known_date")
            effective_date = _as_date(row["effective_date"])
            known_date = _as_date(row["known_date"])
            if known_date > effective_date:
                raise ValueError("security conversion known_date cannot follow effective_date")
            try:
                share_ratio = float(row["share_ratio"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("security conversion requires a numeric share_ratio") from exc
            if not isfinite(share_ratio) or share_ratio <= 0:
                raise ValueError("security conversion share_ratio must be finite and positive")
            basis_policy = _required_text(row, "basis_policy", "security conversion")
            if basis_policy != "carryover":
                raise ValueError(f"unsupported security conversion basis_policy {basis_policy!r}")
            review_status = _required_text(row, "review_status", "security conversion")
            if review_status != "verified":
                raise ValueError(f"unsupported security conversion review_status {review_status!r}")
            clean.append(
                {
                    "source_security_id": source_security_id,
                    "target_security_id": target_security_id,
                    "effective_date": effective_date,
                    "known_date": known_date,
                    "share_ratio": share_ratio,
                    "basis_policy": basis_policy,
                    "review_status": review_status,
                    "verified_date": _verified_date(row, "security conversion"),
                    "source": _required_text(row, "source", "security conversion"),
                    "basis_source": _required_text(row, "basis_source", "security conversion"),
                }
            )

        self._con.register("_tmp_security_conversion", _rows_to_arrowable(clean))
        self.execute("BEGIN TRANSACTION")
        try:
            orphan = self.query(
                """
                WITH referenced AS (
                    SELECT source_security_id AS security_id
                    FROM _tmp_security_conversion
                    UNION
                    SELECT target_security_id AS security_id
                    FROM _tmp_security_conversion
                )
                SELECT referenced.security_id
                FROM referenced
                LEFT JOIN security_master AS security USING (security_id)
                WHERE security.security_id IS NULL
                LIMIT 1
                """
            )
            if orphan:
                raise ValueError(
                    f"security conversion uses unknown security_id {orphan[0]['security_id']!r}"
                )
            conflict = self.query(
                """
                SELECT existing.source_security_id
                FROM security_conversions AS existing
                JOIN _tmp_security_conversion AS incoming
                  ON incoming.source_security_id = existing.source_security_id
                WHERE existing.target_security_id <> incoming.target_security_id
                   OR existing.effective_date <> CAST(incoming.effective_date AS DATE)
                   OR existing.known_date <> CAST(incoming.known_date AS DATE)
                   OR existing.share_ratio <> incoming.share_ratio
                   OR existing.basis_policy <> incoming.basis_policy
                   OR existing.review_status <> incoming.review_status
                   OR existing.source <> incoming.source
                   OR existing.basis_source <> incoming.basis_source
                LIMIT 1
                """
            )
            if conflict:
                raise ValueError("security conversion conflicts with existing provenance")

            count = self.execute(
                """
                INSERT INTO security_conversions
                (source_security_id, target_security_id, effective_date,
                 known_date, share_ratio, basis_policy, review_status,
                 verified_date, source, basis_source, fetched_at)
                SELECT source_security_id, target_security_id,
                       CAST(effective_date AS DATE), CAST(known_date AS DATE),
                       share_ratio, basis_policy, review_status,
                       CAST(verified_date AS DATE), source, basis_source, now()
                FROM _tmp_security_conversion
                ON CONFLICT (source_security_id) DO UPDATE
                SET verified_date = EXCLUDED.verified_date,
                    fetched_at = EXCLUDED.fetched_at
                """
            ).fetchone()[0]

            edges = {
                row["source_security_id"]: row["target_security_id"]
                for row in self.query(
                    """
                    SELECT source_security_id, target_security_id
                    FROM security_conversions
                    """
                )
            }
            for origin in edges:
                visited: set[str] = set()
                current = origin
                while current in edges:
                    if current in visited:
                        raise ValueError("security conversion graph contains a cycle")
                    visited.add(current)
                    current = edges[current]

            self.execute("COMMIT")
            return int(count)
        except Exception:
            self.execute("ROLLBACK")
            raise
        finally:
            self._con.unregister("_tmp_security_conversion")

    def upsert_liquidation_price_extensions(
        self,
        rows: list[dict],
        price_rows: list[dict],
    ) -> dict[str, int]:
        """Atomically add short post-membership ticker paths and their prices."""
        if not rows or not price_rows:
            raise ValueError("liquidation extensions require provenance and price rows")
        normalized_extensions: list[dict] = []
        seen_provenance: set[str] = set()
        for row in rows:
            provenance_id = _required_text(row, "provenance_id", "liquidation extension")
            if provenance_id in seen_provenance:
                raise ValueError(f"duplicate liquidation provenance {provenance_id!r}")
            seen_provenance.add(provenance_id)
            start, end = _half_open_dates(
                row,
                "data_start",
                "data_end",
                "liquidation extension",
            )
            if end is None:
                raise ValueError("liquidation extension requires a finite data_end")
            if (end - start).days > 45:
                raise ValueError("liquidation extension cannot exceed 45 calendar days")
            purpose = _required_text(row, "purpose", "liquidation extension")
            if purpose != "portfolio_liquidation":
                raise ValueError(f"unsupported liquidation purpose {purpose!r}")
            review_policy = _required_text(row, "review_policy", "liquidation extension")
            if review_policy != "adjacent_identity_provider_v1":
                raise ValueError(f"unsupported liquidation review policy {review_policy!r}")
            payload_sha256 = _required_text(row, "payload_sha256", "liquidation extension")
            if len(payload_sha256) != 64 or any(
                character not in "0123456789abcdef" for character in payload_sha256
            ):
                raise ValueError("liquidation extension has an invalid payload hash")
            normalized_extensions.append(
                {
                    "provenance_id": provenance_id,
                    "universe_id": _required_text(row, "universe_id", "liquidation extension"),
                    "security_id": _required_text(row, "security_id", "liquidation extension"),
                    "ticker": _required_text(row, "ticker", "liquidation extension").upper(),
                    "provider": _required_text(row, "provider", "liquidation extension").lower(),
                    "provider_symbol": _required_text(
                        row, "provider_symbol", "liquidation extension"
                    ).upper(),
                    "data_start": start,
                    "data_end": end,
                    "verified_date": _verified_date(row, "liquidation extension"),
                    "identity_source": _required_text(
                        row, "identity_source", "liquidation extension"
                    ),
                    "provider_source": _required_text(
                        row, "provider_source", "liquidation extension"
                    ),
                    "payload_sha256": payload_sha256,
                    "purpose": purpose,
                    "review_policy": review_policy,
                }
            )

        normalized_prices = [normalize_extension_price_row(row) for row in price_rows]
        extensions_by_id = {row["provenance_id"]: row for row in normalized_extensions}
        prices_by_id: dict[str, list[dict]] = {}
        seen_prices: set[tuple[str, str]] = set()
        for row in normalized_prices:
            provenance_id = row["provenance_id"]
            extension = extensions_by_id.get(provenance_id)
            if extension is None:
                raise ValueError("liquidation price references unknown provenance")
            key = (provenance_id, row["date"])
            if key in seen_prices:
                raise ValueError(f"duplicate liquidation price {provenance_id}@{row['date']}")
            seen_prices.add(key)
            row_date = _as_date(row["date"])
            if not extension["data_start"] <= row_date < extension["data_end"]:
                raise ValueError("liquidation price falls outside its reviewed window")
            for field in ("security_id", "ticker", "provider_symbol"):
                if row[field] != extension[field]:
                    raise ValueError(f"liquidation price {field} disagrees with provenance")
            if row["source"] != extension["provider"]:
                raise ValueError("liquidation price provider disagrees with provenance")
            prices_by_id.setdefault(provenance_id, []).append(row)
        for provenance_id, extension in extensions_by_id.items():
            payload = prices_by_id.get(provenance_id, [])
            if not payload:
                raise ValueError(f"liquidation provenance {provenance_id!r} has no prices")
            if canonical_price_payload_hash(payload) != extension["payload_sha256"]:
                raise ValueError("liquidation price payload hash mismatch")

        self._con.register(
            "_tmp_liquidation_extension",
            _rows_to_arrowable(normalized_extensions),
        )
        self._con.register("_tmp_liquidation_prices", _rows_to_arrowable(normalized_prices))
        self.execute("BEGIN TRANSACTION")
        try:
            invalid_anchor = self.query(
                """
                SELECT extension.provenance_id
                FROM _tmp_liquidation_extension AS extension
                LEFT JOIN security_master AS security
                  ON security.security_id = extension.security_id
                WHERE security.security_id IS NULL
                   OR NOT EXISTS (
                       SELECT 1
                       FROM security_identity_assignments AS identity
                       WHERE identity.universe_id = extension.universe_id
                         AND identity.security_id = extension.security_id
                         AND identity.ticker = extension.ticker
                         AND identity.effective_end = CAST(extension.data_start AS DATE)
                   )
                   OR NOT EXISTS (
                       SELECT 1
                       FROM provider_symbol_history AS mapping
                       WHERE mapping.security_id = extension.security_id
                         AND mapping.provider = extension.provider
                         AND mapping.provider_symbol = extension.provider_symbol
                         AND mapping.mapping_status = 'verified'
                         AND mapping.data_end = CAST(extension.data_start AS DATE)
                   )
                LIMIT 1
                """
            )
            if invalid_anchor:
                raise ValueError(
                    "liquidation extension lacks an exact identity/provider end anchor"
                )
            conflict = self.query(
                """
                SELECT extension.provenance_id
                FROM _tmp_liquidation_extension AS extension
                WHERE EXISTS (
                    SELECT 1 FROM security_ticker_extensions AS existing
                    WHERE existing.provenance_id = extension.provenance_id
                      AND (
                          existing.security_id <> extension.security_id
                          OR existing.ticker <> extension.ticker
                          OR existing.provider <> extension.provider
                          OR existing.provider_symbol <> extension.provider_symbol
                          OR existing.data_start <> CAST(extension.data_start AS DATE)
                          OR existing.data_end <> CAST(extension.data_end AS DATE)
                          OR existing.payload_sha256 <> extension.payload_sha256
                      )
                ) OR EXISTS (
                    SELECT 1 FROM provider_symbol_history AS mapping
                    WHERE mapping.security_id = extension.security_id
                      AND mapping.provider = extension.provider
                      AND mapping.data_start < CAST(extension.data_end AS DATE)
                      AND COALESCE(mapping.data_end, DATE '9999-12-31')
                          > CAST(extension.data_start AS DATE)
                      AND NOT (
                          mapping.data_start = CAST(extension.data_start AS DATE)
                          AND mapping.data_end = CAST(extension.data_end AS DATE)
                          AND mapping.provider_symbol = extension.provider_symbol
                          AND mapping.mapping_status = 'verified'
                      )
                )
                LIMIT 1
                """
            )
            if conflict:
                raise ValueError("liquidation extension conflicts with existing provenance")
            price_conflict = self.query(
                """
                SELECT incoming.ticker, incoming.date
                FROM _tmp_liquidation_prices AS incoming
                JOIN prices AS existing
                  ON existing.ticker = incoming.ticker
                 AND existing.date = CAST(incoming.date AS DATE)
                WHERE existing.security_id IS NOT NULL
                  AND existing.security_id <> incoming.security_id
                LIMIT 1
                """
            )
            if price_conflict:
                raise ValueError("liquidation price conflicts with another security")

            extension_count = self.execute(
                """
                INSERT INTO security_ticker_extensions
                (provenance_id, universe_id, security_id, ticker, provider,
                 provider_symbol, data_start, data_end, verified_date,
                 identity_source, provider_source, payload_sha256, purpose,
                 review_policy, fetched_at)
                SELECT provenance_id, universe_id, security_id, ticker, provider,
                       provider_symbol, CAST(data_start AS DATE), CAST(data_end AS DATE),
                       CAST(verified_date AS DATE), identity_source, provider_source,
                       payload_sha256, purpose, review_policy, now()
                FROM _tmp_liquidation_extension
                ON CONFLICT (provenance_id) DO UPDATE
                SET verified_date = EXCLUDED.verified_date,
                    fetched_at = EXCLUDED.fetched_at
                """
            ).fetchone()[0]
            provider_count = self.execute(
                """
                INSERT INTO provider_symbol_history
                (provider, provider_symbol, security_id, data_start, data_end,
                 mapping_status, verified_date, source, fetched_at)
                SELECT provider, provider_symbol, security_id,
                       CAST(data_start AS DATE), CAST(data_end AS DATE), 'verified',
                       CAST(verified_date AS DATE), provider_source, now()
                FROM _tmp_liquidation_extension
                ON CONFLICT (provider, security_id, data_start) DO UPDATE
                SET data_end = EXCLUDED.data_end,
                    verified_date = EXCLUDED.verified_date,
                    source = EXCLUDED.source,
                    fetched_at = EXCLUDED.fetched_at
                """
            ).fetchone()[0]
            price_count = self.execute(
                """
                INSERT INTO prices
                (ticker, security_id, provider_symbol, date, open, high, low, close,
                 adj_close, volume, dividends, split_ratio, actions_complete,
                 close_split_adjusted, split_normalization_factor,
                 split_normalization_through, source, fetched_at)
                SELECT ticker, security_id, provider_symbol, CAST(date AS DATE),
                       open, high, low, close, adj_close, volume, dividends,
                       split_ratio, actions_complete, close_split_adjusted,
                       split_normalization_factor,
                       CAST(split_normalization_through AS DATE), source, now()
                FROM _tmp_liquidation_prices
                ON CONFLICT (ticker, date) DO UPDATE
                SET security_id = EXCLUDED.security_id,
                    provider_symbol = EXCLUDED.provider_symbol,
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    adj_close = EXCLUDED.adj_close,
                    volume = EXCLUDED.volume,
                    dividends = EXCLUDED.dividends,
                    split_ratio = EXCLUDED.split_ratio,
                    actions_complete = EXCLUDED.actions_complete,
                    close_split_adjusted = EXCLUDED.close_split_adjusted,
                    split_normalization_factor = EXCLUDED.split_normalization_factor,
                    split_normalization_through = EXCLUDED.split_normalization_through,
                    source = EXCLUDED.source,
                    fetched_at = EXCLUDED.fetched_at
                """
            ).fetchone()[0]
            self.execute("COMMIT")
            return {
                "extensions": int(extension_count),
                "provider_symbols": int(provider_count),
                "prices": int(price_count),
            }
        except Exception:
            self.execute("ROLLBACK")
            raise
        finally:
            self._con.unregister("_tmp_liquidation_extension")
            self._con.unregister("_tmp_liquidation_prices")

    def upsert_prices(self, rows: list[dict]) -> int:
        """Upsert daily prices. Idempotent on (ticker, date)."""
        if not rows:
            return 0
        normalized: list[dict] = []
        for row in rows:
            ticker = str(row.get("ticker") or "").strip().upper()
            row_date = row.get("date")
            try:
                close = float(row.get("close"))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"price close must be positive and finite for "
                    f"{ticker or '<missing ticker>'}@{row_date}"
                ) from exc
            if not isfinite(close) or close <= 0:
                raise ValueError(
                    f"price close must be positive and finite for "
                    f"{ticker or '<missing ticker>'}@{row_date}"
                )
            source = str(row.get("source") or "yfinance").strip().lower()
            close_split_adjusted = row.get("close_split_adjusted")
            if close_split_adjusted is None and source in {
                "yfinance",
                "tiingo",
                "stooq",
                "test",
            }:
                close_split_adjusted = source == "yfinance"
            split_normalization_factor = row.get("split_normalization_factor")
            if split_normalization_factor is None and close_split_adjusted is False:
                split_normalization_factor = 1.0
            if split_normalization_factor is not None:
                split_normalization_factor = float(split_normalization_factor)
                if not isfinite(split_normalization_factor) or split_normalization_factor <= 0:
                    raise ValueError("split_normalization_factor must be positive and finite")
            normalized.append(
                {
                    "ticker": ticker,
                    "security_id": row.get("security_id"),
                    "provider_symbol": row.get("provider_symbol"),
                    "date": row_date,
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": close,
                    "adj_close": row.get("adj_close"),
                    "volume": row.get("volume"),
                    "dividends": row.get("dividends", 0),
                    "split_ratio": row.get("split_ratio", 1),
                    "actions_complete": bool(row.get("actions_complete", source == "test")),
                    "close_split_adjusted": close_split_adjusted,
                    "split_normalization_factor": split_normalization_factor,
                    "split_normalization_through": row.get("split_normalization_through"),
                    "source": source,
                }
            )
        self._con.register("_tmp_px", _rows_to_arrowable(normalized))
        try:
            n = self._con.execute(
                """
                INSERT INTO prices
                (ticker, security_id, provider_symbol, date, open, high, low, close,
                 adj_close, volume, dividends, split_ratio, actions_complete,
                 close_split_adjusted, split_normalization_factor,
                 split_normalization_through, source, fetched_at)
                SELECT
                    ticker, security_id, provider_symbol, CAST(date AS DATE),
                    open, high, low, close, adj_close, volume,
                    COALESCE(dividends, 0), COALESCE(split_ratio, 1),
                    COALESCE(actions_complete, FALSE),
                    close_split_adjusted,
                    split_normalization_factor,
                    CAST(split_normalization_through AS DATE),
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
                    actions_complete = EXCLUDED.actions_complete,
                    close_split_adjusted = EXCLUDED.close_split_adjusted,
                    split_normalization_factor = EXCLUDED.split_normalization_factor,
                    split_normalization_through = EXCLUDED.split_normalization_through,
                    source = EXCLUDED.source,
                    fetched_at = EXCLUDED.fetched_at
                """.strip()
            ).fetchone()[0]
        finally:
            self._con.unregister("_tmp_px")
        return int(n)

    def upsert_factor_price_warmup(
        self,
        provenance_rows: list[dict],
        price_rows: list[dict],
    ) -> dict[str, int]:
        """Atomically import identity-safe, overlap-reviewed factor history.

        Warm-up observations deliberately have no ticker. Their security identity
        is authorized only by a provenance row anchored exactly at the start of
        an existing verified provider mapping. Re-importing a reviewed interval
        replaces that interval's observations so a smaller corrected snapshot
        cannot leave stale dates behind.
        """
        if not provenance_rows:
            if price_rows:
                raise ValueError("factor-price rows require provenance")
            return {"provenance": 0, "factor_prices": 0}
        if not price_rows:
            raise ValueError("factor-price provenance has no payload rows")

        normalized_provenance: list[dict] = []
        seen_provenance: set[str] = set()
        for row in provenance_rows:
            provenance_id = _required_text(row, "provenance_id", "factor-price provenance")
            if provenance_id in seen_provenance:
                raise ValueError(f"duplicate factor-price provenance {provenance_id!r}")
            seen_provenance.add(provenance_id)
            normalized_provenance.append(
                {
                    "provenance_id": provenance_id,
                    "universe_id": _required_text(row, "universe_id", "factor-price provenance"),
                    "security_id": _required_text(row, "security_id", "factor-price provenance"),
                    "provider": _required_text(row, "provider", "factor-price provenance").lower(),
                    "provider_symbol": _required_text(
                        row, "provider_symbol", "factor-price provenance"
                    ).upper(),
                    "data_start": row.get("data_start"),
                    "data_end": row.get("data_end"),
                    "overlap_start": row.get("overlap_start"),
                    "overlap_end": row.get("overlap_end"),
                    "verified_date": row.get("verified_date"),
                    "source": _required_text(row, "source", "factor-price provenance"),
                    "payload_sha256": _required_text(
                        row, "payload_sha256", "factor-price provenance"
                    ),
                    "overlap_sha256": _required_text(
                        row, "overlap_sha256", "factor-price provenance"
                    ),
                    "review_policy": _required_text(
                        row, "review_policy", "factor-price provenance"
                    ),
                }
            )

        normalized_prices: list[dict] = []
        seen_prices: set[tuple[str, str]] = set()
        for row in price_rows:
            security_id = _required_text(row, "security_id", "factor price")
            row_date = _required_text(row, "date", "factor price")
            key = (security_id, row_date)
            if key in seen_prices:
                raise ValueError(f"duplicate factor price {security_id}@{row_date}")
            seen_prices.add(key)
            close = float(row.get("close"))
            dividends = float(row.get("dividends", 0))
            split_ratio = float(row.get("split_ratio", 1))
            factor = float(row.get("split_normalization_factor"))
            if not isfinite(close) or close <= 0:
                raise ValueError("factor-price close must be positive and finite")
            if not isfinite(dividends) or dividends < 0:
                raise ValueError("factor-price dividends must be non-negative and finite")
            if not isfinite(split_ratio) or split_ratio <= 0:
                raise ValueError("factor-price split_ratio must be positive and finite")
            if not isfinite(factor) or factor <= 0:
                raise ValueError(
                    "factor-price split_normalization_factor must be positive and finite"
                )
            if row.get("actions_complete") is not True:
                raise ValueError("factor-price corporate actions must be reviewed")
            if row.get("close_split_adjusted") not in {True, False}:
                raise ValueError("factor-price split adjustment basis must be explicit")
            normalized_prices.append(
                {
                    "security_id": security_id,
                    "date": row_date,
                    "provider": _required_text(row, "provider", "factor price").lower(),
                    "provider_symbol": _required_text(
                        row, "provider_symbol", "factor price"
                    ).upper(),
                    "close": close,
                    "adj_close": row.get("adj_close"),
                    "dividends": dividends,
                    "split_ratio": split_ratio,
                    "actions_complete": True,
                    "close_split_adjusted": row["close_split_adjusted"],
                    "split_normalization_factor": factor,
                    "split_normalization_through": row.get("split_normalization_through"),
                    "provenance_id": _required_text(row, "provenance_id", "factor price"),
                }
            )

        self._con.register("_tmp_factor_provenance", _rows_to_arrowable(normalized_provenance))
        self._con.register("_tmp_factor_prices", _rows_to_arrowable(normalized_prices))
        self.execute("BEGIN TRANSACTION")
        try:
            invalid_interval = self.query(
                """
                SELECT provenance_id
                FROM _tmp_factor_provenance
                WHERE CAST(data_end AS DATE) <= CAST(data_start AS DATE)
                   OR CAST(overlap_start AS DATE) <> CAST(data_end AS DATE)
                   OR CAST(overlap_end AS DATE) <= CAST(overlap_start AS DATE)
                   OR CAST(verified_date AS DATE) > CURRENT_DATE
                LIMIT 1
                """
            )
            if invalid_interval:
                raise ValueError("invalid factor-price provenance interval")

            unknown_security = self.query(
                """
                SELECT incoming.security_id
                FROM _tmp_factor_provenance AS incoming
                LEFT JOIN security_master AS security USING (security_id)
                WHERE security.security_id IS NULL
                LIMIT 1
                """
            )
            if unknown_security:
                raise ValueError(
                    "factor-price provenance uses unknown security_id "
                    f"{unknown_security[0]['security_id']!r}"
                )

            unanchored = self.query(
                """
                SELECT incoming.provenance_id
                FROM _tmp_factor_provenance AS incoming
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM provider_symbol_history AS mapping
                    WHERE mapping.security_id = incoming.security_id
                      AND mapping.provider = incoming.provider
                      AND mapping.provider_symbol = incoming.provider_symbol
                      AND mapping.mapping_status = 'verified'
                      AND mapping.data_start = CAST(incoming.data_end AS DATE)
                )
                LIMIT 1
                """
            )
            if unanchored:
                raise ValueError("factor-price provenance lacks an exact verified mapping anchor")

            conflicting_provenance = self.query(
                """
                SELECT incoming.provenance_id
                FROM _tmp_factor_provenance AS incoming
                JOIN factor_price_provenance AS existing USING (provenance_id)
                WHERE existing.universe_id IS DISTINCT FROM incoming.universe_id
                   OR existing.security_id IS DISTINCT FROM incoming.security_id
                   OR existing.provider IS DISTINCT FROM incoming.provider
                   OR existing.provider_symbol IS DISTINCT FROM incoming.provider_symbol
                   OR existing.data_start IS DISTINCT FROM CAST(incoming.data_start AS DATE)
                   OR existing.data_end IS DISTINCT FROM CAST(incoming.data_end AS DATE)
                   OR existing.payload_sha256 IS DISTINCT FROM incoming.payload_sha256
                   OR existing.overlap_sha256 IS DISTINCT FROM incoming.overlap_sha256
                   OR existing.review_policy IS DISTINCT FROM incoming.review_policy
                LIMIT 1
                """
            )
            if conflicting_provenance:
                raise ValueError("factor-price provenance ID conflicts with stored evidence")

            invalid_price = self.query(
                """
                SELECT price.security_id
                FROM _tmp_factor_prices AS price
                LEFT JOIN _tmp_factor_provenance AS provenance
                  ON provenance.provenance_id = price.provenance_id
                WHERE provenance.provenance_id IS NULL
                   OR price.security_id <> provenance.security_id
                   OR price.provider <> provenance.provider
                   OR price.provider_symbol <> provenance.provider_symbol
                   OR CAST(price.date AS DATE) < CAST(provenance.data_start AS DATE)
                   OR CAST(price.date AS DATE) >= CAST(provenance.data_end AS DATE)
                LIMIT 1
                """
            )
            if invalid_price:
                raise ValueError("factor-price row falls outside its reviewed provenance")

            missing_payload = self.query(
                """
                SELECT provenance.provenance_id
                FROM _tmp_factor_provenance AS provenance
                LEFT JOIN _tmp_factor_prices AS price USING (provenance_id)
                GROUP BY provenance.provenance_id
                HAVING COUNT(price.provenance_id) = 0
                LIMIT 1
                """
            )
            if missing_payload:
                raise ValueError("factor-price provenance has no payload rows")

            provenance_count = self.execute(
                """
                INSERT INTO factor_price_provenance
                (provenance_id, universe_id, security_id, provider, provider_symbol,
                 data_start, data_end, overlap_start, overlap_end, verified_date,
                 source, payload_sha256, overlap_sha256, review_policy, fetched_at)
                SELECT provenance_id, universe_id, security_id, provider,
                       provider_symbol, CAST(data_start AS DATE), CAST(data_end AS DATE),
                       CAST(overlap_start AS DATE), CAST(overlap_end AS DATE),
                       CAST(verified_date AS DATE), source, payload_sha256,
                       overlap_sha256, review_policy, now()
                FROM _tmp_factor_provenance
                ON CONFLICT (provenance_id) DO UPDATE
                SET verified_date = EXCLUDED.verified_date,
                    source = EXCLUDED.source,
                    fetched_at = EXCLUDED.fetched_at
                """
            ).fetchone()[0]

            self.execute(
                """
                DELETE FROM factor_prices AS existing
                USING _tmp_factor_provenance AS incoming
                WHERE existing.security_id = incoming.security_id
                  AND existing.date >= CAST(incoming.data_start AS DATE)
                  AND existing.date < CAST(incoming.data_end AS DATE)
                """
            )
            price_count = self.execute(
                """
                INSERT INTO factor_prices
                (security_id, date, provider, provider_symbol, close, adj_close,
                 dividends, split_ratio, actions_complete, close_split_adjusted,
                 split_normalization_factor, split_normalization_through,
                 provenance_id, fetched_at)
                SELECT security_id, CAST(date AS DATE), provider, provider_symbol,
                       close, adj_close, dividends, split_ratio, actions_complete,
                       close_split_adjusted, split_normalization_factor,
                       CAST(split_normalization_through AS DATE), provenance_id, now()
                FROM _tmp_factor_prices
                ON CONFLICT (security_id, date) DO UPDATE
                SET provider = EXCLUDED.provider,
                    provider_symbol = EXCLUDED.provider_symbol,
                    close = EXCLUDED.close,
                    adj_close = EXCLUDED.adj_close,
                    dividends = EXCLUDED.dividends,
                    split_ratio = EXCLUDED.split_ratio,
                    actions_complete = EXCLUDED.actions_complete,
                    close_split_adjusted = EXCLUDED.close_split_adjusted,
                    split_normalization_factor = EXCLUDED.split_normalization_factor,
                    split_normalization_through = EXCLUDED.split_normalization_through,
                    provenance_id = EXCLUDED.provenance_id,
                    fetched_at = EXCLUDED.fetched_at
                """
            ).fetchone()[0]
            self.execute("COMMIT")
            return {
                "provenance": int(provenance_count),
                "factor_prices": int(price_count),
            }
        except Exception:
            self.execute("ROLLBACK")
            raise
        finally:
            self._con.unregister("_tmp_factor_provenance")
            self._con.unregister("_tmp_factor_prices")

    def upsert_fundamentals(
        self,
        rows: list[dict],
        *,
        _manage_transaction: bool = True,
    ) -> int:
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
            provenance = _fundamental_provenance(row)
            normalized.append(
                {
                    "ticker": str(row.get("ticker") or "").strip().upper(),
                    "issuer_id": row.get("issuer_id"),
                    "security_id": row.get("security_id"),
                    **provenance,
                    "source_fact_locator": row.get("source_fact_locator"),
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
        if _manage_transaction:
            self.execute("BEGIN TRANSACTION")
        try:
            duplicate = self.query(
                """
                SELECT ticker, CAST(period_end AS DATE) AS period_end,
                       CAST(as_of_date AS DATE) AS as_of_date, metric,
                       COUNT(*) AS duplicate_count
                FROM _tmp_fund
                GROUP BY ticker, CAST(period_end AS DATE),
                         CAST(as_of_date AS DATE), metric
                HAVING COUNT(*) > 1
                LIMIT 1
                """
            )
            if duplicate:
                sample = duplicate[0]
                raise ValueError(
                    "upsert_fundamentals contains duplicate economic key "
                    f"{sample['ticker']}:{sample['metric']}@"
                    f"{sample['period_end']}/{sample['as_of_date']}"
                )
            lineage_conflict = self.query(
                """
                SELECT existing.ticker, existing.period_end, existing.as_of_date,
                       existing.metric
                FROM fundamentals AS existing
                JOIN _tmp_fund AS incoming
                  ON existing.ticker = incoming.ticker
                 AND existing.period_end = CAST(incoming.period_end AS DATE)
                 AND existing.as_of_date = CAST(incoming.as_of_date AS DATE)
                 AND existing.metric = incoming.metric
                WHERE incoming.ingest_run_id IS NULL
                  AND (
                      existing.ingest_run_id IS NOT NULL
                      OR existing.source_snapshot_id IS NOT NULL
                      OR existing.source_rowset_sha256 IS NOT NULL
                      OR existing.source_row_sha256 IS NOT NULL
                  )
                LIMIT 1
                """
            )
            if lineage_conflict:
                sample = lineage_conflict[0]
                raise ValueError(
                    "unlineaged fundamental cannot overwrite explicitly "
                    "lineaged evidence for "
                    f"{sample['ticker']}:{sample['metric']}@"
                    f"{sample['period_end']}/{sample['as_of_date']}"
                )
            n = self._con.execute(
                """
                INSERT INTO fundamentals
                (ticker, issuer_id, security_id, ingest_run_id, source_snapshot_id,
                 source_rowset_sha256, source_row_sha256, source_fact_locator,
                 period_end, as_of_date, fiscal_period, statement, metric, value,
                 quarter_value, unit, source, fetched_at)
                SELECT
                    ticker, issuer_id, security_id, ingest_run_id, source_snapshot_id,
                    source_rowset_sha256, source_row_sha256, source_fact_locator,
                    CAST(period_end AS DATE), CAST(as_of_date AS DATE),
                    fiscal_period, statement, metric, value, quarter_value,
                    COALESCE(unit, 'USD'), source, now()
                FROM _tmp_fund
                ON CONFLICT (ticker, period_end, as_of_date, metric) DO UPDATE
                SET issuer_id = COALESCE(EXCLUDED.issuer_id, fundamentals.issuer_id),
                    security_id = COALESCE(
                        EXCLUDED.security_id, fundamentals.security_id
                    ),
                    ingest_run_id = EXCLUDED.ingest_run_id,
                    source_snapshot_id = EXCLUDED.source_snapshot_id,
                    source_rowset_sha256 = EXCLUDED.source_rowset_sha256,
                    source_row_sha256 = EXCLUDED.source_row_sha256,
                    source_fact_locator = EXCLUDED.source_fact_locator,
                    fiscal_period = EXCLUDED.fiscal_period,
                    statement = EXCLUDED.statement,
                    value = EXCLUDED.value,
                    quarter_value = EXCLUDED.quarter_value,
                    unit = EXCLUDED.unit,
                    source = EXCLUDED.source,
                    fetched_at = EXCLUDED.fetched_at
                """.strip()
            ).fetchone()[0]
            # Version the resolved projection, not the raw incoming row.  In
            # particular, identity columns intentionally retain an existing
            # non-null value when an update omits them.  Capturing before the
            # merge would make a named evidence generation disagree with the
            # current projection immediately after a successful transaction.
            self.execute(
                """
                INSERT INTO fundamental_versions
                (ticker, issuer_id, security_id, ingest_run_id,
                 source_snapshot_id, source_rowset_sha256,
                 source_row_sha256, source_fact_locator, period_end,
                 as_of_date, fiscal_period, statement, metric, value,
                 quarter_value, unit, source, recorded_at, is_deleted)
                SELECT current.ticker, current.issuer_id, current.security_id,
                       current.ingest_run_id, current.source_snapshot_id,
                       current.source_rowset_sha256, current.source_row_sha256,
                       current.source_fact_locator, current.period_end,
                       current.as_of_date, current.fiscal_period,
                       current.statement, current.metric, current.value,
                       current.quarter_value, current.unit, current.source,
                       current.fetched_at, FALSE
                FROM fundamentals AS current
                JOIN _tmp_fund AS incoming
                  ON current.ticker = incoming.ticker
                 AND current.period_end = CAST(incoming.period_end AS DATE)
                 AND current.as_of_date = CAST(incoming.as_of_date AS DATE)
                 AND current.metric = incoming.metric
                """
            )
            if _manage_transaction:
                self.execute("COMMIT")
            return int(n)
        except Exception:
            if _manage_transaction:
                self.execute("ROLLBACK")
            raise
        finally:
            self._con.unregister("_tmp_fund")

    def refresh_issuer_fundamentals(
        self,
        rows: list[dict],
        *,
        issuer_id: str,
        canonical_ticker: str,
    ) -> tuple[int, int]:
        """Refresh one complete issuer relation without silent shrinkage.

        SEC Company Facts is fetched as a complete issuer history. Once that
        complete payload has been extracted successfully, every current key for
        the same ``issuer_id`` must remain represented. Unexpected omissions
        fail the transaction; a future governed replacement path must provide
        explicit confirmation, backup, and compare-and-set evidence. Untagged
        legacy rows and rows belonging to other issuers are untouched.
        """
        issuer_id, canonical_ticker = self._validated_issuer_fundamental_refresh(
            rows,
            issuer_id=issuer_id,
            canonical_ticker=canonical_ticker,
        )
        if not rows:
            return 0, 0

        self.execute("BEGIN TRANSACTION")
        try:
            result = self._refresh_issuer_fundamentals_in_transaction(
                rows,
                issuer_id=issuer_id,
                canonical_ticker=canonical_ticker,
            )
            self.execute("COMMIT")
            return result
        except Exception:
            self.execute("ROLLBACK")
            raise

    def commit_issuer_fundamental_ingest(
        self,
        rows: list[dict],
        *,
        issuer_id: str,
        canonical_ticker: str,
        security_rows: list[dict],
        submissions_row: dict[str, Any],
        run_id: str,
        source: str,
        rows_rejected: int,
        started_at: datetime,
        status: str,
        error: str | None,
        rejection_codes: tuple[str, ...] | list[str] | None = None,
    ) -> tuple[int, int]:
        """Atomically publish one accepted SEC issuer ingest outcome.

        Raw provider snapshots are captured before this boundary and remain
        immutable evidence even when this transaction fails. Accepted facts,
        stale-label cleanup, display metadata, and the successful or warning
        outcome become visible together.
        """
        if source != "edgar:issuer-cik-history":
            raise ValueError("accepted issuer ingest requires the reviewed SEC source route")
        if status not in {"success", "warning"}:
            raise ValueError("accepted issuer ingest status must be success or warning")
        if isinstance(rows_rejected, bool) or not isinstance(rows_rejected, int):
            raise TypeError("accepted issuer ingest rows_rejected must be an integer")
        if rows_rejected < 0:
            raise ValueError("accepted issuer ingest rows_rejected cannot be negative")
        encoded_rejection_codes = canonical_rejection_codes(rejection_codes)
        decoded_rejection_codes = decode_rejection_codes(encoded_rejection_codes)
        if decoded_rejection_codes is not None and not (
            set(decoded_rejection_codes) <= SEC_FUNDAMENTAL_REJECTION_CODES
        ):
            raise ValueError("accepted issuer ingest has an unknown rejection code")
        if status == "success" and (
            not rows
            or rows_rejected != 0
            or decoded_rejection_codes is not None
            or error is not None
        ):
            raise ValueError("successful issuer ingest requires rows and zero rejection evidence")
        if status == "warning":
            if rows_rejected > 0 and (
                decoded_rejection_codes is None or not str(error or "").strip()
            ):
                raise ValueError("warning issuer ingest with rejections requires codes and detail")
            if rows_rejected == 0 and (
                rows
                or decoded_rejection_codes is not None
                or error != "SEC returned no fundamental rows"
            ):
                raise ValueError("zero-rejection issuer warning must be an exact zero-row outcome")
        issuer_id, canonical_ticker = self._validated_issuer_fundamental_refresh(
            rows,
            issuer_id=issuer_id,
            canonical_ticker=canonical_ticker,
        )
        self._validate_issuer_ingest_source_lineage(
            rows,
            run_id=run_id,
            issuer_id=issuer_id,
            canonical_ticker=canonical_ticker,
            security_rows=security_rows,
            submissions_row=submissions_row,
            rows_rejected=rows_rejected,
            rejection_codes=encoded_rejection_codes,
        )
        self.execute("BEGIN TRANSACTION")
        try:
            inserted, stale = self._refresh_issuer_fundamentals_in_transaction(
                rows,
                issuer_id=issuer_id,
                canonical_ticker=canonical_ticker,
            )
            self.upsert_securities(security_rows)
            self.record_ingest(
                run_id=run_id,
                source=source,
                table_name="fundamentals",
                subject_type="issuer",
                subject_id=issuer_id,
                rows_inserted=inserted,
                rows_rejected=rows_rejected,
                started_at=started_at,
                status=status,
                error=error,
                rejection_codes=list(decoded_rejection_codes or ()),
            )
            self.execute("COMMIT")
            return inserted, stale
        except Exception:
            self.execute("ROLLBACK")
            raise

    def _validate_issuer_ingest_source_lineage(
        self,
        rows: list[dict],
        *,
        run_id: str,
        issuer_id: str,
        canonical_ticker: str,
        security_rows: list[dict],
        submissions_row: dict[str, Any],
        rows_rejected: int,
        rejection_codes: str | None,
    ) -> None:
        """Require accepted issuer rows to cite one exact captured response."""

        lineage_fields = (
            "ingest_run_id",
            "source_snapshot_id",
            "source_rowset_sha256",
            "source_row_sha256",
        )
        if rows and any(
            not all(str(row.get(field) or "").strip() for field in lineage_fields) for row in rows
        ):
            raise ValueError(
                "accepted issuer fundamentals require explicit source snapshot lineage"
            )
        if rows and any(str(row["ingest_run_id"]).strip() != run_id for row in rows):
            raise ValueError("issuer fundamental lineage run does not match the outcome")
        snapshot_ids = {str(row["source_snapshot_id"]).strip() for row in rows}
        rowset_hashes = {str(row["source_rowset_sha256"]).strip().lower() for row in rows}
        if rows and (len(snapshot_ids) != 1 or len(rowset_hashes) != 1):
            raise ValueError("issuer fundamental rows cite different source evidence")
        snapshot_id = next(iter(snapshot_ids)) if snapshot_ids else None
        rowset_hash = next(iter(rowset_hashes)) if rowset_hashes else None
        evidence = self.query(
            """
            SELECT snapshot.snapshot_id, snapshot.provider, snapshot.dataset,
                   snapshot.artifact_kind, snapshot.http_status,
                   snapshot.parser_version, snapshot.parsed_row_count,
                   snapshot.parsed_rows_sha256, snapshot.parsed_rows_rejected,
                   snapshot.parsed_rejection_codes
            FROM ingest_raw_snapshots AS linked
            JOIN raw_snapshots AS snapshot USING (snapshot_id)
            WHERE linked.run_id = ?
              AND linked.role = 'companyfacts'
            ORDER BY snapshot.snapshot_id
            """,
            (run_id,),
        )
        if len(evidence) != 1:
            raise ValueError(
                "accepted issuer fundamentals require one linked Company Facts response"
            )
        source = evidence[0]
        from aios.ingest.edgar import canonical_sec_fundamental_row_sha256
        from aios.raw_snapshots import canonical_parsed_rows_sha256

        reference = self.issuer_reference(issuer_id)
        try:
            cik = int(reference["cik"]) if reference is not None else None
        except (KeyError, TypeError, ValueError):
            cik = None
        if cik is None:
            raise ValueError("issuer fundamental lineage requires one reviewed SEC CIK")
        security_assignments = self.query(
            """
            SELECT DISTINCT security_id
            FROM security_issuer_assignments
            WHERE issuer_id = ?
            ORDER BY security_id
            """,
            (issuer_id,),
        )
        expected_security_id = (
            str(security_assignments[0]["security_id"]) if len(security_assignments) == 1 else None
        )
        if any(
            (str(row.get("security_id")).strip() if row.get("security_id") is not None else None)
            != expected_security_id
            for row in rows
        ):
            raise ValueError("issuer fundamental security_id does not match reviewed assignments")

        provider_rows: list[dict[str, Any]] = []
        for row in rows:
            provider_row = {
                "cik": f"{cik:010d}",
                "period_end": str(row["period_end"]),
                "as_of_date": str(row["as_of_date"]),
                "fiscal_period": row.get("fiscal_period"),
                "statement": row.get("statement"),
                "metric": row["metric"],
                "value": row.get("value"),
                "quarter_value": row.get("quarter_value"),
                "unit": row.get("unit") or "USD",
                "source": row.get("source") or "edgar",
            }
            if row.get("source_fact_locator") is not None:
                provider_row["source_fact_locator"] = row["source_fact_locator"]
            if (
                canonical_sec_fundamental_row_sha256(provider_row)
                != str(row["source_row_sha256"]).lower()
            ):
                raise ValueError("issuer fundamental row hash does not match its economic content")
            provider_rows.append(provider_row)
        computed_rowset_hash = canonical_parsed_rows_sha256(provider_rows)
        if (
            (snapshot_id is not None and source.get("snapshot_id") != snapshot_id)
            or source.get("provider") != "sec-edgar"
            or source.get("dataset") != "companyfacts"
            or source.get("artifact_kind") != "exact_response"
            or not isinstance(source.get("http_status"), int)
            or not 200 <= int(source["http_status"]) <= 299
            or source.get("parser_version")
            not in {
                "sec-companyfacts-v2",
                "sec-companyfacts-v2-storage-safe-v1",
                "sec-companyfacts-v2-storage-safe-v2",
            }
            or not isinstance(source.get("parsed_row_count"), int)
            or int(source["parsed_row_count"]) != len(rows)
            or str(source.get("parsed_rows_sha256") or "").lower() != computed_rowset_hash
            or (rowset_hash is not None and rowset_hash != computed_rowset_hash)
            or source.get("parsed_rows_rejected") != rows_rejected
            or source.get("parsed_rejection_codes") != rejection_codes
        ):
            raise ValueError(
                "issuer fundamental lineage does not match its exact Company Facts response"
            )
        submissions = self.query(
            """
            SELECT snapshot.provider, snapshot.dataset, snapshot.artifact_kind,
                   snapshot.http_status, snapshot.parser_version,
                   snapshot.parsed_row_count, snapshot.parsed_rows_sha256
            FROM ingest_raw_snapshots AS linked
            JOIN raw_snapshots AS snapshot USING (snapshot_id)
            WHERE linked.run_id = ?
              AND linked.role = 'submissions'
            ORDER BY snapshot.snapshot_id
            """,
            (run_id,),
        )
        if len(submissions) != 1:
            raise ValueError(
                "accepted issuer fundamentals require one linked SEC Submissions response"
            )
        submission = submissions[0]
        if (
            submission.get("provider") != "sec-edgar"
            or submission.get("dataset") != "submissions"
            or submission.get("artifact_kind") != "exact_response"
            or not isinstance(submission.get("http_status"), int)
            or not 200 <= int(submission["http_status"]) <= 299
            or submission.get("parser_version") != "sec-submissions-v2"
            or submission.get("parsed_row_count") != 1
            or not str(submission.get("parsed_rows_sha256") or "").strip()
        ):
            raise ValueError("issuer fundamental lineage has invalid SEC Submissions evidence")
        self._validate_issuer_security_metadata(
            security_rows=security_rows,
            submissions_row=submissions_row,
            canonical_ticker=canonical_ticker,
            reference=reference,
            submissions_rowset_sha256=str(submission["parsed_rows_sha256"]),
        )

    @staticmethod
    def _validate_issuer_security_metadata(
        *,
        security_rows: list[dict],
        submissions_row: dict[str, Any],
        canonical_ticker: str,
        reference: dict[str, Any],
        submissions_rowset_sha256: str,
    ) -> None:
        """Bind display metadata to the exact parsed Submissions response."""

        expected_submission_fields = {
            "cik",
            "name",
            "sic",
            "sic_description",
            "exchanges",
        }
        if (
            not isinstance(submissions_row, dict)
            or set(submissions_row) != expected_submission_fields
        ):
            raise ValueError("issuer security metadata requires one canonical Submissions row")
        try:
            cik = int(reference["cik"])
            submitted_cik = int(submissions_row["cik"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("issuer security metadata has an invalid SEC CIK") from exc
        if submitted_cik != cik:
            raise ValueError("issuer security metadata CIK does not match reviewed issuer")

        def optional_text(value: Any) -> str | None:
            if value is None:
                return None
            if not isinstance(value, str):
                raise ValueError("issuer Submissions metadata must use strings or null")
            normalized = value.strip()
            return normalized or None

        exchanges_value = submissions_row["exchanges"]
        if not isinstance(exchanges_value, list):
            raise ValueError("issuer Submissions exchanges must be a list")
        exchanges = [optional_text(value) for value in exchanges_value]
        if any(value is None for value in exchanges):
            raise ValueError("issuer Submissions exchanges must be nonblank")
        canonical_submissions = {
            "cik": f"{cik:010d}",
            "name": optional_text(submissions_row["name"]),
            "sic": optional_text(submissions_row["sic"]),
            "sic_description": optional_text(submissions_row["sic_description"]),
            "exchanges": exchanges,
        }
        if canonical_submissions != submissions_row:
            raise ValueError("issuer Submissions metadata is not canonical")
        from aios.raw_snapshots import canonical_parsed_rows_sha256

        if canonical_parsed_rows_sha256([canonical_submissions]) != submissions_rowset_sha256:
            raise ValueError("issuer security metadata does not match exact Submissions evidence")
        if len(security_rows) != 1 or not isinstance(security_rows[0], dict):
            raise ValueError("issuer ingest requires exactly one security metadata row")
        expected_security = {
            "ticker": canonical_ticker,
            "cik": cik,
            "name": canonical_submissions["name"] or str(reference["canonical_name"]),
            "exchange": (
                canonical_submissions["exchanges"][0]
                if canonical_submissions["exchanges"]
                else None
            ),
            "sector": canonical_submissions["sic_description"],
            "industry": canonical_submissions["sic_description"],
            "market_cap_bucket": None,
            "sic_code": canonical_submissions["sic"],
        }
        if security_rows[0] != expected_security:
            raise ValueError(
                "issuer security metadata is not derived from reviewed identity "
                "and exact Submissions evidence"
            )

    @staticmethod
    def _validated_issuer_fundamental_refresh(
        rows: list[dict],
        *,
        issuer_id: str,
        canonical_ticker: str,
    ) -> tuple[str, str]:
        issuer_id = issuer_id.strip()
        canonical_ticker = canonical_ticker.strip().upper()
        if not issuer_id or not canonical_ticker:
            raise ValueError("issuer refresh requires issuer_id and canonical_ticker")
        if any(str(row.get("issuer_id") or "").strip() != issuer_id for row in rows):
            raise ValueError("issuer refresh contains a different issuer_id")
        if any(str(row.get("ticker") or "").strip().upper() != canonical_ticker for row in rows):
            raise ValueError("issuer refresh contains a non-canonical ticker")
        return issuer_id, canonical_ticker

    def _refresh_issuer_fundamentals_in_transaction(
        self,
        rows: list[dict],
        *,
        issuer_id: str,
        canonical_ticker: str,
    ) -> tuple[int, int]:
        if not rows:
            return 0, 0
        keys = [
            {
                "ticker": str(row["ticker"]).strip().upper(),
                "period_end": _as_date(row["period_end"]),
                "as_of_date": _as_date(row["as_of_date"]),
                "metric": str(row["metric"]),
            }
            for row in rows
        ]
        self._con.register("_tmp_issuer_fundamental_keys", _rows_to_arrowable(keys))
        try:
            collision = self.query(
                """
                SELECT existing.ticker, existing.period_end,
                       existing.as_of_date, existing.metric,
                       existing.issuer_id
                FROM fundamentals AS existing
                JOIN _tmp_issuer_fundamental_keys AS incoming
                  ON existing.ticker = incoming.ticker
                 AND existing.period_end = CAST(incoming.period_end AS DATE)
                 AND existing.as_of_date = CAST(incoming.as_of_date AS DATE)
                 AND existing.metric = incoming.metric
                WHERE existing.issuer_id IS NOT NULL
                  AND existing.issuer_id <> ?
                LIMIT 1
                """,
                (issuer_id,),
            )
            if collision:
                raise ValueError(
                    "issuer fundamental relation collides with another reviewed issuer"
                )
            stale = int(
                self.query(
                    """
                    SELECT COUNT(*) AS n
                    FROM fundamentals AS existing
                    WHERE existing.issuer_id = ?
                      AND NOT EXISTS (
                          SELECT 1
                          FROM _tmp_issuer_fundamental_keys AS incoming
                          WHERE existing.ticker = incoming.ticker
                            AND existing.period_end =
                                CAST(incoming.period_end AS DATE)
                            AND existing.as_of_date =
                                CAST(incoming.as_of_date AS DATE)
                            AND existing.metric = incoming.metric
                      )
                    """,
                    (issuer_id,),
                )[0]["n"]
            )
            if stale:
                raise ValueError(
                    "issuer fundamental relation would shrink; governed "
                    "replacement confirmation is required"
                )
            inserted = self.upsert_fundamentals(
                rows,
                _manage_transaction=False,
            )
            return inserted, stale
        finally:
            self._con.unregister("_tmp_issuer_fundamental_keys")

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
        subject_type: str | None = None,
        subject_id: str | None = None,
        rejection_codes: tuple[str, ...] | list[str] | None = None,
    ) -> str:
        """Write one auditable ingest outcome and return its run id.

        This is intentionally explicit instead of hidden inside every upsert:
        one ingest operation may fetch several source payloads before it writes
        one table, and the caller knows the operation's true boundary.
        """
        run_id = run_id or str(uuid4())
        started_at = _utc_naive_database_timestamp(
            started_at or datetime.now(UTC),
            label="ingest started_at",
        )
        finished_at = _utc_naive_database_timestamp(
            finished_at or datetime.now(UTC),
            label="ingest finished_at",
        )
        normalized_subject_type, normalized_subject_id = _ingest_subject(
            subject_type,
            subject_id,
        )
        normalized_rejection_codes = canonical_rejection_codes(rejection_codes)
        self._con.execute(
            """
            INSERT INTO ingest_log
            (run_id, source, table_name, subject_type, subject_id,
             rows_inserted, rows_rejected, started_at, finished_at, status, error,
             rejection_codes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                source,
                table_name,
                normalized_subject_type,
                normalized_subject_id,
                rows_inserted,
                rows_rejected,
                started_at,
                finished_at,
                status,
                error,
                normalized_rejection_codes,
            ),
        )
        return run_id

    def record_raw_snapshot(
        self,
        *,
        payload: dict[str, Any],
        snapshot: dict[str, Any],
        ingest_run_id: str | None = None,
        role: str = "source",
    ) -> None:
        """Register immutable payload metadata and one fetch observation."""
        _validate_raw_snapshot_registration(payload, snapshot)
        self._con.execute("BEGIN TRANSACTION")
        try:
            self._con.execute(
                """
                INSERT INTO raw_payloads
                (payload_sha256, relative_path, original_bytes, stored_bytes,
                 compression)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (payload_sha256) DO NOTHING
                """,
                (
                    payload["payload_sha256"],
                    payload["relative_path"],
                    payload["original_bytes"],
                    payload["stored_bytes"],
                    payload["compression"],
                ),
            )
            stored = self.query(
                """
                SELECT relative_path, original_bytes, stored_bytes, compression
                FROM raw_payloads WHERE payload_sha256 = ?
                """,
                (payload["payload_sha256"],),
            )[0]
            expected = {
                key: payload[key]
                for key in ("relative_path", "original_bytes", "stored_bytes", "compression")
            }
            if stored != expected:
                raise ValueError("raw payload metadata conflicts with existing content hash")
            self._con.execute(
                """
                INSERT INTO raw_snapshots
                (snapshot_id, provider, dataset, artifact_kind, requested_at,
                 received_at, http_status, content_type, request_fingerprint,
                 payload_sha256, adapter_name, adapter_version, parser_version,
                 parsed_row_count, parsed_rows_sha256, parsed_rows_rejected,
                 parsed_rejection_codes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot["snapshot_id"],
                    snapshot["provider"],
                    snapshot["dataset"],
                    snapshot["artifact_kind"],
                    _utc_naive_database_timestamp(
                        snapshot["requested_at"],
                        label="raw snapshot requested_at",
                    ),
                    _utc_naive_database_timestamp(
                        snapshot["received_at"],
                        label="raw snapshot received_at",
                    ),
                    snapshot.get("http_status"),
                    snapshot.get("content_type"),
                    snapshot["request_fingerprint"],
                    snapshot["payload_sha256"],
                    snapshot["adapter_name"],
                    snapshot["adapter_version"],
                    snapshot["parser_version"],
                    snapshot.get("parsed_row_count"),
                    snapshot.get("parsed_rows_sha256"),
                    snapshot.get("parsed_rows_rejected"),
                    snapshot.get("parsed_rejection_codes"),
                ),
            )
            if ingest_run_id is not None:
                self._con.execute(
                    """
                    INSERT INTO ingest_raw_snapshots (run_id, snapshot_id, role)
                    VALUES (?, ?, ?)
                    """,
                    (ingest_run_id, snapshot["snapshot_id"], role),
                )
            self._con.execute("COMMIT")
        except Exception:
            self._con.execute("ROLLBACK")
            raise

    def raw_payload_records(self) -> list[dict]:
        """Return immutable payload records for filesystem verification."""
        return self.query(
            """
            SELECT payload_sha256, relative_path, original_bytes, stored_bytes,
                   compression
            FROM raw_payloads
            ORDER BY relative_path
            """
        )

    def raw_payload_record(self, payload_sha256: str) -> dict | None:
        """Return an existing globally deduplicated payload, if present."""
        rows = self.query(
            """
            SELECT payload_sha256, relative_path, original_bytes, stored_bytes,
                   compression
            FROM raw_payloads
            WHERE payload_sha256 = ?
            """,
            (payload_sha256,),
        )
        return rows[0] if rows else None

    def raw_snapshot_for_run_role(self, run_id: str, role: str) -> dict[str, Any]:
        """Return exactly one staged snapshot without requiring an ingest outcome.

        Universe-change evidence is captured before its canonical transaction,
        so ``ingest_log`` intentionally has no success row yet.  This lookup
        binds the preallocated run and semantic role directly to the complete
        immutable snapshot/payload contract and refuses ambiguous linkage.
        """

        normalized_run_id = str(run_id).strip()
        normalized_role = str(role).strip()
        if not normalized_run_id or not normalized_role:
            raise ValueError("raw snapshot lookup requires run_id and role")
        link_count = int(
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM ingest_raw_snapshots
                WHERE run_id = ? AND role = ?
                """,
                (normalized_run_id, normalized_role),
            )[0]["n"]
        )
        if link_count != 1:
            raise ValueError(
                "raw snapshot run/role must resolve to exactly one observation: "
                f"{normalized_run_id}:{normalized_role} resolved to {link_count}"
            )
        rows = self.query(
            """
            SELECT linked.run_id, linked.role, linked.linked_at,
                   snapshot.snapshot_id, snapshot.provider, snapshot.dataset,
                   snapshot.artifact_kind, snapshot.requested_at,
                   snapshot.received_at, snapshot.http_status,
                   snapshot.content_type, snapshot.request_fingerprint,
                   snapshot.payload_sha256, snapshot.adapter_name,
                   snapshot.adapter_version, snapshot.parser_version,
                   snapshot.parsed_row_count, snapshot.parsed_rows_sha256,
                   snapshot.parsed_rows_rejected,
                   snapshot.parsed_rejection_codes, snapshot.created_at,
                   payload.relative_path, payload.original_bytes,
                   payload.stored_bytes, payload.compression
            FROM ingest_raw_snapshots AS linked
            JOIN raw_snapshots AS snapshot USING (snapshot_id)
            JOIN raw_payloads AS payload USING (payload_sha256)
            WHERE linked.run_id = ? AND linked.role = ?
            ORDER BY snapshot.snapshot_id
            """,
            (normalized_run_id, normalized_role),
        )
        if len(rows) != 1:
            raise ValueError(
                "raw snapshot run/role has incomplete snapshot or payload linkage: "
                f"{normalized_run_id}:{normalized_role}"
            )
        return rows[0]

    def attach_raw_snapshot_parse_evidence(
        self,
        *,
        ingest_run_id: str,
        role: str,
        expected_parser_version: str,
        parser_version: str,
        parsed_row_count: int,
        parsed_rows_sha256: str,
        parsed_rows_rejected: int = 0,
        parsed_rejection_codes: tuple[str, ...] | list[str] | None = None,
    ) -> str:
        """Atomically promote one linked capture after its parser succeeds.

        External bytes are registered before parsing so malformed responses are
        never lost. A successful downstream parser calls this method once to
        bind its canonical row count/hash to that exact fetch observation.
        Repeating the same promotion is idempotent; conflicting evidence fails.
        """
        run_id = ingest_run_id.strip()
        normalized_role = role.strip()
        expected_version = expected_parser_version.strip()
        final_version = parser_version.strip()
        digest = parsed_rows_sha256.strip().lower()
        if (
            isinstance(parsed_rows_rejected, bool)
            or not isinstance(parsed_rows_rejected, int)
            or parsed_rows_rejected < 0
        ):
            raise ValueError("raw snapshot rejected row count cannot be negative")
        encoded_rejection_codes = canonical_rejection_codes(parsed_rejection_codes)
        if (parsed_rows_rejected == 0) != (encoded_rejection_codes is None):
            raise ValueError("raw snapshot rejection count and codes are inconsistent")
        if not run_id or not normalized_role:
            raise ValueError("raw snapshot parse evidence requires an ingest run and role")
        if not expected_version or not final_version:
            raise ValueError("raw snapshot parse evidence requires parser versions")
        if isinstance(parsed_row_count, bool) or parsed_row_count < 0:
            raise ValueError("raw snapshot parsed row count cannot be negative")
        if len(digest) != 64:
            raise ValueError("raw snapshot parsed-row hash must be a 64-character SHA-256")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ValueError("raw snapshot parsed-row hash must be a 64-character SHA-256") from exc

        self._con.execute("BEGIN TRANSACTION")
        try:
            current = self.raw_snapshot_for_run_role(run_id, normalized_role)
            current_count = current["parsed_row_count"]
            current_hash = current["parsed_rows_sha256"]
            current_rejected = current["parsed_rows_rejected"]
            current_rejection_codes = current["parsed_rejection_codes"]
            current_version = str(current["parser_version"])
            if current_count is None and current_hash is None:
                if current_version != expected_version:
                    raise ValueError("raw snapshot capture parser version changed before promotion")
                self._con.execute(
                    """
                    UPDATE raw_snapshots
                    SET parser_version = ?, parsed_row_count = ?,
                        parsed_rows_sha256 = ?, parsed_rows_rejected = ?,
                        parsed_rejection_codes = ?
                    WHERE snapshot_id = ?
                    """,
                    (
                        final_version,
                        parsed_row_count,
                        digest,
                        parsed_rows_rejected,
                        encoded_rejection_codes,
                        current["snapshot_id"],
                    ),
                )
            elif current_count is None or current_hash is None or current_rejected is None:
                raise ValueError("raw snapshot has incomplete parsed evidence")
            elif (
                current_version != final_version
                or int(current_count) != parsed_row_count
                or str(current_hash) != digest
                or int(current_rejected) != parsed_rows_rejected
                or current_rejection_codes != encoded_rejection_codes
            ):
                raise ValueError("raw snapshot parsed evidence conflicts with existing values")
            self._con.execute("COMMIT")
            return str(current["snapshot_id"])
        except Exception:
            self._con.execute("ROLLBACK")
            raise

    def apply_universe_coverage_attestation(
        self,
        attestation: dict[str, Any],
        references: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Record one review and atomically extend every bounded reference window.

        ``accepted_no_change`` is the only status allowed to mutate reference
        windows. The exact official/component responses must already be linked
        to the attestation's ingest run. Any mismatch rolls back the attestation
        and every reference update together.
        """
        status = str(attestation.get("status") or "").strip()
        if status not in {"accepted_no_change", "blocked_review_required"}:
            raise ValueError("unsupported universe coverage attestation status")
        prior = _as_date(attestation["prior_coverage_through"])
        target = _as_date(attestation["requested_coverage_through"])
        if target <= prior:
            raise ValueError("universe coverage target must follow prior coverage")

        counts = {
            "membership_rows_extended": 0,
            "security_rows_extended": 0,
            "owner_rows_extended": 0,
            "cik_rows_extended": 0,
            "provider_rows_extended": 0,
        }
        normalized_references = [
            {
                "ticker": _required_text(row, "ticker", "universe reference").upper(),
                "security_id": _required_text(row, "security_id", "universe reference"),
                "issuer_id": _required_text(row, "issuer_id", "universe reference"),
                "cik": _required_text(row, "cik", "universe reference"),
            }
            for row in references
        ]
        if status == "accepted_no_change" and not normalized_references:
            raise ValueError("accepted universe coverage requires reviewed references")

        self._con.register("_tmp_coverage_refs", _rows_to_arrowable(normalized_references))
        self._con.execute("BEGIN TRANSACTION")
        try:
            run_id = _required_text(attestation, "run_id", "universe attestation")
            evidence = self.query(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE linked.role LIKE 'official_release_archive_page_%'
                    ) AS official_pages,
                    COUNT(*) FILTER (
                        WHERE linked.role = 'independent_component_snapshot'
                    ) AS component_pages
                FROM ingest_raw_snapshots AS linked
                JOIN raw_snapshots AS snapshot USING (snapshot_id)
                WHERE linked.run_id = ?
                  AND snapshot.artifact_kind = 'exact_response'
                  AND snapshot.http_status BETWEEN 200 AND 299
                """,
                (run_id,),
            )[0]
            if int(evidence["official_pages"]) < 1 or int(evidence["component_pages"]) != 1:
                raise ValueError(
                    "universe attestation requires archived official and component responses"
                )

            if status == "accepted_no_change":
                expected = {
                    (str(row["ticker"]), str(row["security_id"])) for row in normalized_references
                }
                active_members = self.query(
                    """
                    SELECT membership.ticker, membership.security_id,
                           membership.effective_end, membership.end_known_date
                    FROM universe_membership AS membership
                    WHERE membership.universe_id = ?
                      AND membership.known_date <= CAST(? AS DATE)
                      AND membership.effective_start <= CAST(? AS DATE)
                      AND (
                          membership.effective_end IS NULL
                          OR membership.effective_end > CAST(? AS DATE)
                          OR membership.end_known_date > CAST(? AS DATE)
                      )
                    ORDER BY membership.ticker
                    """,
                    (
                        attestation["universe_id"],
                        prior.isoformat(),
                        prior.isoformat(),
                        prior.isoformat(),
                        prior.isoformat(),
                    ),
                )
                observed = {(str(row["ticker"]), str(row["security_id"])) for row in active_members}
                if observed != expected or len(active_members) != len(expected):
                    raise ValueError("reviewed member/reference set changed before extension")
                boundary = prior + timedelta(days=1)
                if any(
                    row["effective_end"] != boundary or row["end_known_date"] != prior
                    for row in active_members
                ):
                    raise ValueError(
                        "universe rows do not share the expected certified coverage boundary"
                    )

                identity_rows = self.query(
                    """
                    SELECT identity.ticker, identity.security_id, identity.effective_end
                    FROM security_identity_assignments AS identity
                    JOIN _tmp_coverage_refs AS reference
                      ON reference.ticker = identity.ticker
                     AND reference.security_id = identity.security_id
                    WHERE identity.universe_id = ?
                      AND identity.known_date <= CAST(? AS DATE)
                      AND identity.effective_start <= CAST(? AS DATE)
                      AND (
                          identity.effective_end IS NULL
                          OR identity.effective_end > CAST(? AS DATE)
                      )
                    """,
                    (
                        attestation["universe_id"],
                        prior.isoformat(),
                        prior.isoformat(),
                        prior.isoformat(),
                    ),
                )
                if (
                    {(str(row["ticker"]), str(row["security_id"])) for row in identity_rows}
                    != expected
                    or len(identity_rows) != len(expected)
                    or any(row["effective_end"] != boundary for row in identity_rows)
                ):
                    raise ValueError("security identity windows do not match the member boundary")

                owner_rows = self.query(
                    """
                    SELECT owner.security_id, owner.issuer_id, owner.effective_end
                    FROM security_issuer_assignments AS owner
                    JOIN _tmp_coverage_refs AS reference
                      ON reference.security_id = owner.security_id
                     AND reference.issuer_id = owner.issuer_id
                    WHERE owner.effective_start <= CAST(? AS DATE)
                      AND (owner.effective_end IS NULL OR owner.effective_end > CAST(? AS DATE))
                    """,
                    (prior.isoformat(), prior.isoformat()),
                )
                expected_owners = {
                    (str(row["security_id"]), str(row["issuer_id"]))
                    for row in normalized_references
                }
                if (
                    {(str(row["security_id"]), str(row["issuer_id"])) for row in owner_rows}
                    != expected_owners
                    or len(owner_rows) != len(expected_owners)
                    or any(row["effective_end"] != boundary for row in owner_rows)
                ):
                    raise ValueError("issuer ownership windows do not match the member boundary")

                expected_ciks = {
                    (str(row["issuer_id"]), str(row["cik"])) for row in normalized_references
                }
                cik_rows = self.query(
                    """
                    SELECT cik.issuer_id, cik.cik, cik.effective_end
                    FROM issuer_cik_history AS cik
                    JOIN (
                        SELECT DISTINCT issuer_id, cik FROM _tmp_coverage_refs
                    ) AS reference
                      ON reference.issuer_id = cik.issuer_id
                     AND reference.cik = cik.cik
                    WHERE cik.effective_start <= CAST(? AS DATE)
                      AND (cik.effective_end IS NULL OR cik.effective_end > CAST(? AS DATE))
                    """,
                    (prior.isoformat(), prior.isoformat()),
                )
                if (
                    {(str(row["issuer_id"]), str(row["cik"])) for row in cik_rows} != expected_ciks
                    or len(cik_rows) != len(expected_ciks)
                    or any(row["effective_end"] != boundary for row in cik_rows)
                ):
                    raise ValueError("issuer CIK windows do not match the member boundary")

                provider_rows = self.query(
                    """
                    SELECT mapping.provider, mapping.security_id, mapping.data_start,
                           mapping.data_end
                    FROM provider_symbol_history AS mapping
                    JOIN (
                        SELECT DISTINCT security_id FROM _tmp_coverage_refs
                    ) AS reference USING (security_id)
                    WHERE mapping.mapping_status = 'verified'
                      AND mapping.data_start <= CAST(? AS DATE)
                      AND (mapping.data_end IS NULL OR mapping.data_end > CAST(? AS DATE))
                    """,
                    (prior.isoformat(), prior.isoformat()),
                )
                provider_security_ids = {str(row["security_id"]) for row in provider_rows}
                if provider_security_ids != {
                    str(row["security_id"]) for row in normalized_references
                } or any(row["data_end"] != boundary for row in provider_rows):
                    raise ValueError("provider-symbol windows do not match the member boundary")

                marker = f"|coverage-attestation:{attestation['attestation_id']}"
                membership_source = (
                    "regexp_replace("
                    "regexp_replace(source, '\\\\|coverage-end:[0-9-]+$', ''), "
                    "'\\\\|coverage-attestation:[^|]+', '') || ?"
                )
                counts["membership_rows_extended"] = int(
                    self._con.execute(
                        f"""
                        UPDATE universe_membership AS membership
                        SET effective_end = CAST(? AS DATE),
                            end_known_date = CAST(? AS DATE),
                            source = {membership_source},
                            fetched_at = now()
                        FROM _tmp_coverage_refs AS reference
                        WHERE membership.universe_id = ?
                          AND membership.ticker = reference.ticker
                          AND membership.security_id = reference.security_id
                          AND membership.effective_end = CAST(? AS DATE)
                          AND membership.end_known_date = CAST(? AS DATE)
                        """,
                        (
                            (target + timedelta(days=1)).isoformat(),
                            target.isoformat(),
                            marker,
                            attestation["universe_id"],
                            boundary.isoformat(),
                            prior.isoformat(),
                        ),
                    ).fetchone()[0]
                )
                counts["security_rows_extended"] = int(
                    self._con.execute(
                        f"""
                        UPDATE security_identity_assignments AS identity
                        SET effective_end = CAST(? AS DATE),
                            source = {membership_source},
                            fetched_at = now()
                        FROM _tmp_coverage_refs AS reference
                        WHERE identity.universe_id = ?
                          AND identity.ticker = reference.ticker
                          AND identity.security_id = reference.security_id
                          AND identity.effective_end = CAST(? AS DATE)
                        """,
                        (
                            (target + timedelta(days=1)).isoformat(),
                            marker,
                            attestation["universe_id"],
                            boundary.isoformat(),
                        ),
                    ).fetchone()[0]
                )
                counts["owner_rows_extended"] = int(
                    self._con.execute(
                        """
                        UPDATE security_issuer_assignments AS owner
                        SET effective_end = CAST(? AS DATE), fetched_at = now()
                        FROM _tmp_coverage_refs AS reference
                        WHERE owner.security_id = reference.security_id
                          AND owner.issuer_id = reference.issuer_id
                          AND owner.effective_end = CAST(? AS DATE)
                        """,
                        (
                            (target + timedelta(days=1)).isoformat(),
                            boundary.isoformat(),
                        ),
                    ).fetchone()[0]
                )
                counts["cik_rows_extended"] = int(
                    self._con.execute(
                        """
                        UPDATE issuer_cik_history AS cik
                        SET effective_end = CAST(? AS DATE), fetched_at = now()
                        FROM (
                            SELECT DISTINCT issuer_id, cik FROM _tmp_coverage_refs
                        ) AS reference
                        WHERE cik.issuer_id = reference.issuer_id
                          AND cik.cik = reference.cik
                          AND cik.effective_end = CAST(? AS DATE)
                        """,
                        (
                            (target + timedelta(days=1)).isoformat(),
                            boundary.isoformat(),
                        ),
                    ).fetchone()[0]
                )
                counts["provider_rows_extended"] = int(
                    self._con.execute(
                        """
                        UPDATE provider_symbol_history AS mapping
                        SET data_end = CAST(? AS DATE), fetched_at = now()
                        FROM (
                            SELECT DISTINCT security_id FROM _tmp_coverage_refs
                        ) AS reference
                        WHERE mapping.security_id = reference.security_id
                          AND mapping.mapping_status = 'verified'
                          AND mapping.data_end = CAST(? AS DATE)
                        """,
                        (
                            (target + timedelta(days=1)).isoformat(),
                            boundary.isoformat(),
                        ),
                    ).fetchone()[0]
                )
                expected_counts = {
                    "membership_rows_extended": len(expected),
                    "security_rows_extended": len(expected),
                    "owner_rows_extended": len(expected_owners),
                    "cik_rows_extended": len(expected_ciks),
                    "provider_rows_extended": len(provider_rows),
                }
                if counts != expected_counts:
                    raise ValueError(
                        f"reference extension count mismatch: {counts} != {expected_counts}"
                    )

            row = dict(attestation)
            row.update(counts)
            self._insert_universe_coverage_attestation(row)
            self._con.execute("COMMIT")
            return counts
        except Exception:
            self._con.execute("ROLLBACK")
            raise
        finally:
            self._con.unregister("_tmp_coverage_refs")

    def _insert_universe_coverage_attestation(self, row: dict[str, Any]) -> None:
        """Insert one immutable universe review inside the caller's transaction."""
        columns = (
            "attestation_id",
            "run_id",
            "universe_id",
            "prior_coverage_through",
            "requested_coverage_through",
            "checked_at",
            "completed_new_york_date",
            "status",
            "official_source_url",
            "component_source_url",
            "official_release_count",
            "relevant_release_count",
            "reviewed_member_count",
            "component_count",
            "reviewed_member_set_sha256",
            "component_set_sha256",
            "identity_match_count",
            "identity_mismatch_count",
            "candidate_releases_json",
            "mismatch_detail_json",
            "membership_rows_extended",
            "security_rows_extended",
            "owner_rows_extended",
            "cik_rows_extended",
            "provider_rows_extended",
            "detail",
        )
        missing = [column for column in columns if column not in row]
        if missing:
            raise ValueError("universe attestation is missing fields: " + ", ".join(missing))
        placeholders = ", ".join("?" for _ in columns)
        self._con.execute(
            f"""
            INSERT INTO universe_coverage_attestations ({", ".join(columns)})
            VALUES ({placeholders})
            """,
            tuple(
                _utc_naive_database_timestamp(
                    row[column],
                    label="universe attestation checked_at",
                )
                if column == "checked_at"
                else row[column]
                for column in columns
            ),
        )

    def universe_coverage_attestations(self, limit: int = 20) -> list[dict]:
        """Return the newest immutable no-change reviews for operators."""
        if limit < 1:
            return []
        return self.query(
            """
            SELECT *
            FROM universe_coverage_attestations
            ORDER BY checked_at DESC, created_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    def ingest_history(self, limit: int = 20) -> list[dict]:
        """Return the most recent ingest outcomes for operators and agents."""
        if limit < 1:
            return []
        columns = {
            row["column_name"]
            for row in self.query(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'main' AND table_name = 'ingest_log'
                """
            )
        }
        present = {"subject_type", "subject_id"} & columns
        if present and present != {"subject_type", "subject_id"}:
            raise RuntimeError(
                "Ingest subject schema is incomplete: subject_type and "
                "subject_id must exist together."
            )
        subject_projection = (
            "subject_type, subject_id" if present else "NULL AS subject_type, NULL AS subject_id"
        )
        rejection_projection = (
            "rejection_codes" if "rejection_codes" in columns else "NULL AS rejection_codes"
        )
        return self.query(
            f"""
            SELECT id, run_id, source, table_name, {subject_projection},
                   rows_inserted, rows_rejected, started_at, finished_at,
                   status, error, {rejection_projection}
            FROM ingest_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )

    def ingest_evidence(self, run_id: str) -> dict | None:
        """Return one ingest outcome and the immutable payload metadata it cites.

        A read-only connection can point at a database created before subject
        provenance existed. Both missing subject columns are represented as
        ``None`` without upgrading that database. A one-column partial schema
        is ambiguous and fails closed.
        """
        normalized_run_id = str(run_id).strip()
        if not normalized_run_id:
            raise ValueError("ingest evidence requires a run id")
        columns = {
            row["column_name"]
            for row in self.query(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'main' AND table_name = 'ingest_log'
                """
            )
        }
        present = {"subject_type", "subject_id"} & columns
        if present and present != {"subject_type", "subject_id"}:
            raise RuntimeError(
                "Ingest subject schema is incomplete: subject_type and "
                "subject_id must exist together."
            )
        subject_projection = (
            "subject_type, subject_id" if present else "NULL AS subject_type, NULL AS subject_id"
        )
        rejection_projection = (
            "rejection_codes" if "rejection_codes" in columns else "NULL AS rejection_codes"
        )

        outcomes = self.query(
            f"""
            SELECT id, run_id, source, table_name, {subject_projection},
                   rows_inserted, rows_rejected, started_at, finished_at,
                   status, error, {rejection_projection}
            FROM ingest_log
            WHERE run_id = ?
            ORDER BY id
            """,
            (normalized_run_id,),
        )
        if not outcomes:
            return None
        if len(outcomes) != 1:
            raise ValueError(f"ingest run {normalized_run_id!r} is ambiguous")

        evidence = outcomes[0]
        evidence["snapshots"] = self.query(
            """
            SELECT linked.role, linked.linked_at,
                   snapshot.snapshot_id, snapshot.provider, snapshot.dataset,
                   snapshot.artifact_kind, snapshot.requested_at,
                   snapshot.received_at, snapshot.http_status,
                   snapshot.content_type, snapshot.request_fingerprint,
                   snapshot.payload_sha256, snapshot.adapter_name,
                   snapshot.adapter_version, snapshot.parser_version,
                   snapshot.parsed_row_count, snapshot.parsed_rows_sha256,
                   payload.relative_path, payload.original_bytes,
                   payload.stored_bytes, payload.compression
            FROM ingest_raw_snapshots AS linked
            LEFT JOIN raw_snapshots AS snapshot USING (snapshot_id)
            LEFT JOIN raw_payloads AS payload USING (payload_sha256)
            WHERE linked.run_id = ?
            ORDER BY linked.role, linked.snapshot_id
            """,
            (normalized_run_id,),
        )
        return evidence

    def sec_fundamental_lineage_rows(
        self,
        issuer_ids: list[str],
        as_of: date | str,
    ) -> list[dict]:
        """Return only rows linked to a successful, subject-scoped SEC ingest.

        This is metadata validation, not the final trust decision. The anomaly
        detector still replays the exact response and verifies each row hash.
        Legacy rows with nullable lineage are deliberately excluded.
        """
        normalized = sorted(
            {str(issuer_id).strip() for issuer_id in issuer_ids if str(issuer_id).strip()}
        )
        if not normalized:
            return []
        columns = {
            row["column_name"]
            for row in self.query(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'main' AND table_name = 'fundamentals'
                """
            )
        }
        required = {
            "ingest_run_id",
            "source_snapshot_id",
            "source_rowset_sha256",
            "source_row_sha256",
        }
        if not required <= columns:
            return []
        ingest_columns = {
            row["column_name"]
            for row in self.query(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'main' AND table_name = 'ingest_log'
                """
            )
        }
        subject_present = {"subject_type", "subject_id"} & ingest_columns
        if subject_present and subject_present != {"subject_type", "subject_id"}:
            raise RuntimeError(
                "Ingest subject schema is incomplete: subject_type and "
                "subject_id must exist together."
            )
        if not subject_present:
            return []
        rejection_projection = (
            "outcome.rejection_codes AS ingest_rejection_codes"
            if "rejection_codes" in ingest_columns
            else "NULL AS ingest_rejection_codes"
        )
        locator_projection = (
            "fundamental.source_fact_locator"
            if "source_fact_locator" in columns
            else "NULL AS source_fact_locator"
        )
        placeholders = ",".join("?" for _ in normalized)
        rows = self.query(
            f"""
            SELECT fundamental.ticker, fundamental.issuer_id,
                   fundamental.security_id, fundamental.period_end,
                   fundamental.as_of_date, fundamental.fiscal_period,
                   fundamental.statement, fundamental.metric,
                   fundamental.value, fundamental.quarter_value,
                   fundamental.unit, fundamental.source,
                   fundamental.ingest_run_id,
                   fundamental.source_snapshot_id,
                   fundamental.source_rowset_sha256,
                   fundamental.source_row_sha256,
                   {locator_projection},
                   outcome.id AS ingest_id,
                   outcome.rows_inserted AS ingest_rows_inserted,
                   outcome.finished_at AS ingest_finished_at,
                   outcome.status AS ingest_status,
                   outcome.error AS ingest_error,
                   {rejection_projection}
            FROM fundamentals AS fundamental
            JOIN ingest_log AS outcome
              ON outcome.run_id = fundamental.ingest_run_id
             AND outcome.table_name = 'fundamentals'
             AND outcome.source LIKE 'edgar:%'
             AND outcome.status IN ('success', 'warning')
             AND outcome.rows_inserted > 0
             AND outcome.subject_type = 'issuer'
             AND outcome.subject_id = fundamental.issuer_id
            JOIN ingest_raw_snapshots AS linked
              ON linked.run_id = fundamental.ingest_run_id
             AND linked.snapshot_id = fundamental.source_snapshot_id
             AND linked.role = 'companyfacts'
            JOIN raw_snapshots AS snapshot
              ON snapshot.snapshot_id = linked.snapshot_id
             AND snapshot.provider = 'sec-edgar'
             AND snapshot.dataset = 'companyfacts'
             AND snapshot.artifact_kind = 'exact_response'
             AND snapshot.http_status BETWEEN 200 AND 299
             AND snapshot.parsed_row_count > 0
             AND snapshot.parsed_rows_sha256 =
                 fundamental.source_rowset_sha256
            WHERE fundamental.issuer_id IN ({placeholders})
              AND fundamental.as_of_date <= CAST(? AS DATE)
              AND fundamental.period_end <= fundamental.as_of_date
            ORDER BY fundamental.issuer_id, outcome.id,
                     fundamental.period_end, fundamental.as_of_date,
                     fundamental.metric
            """,
            (*normalized, str(as_of)),
        )
        return [
            row
            for row in rows
            if accepted_sec_fundamental_outcome(
                status=row.get("ingest_status"),
                error=row.get("ingest_error"),
                rejection_codes=row.get("ingest_rejection_codes"),
            )
        ]

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
            "fundamental_evidence_unversioned_projection",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM fundamentals AS current
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM fundamental_versions AS version
                    WHERE version.ticker = current.ticker
                      AND version.period_end = current.period_end
                      AND version.as_of_date = current.as_of_date
                      AND version.metric = current.metric
                      AND version.is_deleted = FALSE
                      AND version.issuer_id IS NOT DISTINCT FROM current.issuer_id
                      AND version.security_id IS NOT DISTINCT FROM current.security_id
                      AND version.ingest_run_id IS NOT DISTINCT FROM current.ingest_run_id
                      AND version.source_snapshot_id IS NOT DISTINCT FROM
                          current.source_snapshot_id
                      AND version.source_rowset_sha256 IS NOT DISTINCT FROM
                          current.source_rowset_sha256
                      AND version.source_row_sha256 IS NOT DISTINCT FROM
                          current.source_row_sha256
                      AND version.source_fact_locator IS NOT DISTINCT FROM
                          current.source_fact_locator
                      AND version.fiscal_period IS NOT DISTINCT FROM current.fiscal_period
                      AND version.statement IS NOT DISTINCT FROM current.statement
                      AND version.value IS NOT DISTINCT FROM current.value
                      AND version.quarter_value IS NOT DISTINCT FROM current.quarter_value
                      AND version.unit IS NOT DISTINCT FROM current.unit
                      AND version.source IS NOT DISTINCT FROM current.source
                )
                """
            )[0]["n"],
            "fail",
            "Every current fundamental must have an immutable system-time version.",
        )
        add(
            "fundamental_evidence_latest_projection_mismatch",
            self._fundamental_projection_version_mismatch_count(),
            "fail",
            "Latest fact versions, including deletion tombstones, must exactly "
            "reconstruct the current fundamental projection.",
        )
        add(
            "fundamental_evidence_generation_ahead_of_history",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM fundamental_evidence_generations AS generation
                WHERE generation.version_sequence > COALESCE(
                    (SELECT MAX(version_sequence) FROM fundamental_versions),
                    0
                )
                """
            )[0]["n"],
            "fail",
            "Named evidence generations cannot point beyond immutable fact history.",
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
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM prices
                WHERE close IS NULL
                   OR NOT isfinite(close)
                   OR close <= 0
                """
            )[0]["n"],
            "fail",
            "A missing, non-finite, or non-positive close cannot support valuation or returns.",
        )
        add(
            "prices_unverified_corporate_actions",
            self.query("SELECT COUNT(*) AS n FROM prices WHERE actions_complete IS NOT TRUE")[0][
                "n"
            ],
            "warn",
            "Refresh these rows before action-aware factors or after-tax backtests.",
        )
        add(
            "prices_repaired_ohlc_envelope",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM prices
                WHERE source = 'yfinance:ohlc-envelope-v1'
                """
            )[0]["n"],
            "warn",
            "Raw Yahoo evidence had a sub-1% OHLC envelope inconsistency; "
            "the stored high/low conservatively include open and close.",
        )
        add(
            "prices_unknown_split_adjustment_basis",
            self.query("SELECT COUNT(*) AS n FROM prices WHERE close_split_adjusted IS NULL")[0][
                "n"
            ],
            "fail",
            "Every close must declare whether its provider already normalized splits.",
        )
        add(
            "tagged_prices_unknown_split_normalization_factor",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM prices
                WHERE security_id IS NOT NULL
                  AND close_split_adjusted IS TRUE
                  AND split_normalization_factor IS NULL
                """
            )[0]["n"],
            "fail",
            "Reviewed split-normalized closes need a factor restoring their contemporaneous basis.",
        )
        add(
            "prices_invalid_split_normalization_factor",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM prices
                WHERE split_normalization_factor IS NOT NULL
                  AND split_normalization_factor <= 0
                """
            )[0]["n"],
            "fail",
            "Split-normalization factors must be positive.",
        )
        add(
            "factor_price_provenance_invalid_intervals",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM factor_price_provenance
                WHERE data_end <= data_start
                   OR overlap_start <> data_end
                   OR overlap_end <= overlap_start
                   OR NOT regexp_matches(payload_sha256, '^[0-9a-f]{64}$')
                   OR NOT regexp_matches(overlap_sha256, '^[0-9a-f]{64}$')
                """
            )[0]["n"],
            "fail",
            "Warm-up provenance needs valid half-open windows and canonical payload hashes.",
        )
        add(
            "factor_price_provenance_future_verification_dates",
            self.query(
                """
                SELECT COUNT(*) AS n FROM factor_price_provenance
                WHERE verified_date > CURRENT_DATE
                """
            )[0]["n"],
            "fail",
            "Warm-up evidence cannot be marked reviewed in the future.",
        )
        add(
            "factor_price_provenance_orphans",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM factor_price_provenance AS provenance
                LEFT JOIN security_master AS security USING (security_id)
                WHERE security.security_id IS NULL
                """
            )[0]["n"],
            "fail",
            "Every warm-up snapshot must reference a reviewed security identity.",
        )
        add(
            "factor_price_provenance_unanchored",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM factor_price_provenance AS provenance
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM provider_symbol_history AS mapping
                    WHERE mapping.security_id = provenance.security_id
                      AND mapping.provider = provenance.provider
                      AND mapping.provider_symbol = provenance.provider_symbol
                      AND mapping.mapping_status = 'verified'
                      AND mapping.data_start = provenance.data_end
                )
                """
            )[0]["n"],
            "fail",
            "Warm-up history must meet an exact reviewed provider-series anchor.",
        )
        add(
            "factor_prices_outside_provenance",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM factor_prices AS price
                LEFT JOIN factor_price_provenance AS provenance USING (provenance_id)
                WHERE provenance.provenance_id IS NULL
                   OR price.security_id <> provenance.security_id
                   OR price.provider <> provenance.provider
                   OR price.provider_symbol <> provenance.provider_symbol
                   OR price.date < provenance.data_start
                   OR price.date >= provenance.data_end
                """
            )[0]["n"],
            "fail",
            "Every factor-price row must remain inside its hashed review window.",
        )
        add(
            "factor_prices_invalid_rows",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM factor_prices
                WHERE close <= 0
                   OR dividends < 0
                   OR split_ratio <= 0
                   OR actions_complete IS NOT TRUE
                   OR close_split_adjusted IS NULL
                   OR split_normalization_factor <= 0
                """
            )[0]["n"],
            "fail",
            "Warm-up factor prices require valid closes, actions, and split basis.",
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
            "macro_primary_fallback_divergence",
            self.query(
                """
                WITH fred_ranked AS (
                    SELECT series_id, date, value,
                           ROW_NUMBER() OVER (
                               PARTITION BY series_id, date
                               ORDER BY release_date DESC, fetched_at DESC
                           ) AS rn
                    FROM macro
                    WHERE source = 'fred'
                ), treasury_ranked AS (
                    SELECT series_id, date, value,
                           ROW_NUMBER() OVER (
                               PARTITION BY series_id, date
                               ORDER BY release_date DESC, fetched_at DESC
                           ) AS rn
                    FROM macro
                    WHERE source = 'treasury'
                )
                SELECT COUNT(*) AS n
                FROM fred_ranked AS primary_source
                JOIN treasury_ranked AS fallback_source
                  ON fallback_source.series_id = primary_source.series_id
                 AND fallback_source.date = primary_source.date
                WHERE primary_source.rn = 1
                  AND fallback_source.rn = 1
                  AND ABS(primary_source.value - fallback_source.value) > 0.05
                """
            )[0]["n"],
            "warn",
            "FRED and Treasury yields for the same observation date should agree within 5 bps.",
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
            "universe_missing_end_known_dates",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM universe_membership
                WHERE effective_end IS NOT NULL AND end_known_date IS NULL
                """
            )[0]["n"],
            "fail",
            "Every finite universe interval needs independently dated end knowledge.",
        )
        add(
            "universe_invalid_end_known_dates",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM universe_membership
                WHERE end_known_date IS NOT NULL
                  AND (
                      effective_end IS NULL
                      OR end_known_date < known_date
                      OR end_known_date > effective_end
                  )
                """
            )[0]["n"],
            "fail",
            "Membership end knowledge must be after start knowledge and no later than its end.",
        )
        add(
            "universe_future_known_dates",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM universe_membership
                WHERE known_date > CURRENT_DATE OR end_known_date > CURRENT_DATE
                """
            )[0]["n"],
            "fail",
            "Future membership knowledge dates cannot be used in a backtest.",
        )
        add(
            "universe_attestation_invalid_rows",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM universe_coverage_attestations AS attestation
                WHERE requested_coverage_through <= prior_coverage_through
                   OR requested_coverage_through > completed_new_york_date
                   OR official_release_count < 1
                   OR component_count NOT BETWEEN 450 AND 550
                   OR (
                       status = 'accepted_no_change'
                       AND (
                           relevant_release_count <> 0
                           OR identity_mismatch_count <> 0
                           OR reviewed_member_count <> component_count
                           OR identity_match_count <> reviewed_member_count
                           OR (
                               reviewed_member_set_sha256 <> component_set_sha256
                               AND NOT EXISTS (
                                   SELECT 1
                                   FROM universe_constituent_change_activations
                                       AS activation
                                   WHERE activation.activation_id = json_extract_string(
                                       mismatch_detail_json,
                                       '$.accepted_activation_component_lag.activation_id'
                                   )
                                     AND activation.status = 'accepted'
                                     AND (
                                         (
                                             requested_coverage_through -
                                                 activation.effective_date BETWEEN 0 AND 7
                                             AND json_extract_string(
                                                 mismatch_detail_json,
                                                 '$.accepted_activation_component_lag.mode'
                                             ) IS NULL
                                         )
                                         OR (
                                             requested_coverage_through >=
                                                 activation.effective_date
                                             AND json_extract_string(
                                                 mismatch_detail_json,
                                                 '$.accepted_activation_component_lag.mode'
                                             ) = 'receipt_bound_component_divergence'
                                             AND json_extract_string(
                                                 mismatch_detail_json,
                                                 '$.accepted_activation_component_lag.reconciliation_basis'
                                             ) = 'accepted_activation_receipt+dated_ivv_holdings'
                                             AND activation.universe_id = attestation.universe_id
                                             AND activation.policy_version LIKE
                                                 'governed-sp500-constituent-activation.%'
                                             AND attestation.reviewed_member_count = (
                                                 SELECT COUNT(*)
                                                 FROM universe_membership AS member
                                                 WHERE member.universe_id =
                                                     attestation.universe_id
                                                   AND member.effective_start <=
                                                     attestation.requested_coverage_through
                                                   AND (
                                                       member.effective_end IS NULL
                                                       OR member.effective_end >
                                                           attestation.requested_coverage_through
                                                   )
                                             )
                                             AND attestation.reviewed_member_set_sha256 = (
                                                 SELECT sha256(string_agg(
                                                     member.ticker,
                                                     chr(10) ORDER BY member.ticker
                                                 ))
                                                 FROM universe_membership AS member
                                                 WHERE member.universe_id =
                                                     attestation.universe_id
                                                   AND member.effective_start <=
                                                     attestation.requested_coverage_through
                                                   AND (
                                                       member.effective_end IS NULL
                                                       OR member.effective_end >
                                                           attestation.requested_coverage_through
                                                   )
                                             )
                                             AND activation.after_member_set_sha256 = (
                                                 SELECT sha256(CAST(to_json(list_sort(
                                                     list(member.ticker)
                                                 )) AS VARCHAR))
                                                 FROM universe_membership AS member
                                                 WHERE member.universe_id =
                                                     attestation.universe_id
                                                   AND member.effective_start <=
                                                     attestation.requested_coverage_through
                                                   AND (
                                                       member.effective_end IS NULL
                                                       OR member.effective_end >
                                                           attestation.requested_coverage_through
                                                   )
                                             )
                                             AND attestation.component_count = (
                                                 SELECT COUNT(*)
                                                 FROM (
                                                     SELECT member.ticker
                                                     FROM universe_membership AS member
                                                     WHERE member.universe_id =
                                                         attestation.universe_id
                                                       AND member.effective_start <=
                                                         attestation.requested_coverage_through
                                                       AND (
                                                           member.effective_end IS NULL
                                                           OR member.effective_end >
                                                               attestation.requested_coverage_through
                                                       )
                                                       AND member.ticker NOT IN (
                                                           SELECT json_extract_string(
                                                               value, '$.ticker'
                                                           )
                                                           FROM json_each(
                                                               activation.activation_payload_json,
                                                               '$.change_rows'
                                                           )
                                                           WHERE json_extract_string(
                                                               value, '$.action'
                                                           ) = 'addition'
                                                       )
                                                     UNION ALL
                                                     SELECT json_extract_string(
                                                         value, '$.ticker'
                                                     )
                                                     FROM json_each(
                                                         activation.activation_payload_json,
                                                         '$.change_rows'
                                                     )
                                                     WHERE json_extract_string(
                                                         value, '$.action'
                                                     ) = 'deletion'
                                                 ) AS receipt_before
                                             )
                                             AND attestation.component_set_sha256 = (
                                                 SELECT sha256(string_agg(
                                                     receipt_before.ticker,
                                                     chr(10) ORDER BY receipt_before.ticker
                                                 ))
                                                 FROM (
                                                     SELECT member.ticker
                                                     FROM universe_membership AS member
                                                     WHERE member.universe_id =
                                                         attestation.universe_id
                                                       AND member.effective_start <=
                                                         attestation.requested_coverage_through
                                                       AND (
                                                           member.effective_end IS NULL
                                                           OR member.effective_end >
                                                               attestation.requested_coverage_through
                                                       )
                                                       AND member.ticker NOT IN (
                                                           SELECT json_extract_string(
                                                               value, '$.ticker'
                                                           )
                                                           FROM json_each(
                                                               activation.activation_payload_json,
                                                               '$.change_rows'
                                                           )
                                                           WHERE json_extract_string(
                                                               value, '$.action'
                                                           ) = 'addition'
                                                       )
                                                     UNION ALL
                                                     SELECT json_extract_string(
                                                         value, '$.ticker'
                                                     )
                                                     FROM json_each(
                                                         activation.activation_payload_json,
                                                         '$.change_rows'
                                                     )
                                                     WHERE json_extract_string(
                                                         value, '$.action'
                                                     ) = 'deletion'
                                                 ) AS receipt_before
                                             )
                                             AND activation.before_member_set_sha256 = (
                                                 SELECT sha256(CAST(to_json(list_sort(
                                                     list(receipt_before.ticker)
                                                 )) AS VARCHAR))
                                                 FROM (
                                                     SELECT member.ticker
                                                     FROM universe_membership AS member
                                                     WHERE member.universe_id =
                                                         attestation.universe_id
                                                       AND member.effective_start <=
                                                         attestation.requested_coverage_through
                                                       AND (
                                                           member.effective_end IS NULL
                                                           OR member.effective_end >
                                                               attestation.requested_coverage_through
                                                       )
                                                       AND member.ticker NOT IN (
                                                           SELECT json_extract_string(
                                                               value, '$.ticker'
                                                           )
                                                           FROM json_each(
                                                               activation.activation_payload_json,
                                                               '$.change_rows'
                                                           )
                                                           WHERE json_extract_string(
                                                               value, '$.action'
                                                           ) = 'addition'
                                                       )
                                                     UNION ALL
                                                     SELECT json_extract_string(
                                                         value, '$.ticker'
                                                     )
                                                     FROM json_each(
                                                         activation.activation_payload_json,
                                                         '$.change_rows'
                                                     )
                                                     WHERE json_extract_string(
                                                         value, '$.action'
                                                     ) = 'deletion'
                                                 ) AS receipt_before
                                             )
                                             AND json_extract_string(
                                                 activation.activation_payload_json,
                                                 '$.receipt.activation_id'
                                             ) = activation.activation_id
                                             AND json_extract_string(
                                                 activation.activation_payload_json,
                                                 '$.receipt.universe_id'
                                             ) = activation.universe_id
                                             AND json_extract_string(
                                                 activation.activation_payload_json,
                                                 '$.receipt.status'
                                             ) = 'accepted'
                                             AND json_extract_string(
                                                 activation.activation_payload_json,
                                                 '$.receipt.policy_version'
                                             ) = activation.policy_version
                                             AND json_extract_string(
                                                 activation.activation_payload_json,
                                                 '$.receipt.before_member_set_sha256'
                                             ) = activation.before_member_set_sha256
                                             AND json_extract_string(
                                                 activation.activation_payload_json,
                                                 '$.receipt.after_member_set_sha256'
                                             ) = activation.after_member_set_sha256
                                             AND json_extract_string(
                                                 activation.activation_payload_json,
                                                 '$.post_event_reconciliation.source_url'
                                             ) = 'https://www.ishares.com/us/products/239726/ishares-core-s-p-500-etf/latest-holdings.csv'
                                             AND json_extract_string(
                                                 activation.activation_payload_json,
                                                 '$.post_event_reconciliation.review.fund'
                                             ) = 'IVV'
                                             AND json_extract_string(
                                                 mismatch_detail_json,
                                                 '$.accepted_activation_component_lag.holdings_as_of'
                                             ) = json_extract_string(
                                                 activation.activation_payload_json,
                                                 '$.post_event_reconciliation.review.as_of'
                                             )
                                             AND CAST(json_extract_string(
                                                 activation.activation_payload_json,
                                                 '$.post_event_reconciliation.review.as_of'
                                             ) AS DATE) BETWEEN activation.effective_date
                                                 AND requested_coverage_through
                                             AND (
                                                 SELECT COUNT(*)
                                                 FROM json_each(
                                                     activation.activation_payload_json,
                                                     '$.change_rows'
                                                 )
                                                 WHERE json_extract_string(
                                                     value, '$.action'
                                                 ) = 'addition'
                                             ) > 0
                                             AND (
                                                 SELECT COUNT(*)
                                                 FROM json_each(
                                                     activation.activation_payload_json,
                                                     '$.change_rows'
                                                 )
                                                 WHERE json_extract_string(
                                                     value, '$.action'
                                                 ) = 'addition'
                                             ) = (
                                                 SELECT COUNT(*)
                                                 FROM json_each(
                                                     activation.activation_payload_json,
                                                     '$.change_rows'
                                                 )
                                                 WHERE json_extract_string(
                                                     value, '$.action'
                                                 ) = 'deletion'
                                             )
                                             AND NOT EXISTS (
                                                 SELECT 1
                                                 FROM json_each(
                                                     activation.activation_payload_json,
                                                     '$.change_rows'
                                                 ) AS change
                                                 WHERE json_extract_string(
                                                     change.value, '$.action'
                                                 ) NOT IN ('addition', 'deletion')
                                                    OR COALESCE(json_extract_string(
                                                        change.value, '$.ticker'
                                                    ), '') = ''
                                                    OR json_extract_string(
                                                        change.value, '$.effective_date'
                                                    ) IS DISTINCT FROM CAST(
                                                        activation.effective_date AS VARCHAR
                                                    )
                                             )
                                             AND NOT EXISTS (
                                                 SELECT 1
                                                 FROM json_each(
                                                     activation.activation_payload_json,
                                                     '$.change_rows'
                                                 ) AS addition
                                                 WHERE json_extract_string(
                                                     addition.value, '$.action'
                                                 ) = 'addition'
                                                   AND NOT EXISTS (
                                                       SELECT 1
                                                       FROM json_each(
                                                           activation.activation_payload_json,
                                                           '$.post_event_reconciliation.review.tickers'
                                                       ) AS holding
                                                       WHERE UPPER(json_extract_string(
                                                           holding.value, '$'
                                                       )) = UPPER(json_extract_string(
                                                           addition.value, '$.ticker'
                                                       ))
                                                   )
                                             )
                                             AND NOT EXISTS (
                                                 SELECT 1
                                                 FROM json_each(
                                                     activation.activation_payload_json,
                                                     '$.change_rows'
                                                 ) AS deletion
                                                 WHERE json_extract_string(
                                                     deletion.value, '$.action'
                                                 ) = 'deletion'
                                                   AND EXISTS (
                                                       SELECT 1
                                                       FROM json_each(
                                                           activation.activation_payload_json,
                                                           '$.post_event_reconciliation.review.tickers'
                                                       ) AS holding
                                                       WHERE UPPER(json_extract_string(
                                                           holding.value, '$'
                                                       )) = UPPER(json_extract_string(
                                                           deletion.value, '$.ticker'
                                                       ))
                                                   )
                                             )
                                         )
                                     )
                                     AND json_extract_string(
                                         mismatch_detail_json,
                                         '$.accepted_activation_component_lag.effective_date'
                                     ) = CAST(activation.effective_date AS VARCHAR)
                                     AND CAST(json_extract(
                                         mismatch_detail_json,
                                         '$.accepted_activation_component_lag.lag_days'
                                     ) AS INTEGER) = requested_coverage_through -
                                         activation.effective_date
                                     AND (
                                         SELECT list_sort(list(json_extract_string(
                                             value, '$'
                                         )))
                                         FROM json_each(
                                             mismatch_detail_json,
                                             '$.missing_from_component_snapshot'
                                         )
                                     ) = (
                                         SELECT list_sort(list(json_extract_string(
                                             value, '$.ticker'
                                         )))
                                         FROM json_each(
                                             activation.activation_payload_json,
                                             '$.change_rows'
                                         )
                                         WHERE json_extract_string(
                                             value, '$.action'
                                         ) = 'addition'
                                     )
                                     AND (
                                         SELECT list_sort(list(json_extract_string(
                                             value, '$'
                                         )))
                                         FROM json_each(
                                             mismatch_detail_json,
                                             '$.unexpected_in_component_snapshot'
                                         )
                                     ) = (
                                         SELECT list_sort(list(json_extract_string(
                                             value, '$.ticker'
                                         )))
                                         FROM json_each(
                                             activation.activation_payload_json,
                                             '$.change_rows'
                                         )
                                         WHERE json_extract_string(
                                             value, '$.action'
                                         ) = 'deletion'
                                     )
                               )
                           )
                           OR membership_rows_extended <> reviewed_member_count
                           OR security_rows_extended <> reviewed_member_count
                           OR owner_rows_extended <> reviewed_member_count
                           OR provider_rows_extended < reviewed_member_count
                       )
                   )
                   OR (
                       status = 'blocked_review_required'
                       AND (
                           membership_rows_extended <> 0
                           OR security_rows_extended <> 0
                           OR owner_rows_extended <> 0
                           OR cik_rows_extended <> 0
                           OR provider_rows_extended <> 0
                       )
                   )
                """
            )[0]["n"],
            "fail",
            "Universe no-change attestations must retain consistent clocks, hashes, and counts.",
        )
        add(
            "universe_attestation_missing_raw_evidence",
            self.query(
                """
                WITH evidence AS (
                    SELECT attestation.attestation_id,
                           COUNT(*) FILTER (
                               WHERE linked.role LIKE 'official_release_archive_page_%'
                                 AND snapshot.artifact_kind = 'exact_response'
                                 AND snapshot.http_status BETWEEN 200 AND 299
                           ) AS official_pages,
                           COUNT(*) FILTER (
                               WHERE linked.role = 'independent_component_snapshot'
                                 AND snapshot.artifact_kind = 'exact_response'
                                 AND snapshot.http_status BETWEEN 200 AND 299
                           ) AS component_pages
                    FROM universe_coverage_attestations AS attestation
                    LEFT JOIN ingest_raw_snapshots AS linked
                      ON linked.run_id = attestation.run_id
                    LEFT JOIN raw_snapshots AS snapshot USING (snapshot_id)
                    GROUP BY attestation.attestation_id
                )
                SELECT COUNT(*) AS n
                FROM evidence
                WHERE official_pages < 1 OR component_pages <> 1
                """
            )[0]["n"],
            "fail",
            "Every universe attestation needs exact official and independent raw responses.",
        )
        add(
            "universe_attestation_orphan_markers",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM universe_membership AS membership
                LEFT JOIN universe_coverage_attestations AS attestation
                  ON attestation.attestation_id = regexp_extract(
                      membership.source,
                      'coverage-attestation:([^|]+)',
                      1
                  )
                WHERE membership.source LIKE '%|coverage-attestation:%'
                  AND attestation.attestation_id IS NULL
                """
            )[0]["n"],
            "fail",
            "Every membership coverage marker must resolve to an immutable attestation.",
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
            "security_conversion_invalid_rows",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM security_conversions
                WHERE source_security_id = target_security_id
                   OR known_date > effective_date
                   OR share_ratio <= 0
                   OR NOT isfinite(share_ratio)
                   OR basis_policy <> 'carryover'
                   OR review_status <> 'verified'
                   OR verified_date > CURRENT_DATE
                   OR NOT starts_with(source, 'https://')
                   OR NOT starts_with(basis_source, 'https://')
                """
            )[0]["n"],
            "fail",
            "Security conversions require reviewed dates, ratios, carry-over "
            "basis, and HTTPS evidence.",
        )
        add(
            "security_conversion_orphans",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM security_conversions AS conversion
                LEFT JOIN security_master AS source_security
                  ON source_security.security_id = conversion.source_security_id
                LEFT JOIN security_master AS target_security
                  ON target_security.security_id = conversion.target_security_id
                WHERE source_security.security_id IS NULL
                   OR target_security.security_id IS NULL
                """
            )[0]["n"],
            "fail",
            "Every reviewed conversion endpoint must exist in the security master.",
        )
        add(
            "security_conversion_missing_dated_tickers",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM security_conversions AS conversion
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM security_identity_assignments AS identity
                    WHERE identity.security_id = conversion.source_security_id
                      AND identity.effective_start <= conversion.effective_date
                      AND (
                          identity.effective_end IS NULL
                          OR identity.effective_end > conversion.effective_date
                      )
                ) OR NOT EXISTS (
                    SELECT 1
                    FROM security_identity_assignments AS identity
                    WHERE identity.security_id = conversion.target_security_id
                      AND identity.known_date <= conversion.effective_date
                      AND identity.effective_start <= conversion.effective_date
                      AND (
                          identity.effective_end IS NULL
                          OR identity.effective_end > conversion.effective_date
                      )
                )
                """
            )[0]["n"],
            "fail",
            "Each conversion needs reviewed source and target market labels on its effective date.",
        )
        add(
            "security_ticker_extension_invalid_rows",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM security_ticker_extensions
                WHERE data_end <= data_start
                   OR data_end > data_start + INTERVAL 45 DAY
                   OR verified_date > CURRENT_DATE
                   OR purpose <> 'portfolio_liquidation'
                   OR review_policy <> 'adjacent_identity_provider_v1'
                   OR NOT regexp_matches(payload_sha256, '^[0-9a-f]{64}$')
                   OR NOT starts_with(identity_source, 'https://')
                   OR NOT starts_with(provider_source, 'https://')
                """
            )[0]["n"],
            "fail",
            "Liquidation ticker extensions must be short, reviewed, hashed, and source-backed.",
        )
        add(
            "security_ticker_extension_broken_anchors",
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM security_ticker_extensions AS extension
                LEFT JOIN security_master AS security USING (security_id)
                WHERE security.security_id IS NULL
                   OR NOT EXISTS (
                       SELECT 1 FROM security_identity_assignments AS identity
                       WHERE identity.universe_id = extension.universe_id
                         AND identity.security_id = extension.security_id
                         AND identity.ticker = extension.ticker
                         AND identity.effective_end = extension.data_start
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM provider_symbol_history AS mapping
                       WHERE mapping.security_id = extension.security_id
                         AND mapping.provider = extension.provider
                         AND mapping.provider_symbol = extension.provider_symbol
                         AND mapping.mapping_status = 'verified'
                         AND mapping.data_end = extension.data_start
                   )
                """
            )[0]["n"],
            "fail",
            "Each liquidation path must touch exact prior identity and provider anchors.",
        )
        add(
            "security_ticker_extension_payload_mismatch",
            self._liquidation_payload_mismatch_count(),
            "fail",
            "Every liquidation extension must exactly retain its hashed reviewed price payload.",
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
                        AND mapping.provider = CASE
                            WHEN price.source = 'yfinance:ohlc-envelope-v1'
                            THEN 'yfinance'
                            ELSE price.source
                        END
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
                  AND NOT EXISTS (
                      SELECT 1 FROM security_ticker_extensions AS extension
                      WHERE extension.security_id = price.security_id
                        AND extension.ticker = price.ticker
                        AND extension.provider = CASE
                            WHEN price.source = 'yfinance:ohlc-envelope-v1'
                            THEN 'yfinance'
                            ELSE price.source
                        END
                        AND extension.provider_symbol = price.provider_symbol
                        AND extension.data_start <= price.date
                        AND extension.data_end > price.date
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

    def _liquidation_payload_mismatch_count(self) -> int:
        """Count reviewed liquidation paths whose stored prices changed or vanished."""
        mismatches = 0
        extensions = self.query(
            """
            SELECT provenance_id, security_id, ticker, provider, provider_symbol,
                   data_start, data_end, payload_sha256
            FROM security_ticker_extensions
            ORDER BY provenance_id
            """
        )
        for extension in extensions:
            prices = self.query(
                """
                SELECT ticker, security_id, provider_symbol, date, open, high, low,
                       close, adj_close, volume, dividends, split_ratio,
                       actions_complete, close_split_adjusted,
                       split_normalization_factor, split_normalization_through,
                       source
                FROM prices
                WHERE security_id = ?
                  AND ticker = ?
                  AND (
                      source = ?
                      OR (? = 'yfinance' AND source = 'yfinance:ohlc-envelope-v1')
                  )
                  AND provider_symbol = ?
                  AND date >= CAST(? AS DATE)
                  AND date < CAST(? AS DATE)
                ORDER BY date
                """,
                (
                    extension["security_id"],
                    extension["ticker"],
                    extension["provider"],
                    extension["provider"],
                    extension["provider_symbol"],
                    str(extension["data_start"]),
                    str(extension["data_end"]),
                ),
            )
            payload = [{**price, "provenance_id": extension["provenance_id"]} for price in prices]
            try:
                actual_hash = canonical_price_payload_hash(payload)
            except (TypeError, ValueError):
                mismatches += 1
                continue
            if actual_hash != extension["payload_sha256"]:
                mismatches += 1
        return mismatches

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
        if not count:
            return 0
        self.execute("BEGIN TRANSACTION")
        try:
            self.execute(
                f"""
                INSERT INTO fundamental_versions
                (ticker, issuer_id, security_id, ingest_run_id,
                 source_snapshot_id, source_rowset_sha256,
                 source_row_sha256, source_fact_locator, period_end,
                 as_of_date, fiscal_period, statement, metric, value,
                 quarter_value, unit, source, recorded_at, is_deleted)
                SELECT ticker, issuer_id, security_id, ingest_run_id,
                       source_snapshot_id, source_rowset_sha256,
                       source_row_sha256, source_fact_locator, period_end,
                       as_of_date, fiscal_period, statement, metric, value,
                       quarter_value, unit, source, now(), TRUE
                FROM fundamentals
                WHERE {where}
                """,
                params,
            )
            self.execute(f"DELETE FROM fundamentals WHERE {where}", params)
            self.execute("COMMIT")
        except Exception:
            self.execute("ROLLBACK")
            raise
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
                (ticker, issuer_id, security_id, ingest_run_id,
                 source_snapshot_id, source_rowset_sha256, source_row_sha256,
                 source_fact_locator, period_end, as_of_date, fiscal_period,
                 statement, metric, value, quarter_value, unit, source, fetched_at,
                 quarantine_reason, quarantined_at)
                SELECT ticker, issuer_id, security_id, ingest_run_id,
                       source_snapshot_id, source_rowset_sha256,
                       source_row_sha256, source_fact_locator, period_end,
                       as_of_date, fiscal_period, statement, metric, value,
                       quarter_value, unit, source, fetched_at,
                       'period_end_after_as_of_date', now()
                FROM fundamentals
                WHERE period_end > as_of_date
                """
            )
            self.execute(
                """
                INSERT INTO fundamental_versions
                (ticker, issuer_id, security_id, ingest_run_id,
                 source_snapshot_id, source_rowset_sha256,
                 source_row_sha256, source_fact_locator, period_end,
                 as_of_date, fiscal_period, statement, metric, value,
                 quarter_value, unit, source, recorded_at, is_deleted)
                SELECT ticker, issuer_id, security_id, ingest_run_id,
                       source_snapshot_id, source_rowset_sha256,
                       source_row_sha256, source_fact_locator, period_end,
                       as_of_date, fiscal_period, statement, metric, value,
                       quarter_value, unit, source, now(), TRUE
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
    def create_fundamental_evidence_generation(
        self,
        *,
        purpose: str,
        decision_date: date | str | None = None,
        now: datetime | None = None,
    ) -> FundamentalEvidenceGeneration:
        """Capture one immutable named boundary over append-only fact versions."""

        normalized_purpose = str(purpose).strip()
        if not normalized_purpose or len(normalized_purpose) > 80:
            raise ValueError("fundamental evidence purpose must contain 1-80 characters")
        if any(
            not (character.isalnum() or character in "-_.:") for character in normalized_purpose
        ):
            raise ValueError(
                "fundamental evidence purpose may contain only letters, numbers, -, _, ., and :"
            )
        normalized_date = _as_date(decision_date) if decision_date is not None else None
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("fundamental evidence capture time must be timezone-aware")
        captured_at = moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
        generation_id = f"fundamental-generation-{uuid4().hex}"
        self.execute("BEGIN TRANSACTION")
        try:
            self.execute(
                """
                INSERT INTO fundamental_evidence_generations
                (generation_id, version_sequence, purpose, decision_date, captured_at)
                SELECT ?, COALESCE(MAX(version_sequence), 0), ?, CAST(? AS DATE),
                       CAST(? AS TIMESTAMP)
                FROM fundamental_versions
                """,
                (
                    generation_id,
                    normalized_purpose,
                    normalized_date.isoformat() if normalized_date is not None else None,
                    captured_at,
                ),
            )
            self.execute("COMMIT")
        except Exception:
            self.execute("ROLLBACK")
            raise
        return self.fundamental_evidence_generation(generation_id)

    def fundamental_evidence_generation(
        self,
        generation_id: str,
    ) -> FundamentalEvidenceGeneration:
        normalized = str(generation_id).strip()
        if not normalized:
            raise ValueError("fundamental evidence generation id is required")
        rows = self.query(
            """
            SELECT generation_id, version_sequence, purpose, decision_date,
                   captured_at
            FROM fundamental_evidence_generations
            WHERE generation_id = ?
            """,
            (normalized,),
        )
        if len(rows) != 1:
            raise ValueError(f"unknown fundamental evidence generation: {generation_id}")
        row = rows[0]
        return FundamentalEvidenceGeneration(
            generation_id=str(row["generation_id"]),
            version_sequence=int(row["version_sequence"]),
            purpose=str(row["purpose"]),
            decision_date=(str(row["decision_date"]) if row["decision_date"] is not None else None),
            captured_at=_utc_database_timestamp(row["captured_at"]),
        )

    def _fundamental_evidence_relation(
        self,
        generation_id: str | None,
    ) -> tuple[str, str, tuple[Any, ...]]:
        if generation_id is None:
            return "WITH", "fundamentals", ()
        generation = self.fundamental_evidence_generation(generation_id)
        prefix = """
            WITH generation_versions AS (
                SELECT version.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY ticker, period_end, as_of_date, metric
                           ORDER BY version_sequence DESC
                       ) AS projection_rank
                FROM fundamental_versions AS version
                WHERE version.version_sequence <= ?
            ), fundamental_evidence AS (
                SELECT * EXCLUDE (projection_rank)
                FROM generation_versions
                WHERE projection_rank = 1 AND is_deleted = FALSE
            ),
        """
        return prefix, "fundamental_evidence", (generation.version_sequence,)

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
        """Resolve the reporting issuer that owns a security on one date.

        Gated on ``effective_start``/``effective_end`` — the assignment's
        factual validity window — not on when a reviewer happened to confirm
        it. ``verified_date`` records operator due-diligence timing, an
        artifact of when this codebase's own review backlog was worked
        through; it is not a public-knowability fact like fundamentals'
        ``as_of_date`` or membership's ``known_date``, and gating on it made
        an already-reviewed, factually correct historical assignment
        unusable for any decision date before its own review happened to
        occur — including for securities with no history of ever being
        wrong.
        """
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

    def has_later_verified_issuer_assignment(
        self,
        security_id: str,
        as_of: date | str,
    ) -> bool:
        """Return whether an effective assignment was reviewed only after ``as_of``.

        This deliberately exposes only a boolean. It lets current refreshes
        distinguish pending reviewed evidence from a missing identity without
        leaking the later-reviewed issuer into an earlier decision snapshot.
        """
        rows = self.query(
            """
            SELECT DISTINCT issuer_id
            FROM security_issuer_assignments
            WHERE security_id = ?
              AND verified_date > CAST(? AS DATE)
              AND effective_start <= CAST(? AS DATE)
              AND (effective_end IS NULL OR effective_end > CAST(? AS DATE))
            """,
            (security_id, str(as_of), str(as_of), str(as_of)),
        )
        if len(rows) > 1:
            raise ValueError(f"ambiguous later-verified issuer identity for {security_id}@{as_of}")
        return bool(rows)

    def issuer_id_for_ticker(self, ticker: str, as_of: date | str) -> str | None:
        security_id = self.security_id_for_ticker(ticker, as_of)
        if security_id is None:
            return None
        return self.issuer_id_for_security(security_id, as_of)

    def issuer_has_fundamentals(self, issuer_id: str) -> bool:
        """Return whether an issuer has ever produced accepted Company Facts.

        Current reviewed issuers can exist before their first XBRL facts are
        published. Refresh orchestration uses this distinction to keep a
        pre-filing issuer visible as pending without treating an established
        issuer's unexpectedly empty response as harmless.
        """
        rows = self.query(
            """
            SELECT 1
            FROM fundamentals
            WHERE issuer_id = ?
            LIMIT 1
            """,
            (issuer_id,),
        )
        return bool(rows)

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
            historical = self.query(
                """
                SELECT DISTINCT security_id
                FROM security_identity_assignments
                WHERE ticker = ?
                  AND known_date <= CAST(? AS DATE)
                  AND effective_start <= CAST(? AS DATE)
                """,
                (normalized_ticker, str(as_of), str(as_of)),
            )
            if len(historical) > 1:
                raise ValueError(f"ambiguous security identity for {normalized_ticker}@{as_of}")
            security_id = historical[0]["security_id"] if historical else None
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

    def _factor_identity_routes(
        self,
        tickers: list[str],
        as_of: date | str,
    ) -> dict[str, dict[str, Any]]:
        """Resolve factor identity/provenance state for a universe in one read.

        The returned state is intentionally equivalent to the scalar identity
        helpers used by ``pit_factor_fundamentals``, ``latest_price``, and
        ``pit_factor_price_history``. Ambiguous dated identities still raise so
        callers can fall back to the scalar compatibility path without silently
        choosing one security or issuer.
        """
        normalized = sorted({ticker.upper() for ticker in tickers})
        if not normalized:
            return {}

        relation = f"_tmp_factor_tickers_{uuid4().hex}"
        self._con.register(
            relation,
            _rows_to_arrowable([{"requested_ticker": ticker} for ticker in normalized]),
        )
        try:
            rows = self.query(
                f"""
                WITH active_security_routes AS (
                    SELECT requested.requested_ticker,
                           COUNT(DISTINCT identity.security_id) AS security_count,
                           MIN(identity.security_id) AS security_id
                    FROM {relation} AS requested
                    LEFT JOIN security_identity_assignments AS identity
                      ON identity.ticker = requested.requested_ticker
                     AND identity.known_date <= CAST(? AS DATE)
                     AND identity.effective_start <= CAST(? AS DATE)
                     AND (
                            identity.effective_end IS NULL
                            OR identity.effective_end > CAST(? AS DATE)
                     )
                    GROUP BY requested.requested_ticker
                ), historical_security_routes AS (
                    SELECT requested.requested_ticker,
                           COUNT(DISTINCT identity.security_id) AS security_count,
                           MIN(identity.security_id) AS security_id
                    FROM {relation} AS requested
                    LEFT JOIN security_identity_assignments AS identity
                      ON identity.ticker = requested.requested_ticker
                     AND identity.known_date <= CAST(? AS DATE)
                     AND identity.effective_start <= CAST(? AS DATE)
                    GROUP BY requested.requested_ticker
                ), security_routes AS (
                    SELECT active.requested_ticker,
                           active.security_count,
                           active.security_id,
                           CASE
                               WHEN active.security_count > 0
                               THEN active.security_count
                               ELSE historical.security_count
                           END AS fundamental_security_count,
                           CASE
                               WHEN active.security_count > 0
                               THEN active.security_id
                               ELSE historical.security_id
                           END AS fundamental_security_id
                    FROM active_security_routes AS active
                    JOIN historical_security_routes AS historical
                      USING (requested_ticker)
                ), active_owners AS (
                    SELECT security_id,
                           COUNT(DISTINCT issuer_id) AS issuer_count,
                           MIN(issuer_id) AS issuer_id
                    FROM security_issuer_assignments
                    WHERE effective_start <= CAST(? AS DATE)
                      AND (effective_end IS NULL OR effective_end > CAST(? AS DATE))
                    GROUP BY security_id
                ), owner_history AS (
                    SELECT DISTINCT security_id
                    FROM security_issuer_assignments
                ), mapping_history AS (
                    SELECT DISTINCT security_id
                    FROM provider_symbol_history
                ), active_mappings AS (
                    SELECT DISTINCT security_id
                    FROM provider_symbol_history
                    WHERE mapping_status = 'verified'
                      AND data_start <= CAST(? AS DATE)
                      AND (data_end IS NULL OR data_end > CAST(? AS DATE))
                )
                SELECT route.requested_ticker,
                       route.security_count,
                       route.security_id,
                       route.fundamental_security_count,
                       route.fundamental_security_id,
                       COALESCE(owner.issuer_count, 0) AS issuer_count,
                       owner.issuer_id,
                       history.security_id IS NOT NULL AS has_reviewed_owner,
                       mapping.security_id IS NOT NULL AS has_reviewed_mapping,
                       active.security_id IS NOT NULL AS has_active_mapping
                FROM security_routes AS route
                LEFT JOIN active_owners AS owner
                  ON owner.security_id = route.fundamental_security_id
                LEFT JOIN owner_history AS history
                  ON history.security_id = route.fundamental_security_id
                LEFT JOIN mapping_history AS mapping
                  ON mapping.security_id = route.security_id
                LEFT JOIN active_mappings AS active
                  ON active.security_id = route.security_id
                ORDER BY route.requested_ticker
                """,
                (str(as_of),) * 9,
            )
        finally:
            self._con.unregister(relation)

        resolved: dict[str, dict[str, Any]] = {}
        for row in rows:
            ticker = str(row["requested_ticker"])
            if int(row["security_count"]) > 1:
                raise ValueError(f"ambiguous security identity for {ticker}@{as_of}")
            resolved[ticker] = row
        return resolved

    def issuer_reference(
        self,
        issuer_id: str,
        *,
        as_of: date | str | None = None,
    ) -> dict | None:
        """Return canonical issuer metadata and one reviewed SEC CIK.

        Supplying ``as_of`` resolves the CIK whose reviewed successor-lineage
        window covers that date (``effective_start``/``effective_end`` —
        never naive current-CIK equality). Omitting it preserves the
        current-ingest behavior of selecting the latest reviewed interval.
        Gating is on that factual window only, not on ``verified_date``: the
        review timestamp records when this codebase's own backlog reached the
        row, not when the CIK lineage was actually true, and gating a
        historical lookup on it made an already-reviewed, correct interval
        unusable before its own review happened to occur.
        """
        where = "WHERE issuer.issuer_id = ?"
        parameters: list[Any] = [issuer_id]
        limit = "LIMIT 1"
        if as_of is not None:
            where += """
              AND cik.effective_start <= CAST(? AS DATE)
              AND (cik.effective_end IS NULL OR cik.effective_end > CAST(? AS DATE))
            """
            parameters.extend((str(as_of), str(as_of)))
            limit = ""
        rows = self.query(
            f"""
            SELECT issuer.issuer_id, issuer.canonical_name,
                   issuer.canonical_ticker, cik.cik,
                   cik.effective_start, cik.effective_end,
                   cik.verified_date, cik.source AS cik_source,
                   issuer.source AS issuer_source
            FROM issuer_master AS issuer
            JOIN issuer_cik_history AS cik USING (issuer_id)
            {where}
            ORDER BY cik.effective_start DESC
            {limit}
            """,
            tuple(parameters),
        )
        if as_of is not None and len(rows) > 1:
            raise ValueError(f"ambiguous SEC CIK for {issuer_id}@{as_of}")
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
            FROM (
                SELECT ticker, security_id, effective_start, effective_end
                FROM security_identity_assignments
                UNION ALL
                SELECT ticker, security_id, data_start AS effective_start,
                       data_end AS effective_end
                FROM security_ticker_extensions
            ) AS ticker_history
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

    def ticker_for_security_id(
        self,
        security_id: str,
        as_of: date | str,
    ) -> str | None:
        """Resolve the reviewed dated market label for an immutable security."""
        rows = self.query(
            """
            SELECT DISTINCT ticker
            FROM (
                SELECT ticker, security_id, effective_start, effective_end,
                       known_date
                FROM security_identity_assignments
                UNION ALL
                SELECT ticker, security_id, data_start AS effective_start,
                       data_end AS effective_end, data_start AS known_date
                FROM security_ticker_extensions
            ) AS ticker_history
            WHERE security_id = ?
              AND known_date <= CAST(? AS DATE)
              AND effective_start <= CAST(? AS DATE)
              AND (effective_end IS NULL OR effective_end > CAST(? AS DATE))
            """,
            (security_id, str(as_of), str(as_of), str(as_of)),
        )
        if len(rows) > 1:
            raise ValueError(f"ambiguous ticker for {security_id}@{as_of}")
        return rows[0]["ticker"] if rows else None

    def security_conversions_between(
        self,
        source_security_ids: list[str] | tuple[str, ...] | set[str],
        start: date | str,
        end: date | str,
    ) -> list[dict]:
        """Return reviewed identity-changing share events in ``(start, end]``."""
        normalized = sorted(
            {str(value).strip() for value in source_security_ids if str(value).strip()}
        )
        if not normalized:
            return []
        placeholders = ",".join("?" for _ in normalized)
        return self.query(
            f"""
            SELECT source_security_id, target_security_id, effective_date,
                   known_date, share_ratio, basis_policy, review_status,
                   verified_date, source, basis_source
            FROM security_conversions
            WHERE source_security_id IN ({placeholders})
              AND effective_date > CAST(? AS DATE)
              AND effective_date <= CAST(? AS DATE)
              AND known_date <= effective_date
              AND review_status = 'verified'
            ORDER BY effective_date, source_security_id
            """,
            (*normalized, str(start), str(end)),
        )

    def pit_fundamentals(
        self,
        ticker: str,
        as_of: date | str,
        metrics: list[str] | None = None,
        *,
        evidence_generation_id: str | None = None,
    ) -> list[dict]:
        """Get the latest fundamentals known as of `as_of` for a ticker.

        This is THE point-in-time read. It returns the most recent filing for
        each metric whose as_of_date <= as_of. No look-ahead possible.
        """
        identity = self._fundamental_identity_filter(ticker, as_of)
        if identity is None:
            return []
        identity_filter, identity_value = identity
        generation_prefix, relation, generation_params = self._fundamental_evidence_relation(
            evidence_generation_id
        )
        sql = f"""
            {generation_prefix} ranked AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY metric
                           ORDER BY as_of_date DESC, period_end DESC
                       ) AS rn
                FROM {relation}
                WHERE {identity_filter}
                  AND as_of_date <= CAST(? AS DATE)
                  AND period_end <= as_of_date
            )
            SELECT ticker, issuer_id, security_id, period_end, as_of_date, fiscal_period,
                   statement, metric, value, quarter_value, unit, source
            FROM ranked
            WHERE rn = 1
        """
        params: tuple[Any, ...] = (*generation_params, identity_value, str(as_of))
        if metrics:
            placeholders = ",".join("?" for _ in metrics)
            sql += f" AND metric IN ({placeholders})"
            params = (*generation_params, identity_value, str(as_of), *metrics)
        return self.query(sql, params)

    def fundamental_history(
        self,
        ticker: str,
        as_of: date | str,
        metric: str,
        *,
        evidence_generation_id: str | None = None,
    ) -> list[dict]:
        """Return PIT-deduped metric history using issuer identity when verified."""
        identity = self._fundamental_identity_filter(ticker, as_of)
        if identity is None:
            return []
        identity_filter, identity_value = identity
        generation_prefix, relation, generation_params = self._fundamental_evidence_relation(
            evidence_generation_id
        )
        return self.query(
            f"""
            {generation_prefix} ranked AS (
                SELECT period_end, fiscal_period, quarter_value,
                       ROW_NUMBER() OVER (
                           PARTITION BY period_end
                           ORDER BY as_of_date DESC
                       ) AS rn
                FROM {relation}
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
            (*generation_params, identity_value, metric, str(as_of)),
        )

    def pit_factor_fundamentals(
        self,
        ticker: str,
        as_of: date | str,
        metrics: list[str],
        *,
        evidence_generation_id: str | None = None,
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
        generation_prefix, relation, generation_params = self._fundamental_evidence_relation(
            evidence_generation_id
        )
        placeholders = ",".join("?" for _ in normalized_metrics)
        return self.query(
            f"""
            {generation_prefix} ranked AS (
                SELECT metric, period_end, as_of_date, fiscal_period,
                       value, quarter_value,
                       ROW_NUMBER() OVER (
                           PARTITION BY metric, period_end
                           ORDER BY as_of_date DESC
                       ) AS period_rn
                FROM {relation}
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
            (*generation_params, identity_value, *normalized_metrics, str(as_of)),
        )

    def pit_factor_fundamentals_batch(
        self,
        tickers: list[str],
        as_of: date | str,
        metrics: list[str],
        *,
        evidence_generation_id: str | None = None,
    ) -> dict[str, list[dict]]:
        """Return PIT-deduped factor histories for a universe in one data read.

        Results use the same dated security-to-issuer routing, reviewed-owner
        gap, restatement, and legacy-ticker policy as
        :meth:`pit_factor_fundamentals`. No result is persisted beyond the
        caller's decision scope.
        """
        routes = self._factor_identity_routes(tickers, as_of)
        result = {ticker: [] for ticker in routes}
        normalized_metrics = sorted({metric.strip() for metric in metrics if metric.strip()})
        if not routes or not normalized_metrics:
            return result

        readable: list[dict] = []
        for ticker, route in routes.items():
            if int(route["fundamental_security_count"]) > 1:
                raise ValueError(f"ambiguous security identity for {ticker}@{as_of}")
            if int(route["issuer_count"]) > 1:
                raise ValueError(
                    f"ambiguous issuer identity for {route['fundamental_security_id']}@{as_of}"
                )
            issuer_id = route.get("issuer_id")
            if issuer_id is not None:
                readable.append(
                    {
                        "requested_ticker": ticker,
                        "identity_kind": "issuer",
                        "identity_value": issuer_id,
                    }
                )
            elif route.get("fundamental_security_id") is not None and route.get(
                "has_reviewed_owner"
            ):
                # Once reviewed ownership exists, a date without an active
                # owner is an explicit evidence gap and cannot use ticker rows.
                continue
            else:
                readable.append(
                    {
                        "requested_ticker": ticker,
                        "identity_kind": "ticker",
                        "identity_value": ticker,
                    }
                )
        if not readable:
            return result

        relation = f"_tmp_factor_fundamental_routes_{uuid4().hex}"
        self._con.register(relation, _rows_to_arrowable(readable))
        placeholders = ",".join("?" for _ in normalized_metrics)
        generation_prefix, fundamental_relation, generation_params = (
            self._fundamental_evidence_relation(evidence_generation_id)
        )
        try:
            rows = self.query(
                f"""
                {generation_prefix} selected AS (
                    SELECT route.requested_ticker,
                           fundamental.metric,
                           fundamental.period_end,
                           fundamental.as_of_date,
                           fundamental.fiscal_period,
                           fundamental.value,
                           fundamental.quarter_value
                    FROM {relation} AS route
                    JOIN {fundamental_relation} AS fundamental
                      ON route.identity_kind = 'issuer'
                     AND fundamental.issuer_id = route.identity_value
                    UNION ALL
                    SELECT route.requested_ticker,
                           fundamental.metric,
                           fundamental.period_end,
                           fundamental.as_of_date,
                           fundamental.fiscal_period,
                           fundamental.value,
                           fundamental.quarter_value
                    FROM {relation} AS route
                    JOIN {fundamental_relation} AS fundamental
                      ON route.identity_kind = 'ticker'
                     AND fundamental.ticker = route.identity_value
                ), ranked AS (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY requested_ticker, metric, period_end
                               ORDER BY as_of_date DESC
                           ) AS period_rn
                    FROM selected
                    WHERE metric IN ({placeholders})
                      AND as_of_date <= CAST(? AS DATE)
                      AND period_end <= as_of_date
                )
                SELECT requested_ticker, metric, period_end, as_of_date,
                       fiscal_period, value, quarter_value
                FROM ranked
                WHERE period_rn = 1
                ORDER BY requested_ticker, metric, period_end
                """,
                (*generation_params, *normalized_metrics, str(as_of)),
            )
        finally:
            self._con.unregister(relation)

        for row in rows:
            ticker = str(row.pop("requested_ticker"))
            result[ticker].append(row)
        return result

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
                           ORDER BY release_date DESC,
                                    CASE source
                                        WHEN 'fred' THEN 0
                                        WHEN 'treasury' THEN 1
                                        ELSE 2
                                    END,
                                    fetched_at DESC
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
                           ORDER BY release_date DESC,
                                    CASE source
                                        WHEN 'fred' THEN 0
                                        WHEN 'treasury' THEN 1
                                        ELSE 2
                                    END,
                                    fetched_at DESC
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

        "Active" means a currently-verified mapping whose ``data_start``/
        ``data_end`` window factually covers ``as_of`` — not one whose
        ``verified_date`` also happens to predate ``as_of``. The review
        timestamp records operator due-diligence timing, an artifact of this
        codebase's own backlog; requiring it to precede a historical decision
        date made an already-reviewed, factually correct mapping unusable
        for any date before its own review happened to occur, which made
        every historical backtest silently drop securities for reasons
        unrelated to real data availability.
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

    def pit_factor_latest_prices_batch(
        self,
        tickers: list[str],
        as_of: date | str,
    ) -> dict[str, dict | None]:
        """Return the factor-compatible latest price for every requested ticker.

        Provider mapping gaps fail closed exactly as in :meth:`latest_price`.
        The selected columns are the complete price/action evidence consumed by
        the factor layer; this method is not a general replacement for the
        scalar storage API.
        """
        routes = self._factor_identity_routes(tickers, as_of)
        result: dict[str, dict | None] = {ticker: None for ticker in routes}
        readable: list[dict] = []
        for ticker, route in routes.items():
            if route.get("has_reviewed_mapping"):
                if not route.get("has_active_mapping"):
                    continue
                readable.append(
                    {
                        "requested_ticker": ticker,
                        "identity_kind": "security",
                        "identity_value": route["security_id"],
                    }
                )
            else:
                readable.append(
                    {
                        "requested_ticker": ticker,
                        "identity_kind": "ticker",
                        "identity_value": ticker,
                    }
                )
        if not readable:
            return result

        relation = f"_tmp_factor_latest_price_routes_{uuid4().hex}"
        self._con.register(relation, _rows_to_arrowable(readable))
        try:
            rows = self.query(
                f"""
                WITH candidates AS (
                    SELECT route.requested_ticker,
                           price.ticker,
                           price.security_id,
                           price.date,
                           price.close,
                           price.dividends,
                           price.split_ratio,
                           price.actions_complete,
                           price.close_split_adjusted,
                           price.split_normalization_factor,
                           price.split_normalization_through,
                           price.source,
                           ROW_NUMBER() OVER (
                               PARTITION BY route.requested_ticker
                               ORDER BY price.date DESC
                           ) AS price_rn
                    FROM {relation} AS route
                    JOIN prices AS price
                      ON (
                            route.identity_kind = 'security'
                            AND price.security_id = route.identity_value
                      )
                      OR (
                            route.identity_kind = 'ticker'
                            AND price.ticker = route.identity_value
                      )
                    WHERE price.date <= CAST(? AS DATE)
                )
                SELECT requested_ticker, ticker, security_id, date, close,
                       dividends, split_ratio, actions_complete,
                       close_split_adjusted, split_normalization_factor,
                       split_normalization_through, source
                FROM candidates
                WHERE price_rn = 1
                ORDER BY requested_ticker
                """,
                (str(as_of),),
            )
        finally:
            self._con.unregister(relation)

        for row in rows:
            ticker = str(row.pop("requested_ticker"))
            result[ticker] = row
        return result

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

    def pit_factor_price_history(
        self,
        ticker: str,
        as_of: date | str,
        *,
        observations: int,
    ) -> list[dict]:
        """Return an identity-safe raw-price/action window for market factors.

        The provider policy matches :meth:`latest_price`: once a security has
        reviewed provider history, a date without an active verified mapping
        fails closed. Active reviewed securities read by stable ``security_id``
        so a ticker change cannot break Momentum or Low Volatility. Legacy
        unreviewed securities retain the ticker path. Rows are returned oldest
        first and duplicate security/date observations are collapsed to the
        newest stored copy.

        "Active" is gated on the mapping's ``data_start``/``data_end``
        window covering ``as_of``, not on its ``verified_date`` also
        predating ``as_of`` — see :meth:`latest_price` for why: the review
        timestamp is operator due-diligence timing, not a public-knowability
        fact, and gating on it made Momentum and Low Volatility structurally
        uncomputable for essentially every historical backtest date.
        """
        if observations < 2:
            raise ValueError("factor price history requires at least two observations")
        normalized_ticker = ticker.upper()
        security_id = self.security_id_for_ticker(normalized_ticker, as_of)
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
            return []

        if has_active_mapping:
            rows = self.query(
                """
                WITH combined AS (
                    SELECT ticker, security_id, date, close, dividends, split_ratio,
                           actions_complete, close_split_adjusted,
                           split_normalization_factor, split_normalization_through,
                           source, fetched_at, 2 AS source_priority
                    FROM prices
                    WHERE security_id = ?
                      AND date <= CAST(? AS DATE)
                    UNION ALL
                    SELECT CAST(? AS VARCHAR) AS ticker, security_id, date, close,
                           dividends, split_ratio, actions_complete,
                           close_split_adjusted, split_normalization_factor,
                           split_normalization_through, provider AS source,
                           fetched_at, 1 AS source_priority
                    FROM factor_prices
                    WHERE security_id = ?
                      AND date <= CAST(? AS DATE)
                ), deduped AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY date
                        ORDER BY source_priority DESC, fetched_at DESC
                    ) AS date_rn
                    FROM combined
                ), recent AS (
                    SELECT ticker, security_id, date, close, dividends, split_ratio,
                           actions_complete, close_split_adjusted,
                           split_normalization_factor, split_normalization_through, source
                    FROM deduped
                    WHERE date_rn = 1
                    ORDER BY date DESC
                    LIMIT ?
                )
                SELECT ticker, security_id, date, close, dividends, split_ratio,
                       actions_complete, close_split_adjusted,
                       split_normalization_factor, split_normalization_through, source
                FROM recent
                ORDER BY date
                """,
                (
                    security_id,
                    str(as_of),
                    normalized_ticker,
                    security_id,
                    str(as_of),
                    observations,
                ),
            )
        else:
            rows = self.query(
                """
                WITH deduped AS (
                    SELECT ticker, security_id, date, close, dividends, split_ratio,
                           actions_complete, close_split_adjusted,
                           split_normalization_factor, split_normalization_through, source,
                           ROW_NUMBER() OVER (
                               PARTITION BY date
                               ORDER BY fetched_at DESC, ticker DESC
                           ) AS date_rn
                    FROM prices
                    WHERE ticker = ?
                      AND date <= CAST(? AS DATE)
                ), recent AS (
                    SELECT ticker, security_id, date, close, dividends, split_ratio,
                           actions_complete, close_split_adjusted,
                           split_normalization_factor, split_normalization_through, source
                    FROM deduped
                    WHERE date_rn = 1
                    ORDER BY date DESC
                    LIMIT ?
                )
                SELECT ticker, security_id, date, close, dividends, split_ratio,
                       actions_complete, close_split_adjusted,
                       split_normalization_factor, split_normalization_through, source
                FROM recent
                ORDER BY date
                """,
                (normalized_ticker, str(as_of), observations),
            )
        return rows

    def pit_factor_price_histories_batch(
        self,
        tickers: list[str],
        as_of: date | str,
        *,
        observations: int,
    ) -> dict[str, list[dict]]:
        """Return identity-safe factor price windows for a whole universe.

        This is the set-based equivalent of :meth:`pit_factor_price_history`.
        It preserves provider-gap handling, source priority, date de-duplication,
        and the legacy ticker-only compatibility route.
        """
        if observations < 2:
            raise ValueError("factor price history requires at least two observations")
        routes = self._factor_identity_routes(tickers, as_of)
        result = {ticker: [] for ticker in routes}
        readable: list[dict] = []
        for ticker, route in routes.items():
            if route.get("has_reviewed_mapping"):
                if not route.get("has_active_mapping"):
                    continue
                readable.append(
                    {
                        "requested_ticker": ticker,
                        "identity_kind": "security",
                        "identity_value": route["security_id"],
                    }
                )
            else:
                readable.append(
                    {
                        "requested_ticker": ticker,
                        "identity_kind": "ticker",
                        "identity_value": ticker,
                    }
                )
        if not readable:
            return result

        relation = f"_tmp_factor_price_history_routes_{uuid4().hex}"
        self._con.register(relation, _rows_to_arrowable(readable))
        try:
            rows = self.query(
                f"""
                WITH combined AS (
                    SELECT route.requested_ticker,
                           route.identity_kind,
                           price.ticker,
                           price.security_id,
                           price.date,
                           price.close,
                           price.dividends,
                           price.split_ratio,
                           price.actions_complete,
                           price.close_split_adjusted,
                           price.split_normalization_factor,
                           price.split_normalization_through,
                           price.source,
                           price.fetched_at,
                           2 AS source_priority
                    FROM {relation} AS route
                    JOIN prices AS price
                      ON route.identity_kind = 'security'
                     AND price.security_id = route.identity_value
                    WHERE price.date <= CAST(? AS DATE)
                    UNION ALL
                    SELECT route.requested_ticker,
                           route.identity_kind,
                           CAST(route.requested_ticker AS VARCHAR) AS ticker,
                           factor_price.security_id,
                           factor_price.date,
                           factor_price.close,
                           factor_price.dividends,
                           factor_price.split_ratio,
                           factor_price.actions_complete,
                           factor_price.close_split_adjusted,
                           factor_price.split_normalization_factor,
                           factor_price.split_normalization_through,
                           factor_price.provider AS source,
                           factor_price.fetched_at,
                           1 AS source_priority
                    FROM {relation} AS route
                    JOIN factor_prices AS factor_price
                      ON route.identity_kind = 'security'
                     AND factor_price.security_id = route.identity_value
                    WHERE factor_price.date <= CAST(? AS DATE)
                    UNION ALL
                    SELECT route.requested_ticker,
                           route.identity_kind,
                           price.ticker,
                           price.security_id,
                           price.date,
                           price.close,
                           price.dividends,
                           price.split_ratio,
                           price.actions_complete,
                           price.close_split_adjusted,
                           price.split_normalization_factor,
                           price.split_normalization_through,
                           price.source,
                           price.fetched_at,
                           2 AS source_priority
                    FROM {relation} AS route
                    JOIN prices AS price
                      ON route.identity_kind = 'ticker'
                     AND price.ticker = route.identity_value
                    WHERE price.date <= CAST(? AS DATE)
                ), deduped AS (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY requested_ticker, date
                               ORDER BY source_priority DESC,
                                        fetched_at DESC,
                                        CASE
                                            WHEN identity_kind = 'ticker' THEN ticker
                                            ELSE NULL
                                        END DESC
                           ) AS date_rn
                    FROM combined
                ), recent AS (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY requested_ticker
                               ORDER BY date DESC
                           ) AS observation_rn
                    FROM deduped
                    WHERE date_rn = 1
                )
                SELECT requested_ticker, ticker, security_id, date, close,
                       dividends, split_ratio, actions_complete,
                       close_split_adjusted, split_normalization_factor,
                       split_normalization_through, source
                FROM recent
                WHERE observation_rn <= ?
                ORDER BY requested_ticker, date
                """,
                (str(as_of), str(as_of), str(as_of), observations),
            )
        finally:
            self._con.unregister(relation)

        for row in rows:
            ticker = str(row.pop("requested_ticker"))
            result[ticker].append(row)
        return result

    def price_action_refresh_candidates(
        self,
        provider: str,
        start: date | str,
        end: date | str,
    ) -> list[str]:
        """Return reviewed securities with unverified actions in a date window."""
        rows = self.query(
            """
            SELECT DISTINCT price.security_id
            FROM prices AS price
            WHERE price.security_id IS NOT NULL
              AND (
                  price.source = ?
                  OR (? = 'yfinance'
                      AND price.source = 'yfinance:ohlc-envelope-v1')
              )
              AND price.date >= CAST(? AS DATE)
              AND price.date < CAST(? AS DATE)
              AND (
                    price.actions_complete IS NOT TRUE
                    OR (
                        price.close_split_adjusted IS TRUE
                        AND price.split_normalization_factor IS NULL
                    )
              )
              AND EXISTS (
                  SELECT 1
                  FROM provider_symbol_history AS mapping
                  WHERE mapping.security_id = price.security_id
                    AND mapping.provider = CASE
                        WHEN price.source = 'yfinance:ohlc-envelope-v1'
                        THEN 'yfinance'
                        ELSE price.source
                    END
                    AND mapping.provider_symbol = price.provider_symbol
                    AND mapping.mapping_status = 'verified'
                    AND mapping.data_start <= price.date
                    AND (mapping.data_end IS NULL OR mapping.data_end > price.date)
              )
            ORDER BY price.security_id
            """,
            (provider.lower(), provider.lower(), str(start), str(end)),
        )
        return [str(row["security_id"]) for row in rows]

    def unverified_price_action_count(
        self,
        security_id: str,
        provider: str,
        start: date | str,
        end: date | str,
    ) -> int:
        """Count unresolved action-provenance rows after a corrective fetch."""
        return int(
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM prices
                WHERE security_id = ?
                  AND (
                      source = ?
                      OR (? = 'yfinance' AND source = 'yfinance:ohlc-envelope-v1')
                  )
                  AND date >= CAST(? AS DATE)
                  AND date < CAST(? AS DATE)
                  AND (
                        actions_complete IS NOT TRUE
                        OR (
                            close_split_adjusted IS TRUE
                            AND split_normalization_factor IS NULL
                        )
                  )
                """,
                (
                    security_id,
                    provider.lower(),
                    provider.lower(),
                    str(start),
                    str(end),
                ),
            )[0]["n"]
        )

    def unverified_ticker_action_count(
        self,
        ticker: str,
        provider: str,
        start: date | str,
        end: date | str,
    ) -> int:
        """Count unresolved action rows for an explicit benchmark/calendar ticker."""
        return int(
            self.query(
                """
                SELECT COUNT(*) AS n
                FROM prices
                WHERE ticker = ?
                  AND (
                      source = ?
                      OR (? = 'yfinance' AND source = 'yfinance:ohlc-envelope-v1')
                  )
                  AND date >= CAST(? AS DATE)
                  AND date < CAST(? AS DATE)
                  AND (
                        actions_complete IS NOT TRUE
                        OR (
                            close_split_adjusted IS TRUE
                            AND split_normalization_factor IS NULL
                        )
                  )
                """,
                (
                    ticker.upper(),
                    provider.lower(),
                    provider.lower(),
                    str(start),
                    str(end),
                ),
            )[0]["n"]
        )

    def universe_membership_known_on(
        self,
        universe_id: str,
        known_as_of: date | str,
        effective_on: date | str,
    ) -> list[dict]:
        """Return membership known on one date and effective on another."""
        return self.query(
            """
            SELECT universe_id, ticker, effective_start, effective_end,
                   security_id, known_date, end_known_date, source
            FROM universe_membership
            WHERE universe_id = ?
              AND known_date <= CAST(? AS DATE)
              AND effective_start <= CAST(? AS DATE)
              AND (
                  effective_end IS NULL
                  OR effective_end > CAST(? AS DATE)
                  OR end_known_date > CAST(? AS DATE)
              )
            ORDER BY ticker
            """,
            (
                universe_id,
                str(known_as_of),
                str(effective_on),
                str(effective_on),
                str(known_as_of),
            ),
        )

    def universe_membership_on(self, universe_id: str, as_of: date | str) -> list[dict]:
        """Return members active and publicly known on the same date."""
        return self.universe_membership_known_on(universe_id, as_of, as_of)

    def universe_identity_labels(
        self,
        universe_id: str,
        known_as_of: date | str,
        effective_on: date | str | None = None,
    ) -> list[dict]:
        """Return display labels from reviewed security-to-issuer identity links.

        Canonical issuer names are presentation metadata only. They never enter
        factor calculations, and missing names fall back visibly to the ticker
        in the dashboard instead of being guessed from a provider response.
        """
        return self.query(
            """
            WITH members AS (
                SELECT membership.ticker, membership.security_id,
                       CAST(? AS DATE) AS known_as_of
                FROM universe_membership AS membership
                WHERE membership.universe_id = ?
                  AND membership.known_date <= CAST(? AS DATE)
                  AND membership.effective_start <= CAST(? AS DATE)
                  AND (
                      membership.effective_end IS NULL
                      OR membership.effective_end > CAST(? AS DATE)
                      OR membership.end_known_date > CAST(? AS DATE)
                  )
            ), identified AS (
                SELECT members.ticker, members.security_id,
                       (
                           SELECT owner.issuer_id
                           FROM security_issuer_assignments AS owner
                           WHERE owner.security_id = members.security_id
                             AND owner.effective_start <= members.known_as_of
                             AND (
                                 owner.effective_end IS NULL
                                 OR owner.effective_end > members.known_as_of
                             )
                           ORDER BY owner.effective_start DESC
                           LIMIT 1
                       ) AS issuer_id
                FROM members
            )
            SELECT identified.ticker, identified.security_id,
                   identified.issuer_id, issuer.canonical_name,
                   issuer.source AS name_source
            FROM identified
            LEFT JOIN issuer_master AS issuer USING (issuer_id)
            ORDER BY identified.ticker
            """,
            (
                str(known_as_of),
                universe_id,
                str(known_as_of),
                str(effective_on or known_as_of),
                str(effective_on or known_as_of),
                str(known_as_of),
            ),
        )

    def universe_data_coverage(
        self,
        universe_id: str,
        as_of: date | str,
        effective_on: date | str | None = None,
    ) -> list[dict]:
        """Report PIT fundamentals and price availability for each active member.

        Reviewed identities use issuer-tagged fundamentals and security-tagged
        prices. Unreviewed names retain the legacy dated-ticker path. This
        avoids both false gaps after a real ticker change and false coverage
        from an unrelated company that previously reused the same symbol.
        """
        return self.query(
            """
            WITH decision AS (
                SELECT CAST(? AS DATE) AS as_of, CAST(? AS DATE) AS effective_on
            ), members AS (
                SELECT membership.universe_id, membership.ticker,
                       membership.security_id, decision.as_of
                FROM universe_membership AS membership
                CROSS JOIN decision
                WHERE membership.universe_id = ?
                  AND membership.known_date <= decision.as_of
                  AND membership.effective_start <= decision.effective_on
                  AND (
                      membership.effective_end IS NULL
                      OR membership.effective_end > decision.effective_on
                      OR membership.end_known_date > decision.as_of
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
            (str(as_of), str(effective_on or as_of), universe_id),
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
            "security_conversions",
            "security_ticker_extensions",
            "factor_price_provenance",
            "factor_prices",
            "prices",
            "fundamentals",
            "fundamentals_quarantine",
            "macro",
            "universe_membership",
            "universe_coverage_attestations",
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


def _utc_database_timestamp(value: Any) -> str:
    if not isinstance(value, datetime):
        raise ValueError("stored evidence capture time is not a timestamp")
    moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return moment.isoformat().replace("+00:00", "Z")


def _utc_naive_database_timestamp(value: Any, *, label: str) -> datetime:
    """Normalize aware instants before writing a zone-less DuckDB timestamp.

    Existing naive values retain their legacy wall-time basis. New aware values
    use UTC wall time instead of being cast through the host timezone.
    """

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} is not an ISO timestamp") from exc
    else:
        raise ValueError(f"{label} is not a timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _as_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _required_text(row: dict, key: str, label: str) -> str:
    value = str(row.get(key) or "").strip()
    if not value:
        raise ValueError(f"{label} requires {key}")
    return value


def _ingest_subject(
    subject_type: str | None,
    subject_id: str | None,
) -> tuple[str | None, str | None]:
    """Normalize an optional all-or-none ingest subject."""
    if (subject_type is None) != (subject_id is None):
        raise ValueError("ingest subject_type and subject_id must be provided together")
    if subject_type is None:
        return None, None
    if not isinstance(subject_type, str) or not isinstance(subject_id, str):
        raise TypeError("ingest subject_type and subject_id must be strings")
    normalized_type = subject_type.strip().lower()
    normalized_id = subject_id.strip()
    if not normalized_type or not normalized_id:
        raise ValueError("ingest subject_type and subject_id cannot be blank")
    return normalized_type, normalized_id


def _fundamental_provenance(row: dict) -> dict[str, str | None]:
    """Normalize nullable all-or-none immutable SEC row provenance."""
    keys = (
        "ingest_run_id",
        "source_snapshot_id",
        "source_rowset_sha256",
        "source_row_sha256",
    )
    values = {key: (str(row.get(key) or "").strip() or None) for key in keys}
    present = {key for key, value in values.items() if value is not None}
    if present and present != set(keys):
        raise ValueError(
            "fundamental provenance requires ingest_run_id, source_snapshot_id, "
            "source_rowset_sha256, and source_row_sha256 together"
        )
    if not present:
        if row.get("source_fact_locator") is not None:
            raise ValueError(
                "fundamental source_fact_locator requires complete immutable provenance"
            )
        return values
    for key in ("source_rowset_sha256", "source_row_sha256"):
        digest = str(values[key]).lower()
        if len(digest) != 64:
            raise ValueError(f"fundamental {key} must be a 64-character SHA-256")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ValueError(f"fundamental {key} must be a 64-character SHA-256") from exc
        values[key] = digest
    if str(row.get("source") or "edgar").strip().lower() != "edgar":
        raise ValueError("fundamental provenance is supported only for SEC EDGAR rows")
    if not str(row.get("issuer_id") or "").strip():
        raise ValueError("fundamental provenance requires issuer_id")
    locator = row.get("source_fact_locator")
    if locator is not None:
        if not isinstance(locator, str):
            raise TypeError("fundamental source_fact_locator must be canonical JSON text")
        try:
            decoded = json.loads(locator)
        except json.JSONDecodeError as exc:
            raise ValueError("fundamental source_fact_locator is invalid JSON") from exc
        if (
            not isinstance(decoded, list)
            or not decoded
            or any(not isinstance(item, dict) for item in decoded)
        ):
            raise ValueError("fundamental source_fact_locator must contain source locator objects")
        canonical = json.dumps(
            decoded,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        if locator != canonical:
            raise ValueError("fundamental source_fact_locator must be canonical JSON")
        locator_fields = {
            "taxonomy",
            "concept",
            "accession",
            "form",
            "start",
            "end",
            "filed",
            "fiscal_period",
            "fiscal_year",
            "frame",
        }
        required_text = {
            "taxonomy",
            "concept",
            "accession",
            "form",
            "end",
            "filed",
        }
        for item in decoded:
            if set(item) != locator_fields:
                raise ValueError("fundamental source_fact_locator has an incomplete field set")
            if any(
                not isinstance(item[field], str) or not item[field].strip()
                for field in required_text
            ):
                raise ValueError("fundamental source_fact_locator has a blank required field")
            try:
                locator_end = date.fromisoformat(item["end"])
                locator_filed = date.fromisoformat(item["filed"])
                locator_start = (
                    date.fromisoformat(item["start"]) if item["start"] is not None else None
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("fundamental source_fact_locator has an invalid date") from exc
            if locator_end != _as_date(row["period_end"]) or locator_filed != _as_date(
                row["as_of_date"]
            ):
                raise ValueError("fundamental source_fact_locator dates do not match the row")
            if locator_start is not None and locator_start >= locator_end:
                raise ValueError("fundamental source_fact_locator start must precede end")
        encoded_locators = [
            json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            for item in decoded
        ]
        if encoded_locators != sorted(set(encoded_locators)):
            raise ValueError("fundamental source_fact_locator entries must be sorted and unique")
    return values


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


def _validate_raw_snapshot_registration(
    payload: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    """Reject malformed immutable-evidence metadata before opening a transaction."""

    def sha256(value: Any, label: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"{label} must be a lowercase 64-character SHA-256")
        return value

    payload_hash = sha256(payload.get("payload_sha256"), "raw payload hash")
    if sha256(snapshot.get("payload_sha256"), "raw snapshot payload hash") != payload_hash:
        raise ValueError("raw snapshot payload hash does not match its payload record")
    sha256(snapshot.get("request_fingerprint"), "raw snapshot request fingerprint")
    if payload.get("compression") != "gzip":
        raise ValueError("raw snapshot compression must be gzip")
    for key, limit in (
        ("original_bytes", _RAW_SNAPSHOT_MAX_ORIGINAL_BYTES),
        ("stored_bytes", _RAW_SNAPSHOT_MAX_STORED_BYTES),
    ):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= limit:
            raise ValueError(f"raw snapshot {key} is out of bounds")

    parsed_count = snapshot.get("parsed_row_count")
    parsed_hash = snapshot.get("parsed_rows_sha256")
    if (parsed_count is None) != (parsed_hash is None):
        raise ValueError("raw snapshot parsed row evidence is incomplete")
    if parsed_count is not None:
        if isinstance(parsed_count, bool) or not isinstance(parsed_count, int) or parsed_count < 0:
            raise ValueError("raw snapshot parsed row count cannot be negative")
        sha256(parsed_hash, "raw snapshot parsed-row hash")

    rejected = snapshot.get("parsed_rows_rejected")
    rejection_codes = snapshot.get("parsed_rejection_codes")
    if rejected is None:
        if rejection_codes is not None:
            raise ValueError("raw snapshot rejection evidence is incomplete")
    else:
        if isinstance(rejected, bool) or not isinstance(rejected, int) or rejected < 0:
            raise ValueError("raw snapshot rejected row count cannot be negative")
        decoded_codes = decode_rejection_codes(rejection_codes)
        if (rejected == 0) != (decoded_codes is None):
            raise ValueError("raw snapshot rejection count and codes are inconsistent")
        if parsed_count is None:
            raise ValueError("raw snapshot rejection evidence requires parsed row evidence")

    parsed_companyfacts = (
        snapshot.get("provider") == "sec-edgar"
        and snapshot.get("dataset") == "companyfacts"
        and snapshot.get("parser_version")
        in {
            "sec-companyfacts-v2",
            "sec-companyfacts-v2-storage-safe-v1",
            "sec-companyfacts-v2-storage-safe-v2",
            "sec-companyfacts-v3",
            "sec-companyfacts-v4",
        }
        and parsed_count is not None
    )
    if parsed_companyfacts and rejected is None:
        raise ValueError("parsed SEC Company Facts snapshots require rejection evidence")


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
def store_scope(
    db_path: Path | None = None,
    *,
    read_only: bool = False,
    lock_wait_seconds: float | None = None,
) -> Iterator[Store]:
    """Context manager that yields a fresh Store and closes it after."""
    s = Store(
        db_path,
        read_only=read_only,
        lock_wait_seconds=lock_wait_seconds,
    )
    try:
        yield s
    finally:
        s.close()


def close_global_store() -> None:
    """Release the process-global connection when moving to scoped access."""
    global _store
    if _store is None:
        return
    _store.close()
    _store = None


def _connect_with_lock_wait(
    db_path: Path,
    *,
    read_only: bool,
    wait_seconds: float,
) -> duckdb.DuckDBPyConnection:
    """Open DuckDB, retrying only its explicit cross-process lock conflict."""
    deadline = monotonic() + wait_seconds
    warned = False
    while True:
        try:
            return duckdb.connect(str(db_path), read_only=read_only)
        except duckdb.IOException as exc:
            message = str(exc).lower()
            lock_conflict = "could not set lock" in message or "conflicting lock" in message
            if not lock_conflict or monotonic() >= deadline:
                raise
            if not warned:
                log.info(
                    "duckdb.lock_wait",
                    db=str(db_path),
                    read_only=read_only,
                    wait_seconds=wait_seconds,
                )
                warned = True
            sleep(min(0.5, max(0.0, deadline - monotonic())))

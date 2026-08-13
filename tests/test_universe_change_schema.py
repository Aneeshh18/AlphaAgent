from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from aios.storage.store import (
    UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,
    Store,
)


def _insert_activation_receipt(store: Store) -> None:
    store.execute(
        """
        INSERT INTO universe_constituent_change_activations (
            activation_id, event_id, plan_sha256,
            activation_payload_sha256, activation_run_id,
            fundamental_run_id, price_run_id, source_attestation_id,
            schema_version, universe_id, announcement_date, effective_date,
            prior_coverage_through, target_coverage_through,
            official_detail_snapshot_id, component_snapshot_id,
            before_member_set_sha256, after_member_set_sha256,
            before_state_sha256, after_state_sha256, change_rows_sha256,
            activation_payload_json, backup_manifest_sha256, actor,
            policy_version, counts_json, activated_at, status
        ) VALUES (
            'activation-1', 'event-1', ?, ?, 'activation-run-1',
            'fundamental-run-1', 'price-run-1', 'attestation-1',
            1, 'sp500', DATE '2026-07-31', DATE '2026-08-05',
            DATE '2026-08-04', DATE '2026-08-05', 'official-1',
            'component-1', ?, ?, ?, ?, ?, '{not-json', ?, 'operator',
            'invented-policy', '{not-json', ?, 'accepted'
        )
        """,
        (
            "b" * 64,
            "c" * 64,
            "d" * 64,
            "e" * 64,
            "f" * 64,
            "a" * 64,
            "1" * 64,
            "2" * 64,
            datetime(2026, 8, 5, 22, tzinfo=UTC),
        ),
    )


def test_fresh_store_certifies_universe_change_activation_schema(tmp_path: Path) -> None:
    database = tmp_path / "aios.duckdb"
    store = Store(database)
    try:
        columns = store.query("DESCRIBE universe_constituent_change_activations")
        assert len(columns) == 29
        assert columns[0]["column_name"] == "activation_id"
        assert columns[-1]["column_name"] == "created_at"
        assert store.query(
            "SELECT name FROM schema_migrations WHERE name = ?",
            (UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,),
        ) == [{"name": UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION}]
    finally:
        store.close()

    reopened = Store(database)
    try:
        assert reopened.query(
            "SELECT COUNT(*) AS n FROM schema_migrations WHERE name = ?",
            (UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,),
        ) == [{"n": 1}]
    finally:
        reopened.close()


def test_partial_universe_change_activation_schema_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "partial.duckdb"
    connection = duckdb.connect(str(database))
    try:
        connection.execute(
            """
            CREATE TABLE universe_constituent_change_activations (
                activation_id VARCHAR PRIMARY KEY,
                plan_sha256 VARCHAR NOT NULL UNIQUE
            )
            """
        )
    finally:
        connection.close()

    with pytest.raises(
        RuntimeError,
        match="activation schema is incomplete or unsupported",
    ):
        Store(database)


def test_unmarked_universe_change_activation_rows_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "unmarked.duckdb"
    store = Store(database)
    try:
        store.execute(
            "DELETE FROM schema_migrations WHERE name = ?",
            (UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,),
        )
        _insert_activation_receipt(store)
    finally:
        store.close()

    with pytest.raises(RuntimeError, match="rows exist without a migration marker"):
        Store(database)


def test_marked_activation_receipts_fail_when_semantic_payload_is_invalid(
    tmp_path: Path,
) -> None:
    database = tmp_path / "unsupported-receipt.duckdb"
    store = Store(database)
    try:
        _insert_activation_receipt(store)
    finally:
        store.close()

    blocked = Store(database, read_only=True)
    try:
        with pytest.raises(RuntimeError, match="receipt is not JSON"):
            blocked.require_universe_change_activation_schema()
    finally:
        blocked.close()


def test_missing_receipt_table_after_marker_fails_before_recreation(tmp_path: Path) -> None:
    database = tmp_path / "missing.duckdb"
    store = Store(database)
    store.close()

    connection = duckdb.connect(str(database))
    try:
        connection.execute("DROP TABLE universe_constituent_change_activations")
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="receipt table is missing"):
        Store(database)

    read_only = duckdb.connect(str(database), read_only=True)
    try:
        assert read_only.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_schema = 'main'
              AND table_name = 'universe_constituent_change_activations'
            """
        ).fetchone() == (0,)
    finally:
        read_only.close()

def test_full_column_table_without_constraints_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "weak.duckdb"
    store = Store(database)
    store.close()

    connection = duckdb.connect(str(database))
    try:
        ddl = connection.execute(
            """
            SELECT sql FROM duckdb_tables()
            WHERE table_name = 'universe_constituent_change_activations'
            """
        ).fetchone()[0]
        weak_ddl = str(ddl).split(", CHECK(", maxsplit=1)[0] + ");"
        connection.execute("DROP TABLE universe_constituent_change_activations")
        connection.execute(weak_ddl)
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="constraints are incomplete"):
        Store(database)


@pytest.mark.parametrize("extra_constraint", ("UNIQUE(actor)", "CHECK(length(event_id) > 0)"))
def test_foreign_extra_activation_constraints_fail_closed(
    tmp_path: Path,
    extra_constraint: str,
) -> None:
    database = tmp_path / "extra-constraint.duckdb"
    store = Store(database)
    store.close()
    connection = duckdb.connect(str(database))
    try:
        ddl = str(
            connection.execute(
                """
                SELECT sql FROM duckdb_tables()
                WHERE table_name = 'universe_constituent_change_activations'
                """
            ).fetchone()[0]
        )
        connection.execute("DROP TABLE universe_constituent_change_activations")
        connection.execute(ddl[:-2] + f", {extra_constraint});")
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="constraints are incomplete"):
        Store(database)


def test_read_only_capability_check_never_migrates(tmp_path: Path) -> None:
    database = tmp_path / "read-only.duckdb"
    writable = Store(database)
    try:
        writable.execute(
            "DELETE FROM schema_migrations WHERE name = ?",
            (UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,),
        )
    finally:
        writable.close()

    read_only = Store(database, read_only=True)
    try:
        with pytest.raises(RuntimeError, match="capability is not certified"):
            read_only.require_universe_change_activation_schema()
        assert read_only.query(
            "SELECT COUNT(*) AS n FROM schema_migrations WHERE name = ?",
            (UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,),
        ) == [{"n": 0}]
    finally:
        read_only.close()

    with pytest.raises(RuntimeError, match="capability is not certified"):
        Store(database)
    upgraded = Store(database, allow_schema_upgrade=True)
    try:
        upgraded.require_universe_change_activation_schema()
    finally:
        upgraded.close()


def test_read_only_capability_check_accepts_certified_schema(tmp_path: Path) -> None:
    database = tmp_path / "certified.duckdb"
    writable = Store(database)
    writable.close()

    read_only = Store(database, read_only=True)
    try:
        read_only.require_universe_change_activation_schema()
    finally:
        read_only.close()


def test_existing_database_requires_explicit_backup_first_upgrade_mode(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.duckdb"
    current = Store(database)
    current.close()
    legacy = duckdb.connect(str(database))
    try:
        legacy.execute(
            "DELETE FROM schema_migrations WHERE name = ?",
            (UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,),
        )
        legacy.execute("DROP TABLE universe_constituent_change_activations")
    finally:
        legacy.close()

    with pytest.raises(RuntimeError, match="backup-first upgrade-local-state"):
        Store(database)
    untouched = duckdb.connect(str(database), read_only=True)
    try:
        assert untouched.execute(
            """
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_name = 'universe_constituent_change_activations'
            """
        ).fetchone() == (0,)
    finally:
        untouched.close()

    upgraded = Store(database, allow_schema_upgrade=True)
    try:
        upgraded.require_universe_change_activation_schema()
    finally:
        upgraded.close()


def test_future_activation_migration_marker_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "future-marker.duckdb"
    store = Store(database)
    try:
        store.execute(
            """
            UPDATE schema_migrations
            SET applied_at = TIMESTAMP '2099-01-01 00:00:00'
            WHERE name = ?
            """,
            (UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,),
        )
    finally:
        store.close()

    with pytest.raises(RuntimeError, match="migration marker is invalid"):
        Store(database)

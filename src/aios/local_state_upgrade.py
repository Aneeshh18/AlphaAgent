"""Backup-first, rehearsed upgrades for AIOS local governed state."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from aios.alerts import ALERT_SCHEMA_VERSION, AlertStore
from aios.artifacts import publish_text_write_once
from aios.maintenance import project_maintenance_lock
from aios.operations import BackupResult, create_local_backup, verify_local_backup
from aios.paper import canonical_payload_sha256
from aios.storage.store import (
    FUNDAMENTAL_EVIDENCE_VERSIONS_MIGRATION,
    UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,
    Store,
    checkpoint_database_for_backup,
)

LOCAL_STATE_UPGRADE_KIND = "aios.local-state-upgrade"
LOCAL_STATE_UPGRADE_SCHEMA_VERSION = 2
LOCAL_STATE_UPGRADE_REPORT_DIRECTORY = Path("data/reports/local_state_upgrades")
_DUCKDB_ADDITIVE_MIGRATION_TABLES = {
    "fundamental_versions",
    "schema_migrations",
}


@dataclass(frozen=True)
class LocalStateUpgradeResult:
    """Verified evidence for one backup-first local state upgrade."""

    backup: BackupResult
    journal_directory: Path
    receipt: Path
    operations_schema_before: int
    operations_schema_after: int
    fundamentals: int
    fundamental_versions: int


def upgrade_local_state(
    project_root: Path,
    database_path: Path,
    operations_database_path: Path,
    *,
    application_version: str,
    output: Path | None = None,
    confirm: bool = False,
    now: datetime | None = None,
) -> LocalStateUpgradeResult:
    """Back up, rehearse, and apply supported local schema upgrades."""
    if not confirm:
        raise ValueError("local state upgrade requires explicit confirmation")
    if not isinstance(application_version, str) or not application_version.strip():
        raise ValueError("application version is required")
    root = _safe_project_root(project_root)
    with project_maintenance_lock(root, operation="upgrade-local-state"):
        return _upgrade_local_state_under_lease(
            root,
            database_path,
            operations_database_path,
            application_version=application_version,
            output=output,
            now=now,
        )


def recover_local_state_upgrade(
    project_root: Path,
    journal_directory: Path,
    database_path: Path,
    operations_database_path: Path,
    *,
    confirm: bool = False,
) -> LocalStateUpgradeResult:
    """Resume or verify one exact prepared local-state upgrade attempt."""
    if not confirm:
        raise ValueError("local state upgrade recovery requires explicit confirmation")
    root = _safe_project_root(project_root)
    journal = _safe_upgrade_journal(root, journal_directory)
    with project_maintenance_lock(root, operation="upgrade-local-state-recovery"):
        return _recover_local_state_upgrade_under_lease(
            root,
            journal,
            database_path,
            operations_database_path,
        )


def _upgrade_local_state_under_lease(
    root: Path,
    database_path: Path,
    operations_database_path: Path,
    *,
    application_version: str,
    output: Path | None,
    now: datetime | None,
) -> LocalStateUpgradeResult:
    """Run after the public API has acquired the sole mutation lease."""
    database = _safe_existing_file(root, database_path, label="analytical database")
    operations = _safe_existing_file(
        root,
        operations_database_path,
        label="operations database",
    )
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    backup_destination = _safe_backup_destination(
        root,
        output,
        timestamp=timestamp,
    )

    checkpoint_database_for_backup(database)
    backup = create_local_backup(
        root,
        database,
        operations_database_path=operations,
        output=backup_destination,
        application_version=application_version,
        now=timestamp,
    )
    verify_local_backup(backup.path)
    manifest = json.loads((backup.path / "manifest.json").read_text(encoding="utf-8"))
    backup_database = backup.path / str(manifest["database_file"])
    operations_entries = [
        backup.path / str(item["path"])
        for item in manifest["files"]
        if str(item.get("path", "")).startswith("operations/")
    ]
    if len(operations_entries) != 1:
        raise RuntimeError("verified pre-upgrade backup has no unique operations ledger")
    backup_operations = operations_entries[0]

    database_before = _database_baseline(backup_database)
    operations_before = _sqlite_baseline(backup_operations)
    rehearsal = _rehearse_upgrade(
        backup_database,
        backup_operations,
        database_before=database_before,
        operations_before=operations_before,
    )

    if _database_baseline(database) != database_before:
        raise RuntimeError("live analytical database changed after the verified backup")
    if _sqlite_baseline(operations) != operations_before:
        raise RuntimeError("live operations ledger changed after the verified backup")

    attempt_id = uuid4().hex
    journal = (
        root
        / LOCAL_STATE_UPGRADE_REPORT_DIRECTORY
        / "attempts"
        / backup.manifest_sha256
        / attempt_id
    )
    common = {
        "attempt_id": attempt_id,
        "started_at": timestamp.isoformat().replace("+00:00", "Z"),
        "application_version": application_version,
        "backup": {
            "path": backup.path.relative_to(root).as_posix(),
            "files": backup.files,
            "bytes": backup.bytes,
            "manifest_sha256": backup.manifest_sha256,
        },
        "pre_upgrade": {
            "analytical_baseline_sha256": str(database_before["baseline_sha256"]),
            "operations_baseline_sha256": str(operations_before["baseline_sha256"]),
            "operations_schema": int(operations_before["schema_version"]),
        },
        "rehearsal": rehearsal,
    }
    _prepared, previous_sha256 = _write_upgrade_phase(
        journal,
        order=0,
        phase="prepared",
        previous_payload_sha256=None,
        recorded_at=timestamp,
        state=common,
    )
    try:
        Store(database, allow_schema_upgrade=True).close()
        database_after = _validate_upgraded_database(
            database,
            expected_fundamentals=int(database_before["fundamentals"]),
        )
        _require_duckdb_rows_unchanged(database_before, database)
        if database_after != rehearsal["database"]:
            raise RuntimeError("live analytical upgrade differs from the exact rehearsal")
        _analytical, previous_sha256 = _write_upgrade_phase(
            journal,
            order=1,
            phase="analytical_migrated",
            previous_payload_sha256=previous_sha256,
            recorded_at=datetime.now(UTC),
            state={"analytical": database_after},
        )

        AlertStore(operations)
        _checkpoint_operations_wal(operations)
        AlertStore(operations, read_only=True)
        operations_after = _sqlite_baseline(operations)
        _require_sqlite_rows_unchanged(
            operations_before,
            operations_after,
            path=operations,
        )
        if int(operations_after["schema_version"]) != ALERT_SCHEMA_VERSION:
            raise RuntimeError("live operations ledger did not reach the required schema")
        if _sqlite_upgrade_projection(operations_after) != rehearsal["operations"]:
            raise RuntimeError("live operations upgrade differs from the exact rehearsal")
        _operations, previous_sha256 = _write_upgrade_phase(
            journal,
            order=2,
            phase="operations_migrated",
            previous_payload_sha256=previous_sha256,
            recorded_at=datetime.now(UTC),
            state={
                "operations_schema": int(operations_after["schema_version"]),
                "operations_baseline_sha256": str(
                    operations_after["baseline_sha256"]
                ),
            },
        )

        verify_local_backup(backup.path)
        receipt, _receipt_sha256 = _write_upgrade_phase(
            journal,
            order=3,
            phase="verified",
            previous_payload_sha256=previous_sha256,
            recorded_at=datetime.now(UTC),
            state={
                "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "analytical": database_after,
                "operations_schema_before": int(operations_before["schema_version"]),
                "operations_schema_after": int(operations_after["schema_version"]),
                "rehearsal_contract_matched_live": True,
                "backup_reverified_after_upgrade": True,
            },
        )
    except Exception as exc:
        relative_journal = journal.relative_to(root).as_posix()
        raise RuntimeError(
            "local state upgrade attempt requires recovery review; "
            f"journal={relative_journal}; error={type(exc).__name__}: {exc}"
        ) from exc

    return LocalStateUpgradeResult(
        backup=backup,
        journal_directory=journal,
        receipt=receipt,
        operations_schema_before=int(operations_before["schema_version"]),
        operations_schema_after=int(operations_after["schema_version"]),
        fundamentals=int(database_after["fundamentals"]),
        fundamental_versions=int(database_after["fundamental_versions"]),
    )


def _recover_local_state_upgrade_under_lease(
    root: Path,
    journal: Path,
    database_path: Path,
    operations_database_path: Path,
) -> LocalStateUpgradeResult:
    database = _safe_existing_file(root, database_path, label="analytical database")
    operations = _safe_existing_file(
        root,
        operations_database_path,
        label="operations database",
    )
    prepared, previous_sha256 = _read_upgrade_phase(
        journal / "00-prepared.json",
        expected_phase="prepared",
        expected_previous_sha256=None,
    )
    common = prepared["state"]
    if not isinstance(common, dict) or set(common) != {
        "application_version",
        "attempt_id",
        "backup",
        "pre_upgrade",
        "rehearsal",
        "started_at",
    }:
        raise ValueError("upgrade prepared state is invalid")
    attempt_id = str(common["attempt_id"])
    backup_state = common["backup"]
    pre_upgrade = common["pre_upgrade"]
    rehearsal = common["rehearsal"]
    if (
        attempt_id != journal.name
        or not isinstance(backup_state, dict)
        or not isinstance(pre_upgrade, dict)
        or not isinstance(rehearsal, dict)
    ):
        raise ValueError("upgrade prepared identity is invalid")
    backup_path = _safe_existing_backup(root, backup_state.get("path"))
    backup = verify_local_backup(backup_path)
    if backup.manifest_sha256 != backup_state.get("manifest_sha256"):
        raise ValueError("upgrade backup no longer matches its prepared evidence")
    if journal.parent.name != backup.manifest_sha256:
        raise ValueError("upgrade journal is stored under another backup identity")

    manifest = json.loads((backup.path / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("application_version") != common.get("application_version"):
        raise ValueError("upgrade backup application version differs from the journal")
    backup_database = backup.path / str(manifest["database_file"])
    operations_entries = [
        backup.path / str(item["path"])
        for item in manifest["files"]
        if str(item.get("path", "")).startswith("operations/")
    ]
    if len(operations_entries) != 1:
        raise ValueError("upgrade backup has no unique operations ledger")
    database_before = _database_baseline(backup_database)
    operations_before = _sqlite_baseline(operations_entries[0])
    if (
        database_before["baseline_sha256"]
        != pre_upgrade.get("analytical_baseline_sha256")
        or operations_before["baseline_sha256"]
        != pre_upgrade.get("operations_baseline_sha256")
    ):
        raise ValueError("upgrade backup baseline differs from prepared evidence")

    existing = {path.name for path in journal.glob("*.json")}
    allowed = {
        "00-prepared.json",
        "01-analytical_migrated.json",
        "02-operations_migrated.json",
        "03-verified.json",
    }
    if not existing <= allowed:
        raise ValueError("upgrade journal contains an unsupported phase file")

    if _database_baseline(database) == database_before:
        Store(database, allow_schema_upgrade=True).close()
    database_after = _validate_upgraded_database(
        database,
        expected_fundamentals=int(database_before["fundamentals"]),
    )
    _require_duckdb_rows_unchanged(database_before, database)
    if database_after != rehearsal.get("database"):
        raise RuntimeError("recovered analytical state differs from the rehearsal")
    analytical_state = {"analytical": database_after}
    analytical_path = journal / "01-analytical_migrated.json"
    if analytical_path.exists():
        analytical, previous_sha256 = _read_upgrade_phase(
            analytical_path,
            expected_phase="analytical_migrated",
            expected_previous_sha256=previous_sha256,
        )
        if analytical["state"] != analytical_state:
            raise ValueError("upgrade analytical phase evidence is inconsistent")
    else:
        _analytical, previous_sha256 = _write_upgrade_phase(
            journal,
            order=1,
            phase="analytical_migrated",
            previous_payload_sha256=previous_sha256,
            recorded_at=datetime.now(UTC),
            state=analytical_state,
        )

    operations_current = _sqlite_baseline(operations)
    if operations_current == operations_before:
        AlertStore(operations)
        _checkpoint_operations_wal(operations)
    AlertStore(operations, read_only=True)
    operations_after = _sqlite_baseline(operations)
    _require_sqlite_rows_unchanged(
        operations_before,
        operations_after,
        path=operations,
    )
    operations_projection = _sqlite_upgrade_projection(operations_after)
    if operations_projection != rehearsal.get("operations"):
        raise RuntimeError("recovered operations state differs from the rehearsal")
    operations_state = {
        "operations_schema": int(operations_after["schema_version"]),
        "operations_baseline_sha256": str(operations_after["baseline_sha256"]),
    }
    operations_path = journal / "02-operations_migrated.json"
    if operations_path.exists():
        operations_phase, previous_sha256 = _read_upgrade_phase(
            operations_path,
            expected_phase="operations_migrated",
            expected_previous_sha256=previous_sha256,
        )
        if operations_phase["state"] != operations_state:
            raise ValueError("upgrade operations phase evidence is inconsistent")
    else:
        _operations, previous_sha256 = _write_upgrade_phase(
            journal,
            order=2,
            phase="operations_migrated",
            previous_payload_sha256=previous_sha256,
            recorded_at=datetime.now(UTC),
            state=operations_state,
        )

    verify_local_backup(backup.path)
    verified_path = journal / "03-verified.json"
    if verified_path.exists():
        verified, _verified_sha256 = _read_upgrade_phase(
            verified_path,
            expected_phase="verified",
            expected_previous_sha256=previous_sha256,
        )
        verified_state = verified["state"]
        if (
            not isinstance(verified_state, dict)
            or verified_state.get("analytical") != database_after
            or verified_state.get("operations_schema_after")
            != int(operations_after["schema_version"])
            or verified_state.get("backup_reverified_after_upgrade") is not True
        ):
            raise ValueError("upgrade verified phase evidence is inconsistent")
        receipt = verified_path
    else:
        receipt, _verified_sha256 = _write_upgrade_phase(
            journal,
            order=3,
            phase="verified",
            previous_payload_sha256=previous_sha256,
            recorded_at=datetime.now(UTC),
            state={
                "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "analytical": database_after,
                "operations_schema_before": int(operations_before["schema_version"]),
                "operations_schema_after": int(operations_after["schema_version"]),
                "rehearsal_contract_matched_live": True,
                "backup_reverified_after_upgrade": True,
                "recovered_attempt": True,
            },
        )

    return LocalStateUpgradeResult(
        backup=backup,
        journal_directory=journal,
        receipt=receipt,
        operations_schema_before=int(operations_before["schema_version"]),
        operations_schema_after=int(operations_after["schema_version"]),
        fundamentals=int(database_after["fundamentals"]),
        fundamental_versions=int(database_after["fundamental_versions"]),
    )


def _write_upgrade_phase(
    journal: Path,
    *,
    order: int,
    phase: str,
    previous_payload_sha256: str | None,
    recorded_at: datetime,
    state: dict[str, Any],
) -> tuple[Path, str]:
    payload = {
        "phase": phase,
        "recorded_at": recorded_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "previous_payload_sha256": previous_payload_sha256,
        "state": state,
    }
    payload_sha256 = canonical_payload_sha256(payload)
    envelope = {
        "document_kind": LOCAL_STATE_UPGRADE_KIND,
        "schema_version": LOCAL_STATE_UPGRADE_SCHEMA_VERSION,
        "payload": payload,
        "payload_sha256": payload_sha256,
    }
    destination = journal / f"{order:02d}-{phase}.json"
    publish_text_write_once(
        destination,
        json.dumps(
            envelope,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n",
    )
    return destination, payload_sha256


def _read_upgrade_phase(
    path: Path,
    *,
    expected_phase: str,
    expected_previous_sha256: str | None,
) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"upgrade phase is missing or unsafe: {path}")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError(f"upgrade phase must be one regular, unaliased file: {path}")
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"upgrade phase is unreadable: {path}") from exc
    if not isinstance(envelope, dict) or set(envelope) != {
        "document_kind",
        "schema_version",
        "payload",
        "payload_sha256",
    }:
        raise ValueError("upgrade phase envelope is invalid")
    payload = envelope.get("payload")
    digest = envelope.get("payload_sha256")
    if (
        envelope.get("document_kind") != LOCAL_STATE_UPGRADE_KIND
        or envelope.get("schema_version") != LOCAL_STATE_UPGRADE_SCHEMA_VERSION
        or not isinstance(payload, dict)
        or set(payload) != {
            "phase",
            "previous_payload_sha256",
            "recorded_at",
            "state",
        }
        or payload.get("phase") != expected_phase
        or payload.get("previous_payload_sha256") != expected_previous_sha256
        or not isinstance(payload.get("state"), dict)
        or not isinstance(digest, str)
        or len(digest) != 64
        or canonical_payload_sha256(payload) != digest
    ):
        raise ValueError("upgrade phase checksum or chain is invalid")
    try:
        datetime.fromisoformat(str(payload["recorded_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("upgrade phase timestamp is invalid") from exc
    return payload, digest


def _rehearse_upgrade(
    backup_database: Path,
    backup_operations: Path,
    *,
    database_before: dict[str, Any],
    operations_before: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="aios-local-state-upgrade-") as temporary:
        root = Path(temporary)
        database = root / backup_database.name
        operations = root / backup_operations.name
        shutil.copy2(backup_database, database)
        shutil.copy2(backup_operations, operations)
        Store(database, allow_schema_upgrade=True).close()
        AlertStore(operations)
        _checkpoint_operations_wal(operations)
        AlertStore(operations, read_only=True)
        database_after = _validate_upgraded_database(
            database,
            expected_fundamentals=int(database_before["fundamentals"]),
        )
        _require_duckdb_rows_unchanged(database_before, database)
        operations_after = _sqlite_baseline(operations)
        _require_sqlite_rows_unchanged(
            operations_before,
            operations_after,
            path=operations,
        )
        if int(operations_after["schema_version"]) != ALERT_SCHEMA_VERSION:
            raise RuntimeError("rehearsed operations ledger did not reach required schema")
        return {
            "database": database_after,
            "operations": _sqlite_upgrade_projection(operations_after),
        }


def _database_baseline(path: Path) -> dict[str, Any]:
    store = Store(path, read_only=True)
    try:
        tables = [
            str(row["table_name"])
            for row in store.query(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'main' AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """
            )
        ]
        primary_keys = {
            str(row["table_name"]): [str(value) for value in row["columns"]]
            for row in store.query(
                """
                SELECT table_name, constraint_column_names AS columns
                FROM duckdb_constraints()
                WHERE schema_name = 'main' AND constraint_type = 'PRIMARY KEY'
                """
            )
        }
        evidence = {
            table: _duckdb_table_evidence(
                store,
                table=table,
                order_columns=primary_keys.get(table),
            )
            for table in tables
        }
        if "fundamental_versions" in evidence:
            boundary = store.query(
                "SELECT MAX(version_sequence) AS n FROM fundamental_versions"
            )[0]["n"]
            evidence["fundamental_versions"]["additive_boundary"] = {
                "version_sequence_max": int(boundary) if boundary is not None else None,
            }
        if "schema_migrations" in evidence:
            evidence["schema_migrations"]["additive_boundary"] = {
                "names": [
                    str(row["name"])
                    for row in store.query(
                        "SELECT name FROM schema_migrations ORDER BY name"
                    )
                ]
            }
    finally:
        store.close()
    fundamentals = evidence.get("fundamentals")
    if fundamentals is None:
        raise RuntimeError("analytical database has no fundamentals table")
    baseline_payload = {"tables": evidence}
    return {
        "fundamentals": int(fundamentals["rows"]),
        **baseline_payload,
        "baseline_sha256": canonical_payload_sha256(baseline_payload),
    }


def _require_duckdb_rows_unchanged(before: dict[str, Any], path: Path) -> None:
    store = Store(path, read_only=True)
    try:
        current_tables = {
            str(row["table_name"])
            for row in store.query(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'main' AND table_type = 'BASE TABLE'
                """
            )
        }
        for table, expected in before["tables"].items():
            if table in _DUCKDB_ADDITIVE_MIGRATION_TABLES:
                _require_additive_duckdb_rows_unchanged(
                    store,
                    table=table,
                    expected=expected,
                )
                continue
            if table not in current_tables:
                raise RuntimeError(f"analytical upgrade removed table {table}")
            current = _duckdb_table_evidence(
                store,
                table=table,
                columns=[item["name"] for item in expected["columns"]],
                order_columns=list(expected["order_columns"]),
            )
            if current != expected:
                raise RuntimeError(
                    f"analytical upgrade changed pre-existing rows in {table}"
                )
    finally:
        store.close()


def _duckdb_table_evidence(
    store: Store,
    *,
    table: str,
    columns: list[str] | None = None,
    order_columns: list[str] | None = None,
    where_sql: str | None = None,
) -> dict[str, Any]:
    described = store.query(f"DESCRIBE {_quote_identifier(table)}")
    current_columns = [
        {"name": str(row["column_name"]), "type": str(row["column_type"])}
        for row in described
    ]
    selected_names = (
        [item["name"] for item in current_columns]
        if columns is None
        else list(columns)
    )
    current_by_name = {item["name"]: item for item in current_columns}
    try:
        selected_columns = [current_by_name[name] for name in selected_names]
    except KeyError as exc:
        raise RuntimeError(
            f"analytical upgrade removed column {exc.args[0]} from {table}"
        ) from exc
    ordering = list(order_columns or selected_names)
    if any(name not in selected_names for name in ordering):
        ordering = selected_names
    selected_sql = ",".join(_quote_identifier(name) for name in selected_names)
    order_sql = ",".join(_quote_identifier(name) for name in ordering)
    filter_sql = f" WHERE {where_sql}" if where_sql else ""
    cursor = store.execute(
        f"SELECT {selected_sql} FROM {_quote_identifier(table)}"
        f"{filter_sql} ORDER BY {order_sql}"
    )
    digest = hashlib.sha256()
    rows = 0
    while batch := cursor.fetchmany(10_000):
        for row in batch:
            encoded = json.dumps(
                [_canonical_duckdb_value(value) for value in row],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            rows += 1
    return {
        "columns": selected_columns,
        "order_columns": ordering,
        "rows": rows,
        "sha256": digest.hexdigest(),
    }


def _require_additive_duckdb_rows_unchanged(
    store: Store,
    *,
    table: str,
    expected: dict[str, Any],
) -> None:
    boundary = expected.get("additive_boundary")
    if not isinstance(boundary, dict):
        raise RuntimeError(f"analytical baseline lacks additive boundary for {table}")
    expected_projection = {
        key: value for key, value in expected.items() if key != "additive_boundary"
    }
    where_sql: str | None
    if table == "fundamental_versions":
        maximum = boundary.get("version_sequence_max")
        if maximum is None:
            if int(expected_projection["rows"]) != 0:
                raise RuntimeError("fundamental version baseline boundary is invalid")
            return
        where_sql = f"version_sequence <= {int(maximum)}"
    elif table == "schema_migrations":
        names = boundary.get("names")
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise RuntimeError("schema migration baseline boundary is invalid")
        if not names:
            if int(expected_projection["rows"]) != 0:
                raise RuntimeError("schema migration baseline boundary is empty")
            return
        literals = ",".join(_quote_sql_literal(name) for name in names)
        where_sql = f"name IN ({literals})"
    else:  # pragma: no cover - constant and branch must evolve together
        raise RuntimeError(f"unsupported additive migration table {table}")
    current = _duckdb_table_evidence(
        store,
        table=table,
        columns=[item["name"] for item in expected_projection["columns"]],
        order_columns=list(expected_projection["order_columns"]),
        where_sql=where_sql,
    )
    if current != expected_projection:
        raise RuntimeError(
            f"analytical upgrade changed pre-existing rows in {table}"
        )


def _validate_upgraded_database(
    path: Path,
    *,
    expected_fundamentals: int,
) -> dict[str, Any]:
    store = Store(path, read_only=True)
    try:
        store.require_universe_change_activation_schema()
        fundamentals = int(store.query("SELECT COUNT(*) AS n FROM fundamentals")[0]["n"])
        versions = int(
            store.query("SELECT COUNT(*) AS n FROM fundamental_versions")[0]["n"]
        )
        marker = int(
            store.query(
                "SELECT COUNT(*) AS n FROM schema_migrations WHERE name = ?",
                (FUNDAMENTAL_EVIDENCE_VERSIONS_MIGRATION,),
            )[0]["n"]
        )
        hard_failures = [
            str(row["check"])
            for row in store.data_quality_report()
            if row["status"] == "fail"
        ]
        version_evidence = _duckdb_table_evidence(
            store,
            table="fundamental_versions",
            order_columns=["version_sequence"],
        )
        activation_evidence = _universe_change_activation_schema_evidence(store)
    finally:
        store.close()
    if fundamentals != expected_fundamentals:
        raise RuntimeError("fundamental projection row count changed during upgrade")
    if marker != 1 or versions < fundamentals:
        raise RuntimeError("fundamental evidence version migration is incomplete")
    if hard_failures:
        raise RuntimeError(
            "upgraded analytical database has hard data-quality failures: "
            + ", ".join(hard_failures[:5])
        )
    return {
        "fundamentals": fundamentals,
        "fundamental_versions": versions,
        "fundamental_versions_sha256": str(version_evidence["sha256"]),
        "fundamental_evidence_marker": FUNDAMENTAL_EVIDENCE_VERSIONS_MIGRATION,
        "universe_change_activation_schema": activation_evidence,
        "hard_failures": 0,
    }


def _universe_change_activation_schema_evidence(store: Store) -> dict[str, Any]:
    """Return deterministic rehearsal evidence for the activation capability."""

    marker = store.query(
        "SELECT name FROM schema_migrations WHERE name = ?",
        (UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,),
    )
    if marker != [{"name": UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION}]:
        raise RuntimeError("universe-change activation migration marker is incomplete")
    table = _duckdb_table_evidence(
        store,
        table="universe_constituent_change_activations",
        order_columns=["activation_id"],
    )
    constraints = [
        {
            "type": str(row["constraint_type"]),
            "columns": [str(value) for value in row["constraint_column_names"]],
            "expression": str(row["expression"]) if row["expression"] is not None else None,
        }
        for row in store.query(
            """
            SELECT constraint_type, constraint_column_names, expression
            FROM duckdb_constraints()
            WHERE schema_name = 'main'
              AND table_name = 'universe_constituent_change_activations'
            ORDER BY constraint_index
            """
        )
    ]
    contract = {
        "columns": table["columns"],
        "constraints": constraints,
        "marker": UNIVERSE_CONSTITUENT_CHANGE_ACTIVATIONS_MIGRATION,
    }
    return {
        "rows": int(table["rows"]),
        "rows_sha256": str(table["sha256"]),
        "contract_sha256": canonical_payload_sha256(contract),
        **contract,
    }


def _sqlite_baseline(path: Path) -> dict[str, Any]:
    # Use a normal read-only connection: live SQLite may legitimately have a
    # WAL, and immutable mode would ignore it and compare a stale main file.
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise RuntimeError("operations ledger integrity check failed")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise RuntimeError("operations ledger foreign-key check failed")
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        evidence: dict[str, Any] = {}
        for table in tables:
            quoted_table = _quote_identifier(table)
            columns = [
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({quoted_table})")
            ]
            quoted_columns = ",".join(_quote_identifier(column) for column in columns)
            rows = connection.execute(
                f"SELECT {quoted_columns} FROM {quoted_table} ORDER BY rowid"
            ).fetchall()
            canonical_rows = [
                [_canonical_sqlite_value(value) for value in row]
                for row in rows
            ]
            encoded = json.dumps(
                canonical_rows,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
            evidence[table] = {
                "columns": columns,
                "rows": len(rows),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
    baseline_payload = {
        "schema_version": schema_version,
        "tables": evidence,
    }
    return {
        **baseline_payload,
        "baseline_sha256": canonical_payload_sha256(baseline_payload),
    }


def _checkpoint_operations_wal(path: Path) -> None:
    with sqlite3.connect(path, timeout=5.0) as connection:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if checkpoint is None or int(checkpoint[0]) != 0:
        raise RuntimeError("operations ledger WAL checkpoint was busy after migration")


def _require_sqlite_rows_unchanged(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    path: Path,
) -> None:
    before_tables = before["tables"]
    after_tables = after["tables"]
    for table, evidence in before_tables.items():
        current = after_tables.get(table)
        if current is None:
            raise RuntimeError(f"operations upgrade removed table {table}")
        old_columns = list(evidence["columns"])
        new_columns = list(current["columns"])
        if new_columns[: len(old_columns)] != old_columns:
            raise RuntimeError(f"operations upgrade rewrote existing columns in {table}")
        current_projection = _sqlite_table_projection(
            path,
            table=table,
            columns=old_columns,
        )
        if (
            current_projection["rows"] != evidence["rows"]
            or current_projection["sha256"] != evidence["sha256"]
        ):
            raise RuntimeError(f"operations upgrade changed existing rows in {table}")


def _sqlite_table_projection(
    path: Path,
    *,
    table: str,
    columns: list[str],
) -> dict[str, Any]:
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    quoted_table = _quote_identifier(table)
    quoted_columns = ",".join(_quote_identifier(column) for column in columns)
    with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
        rows = connection.execute(
            f"SELECT {quoted_columns} FROM {quoted_table} ORDER BY rowid"
        ).fetchall()
    canonical_rows = [
        [_canonical_sqlite_value(value) for value in row]
        for row in rows
    ]
    encoded = json.dumps(
        canonical_rows,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return {
        "rows": len(rows),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _sqlite_upgrade_projection(baseline: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": int(baseline["schema_version"]),
        "baseline_sha256": str(baseline["baseline_sha256"]),
        "table_rows": {
            table: int(evidence["rows"])
            for table, evidence in sorted(baseline["tables"].items())
        },
    }


def _canonical_duckdb_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return {"float": "nan"}
        if math.isinf(value):
            return {"float": "+inf" if value > 0 else "-inf"}
        return {"float_hex": value.hex()}
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, datetime):
        return {"datetime": value.isoformat()}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, time):
        return {"time": value.isoformat()}
    if isinstance(value, timedelta):
        return {"timedelta_microseconds": int(value.total_seconds() * 1_000_000)}
    if isinstance(value, UUID):
        return {"uuid": str(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"bytes_hex": bytes(value).hex()}
    if isinstance(value, (list, tuple)):
        return [_canonical_duckdb_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _canonical_duckdb_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    raise TypeError(f"unsupported DuckDB evidence value: {type(value).__name__}")


def _canonical_sqlite_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    return value


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _safe_project_root(project_root: Path) -> Path:
    requested = Path(project_root)
    if requested.is_symlink():
        raise ValueError("project root cannot be a symbolic link")
    root = requested.resolve()
    if root == Path(root.anchor) or not root.is_dir():
        raise ValueError("project root is unsafe")
    return root


def _safe_existing_file(root: Path, path: Path, *, label: str) -> Path:
    candidate = Path(path)
    requested = candidate if candidate.is_absolute() else root / candidate
    lexical = Path(os.path.abspath(requested))
    _reject_symlink_path(lexical, root=root, label=label)
    resolved = lexical.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the project root") from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError(f"{label} is missing or unsafe: {resolved}")
    metadata = resolved.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError(f"{label} must be one regular, unaliased file: {resolved}")
    return resolved


def _safe_backup_destination(
    root: Path,
    output: Path | None,
    *,
    timestamp: datetime,
) -> Path:
    requested = (
        root / "backups" / f"pre-upgrade-{timestamp.strftime('%Y%m%dT%H%M%SZ')}"
        if output is None
        else (Path(output) if Path(output).is_absolute() else root / Path(output))
    )
    lexical = Path(os.path.abspath(requested))
    backup_root = root / "backups"
    try:
        lexical.relative_to(backup_root)
    except ValueError as exc:
        raise ValueError(
            "pre-upgrade backup must stay under the project backups directory"
        ) from exc
    _reject_symlink_path(lexical, root=root, label="pre-upgrade backup")
    if lexical.exists() or lexical.is_symlink():
        raise ValueError(f"pre-upgrade backup destination already exists: {lexical}")
    return lexical


def _safe_existing_backup(root: Path, value: Any) -> Path:
    candidate = Path(str(value or ""))
    requested = candidate if candidate.is_absolute() else root / candidate
    lexical = Path(os.path.abspath(requested))
    backup_root = root / "backups"
    try:
        lexical.relative_to(backup_root)
    except ValueError as exc:
        raise ValueError("upgrade backup escapes the project backups directory") from exc
    _reject_symlink_path(lexical, root=root, label="upgrade backup")
    if not lexical.is_dir():
        raise ValueError(f"upgrade backup is missing or unsafe: {lexical}")
    return lexical


def _safe_upgrade_journal(root: Path, value: Path) -> Path:
    candidate = Path(value)
    requested = candidate if candidate.is_absolute() else root / candidate
    lexical = Path(os.path.abspath(requested))
    allowed = root / LOCAL_STATE_UPGRADE_REPORT_DIRECTORY / "attempts"
    try:
        lexical.relative_to(allowed)
    except ValueError as exc:
        raise ValueError("upgrade journal escapes its governed report namespace") from exc
    _reject_symlink_path(lexical, root=root, label="upgrade journal")
    if not lexical.is_dir():
        raise ValueError(f"upgrade journal is missing or unsafe: {lexical}")
    return lexical


def _reject_symlink_path(path: Path, *, root: Path, label: str) -> None:
    current = path
    while True:
        if current.is_symlink():
            raise ValueError(f"{label} path cannot contain symbolic links: {path}")
        if current == root:
            return
        if current == current.parent:
            raise ValueError(f"{label} path escapes the project root")
        current = current.parent

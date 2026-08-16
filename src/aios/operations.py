"""Local operator workflows that hide storage implementation details."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb

BACKUP_KIND = "aios.local-backup"
BACKUP_SCHEMA_VERSION = 1
_LEGACY_020_COLUMN_CONTRACTS = {
    # Original 0.2 release: before immutable fundamental generations.
    "89ac24a0a372355ad67312cf7ea830f6ca6afd4a65ad02bebebd8e3850cfe744": {
        "392f9fd40445ae52ff5dbaff4fa9c034fd93859d830a479d5a42d23b01cfbbd5"
    },
    # Later 0.2 state: after the reviewed additive fundamental migration.
    "ffd24857379a6b00b9cd4dc7d40c082139f5cf5d9c2e8b6f068baab870179d73": {
        "8529aac5e613622f9a1fcc3992f38486ef5dea302b820f6f2c598102ecee4e7c",
        "05692b3a57cdf762d5f3ceae9c121992ac6329d7f777af9385543564891a408d",
    },
    # 2026-08-11: after INDIA_BUILD_PLAN.md phase I1's additive market/venue/
    # security_listings tables (markets, venues, market_profiles,
    # security_listings, trading_sessions, settlement_policies, benchmarks).
    # This dict is not a closed set — every additive schema.py change that
    # a 0.2-simulated test fixture (current schema minus the activation
    # table) would otherwise no longer match needs one more entry here, not
    # a rewrite of the historical ones above.
    "2059c73fb7c0a1cff6d8a58b953897281db3727c8120da459ce2d90aa82a187d": {
        "d15ad5bd16c9e5c5f7c5148fd7411af588a7fea8624fe207d750c568088a8c39"
    },
    # 2026-08-12: after the additive companyfacts_v3_activations receipt table
    # (governed Company Facts v3 activation).
    "88e03ea2f1954da53fc76e916dd1d261b9f1a5564b934d4f79350b5133160d04": {
        "022e32b49f4b23ff0a982b4ed16e737c6b598f8409824e99155f73a7f49192a6"
    },
}
_LEGACY_MACRO_COLUMNS = {
    ("date", "DATE"),
    ("fetched_at", "TIMESTAMP"),
    ("series_id", "VARCHAR"),
    ("source", "VARCHAR"),
    ("unit", "VARCHAR"),
    ("value", "DOUBLE"),
}


@dataclass(frozen=True)
class BackupResult:
    """One completed, manifest-verified local backup."""

    path: Path
    files: int
    bytes: int
    manifest_sha256: str


@dataclass(frozen=True)
class RestoreResult:
    """One completed restore and its automatic rollback snapshot."""

    source: Path
    safety_backup: Path
    files: int
    operations_rescan_required: bool = False
    operations_incident_id: str | None = None


@dataclass(frozen=True)
class RestoreDrillResult:
    """Evidence that a backup restored and validated outside the live project."""

    source: Path
    files: int
    bytes: int
    manifest_sha256: str
    raw_payloads: int
    replayed_snapshots: int
    hard_failures: int


@dataclass(frozen=True)
class _DatabaseCompatibility:
    version: str
    schema_contract_sha256: str


def _database_compatibility_version(
    database_path: Path,
) -> _DatabaseCompatibility:
    """Classify the source database without running an application migration."""

    try:
        connection = duckdb.connect(str(database_path), read_only=True)
    except duckdb.Error:
        raise ValueError("backup source is not a readable DuckDB database") from None
    try:
        tables = {str(row[0]) for row in connection.execute("SHOW TABLES").fetchall()}
        table_exists = "universe_constituent_change_activations" in tables
        marker_exists = False
        if "schema_migrations" in tables:
            marker_exists = bool(
                connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE name = ?",
                    ("universe_constituent_change_activations_v1",),
                ).fetchone()
            )
        if not table_exists and not marker_exists:
            _require_legacy_020_schema_contract(connection, tables=tables)
            return _DatabaseCompatibility(
                version="0.2.0",
                schema_contract_sha256=_database_schema_contract_sha256(connection),
            )
        if table_exists != marker_exists:
            raise ValueError(
                "backup source has an incomplete universe-change activation capability"
            )
        schema_contract_sha256 = _database_schema_contract_sha256(connection)
    finally:
        connection.close()
    from aios.storage.store import Store

    store = Store(database_path, read_only=True)
    try:
        store.require_universe_change_activation_schema()
    except RuntimeError as exc:
        raise ValueError(
            "backup source has an invalid universe-change activation capability"
        ) from exc
    finally:
        store.close()
    return _DatabaseCompatibility(
        version="0.3.0",
        schema_contract_sha256=schema_contract_sha256,
    )


def _require_legacy_020_schema_contract(
    connection: duckdb.DuckDBPyConnection,
    *,
    tables: set[str],
) -> None:
    """Accept only one of the reviewed, frozen AIOS 0.2 schema variants."""

    core_tables = tables - {"macro_legacy"}
    columns = connection.execute(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'main' AND table_name <> 'macro_legacy'
        ORDER BY table_name, column_name
        """
    ).fetchall()
    column_contract = _canonical_sha256(columns)
    constraints = connection.execute(
        """
        SELECT table_name, constraint_type, constraint_column_names, expression
        FROM duckdb_constraints()
        WHERE schema_name = 'main' AND table_name <> 'macro_legacy'
        """
    ).fetchall()
    normalized_constraints = sorted(
        (
            str(row[0]),
            str(row[1]),
            tuple(str(value) for value in (row[2] or [])),
            "".join(str(row[3] or "").split()).casefold(),
        )
        for row in constraints
    )
    constraint_contract = _canonical_sha256(normalized_constraints)
    allowed_constraints = _LEGACY_020_COLUMN_CONTRACTS.get(column_contract)
    if allowed_constraints is None or constraint_contract not in allowed_constraints:
        raise ValueError(
            "backup source is not a reviewed AIOS 0.2 database schema"
        )
    expected_core_tables = {str(row[0]) for row in columns}
    if core_tables != expected_core_tables:
        raise ValueError(
            "backup source is not a reviewed AIOS 0.2 database schema"
        )
    if "macro_legacy" in tables:
        macro_columns = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'main' AND table_name = 'macro_legacy'
                """
            ).fetchall()
        }
        if macro_columns != _LEGACY_MACRO_COLUMNS:
            raise ValueError("backup source has an invalid legacy macro quarantine")


def _database_schema_contract_sha256(
    connection: duckdb.DuckDBPyConnection,
) -> str:
    columns = connection.execute(
        """
        SELECT table_name, ordinal_position, column_name, data_type,
               is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'main'
        ORDER BY table_name, ordinal_position
        """
    ).fetchall()
    constraints = connection.execute(
        """
        SELECT table_name, constraint_type, constraint_column_names, expression
        FROM duckdb_constraints()
        WHERE schema_name = 'main'
        """
    ).fetchall()
    normalized_constraints = sorted(
        (
            str(row[0]),
            str(row[1]),
            tuple(str(value) for value in (row[2] or [])),
            "".join(str(row[3] or "").split()).casefold(),
        )
        for row in constraints
    )
    return _canonical_sha256(
        {
            "columns": [list(row) for row in columns],
            "constraints": normalized_constraints,
        }
    )


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_local_backup(
    project_root: Path,
    database_path: Path,
    *,
    operations_database_path: Path | None = None,
    output: Path | None = None,
    application_version: str,
    now: datetime | None = None,
) -> BackupResult:
    """Copy analytical, paper, and incident evidence into a verified snapshot."""
    root = Path(project_root).resolve()
    database = _resolve_under_root(root, database_path)
    if database.is_symlink() or not database.is_file():
        raise ValueError(f"database does not exist: {database}")
    database_compatibility = _database_compatibility_version(database)
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    generated_suffix = "" if now is not None else f"-{uuid4().hex[:8]}"
    destination = (
        Path(output).resolve()
        if output is not None
        else root
        / "backups"
        / f"aios-{timestamp.strftime('%Y%m%dT%H%M%SZ')}{generated_suffix}"
    )
    if destination.exists():
        raise ValueError(f"backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    sources: list[tuple[Path, Path, bool]] = [
        (database, Path("database") / database.name, False)
    ]
    paper_root = root / "data" / "paper"
    if paper_root.exists():
        if paper_root.is_symlink() or not paper_root.is_dir():
            raise ValueError(f"paper backup source must be a regular directory: {paper_root}")
        for source in sorted(paper_root.rglob("*.json")):
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"paper backup source must be a regular file: {source}")
            sources.append((source, Path("paper") / source.relative_to(paper_root), False))
    if operations_database_path is not None:
        operations_database = _resolve_under_root(root, operations_database_path)
        if operations_database.exists():
            if operations_database.is_symlink() or not operations_database.is_file():
                raise ValueError(
                    f"operations backup source must be a regular file: {operations_database}"
                )
            sources.append(
                (
                    operations_database,
                    Path("operations") / operations_database.name,
                    True,
                )
            )
    raw_root = root / "data" / "raw"
    if raw_root.exists():
        if raw_root.is_symlink() or not raw_root.is_dir():
            raise ValueError(f"raw snapshot source must be a regular directory: {raw_root}")
        for source in sorted(raw_root.rglob("*")):
            if source.is_symlink():
                raise ValueError(f"raw snapshot source cannot be a symlink: {source}")
            if source.is_file():
                sources.append((source, Path("raw") / source.relative_to(raw_root), False))

    temporary = Path(
        tempfile.mkdtemp(prefix=".aios-backup-", dir=str(destination.parent))
    )
    try:
        manifest_files: list[dict[str, Any]] = []
        total_bytes = 0
        for source, relative, sqlite_snapshot in sources:
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if sqlite_snapshot:
                _copy_sqlite_snapshot(source, target)
            else:
                shutil.copy2(source, target)
            size = target.stat().st_size
            total_bytes += size
            manifest_files.append(
                {
                    "path": relative.as_posix(),
                    "bytes": size,
                    "sha256": _file_sha256(target),
                }
            )
        manifest = {
            "document_kind": BACKUP_KIND,
            "schema_version": BACKUP_SCHEMA_VERSION,
            "application_version": application_version,
            "database_compatibility_version": database_compatibility.version,
            "database_schema_contract_sha256": (
                database_compatibility.schema_contract_sha256
            ),
            "created_at": timestamp.isoformat().replace("+00:00", "Z"),
            "database_file": (Path("database") / database.name).as_posix(),
            "files": manifest_files,
            "excludes": [".env", "logs", "backtest artifacts", "mutable provider caches"],
            "restore_policy": {
                "operations": (
                    "audit-only; never rolled backward during analytical restore; "
                    "marked for a fresh anomaly scan"
                ),
                "raw": "immutable merge; newer content-addressed payloads are retained",
            },
        }
        encoded = _canonical_json(manifest)
        manifest_path = temporary / "manifest.json"
        manifest_path.write_bytes(encoded)
        manifest_sha256 = hashlib.sha256(encoded).hexdigest()
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    result = verify_local_backup(destination)
    if result.manifest_sha256 != manifest_sha256:
        raise RuntimeError("backup manifest changed during final verification")
    return result


def verify_local_backup(path: Path) -> BackupResult:
    """Verify backup shape, hashes, and any independent operations ledger."""
    requested = Path(path)
    if requested.is_symlink():
        raise ValueError("backup directory cannot be a symbolic link")
    root = requested.resolve()
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"backup manifest does not exist: {manifest_path}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"backup manifest is unreadable: {manifest_path}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("document_kind") != BACKUP_KIND
        or manifest.get("schema_version") != BACKUP_SCHEMA_VERSION
        or not isinstance(manifest.get("files"), list)
    ):
        raise ValueError("unsupported backup manifest")
    compatibility = manifest.get("database_compatibility_version")
    if compatibility is not None and (
        not isinstance(compatibility, str) or not compatibility.strip()
    ):
        raise ValueError("backup manifest has an invalid database compatibility version")
    schema_contract = manifest.get("database_schema_contract_sha256")
    if schema_contract is not None and (
        not isinstance(schema_contract, str)
        or len(schema_contract) != 64
        or any(character not in "0123456789abcdef" for character in schema_contract)
    ):
        raise ValueError("backup manifest has an invalid database schema contract")

    total_bytes = 0
    seen: set[str] = set()
    operations_files: list[Path] = []
    database_file = str(manifest.get("database_file") or "")
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise ValueError("backup manifest has an invalid file entry")
        relative = _safe_relative_path(item.get("path"))
        label = relative.as_posix()
        if label in seen:
            raise ValueError(f"backup manifest repeats {label}")
        seen.add(label)
        target = root / relative
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"backup file is missing or unsafe: {label}")
        expected_size = item.get("bytes")
        expected_hash = item.get("sha256")
        if (
            not isinstance(expected_size, int)
            or expected_size < 0
            or not isinstance(expected_hash, str)
            or len(expected_hash) != 64
        ):
            raise ValueError(f"backup metadata is invalid for {label}")
        actual_size = target.stat().st_size
        if actual_size != expected_size or _file_sha256(target) != expected_hash:
            raise ValueError(f"backup checksum mismatch: {label}")
        total_bytes += actual_size
        if relative.parts[0] == "operations":
            operations_files.append(target)
    if not database_file or database_file not in seen:
        raise ValueError("backup manifest does not identify its database file")
    database_relative = _safe_relative_path(database_file)
    if database_relative.parts[0] != "database":
        raise ValueError("backup manifest database file is outside database/")
    actual_files: set[str] = set()
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            raise ValueError("backup contains a symbolic link")
        if candidate.is_file():
            actual_files.add(candidate.relative_to(root).as_posix())
    expected_files = seen | {"manifest.json"}
    if actual_files != expected_files:
        unexpected = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        detail = unexpected[0] if unexpected else missing[0]
        raise ValueError(f"backup contains an unmanifested or missing file: {detail}")
    if len(operations_files) > 1:
        raise ValueError("backup contains more than one operations ledger")
    if operations_files:
        _verify_operations_ledger(operations_files[0])

    derived = _database_compatibility_version(root / database_relative)
    if compatibility is not None and compatibility != derived.version:
        raise ValueError(
            "backup manifest database compatibility does not match its DuckDB"
        )
    if (
        schema_contract is not None
        and schema_contract != derived.schema_contract_sha256
    ):
        raise ValueError(
            "backup manifest database schema contract does not match its DuckDB"
        )

    encoded = _canonical_json(manifest)
    return BackupResult(
        path=root,
        files=len(seen),
        bytes=total_bytes,
        manifest_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def restore_local_backup(
    backup: Path,
    project_root: Path,
    database_path: Path,
    *,
    operations_database_path: Path | None = None,
    application_version: str,
    confirm: bool = False,
    now: datetime | None = None,
) -> RestoreResult:
    """Restore one semantically valid snapshot after a safety backup.

    The complete candidate is copied into an isolated project-shaped staging
    tree and opened through the same read paths used by AIOS before any live
    database or paper path is replaced. A checksum-consistent but corrupt,
    incompatible, or internally inconsistent backup therefore cannot become
    live state.
    """
    if not confirm:
        raise ValueError("restore requires explicit confirmation")
    source_result = verify_local_backup(backup)
    source = source_result.path
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    _validate_restore_manifest_compatibility(
        manifest,
        application_version=application_version,
    )
    database_relative = _safe_relative_path(manifest["database_file"])
    source_database_compatibility = _database_compatibility_version(
        source / database_relative
    )
    allowed_files: list[Path] = []
    for item in manifest["files"]:
        relative = _safe_relative_path(item["path"])
        if relative != database_relative and relative.parts[0] not in {
            "paper",
            "operations",
            "raw",
        }:
            raise ValueError(f"backup contains an unsupported restore path: {relative}")
        allowed_files.append(relative)

    root = Path(project_root).resolve()
    database_target = _resolve_under_root(root, database_path)
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    data_root = root / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".aios-restore-", dir=str(data_root)))
    old_paper = data_root / f".paper-before-restore-{uuid4().hex}"
    paper_target = data_root / "paper"
    paper_swapped = False
    safety: BackupResult | None = None
    operations_incident_id = None
    try:
        staged_database = staging / "data" / database_target.name
        staged_database.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / database_relative, staged_database)

        paper_files = [relative for relative in allowed_files if relative.parts[0] == "paper"]
        staged_paper = staging / "data" / "paper"
        for relative in paper_files:
            target = staged_paper / Path(*relative.parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)

        raw_files = [relative for relative in allowed_files if relative.parts[0] == "raw"]
        for relative in raw_files:
            staged_target = staging / "data" / relative
            staged_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, staged_target)

        _validate_staged_restore_candidate(
            staging,
            staged_database,
            paper_files=paper_files,
            source_database_compatibility=source_database_compatibility,
        )

        # Detect every immutable-merge conflict before taking the safety
        # snapshot or changing any live restore target.
        for relative in raw_files:
            live_target = data_root / relative
            source_file = source / relative
            if live_target.exists():
                if live_target.is_symlink() or not live_target.is_file():
                    raise ValueError(f"live raw snapshot path is unsafe: {live_target}")
                if _file_sha256(live_target) != _file_sha256(source_file):
                    raise ValueError(f"immutable raw snapshot conflicts during restore: {relative}")

        safety_destination = (
            root
            / "backups"
            / f"pre-restore-{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        )
        safety = create_local_backup(
            root,
            database_target,
            operations_database_path=operations_database_path,
            output=safety_destination,
            application_version=application_version,
            now=timestamp,
        )
        if operations_database_path is not None:
            operations_database = _resolve_under_root(root, operations_database_path)
            operations_incident_id = _mark_operations_evidence_stale(
                operations_database,
                backup_manifest_sha256=source_result.manifest_sha256,
                now=timestamp,
            )

        # Raw evidence is immutable and additive. Publish only after every
        # candidate validation and conflict check has succeeded.
        for relative in raw_files:
            live_target = data_root / relative
            if not live_target.exists():
                live_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / relative, live_target)

        if paper_target.exists():
            os.replace(paper_target, old_paper)
            paper_swapped = True
        if paper_files:
            os.replace(staged_paper, paper_target)
            paper_swapped = True

        database_target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_database, database_target)
    except Exception:
        if paper_swapped:
            if paper_target.exists():
                failed_paper = data_root / f".paper-failed-restore-{uuid4().hex}"
                os.replace(paper_target, failed_paper)
                shutil.rmtree(failed_paper, ignore_errors=True)
            if old_paper.exists():
                os.replace(old_paper, paper_target)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if old_paper.exists():
        shutil.rmtree(old_paper)
    if safety is None:  # pragma: no cover - defensive invariant
        raise RuntimeError("restore completed without a pre-restore safety backup")

    return RestoreResult(
        source=source,
        safety_backup=safety.path,
        files=source_result.files,
        operations_rescan_required=operations_incident_id is not None,
        operations_incident_id=operations_incident_id,
    )


def _validate_restore_manifest_compatibility(
    manifest: dict[str, Any],
    *,
    application_version: str,
) -> None:
    """Refuse unsupported binary/schema restore paths.

    Version 0.3.0 adds only reviewed additive DuckDB capabilities.  Its staged
    restore validator can therefore migrate a verified 0.2.0 backup before any
    live swap.  Every other cross-version pair remains fail-closed.
    """
    backup_version = manifest.get("application_version")
    if not isinstance(backup_version, str) or not backup_version.strip():
        raise ValueError("backup manifest has no application version")
    supported_additive_upgrades = {("0.2.0", "0.3.0")}
    if backup_version != application_version and (
        backup_version,
        application_version,
    ) not in supported_additive_upgrades:
        raise ValueError(
            "backup application version is incompatible with this AIOS installation: "
            f"{backup_version!r} != {application_version!r}"
        )


def _validate_staged_restore_candidate(
    staging_root: Path,
    database_path: Path,
    *,
    paper_files: list[Path],
    source_database_compatibility: _DatabaseCompatibility,
) -> None:
    """Open and semantically validate every restorable candidate payload."""
    from aios.raw_snapshots import verify_raw_snapshots
    from aios.storage.store import Store

    store: Store | None = None
    try:
        staged_compatibility = _database_compatibility_version(database_path)
        if staged_compatibility != source_database_compatibility:
            raise ValueError(
                "staged DuckDB compatibility differs from the verified backup"
            )
        baseline = None
        if staged_compatibility.version == "0.2.0":
            from aios.local_state_upgrade import _database_baseline

            baseline = _database_baseline(database_path)
        else:
            capability = Store(database_path, read_only=True)
            try:
                capability.require_universe_change_activation_schema()
            finally:
                capability.close()
        # The candidate is already an isolated copy. Opening it writable lets
        # the current release rehearse/apply supported additive migrations
        # before any live path or safety backup is touched. The source backup
        # remains byte-identical and independently verifiable.
        store = Store(
            database_path,
            allow_schema_upgrade=staged_compatibility.version == "0.2.0",
        )
        store.close()
        store = None
        if baseline is not None:
            from aios.local_state_upgrade import _require_duckdb_rows_unchanged

            _require_duckdb_rows_unchanged(baseline, database_path)
        store = Store(database_path, read_only=True)
        hard_failures = [
            row for row in store.data_quality_report() if row["status"] == "fail"
        ]
        if hard_failures:
            names = ", ".join(str(row["check"]) for row in hard_failures[:5])
            raise ValueError(
                "backup DuckDB has "
                f"{len(hard_failures)} hard data-quality failure(s): {names}"
            )
        _validate_staged_paper_state(
            staging_root,
            paper_files=paper_files,
            store=store,
        )
        verify_raw_snapshots(
            store=store,
            project_root=staging_root,
        )
    except RuntimeError as exc:
        raise ValueError(
            "backup DuckDB has a hard data-quality failure during staged migration"
        ) from exc
    except duckdb.Error as exc:
        raise ValueError("backup DuckDB cannot be opened and validated") from exc
    finally:
        if store is not None:
            store.close()


def _validate_staged_paper_state(
    staging_root: Path,
    *,
    paper_files: list[Path],
    store: Any,
) -> None:
    """Validate paper envelopes and active forward-document references."""
    from aios.forward import (
        DEFAULT_FORWARD_RELATIVE_PATH,
        FORWARD_DOCUMENT_KIND,
        read_forward_trial,
    )
    from aios.paper import (
        ACCOUNT_DOCUMENT_KIND,
        ACCOUNT_SCHEMA_VERSION,
        DEFAULT_ACCOUNT_RELATIVE_PATH,
        PROPOSAL_DOCUMENT_KIND,
        PROPOSAL_SCHEMA_VERSION,
        paper_account_summary,
        read_paper_document,
    )
    from aios.rollover_journal import ROLLOVER_ATTEMPT_DOCUMENT_KIND

    documents: dict[Path, Any] = {}
    forward_documents: dict[Path, Any] = {}
    attempt_files: list[Path] = []
    for backup_relative in paper_files:
        candidate_relative = Path("data") / backup_relative
        candidate = staging_root / candidate_relative
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"backup paper JSON is unreadable: {backup_relative.as_posix()}"
            ) from exc
        if not isinstance(raw, dict):
            raise ValueError(
                f"backup paper JSON is not an object: {backup_relative.as_posix()}"
            )
        kind = raw.get("document_kind")
        if kind == ROLLOVER_ATTEMPT_DOCUMENT_KIND:
            attempt_files.append(candidate_relative)
            continue
        if kind == FORWARD_DOCUMENT_KIND:
            forward_documents[candidate_relative] = read_forward_trial(candidate)
            continue
        if kind not in {ACCOUNT_DOCUMENT_KIND, PROPOSAL_DOCUMENT_KIND}:
            raise ValueError(
                "backup paper document has an unsupported kind: "
                f"{backup_relative.as_posix()}"
            )
        document = read_paper_document(candidate, expected_kind=kind)
        if kind == ACCOUNT_DOCUMENT_KIND:
            if document.payload.get("account_schema_version") != ACCOUNT_SCHEMA_VERSION:
                raise ValueError("backup paper account has an unsupported schema")
        elif document.payload.get("proposal_schema_version") != PROPOSAL_SCHEMA_VERSION:
            raise ValueError("backup paper proposal has an unsupported schema")
        documents[candidate_relative] = document

    account = documents.get(DEFAULT_ACCOUNT_RELATIVE_PATH)
    if account is not None:
        paper_account_summary(staging_root / DEFAULT_ACCOUNT_RELATIVE_PATH, store)

    for relative, document in documents.items():
        if document.kind != PROPOSAL_DOCUMENT_KIND:
            continue
        payload = document.payload
        if not isinstance(payload.get("targets"), list) or not payload.get("proposal_id"):
            raise ValueError(
                f"backup paper proposal payload is incomplete: {relative.as_posix()}"
            )

    active = forward_documents.get(DEFAULT_FORWARD_RELATIVE_PATH)
    if active is None:
        _validate_staged_rollover_attempts(
            staging_root,
            attempt_files=attempt_files,
            forward_documents=forward_documents,
            paper_documents=documents,
            active_forward=None,
        )
        return
    if active.payload.get("status") != "active":
        raise ValueError("backup active forward trial is not active")
    if account is None:
        raise ValueError("backup active forward trial has no paper account")
    if active.payload.get("account_id") != account.payload.get("account_id"):
        raise ValueError("backup active forward trial references another paper account")

    records = active.payload.get("proposals")
    if not isinstance(records, list):
        raise ValueError("backup active forward trial proposal registry is invalid")
    registered: list[Any] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("backup active forward trial has an invalid proposal record")
        relative = _safe_relative_path(record.get("path"))
        if relative.parts[:2] != ("data", "paper"):
            raise ValueError("backup forward proposal path is outside paper state")
        proposal = documents.get(relative)
        if proposal is None or proposal.kind != PROPOSAL_DOCUMENT_KIND:
            raise ValueError(
                f"backup forward proposal is missing: {relative.as_posix()}"
            )
        if (
            record.get("proposal_id") != proposal.payload.get("proposal_id")
            or record.get("payload_sha256") != proposal.payload_sha256
            or record.get("decision_date") != proposal.payload.get("decision_date")
            or record.get("generated_at") != proposal.payload.get("generated_at")
            or record.get("status") != proposal.payload.get("status")
        ):
            raise ValueError(
                f"backup forward proposal identity is inconsistent: {relative.as_posix()}"
            )
        if proposal.payload.get("account_id") != account.payload.get("account_id"):
            raise ValueError(
                f"backup proposal references another paper account: {relative.as_posix()}"
            )
        registered.append(proposal)

    if registered:
        latest = max(
            registered,
            key=lambda item: (
                str(item.payload.get("decision_date") or ""),
                str(item.payload.get("generated_at") or ""),
                str(item.payload.get("proposal_id") or ""),
            ),
        )
        executed = {
            str(row.get("proposal_id"))
            for row in account.payload.get("executions", [])
            if isinstance(row, dict) and row.get("proposal_id")
        }
        if (
            str(latest.payload.get("proposal_id")) not in executed
            and latest.payload.get("account_payload_sha256") != account.payload_sha256
        ):
            raise ValueError(
                "backup active proposal is not bound to the restored paper account"
            )

    _validate_staged_rollover_attempts(
        staging_root,
        attempt_files=attempt_files,
        forward_documents=forward_documents,
        paper_documents=documents,
        active_forward=active,
    )


def _validate_staged_rollover_attempts(
    staging_root: Path,
    *,
    attempt_files: list[Path],
    forward_documents: dict[Path, Any],
    paper_documents: dict[Path, Any],
    active_forward: Any | None,
) -> None:
    """Reconcile terminal rollover evidence with immutable staged artifacts."""

    from aios.forward_rollover import (
        ROLLOVER_LINEAGE_SCHEMA_VERSION,
        ROLLOVER_PLAN_SCHEMA_VERSION,
    )
    from aios.paper import canonical_payload_sha256
    from aios.rollover_journal import validate_attempt_directory

    if not attempt_files:
        if (
            active_forward is not None
            and active_forward.payload.get("rollover_lineage") is not None
        ):
            raise ValueError("backup active rollover successor has no transaction journal")
        return

    directories = sorted({(staging_root / path).parent for path in attempt_files})
    states = [validate_attempt_directory(directory) for directory in directories]
    if any(not state.terminal for state in states):
        raise ValueError("backup contains an incomplete rollover transaction")

    verified_by_identity: dict[tuple[str, str], Any] = {}
    for state in states:
        prepared = state.prepared.payload
        plan = prepared["plan"]
        prepared_state = prepared["state"]
        terminal = state.latest.payload["state"]
        if (
            plan.get("plan_schema_version") != ROLLOVER_PLAN_SCHEMA_VERSION
            or plan.get("operation") != "prospective_forward_rollover"
            or plan.get("rollover_key") != prepared["rollover_key"]
            or canonical_payload_sha256(plan) != prepared["plan_sha256"]
        ):
            raise ValueError("backup rollover journal has an invalid plan binding")
        predecessor = plan.get("predecessor")
        account = plan.get("account")
        successor = plan.get("successor")
        transaction = plan.get("transaction")
        if not all(
            isinstance(value, dict)
            for value in (predecessor, account, successor, transaction)
        ):
            raise ValueError("backup rollover plan is incomplete")

        expected_paths = {
            "active_trial": transaction.get("active_trial_path"),
            "account": account.get("account_path"),
            "predecessor_proposal": predecessor.get("proposal_path"),
            "trial_archive": predecessor.get("trial_archive_path"),
            "proposal_archive": predecessor.get("proposal_archive_path"),
            "successor_proposal": successor.get("proposal_path"),
            "staged_trial": transaction.get("staged_trial_path"),
        }
        if prepared_state.get("paths") != expected_paths:
            raise ValueError("backup rollover journal path evidence is inconsistent")
        if (
            terminal.get("account_file_sha256") != account.get("account_file_sha256")
            or terminal.get("account_execution_registry_sha256")
            != account.get("execution_registry_sha256")
            or terminal.get("backup_manifest_sha256")
            != prepared["backup"].get("manifest_sha256")
        ):
            raise ValueError("backup rollover terminal account evidence is inconsistent")

        candidate_trial_hash = prepared_state["successor_trial_file_sha256"]
        candidate_trial_payload_hash = prepared_state[
            "successor_trial_payload_sha256"
        ]
        candidate_trial_id = prepared_state["successor_trial_id"]
        candidate_proposal_hash = prepared_state[
            "successor_proposal_payload_sha256"
        ]
        if state.latest.phase == "recovered_rolled_back":
            if any(
                document.payload_sha256 == candidate_trial_payload_hash
                or document.payload.get("trial_id") == candidate_trial_id
                for document in forward_documents.values()
            ):
                raise ValueError(
                    "backup rolled-back rollover successor remains authoritative"
                )
            if any(
                document.payload_sha256 == candidate_proposal_hash
                for document in paper_documents.values()
            ):
                raise ValueError(
                    "backup rolled-back rollover proposal remains authoritative"
                )
            continue

        if state.latest.phase != "verified":
            raise ValueError("backup rollover journal has an unsupported terminal phase")
        if (
            terminal.get("authority") != "successor"
            or terminal.get("active_trial_file_sha256") != candidate_trial_hash
            or terminal.get("active_trial_payload_sha256")
            != candidate_trial_payload_hash
            or terminal.get("paper_fill_recorded") is not False
            or terminal.get("broker_order_sent") is not False
        ):
            raise ValueError("backup verified rollover evidence is inconsistent")

        matching_trials = [
            (relative, document)
            for relative, document in forward_documents.items()
            if document.payload_sha256 == candidate_trial_payload_hash
            and document.payload.get("trial_id") == candidate_trial_id
            and _file_sha256(staging_root / relative) == candidate_trial_hash
        ]
        if len(matching_trials) != 1:
            raise ValueError(
                "backup verified rollover successor trial is missing or duplicated"
            )
        _relative, successor_trial = matching_trials[0]
        lineage = successor_trial.payload.get("rollover_lineage")
        if (
            lineage != prepared_state.get("lineage")
            or not isinstance(lineage, dict)
            or lineage.get("schema_version") != ROLLOVER_LINEAGE_SCHEMA_VERSION
            or lineage.get("plan_sha256") != prepared["plan_sha256"]
            or lineage.get("attempt_id") != prepared["attempt_id"]
            or lineage.get("backup_manifest_sha256")
            != prepared["backup"].get("manifest_sha256")
        ):
            raise ValueError("backup rollover successor lineage is inconsistent")

        successor_path = _safe_relative_path(successor["proposal_path"])
        successor_proposal = paper_documents.get(successor_path)
        if (
            successor_proposal is None
            or successor_proposal.payload_sha256 != candidate_proposal_hash
            or _file_sha256(staging_root / successor_path)
            != prepared_state["output_file_sha256"]["successor_proposal"]
        ):
            raise ValueError("backup rollover successor proposal is inconsistent")
        for label, relative_value, expected_hash in (
            (
                "predecessor trial archive",
                predecessor["trial_archive_path"],
                predecessor["trial_file_sha256"],
            ),
            (
                "predecessor proposal archive",
                predecessor["proposal_archive_path"],
                predecessor["proposal_file_sha256"],
            ),
        ):
            relative = _safe_relative_path(relative_value)
            candidate = staging_root / relative
            if not candidate.is_file() or _file_sha256(candidate) != expected_hash:
                raise ValueError(f"backup {label} is inconsistent")

        identity = (str(prepared["plan_sha256"]), str(prepared["attempt_id"]))
        if identity in verified_by_identity:
            raise ValueError("backup repeats verified rollover identity")
        verified_by_identity[identity] = successor_trial

    if active_forward is None:
        return
    active_lineage = active_forward.payload.get("rollover_lineage")
    if active_lineage is None:
        return
    if not isinstance(active_lineage, dict):
        raise ValueError("backup active rollover lineage is invalid")
    active_identity = (
        str(active_lineage.get("plan_sha256") or ""),
        str(active_lineage.get("attempt_id") or ""),
    )
    if active_identity not in verified_by_identity:
        raise ValueError("backup active rollover successor has no verified journal")


def drill_local_backup(
    backup: Path,
    *,
    application_version: str,
    scratch_parent: Path | None = None,
) -> RestoreDrillResult:
    """Exercise the real restore path in an isolated disposable project.

    The live database, paper state, incident ledger, and raw directory are
    never opened for writing. The drill verifies the source manifest, performs
    the confirmed restore (including its safety-backup branch), opens the
    restored DuckDB, runs every hard data-quality check, and replays all
    immutable provider evidence before deleting the scratch project.
    """
    source = verify_local_backup(backup)
    manifest = json.loads((source.path / "manifest.json").read_text(encoding="utf-8"))
    database_relative = _safe_relative_path(manifest["database_file"])
    parent = Path(scratch_parent).resolve() if scratch_parent is not None else None
    if parent is not None and (parent.is_symlink() or not parent.is_dir()):
        raise ValueError(f"restore drill parent is unsafe: {parent}")

    with tempfile.TemporaryDirectory(
        prefix="aios-restore-drill-",
        dir=str(parent) if parent is not None else None,
    ) as temporary:
        scratch = Path(temporary).resolve()
        database_target = scratch / "data" / database_relative.name
        database_target.parent.mkdir(parents=True, exist_ok=True)
        # Seed only the analytical database so the actual restore function can
        # exercise its mandatory pre-restore safety-backup path.
        shutil.copy2(source.path / database_relative, database_target)
        restore_local_backup(
            source.path,
            scratch,
            Path("data") / database_relative.name,
            application_version=application_version,
            confirm=True,
        )

        from aios.raw_snapshots import verify_raw_snapshots
        from aios.storage.store import Store

        store = Store(database_target, read_only=True)
        try:
            hard_failures = [
                row for row in store.data_quality_report() if row["status"] == "fail"
            ]
            raw = verify_raw_snapshots(
                store=store,
                project_root=scratch,
            )
        finally:
            store.close()
        if hard_failures:
            names = ", ".join(str(row["check"]) for row in hard_failures[:5])
            raise ValueError(
                f"restored database has {len(hard_failures)} hard validation "
                f"failure(s): {names}"
            )
        return RestoreDrillResult(
            source=source.path,
            files=source.files,
            bytes=source.bytes,
            manifest_sha256=source.manifest_sha256,
            raw_payloads=raw.payloads,
            replayed_snapshots=raw.replayed_snapshots,
            hard_failures=0,
        )


def _resolve_under_root(project_root: Path, path: Path) -> Path:
    return Path(path).resolve() if Path(path).is_absolute() else (project_root / path).resolve()


def _safe_relative_path(value: Any) -> Path:
    candidate = Path(str(value or ""))
    if not candidate.parts or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("backup manifest contains an unsafe path")
    return candidate


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_sqlite_snapshot(source: Path, target: Path) -> None:
    """Use SQLite's online backup API so WAL-backed incident data is consistent."""
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with (
        sqlite3.connect(source_uri, uri=True, timeout=5.0) as source_connection,
        sqlite3.connect(target, timeout=5.0) as target_connection,
    ):
        source_connection.backup(target_connection)
        target_connection.commit()
        checkpoint = target_connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise RuntimeError("operations backup SQLite checkpoint was busy")
        journal_mode = str(
            target_connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
        ).lower()
        if journal_mode != "delete":
            raise RuntimeError(
                "operations backup SQLite snapshot is not self-contained"
            )


def _verify_operations_ledger(path: Path) -> None:
    """Fail closed on corrupt or internally inconsistent operations evidence."""
    from aios.alerts import (
        ALERT_SCHEMA_VERSION,
        REQUIRED_INCIDENT_TRIGGERS,
        classify_incident_resolution,
        verify_anomaly_case_evidence,
        verify_incident_event_evidence,
    )

    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            integrity = [
                str(row[0])
                for row in connection.execute("PRAGMA integrity_check").fetchall()
            ]
            if integrity != ["ok"]:
                detail = integrity[0] if integrity else "no result"
                raise ValueError(
                    f"operations backup SQLite integrity check failed: {detail}"
                )
            foreign_key_failures = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_failures:
                raise ValueError(
                    "operations backup foreign-key check failed: "
                    f"{len(foreign_key_failures)} violation(s)"
                )
            schema_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type = 'table'"
                ).fetchall()
            }
            if "incidents" not in tables:
                raise ValueError("operations backup schema is missing incidents")
            schema_objects = {
                (str(row["type"]), str(row["name"]))
                for row in connection.execute(
                    """
                    SELECT type, name
                    FROM sqlite_schema
                    """
                ).fetchall()
            }
            anomaly_objects = {
                item for item in schema_objects if item[1].startswith("anomaly_")
            }
            if schema_version == 0:
                if anomaly_objects:
                    raise ValueError(
                        "operations backup has anomaly evidence without a "
                        "recognized schema version"
                    )
                return
            if schema_version < 0:
                raise ValueError("operations backup has no recognized schema version")
            if schema_version > ALERT_SCHEMA_VERSION:
                raise ValueError(
                    "operations backup schema is newer than this AIOS installation"
                )
            if schema_version < ALERT_SCHEMA_VERSION:
                return

            required_objects = {
                ("table", "anomaly_scans"),
                ("table", "anomaly_cases"),
                ("table", "anomaly_case_events"),
                ("index", "anomaly_case_scan_observation_unique"),
                ("index", "anomaly_case_events_sequence_unique"),
                ("trigger", "anomaly_scans_no_update"),
                ("trigger", "anomaly_scans_no_delete"),
                ("trigger", "anomaly_case_events_no_update"),
                ("trigger", "anomaly_case_events_no_delete"),
                ("trigger", "anomaly_case_events_sequence_required"),
            }
            missing_objects = sorted(required_objects - anomaly_objects)
            if missing_objects:
                missing_type, missing_name = missing_objects[0]
                raise ValueError(
                    "operations backup anomaly schema is incomplete: "
                    f"missing {missing_type} {missing_name}"
                )
            required_incident_objects = {
                ("table", "incident_events"),
                *(
                    ("trigger", trigger_name)
                    for trigger_name in REQUIRED_INCIDENT_TRIGGERS
                ),
            }
            missing_incident_objects = sorted(
                required_incident_objects - schema_objects
            )
            if missing_incident_objects:
                missing_type, missing_name = missing_incident_objects[0]
                raise ValueError(
                    "operations backup incident schema is incomplete: "
                    f"missing {missing_type} {missing_name}"
                )
            event_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(anomaly_case_events)"
                ).fetchall()
            }
            if "event_sequence" not in event_columns:
                raise ValueError(
                    "operations backup anomaly schema is incomplete: "
                    "missing anomaly event sequence"
                )
            if connection.execute(
                """
                SELECT COUNT(*)
                FROM anomaly_case_events
                WHERE event_sequence IS NULL OR event_sequence <= 0
                """
            ).fetchone()[0]:
                raise ValueError(
                    "operations backup anomaly event sequence is invalid"
                )

            cases = connection.execute("SELECT * FROM anomaly_cases").fetchall()
            for case in cases:
                try:
                    verify_anomaly_case_evidence(connection, case)
                except RuntimeError as exc:
                    raise ValueError(str(exc)) from exc
            _verify_anomaly_observation_events(connection)
            incident_events = connection.execute(
                """
                SELECT event_id, incident_id, event_type, created_at, payload_json
                FROM incident_events
                """
            ).fetchall()
            for event in incident_events:
                try:
                    verify_incident_event_evidence(event)
                except (TypeError, ValueError) as exc:
                    raise ValueError(str(exc)) from exc
            incidents = connection.execute("SELECT * FROM incidents").fetchall()
            for incident in incidents:
                assessment = classify_incident_resolution(connection, incident)
                if assessment.resolution_proof_status == "invalid":
                    raise ValueError(
                        "operations backup contains an invalid incident "
                        f"resolution proof: {incident['incident_id']}"
                    )
    except sqlite3.DatabaseError as exc:
        raise ValueError("operations backup SQLite validation failed") from exc


def _verify_anomaly_observation_events(connection: sqlite3.Connection) -> None:
    """Verify every immutable finding event against its case and scan manifest."""
    rows = connection.execute(
        """
        SELECT event.event_id, event.scan_id, event.observation_sha256,
               event.payload_json, case_state.fingerprint, scan.evidence_json
        FROM anomaly_case_events AS event
        JOIN anomaly_cases AS case_state ON case_state.case_id = event.case_id
        LEFT JOIN anomaly_scans AS scan ON scan.scan_id = event.scan_id
        WHERE event.event_type IN ('opened','evidence_changed','reopened')
        """
    ).fetchall()
    for row in rows:
        expected_sha256 = str(row["observation_sha256"] or "")
        payload_json = str(row["payload_json"])
        if (
            row["scan_id"] is None
            or row["evidence_json"] is None
            or len(expected_sha256) != 64
            or hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            != expected_sha256
        ):
            raise ValueError(
                "operations backup anomaly observation event is inconsistent: "
                f"{row['event_id']}"
            )
        try:
            payload = json.loads(payload_json)
            evidence = json.loads(str(row["evidence_json"]))
        except json.JSONDecodeError as exc:
            raise ValueError(
                "operations backup anomaly observation event is invalid: "
                f"{row['event_id']}"
            ) from exc
        manifest = evidence.get("observation_manifest") if isinstance(evidence, dict) else None
        linked = (
            isinstance(payload, dict)
            and payload.get("fingerprint") == row["fingerprint"]
            and isinstance(manifest, list)
            and any(
                isinstance(item, dict)
                and item.get("fingerprint") == row["fingerprint"]
                and item.get("evidence_sha256") == expected_sha256
                for item in manifest
            )
        )
        if not linked:
            raise ValueError(
                "operations backup anomaly observation event is inconsistent: "
                f"{row['event_id']}"
            )


def _mark_operations_evidence_stale(
    path: Path,
    *,
    backup_manifest_sha256: str,
    now: datetime,
) -> str:
    """Retain the forward-only ledger while making its restored-data boundary explicit."""
    from aios.alerts import Alert, AlertSeverity, AlertStore

    incident = AlertStore(path).emit(
        Alert(
            code="restore_requires_anomaly_rescan",
            severity=AlertSeverity.WARNING,
            title="Data-quality review evidence requires a post-restore scan",
            body=(
                "Analytical and paper state are being restored while the operations "
                "ledger remains forward-only. Record a fresh anomaly scan before "
                "relying on current case status, then resolve this incident explicitly."
            ),
            dedup_key="restore:anomaly-evidence-stale",
            source_job="aios restore",
            payload={
                "backup_manifest_sha256": backup_manifest_sha256,
                "required_action": "aios anomaly-scan --record",
            },
            notify=False,
        ),
        now=now,
    )
    return incident.incident_id


def _canonical_json(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")

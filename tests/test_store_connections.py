from __future__ import annotations

from pathlib import Path

import duckdb

import aios.storage.store as store_module
from aios.storage.store import Store, store_scope


def test_read_only_store_scope_releases_database_for_the_next_writer(tmp_path: Path) -> None:
    db_path = tmp_path / "scoped.duckdb"
    writer = Store(db_path, lock_wait_seconds=0)
    writer.close()

    with store_scope(db_path, read_only=True, lock_wait_seconds=0) as reader:
        assert reader.query("SELECT COUNT(*) AS n FROM prices")[0]["n"] == 0

    next_writer = Store(db_path, lock_wait_seconds=0)
    next_writer.close()


def test_store_retries_only_a_duckdb_lock_conflict(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "existing.duckdb"
    db_path.touch()
    attempts = 0

    class FakeConnection:
        def execute(self, statement: str) -> FakeConnection:
            assert statement == "SET TimeZone = 'UTC'"
            return self

        def close(self) -> None:
            return None

    def connect(_path: str, *, read_only: bool):
        nonlocal attempts
        attempts += 1
        assert read_only is True
        if attempts == 1:
            raise duckdb.IOException("Could not set lock: Conflicting lock is held")
        return FakeConnection()

    monkeypatch.setattr(store_module.duckdb, "connect", connect)
    monkeypatch.setattr(store_module, "sleep", lambda _seconds: None)

    store = Store(db_path, read_only=True, lock_wait_seconds=1)
    store.close()

    assert attempts == 2

from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pytest

from aios.factor_batch import DecisionScopedFactorStore
from aios.storage.store import (
    FUNDAMENTAL_EVIDENCE_VERSIONS_MIGRATION,
    Store,
)


def _fundamental(
    value: float,
    *,
    as_of_date: str = "2024-02-01",
    **overrides: object,
) -> dict:
    row = {
        "ticker": "AAA",
        "period_end": "2023-12-31",
        "as_of_date": as_of_date,
        "fiscal_period": "FY2023",
        "statement": "income",
        "metric": "revenue",
        "value": value,
        "quarter_value": value,
        "unit": "USD",
        "source": "test",
    }
    row.update(overrides)
    return row


def test_named_generation_preserves_overwritten_and_later_accepted_facts(
    tmp_path,
) -> None:
    store = Store(tmp_path / "generation.duckdb")
    store.upsert_fundamentals([_fundamental(100.0)])
    first = store.create_fundamental_evidence_generation(
        purpose="research.factor-decision",
        decision_date="2024-06-30",
        now=datetime(2024, 7, 1, tzinfo=UTC),
    )

    store.upsert_fundamentals([_fundamental(999.0)])
    store.upsert_fundamentals(
        [_fundamental(200.0, as_of_date="2024-03-01")]
    )

    current = store.pit_factor_fundamentals(
        "AAA",
        "2024-06-30",
        ["revenue"],
    )
    pinned = store.pit_factor_fundamentals(
        "AAA",
        "2024-06-30",
        ["revenue"],
        evidence_generation_id=first.generation_id,
    )
    second = store.create_fundamental_evidence_generation(
        purpose="research.factor-decision",
        decision_date="2024-06-30",
        now=datetime(2024, 7, 2, tzinfo=UTC),
    )
    repinned = store.pit_factor_fundamentals(
        "AAA",
        "2024-06-30",
        ["revenue"],
        evidence_generation_id=second.generation_id,
    )

    assert [(row["as_of_date"].isoformat(), row["value"]) for row in current] == [
        ("2024-03-01", 200.0)
    ]
    assert [(row["as_of_date"].isoformat(), row["value"]) for row in pinned] == [
        ("2024-02-01", 100.0)
    ]
    assert [(row["as_of_date"].isoformat(), row["value"]) for row in repinned] == [
        ("2024-03-01", 200.0)
    ]
    assert second.version_sequence > first.version_sequence


def test_decision_scoped_batch_uses_the_exact_named_generation(tmp_path) -> None:
    store = Store(tmp_path / "batch-generation.duckdb")
    store.upsert_fundamentals([_fundamental(100.0)])
    generation = store.create_fundamental_evidence_generation(
        purpose="dashboard.research",
        decision_date="2024-06-30",
    )
    store.upsert_fundamentals([_fundamental(999.0)])

    factor_store = DecisionScopedFactorStore(
        store,
        ["AAA"],
        fundamental_evidence_generation_id=generation.generation_id,
    )
    rows = factor_store.pit_factor_fundamentals(
        "AAA",
        "2024-06-30",
        ["revenue"],
    )

    assert len(rows) == 1
    assert rows[0]["value"] == 100.0


def test_generation_is_durable_and_unknown_ids_fail_closed(tmp_path) -> None:
    path = tmp_path / "durable-generation.duckdb"
    store = Store(path)
    store.upsert_fundamentals([_fundamental(100.0)])
    generation = store.create_fundamental_evidence_generation(
        purpose="paper.proposal-preview",
        decision_date="2024-06-30",
    )
    store.close()

    reopened = Store(path, read_only=True)
    assert reopened.fundamental_evidence_generation(generation.generation_id) == generation
    with pytest.raises(ValueError, match="unknown fundamental evidence generation"):
        reopened.pit_factor_fundamentals(
            "AAA",
            "2024-06-30",
            ["revenue"],
            evidence_generation_id="fundamental-generation-missing",
        )


def test_legacy_projection_migration_is_marked_and_seeded_atomically(tmp_path) -> None:
    path = tmp_path / "legacy-generation.duckdb"
    legacy = duckdb.connect(str(path))
    try:
        legacy.execute(
            """
            CREATE TABLE fundamentals (
                ticker VARCHAR NOT NULL,
                period_end DATE NOT NULL,
                as_of_date DATE NOT NULL,
                fiscal_period VARCHAR,
                statement VARCHAR,
                metric VARCHAR NOT NULL,
                value DOUBLE,
                quarter_value DOUBLE,
                unit VARCHAR,
                source VARCHAR,
                fetched_at TIMESTAMP,
                PRIMARY KEY (ticker, period_end, as_of_date, metric)
            )
            """
        )
        legacy.execute(
            """
            INSERT INTO fundamentals
            (ticker, period_end, as_of_date, fiscal_period, statement,
             metric, value, quarter_value, unit, source, fetched_at)
            VALUES ('AAA', '2023-12-31', '2024-02-01', 'FY2023',
                    'income', 'revenue', 100, 100, 'USD', 'test', NULL)
            """
        )
    finally:
        legacy.close()

    store = Store(path, allow_schema_upgrade=True)
    assert store.query(
        "SELECT COUNT(*) AS n FROM schema_migrations WHERE name = ?",
        (FUNDAMENTAL_EVIDENCE_VERSIONS_MIGRATION,),
    )[0]["n"] == 1
    assert store.query("SELECT COUNT(*) AS n FROM fundamental_versions")[0]["n"] == 1
    assert store.query("SELECT recorded_at FROM fundamental_versions")[0][
        "recorded_at"
    ] is None
    generation = store.create_fundamental_evidence_generation(
        purpose="migration.proof",
        decision_date="2024-06-30",
    )
    assert store.pit_factor_fundamentals(
        "AAA",
        "2024-06-30",
        ["revenue"],
        evidence_generation_id=generation.generation_id,
    )[0]["value"] == 100.0


def test_upsert_versions_the_resolved_projection_after_identity_merge(tmp_path) -> None:
    store = Store(tmp_path / "resolved-projection.duckdb")
    store.upsert_fundamentals(
        [
            _fundamental(
                100.0,
                issuer_id="issuer-aaa",
                security_id="security-aaa",
            )
        ]
    )
    before = store.create_fundamental_evidence_generation(
        purpose="identity.merge.before",
        decision_date="2024-06-30",
    )

    store.upsert_fundamentals(
        [_fundamental(125.0, issuer_id=None, security_id=None)]
    )
    after = store.create_fundamental_evidence_generation(
        purpose="identity.merge.after",
        decision_date="2024-06-30",
    )

    current = store.query(
        """
        SELECT issuer_id, security_id, value
        FROM fundamentals
        WHERE ticker = 'AAA' AND metric = 'revenue'
        """
    )[0]
    latest = store.query(
        """
        SELECT issuer_id, security_id, value, is_deleted
        FROM fundamental_versions
        WHERE ticker = 'AAA' AND metric = 'revenue'
        ORDER BY version_sequence DESC
        LIMIT 1
        """
    )[0]
    old_value = store.pit_factor_fundamentals(
        "AAA",
        "2024-06-30",
        ["revenue"],
        evidence_generation_id=before.generation_id,
    )[0]["value"]
    new_value = store.pit_factor_fundamentals(
        "AAA",
        "2024-06-30",
        ["revenue"],
        evidence_generation_id=after.generation_id,
    )[0]["value"]
    report = {row["check"]: row for row in store.data_quality_report()}

    assert current == {
        "issuer_id": "issuer-aaa",
        "security_id": "security-aaa",
        "value": 125.0,
    }
    assert latest == {**current, "is_deleted": False}
    assert old_value == 100.0
    assert new_value == 125.0
    assert report["fundamental_evidence_latest_projection_mismatch"]["status"] == "ok"


def test_upsert_rejects_duplicate_economic_keys_atomically(tmp_path) -> None:
    store = Store(tmp_path / "duplicate-key.duckdb")

    with pytest.raises(ValueError, match="duplicate economic key"):
        store.upsert_fundamentals([_fundamental(100.0), _fundamental(200.0)])

    assert store.query("SELECT COUNT(*) AS n FROM fundamentals")[0]["n"] == 0
    assert store.query("SELECT COUNT(*) AS n FROM fundamental_versions")[0]["n"] == 0


def test_purge_appends_tombstone_and_preserves_prior_generation(tmp_path) -> None:
    store = Store(tmp_path / "purge-tombstone.duckdb")
    store.upsert_fundamentals([_fundamental(75.0, metric="ebitda")])
    before = store.create_fundamental_evidence_generation(
        purpose="cleanup.before",
        decision_date="2024-06-30",
    )

    assert store.purge_legacy_ebitda("AAA") == 1
    after = store.create_fundamental_evidence_generation(
        purpose="cleanup.after",
        decision_date="2024-06-30",
    )

    assert store.pit_factor_fundamentals(
        "AAA",
        "2024-06-30",
        ["ebitda"],
        evidence_generation_id=before.generation_id,
    )[0]["value"] == 75.0
    assert store.pit_factor_fundamentals(
        "AAA",
        "2024-06-30",
        ["ebitda"],
        evidence_generation_id=after.generation_id,
    ) == []
    assert store.query(
        """
        SELECT is_deleted
        FROM fundamental_versions
        WHERE ticker = 'AAA' AND metric = 'ebitda'
        ORDER BY version_sequence DESC
        LIMIT 1
        """
    )[0]["is_deleted"] is True
    report = {row["check"]: row for row in store.data_quality_report()}
    assert report["fundamental_evidence_latest_projection_mismatch"]["status"] == "ok"


def test_quarantine_appends_deletion_tombstone(tmp_path) -> None:
    store = Store(tmp_path / "quarantine-tombstone.duckdb")
    store.execute(
        """
        INSERT INTO fundamentals
        (ticker, period_end, as_of_date, fiscal_period, statement, metric,
         value, quarter_value, unit, source)
        VALUES ('AAA', '2024-12-31', '2024-06-30', 'FY2024', 'income',
                'revenue', 100, 100, 'USD', 'test')
        """
    )
    store.execute(
        """
        INSERT INTO fundamental_versions
        (ticker, period_end, as_of_date, fiscal_period, statement, metric,
         value, quarter_value, unit, source, is_deleted)
        SELECT ticker, period_end, as_of_date, fiscal_period, statement, metric,
               value, quarter_value, unit, source, FALSE
        FROM fundamentals
        """
    )

    assert store.quarantine_invalid_fundamental_periods() == 1
    assert store.query("SELECT COUNT(*) AS n FROM fundamentals")[0]["n"] == 0
    assert store.query("SELECT COUNT(*) AS n FROM fundamentals_quarantine")[0]["n"] == 1
    assert store.query(
        """
        SELECT is_deleted
        FROM fundamental_versions
        ORDER BY version_sequence DESC
        LIMIT 1
        """
    )[0]["is_deleted"] is True
    report = {row["check"]: row for row in store.data_quality_report()}
    assert report["fundamental_evidence_latest_projection_mismatch"]["status"] == "ok"


@pytest.mark.parametrize("corruption", ["delete", "mutate"])
def test_data_quality_detects_projection_history_divergence(
    tmp_path,
    corruption: str,
) -> None:
    store = Store(tmp_path / f"projection-divergence-{corruption}.duckdb")
    store.upsert_fundamentals([_fundamental(100.0)])
    if corruption == "delete":
        store.execute("DELETE FROM fundamentals WHERE ticker = 'AAA'")
    else:
        store.execute(
            "UPDATE fundamentals SET value = 999 WHERE ticker = 'AAA'"
        )

    report = {row["check"]: row for row in store.data_quality_report()}
    assert report["fundamental_evidence_latest_projection_mismatch"] == {
        "check": "fundamental_evidence_latest_projection_mismatch",
        "status": "fail",
        "count": 1,
        "detail": (
            "Latest fact versions, including deletion tombstones, must exactly "
            "reconstruct the current fundamental projection."
        ),
    }


def test_projection_upsert_rolls_back_when_version_append_fails(
    tmp_path,
    monkeypatch,
) -> None:
    store = Store(tmp_path / "upsert-version-failure.duckdb")
    original_execute = store.execute

    def fail_version_append(sql: str, params=None):
        if "INSERT INTO fundamental_versions" in sql:
            raise RuntimeError("injected version append failure")
        return original_execute(sql, params)

    monkeypatch.setattr(store, "execute", fail_version_append)
    with pytest.raises(RuntimeError, match="injected version append failure"):
        store.upsert_fundamentals([_fundamental(100.0)])

    assert store.query("SELECT COUNT(*) AS n FROM fundamentals")[0]["n"] == 0
    assert store.query("SELECT COUNT(*) AS n FROM fundamental_versions")[0]["n"] == 0


def test_tombstone_append_rolls_back_when_projection_delete_fails(
    tmp_path,
    monkeypatch,
) -> None:
    store = Store(tmp_path / "tombstone-delete-failure.duckdb")
    store.upsert_fundamentals([_fundamental(75.0, metric="ebitda")])
    original_execute = store.execute

    def fail_projection_delete(sql: str, params=None):
        if sql.strip().startswith("DELETE FROM fundamentals WHERE"):
            raise RuntimeError("injected projection delete failure")
        return original_execute(sql, params)

    monkeypatch.setattr(store, "execute", fail_projection_delete)
    with pytest.raises(RuntimeError, match="injected projection delete failure"):
        store.purge_legacy_ebitda("AAA")

    assert store.query("SELECT COUNT(*) AS n FROM fundamentals")[0]["n"] == 1
    assert store.query("SELECT COUNT(*) AS n FROM fundamental_versions")[0]["n"] == 1
    assert store.query(
        "SELECT is_deleted FROM fundamental_versions"
    )[0]["is_deleted"] is False


@pytest.mark.parametrize("corruption", ["unversioned_current", "live_after_tombstone"])
def test_data_quality_covers_both_remaining_projection_join_branches(
    tmp_path,
    corruption: str,
) -> None:
    store = Store(tmp_path / f"projection-join-{corruption}.duckdb")
    if corruption == "unversioned_current":
        store.execute(
            """
            INSERT INTO fundamentals
            (ticker, period_end, as_of_date, fiscal_period, statement, metric,
             value, quarter_value, unit, source)
            VALUES ('AAA', '2023-12-31', '2024-02-01', 'FY2023', 'income',
                    'revenue', 100, 100, 'USD', 'test')
            """
        )
    else:
        store.upsert_fundamentals([_fundamental(100.0)])
        store.execute(
            """
            INSERT INTO fundamental_versions
            (ticker, period_end, as_of_date, fiscal_period, statement, metric,
             value, quarter_value, unit, source, is_deleted)
            SELECT ticker, period_end, as_of_date, fiscal_period, statement,
                   metric, value, quarter_value, unit, source, TRUE
            FROM fundamentals
            """
        )

    report = {row["check"]: row for row in store.data_quality_report()}
    assert report["fundamental_evidence_latest_projection_mismatch"]["status"] == "fail"
    assert report["fundamental_evidence_latest_projection_mismatch"]["count"] == 1


def test_writable_reopen_fails_closed_on_projection_history_corruption(tmp_path) -> None:
    path = tmp_path / "corrupt-reopen.duckdb"
    store = Store(path)
    store.upsert_fundamentals([_fundamental(100.0)])
    store.execute("UPDATE fundamentals SET value = 999 WHERE ticker = 'AAA'")
    store.close()

    with pytest.raises(RuntimeError, match="do not reconstruct"):
        Store(path)

    # Constructor failure must close its connection so recovery tooling can
    # immediately obtain a direct engine connection to inspect the file.
    recovery = duckdb.connect(str(path), read_only=False)
    try:
        assert recovery.execute("SELECT COUNT(*) FROM fundamentals").fetchone()[0] == 1
    finally:
        recovery.close()

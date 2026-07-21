from datetime import date

import pytest

from aios.ingest.universe import (
    EffectiveSpan,
    UniverseEvent,
    build_membership_from_events,
    load_membership_csv,
    load_universe_events_csv,
    merge_universe_event_batches,
    reconcile_event_boundaries,
)
from aios.storage.store import Store


def _membership(ticker: str, start: str, end: str | None, known: str) -> dict:
    return {
        "universe_id": "demo",
        "ticker": ticker,
        "effective_start": start,
        "effective_end": end,
        "known_date": known,
        "end_known_date": known if end is not None else None,
        "source": "test",
    }


def test_membership_is_point_in_time_and_half_open(tmp_path):
    store = Store(tmp_path / "universe.duckdb")
    try:
        store.upsert_universe_membership(
            [
                _membership("A", "2024-01-01", "2024-07-01", "2023-12-15"),
                _membership("B", "2024-07-01", None, "2024-06-15"),
            ]
        )

        assert [row["ticker"] for row in store.universe_membership_on("demo", "2024-06-30")] == [
            "A"
        ]
        assert [row["ticker"] for row in store.universe_membership_on("demo", "2024-07-01")] == [
            "B"
        ]
        assert store.universe_membership_on("demo", "2024-06-01")[0]["known_date"] == date(
            2023, 12, 15
        )
    finally:
        store.close()


def test_membership_separates_decision_knowledge_from_execution_effective_date(tmp_path):
    store = Store(tmp_path / "universe-execution.duckdb")
    try:
        ending = _membership("A", "2024-01-01", "2024-07-01", "2023-12-15")
        ending["end_known_date"] = "2024-06-15"
        store.upsert_universe_membership(
            [
                ending,
                _membership("B", "2024-07-01", None, "2024-06-15"),
            ]
        )

        before = store.universe_membership_known_on(
            "demo", "2024-06-14", "2024-07-01"
        )
        after = store.universe_membership_known_on(
            "demo", "2024-06-15", "2024-07-01"
        )

        assert [row["ticker"] for row in before] == ["A"]
        assert [row["ticker"] for row in after] == ["B"]
    finally:
        store.close()


def test_membership_rejects_overlap_and_future_knowledge(tmp_path):
    store = Store(tmp_path / "universe-invalid.duckdb")
    try:
        store.upsert_universe_membership(
            [_membership("A", "2024-01-01", "2024-07-01", "2023-12-15")]
        )
        with pytest.raises(ValueError, match="overlapping"):
            store.upsert_universe_membership(
                [_membership("A", "2024-06-01", None, "2024-05-01")]
            )
        with pytest.raises(ValueError, match="known_date"):
            store.upsert_universe_membership(
                [_membership("B", "2024-01-01", None, "2024-01-02")]
            )
    finally:
        store.close()


def test_membership_csv_requires_known_date(tmp_path):
    path = tmp_path / "members.csv"
    path.write_text("ticker,effective_start\nA,2024-01-01\n", encoding="utf-8")
    with pytest.raises(ValueError, match="known_date"):
        load_membership_csv(path, universe_id="demo")


def test_membership_csv_applies_defaults(tmp_path):
    path = tmp_path / "members.csv"
    path.write_text(
        "ticker,effective_start,effective_end,known_date\n"
        "a,2024-01-01,,2023-12-15\n",
        encoding="utf-8",
    )
    rows = load_membership_csv(path, universe_id="demo", source="test-csv")
    assert rows == [
        {
            "universe_id": "demo",
            "ticker": "A",
            "effective_start": date(2024, 1, 1),
            "effective_end": None,
            "known_date": date(2023, 12, 15),
            "end_known_date": None,
            "source": "test-csv",
        }
    ]


def test_membership_csv_rejects_trailing_extra_field(tmp_path):
    path = tmp_path / "members.csv"
    path.write_text(
        "ticker,effective_start,effective_end,known_date\n"
        "A,2024-01-01,,2023-12-15,\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="extra fields"):
        load_membership_csv(path, universe_id="demo")


def test_event_csv_requires_actionable_dates_and_official_source(tmp_path):
    path = tmp_path / "events.csv"
    path.write_text(
        "ticker,effective_date,action,known_date,source\n"
        "A,2024-01-01,Addition,2024-01-02,https://example.com/release\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="known_date follows"):
        load_universe_events_csv(path, require_official_sources=True)

    path.write_text(
        "ticker,effective_date,action,known_date,source\n"
        "A,2024-01-02,Addition,2024-01-01,https://example.com/release\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not an official"):
        load_universe_events_csv(path, require_official_sources=True)


def test_reviewed_event_batches_refuse_overlapping_keys():
    event = UniverseEvent(
        "sp500",
        "A",
        date(2024, 1, 2),
        "addition",
        date(2024, 1, 1),
        "https://press.spglobal.com/release",
    )

    with pytest.raises(ValueError, match="duplicate event across reviewed batches"):
        merge_universe_event_batches([event], [event])


def test_reference_reconciliation_catches_false_replacement():
    spans = [
        EffectiveSpan("AAP", date(2020, 1, 1), date(2023, 8, 25)),
        EffectiveSpan("JNJ", date(2020, 1, 1), None),
        EffectiveSpan("KVUE", date(2023, 8, 25), None),
    ]
    source = "https://press.spglobal.com/2023-08-21-kvue"
    events = [
        UniverseEvent("sp500", "JNJ", date(2023, 8, 25), "deletion", date(2023, 8, 21), source),
        UniverseEvent("sp500", "KVUE", date(2023, 8, 25), "addition", date(2023, 8, 21), source),
    ]

    result = reconcile_event_boundaries(
        spans,
        events,
        coverage_start=date(2023, 8, 1),
        coverage_end=date(2023, 8, 31),
    )

    assert result.missing_events == (("AAP", date(2023, 8, 25), "deletion"),)
    assert result.unexpected_events == (("JNJ", date(2023, 8, 25), "deletion"),)


def test_reference_reconciliation_keeps_small_date_conflict_visible():
    spans = [EffectiveSpan("VFC", date(2020, 1, 1), date(2024, 4, 1))]
    events = [
        UniverseEvent(
            "sp500",
            "VFC",
            date(2024, 4, 3),
            "deletion",
            date(2024, 3, 27),
            "https://press.spglobal.com/release",
        )
    ]

    result = reconcile_event_boundaries(
        spans,
        events,
        coverage_start=date(2024, 3, 1),
        coverage_end=date(2024, 4, 30),
    )

    assert result.is_complete is True
    assert result.is_clean is False
    assert result.date_conflicts == (
        ("VFC", date(2024, 4, 1), date(2024, 4, 3), "deletion"),
    )


def test_event_builder_handles_deletion_reentry_and_certified_end():
    spans = [
        EffectiveSpan("A", date(2020, 1, 1), date(2024, 2, 1)),
        EffectiveSpan("A", date(2024, 3, 1), None),
        EffectiveSpan("B", date(2020, 1, 1), None),
        EffectiveSpan("C", date(2024, 2, 1), date(2024, 3, 1)),
    ]
    source = "https://press.spglobal.com/release"
    events = [
        UniverseEvent("sp500", "A", date(2024, 2, 1), "deletion", date(2024, 1, 20), source),
        UniverseEvent("sp500", "C", date(2024, 2, 1), "addition", date(2024, 1, 20), source),
        UniverseEvent("sp500", "C", date(2024, 3, 1), "deletion", date(2024, 2, 20), source),
        UniverseEvent("sp500", "A", date(2024, 3, 1), "addition", date(2024, 2, 20), source),
    ]

    rows = build_membership_from_events(
        spans,
        events,
        coverage_start=date(2024, 1, 1),
        coverage_end=date(2024, 3, 31),
        universe_id="sp500",
        baseline_source="https://github.com/example/spans@sha",
    )

    assert [(row["ticker"], row["effective_start"], row["effective_end"]) for row in rows] == [
        ("A", date(2024, 1, 1), date(2024, 2, 1)),
        ("A", date(2024, 3, 1), date(2024, 4, 1)),
        ("B", date(2024, 1, 1), date(2024, 4, 1)),
        ("C", date(2024, 2, 1), date(2024, 3, 1)),
    ]
    assert rows[1]["known_date"] == date(2024, 2, 20)
    assert rows[0]["end_known_date"] == date(2024, 1, 20)
    assert rows[1]["end_known_date"] == date(2024, 3, 31)
    assert rows[2]["known_date"] == date(2024, 1, 1)
    assert rows[2]["end_known_date"] == date(2024, 3, 31)
    assert rows[3]["end_known_date"] == date(2024, 2, 20)

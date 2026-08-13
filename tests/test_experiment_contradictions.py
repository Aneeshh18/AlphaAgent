"""Contradiction detection across registered experiment windows.

Written after a concrete failure in this repository: QVML was promoted to a
live forward trial on the 2025-01→2026-07 window, where it returned +49.4%
against SPY's +35.0%. A registered run on 2023-08→2024-12, where the same
model returned +25.0% against the same benchmark's +39.5%, was already on
disk. Both runs were visible and the registry reported neither as a problem,
because it only ever placed rows side by side.

Reporting is not enough. A reader comparing two rows of returns will see the
one that agrees with the decision already being made, so an unstable ranking
has to be named as a contradiction.
"""

from __future__ import annotations

from typing import Any

from aios.experiments import _beat_benchmark, _detect_contradictions


def _document(regime: float | None, benchmark: float | None) -> dict[str, Any]:
    metrics: dict[str, Any] = {"regime": {"cumulative_return": regime}}
    if benchmark is not None:
        metrics["benchmark"] = {"SPY": {"cumulative_return": benchmark}}
    return {"metrics": metrics}


def _row(experiment_id: str, factor_model: str, beat: bool | None) -> dict[str, Any]:
    return {
        "experiment_id": experiment_id,
        "factor_model": factor_model,
        "beat_benchmark": beat,
    }


def test_beat_benchmark_is_true_only_when_it_exceeds_every_benchmark() -> None:
    assert _beat_benchmark(_document(0.50, 0.40)) is True
    assert _beat_benchmark(_document(0.25, 0.40)) is False


def test_beat_benchmark_is_none_without_a_recorded_benchmark() -> None:
    """An absent benchmark must never read as a win.

    Every experiment registered before benchmarks were stored lands here.
    """
    assert _beat_benchmark(_document(0.50, None)) is None
    assert _beat_benchmark(_document(None, 0.40)) is None


def test_contradiction_is_flagged_when_a_model_wins_and_loses() -> None:
    """The exact QVML case that misled this project."""
    contradictions = _detect_contradictions(
        [
            _row("exp-2023window", "qvml", False),
            _row("exp-2025window", "qvml", True),
        ]
    )

    assert len(contradictions) == 1
    found = contradictions[0]
    assert found["factor_model"] == "qvml"
    assert found["beat_benchmark_in"] == ["exp-2025window"]
    assert found["lost_to_benchmark_in"] == ["exp-2023window"]
    assert "not stable" in found["detail"]
    assert "neither window may be cited on its own" in found["detail"]


def test_consistent_results_are_not_flagged() -> None:
    assert (
        _detect_contradictions(
            [_row("exp-a", "qv", True), _row("exp-b", "qv", True)]
        )
        == []
    )
    assert (
        _detect_contradictions(
            [_row("exp-a", "qv", False), _row("exp-b", "qv", False)]
        )
        == []
    )


def test_different_models_do_not_contradict_each_other() -> None:
    """One model winning while another loses is a comparison, not a contradiction."""
    assert (
        _detect_contradictions(
            [_row("exp-a", "qv", True), _row("exp-b", "qvml", False)]
        )
        == []
    )


def test_rows_without_benchmarks_are_excluded_from_the_check() -> None:
    """An unknown outcome must not be silently counted as a loss."""
    assert (
        _detect_contradictions(
            [_row("exp-a", "qv", None), _row("exp-b", "qv", True)]
        )
        == []
    )


def test_every_contradicting_model_is_reported() -> None:
    contradictions = _detect_contradictions(
        [
            _row("exp-a", "qv", True),
            _row("exp-b", "qv", False),
            _row("exp-c", "qvml", False),
            _row("exp-d", "qvml", True),
        ]
    )

    assert [entry["factor_model"] for entry in contradictions] == ["qv", "qvml"]

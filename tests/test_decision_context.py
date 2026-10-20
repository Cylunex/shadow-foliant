from __future__ import annotations

import pytest

from core.decision_context import DecisionContext


@pytest.mark.parametrize("day,mode,cutoff,expected", [
    ("2026-10-19", "preopen", None, ("2026-10-19", False)),
    ("2026-10-20", "preopen", None, ("2026-10-20", False)),
    ("2026-10-19", "preopen", "2026-10-15", ("2026-10-15", True)),
    ("2026-10-19", "historical_close", None, ("2026-10-19", True)),
])
def test_calendar_boundary_keeps_the_same_market_cutoff(day, mode, cutoff, expected):
    context = DecisionContext.build(day, mode=mode, data_cutoff=cutoff,
                                    policy_version="example", policy_hash="example")
    assert context.calendar_boundary() == expected


def test_historical_close_defaults_after_market_data_is_available() -> None:
    context = DecisionContext.build(
        "2026-08-21", mode="historical_close", policy_version="example",
        policy_hash="example",
    )
    assert context.decision_at == "2026-08-21T15:30:00+08:00"
    assert context.market_cutoff == "2026-08-21"
    assert context.market_cutoff_inclusive is True


def test_decision_context_uses_shanghai_and_rejects_temporal_leakage() -> None:
    preopen = DecisionContext.build(
        "2026-08-21", mode="preopen", decision_at="2026-08-21T09:00:00",
        policy_version="example", policy_hash="example",
    )
    assert preopen.decision_at.endswith("+08:00")
    with pytest.raises(ValueError, match="before the A-share open"):
        DecisionContext.build(
            "2026-08-21", mode="preopen", decision_at="2026-08-21T09:30:00+08:00",
            policy_version="example", policy_hash="example",
        )
    with pytest.raises(ValueError, match="at or after 15:30"):
        DecisionContext.build(
            "2026-08-21", mode="historical_close",
            decision_at="2026-08-21T09:30:00+08:00",
            policy_version="example", policy_hash="example",
        )
    with pytest.raises(ValueError, match="selection_date"):
        DecisionContext.build(
            "2026-08-21", mode="historical_close",
            decision_at="2026-08-22T15:30:00+08:00",
            policy_version="example", policy_hash="example",
        )

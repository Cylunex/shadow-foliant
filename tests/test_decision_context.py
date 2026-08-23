from __future__ import annotations

import pytest

from core.decision_context import DecisionContext


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

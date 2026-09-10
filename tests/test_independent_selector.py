from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from analysis.independent_selector import (
    IndependentPolicy,
    _score,
    artifact_payload,
    build,
    comparison,
)


def _features(count: int = 20) -> pd.DataFrame:
    rows = []
    for index in range(count):
        rows.append({
            "symbol": f"60{index:04d}", "name": f"独立{index}", "industry": "测试",
            "history_days": 300, "paused_days_20": 0,
            "roe": 5 + index, "net_profit_growth_pct": -5 + index,
            "debt_ratio": 0.6 - index / 100, "cash_quality": 0.5 + index / 20,
            "ret_60": -0.1 + index / 100, "ma60_slope": -0.02 + index / 500,
            "persistence_60": index / 20, "pe_ttm": 35 - index,
            "pb": 5 - index / 10, "average_amount_20": 30_000_000 + index * 100_000,
            "volume_20_vs_60": 0.8 + index / 50,
            "amount_20_vs_60": 0.8 + index / 40,
            "max_drawdown_60": -0.3 + index / 100,
            "volatility_60": 0.5 - index / 100,
        })
    return pd.DataFrame(rows)


class _Store:
    def load_selection_manifest(self, _manifest_id):
        return {"decision_context": {
            "selection_date": "2026-09-10",
            "decision_at": "2026-09-10T09:45:00+08:00",
        }}


def test_fixed_component_weights_sum_to_100_and_missing_dimension_is_not_reweighted():
    policy = IndependentPolicy()
    assert policy.public_dict()["component_weights"] == {
        "fundamental_quality": 30,
        "medium_trend": 25,
        "valuation": 20,
        "flow_liquidity": 15,
        "risk_discount": 10,
    }
    frame = _features()
    frame.loc[19, "pe_ttm"] = None
    scored, missing = _score(frame, policy)
    assert len(scored) == 19
    assert missing["pe_ttm"] == 1
    assert "600019" not in set(scored["symbol"])
    assert scored["total_score"].between(0, 100).all()


def test_build_fails_closed_when_fewer_than_15_rows_have_all_dimensions():
    with patch("analysis.independent_selector._prepare_frame", return_value=(
        _features(14),
        {"market_as_of": "2026-09-09", "universe_count": 20,
         "hard_gate_count": 20, "feature_count": 14},
    )):
        result = build("manifest-1", store=_Store())
    assert result["status"] == "unavailable"
    assert result["reason"] == "fewer_than_15_complete_eligible_rows"
    assert result["top15"] == []


def test_comparison_omits_wencai_pairs_and_triple_until_every_reference_is_ready():
    formal = [{"code": "600001"}, {"code": "600002"}]
    independent = {"status": "ready", "top15": [
        {"symbol": "600002"}, {"symbol": "600003"},
    ]}
    failed_wencai = {"strategies": {
        "低估值": {"status": "failed", "picks": [{"symbol": "600003"}]},
    }}
    result = comparison(formal, independent, failed_wencai)
    assert result["availability"] == {
        "formal": True, "independent": True, "wencai": False,
    }
    assert result["pairwise"]["formal_independent"] == {
        "intersection": ["600002"],
        "formal_only": ["600001"],
        "independent_only": ["600003"],
    }
    assert result["triple"] is None
    assert "formal_wencai" not in result["pairwise"]


def test_ready_append_only_repair_wins_over_preserved_failed_attempt():
    artifacts = {
        "independent_selection": {"payload": {
            "status": "unavailable", "reason": "manifest_history_incomplete",
        }},
        "independent_selection_repair": {"payload": {
            "status": "ready", "top15": [{"symbol": "600001"}],
            "top5": [{"symbol": "600001"}],
        }},
    }
    assert artifact_payload(artifacts)["status"] == "ready"

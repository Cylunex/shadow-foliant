import hashlib
import json
import os
import sqlite3
import tempfile
import unittest

import numpy as np
import pandas as pd

from analysis.local_fusion import FusionPolicy, LocalFusionComposer
from analysis.strategy_policy_controller import validate_proposal
from data.research_store import ResearchStore


def _core(symbol, score):
    return {
        "symbol": symbol, "name": symbol, "industry": f"行业{symbol[-2:]}",
        "total_score": score, "data_coverage": 0.95,
        "fundamental_score": 30, "technical_60_score": 20,
        "source_labels": ["本地PIT数据仓"],
    }


class LocalFusionTest(unittest.TestCase):
    def setUp(self):
        self.policy = FusionPolicy(max_pairwise_correlation=1.0)
        self.core = [_core(f"600{i:03d}", 100 - i) for i in range(10)]
        self.local = {
            "rule_version": "local-satellite-v2",
            "strategies": {
                "主力资金": {"status": "ready", "rows": [
                    {"symbol": "001001", "name": "主力1", "lane_score": 90},
                    {"symbol": "001002", "name": "主力2", "lane_score": 80},
                ]},
                "低价擒牛": {"status": "ready", "rows": [
                    {"symbol": "001003", "name": "低价1", "lane_score": 90},
                    {"symbol": "001004", "name": "低价2", "lane_score": 80},
                ]},
                "低估值": {"status": "ready", "rows": [
                    {"symbol": "001005", "name": "价值1", "lane_score": 90},
                    {"symbol": "001006", "name": "价值2", "lane_score": 80},
                ]},
                "小市值": {"status": "ready", "rows": [
                    {"symbol": "001007", "name": "小盘", "lane_score": 99},
                ]},
                "净利增长": {"status": "ready", "rows": [
                    {"symbol": "001008", "name": "增长", "lane_score": 99},
                ]},
            },
        }
        self.genome = {"status": "ready", "rows": [
            {"symbol": "002001", "name": "基因1", "lane_score": 75,
             "strategy_snapshot_id": "g1"},
            {"symbol": "002002", "name": "基因2", "lane_score": 70,
             "strategy_snapshot_id": "g1"},
        ]}
        symbols = [row["symbol"] for row in self.core]
        symbols += [f"001{i:03d}" for i in range(1, 9)] + ["002001", "002002"]
        rng = np.random.default_rng(42)
        self.eligible = pd.DataFrame([{
            "symbol": symbol, "name": symbol,
            "industry": "卫星行业" if symbol.startswith("001") else f"行业{pos}",
            "data_coverage": 0.95, "state": "趋势确认",
            "return_series_60": pd.Series(rng.normal(0, 0.01, 60)),
        } for pos, symbol in enumerate(symbols)])

    def test_direct_production_uses_8_5_2_and_independent_top5(self):
        result = LocalFusionComposer(self.policy).compose(
            self.core, self.local, self.genome, self.eligible
        )
        self.assertEqual(result["lane_counts"], {"core": 8, "satellite": 5, "timing": 2})
        self.assertEqual(len(result["top15"]), 15)
        self.assertEqual(
            {row["assigned_lane"] for row in result["top5"]},
            {"core", "satellite", "timing"},
        )
        self.assertEqual(
            [row["assigned_lane"] for row in result["top5"]].count("core"), 3
        )

    def test_high_priority_local_strategies_win_satellite_capacity(self):
        result = LocalFusionComposer(self.policy).compose(
            self.core, self.local, self.genome, self.eligible
        )
        selected = [row["primary_strategy_name"] for row in result["top15"]
                    if row["assigned_lane"] == "satellite"]
        self.assertTrue(set(selected).issubset({"主力资金", "低价擒牛", "低估值"}))
        self.assertNotIn("小市值", selected)
        self.assertNotIn("净利增长", selected)


class StrategyPolicyValidationTest(unittest.TestCase):
    def setUp(self):
        self.current = FusionPolicy().as_dict()
        self.current_hash = hashlib.sha256(json.dumps(
            self.current, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        self.evidence = {"evidence_snapshot_id": "e1", "strategies": []}

    def test_degrade_is_allowed_without_mature_sample(self):
        proposal = {
            "base_policy_hash": self.current_hash,
            "evidence_snapshot_id": "e1",
            "changes": [{"path": "top15_satellite_cap", "from": 5, "to": 4}],
        }
        valid, _, updated = validate_proposal(
            proposal, self.current, self.evidence, self.current_hash
        )
        self.assertTrue(valid)
        self.assertEqual(updated["top15_satellite_cap"], 4)

    def test_core_floor_and_priority_order_cannot_be_overridden(self):
        forbidden = {
            "base_policy_hash": self.current_hash,
            "evidence_snapshot_id": "e1",
            "changes": [{"path": "top15_core_floor", "from": 8, "to": 7}],
        }
        self.assertFalse(validate_proposal(
            forbidden, self.current, self.evidence, self.current_hash
        )[0])
        inverted = {
            "base_policy_hash": self.current_hash,
            "evidence_snapshot_id": "e1",
            "changes": [
                {"path": "strategy_priority.小市值", "from": 0.7, "to": 0.6},
                {"path": "strategy_priority.净利增长", "from": 0.5, "to": 0.6},
            ],
        }
        self.assertFalse(validate_proposal(
            inverted, self.current, self.evidence, self.current_hash
        )[0])


class SelectionOutcomeTest(unittest.TestCase):
    def test_nomination_outcome_uses_next_open_and_trading_horizon(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ResearchStore(
                os.path.join(tmp, "research.db"), connect_fn=sqlite3.connect
            )
            policy = FusionPolicy().as_dict()
            policy_hash = FusionPolicy().policy_hash
            store.save_selection_strategy_records(
                "run-1", [{
                    "symbol": "600001", "lane": "satellite",
                    "strategy_id": "local_value_v2", "strategy_version": "v2",
                    "lane_rank": 1, "lane_score_raw": 80, "priority_weight": 1,
                    "evidence": {"pe_ttm": 8},
                }], policy=policy, policy_hash=policy_hash,
                selection_date="2026-08-21", input_snapshot_id="snapshot",
            )
            bars = pd.DataFrame([
                {"symbol": "600001", "trade_date": "2026-08-24", "open": 10,
                 "high": 11, "low": 9.8, "close": 10.5, "volume": 100, "amount": 1000},
                {"symbol": "600001", "trade_date": "2026-08-25", "open": 10.5,
                 "high": 11, "low": 10.2, "close": 11, "volume": 100, "amount": 1000},
                {"symbol": "600001", "trade_date": "2026-08-26", "open": 11,
                 "high": 12, "low": 10.8, "close": 12, "volume": 100, "amount": 1000},
                {"symbol": "600001", "trade_date": "2026-08-27", "open": 12,
                 "high": 12.2, "low": 11.5, "close": 11.8, "volume": 100, "amount": 1000},
                {"symbol": "600001", "trade_date": "2026-08-28", "open": 11.8,
                 "high": 12.6, "low": 11.7, "close": 12.5, "volume": 100, "amount": 1000},
            ])
            bars.attrs["provenance"] = {
                "provider": "fixture", "retrieved_at": "2026-08-28T17:00:00+08:00",
                "quality_status": "ok", "unit": "price/currency/shares",
            }
            store.upsert_daily_bars(bars)
            result = store.update_selection_candidate_outcomes(horizons=(1, 5))
            self.assertEqual(result["updated"], 2)
            repeated = store.update_selection_candidate_outcomes(horizons=(1, 5))
            self.assertEqual(repeated["updated"], 0)
            self.assertEqual(repeated["nomination_count"], 0)
            evidence = store.selection_strategy_evidence(horizon_days=5, lookback_days=3650)
            row = evidence["strategies"][0]
            self.assertEqual(row["strategy_id"], "local_value_v2")
            self.assertEqual(row["sample_size"], 1)
            self.assertEqual(row["avg_return_pct"], 25.0)


if __name__ == "__main__":
    unittest.main()

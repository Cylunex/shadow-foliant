import os
import sqlite3
import tempfile
import unittest

import pandas as pd

from analysis.local_reference_strategies import LocalReferenceStrategyEngine
from data.research_store import ResearchStore


def _sqlite(path):
    return sqlite3.connect(path)


class LocalReferenceStrategyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ResearchStore(
            os.path.join(self.tmp.name, "research.db"), connect_fn=_sqlite
        )
        self.market_date = "2026-08-21"
        self.frame = pd.DataFrame([
            {"symbol": "002001", "name": "低价成长", "close": 8, "market_cap": 45,
             "circulating_market_cap": 30, "net_profit_growth_pct": 120,
             "revenue_growth_pct": 20, "debt_ratio": .20, "pe_ttm": 12, "pb": 1.2,
             "dividend_yield": 1.5, "average_amount_20": 30_000_000},
            {"symbol": "000002", "name": "深市增长", "close": 15, "market_cap": 80,
             "circulating_market_cap": 60, "net_profit_growth_pct": 30,
             "revenue_growth_pct": 8, "debt_ratio": .25, "pe_ttm": 15, "pb": 1.1,
             "dividend_yield": 2, "average_amount_20": 40_000_000},
            {"symbol": "600003", "name": "主力流入", "close": 20, "market_cap": 100,
             "circulating_market_cap": 70, "net_profit_growth_pct": 5,
             "revenue_growth_pct": 5, "debt_ratio": .40, "pe_ttm": 25, "pb": 2,
             "dividend_yield": .5, "average_amount_20": 50_000_000},
            {"symbol": "300004", "name": "创业板排除", "close": 6, "market_cap": 20,
             "circulating_market_cap": 15, "net_profit_growth_pct": 300,
             "revenue_growth_pct": 100, "debt_ratio": .10, "pe_ttm": 10, "pb": 1,
             "dividend_yield": 3, "average_amount_20": 20_000_000},
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_five_local_strategies_are_deterministic_and_fail_closed(self):
        flow = pd.DataFrame([
            {"symbol": "600003", "name": "主力流入", "trade_date": self.market_date,
             "close": 20, "change_pct": 2, "main_net_inflow": 900_000_000,
             "main_net_inflow_ratio": 8},
            {"symbol": "000002", "name": "深市增长", "trade_date": self.market_date,
             "close": 15, "change_pct": 1, "main_net_inflow": -10,
             "main_net_inflow_ratio": -1},
        ])
        flow.attrs["provenance"] = {
            "provider": "fixture", "origin": "test", "quality_status": "ok",
            "retrieved_at": "2026-08-21T17:10:00+08:00", "schema_version": "1",
        }
        self.assertEqual(
            self.store.upsert_fund_flow_daily(flow, trade_date=self.market_date), 2
        )

        result = LocalReferenceStrategyEngine(self.store).run(
            self.frame, market_as_of=self.market_date
        )
        self.assertEqual(result["rule_version"], "local-satellite-v3")
        strategies = result["strategies"]
        self.assertFalse(result["reference_affects_score"])
        self.assertEqual(strategies["低价擒牛"]["rows"][0]["symbol"], "002001")
        self.assertEqual(strategies["小市值"]["rows"][0]["symbol"], "002001")
        self.assertEqual(strategies["净利增长"]["rows"][0]["symbol"], "002001")
        self.assertEqual(
            [row["symbol"] for row in strategies["低估值"]["rows"]],
            ["002001", "000002"],
        )
        self.assertEqual(strategies["主力资金"]["rows"][0]["symbol"], "600003")
        self.assertNotIn(
            "300004",
            {row["symbol"] for item in strategies.values() for row in item["rows"]},
        )

    def test_main_force_does_not_reuse_a_stale_snapshot(self):
        result = LocalReferenceStrategyEngine(self.store).run(
            self.frame, market_as_of=self.market_date
        )
        main_force = result["strategies"]["主力资金"]
        self.assertEqual(main_force["status"], "unavailable")
        self.assertIn("未使用成交量代理", main_force["reason"])

    def test_low_value_exposes_missing_dividend_coverage(self):
        frame = self.frame.copy()
        frame["dividend_yield"] = None
        result = LocalReferenceStrategyEngine(self.store).run(
            frame, market_as_of=self.market_date
        )
        value = result["strategies"]["低估值"]
        self.assertEqual(value["status"], "degraded")
        self.assertTrue(value["rows"])
        self.assertIn("股息率", value["reason"])

    def test_low_price_bull_uses_strict_twenty_and_known_growth_stability(self):
        frame = self.frame.copy()
        frame["close"] = frame["close"].astype(float)
        frame.loc[frame["symbol"] == "002001", "close"] = 20.0
        result = LocalReferenceStrategyEngine(self.store).run(
            frame, market_as_of=self.market_date
        )
        self.assertNotIn(
            "002001", [row["symbol"] for row in result["strategies"]["低价擒牛"]["rows"]]
        )

        frame.loc[frame["symbol"] == "002001", "close"] = 19.99
        frame.loc[
            frame["symbol"] == "002001", "net_profit_growth_positive_ratio"
        ] = 1 / 3
        result = LocalReferenceStrategyEngine(self.store).run(
            frame, market_as_of=self.market_date
        )
        self.assertNotIn(
            "002001", [row["symbol"] for row in result["strategies"]["低价擒牛"]["rows"]]
        )

        frame.loc[
            frame["symbol"] == "002001", "net_profit_growth_positive_ratio"
        ] = 2 / 3
        result = LocalReferenceStrategyEngine(self.store).run(
            frame, market_as_of=self.market_date
        )
        self.assertIn(
            "002001", [row["symbol"] for row in result["strategies"]["低价擒牛"]["rows"]]
        )


if __name__ == "__main__":
    unittest.main()

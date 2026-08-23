import os
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import _bootstrap  # noqa: F401
from analysis import factor_eval
from analysis import portfolio_backtest as pbt
from core.db_compat import _convert_placeholders


class PlaceholderConversionTest(unittest.TestCase):
    def test_quoted_comment_and_json_question_marks_are_preserved(self):
        sql = "SELECT '?' AS literal, payload ? 'key' FROM t WHERE id=? -- ?\nAND x=?"
        converted = _convert_placeholders(sql)
        self.assertEqual(converted.count("%s"), 2)
        self.assertIn("payload ? 'key'", converted)
        self.assertIn("'?' AS literal", converted)
        self.assertIn("-- ?", converted)


class FactorDateAlignmentTest(unittest.TestCase):
    def test_factor_eval_declares_and_uses_trade_date_alignment(self):
        dates = pd.bdate_range("2025-01-02", periods=180)
        frames = {}
        for pos in range(15):
            own_dates = dates.delete(pos % 7)
            returns = 0.0005 + pos * 0.00002 + np.sin(np.arange(len(own_dates)) / 11) * 0.0001
            close = 10 * np.cumprod(1 + returns)
            frames[f"{600000 + pos:06d}"] = pd.DataFrame({
                "date": own_dates, "open": close, "high": close * 1.01,
                "low": close * 0.99, "close": close, "volume": 1_000_000,
                "test_factor": np.arange(len(own_dates), dtype=float) + pos,
            })

        fake_factors = {"test": ("test", "test", 1, None)}

        def fake_compute(frame, _keys):
            return {"test": frame["test_factor"]}

        with patch("datahub.kline", side_effect=lambda code, *_a, **_k: frames[code]), \
                patch("factor_zoo.FACTORS", fake_factors), \
                patch("factor_zoo.compute", side_effect=fake_compute):
            result = factor_eval.evaluate(
                factor_keys=["test"], universe=list(frames), horizon=1,
                rebalance=10, period="1y", with_random=False,
            )
        self.assertNotIn("error", result)
        self.assertEqual(result["alignment"], "trade_date")
        self.assertGreaterEqual(result["n_points"], 5)


class PortfolioExecutionGuardTest(unittest.TestCase):
    @staticmethod
    def _bars(*, one_price_entry=False):
        dates = pd.bdate_range("2026-01-05", periods=5)
        open_values = [10.0, 11.0 if one_price_entry else 10.0, 8.0, 8.2, 8.3]
        high_values = [10.2, 11.0 if one_price_entry else 10.3, 8.5, 8.4, 8.5]
        low_values = [9.8, 11.0 if one_price_entry else 9.8, 7.0, 8.0, 8.1]
        close_values = [10.0, 11.0 if one_price_entry else 10.0, 8.1, 8.3, 8.4]
        return pd.DataFrame({
            "date": dates, "open": open_values, "high": high_values,
            "low": low_values, "close": close_values, "volume": 1_000_000,
        })

    def test_gap_down_stop_uses_open_not_unreachable_stop_price(self):
        with patch.object(pbt, "_trigger_dates", return_value=["2026-01-05"]):
            result = pbt.portfolio_backtest(
                [("600001", "样本")], "2026-01-05", "2026-01-09",
                hold_days=4, stop_pct=8, target_pct=None, benchmark=None,
                df_fetcher=lambda *_a: self._bars(), max_workers=1,
            )
        self.assertEqual(result["summary"]["trade_count"], 1)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "stop")
        self.assertLess(trade["exit_price"], 8.01)

    def test_one_price_limit_up_entry_is_not_filled(self):
        with patch.object(pbt, "_trigger_dates", return_value=["2026-01-05"]):
            result = pbt.portfolio_backtest(
                [("600001", "样本")], "2026-01-05", "2026-01-09",
                benchmark=None, df_fetcher=lambda *_a: self._bars(one_price_entry=True),
                max_workers=1,
            )
        self.assertEqual(result["summary"]["trade_count"], 0)


@unittest.skipUnless(os.getenv("RUN_POSTGRES_INTEGRATION") == "1",
                     "PostgreSQL integration is enabled in CI")
class PostgresResearchIntegrationTest(unittest.TestCase):
    def test_v4_schema_and_qmark_transactions_are_native_postgres_safe(self):
        from data.research_store import ResearchStore

        store = ResearchStore()
        conn = store.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT version FROM research_schema_migrations WHERE version=?",
                ("4-manifest-dependency-lock",),
            )
            self.assertEqual(cur.fetchone()[0], "4-manifest-dependency-lock")
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_name IN (?,?,?)",
                ("selection_artifacts", "selection_input_manifests",
                 "research_market_observations"),
            )
            self.assertEqual(int(cur.fetchone()[0]), 3)
        finally:
            conn.close()

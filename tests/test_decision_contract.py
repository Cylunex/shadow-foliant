import unittest
from unittest import mock

import numpy as np
import pandas as pd

from analysis import decision_signal
from analysis.decision_signal import is_material_transition
from analysis.market_breadth import evaluate_quotes
from analysis.multi_timeframe import evaluate, resample_calendar_week
from analysis.trade_plan import build_trade_plan


def _trend_frame(rows=220):
    dates = pd.bdate_range("2025-01-02", periods=rows)
    close = np.linspace(10.0, 20.0, rows)
    close[-1] = close[-2] + 1.0
    volume = np.full(rows, 1000.0)
    volume[-1] = 1800.0
    return pd.DataFrame({
        "Open": close - 0.15,
        "High": close + 0.35,
        "Low": close - 0.35,
        "Close": close,
        "Volume": volume,
    }, index=dates)


class CalendarWeekTests(unittest.TestCase):
    def test_calendar_week_respects_holiday_boundary(self):
        frame = pd.DataFrame({
            "Open": [10, 11, 12], "High": [11, 12, 13], "Low": [9, 10, 11],
            "Close": [10.5, 11.5, 12.5], "Volume": [100, 200, 300],
        }, index=pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"]))
        weekly = resample_calendar_week(frame)
        self.assertEqual(len(weekly), 2)
        self.assertEqual(float(weekly.iloc[0]["Close"]), 11.5)
        self.assertEqual(float(weekly.iloc[0]["Volume"]), 300.0)

    def test_uptrend_reports_weekly_daily_resonance(self):
        result = evaluate(_trend_frame())
        self.assertTrue(result["available"])
        self.assertEqual(result["weekly_regime"], "bullish")
        self.assertEqual(result["resonance"], "confirmed")


class TradePlanTests(unittest.TestCase):
    def test_plan_exposes_deterministic_contract_and_rr_gate(self):
        plan = build_trade_plan(
            "600001", _trend_frame(), name="测试股",
            market_signal={"action": "hold", "action_cn": "持有"},
        )
        self.assertTrue(plan["available"])
        for key in ("entry_low", "entry_high", "stop_loss", "target_price",
                    "risk_reward_ratio", "suggested_position_pct", "evidence", "blockers"):
            self.assertIn(key, plan)
        self.assertLess(plan["stop_loss"], plan["entry_low"])
        if plan["action"] == "buy":
            self.assertGreaterEqual(plan["risk_reward_ratio"], 2.0)

    def test_pattern_target_never_replaces_first_target_or_rr(self):
        technical = {
            "available": True, "score": 2, "grade": "neutral",
            "positives": ["确认看涨形态:箱体突破"], "risks": [],
            "donchian": {},
            "confirmed_patterns": [{
                "name": "箱体突破", "direction": "bullish",
                "status": "confirmed", "measured_target": 100.0,
            }],
        }
        plan = build_trade_plan("600001", _trend_frame(), technical_state=technical)
        self.assertEqual(plan["measured_pattern_target"], 100.0)
        self.assertNotEqual(plan.get("target_price"), 100.0)
        if plan.get("target_price_2") is not None:
            self.assertGreater(plan["target_price_2"], plan["target_price"])


class MarketBreadthTests(unittest.TestCase):
    def test_breadth_requires_coverage_and_calculates_cross_section(self):
        quotes = {f"{i:06d}": {"change_pct": 1 if i < 70 else -1} for i in range(120)}
        result = evaluate_quotes(quotes, expected=120)
        self.assertTrue(result["available"])
        self.assertEqual(result["up_count"], 70)
        self.assertAlmostEqual(result["up_ratio"], 70 / 120, places=4)

    def test_small_sample_is_not_misrepresented_as_market_breadth(self):
        result = evaluate_quotes({"000001": {"change_pct": 1}}, expected=500)
        self.assertFalse(result["available"])


class SignalTransitionTests(unittest.TestCase):
    def test_only_cross_risk_state_is_material(self):
        self.assertFalse(is_material_transition("hold", "watch"))
        self.assertFalse(is_material_transition("buy", "add"))
        self.assertTrue(is_material_transition("watch", "buy"))
        self.assertTrue(is_material_transition("buy", "reduce"))

    def test_low_sample_outcome_never_enters_prompt_feedback(self):
        bucket = {'hit': 3, 'miss': 0}
        calibrated = decision_signal.calibrate_outcome_bucket(bucket)
        self.assertEqual(calibrated['sample_status'], 'observational')
        self.assertIsNone(calibrated['posterior_hit_rate_pct'])
        self.assertEqual(calibrated['performance_factor'], 1.0)

    def test_sufficient_outcome_uses_beta_shrinkage_and_bounded_weight(self):
        calibrated = decision_signal.calibrate_outcome_bucket({'hit': 30, 'miss': 0})
        self.assertEqual(calibrated['sample_status'], 'evaluated')
        self.assertEqual(calibrated['posterior_hit_rate_pct'], 75.0)
        self.assertGreater(calibrated['performance_factor'], 1.0)
        self.assertLessEqual(calibrated['performance_factor'], 1.2)

    def test_feedback_text_enforces_thirty_samples_even_if_caller_asks_for_less(self):
        fake = {'buckets': [{
            'bucket': 'buy', 'bucket_cn': '买入', 'hit': 3, 'miss': 0,
        }]}
        with mock.patch.object(decision_signal, 'outcome_stats', return_value=fake):
            self.assertEqual(decision_signal.feedback_text(min_n=1), '')


if __name__ == "__main__":
    unittest.main()

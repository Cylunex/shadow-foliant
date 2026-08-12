import unittest

import numpy as np
import pandas as pd

from analysis.pattern_recognition import PatternDetector
from analysis.technical_state import (
    analyze_donchian,
    analyze_risk_quality,
    analyze_technical_state,
    analyze_trend_quality,
    analyze_volume_price,
)


def _frame(close, volume=None, high=None, low=None):
    close = np.asarray(close, dtype=float)
    volume = np.asarray(volume if volume is not None else np.full(len(close), 100.0), dtype=float)
    high = np.asarray(high if high is not None else close + 0.2, dtype=float)
    low = np.asarray(low if low is not None else close - 0.2, dtype=float)
    dates = pd.bdate_range("2025-01-02", periods=len(close))
    return pd.DataFrame({
        "Open": close - 0.05, "High": high, "Low": low,
        "Close": close, "Volume": volume,
    }, index=dates)


class PatternFallbackTests(unittest.TestCase):
    def test_composite_box_breakout_works_without_talib(self):
        box = 10.0 + np.sin(np.arange(20) * np.pi / 2) * 0.10
        close = np.r_[np.full(60, 10.0), box, 10.2]
        detector = PatternDetector()
        detector.talib_available = False
        result = detector.detect_all(_frame(close))
        hits = [item for item in result.values() if isinstance(item, dict) and item.get("found")]
        box = next(item for item in hits if item.get("name") == "箱体突破")
        self.assertEqual(box["status"], "confirmed")
        self.assertEqual(box["direction"], "bullish")
        self.assertGreater(box["measured_target"], close[-1])

    def test_failed_composite_is_visible_but_never_a_hit(self):
        # 直接验证统一合同：失效形态保留给 Agent 解释，但不会被筛选器当作命中。
        from analysis.pattern_recognition import _pattern
        data = _frame(np.linspace(10.0, 11.0, 40)).copy()
        data = data.rename(columns={"Close": "close"})
        data["date"] = data.index
        item = _pattern("测试形态", "bullish", 5, 30, data, status="failed",
                        breakout=11.0, invalidation=9.5, measured_target=12.0)
        self.assertEqual(item["status"], "failed")
        self.assertFalse(item["found"])


class TechnicalStateTests(unittest.TestCase):
    def test_donchian_channel_excludes_current_bar(self):
        close = np.r_[np.full(59, 10.0), 10.6]
        high = np.r_[np.full(59, 10.2), 12.0]
        low = np.r_[np.full(59, 9.8), 10.1]
        result = analyze_donchian(_frame(close, high=high, low=low))
        self.assertTrue(result["available"])
        self.assertEqual(result["upper20"], 10.2)
        self.assertEqual(result["status"], "breakout_55")

    def test_price_volume_and_obv_are_structured(self):
        close = np.linspace(10.0, 12.0, 30)
        volume = np.r_[np.full(27, 100.0), np.full(3, 200.0)]
        result = analyze_volume_price(_frame(close, volume=volume))
        self.assertEqual(result["state"], "price_up_volume_up")
        self.assertTrue(result["volume_breakout"])
        self.assertIn(result["obv_divergence"], {"none", "bullish", "bearish"})

    def test_trend_quality_is_price_normalized(self):
        first = analyze_trend_quality(_frame(np.linspace(10.0, 15.0, 80)))
        second = analyze_trend_quality(_frame(np.linspace(100.0, 150.0, 80)))
        self.assertAlmostEqual(first["slope_pct_20"], second["slope_pct_20"], places=4)
        self.assertGreater(first["r2_20"], 0.99)

    def test_technical_score_is_bounded(self):
        close = np.linspace(10.0, 20.0, 90)
        close[-1] += 2.0
        result = analyze_technical_state(_frame(close, volume=np.r_[np.full(89, 100.0), 500.0]))
        self.assertGreaterEqual(result["score"], -8)
        self.assertLessEqual(result["score"], 8)

    def test_low_adx_downweights_apparent_channel_breakout(self):
        close = 10 + np.sin(np.arange(80) * 1.5) * 0.05
        close[-1] = max(close[-21:-1]) + 0.30
        result = analyze_technical_state(_frame(close))
        self.assertEqual(result["trend_quality"]["regime"], "sideways")
        self.assertIn(result["donchian"]["status"], {"breakout_20", "breakout_55"})
        self.assertTrue(any("ADX偏低" in item for item in result["risks"]))

    def test_low_volatility_quality_exposes_bounded_risk_features(self):
        close = np.linspace(10.0, 10.8, 60)
        result = analyze_risk_quality(_frame(close))
        self.assertTrue(result['available'])
        self.assertTrue(result['low_vol_quality'])
        self.assertGreaterEqual(result['max_drawdown_20d_pct'], -8.0)
        self.assertLessEqual(result['atr_20_pct'], 4.5)
        self.assertIn('consolidation_days_20d', result)

    def test_volatile_drawdown_is_flagged_as_high_risk(self):
        close = np.r_[np.linspace(10.0, 14.0, 50),
                      [13.0, 11.0, 12.5, 9.5, 11.5, 8.5, 10.5, 8.0, 9.0, 7.5]]
        result = analyze_technical_state(_frame(close))
        self.assertTrue(result['risk_quality']['high_risk'])
        self.assertTrue(any('波动/回撤偏高' in item for item in result['risks']))


if __name__ == "__main__":
    unittest.main()

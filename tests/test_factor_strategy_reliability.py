import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from analysis.factor_eval import _residualize_cross_section
from analysis.factor_zoo import f_amihud
from analysis.multi_factor_screener import (
    _family_balanced_fallback,
    pv_ic_weights,
)


class FactorReliabilityTest(unittest.TestCase):
    def test_residualization_removes_available_exposures(self):
        beta = np.linspace(0.6, 1.5, 12)
        size = np.exp(np.linspace(16, 20, 12))
        industries = ["A"] * 6 + ["B"] * 6
        values = 2.0 * beta + 0.3 * np.log(size)
        residual, applied = _residualize_cross_section(
            values, beta, industries, size
        )
        self.assertEqual(set(applied), {"industry", "beta", "size"})
        self.assertLess(abs(np.corrcoef(residual, beta)[0, 1]), 1e-8)
        self.assertLess(abs(np.corrcoef(residual, np.log(size))[0, 1]), 1e-8)

    def test_ic_weights_reject_wrong_direction_and_balance_families(self):
        report = {"factors": [
            {"key": "mom1", "category": "动量", "rank_ic": .05, "ic_ir": .8,
             "verdict": "✅有效"},
            {"key": "mom2", "category": "动量", "rank_ic": .04, "ic_ir": .7,
             "verdict": "⚠️弱"},
            {"key": "mom3", "category": "动量", "rank_ic": .03, "ic_ir": .6,
             "verdict": "✅有效"},
            {"key": "risk1", "category": "波动", "rank_ic": .03, "ic_ir": .5,
             "verdict": "✅有效"},
            {"key": "wrong", "category": "波动", "rank_ic": -.10, "ic_ir": 1.2,
             "verdict": "❌方向相反"},
        ]}
        with patch("cache.cache_get", return_value=report):
            weights = pv_ic_weights()
        self.assertIsNotNone(weights)
        self.assertNotIn("mom3", weights)
        self.assertNotIn("wrong", weights)
        self.assertAlmostEqual(weights["mom1"] + weights["mom2"], 0.5)
        self.assertAlmostEqual(weights["risk1"], 0.5)

    def test_fallback_equalizes_families_not_factor_count(self):
        weights = _family_balanced_fallback({
            "m1": ("m1", "动量", 1, None),
            "m2": ("m2", "动量", 1, None),
            "r1": ("r1", "风险", -1, None),
        })
        self.assertAlmostEqual(weights["m1"] + weights["m2"], 0.5)
        self.assertAlmostEqual(weights["r1"], 0.5)

    def test_amihud_uses_traded_amount(self):
        close = pd.Series(np.linspace(10, 12, 30))
        base = pd.DataFrame({"close": close, "volume": 1_000_000})
        low_amount = base.assign(amount=1_000_000.0)
        high_amount = base.assign(amount=10_000_000.0)
        self.assertGreater(
            float(f_amihud(low_amount).dropna().iloc[-1]),
            float(f_amihud(high_amount).dropna().iloc[-1]),
        )


if __name__ == "__main__":
    unittest.main()

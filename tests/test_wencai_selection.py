import os
import unittest
from unittest.mock import patch

import pandas as pd

from selection.data_source_config import _normalize_wencai


class WencaiNormalizationTests(unittest.TestCase):
    def test_normalizes_decorated_and_duplicate_columns(self):
        raw = pd.DataFrame(
            [[
                "002521.SZ",
                "002521.SZ",
                "齐峰新材",
                "8.31",
                "126.5",
                "4200000000",
            ]],
            columns=[
                "股票代码",
                "股票代码",
                "股票简称",
                "收盘价:不复权[20260729]",
                "归属母公司股东的净利润(同比增长率)[20260331]",
                "总市值[20260729]",
            ],
        )

        result = _normalize_wencai(raw)

        self.assertIsNotNone(result)
        self.assertTrue(result.columns.is_unique)
        self.assertEqual(result.iloc[0]["code"], "002521")
        self.assertEqual(result.iloc[0]["name"], "齐峰新材")
        self.assertEqual(result.iloc[0]["price"], 8.31)
        self.assertEqual(result.iloc[0]["growth"], 126.5)
        self.assertEqual(result.iloc[0]["mcap"], 4200000000)

    def test_numeric_code_restores_leading_zero(self):
        raw = pd.DataFrame([{
            "股票代码": 2521,
            "股票简称": "齐峰新材",
            "最新价[20260729]": 8.31,
        }])

        result = _normalize_wencai(raw)

        self.assertEqual(result.iloc[0]["code"], "002521")

    def test_missing_price_is_not_misreported_as_success(self):
        raw = pd.DataFrame([{"股票代码": "600519.SH", "股票简称": "贵州茅台"}])
        self.assertIsNone(_normalize_wencai(raw))


class WencaiSelectorRequestTests(unittest.TestCase):
    def test_low_price_selector_requests_wencai_once(self):
        from selection import low_price_bull_selector as module

        with patch.object(module, "pywencai_get", return_value=None) as get:
            ok, _, _ = module.LowPriceBullSelector().get_low_price_stocks(top_n=5)

        self.assertFalse(ok)
        get.assert_called_once()

    def test_selector_returns_unique_canonical_code(self):
        from selection import low_price_bull_selector as module

        raw = pd.DataFrame(
            [["002521.SZ", "002521.SZ", "齐峰新材", 8.31, 126.5]],
            columns=[
                "股票代码",
                "股票代码",
                "股票简称",
                "收盘价:不复权[20260729]",
                "归属母公司股东的净利润(同比增长率)[20260331]",
            ],
        )
        with patch.object(module, "pywencai_get", return_value=raw) as get:
            ok, result, _ = module.LowPriceBullSelector().get_low_price_stocks(top_n=5)

        self.assertTrue(ok)
        self.assertTrue(result.columns.is_unique)
        self.assertEqual(result.iloc[0]["code"], "002521")
        get.assert_called_once()

    def test_small_cap_selector_requests_wencai_once(self):
        from selection import small_cap_selector as module

        with patch("data.pywencai_safe.pywencai_get", return_value=None) as get:
            ok, _, _ = module.SmallCapSelector().get_small_cap_stocks(top_n=5)

        self.assertFalse(ok)
        get.assert_called_once()

    def test_profit_growth_selector_requests_wencai_once(self):
        from selection import profit_growth_selector as module

        with patch("data.pywencai_safe.pywencai_get", return_value=None) as get:
            ok, _, _ = module.ProfitGrowthSelector().get_profit_growth_stocks(top_n=5)

        self.assertFalse(ok)
        get.assert_called_once()

    def test_value_selector_requests_wencai_once(self):
        from selection import value_stock_selector as module

        with patch.object(module, "pywencai_get", return_value=None) as get:
            ok, _, _ = module.ValueStockSelector().get_value_stocks(top_n=5)

        self.assertFalse(ok)
        get.assert_called_once()


class WencaiRetryScheduleTests(unittest.TestCase):
    def test_retry_job_is_soft_enrichment(self):
        from jobs.automation_config import REGISTRY

        retry = REGISTRY["strategy_prefetch_retry"]
        self.assertTrue(retry["default"])
        self.assertNotIn("depends_on", retry)
        self.assertNotIn("depends_on", REGISTRY["unified_selection"])


class WencaiCookieTests(unittest.TestCase):
    def test_environment_cookie_is_forwarded_without_logging_value(self):
        from data.sources import pywencai as source

        source._streak_fail = 0
        result = pd.DataFrame([{"股票代码": "600519.SH"}])
        with patch.dict(os.environ, {"PYWENCAI_COOKIE": "sensitive-cookie"}):
            self.assertTrue(source.cookie_configured())
            with patch.object(source.pywencai, "get", return_value=result) as get:
                actual = source.pywencai_get("贵州茅台最新价", timeout=2, loop=False)

        self.assertIs(actual, result)
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs["cookie"], "sensitive-cookie")


if __name__ == "__main__":
    unittest.main()

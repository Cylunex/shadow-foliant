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
        self.assertFalse(get.call_args.kwargs["loop"])

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
        self.assertFalse(get.call_args.kwargs["loop"])

    def test_small_cap_selector_requests_wencai_once(self):
        from selection import small_cap_selector as module

        with patch("data.pywencai_safe.pywencai_get", return_value=None) as get:
            ok, _, _ = module.SmallCapSelector().get_small_cap_stocks(top_n=5)

        self.assertFalse(ok)
        get.assert_called_once()
        self.assertFalse(get.call_args.kwargs["loop"])

    def test_profit_growth_selector_requests_wencai_once(self):
        from selection import profit_growth_selector as module

        with patch("data.pywencai_safe.pywencai_get", return_value=None) as get:
            ok, _, _ = module.ProfitGrowthSelector().get_profit_growth_stocks(top_n=5)

        self.assertFalse(ok)
        get.assert_called_once()
        self.assertFalse(get.call_args.kwargs["loop"])

    def test_value_selector_requests_wencai_once(self):
        from selection import value_stock_selector as module

        with patch.object(module, "pywencai_get", return_value=None) as get:
            ok, _, _ = module.ValueStockSelector().get_value_stocks(top_n=5)

        self.assertFalse(ok)
        get.assert_called_once()
        self.assertFalse(get.call_args.kwargs["loop"])

    def test_main_force_requests_only_first_page(self):
        from selection import main_force_selector as module

        raw = pd.DataFrame([{
            "股票代码": "600519.SH",
            "股票简称": "贵州茅台",
            "主力资金流向": 1000000,
        }])
        with patch.object(module, "_throttle"), patch.object(
            module, "pywencai_get", return_value=raw
        ) as get:
            ok, result, _ = module.MainForceStockSelector().get_main_force_stocks(
                days_ago=5
            )

        self.assertTrue(ok)
        self.assertEqual(len(result), 1)
        get.assert_called_once()
        self.assertFalse(get.call_args.kwargs["loop"])
        self.assertEqual(get.call_args.args[0], "主力资金净流入排名")

    def test_main_force_filters_st_and_star_market_locally(self):
        from selection import main_force_selector as module

        raw = pd.DataFrame([
            {"股票代码": "600519.SH", "股票简称": "贵州茅台", "主力资金流向": 3},
            {"股票代码": "688001.SH", "股票简称": "华兴源创", "主力资金流向": 2},
            {"股票代码": "600001.SH", "股票简称": "ST测试", "主力资金流向": 1},
        ])
        with patch.object(module, "_throttle"), patch.object(
            module, "pywencai_get", return_value=raw
        ):
            ok, result, _ = module.MainForceStockSelector().get_main_force_stocks(
                days_ago=5
            )

        self.assertTrue(ok)
        self.assertEqual(result["股票代码"].tolist(), ["600519.SH"])


class WencaiRetryScheduleTests(unittest.TestCase):
    def test_retry_job_is_soft_enrichment(self):
        from jobs.automation_config import REGISTRY

        retry = REGISTRY["strategy_prefetch_retry"]
        self.assertTrue(retry["default"])
        self.assertNotIn("depends_on", retry)
        self.assertNotIn("depends_on", REGISTRY["unified_selection"])
        self.assertIn("主力资金", retry["description"])

    def test_wencai_sources_are_explicitly_labeled(self):
        from jobs.jobs_hub import _WENCAI_SOURCE_LABELS

        self.assertEqual(_WENCAI_SOURCE_LABELS["主力资金"], "问财·主力")
        self.assertEqual(_WENCAI_SOURCE_LABELS["小市值"], "问财·小市值")

    def test_prefetch_runs_main_force_before_other_wencai_queries(self):
        from jobs import jobs_hub as module
        import mx_strategies

        calls = []
        with patch.object(module, "_skip_if_not_trading", return_value=False), \
                patch.object(
                    module,
                    "_prefetch_main_force",
                    side_effect=lambda **_kwargs: calls.append("main_force") or 0,
                ), \
                patch.object(
                    module,
                    "_prefetch_wencai_strategies",
                    side_effect=lambda **_kwargs: calls.append("others") or 0,
                ), \
                patch.object(module, "_log_run") as log_run, \
                patch.object(mx_strategies, "MX_STRATEGIES", []):
            module.task_strategy_prefetch()

        self.assertEqual(calls, ["main_force", "others"])
        self.assertEqual(log_run.call_args.args[:2], ("strategy_prefetch", "skipped"))
        self.assertIn("source_unavailable 0/5", log_run.call_args.kwargs["error"])

    def test_retry_also_prioritizes_main_force(self):
        from jobs import jobs_hub as module

        calls = []
        with patch.object(module, "_skip_if_not_trading", return_value=False), \
                patch.object(
                    module,
                    "_prefetch_main_force",
                    side_effect=lambda **kwargs: calls.append(
                        ("main_force", kwargs["use_cache"])
                    ) or 0,
                ), \
                patch.object(
                    module,
                    "_prefetch_wencai_strategies",
                    side_effect=lambda **kwargs: calls.append(
                        ("others", kwargs["use_cache"])
                    ) or 0,
                ), \
                patch.object(module, "_log_run") as log_run:
            module.task_strategy_prefetch_retry()

        self.assertEqual(calls, [("main_force", True), ("others", True)])
        self.assertEqual(log_run.call_args.args[:2], ("strategy_prefetch_retry", "skipped"))
        self.assertIn("source_unavailable 0/5", log_run.call_args.kwargs["error"])


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
        self.assertEqual(get.call_args.kwargs["retry"], 2)
        self.assertEqual(get.call_args.kwargs["sleep"], 1)

    def test_cookie_is_optional_and_not_forwarded_when_absent(self):
        from data.sources import pywencai as source

        source._streak_fail = 0
        with patch.dict(
            os.environ,
            {"PYWENCAI_COOKIE": "", "WENCAI_COOKIE": ""},
            clear=False,
        ):
            with patch.object(source.pywencai, "get", return_value=None) as get:
                source.pywencai_get("贵州茅台最新价", timeout=2, loop=False)

        self.assertNotIn("cookie", get.call_args.kwargs)


class WencaiHttpsCompatibilityTests(unittest.TestCase):
    def test_rewrites_only_iwencai_http_url(self):
        from data.sources import pywencai as source

        response = object()
        with patch.object(
            source._HTTPS_REQUESTS._requests,
            "request",
            return_value=response,
        ) as request:
            actual = source._HTTPS_REQUESTS.request(
                method="POST",
                url="http://www.iwencai.com/customized/chart/get-robot-data",
            )

        self.assertIs(actual, response)
        self.assertEqual(
            request.call_args.kwargs["url"],
            "https://www.iwencai.com/customized/chart/get-robot-data",
        )

    def test_leaves_other_hosts_unchanged(self):
        from data.sources import pywencai as source

        with patch.object(source._HTTPS_REQUESTS._requests, "request") as request:
            request.return_value.status_code = 200
            source._HTTPS_REQUESTS.request("GET", "http://example.com/data")

        self.assertEqual(request.call_args.args[1], "http://example.com/data")

    def test_http_rejection_is_not_treated_as_empty_business_result(self):
        from data.sources import pywencai as source

        source._streak_fail = 0

        def rejected(**_kwargs):
            source._HTTPS_REQUESTS._state.last_status = 403
            return None

        with patch.object(source.pywencai, "get", side_effect=rejected):
            with self.assertRaises(source.PyWencaiRequestRejected) as raised:
                source.pywencai_get("贵州茅台最新价", timeout=2, loop=False)

        self.assertIn("HTTP 403", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

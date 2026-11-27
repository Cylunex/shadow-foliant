import unittest
from unittest.mock import call, patch

import pandas as pd

import _bootstrap  # noqa: F401
from data.stock_data import StockDataFetcher


class ChineseFinancialResilienceTest(unittest.TestCase):
    def test_empty_report_response_stops_repeated_statement_requests(self):
        fetcher = StockDataFetcher()
        with patch(
            "data.datahub.financials", return_value=[],
        ) as statements, patch(
            "data.stock_data.ak.stock_financial_abstract",
            side_effect=TypeError("NoneType is not subscriptable"),
        ) as ratios:
            result = fetcher._get_chinese_financial_data("600519")

        statements.assert_called_once_with("600519", "fzb")
        self.assertEqual(ratios.call_count, 1)
        self.assertEqual(result["source_status"], "degraded")
        self.assertEqual(
            [item["section"] for item in result["source_errors"]],
            ["资产负债表", "financial_ratios"],
        )


    def test_three_statements_use_distinct_reports_and_keep_eight_periods(self):
        def rows(symbol, report):
            return [{"section": report, "period": i} for i in range(10)]

        ratios = pd.DataFrame([{"指标": "流动比率", "20260930": 2}])
        with patch("data.datahub.financials", side_effect=rows) as statements, patch(
            "data.stock_data.ak.stock_financial_abstract", return_value=ratios,
        ):
            result = StockDataFetcher()._get_chinese_financial_data("600519")
        self.assertEqual(statements.call_args_list, [call("600519", r) for r in ("fzb", "lrb", "llb")])
        self.assertEqual(result["source_status"], "ok")
        for key, report in (("balance_sheet", "fzb"), ("income_statement", "lrb"), ("cash_flow", "llb")):
            self.assertEqual(result[key], rows("600519", report)[:8])
        self.assertEqual(result["financial_ratios"]["报告期"], "20260930")


if __name__ == "__main__":
    unittest.main()

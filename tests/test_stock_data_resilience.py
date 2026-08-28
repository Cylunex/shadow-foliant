import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from data.stock_data import StockDataFetcher


class ChineseFinancialResilienceTest(unittest.TestCase):
    def test_empty_ths_response_stops_repeated_statement_requests(self):
        fetcher = StockDataFetcher()
        with patch(
            "data.stock_data.ak.stock_financial_abstract_ths",
            side_effect=AttributeError("NoneType has no attribute string"),
        ) as statements, patch(
            "data.stock_data.ak.stock_financial_abstract",
            side_effect=TypeError("NoneType is not subscriptable"),
        ) as ratios:
            result = fetcher._get_chinese_financial_data("600519")

        self.assertEqual(statements.call_count, 1)
        self.assertEqual(ratios.call_count, 1)
        self.assertEqual(result["source_status"], "degraded")
        self.assertEqual(
            [item["section"] for item in result["source_errors"]],
            ["资产负债表", "financial_ratios"],
        )


if __name__ == "__main__":
    unittest.main()

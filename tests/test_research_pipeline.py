import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import _bootstrap  # noqa: F401
from analysis.local_stock_selector import LocalStockSelector, SelectionPolicy
from data.research_store import ResearchStore
from data.source_contracts import contracts, get_contract
from data.sources import zzshare


def _sqlite(path):
    return sqlite3.connect(path)


class SourceContractTest(unittest.TestCase):
    def test_published_and_operational_limits_are_explicit(self):
        matrix = contracts()
        self.assertEqual(matrix["zzshare"]["daily_symbol"]["hard_max_rows"], 1000)
        self.assertEqual(matrix["zzshare"]["daily_market"]["hard_max_rows"], 10000)
        self.assertGreaterEqual(matrix["zzshare"]["realtime"]["min_interval_seconds"], 3.0)
        self.assertEqual(matrix["eltdx"]["bars"]["page_size"], 800)
        self.assertEqual(matrix["baostock"]["daily"]["max_concurrency"], 1)
        self.assertTrue(matrix["zzshare"]["finance_pit"]["supports_pit"])
        self.assertEqual(matrix["pywencai"]["discovery"]["capability"],
                         "external_reference_only")

    def test_environment_cannot_loosen_upstream_boundary(self):
        with patch.dict(os.environ, {
            "SOURCE_ZZSHARE_REALTIME_MIN_INTERVAL_SECONDS": "0",
            "SOURCE_ZZSHARE_REALTIME_MAX_CONCURRENCY": "99",
            "SOURCE_ZZSHARE_REALTIME_RETRIES": "99",
        }):
            actual = get_contract("zzshare", "realtime")
        self.assertEqual(actual.min_interval_seconds, 3.1)
        self.assertEqual(actual.max_concurrency, 1)
        self.assertEqual(actual.retries, 1)

    def test_zzshare_pit_rejects_future_publication(self):
        class Api:
            def finance_pit(self, **kwargs):
                return pd.DataFrame([
                    {"code": "600000.SH", "pubDate": "2026-04-29", "roe": 12},
                    {"code": "000001.SZ", "pubDate": "2026-05-01", "roe": 15},
                ])

        zzshare._api = Api()
        try:
            with patch.dict(os.environ, {"ZZSHARE_TOKEN": "not-logged"}), \
                    patch("data.sources.zzshare.source_call"):
                result = zzshare.get_finance_pit("indicator", "2026-04-30")
        finally:
            zzshare._reset_for_tests()
        self.assertEqual(result["code"].tolist(), ["600000.SH"])


class ResearchStoreAndSelectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ResearchStore(os.path.join(self.tmp.name, "research.db"), connect_fn=_sqlite)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _provenance(as_of):
        return {
            "provider": "fixture", "origin": "test", "as_of": as_of,
            "effective_at": as_of, "retrieved_at": f"{as_of}T17:00:00+08:00",
            "adjustment": "qfq", "unit": "price/currency/shares",
            "schema_version": "1", "quality_status": "ok",
        }

    def _seed(self, count=24, history=400):
        end = pd.Timestamp("2026-08-21")
        dates = pd.bdate_range(end=end, periods=history)
        securities = pd.DataFrame([{
            "ts_code": f"{600000 + i:06d}.SH", "name": f"样本{i}", "exchange": "SSE",
            "market": "主板", "industry": f"行业{i % 6}", "list_status": "L",
            "list_date": "2010-01-01", "delist_date": "", "is_hs": "N",
        } for i in range(count)])
        securities.attrs["provenance"] = self._provenance(end.date().isoformat())
        self.store.upsert_securities(securities)

        records = []
        for i in range(count):
            base = 8 + i / 10
            trend = np.linspace(0, 2 + i / 20, history)
            wave = np.sin(np.arange(history) / 15) * 0.08
            for pos, day in enumerate(dates):
                close = base + trend[pos] + wave[pos]
                records.append({
                    "ts_code": f"{600000 + i:06d}.SH", "trade_date": day.date().isoformat(),
                    "open": close * 0.995, "high": close * 1.01, "low": close * 0.99,
                    "close": close, "volume": 1_000_000 + i * 1000 + pos * 10,
                    "turnover": close * 1_000_000, "turnover_rate": 1.2,
                    "is_paused": 0, "is_st": 0,
                })
        bars = pd.DataFrame(records)
        bars.attrs["provenance"] = self._provenance(end.date().isoformat())
        self.store.upsert_daily_bars(bars, adjustment="qfq")

        valuation = pd.DataFrame([{
            "code": f"{600000 + i:06d}.SH", "trade_date": end.date().isoformat(),
            "market_cap": 100 + i, "pe_ratio": 8 + i, "pb_ratio": 1 + i / 20,
            "turnover_ratio": 1.2,
        } for i in range(count)])
        valuation.attrs["provenance"] = self._provenance(end.date().isoformat())
        self.store.upsert_valuations(valuation, as_of=end.date().isoformat())

        frames = {
            "indicator": [{"roe": 8 + i, "net_profit_growth": i - 3} for i in range(count)],
            "income": [{"np_parent_company_owners": 100 + i * 3} for i in range(count)],
            "balance": [{"total_liability": 40 + i, "total_assets": 100 + i * 2} for i in range(count)],
            "cash_flow": [{"net_operate_cash_flow": 105 + i * 3} for i in range(count)],
        }
        for table, values in frames.items():
            frame = pd.DataFrame([{
                "code": f"{600000 + i:06d}.SH", "statDate": "2026-06-30",
                "pubDate": "2026-08-20", **values[i],
            } for i in range(count)])
            frame.attrs["provenance"] = self._provenance(end.date().isoformat())
            self.store.upsert_financial_pit(table, frame, as_of=end.date().isoformat())
        return end.date().isoformat()

    def test_future_finance_never_enters_pit_store(self):
        frame = pd.DataFrame([{
            "code": "600000.SH", "statDate": "2026-06-30",
            "pubDate": "2026-09-01", "roe": 99,
        }])
        frame.attrs["provenance"] = self._provenance("2026-08-21")
        self.assertEqual(self.store.upsert_financial_pit(
            "indicator", frame, as_of="2026-08-21"
        ), 0)

    def test_local_pipeline_is_primary_and_wencai_is_reference_only(self):
        selection_date = self._seed()
        policy = SelectionPolicy(
            fundamental_top_n=20, technical_top_n=12, diversified_top_n=8,
            final_n=6, min_history_days=60, preferred_history_days=400,
            max_per_industry=2, min_warehouse_coverage=0.8,
        )
        result = LocalStockSelector(self.store, policy).run(
            selection_date,
            wencai_reference=[
                {"symbol": "600023", "source_labels": ["问财·低估值"]},
                {"symbol": "000001", "source_labels": ["问财·小市值"]},
            ],
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["candidates"]), 6)
        self.assertFalse(result["comparison"]["reference_affects_score"])
        self.assertIn("000001", result["comparison"]["reference_only"])
        for candidate in result["candidates"]:
            self.assertEqual(candidate["source_labels"], ["本地PIT数据仓"])
            self.assertGreaterEqual(candidate["data_coverage"], 0.8)
        latest = self.store.latest_selection()
        self.assertEqual(latest["metadata"]["primary_pipeline"], "local_pit")

    def test_incomplete_warehouse_does_not_fall_back_to_wencai(self):
        result = LocalStockSelector(self.store).run(
            "2026-08-21", wencai_reference=[{"symbol": "600000"}]
        )
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["comparison"]["reference_only"], ["600000"])


if __name__ == "__main__":
    unittest.main()

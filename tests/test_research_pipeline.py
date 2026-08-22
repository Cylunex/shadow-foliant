import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import _bootstrap  # noqa: F401
from analysis.local_stock_selector import LocalStockSelector, SelectionPolicy
from analysis.selection_finalizer import finalize_local_selection
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

    def test_zzshare_generic_query_compatibility_for_new_shortcuts(self):
        class Api:
            def __init__(self):
                self.calls = []

            def query(self, path, params=None):
                self.calls.append((path, params or {}))
                if path == "market/trade/days":
                    return {"trade_days": ["2026-08-20", "2026-08-21"]}
                if "/valuation/" in path:
                    return [{"code": "600000.SH", "trade_date": "2026-08-21"}]
                return [{"code": "600000.SH", "pubDate": "2026-08-20"}]

        api = Api()
        zzshare._api = api
        try:
            with patch.dict(os.environ, {"ZZSHARE_TOKEN": "not-logged"}), \
                    patch("data.sources.zzshare.source_call"):
                days = zzshare.get_trade_days("2026-08-20", "2026-08-21")
                valuation = zzshare.get_valuation("2026-08-21")
                finance = zzshare.get_finance_pit("indicator", "2026-08-21")
        finally:
            zzshare._reset_for_tests()
        self.assertEqual(days, ["2026-08-20", "2026-08-21"])
        self.assertEqual(len(valuation), 1)
        self.assertEqual(len(finance), 1)
        self.assertEqual(api.calls[0][0], "market/trade/days")
        self.assertEqual(api.calls[1][0], "v3/fundamentals/valuation/2026-08-21")
        self.assertEqual(api.calls[2][0], "v3/fundamentals/indicator/pit/2026-08-21")

    def test_zzshare_security_master_normalizes_numeric_listed_status(self):
        class Api:
            def stock_basic(self, **kwargs):
                self.kwargs = kwargs
                return pd.DataFrame([{
                    "ts_code": "600000.SH", "name": "样本", "list_status": 1,
                }])

        api = Api()
        zzshare._api = api
        try:
            with patch.dict(os.environ, {"ZZSHARE_TOKEN": "not-logged"}), \
                    patch("data.sources.zzshare.source_call"):
                result = zzshare.get_security_master()
        finally:
            zzshare._reset_for_tests()
        self.assertEqual(result["list_status"].tolist(), ["L"])
        self.assertEqual(result["provider_list_status"].tolist(), [1])
        self.assertEqual(api.kwargs["list_status"], "L")

    def test_unknown_volume_unit_is_rejected_instead_of_guessed(self):
        raw = pd.DataFrame({
            "trade_date": pd.date_range("2026-08-17", periods=3),
            "open": [10, 10, 10], "high": [11, 11, 11],
            "low": [9, 9, 9], "close": [10, 10, 10],
            "vol": [100, 100, 100], "amount": [12_340, 12_340, 12_340],
        })
        result = zzshare._standardize(raw, time_col="trade_date", volume_col="vol")
        self.assertTrue(result["volume"].isna().all())
        self.assertEqual(result.attrs["quality_status"], "unknown_unit")

    def test_lot_volume_is_normalized_to_shares_only_when_validated(self):
        raw = pd.DataFrame({
            "trade_date": pd.date_range("2026-08-17", periods=3),
            "open": [10, 10, 10], "high": [11, 11, 11],
            "low": [9, 9, 9], "close": [10, 10, 10],
            "vol": [100, 200, 300], "amount": [100_000, 200_000, 300_000],
        })
        result = zzshare._standardize(raw, time_col="trade_date", volume_col="vol")
        self.assertEqual(result["volume"].tolist(), [10_000, 20_000, 30_000])
        self.assertEqual(result.attrs["volume_unit"], "shares")


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
        self.store.upsert_trade_days(day.date().isoformat() for day in dates)

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
        return (end + pd.offsets.BDay(1)).date().isoformat()

    def test_future_finance_never_enters_pit_store(self):
        frame = pd.DataFrame([{
            "code": "600000.SH", "statDate": "2026-06-30",
            "pubDate": "2026-09-01", "roe": 99,
        }])
        frame.attrs["provenance"] = self._provenance("2026-08-21")
        self.assertEqual(self.store.upsert_financial_pit(
            "indicator", frame, as_of="2026-08-21"
        ), 0)

    def test_finance_revisions_are_preserved_and_loaded_point_in_time(self):
        for as_of, pub_date, roe in (
            ("2026-08-20", "2026-08-20", 10),
            ("2026-08-21", "2026-08-21", 12),
        ):
            frame = pd.DataFrame([{
                "code": "600000.SH", "statDate": "2026-06-30",
                "pubDate": pub_date, "roe": roe,
            }])
            frame.attrs["provenance"] = self._provenance(as_of)
            self.assertEqual(self.store.upsert_financial_pit(
                "indicator", frame, as_of=as_of
            ), 1)
        early = self.store.load_financial_pit("indicator", "2026-08-20")
        late = self.store.load_financial_pit("indicator", "2026-08-21")
        self.assertEqual(early.iloc[0]["roe"], 10)
        self.assertEqual(late.iloc[0]["roe"], 12)

    def test_title_only_event_never_enters_formal_event_store(self):
        title_only = {
            "symbol": "600000", "event_type": "earnings",
            "event_date": "2026-08-20", "direction": -1,
            "confidence": 1, "materiality": 1, "surprise": -1,
            "confirmation_status": "title_only", "source": "cninfo",
        }
        self.assertEqual(self.store.save_events([title_only]), 0)
        self.assertTrue(self.store.load_events("2026-08-21").empty)
        confirmed = {
            **title_only, "confirmation_status": "confirmed",
            "document_id": "example-document", "novelty": 1,
        }
        self.assertEqual(self.store.save_events([confirmed]), 1)
        self.assertEqual(len(self.store.load_events("2026-08-21")), 1)

    def test_latest_master_is_only_available_through_ingestion_view(self):
        frame = pd.DataFrame([{
            "ts_code": "600000.SH", "name": "样本", "list_status": "L",
            "list_date": "2010-01-01", "delist_date": "",
        }])
        frame.attrs["provenance"] = self._provenance("2026-08-22")
        self.store.upsert_securities(frame)
        self.assertTrue(self.store.load_universe("2026-08-21").empty)
        self.assertEqual(
            self.store.load_latest_universe()["symbol"].tolist(), ["600000"]
        )

    def test_local_pipeline_is_primary_and_wencai_is_reference_only(self):
        selection_date = self._seed()
        policy = SelectionPolicy(
            fundamental_top_n=20, technical_top_n=12, diversified_top_n=8,
            final_n=6, min_history_days=60, preferred_history_days=400,
            max_per_industry=2, max_pairwise_correlation=1.0,
            min_warehouse_coverage=0.8,
        )
        result = LocalStockSelector(self.store, policy).run(
            selection_date,
            wencai_reference=[
                {"symbol": "600023", "source_labels": ["问财·低估值"]},
                {"symbol": "000001", "source_labels": ["问财·小市值"]},
            ],
        )
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(len(result["candidates"]), 6)
        self.assertFalse(result["comparison"]["reference_affects_score"])
        self.assertIn("000001", result["comparison"]["reference_only"])
        for candidate in result["candidates"]:
            self.assertEqual(candidate["source_labels"], ["本地PIT数据仓"])
            self.assertGreaterEqual(candidate["data_coverage"], 0.8)
        latest = self.store.latest_selection()
        self.assertEqual(latest["metadata"]["primary_pipeline"], "local_pit")

    def test_local_pipeline_accepts_pre_normalization_numeric_listed_status(self):
        selection_date = self._seed()
        conn = self.store.connect()
        try:
            conn.execute("UPDATE research_security_snapshots SET list_status='1'")
            conn.commit()
        finally:
            conn.close()
        result = LocalStockSelector(self.store).run(selection_date, persist=False)
        self.assertEqual(result["status"], "success", result)
        self.assertGreater(len(result["candidates"]), 0)

    def test_diversification_uses_board_buckets_when_industry_is_missing(self):
        policy = SelectionPolicy(max_per_industry=2, max_pairwise_correlation=1.0)
        selector = LocalStockSelector(self.store, policy)
        rng = np.random.default_rng(7)
        frame = pd.DataFrame([{
            "symbol": symbol, "industry": "未分类", "market": "",
            "return_vector_60": rng.normal(0, 0.01, 60), "total_score": 100 - pos,
        } for pos, symbol in enumerate([
            "600001", "600002", "600003", "000001", "000002", "000003",
            "300001", "300002", "300003",
        ])])
        result = selector._diversify(frame)
        self.assertEqual(len(result), 6)
        self.assertEqual(result["symbol"].str.startswith("6").sum(), 2)
        self.assertEqual(result["symbol"].str.startswith("0").sum(), 2)
        self.assertEqual(result["symbol"].str.startswith("3").sum(), 2)

    def test_diversification_rejects_highly_correlated_candidates(self):
        selector = LocalStockSelector(
            self.store,
            SelectionPolicy(max_per_industry=5, max_pairwise_correlation=0.8),
        )
        base = np.linspace(-0.01, 0.01, 60)
        frame = pd.DataFrame([
            {"symbol": "600001", "industry": "甲", "return_vector_60": base},
            {"symbol": "600002", "industry": "乙", "return_vector_60": base * 1.01},
            {"symbol": "600003", "industry": "丙", "return_vector_60": base[::-1]},
        ])
        result = selector._diversify(frame)
        self.assertEqual(result["symbol"].tolist(), ["600001", "600003"])

    def test_incomplete_warehouse_does_not_fall_back_to_wencai(self):
        result = LocalStockSelector(self.store).run(
            "2026-08-21", wencai_reference=[{"symbol": "600000"}]
        )
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["comparison"]["reference_only"], ["600000"])

    def test_stale_market_snapshot_fails_closed(self):
        self._seed()
        self.store.upsert_trade_days(["2026-08-24"])
        result = LocalStockSelector(self.store).run("2026-08-25", persist=False)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["metadata"]["reason"], "local market snapshot is stale")

    def test_final_top_five_ignores_llm_and_quote_fields(self):
        rows = [
            {"code": "600001", "score": 80, "rank": 2,
             "debate_verdict": "否决", "change_pct": 9},
            {"code": "600002", "score": 90, "rank": 1,
             "debate_verdict": "买入", "change_pct": -2},
        ]
        result = finalize_local_selection(rows, limit=2)
        self.assertEqual([row["code"] for row in result], ["600002", "600001"])
        self.assertTrue(all(row["ranking_source"] == "local_pit_snapshot" for row in result))


if __name__ == "__main__":
    unittest.main()

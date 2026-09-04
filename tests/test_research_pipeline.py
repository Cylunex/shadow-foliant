import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import _bootstrap  # noqa: F401
from analysis.local_stock_selector import LocalStockSelector, SelectionPolicy
from analysis.selection_finalizer import finalize_local_selection
from core.research_health import snapshot as research_health_snapshot
from data.research_sync import (
    ResearchSourceUnavailable, ResearchSynchronizer,
    _calendar_chunk_quality, _fetch_calendar_sources,
)
from data.research_store import ResearchStore, _iso_date
from data.source_contracts import contracts, get_contract
from data.sources import baostock as baostock_source, zzshare


def _sqlite(path):
    return sqlite3.connect(path)


class SourceContractTest(unittest.TestCase):
    def test_missing_dates_are_rejected_before_calendar_comparison(self):
        quality, detail = _calendar_chunk_quality(
            [(pd.NaT, True), ('2026-08-21', True)], '2026-08-20', '2026-08-21'
        )
        self.assertEqual(_iso_date(pd.NaT), '')
        self.assertEqual(quality, 'incomplete')
        self.assertEqual(detail['invalid_row_count'], 1)
        self.assertIn('invalid_calendar_rows', detail['reasons'])

    def test_calendar_sources_share_one_hard_deadline(self):
        def slow_source(*_args):
            time.sleep(0.12)
            return [('2026-08-21', True)]

        started = time.monotonic()
        with patch('data.research_sync.zzshare.get_trade_calendar_evidence', slow_source), \
                patch('data.research_sync.baostock.trade_calendar_evidence',
                      return_value=[('2026-08-21', True)]):
            evidence, failures = _fetch_calendar_sources(
                '2026-08-21', '2026-08-21', timeout_seconds=0.02
            )
        self.assertLess(time.monotonic() - started, 0.10)
        self.assertEqual(evidence['zzshare'], [])
        self.assertEqual(failures['zzshare'], 'source_timeout')
        self.assertEqual(evidence['baostock'], [('2026-08-21', True)])

    def test_daily_sync_uses_complete_same_day_calendar_cache_on_transient_failure(self):
        class Store:
            def calendar_consensus(self, cutoff, *, inclusive=False):
                self.called = (cutoff, inclusive)
                return {
                    'ready': True, 'coverage_through_date': '2026-08-24',
                    'latest_confirmed_open_date': '2026-08-24',
                }

        store = Store()
        syncer = ResearchSynchronizer(store=store)
        with patch.object(syncer, 'sync_calendar', side_effect=RuntimeError('transient')):
            result = syncer.refresh_calendar_for_day('2026-08-24')
        self.assertEqual(result['quality_status'], 'cached')
        self.assertEqual(result['refresh_error_type'], 'RuntimeError')
        self.assertEqual(store.called, ('2026-08-24', True))

    def test_daily_sync_rejects_stale_calendar_cache(self):
        class Store:
            def calendar_consensus(self, *_args, **_kwargs):
                return {'ready': True, 'coverage_through_date': '2026-08-21'}

        syncer = ResearchSynchronizer(store=Store())
        with patch.dict(os.environ, {'RESEARCH_CALENDAR_RETRY_DELAY_SECONDS': '0'}), \
                patch.object(syncer, 'sync_calendar',
                             side_effect=RuntimeError('transient')) as refresh:
            with self.assertRaisesRegex(RuntimeError, 'same-day consensus cache'):
                syncer.refresh_calendar_for_day('2026-08-24')
        self.assertEqual(refresh.call_count, 3)

    def test_baostock_daily_breaker_does_not_block_calendar(self):
        original = {
            capability: dict(state)
            for capability, state in baostock_source._BREAKER_STATE.items()
        }
        try:
            for state in baostock_source._BREAKER_STATE.values():
                state.update(fail_count=0, last_fail=0.0)
            baostock_source._mark_fail('daily')
            baostock_source._mark_fail('daily')
            self.assertTrue(baostock_source._in_cooldown('daily'))
            self.assertFalse(baostock_source._in_cooldown('calendar'))
            self.assertTrue(baostock_source.breaker_open())
        finally:
            baostock_source._BREAKER_STATE.clear()
            baostock_source._BREAKER_STATE.update(original)

    def test_published_and_operational_limits_are_explicit(self):
        matrix = contracts()
        self.assertEqual(matrix["zzshare"]["daily_symbol"]["hard_max_rows"], 1000)
        self.assertEqual(matrix["zzshare"]["daily_market"]["hard_max_rows"], 10000)
        self.assertGreaterEqual(matrix["zzshare"]["realtime"]["min_interval_seconds"], 3.0)
        self.assertEqual(matrix["eltdx"]["bars"]["page_size"], 800)
        self.assertEqual(matrix["baostock"]["daily"]["max_concurrency"], 1)
        self.assertEqual(matrix["baostock"]["daily"]["daily_request_limit"], 50_000)
        self.assertEqual(matrix["baostock"]["daily"]["quota_basis"], "published")
        self.assertTrue(matrix["baostock"]["calendar"]["supports_pit"])
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

    def test_baostock_budget_is_persisted_and_cannot_exceed_published_limit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "BAOSTOCK_STATE_DIR": tmp,
            "BAOSTOCK_DAILY_REQUEST_BUDGET": "2",
        }):
            with baostock_source._provider_slot() as handle:
                self.assertEqual(baostock_source._reserve_request(handle, "calendar"), 1)
                self.assertEqual(baostock_source._reserve_request(handle, "daily"), 2)
                with self.assertRaises(baostock_source.BaoStockBudgetExceeded):
                    baostock_source._reserve_request(handle, "daily")
            self.assertEqual(baostock_source.request_budget_status()["used"], 2)

        with patch.dict(os.environ, {"BAOSTOCK_DAILY_REQUEST_BUDGET": "999999"}):
            self.assertEqual(baostock_source._daily_budget(), 50_000)

    def test_preselection_repair_skips_optional_data_and_is_idempotent(self):
        store = unittest.mock.MagicMock()
        store.completed_sync.return_value = False
        syncer = ResearchSynchronizer(store=store)
        repaired = {
            "quality_status": "ok", "coverage": 0.996,
            "providers": {"zzshare": 5550},
        }
        with patch.object(syncer, "sync_day", return_value=repaired) as sync_day:
            result = syncer.repair_daily_market_if_missing("2026-08-27")
        sync_day.assert_called_once_with(
            "2026-08-27", fundamentals=False, fallback=False, refresh_calendar=False,
            optional_fund_flow=False,
        )
        self.assertTrue(result["repaired"])
        self.assertEqual(result["repair_mode"], "bulk_market_with_multi_source_valuation")

        store.completed_sync.return_value = True
        with patch.object(syncer, "sync_day") as sync_day:
            result = syncer.repair_daily_market_if_missing("2026-08-27")
        sync_day.assert_not_called()
        self.assertFalse(result["repaired"])

    def test_preselection_repair_exposes_unpublished_valuation_as_typed_state(self):
        store = unittest.mock.MagicMock()
        store.completed_sync.return_value = False
        store.latest_valuation_as_of.return_value = None
        store.load_universe.return_value = pd.DataFrame({"symbol": ["600000"]})
        syncer = ResearchSynchronizer(store=store)
        pending = {
            "trade_date": "2026-09-01",
            "quality_status": "incomplete",
            "coverage": 0.997,
            "market_quality_status": "ok",
            "providers": {"zzshare": 5553},
            "valuation_rows": 0,
            "valuation_coverage": 0.0,
            "valuation_quality_status": "unavailable",
        }
        with patch.object(syncer, "sync_day", return_value=pending):
            with self.assertRaises(ResearchSourceUnavailable) as raised:
                syncer.repair_daily_market_if_missing("2026-09-01")
        self.assertEqual(raised.exception.result["trade_date"], "2026-09-01")
        self.assertEqual(raised.exception.result["valuation_rows"], 0)

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

    def test_zzshare_empty_valuation_is_not_reported_as_ok(self):
        class Api:
            @staticmethod
            def query(_path, params=None):
                return []

        zzshare._api = Api()
        try:
            with patch.dict(os.environ, {"ZZSHARE_TOKEN": "not-logged"}), \
                    patch("data.sources.zzshare.source_call"):
                result = zzshare.get_valuation("2026-09-01")
        finally:
            zzshare._reset_for_tests()
        self.assertTrue(result.empty)
        self.assertEqual(
            result.attrs["provenance"]["quality_status"], "unavailable"
        )

    def test_zzshare_calendar_filters_explicit_closed_dates(self):
        class Api:
            def trade_days(self, **kwargs):
                return pd.DataFrame([
                    {"cal_date": "2026-08-21", "is_open": 1},
                    {"cal_date": "2026-08-22", "is_open": 0},
                ])

        zzshare._api = Api()
        try:
            with patch.dict(os.environ, {"ZZSHARE_TOKEN": "not-logged"}), \
                    patch("data.sources.zzshare.source_call"):
                days = zzshare.get_trade_days("2026-08-21", "2026-08-22")
        finally:
            zzshare._reset_for_tests()
        self.assertEqual(days, ["2026-08-21"])

    def test_zzshare_calendar_accepts_direct_date_list_from_released_sdk(self):
        class Api:
            def trade_days(self, **kwargs):
                return ["2026-08-20", "2026-08-21"]

        zzshare._api = Api()
        try:
            with patch.dict(os.environ, {"ZZSHARE_TOKEN": "not-logged"}), \
                    patch("data.sources.zzshare.source_call"):
                days = zzshare.get_trade_days("2026-08-20", "2026-08-21")
        finally:
            zzshare._reset_for_tests()
        self.assertEqual(days, ["2026-08-20", "2026-08-21"])

    def test_zzshare_security_master_normalizes_numeric_listed_status(self):
        class Api:
            def __init__(self):
                self.calls = []

            def stock_basic(self, **kwargs):
                self.calls.append(kwargs)
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
        self.assertEqual([call["list_status"] for call in api.calls], ["L", "D", "P"])
        self.assertFalse(result.attrs["lifecycle_complete"])

    def test_zzshare_security_master_keeps_verified_lifecycle_states(self):
        class Api:
            def stock_basic(self, **kwargs):
                status = kwargs["list_status"]
                rows = {
                    "L": [{"ts_code": "600000.SH", "name": "在市", "list_status": "L"}],
                    "D": [{"ts_code": "600001.SH", "name": "退市", "list_status": "D",
                           "delist_date": "2025-12-31"}],
                    "P": [],
                }
                return pd.DataFrame(rows[status])

        zzshare._api = Api()
        try:
            with patch.dict(os.environ, {"ZZSHARE_TOKEN": "not-logged"}), \
                    patch("data.sources.zzshare.source_call"):
                result = zzshare.get_security_master()
        finally:
            zzshare._reset_for_tests()
        self.assertEqual(set(result["list_status"]), {"L", "D"})
        self.assertTrue(result.attrs["lifecycle_complete"])

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

    def test_complete_legacy_master_is_promoted_idempotently(self):
        frame = pd.DataFrame([{
            'ts_code': '600000.SH', 'name': '样本', 'exchange': 'SSE',
            'market': '主板', 'industry': '银行', 'list_status': 'L',
            'list_date': '1999-11-10', 'delist_date': '', 'is_hs': 'Y',
        }])
        frame.attrs['provenance'] = self._provenance('2026-08-21')
        self.store.upsert_securities(frame)
        conn = self.store.connect()
        try:
            conn.execute('DELETE FROM research_security_master_rows')
            conn.execute('DELETE FROM research_master_snapshot_runs')
            with patch.dict(os.environ, {'RESEARCH_MIN_UNIVERSE_ROWS': '1'}):
                first = self.store._promote_legacy_master(conn.cursor())
                second = self.store._promote_legacy_master(conn.cursor())
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(first)
        self.assertIsNone(second)
        universe = self.store.load_universe('2026-08-22')
        self.assertEqual(universe['symbol'].tolist(), ['600000'])

    def test_lifecycle_universe_includes_later_delisted_names(self):
        frame = pd.DataFrame([
            {'ts_code': '600000.SH', 'name': '在市', 'exchange': 'SSE',
             'list_status': 'L', 'list_date': '1999-01-01', 'delist_date': ''},
            {'ts_code': '600001.SH', 'name': '后来退市', 'exchange': 'SSE',
             'list_status': 'D', 'list_date': '2000-01-01', 'delist_date': '2025-12-31'},
            {'ts_code': '600002.SH', 'name': '后来上市', 'exchange': 'SSE',
             'list_status': 'L', 'list_date': '2025-01-01', 'delist_date': ''},
        ])
        frame.attrs['provenance'] = self._provenance('2026-08-21')
        frame.attrs['lifecycle_complete'] = True
        frame.attrs['available_list_statuses'] = ['L', 'D']
        self.store.publish_security_master(frame, minimum_rows=1, validate_change=False)

        historical = self.store.load_lifecycle_universe('2024-12-31')
        self.assertEqual(set(historical['symbol']), {'600000', '600001'})
        self.assertTrue(historical.attrs['lifecycle_complete'])
        self.assertTrue(historical.attrs['observed_after_as_of'])
        current = self.store.load_lifecycle_universe('2026-01-01')
        self.assertEqual(set(current['symbol']), {'600000', '600002'})

    def test_incomplete_lifecycle_snapshot_is_not_published(self):
        frame = pd.DataFrame([{
            'ts_code': '600000.SH', 'name': '样本', 'exchange': 'SSE',
            'list_status': 'L', 'list_date': '1999-01-01', 'delist_date': '',
        }])
        frame.attrs['provenance'] = self._provenance('2026-08-21')
        frame.attrs['lifecycle_complete'] = False
        result = self.store.publish_security_master(
            frame, minimum_rows=1, validate_change=False
        )
        self.assertFalse(result['published'])
        self.assertIn('lifecycle_statuses_incomplete', result['reasons'])

    def test_financial_upsert_repairs_missing_first_seen_timestamp(self):
        frame = pd.DataFrame([{
            'code': '600000.SH', 'statDate': '2026-06-30',
            'pubDate': '2026-08-20', 'roe': 12.5,
        }])
        frame.attrs['provenance'] = self._provenance('2026-08-21')
        self.store.upsert_financial_pit('indicator', frame, as_of='2026-08-21')
        conn = self.store.connect()
        try:
            conn.execute('UPDATE research_financial_facts SET first_seen_at=NULL')
            conn.commit()
        finally:
            conn.close()
        self.store.upsert_financial_pit('indicator', frame, as_of='2026-08-21')
        visible = self.store.load_financial_pit(
            'indicator', '2026-08-21', cutoff_at='2026-08-21T18:00:00+08:00'
        )
        self.assertEqual(visible['symbol'].tolist(), ['600000'])

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
        selection_date = (end + pd.offsets.BDay(1)).date().isoformat()
        calendar_days = pd.date_range(start=dates.min(), end=selection_date, freq="D")
        open_days = {day.date().isoformat() for day in dates}
        open_days.add(selection_date)
        for provider in ("fixture-calendar-a", "fixture-calendar-b"):
            evidence = [
                (day.date().isoformat(), day.date().isoformat() in open_days)
                for day in calendar_days
            ]
            self.store.upsert_calendar_evidence(evidence, provider=provider)
            self.store.record_calendar_fetch(
                provider, calendar_days.min().date().isoformat(), selection_date,
                evidence, quality_status="ok",
            )
        self.store.upsert_trade_days(sorted(open_days), provider="fixture-consensus")

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
                    "turnover": max(30_000_000, close * 1_000_000), "turnover_rate": 1.2,
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
        return selection_date

    def test_future_finance_never_enters_pit_store(self):
        frame = pd.DataFrame([{
            "code": "600000.SH", "statDate": "2026-06-30",
            "pubDate": "2026-09-01", "roe": 99,
        }])
        frame.attrs["provenance"] = self._provenance("2026-08-21")
        self.assertEqual(self.store.upsert_financial_pit(
            "indicator", frame, as_of="2026-08-21"
        ), 0)

    def test_calendar_sync_persists_independent_open_closed_evidence(self):
        syncer = ResearchSynchronizer(self.store)
        evidence = [
            ("2026-08-20", True), ("2026-08-21", True), ("2026-08-22", False),
        ]
        with patch("data.research_sync.zzshare.get_trade_calendar_evidence", return_value=evidence), \
                patch("data.research_sync.baostock.trade_calendar_evidence", return_value=evidence):
            result = syncer.sync_calendar("2026-08-20", "2026-08-22")
        self.assertEqual(result["quality_status"], "ok")
        consensus = self.store.calendar_consensus("2026-08-22")
        self.assertTrue(consensus["ready"], consensus)
        self.assertEqual(consensus["latest_confirmed_open_date"], "2026-08-21")

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
        first_dataset = self.store.event_dataset_id("2026-08-21")
        corrected = {**confirmed, "direction": 0.75, "payload_revision": 2}
        self.assertEqual(self.store.save_events([corrected]), 1)
        second_dataset = self.store.event_dataset_id("2026-08-21")
        self.assertNotEqual(first_dataset, second_dataset)
        loaded = self.store.load_events("2026-08-21")
        self.assertEqual(float(loaded.iloc[0]["direction"]), 0.75)
        conn = self.store.connect()
        try:
            revisions = conn.execute(
                "SELECT COUNT(*) FROM research_event_revisions WHERE event_id=?",
                (next(iter(conn.execute("SELECT event_id FROM research_event_records")))[0],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(revisions, 2)

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
            self.assertTrue(candidate["source_labels"])
            self.assertTrue(all("问财" not in label for label in candidate["source_labels"]))
            self.assertIn(candidate["assigned_lane"], {"core", "satellite", "timing"})
            self.assertGreaterEqual(candidate["data_coverage"], 0.8)
        latest = self.store.latest_selection()
        self.assertEqual(latest["metadata"]["primary_pipeline"], "local_fusion")
        self.assertIn("formal_membership_nominations", latest["artifacts"])
        evidence_conn = self.store.connect()
        try:
            recorded_strategy_ids = {
                row[0] for row in evidence_conn.execute(
                    "SELECT strategy_id FROM selection_strategy_runs WHERE selection_run_id=?",
                    (latest["run_id"],),
                ).fetchall()
            }
        finally:
            evidence_conn.close()
        self.assertIn("formal_local_fusion_top15", recorded_strategy_ids)
        self.assertIn("formal_local_fusion_top5", recorded_strategy_ids)
        self.assertEqual(sum(latest["metadata"]["lane_counts"].values()), 6)
        self.assertLessEqual(latest["metadata"]["lane_counts"]["satellite"], 5)
        self.assertLessEqual(latest["metadata"]["lane_counts"]["timing"], 2)
        self.assertEqual(
            self.store.latest_formal_selection()["run_id"], latest["run_id"]
        )
        verification = self.store.verify_selection_artifacts(latest["run_id"])
        self.assertTrue(verification["exact_match"], verification)
        replay = self.store.replay_selection(latest["metadata"]["manifest_id"])
        self.assertTrue(replay["exact_match"], replay)
        conn = self.store.connect()
        try:
            conn.execute(
                """INSERT INTO selection_runs
                   (run_id,selection_date,created_at,status,primary_source,universe_count,
                    eligible_count,final_count,coverage,reference_source,comparison,metadata,
                    publication_status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("failed-newer", selection_date, "9999-01-01T00:00:00+08:00", "error",
                 "local_pit", 0, 0, 0, 0, None, "{}", "{}", "unpublished"),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.store.latest_selection_attempt()["run_id"], "failed-newer")
        self.assertEqual(self.store.latest_formal_selection()["run_id"], latest["run_id"])

    def test_local_pipeline_accepts_pre_normalization_numeric_listed_status(self):
        selection_date = self._seed()
        conn = self.store.connect()
        try:
            conn.execute("UPDATE research_security_master_rows SET list_status='1'")
            conn.commit()
        finally:
            conn.close()
        result = LocalStockSelector(self.store).run(selection_date, persist=False)
        self.assertEqual(result["status"], "success", result)
        self.assertGreater(len(result["candidates"]), 0)

    def test_local_pipeline_uses_observed_history_when_list_date_is_missing(self):
        selection_date = self._seed()
        conn = self.store.connect()
        try:
            conn.execute("UPDATE research_security_master_rows SET list_date=NULL")
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
            "return_series_60": pd.Series(
                rng.normal(0, 0.01, 60),
                index=pd.bdate_range("2026-05-01", periods=60),
            ), "total_score": 100 - pos,
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
        dates = pd.bdate_range("2026-06-01", periods=60)
        base = pd.Series(np.linspace(-0.01, 0.01, 60), index=dates)
        frame = pd.DataFrame([
            {"symbol": "600001", "industry": "甲", "return_series_60": base},
            {"symbol": "600002", "industry": "乙", "return_series_60": base * 1.01},
            {"symbol": "600003", "industry": "丙", "return_series_60": pd.Series(
                base.to_numpy()[::-1], index=dates
            )},
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
        for provider in ("fixture-calendar-a", "fixture-calendar-b"):
            self.store.upsert_calendar_evidence(
                [("2026-08-25", False)], provider=provider
            )
            self.store.record_calendar_fetch(
                provider, "2026-08-25", "2026-08-25", [("2026-08-25", False)],
                quality_status="ok",
            )
        result = LocalStockSelector(self.store).run("2026-08-25", persist=False)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["metadata"]["reason"], "local market snapshot is stale")

    def test_stale_valuation_snapshot_fails_closed(self):
        selection_date = self._seed()
        conn = self.store.connect()
        try:
            conn.execute("UPDATE research_valuations SET trade_date='2026-08-20'")
            conn.commit()
        finally:
            conn.close()
        result = LocalStockSelector(self.store).run(selection_date, persist=False)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(
            result["metadata"]["reason"], "valuation snapshot stale or incomplete"
        )
        self.assertEqual(result["metadata"]["valuation_as_of"], "2026-08-20")

    def _lag_valuation(self, day="2026-08-20"):
        frame = self.store.load_valuations("2026-08-21")
        frame["trade_date"] = day
        frame["provider_effective_as_of"] = day
        frame.attrs["provenance"] = self._provenance(day)
        self.store.upsert_valuations(frame, as_of=day)
        conn = self.store.connect()
        try:
            conn.execute("DELETE FROM research_valuations WHERE trade_date='2026-08-21'")
            conn.commit()
        finally:
            conn.close()

    def test_previous_trading_day_valuation_is_usable_audited_and_replayable(self):
        selected = self._seed()
        self._lag_valuation()
        result = LocalStockSelector(self.store).run(selected, persist=True)
        self.assertEqual(result["status"], "success", result.get("metadata"))
        self.assertTrue(result["metadata"]["data_degraded"])
        self.assertEqual(result["metadata"]["valuation_as_of"], "2026-08-20")
        self.assertEqual(result["metadata"]["market_as_of"], "2026-08-21")
        self.assertEqual(result["metadata"]["valuation_stale_trading_days"], 1)
        self.assertEqual(result["metadata"]["valuation_status"], "lagged")
        self.assertTrue(self.store.load_valuations("2026-08-21", exact=True).empty)
        replay = self.store.replay_selection(result["metadata"]["manifest_id"])
        self.assertTrue(replay["exact_match"], replay)

        run_id = self.store.start_sync("zzshare", "daily_market", "2026-08-21")
        self.store.finish_sync(run_id, status="success", row_count=24, quality_status="incomplete")
        readiness = research_health_snapshot(store=self.store, selection_date=selected)
        self.assertTrue(readiness["ready"], readiness)
        self.assertFalse(readiness["data_complete"])
        self.assertTrue(readiness["data_degraded"])
        self.assertFalse(readiness["ingestion_checks"]["valuation_fresh"])
        self.assertFalse(self.store.completed_sync("daily_market", "2026-08-21"))

    def test_one_day_fallback_does_not_relax_strict_or_older_valuation_gate(self):
        selected = self._seed()
        self._lag_valuation()
        strict = LocalStockSelector(self.store, policy=SelectionPolicy()).run(
            selected, persist=False
        )
        self.assertEqual(strict["status"], "incomplete")
        self._lag_valuation("2026-08-19")
        conn = self.store.connect()
        try:
            conn.execute("DELETE FROM research_valuations WHERE trade_date='2026-08-20'")
            conn.commit()
        finally:
            conn.close()
        result = LocalStockSelector(self.store).run(selected, persist=False)
        self.assertEqual(result["status"], "incomplete")

    def test_lagged_repair_is_success_without_marking_current_valuation_complete(self):
        self._seed()
        self._lag_valuation()
        syncer = ResearchSynchronizer(self.store)
        pending = {
            "trade_date": "2026-08-21", "quality_status": "incomplete",
            "coverage": 0.997, "market_quality_status": "ok",
            "valuation_rows": 0, "valuation_quality_status": "unavailable",
        }
        with patch.object(syncer, "sync_day", return_value=pending) as sync:
            result = syncer.repair_daily_market_if_missing("2026-08-21")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["quality_status"], "incomplete")
        self.assertTrue(result["selection_ready"])
        self.assertEqual(result["valuation_rows"], 0)
        self.assertEqual(result["selection_valuation"]["valuation_as_of"], "2026-08-20")
        sync.assert_called_once_with(
            "2026-08-21", fundamentals=False, fallback=False,
            refresh_calendar=False, optional_fund_flow=False,
        )
        self.assertFalse(self.store.completed_sync("daily_market", "2026-08-21"))

    def test_calendar_disagreement_fails_closed(self):
        selection_date = self._seed()
        self.store.upsert_calendar_evidence(
            [("2026-08-21", False)], provider="fixture-calendar-b"
        )
        result = LocalStockSelector(self.store).run(selection_date, persist=False)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(
            result["metadata"]["reason"], "trade calendar consensus unavailable"
        )
        self.assertEqual(result["metadata"]["calendar_consensus"]["disagreement_count"], 1)

    def test_historical_selection_before_pit_start_is_rejected_explicitly(self):
        self._seed()
        result = LocalStockSelector(self.store).run("2026-08-20", persist=False)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["metadata"]["reason"], "historical_pit_unavailable")

    def test_research_readiness_is_distinct_and_aggregate(self):
        selection_date = self._seed()
        result = LocalStockSelector(self.store).run(selection_date, persist=True)
        self.assertEqual(result["status"], "success")
        sync_id = self.store.start_sync("zzshare", "daily_market", "2026-08-21")
        self.store.finish_sync(
            sync_id, status="success", row_count=24, quality_status="ok"
        )
        readiness = research_health_snapshot(
            store=self.store, selection_date=selection_date, mode="preopen"
        )
        self.assertTrue(readiness["ready"], readiness)
        self.assertEqual(readiness["expected_market_date"], "2026-08-21")
        self.assertNotIn("symbols", readiness)

    def test_data_completeness_does_not_require_a_selection_output(self):
        selection_date = self._seed()
        sync_id = self.store.start_sync("zzshare", "daily_market", "2026-08-21")
        self.store.finish_sync(sync_id, status="success", row_count=24, quality_status="ok")
        readiness = research_health_snapshot(
            store=self.store, selection_date=selection_date, mode="preopen"
        )
        self.assertFalse(readiness["ready"])
        self.assertTrue(readiness["data_complete"], readiness)

    def test_previous_dates_formal_artifact_does_not_satisfy_todays_readiness(self):
        selected = self._seed()
        result = LocalStockSelector(self.store).run(selected, persist=True)
        self.assertEqual(result['status'], 'success')
        sync_id = self.store.start_sync('zzshare', 'daily_market', '2026-08-21')
        self.store.finish_sync(sync_id, status='success', row_count=24, quality_status='ok')
        with self.store.connect() as conn:
            conn.execute("UPDATE selection_runs SET selection_date='2026-08-23'")
        report = research_health_snapshot(store=self.store, selection_date=selected, mode='preopen')
        self.assertTrue(report['data_complete'])
        self.assertFalse(report['checks']['formal_selection'])

    def test_final_top_five_ignores_llm_and_quote_fields(self):
        formal = {
            "run_id": "run", "snapshot_id": "snapshot",
            "selection_date": "2026-08-24", "market_as_of": "2026-08-21",
            "valuation_as_of": "2026-08-21", "financial_as_of": "2026-08-24",
            "rule_version": "local-pit-v4", "policy_hash": "policy",
            "manifest_id": "manifest",
        }
        rows = [
            {**formal, "code": "600001", "score": 80, "rank": 2,
             "debate_verdict": "否决", "change_pct": 9},
            {**formal, "code": "600002", "score": 90, "rank": 1,
             "debate_verdict": "买入", "change_pct": -2},
        ]
        result = finalize_local_selection(rows, limit=2)
        self.assertEqual([row["code"] for row in result], ["600002", "600001"])
        self.assertTrue(all(row["ranking_source"] == "local_pit_snapshot" for row in result))
        forbidden = {
            "price", "change_pct", "held", "debate_verdict",
            "debate_confidence", "debate_reason", "name",
        }
        self.assertTrue(all(forbidden.isdisjoint(row) for row in result))
        changed = [
            {**row, "price": 999, "held": True, "debate_verdict": "买入"}
            for row in rows
        ]
        self.assertEqual(result, finalize_local_selection(changed, limit=2))

    def test_final_top_five_persists_independent_shortlist_score(self):
        formal = {
            "run_id": "run", "snapshot_id": "snapshot",
            "selection_date": "2026-08-24", "market_as_of": "2026-08-21",
            "valuation_as_of": "2026-08-21", "financial_as_of": "2026-08-24",
            "rule_version": "local-fusion-v2", "policy_hash": "policy",
            "manifest_id": "manifest", "assigned_lane": "core",
            "primary_strategy_name": "本地PIT",
        }
        rows = [
            {**formal, "code": "600001", "lane_score_raw": 90,
             "shortlist_score": 70, "shortlist_rank": 2,
             "selection_priority": 2, "top15_rank": 1,
             "shortlist_components": {"pit_quality": 60}},
            {**formal, "code": "600002", "lane_score_raw": 80,
             "shortlist_score": 95, "shortlist_rank": 1,
             "selection_priority": 1, "top15_rank": 8,
             "shortlist_components": {"pit_quality": 100}},
        ]

        result = finalize_local_selection(rows, limit=2)

        self.assertEqual([row["code"] for row in result], ["600002", "600001"])
        self.assertEqual(result[0]["final_score"], 95)
        self.assertEqual(result[0]["lane_score_raw"], 80)
        self.assertEqual(result[0]["top15_rank"], 8)
        self.assertEqual(result[0]["ranking_source"], "local_fusion_shortlist_v2")


if __name__ == "__main__":
    unittest.main()

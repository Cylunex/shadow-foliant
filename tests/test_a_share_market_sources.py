import logging
import os
import sys
import types
import unittest
from datetime import timedelta
from enum import IntEnum
from unittest.mock import patch

import pandas as pd

import _bootstrap  # noqa: F401
import datahub
from data.sources import easy_tdx, eltdx, tdx_python, zzshare


class _Market(IntEnum):
    SZ = 0
    SH = 1
    BJ = 2


class _Category(IntEnum):
    MIN_5 = 0
    MIN_15 = 1
    MIN_30 = 2
    MIN_60 = 3
    DAY = 4
    WEEK = 5
    MONTH = 6
    MIN_1 = 7


class EasyTdxSourceTest(unittest.TestCase):
    def tearDown(self):
        easy_tdx._reset_for_tests()

    def test_market_mapping_includes_beijing(self):
        fake_module = types.SimpleNamespace(Market=_Market)
        with patch.dict(sys.modules, {'easy_tdx': fake_module}):
            self.assertEqual(easy_tdx._market_for('600000'), _Market.SH)
            self.assertEqual(easy_tdx._market_for('000001'), _Market.SZ)
            self.assertEqual(easy_tdx._market_for('920001'), _Market.BJ)

    def test_kline_pages_and_preserves_minute_timestamp(self):
        calls = []

        class Client:
            def get_security_bars(self, market, code, category, start, count, bar_time):
                calls.append((market, code, category, start, count, bar_time))
                n = count if start == 0 else 100
                base = pd.Timestamp('2026-08-20 09:31') - timedelta(minutes=int(start))
                return pd.DataFrame({
                    'datetime': pd.date_range(base, periods=n, freq='min'),
                    'open': [10.0] * n, 'high': [10.2] * n,
                    'low': [9.9] * n, 'close': [10.1] * n,
                    'vol': [100.0] * n, 'amount': [1010.0] * n,
                })

            def close(self):
                pass

        fake_module = types.SimpleNamespace(Market=_Market, KlineCategory=_Category)
        easy_tdx._client = Client()
        with patch.dict(sys.modules, {'easy_tdx': fake_module}), \
                patch.dict(os.environ, {'MARKET_DATA_MAX_BARS': '3200'}):
            result = easy_tdx.get_kline('600000', '1m', 900)

        self.assertEqual([call[3] for call in calls], [0, 800])
        self.assertEqual(calls[0][-1], 'end')
        self.assertEqual(len(result), 900)
        self.assertEqual(list(result.columns),
                         ['date', 'open', 'high', 'low', 'close', 'volume', 'amount'])
        self.assertTrue(result['date'].is_monotonic_increasing)

    def test_invalid_numeric_environment_falls_back_safely(self):
        self.assertEqual(easy_tdx._positive_int('not-a-number', 3200), 3200)
        self.assertEqual(easy_tdx._positive_float('not-a-number', 8.0), 8.0)


class ZzshareSourceTest(unittest.TestCase):
    def tearDown(self):
        zzshare._reset_for_tests()

    def test_symbol_mapping(self):
        self.assertEqual(zzshare._ts_code('600000'), '600000.SH')
        self.assertEqual(zzshare._ts_code('000001'), '000001.SZ')
        self.assertEqual(zzshare._ts_code('920001'), '920001.BJ')

    def test_volume_unit_is_inferred_without_cross_source_lookup(self):
        raw = pd.DataFrame({
            'trade_date': ['20260820', '20260821', '20260822'],
            'open': [10, 10, 10], 'high': [11, 11, 11],
            'low': [9, 9, 9], 'close': [10, 10, 10],
            'vol': [10, 20, 30], 'amount': [10000, 20000, 30000],
        })
        out = zzshare._standardize(raw, time_col='trade_date', volume_col='vol')
        self.assertEqual(out['volume'].tolist(), [1000, 2000, 3000])

    def test_sdk_exception_does_not_log_token_or_response(self):
        class Api:
            def stk_mins(self, **kwargs):
                raise RuntimeError('secret-token private-response-body')

        zzshare._api = Api()
        logger = logging.getLogger('zzshare')
        records = []

        class Handler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Handler()
        logger.addHandler(handler)
        try:
            with patch.dict(os.environ, {'ZZSHARE_TOKEN': 'secret-token'}):
                out = zzshare.get_kline('600000', frequency='1m', count=5)
        finally:
            logger.removeHandler(handler)
        self.assertTrue(out.empty)
        self.assertEqual(records, [])


class ElTdxSourceTest(unittest.TestCase):
    def tearDown(self):
        eltdx._reset_for_tests()

    def test_symbol_mapping(self):
        self.assertEqual(eltdx._sdk_symbol('600000'), 'sh600000')
        self.assertEqual(eltdx._sdk_symbol('000001'), 'sz000001')
        self.assertEqual(eltdx._sdk_symbol('920001'), 'bj920001')

    def test_proxy_config_decodes_credentials_without_exposing_it(self):
        with patch.dict(os.environ, {
            'ELTDX_PROXY_URL': 'http://user%40name:p%40ss@proxy.example.com:8080'
        }):
            config = eltdx._proxy_config()
        self.assertEqual(config['scheme'], 'http')
        self.assertEqual(config['host'], 'proxy.example.com')
        self.assertEqual(config['port'], 8080)
        self.assertEqual(config['username'], 'user@name')
        self.assertEqual(config['password'], 'p@ss')

    def test_proxy_connector_is_scoped_to_eltdx_connection(self):
        calls = []

        class FakeSocket:
            def set_proxy(self, *args, **kwargs):
                calls.append(('proxy', args, kwargs))

            def settimeout(self, value):
                calls.append(('timeout', value))

            def connect(self, target):
                calls.append(('connect', target))

            def close(self):
                calls.append(('close',))

        fake_socks = types.SimpleNamespace(
            HTTP=1, SOCKS4=2, SOCKS5=3, socksocket=FakeSocket,
        )

        class FakeConnectionClosedError(Exception):
            pass

        class FakeTdxConnection:
            def _connect_socket(self):
                raise AssertionError('direct connector must be replaced')

        modules = {
            'socks': fake_socks,
            'eltdx.exceptions': types.SimpleNamespace(
                ConnectionClosedError=FakeConnectionClosedError),
            'eltdx.transport.connection': types.SimpleNamespace(
                TdxConnection=FakeTdxConnection),
        }
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {
            'ELTDX_PROXY_URL': 'socks5h://proxy.example.com:1080'
        }):
            self.assertTrue(eltdx._install_proxy_connector())
            connection = FakeTdxConnection()
            connection._hosts = ['example.invalid:7709']
            connection._timeout = 3.0
            connection._socket = None
            connection._connected_host = None
            connection._connect_socket()

        self.assertIsInstance(connection._socket, FakeSocket)
        self.assertEqual(connection._connected_host, 'example.invalid:7709')
        self.assertIn(('connect', ('example.invalid', 7709)), calls)
        proxy_call = next(item for item in calls if item[0] == 'proxy')
        self.assertTrue(proxy_call[2]['rdns'])

    def test_kline_pages_and_normalizes_volume_to_shares(self):
        calls = []

        class Client:
            def get_kline(self, frequency, code, *, start, count):
                calls.append((frequency, code, start, count))
                n = count if start == 0 else 100
                rows = [types.SimpleNamespace(
                    time=pd.Timestamp('2026-08-20 09:31') + timedelta(minutes=int(i + start)),
                    open_price=10.0, high_price=10.2, low_price=9.9,
                    close_price=10.1, volume=12, amount=12120,
                ) for i in range(n)]
                return types.SimpleNamespace(items=rows)

            def close(self):
                pass

        eltdx._client = Client()
        with patch.dict(os.environ, {'MARKET_DATA_MAX_BARS': '3200'}):
            out = eltdx.get_kline('600000', '1m', 900)
        self.assertEqual([call[2] for call in calls], [0, 800])
        self.assertEqual(len(out), 900)
        self.assertEqual(float(out.iloc[0]['volume']), 1200.0)
        self.assertTrue(out['date'].is_monotonic_increasing)
        self.assertIsNone(out['date'].dt.tz)


class TdxPythonSourceTest(unittest.TestCase):
    def tearDown(self):
        tdx_python._reset_for_tests()

    def test_symbol_mapping(self):
        self.assertEqual(tdx_python._sdk_symbol('600000'), 'sh600000')
        self.assertEqual(tdx_python._sdk_symbol('000001'), 'sz000001')
        self.assertEqual(tdx_python._sdk_symbol('920001'), 'bj920001')

    def test_price_volume_and_time_contract(self):
        response = {'ok': True, 'rows': [{
            'time': '2026-08-21T09:31:00',
            'open': 10000, 'high': 10200, 'low': 9900, 'close': 10100,
            'volume': 12, 'amount': 12120,
        }]}
        with patch.object(tdx_python, '_request', return_value=response):
            out = tdx_python.get_kline('600000', '1m', 1)
        self.assertEqual(float(out.iloc[0]['close']), 10.1)
        self.assertEqual(float(out.iloc[0]['volume']), 1200.0)
        self.assertEqual(str(out.iloc[0]['date']), '2026-08-21 09:31:00')

    def test_quotes_are_split_into_protocol_safe_batches(self):
        symbols = [f'{600000 + i:06d}' for i in range(81)]
        calls = []

        def request(payload):
            calls.append(payload['symbols'])
            return {'ok': True, 'rows': []}

        with patch.object(tdx_python, '_request', side_effect=request):
            self.assertEqual(tdx_python.get_quotes(symbols), {})
        self.assertEqual([len(batch) for batch in calls], [80, 1])

    def test_invalid_numeric_environment_falls_back_safely(self):
        response = {'ok': True, 'rows': []}
        with patch.dict(os.environ, {'MARKET_DATA_MAX_BARS': 'not-a-number'}), \
                patch.object(tdx_python, '_request', return_value=response) as request:
            out = tdx_python.get_kline('600000', '1m', 'not-a-number')
        self.assertTrue(out.empty)
        self.assertEqual(request.call_args.args[0]['count'], 800)


class DataHubIntradayTest(unittest.TestCase):
    def setUp(self):
        datahub._STATS.clear()

    @staticmethod
    def _frame():
        idx = pd.date_range('2026-08-20 09:31', periods=2, freq='min')
        return pd.DataFrame({
            'Open': [10.0, 10.1], 'Close': [10.1, 10.2],
            'High': [10.2, 10.3], 'Low': [9.9, 10.0],
            'Volume': [1000, 1200],
        }, index=idx).rename_axis('Date')

    def test_minute_route_prefers_zzshare_and_records_source(self):
        with patch.object(datahub, '_zzshare_available', return_value=True), \
                patch.object(datahub, '_eltdx_available', return_value=True), \
                patch.object(datahub, '_tdx_python_available', return_value=True), \
                patch.object(datahub, '_easy_tdx_available', return_value=True), \
                patch.object(datahub, '_kline_zzshare', return_value=self._frame()) as zz, \
                patch.object(datahub, '_kline_eltdx') as eltdx_source, \
                patch.object(datahub, '_kline_tdx_python') as modern_tdx, \
                patch.object(datahub, '_kline_easy_tdx') as tdx, \
                patch.object(datahub, '_kline_mootdx') as old:
            out = datahub.kline('600000', '1mo', '1m', use_cache=False)

        self.assertFalse(out.empty)
        self.assertEqual(out.attrs['datahub_source'], 'zzshare')
        self.assertEqual(out.attrs['datahub_interval'], '1m')
        zz.assert_called_once()
        eltdx_source.assert_not_called()
        modern_tdx.assert_not_called()
        tdx.assert_not_called()
        old.assert_not_called()

    def test_empty_primary_falls_back_to_python_311_tdx(self):
        with patch.object(datahub, '_zzshare_available', return_value=True), \
                patch.object(datahub, '_eltdx_available', return_value=True), \
                patch.object(datahub, '_tdx_python_available', return_value=True), \
                patch.object(datahub, '_easy_tdx_available', return_value=False), \
                patch.object(datahub, '_kline_zzshare', return_value=pd.DataFrame()), \
                patch.object(datahub, '_kline_eltdx', return_value=self._frame()) as tdx, \
                patch.object(datahub, '_kline_tdx_python') as enhanced_tdx, \
                patch.object(datahub, '_kline_mootdx') as old:
            out = datahub.kline('600000', '1mo', '1m', use_cache=False)
        self.assertEqual(out.attrs['datahub_source'], 'eltdx')
        tdx.assert_called_once()
        enhanced_tdx.assert_not_called()
        old.assert_not_called()

    def test_stale_minute_cache_is_not_actionable_after_ten_minutes(self):
        frame = self._frame()
        frame.attrs.update(
            datahub_stale=True,
            datahub_cache_age_days=11 / 1440,
            datahub_source='stale_cache',
            datahub_interval='1m',
        )
        quality = datahub.kline_quality(frame)
        self.assertFalse(quality['actionable'])


if __name__ == '__main__':
    unittest.main()

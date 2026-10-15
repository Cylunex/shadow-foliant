import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401  保持与 MCP 入口相同的扁平模块路径
from portfolio.trade_import_service import (
    import_trade_records, parse_markdown_table, prepare_trades, preview_position_effects,
    trade_execution_key,
)
from portfolio.portfolio_db_pg import PortfolioDBPG


class _FakePortfolioDB:
    def __init__(self, holdings=None, trades=None):
        self.holdings = holdings or []
        self.trades = trades or []
        self.imported_rows = []
        self.update_position = None

    def get_all_stocks(self):
        return self.holdings

    def get_trades(self, code=None, limit=10000):
        return self.trades

    def import_trades(self, rows, update_position=True):
        self.imported_rows = rows
        self.update_position = update_position
        return {'imported': len(rows), 'failed': 0,
                'positions_updated': len(rows), 'errors': []}


class TradeImportServiceTests(unittest.TestCase):
    def test_parses_markdown_table_and_strips_bold_name(self):
        table = """| 成交时间 | 股票名称 | 成交价 | 成交量 | 成交额 | 交易类型 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 2026-01-02 10:00:00 | **贵州茅台** | 1500.00 | 100 | 150000 | 买入 |"""
        rows = parse_markdown_table(table)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['股票名称'], '贵州茅台')
        self.assertEqual(rows[0]['交易类型'], '买入')

    def test_minimal_name_row_fills_code_and_amount(self):
        db = _FakePortfolioDB(holdings=[{'code': '600519', 'name': '贵州茅台'}])
        rows = [{'股票名称': '贵州茅台', '成交时间': '2026-01-02 10:00:00',
                 '成交价': 1500, '成交量': 100, '交易类型': '买入'}]
        result = import_trade_records(rows=rows, dry_run=True, portfolio_db=db)
        self.assertEqual(result['status'], 'preview')
        self.assertEqual(result['resolved_codes'], {'贵州茅台': '600519'})
        self.assertEqual(result['preview'][0]['amount'], 150000.0)

    @patch('datahub.stock_codes', return_value={'招商银行': '600036'})
    def test_missing_local_name_uses_one_batch_resolver(self, resolver):
        db = _FakePortfolioDB()
        table = """| 成交时间 | 股票名称 | 成交价 | 成交量 | 交易类型 |
| --- | --- | --- | --- | --- |
| 2026-01-02 10:00:00 | 招商银行 | 40 | 200 | 买入 |"""
        result = import_trade_records(table=table, dry_run=True, portfolio_db=db)
        self.assertEqual(result['preview'][0]['code'], '600036')
        resolver.assert_called_once_with(['招商银行'], allow_refresh=True)

    def test_original_full_row_shape_remains_supported(self):
        db = _FakePortfolioDB()
        rows = [{'code': '600519', 'name': '贵州茅台', 'trade_type': '卖出',
                 'quantity': 100, 'price': 1600, 'amount': 160000,
                 'trade_time': '2026-01-02 10:00:00'}]
        result = import_trade_records(rows=rows, update_position=False, portfolio_db=db)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['imported'], 1)
        self.assertFalse(db.update_position)

    def test_unresolved_name_blocks_whole_batch(self):
        db = _FakePortfolioDB(holdings=[{'code': '600519', 'name': '贵州茅台'}])
        rows = [
            {'股票名称': '贵州茅台', '成交价': 1500, '成交量': 100, '交易类型': '买入'},
            {'股票名称': '不存在证券', '成交价': 10, '成交量': 100, '交易类型': '买入'},
        ]
        with patch('datahub.stock_codes', return_value={}):
            result = import_trade_records(rows=rows, portfolio_db=db)
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual(result['imported'], 0)
        self.assertEqual(db.imported_rows, [])
        self.assertEqual(result['unresolved'], ['不存在证券'])

    def test_duplicate_trade_ignores_stored_timezone_suffix(self):
        existing = [{'code': '600519', 'name': '贵州茅台', 'trade_type': '买入',
                     'quantity': 100, 'price': 1500,
                     'trade_time': '2026-01-02 10:00:00+00:00'}]
        db = _FakePortfolioDB(trades=existing)
        rows = [{'code': '600519', 'name': '贵州茅台', 'trade_type': '买入',
                 'quantity': 100, 'price': 1500,
                 'trade_time': '2026-01-02 10:00:00'}]
        result = import_trade_records(rows=rows, portfolio_db=db)
        self.assertEqual(result['status'], 'noop')
        self.assertEqual(result['skipped_existing'], 1)
        self.assertEqual(db.imported_rows, [])

    def test_explicit_offsets_use_instant_not_raw_wall_time_for_fingerprint(self):
        base = {'code': '600519', 'trade_type': '买入', 'quantity': 100,
                'price': 1500, 'source': 'web:trade-entry'}
        naive = trade_execution_key({**base, 'trade_time': '2026-09-18 13:26:46'})
        same_instant = trade_execution_key({**base, 'trade_time': '2026-09-18 05:26:46Z'})
        different_instant = trade_execution_key({**base, 'trade_time': '2026-09-18 13:26:46Z'})
        self.assertEqual(naive, same_instant)
        self.assertNotEqual(naive, different_instant)

    def test_historical_explicit_offset_fingerprint_is_checked_with_instant(self):
        class RecordingDB(_FakePortfolioDB):
            def existing_trade_execution_keys(self, keys, **kwargs):
                self.lookup = (keys, kwargs)
                return keys[:1]

        db = RecordingDB()
        row = {'code': '600519', 'name': '贵州茅台', 'trade_type': '买入',
               'quantity': 100, 'price': 1500,
               'trade_time': '2026-09-18 13:26:46+00:00'}
        result = import_trade_records(rows=[row], portfolio_db=db)
        self.assertEqual(result['status'], 'noop')
        historical = db.lookup[1]['historical_fingerprints']
        self.assertEqual(len(historical), 1)
        self.assertNotEqual(historical[0][0], historical[0][1])
        self.assertEqual(historical[0][3], '2026-09-18T13:26:46+00:00')
        self.assertEqual(db.lookup[1]['legacy_utc_keys'], [])

    def test_only_naive_market_time_requests_legacy_utc_comparison(self):
        class RecordingDB(_FakePortfolioDB):
            def existing_trade_execution_keys(self, keys, **kwargs):
                self.lookup = kwargs
                return []

        db = RecordingDB()
        row = {'code': '600519', 'name': '贵州茅台', 'trade_type': '买入',
               'quantity': 100, 'price': 1500,
               'trade_time': '2026-09-18 13:26:46'}
        import_trade_records(rows=[row], portfolio_db=db)
        self.assertEqual(len(db.lookup['legacy_utc_keys']), 1)
        row['trade_time'] = '2026-09-18 13:26:46+00:00'
        import_trade_records(rows=[row], portfolio_db=db)
        self.assertEqual(db.lookup['legacy_utc_keys'], [])

    def test_postgres_legacy_lookup_is_zone_explicit_and_instant_scoped(self):
        from portfolio import portfolio_db_pg as pg

        candidate = ('600519', '2026-09-18 13:26:46', '买入', 100, 1500.0)
        responses = [[], [], [candidate], [('fp:new',)]]

        class Cursor:
            def __init__(self):
                self.calls = []

            def execute(self, sql, params):
                self.calls.append((sql, params))

            def fetchall(self):
                return responses.pop(0)

            def close(self):
                pass

        class Connection:
            def __init__(self):
                self.cur = Cursor()

            def cursor(self):
                return self.cur

            def close(self):
                pass

        conn = Connection()
        with patch.object(pg, 'get_conn', return_value=conn):
            found = PortfolioDBPG.__new__(PortfolioDBPG).existing_trade_execution_keys(
                ['fp:new'], legacy_keys=[candidate], legacy_utc_keys=[candidate],
                historical_fingerprints=[
                    ('fp:old', 'fp:new', '600519', '2026-09-18T13:26:46+00:00',
                     '买入', 100, 1500.0),
                ],
            )
        self.assertIn(candidate, found)
        self.assertIn('fp:new', found)
        self.assertIn("AT TIME ZONE 'Asia/Shanghai'", conn.cur.calls[1][0])
        self.assertIn("AT TIME ZONE 'UTC'", conn.cur.calls[2][0])
        self.assertIn("t.trade_time=w.instant::timestamptz", conn.cur.calls[3][0])
        self.assertNotIn("current_setting('TIMEZONE')", ''.join(c[0] for c in conn.cur.calls))

    def test_change_log_binds_naive_time_with_shanghai_offset(self):
        from datetime import datetime, timedelta

        class Cursor:
            def __init__(self):
                self.calls = []

            def execute(self, sql, params=None):
                self.calls.append((sql, params))

        cur = Cursor()
        db = PortfolioDBPG.__new__(PortfolioDBPG)
        db._log_change(
            '600519', '贵州茅台', 'add', None,
            {'quantity': 100, 'cost_price': 1500},
            conn=object(), cur=cur, trade_time='2026-09-18 13:26:46',
        )
        bound = next(params[-1] for sql, params in cur.calls
                     if 'INSERT INTO trade_records' in sql)
        self.assertIsInstance(bound, datetime)
        self.assertEqual(bound.utcoffset(), timedelta(hours=8))
        self.assertEqual(bound.strftime('%Y-%m-%d %H:%M:%S'), '2026-09-18 13:26:46')

    def test_duplicate_sell_is_removed_before_position_validation(self):
        existing = [{'code': '600519', 'name': '贵州茅台', 'trade_type': '卖出',
                     'quantity': 100, 'price': 1500,
                     'trade_time': '2026-01-02 10:00:00'}]
        db = _FakePortfolioDB(trades=existing)
        result = import_trade_records(rows=[dict(existing[0])], portfolio_db=db)
        self.assertEqual(result['status'], 'noop')
        self.assertEqual(result['skipped_existing'], 1)
        self.assertEqual(result['errors'], [])

    def test_wrong_amount_is_recomputed_with_warning(self):
        db = _FakePortfolioDB()
        rows = [{'code': '600519', 'name': '贵州茅台', 'trade_type': '买入',
                 'quantity': 100, 'price': 1500, 'amount': 1}]
        result = import_trade_records(rows=rows, dry_run=True, portfolio_db=db)
        self.assertEqual(result['preview'][0]['amount'], 150000.0)
        self.assertTrue(result['warnings'])

    def test_position_changing_oversell_is_rejected_before_write(self):
        db = _FakePortfolioDB(holdings=[{
            'code': '600519', 'name': '贵州茅台', 'quantity': 100, 'cost_price': 1400,
        }])
        rows = [{'code': '600519', 'name': '贵州茅台', 'trade_type': '卖出',
                 'quantity': 200, 'price': 1500}]
        result = import_trade_records(rows=rows, update_position=True, portfolio_db=db)
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual(result['imported'], 0)
        self.assertIn('超过可用持仓', result['errors'][0])
        self.assertEqual(db.imported_rows, [])

    def test_record_only_sell_does_not_require_current_position(self):
        db = _FakePortfolioDB()
        rows = [{'code': '600519', 'name': '贵州茅台', 'trade_type': '卖出',
                 'quantity': 100, 'price': 1500}]
        result = import_trade_records(
            rows=rows, update_position=False, dry_run=True, portfolio_db=db
        )
        self.assertEqual(result['status'], 'preview')
        self.assertEqual(result['effects'][0]['position_effect'], 'record_only')

    def test_equal_fills_with_distinct_execution_ids_are_not_collapsed(self):
        db = _FakePortfolioDB()
        base = {'code': '600519', 'name': '贵州茅台', 'trade_type': '买入',
                'quantity': 100, 'price': 1500, 'trade_time': '2026-01-02 10:00:00',
                'source': 'broker-example', 'account_ref': 'account-example'}
        rows = [
            {**base, 'broker_execution_id': 'execution-example-1'},
            {**base, 'broker_execution_id': 'execution-example-2'},
        ]
        result = import_trade_records(rows=rows, portfolio_db=db)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['imported'], 2)
        self.assertNotEqual(db.imported_rows[0]['external_fingerprint'],
                            db.imported_rows[1]['external_fingerprint'])

    def test_validated_selection_origin_is_preserved(self):
        db = _FakePortfolioDB()
        row = {'code': '600519', 'name': '贵州茅台', 'trade_type': '买入',
               'quantity': 100, 'price': 1500, 'selection_run_id': 'run-1',
               'strategy_id': 'low_price_bull'}
        resolver = lambda *_a, **_k: {
            'selection_run_id': 'run-1', 'nomination_id': 'nom-1',
            'strategy_id': 'low_price_bull', 'symbol': '600519',
        }
        result = import_trade_records(
            rows=[row], dry_run=True, portfolio_db=db, origin_resolver=resolver
        )
        self.assertEqual(result['status'], 'preview')
        self.assertEqual(result['preview'][0]['nomination_id'], 'nom-1')
        self.assertEqual(result['preview'][0]['strategy_id'], 'low_price_bull')

    def test_mismatched_selection_origin_blocks_whole_batch(self):
        db = _FakePortfolioDB()
        row = {'code': '600519', 'name': '贵州茅台', 'trade_type': '买入',
               'quantity': 100, 'price': 1500, 'selection_run_id': 'wrong-run'}
        result = import_trade_records(
            rows=[row], portfolio_db=db, origin_resolver=lambda *_a, **_k: None
        )
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual(db.imported_rows, [])
        self.assertIn('选股来源不存在', result['errors'][0])

    def test_postgres_normalizer_keeps_verified_attribution_fields(self):
        normalized = PortfolioDBPG._normalize_trade(PortfolioDBPG.__new__(PortfolioDBPG), {
            'code': '600519', 'name': '贵州茅台', 'trade_type': '买入',
            'quantity': 100, 'price': 1500, 'selection_run_id': 'run-1',
            'nomination_id': 'nom-1', 'strategy_id': 'low_price_bull',
            'decision_signal_id': 42,
        })
        self.assertEqual(normalized['extra']['selection_run_id'], 'run-1')
        self.assertEqual(normalized['extra']['nomination_id'], 'nom-1')
        self.assertEqual(normalized['extra']['decision_signal_id'], 42)


if __name__ == '__main__':
    unittest.main()

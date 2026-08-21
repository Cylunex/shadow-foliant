import unittest
from unittest import mock

from portfolio import portfolio_snapshot
from portfolio.portfolio_snapshot import (
    _infer_trade_watermark,
    _watermarked_trade_flow,
)


class PortfolioSnapshotCashflowTests(unittest.TestCase):
    def test_schema_initialization_runs_once_per_process(self):
        previous = portfolio_snapshot._INITIALIZED
        try:
            portfolio_snapshot._INITIALIZED = False
            with mock.patch.object(portfolio_snapshot, '_init_db_once') as initialize:
                portfolio_snapshot.init_db()
                portfolio_snapshot.init_db()
            initialize.assert_called_once_with()
        finally:
            portfolio_snapshot._INITIALIZED = previous

    def test_current_schema_initialization_does_not_run_ddl(self):
        conn = mock.Mock()
        cursor = mock.Mock()
        cursor.fetchall.return_value = [
            ('daily_mv_change',),
            ('quote_count',),
            ('fallback_count',),
            ('anomaly_count',),
            ('trade_watermark',),
        ]
        conn.cursor.return_value = cursor

        with mock.patch.object(portfolio_snapshot, '_conn', return_value=conn), \
                mock.patch.object(portfolio_snapshot, 'is_postgres', return_value=True):
            portfolio_snapshot._init_db_once()

        statements = [str(call.args[0]).strip().upper()
                      for call in cursor.execute.call_args_list]
        self.assertFalse(any(sql.startswith(('CREATE ', 'ALTER ')) for sql in statements))
        conn.commit.assert_called_once()
        conn.close.assert_called_once()

    def test_late_backfilled_trade_enters_next_snapshot_as_cashflow(self):
        trades = [
            {
                'id': 10,
                'trade_type': '买入',
                'amount': 1000,
                'trade_time': '2026-08-01 10:00:00+00:00',
                'created_at': '2026-08-20 08:50:00',
            },
            {
                'id': 11,
                'trade_type': '买入',
                'amount': 500,
                # 成交日期早于快照也无妨；录入时间才决定它首次进入哪张快照。
                'trade_time': '2026-08-10 10:00:00+00:00',
                'created_at': '2026-08-20 09:10:00',
            },
        ]

        previous = _infer_trade_watermark(trades, '2026-08-20T09:00:00+00:00')
        self.assertEqual(previous, 10)
        self.assertEqual(_watermarked_trade_flow(trades, previous, 11), 500.0)

    def test_sell_is_negative_cashflow(self):
        trades = [
            {'id': 20, 'trade_type': '卖出', 'amount': 300},
            {'id': 21, 'trade_type': '调整', 'amount': 999},
        ]
        self.assertEqual(_watermarked_trade_flow(trades, 19, 21), -300.0)


if __name__ == '__main__':
    unittest.main()

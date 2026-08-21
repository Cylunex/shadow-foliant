import unittest
from unittest import mock

from portfolio import daily_pnl


class DailyPnlFallbackTests(unittest.TestCase):
    def test_missing_snapshot_releases_read_transaction_before_fallback(self):
        first_conn = mock.Mock()
        first_cursor = mock.Mock()
        first_cursor.fetchone.return_value = None
        first_conn.cursor.return_value = first_cursor

        second_conn = mock.Mock()
        second_cursor = mock.Mock()
        second_cursor.fetchone.side_effect = [
            ('2026-08-17', 1000, 1, 20, 900, 1, 0, 0),
            (0, 0, 0, 0, 0, 0),
        ]
        second_conn.cursor.return_value = second_cursor

        def save_snapshot(_snap_date):
            self.assertTrue(
                first_conn.close.called,
                'fallback must release the initial PostgreSQL read transaction first',
            )
            return {'ok': True}

        with mock.patch.object(daily_pnl, '_conn', side_effect=[first_conn, second_conn]), \
                mock.patch.object(daily_pnl, 'init_db'), \
                mock.patch('portfolio.portfolio_snapshot.init_db'), \
                mock.patch('portfolio.portfolio_snapshot.save_snapshot',
                           side_effect=save_snapshot):
            result = daily_pnl.merge_save('2026-08-17')

        self.assertEqual(result['stock_count'], 1)
        self.assertEqual(result['stock_daily_pnl'], 20)
        second_conn.commit.assert_called_once()
        second_conn.close.assert_called_once()

    def test_fund_pnl_uses_nav_difference_and_reports_coverage(self):
        result = daily_pnl.calculate_fund_pnl(
            [
                {'code': '000001', 'shares': 100},
                {'code': '000002', 'shares': 50},
                {'code': '000003', 'shares': 10},
            ],
            {
                # 旧公式 current_mv * 10% 会得到 11；净值差精算应为 10。
                '000001': {'unit_nav': 1.1, 'prev_nav': 1.0,
                           'nav_date': '2026-08-20'},
                # 滞后净值只进入市值，不进入当日收益。
                '000002': {'unit_nav': 2.0, 'prev_nav': 1.9,
                           'nav_date': '2026-08-19'},
            },
            '2026-08-20',
        )

        self.assertEqual(result['fund_daily_pnl'], 10.0)
        self.assertEqual(result['fund_daily_pct'], 10.0)
        self.assertEqual(result['fund_count'], 2)
        self.assertEqual(result['fund_fresh_count'], 1)
        self.assertEqual(result['fund_total_count'], 3)
        self.assertEqual(result['fund_mv'], 210.0)


if __name__ == '__main__':
    unittest.main()

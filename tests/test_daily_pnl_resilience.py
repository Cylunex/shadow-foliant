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
            (0, 0, 0, 0),
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


if __name__ == '__main__':
    unittest.main()

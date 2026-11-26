import unittest
from unittest import mock

from portfolio import daily_pnl


class DailyPnlFallbackTests(unittest.TestCase):
    def test_schema_initialization_runs_once_per_process(self):
        previous = daily_pnl._INITIALIZED
        try:
            daily_pnl._INITIALIZED = False
            with mock.patch.object(daily_pnl, '_init_db_once') as initialize:
                daily_pnl.init_db()
                daily_pnl.init_db()
            initialize.assert_called_once_with()
        finally:
            daily_pnl._INITIALIZED = previous

    def test_current_schema_initialization_does_not_run_ddl(self):
        conn = mock.Mock()
        cursor = mock.Mock()
        cursor.fetchall.return_value = [
            ('fund_total_count',),
            ('fund_fresh_count',),
            ('fund_updated_at',),
            ('fund_prior_fresh_count',),
            ('fund_revision_delta',),
        ]
        conn.cursor.return_value = cursor

        with mock.patch.object(daily_pnl, '_conn', return_value=conn), \
                mock.patch.object(daily_pnl, 'is_postgres', return_value=True):
            daily_pnl._init_db_once()

        statements = [str(call.args[0]).strip().upper()
                      for call in cursor.execute.call_args_list]
        self.assertFalse(any(sql.startswith(('CREATE ', 'ALTER ')) for sql in statements))
        conn.commit.assert_called_once()
        conn.close.assert_called_once()

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
            (0, 0, 0, 0, 0, 0, 20, 2.0408),
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


def test_reconciles_late_nav_without_replacing_stock_pnl_or_using_future_nav(tmp_path, monkeypatch):
    from datetime import date
    import sqlite3
    from fund import fund_ledger
    import fund_db

    path = tmp_path / "pnl.db"
    monkeypatch.setattr(daily_pnl, "_conn", lambda: sqlite3.connect(path))
    monkeypatch.setattr(daily_pnl, "_INITIALIZED", False)
    monkeypatch.setattr(daily_pnl, "is_postgres", lambda: False)
    day = date(2026, 11, 18).isoformat()
    daily_pnl.upsert_fund_pnl(day, 2, 200, 0, 0, fund_fresh_count=0)
    with sqlite3.connect(path) as conn:
        fund_ledger.create_schema(conn.cursor())
        conn.execute("UPDATE daily_pnl_snapshots SET stock_daily_pnl=5,stock_mv=100 WHERE snap_date=?", (day,))

    def ledger(*, as_of):
        assert as_of == day
        return {
            "positions": [{"code": "000001", "shares": 100}, {"code": "000002", "shares": 100}],
            "transactions": [{"code": "000001", "confirmation_date": day,
                              "delta_shares": 25, "cash_paid": 27.5}],
        }

    def nav(codes, *, upto_date):
        assert set(codes) == {"000001", "000002"} and upto_date == day
        return {
            "000001": {"unit_nav": 1.1, "prev_nav": 1.0, "nav_date": day},
            "000002": {"unit_nav": 1.0, "prev_nav": 1.0, "nav_date": "2026-11-17"},
        }

    monkeypatch.setattr(fund_ledger, "read", ledger)
    monkeypatch.setattr(fund_db, "get_nav_pairs", nav)
    assert daily_pnl.reconcile_fund_pnl("2026-11-20")[day]["fund_fresh_count"] == 1
    row = daily_pnl.get_pnl(day)
    assert row["fund_daily_pnl"] == 7.5 and row["total_daily_pnl"] == 12.5
    assert row["stock_daily_pnl"] == 5.0
    assert row["fund_prior_fresh_count"] == 0 and row["fund_revision_delta"] == 7.5
    assert row["fund_updated_at"]
    from application.account_preview import account_books
    latest = account_books()["summary"]["latest"]
    assert latest["fund_fresh_count"] == 1 and latest["fund_pending_count"] == 1
    assert latest["fund_revision_delta"] == 7.5 and latest["stock_daily_pnl"] == 5.0
    assert daily_pnl.reconcile_fund_pnl("2026-11-20") == {}

    # A legacy holding has no dated opening. New nightly snapshots carry the
    # exact basis needed to calculate its return after NAV becomes available.
    book = ledger(as_of=day)
    basis = [{**h, "day_transactions": [e for e in book["transactions"] if e["code"] == h["code"]]}
             for h in book["positions"]]
    daily_pnl.upsert_fund_pnl(day, 2, 200, 0, 0, fund_fresh_count=0, basis=basis)
    monkeypatch.setattr(fund_ledger, "read", lambda **_: None)
    assert daily_pnl.reconcile_fund_pnl("2026-11-20")[day]["fund_daily_pnl"] == 7.5

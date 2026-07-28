import os
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

import pandas as pd

from selection.main_force_selector import MainForceStockSelector
from jobs import task_control
from jobs import ai_recommendation_monitor
from monitor.monitor_db import StockMonitorDatabase


class MainForceQualityTests(unittest.TestCase):
    def test_generic_candidates_are_not_main_force_results(self):
        selector = MainForceStockSelector()
        generic = pd.DataFrame([
            {'股票代码': '000001', '股票简称': '平安银行', '总市值': 1000},
            {'股票代码': '000002', '股票简称': '万科A', '总市值': 800},
        ])

        result = selector.get_top_stocks(generic, top_n=1)

        self.assertTrue(result.empty)

    def test_real_main_force_column_is_sorted(self):
        selector = MainForceStockSelector()
        rows = pd.DataFrame([
            {'股票代码': '000001', '主力资金流向': 10},
            {'股票代码': '000002', '主力资金流向': 30},
        ])

        result = selector.get_top_stocks(rows, top_n=1)

        self.assertEqual(result.iloc[0]['股票代码'], '000002')


class MonitorDatabaseTests(unittest.TestCase):
    def test_single_monitor_mapping_and_name_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = StockMonitorDatabase(os.path.join(tmp, 'monitor.db'))
            monitor_id = db.add_monitored_stock(
                '600519', '贵州茅台', '持有', {'min': 1400, 'max': 1500},
                1700, 1350, trading_hours_only=False,
            )
            first = db.get_monitor_by_code('600519')
            self.assertFalse(first['trading_hours_only'])
            self.assertFalse(first['quant_enabled'])
            self.assertIsNone(first['quant_config'])

            db.update_monitored_stock(
                monitor_id, '买入', {'min': 1450, 'max': 1520},
                1750, 1380, 30, True, True, name='茅台')
            updated = db.get_monitor_by_code('600519')
            self.assertEqual(updated['name'], '茅台')
            self.assertTrue(updated['trading_hours_only'])


class RecommendationLifecycleTests(unittest.TestCase):
    def test_sqlite_table_initializes_and_recommendation_closes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_path = ai_recommendation_monitor._DB_PATH
            original_ready = ai_recommendation_monitor._perf_cols_ready
            ai_recommendation_monitor._DB_PATH = os.path.join(tmp, 'recommendations.db')
            ai_recommendation_monitor._perf_cols_ready = False
            try:
                rec_id = ai_recommendation_monitor.save_recommendation(
                    '600519', '贵州茅台', source='test', rating='买入',
                    ref_price=100, take_profit=120, stop_loss=90)
                rows = ai_recommendation_monitor.list_active(symbol='600519')
                self.assertEqual(rows[0]['id'], rec_id)

                closed = ai_recommendation_monitor.close_recommendation(
                    rec_id, reason='test', close_price=110)
                self.assertTrue(closed['closed'])
                self.assertEqual(closed['realized_pnl_pct'], 10.0)
                self.assertEqual(
                    ai_recommendation_monitor.close_recommendation(
                        rec_id, reason='again', close_price=105)['error'],
                    'already_closed',
                )
                self.assertEqual(
                    ai_recommendation_monitor.list_active(symbol='600519'), [])
            finally:
                ai_recommendation_monitor._DB_PATH = original_path
                ai_recommendation_monitor._perf_cols_ready = original_ready


class RagDisabledTests(unittest.TestCase):
    def test_disabled_rag_does_not_call_embedding_service(self):
        from rag import service
        with mock.patch.dict(os.environ, {'RAG_ENABLED': 'false'}):
            with mock.patch.object(
                    service.embed_client, 'embed_one',
                    side_effect=AssertionError('disabled RAG must not embed')):
                self.assertEqual(service.semantic_search('任意查询'), [])
            result = service.ingest_all()
            self.assertTrue(result['disabled'])
            self.assertEqual(result['analysis'], 0)


class AsyncTaskControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = task_control._DB_PATH
        self.original_initialized = task_control._INITIALIZED
        self.original_resolve = task_control._resolve_task
        self.original_stale_check = task_control._LAST_STALE_CHECK
        task_control._DB_PATH = os.path.join(self.tmp.name, 'task_runs.db')
        task_control._INITIALIZED = False
        task_control._LAST_STALE_CHECK = 0.0

    def tearDown(self):
        task_control._DB_PATH = self.original_db_path
        task_control._INITIALIZED = self.original_initialized
        task_control._resolve_task = self.original_resolve
        task_control._LAST_STALE_CHECK = self.original_stale_check
        self.tmp.cleanup()

    def test_submit_is_async_queryable_and_idempotent(self):
        calls = []

        def fake_task():
            calls.append('called')
            return {'value': 42}

        task_control._resolve_task = lambda _name: fake_task
        first = task_control.submit_task(
            'fake_job', requested_by='test', idempotency_key='same-intent')
        second = task_control.submit_task(
            'fake_job', requested_by='test', idempotency_key='same-intent')

        self.assertTrue(first['accepted'])
        self.assertEqual(second['run_id'], first['run_id'])
        self.assertTrue(second['duplicate'])

        claimed = task_control.claim_next_task('test-worker')
        self.assertEqual(claimed['run_id'], first['run_id'])
        task_control.execute_claimed_task(claimed)
        row = task_control.get_task_run(first['run_id'])

        self.assertIsNotNone(row)
        self.assertEqual(row['status'], 'success')
        self.assertEqual(row['result']['return_value']['value'], 42)
        self.assertEqual(calls, ['called'])

    def test_latest_selection_reports_stale_snapshot(self):
        task_control._init_db()
        conn = task_control.db_connect(task_control._DB_PATH)
        conn.execute('''
            INSERT INTO indicator_snapshots(symbol, snapshot_date, indicators)
            VALUES (?, ?, ?)
        ''', ('_last_selection', '2020-01-01', json.dumps({'picks': ['600519']})))
        conn.commit()
        conn.close()

        artifact = task_control.latest_selection_artifact()

        self.assertEqual(artifact['status'], 'stale')
        self.assertEqual(artifact['data']['picks'], ['600519'])
        self.assertTrue(artifact['meta']['warnings'])

    def test_stale_worker_is_requeued_then_interrupted_after_retry_limit(self):
        task_control._resolve_task = lambda _name: lambda: None
        submitted = task_control.submit_task(
            'fake_job', requested_by='test', idempotency_key='recover', max_attempts=2)
        first = task_control.claim_next_task('worker-1')
        self.assertEqual(first['attempts'], 1)

        stale = (datetime.now().astimezone() - timedelta(minutes=10)).isoformat(
            timespec='seconds')
        conn = task_control.db_connect(task_control._DB_PATH)
        conn.execute(
            'UPDATE manual_task_runs SET heartbeat_at=? WHERE run_id=?',
            (stale, submitted['run_id']),
        )
        conn.commit()
        conn.close()
        task_control._LAST_STALE_CHECK = 0.0

        recovered = task_control.recover_stale_runs(stale_after_seconds=60)
        self.assertEqual(recovered['requeued'], 1)
        self.assertEqual(task_control.get_task_run(submitted['run_id'])['status'], 'queued')

        second = task_control.claim_next_task('worker-2')
        self.assertEqual(second['attempts'], 2)
        conn = task_control.db_connect(task_control._DB_PATH)
        conn.execute(
            'UPDATE manual_task_runs SET heartbeat_at=? WHERE run_id=?',
            (stale, submitted['run_id']),
        )
        conn.commit()
        conn.close()
        task_control._LAST_STALE_CHECK = 0.0

        exhausted = task_control.recover_stale_runs(stale_after_seconds=60)
        row = task_control.get_task_run(submitted['run_id'])
        self.assertEqual(exhausted['interrupted'], 1)
        self.assertEqual(row['status'], 'interrupted')
        self.assertIsNone(task_control.claim_next_task('worker-3'))


if __name__ == '__main__':
    unittest.main()

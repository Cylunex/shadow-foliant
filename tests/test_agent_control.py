import os
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import pandas as pd

from selection.main_force_selector import MainForceStockSelector
from jobs import task_control
from jobs import ai_recommendation_monitor
from jobs import jobs_hub
from jobs.automation_config import REGISTRY
from data.factor_collector import _snapshot_date
from monitor import monitor_db as monitor_db_module
from monitor.monitor_db import StockMonitorDatabase


def _sqlite_test_connect(path, **_kwargs):
    return sqlite3.connect(path)


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
            with mock.patch.object(monitor_db_module, 'USE_POSTGRES', False), \
                    mock.patch.object(monitor_db_module, 'db_connect', _sqlite_test_connect):
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
                with mock.patch.object(ai_recommendation_monitor, 'USE_POSTGRES', False), \
                        mock.patch.object(ai_recommendation_monitor, 'db_connect',
                                          _sqlite_test_connect):
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


class ScheduledDependencyTests(unittest.TestCase):
    def setUp(self):
        self.hub = jobs_hub._JobsHub()

    def tearDown(self):
        self.hub._executor.shutdown(wait=False)
        self.hub._manual_executor.shutdown(wait=False)

    def test_registry_exposes_hard_dependencies(self):
        for name in (
                'factor_collection', 'portfolio_indicator_snapshot', 'eod_outcomes'):
            self.assertEqual(REGISTRY[name]['depends_on'], ['kline_prefetch'])
        self.assertEqual(
            REGISTRY['mx_selection_review']['depends_on'], ['unified_selection'])

    def test_formal_selection_failure_does_not_run_external_candidate_fallback(self):
        with mock.patch.object(jobs_hub, '_skip_if_not_trading', return_value=False), \
                mock.patch(
                    'data.research_sync.ResearchSynchronizer.sync_calendar',
                    side_effect=ValueError('bad calendar'),
                ), mock.patch.object(jobs_hub, '_daily_candidate_pool') as fallback, \
                mock.patch.object(jobs_hub, '_log_run') as log_run:
            with self.assertRaisesRegex(ValueError, 'bad calendar'):
                jobs_hub.task_unified_selection()
        fallback.assert_not_called()
        log_run.assert_not_called()

    def test_scheduled_consumers_read_today_formal_artifacts_only(self):
        formal = {
            'selection_date': datetime.now().strftime('%Y-%m-%d'),
            'artifacts': {
                'formal_top15': {'payload': [
                    {'code': '600001', 'rank': 1, 'assigned_lane': 'core'},
                    {'code': '600002', 'rank': 2, 'assigned_lane': 'satellite'},
                ]},
                'formal_top5': {'payload': [
                    {'code': '600002', 'rank': 1, 'assigned_lane': 'satellite'},
                ]},
                'display_overlay': {'payload': [
                    {'code': '600001', 'name': '甲'}, {'code': '600002', 'name': '乙'},
                ]},
            },
        }
        with mock.patch('data.research_store.ResearchStore') as store_type:
            store_type.return_value.latest_formal_selection.return_value = formal
            self.assertEqual(jobs_hub._last_selection_picks(), ['600001', '600002'])
            self.assertEqual(jobs_hub._last_selection_picks('top5'), ['600002'])
            self.assertEqual(jobs_hub._formal_selection_rows('top5')[0]['name'], '乙')

    def test_naive_job_timestamp_is_stored_as_shanghai_time(self):
        normalized = jobs_hub._normalize_run_timestamp('2026-08-17T16:30:00')
        parsed = datetime.fromisoformat(normalized)

        self.assertEqual(parsed.utcoffset(), timedelta(hours=8))
        self.assertEqual(parsed.hour, 16)

    def test_pending_dependency_defers_without_consuming_worker(self):
        pending = {
            'state': 'pending',
            'dependencies': ('parent_job',),
            'details': [{
                'state': 'pending', 'dependency': 'parent_job', 'reason': 'running',
            }],
        }
        ready = {
            'state': 'ready',
            'dependencies': ('parent_job',),
            'details': [{
                'state': 'ready', 'dependency': 'parent_job', 'reason': 'success',
            }],
        }
        runner = self.hub._wrap('test_child_job', lambda: None)
        with mock.patch.object(self.hub._executor, 'submit') as submit:
            with mock.patch.object(jobs_hub, '_dependency_decision',
                                   return_value=pending):
                runner()
            submit.assert_not_called()
            self.assertIn('test_child_job', self.hub._deferred)

            with mock.patch.object(jobs_hub, '_dependency_decision',
                                   return_value=ready):
                self.hub._poll_deferred_tasks()
            submit.assert_called_once()
            self.assertNotIn('test_child_job', self.hub._deferred)

    def test_failed_dependency_is_skipped_not_executed(self):
        blocked = {
            'state': 'blocked',
            'dependencies': ('parent_job',),
            'details': [{
                'state': 'blocked', 'dependency': 'parent_job', 'reason': 'error',
            }],
        }
        runner = self.hub._wrap('test_child_job', lambda: None)
        with mock.patch.object(self.hub._executor, 'submit') as submit, \
                mock.patch.object(jobs_hub, '_log_run') as log_run, \
                mock.patch.object(jobs_hub, '_dependency_decision',
                                  return_value=blocked):
            runner()

        submit.assert_not_called()
        self.assertEqual(log_run.call_args.args[1], 'skipped')
        self.assertIn('dependency_failed', log_run.call_args.kwargs['error'])

    def test_restart_recovers_only_due_dependency_chain(self):
        when = (datetime.now().astimezone() - timedelta(minutes=1)).strftime('%H:%M')
        self.hub._registered = [{
            'name': 'test_child_job', 'when': when, 'job': mock.Mock(),
            'func': lambda: None, 'args': (), 'kwargs': {},
        }]
        pending = {
            'state': 'pending',
            'dependencies': ('parent_job',),
            'details': [{
                'state': 'pending', 'dependency': 'parent_job', 'reason': 'running',
            }],
        }
        with mock.patch.object(jobs_hub, '_job_dependencies',
                               return_value=('parent_job',)), \
                mock.patch.object(jobs_hub, '_latest_job_run_today',
                                  return_value=None), \
                mock.patch.object(jobs_hub, '_dependency_decision',
                                  return_value=pending), \
                mock.patch.object(self.hub._executor, 'submit') as submit:
            self.hub._recover_due_dependencies()

        submit.assert_not_called()
        self.assertIn('test_child_job', self.hub._deferred)

    def test_execution_timeout_is_error_not_generic_data_source_report(self):
        name = 'test_execution_timeout'
        with mock.patch.dict(jobs_hub._TASK_HARD_TIMEOUTS, {name: 0.02}), \
                mock.patch.object(jobs_hub, '_log_run') as log_run, \
                mock.patch.object(jobs_hub, '_notify_execution_timeout') as notify, \
                mock.patch.object(jobs_hub, '_notify_data_unavailable') as data_notice:
            jobs_hub._run_with_log(name, lambda: time.sleep(0.06))

        self.assertEqual(log_run.call_args.args[1], 'error')
        self.assertIn('execution_timeout', log_run.call_args.kwargs['error'])
        notify.assert_called_once()
        data_notice.assert_not_called()

    def test_dependency_timeout_is_skipped_and_cancels_barrier(self):
        name = 'test_dependency_timeout'

        def wait_for_parent():
            with jobs_hub._TASK_LOCK:
                jobs_hub._TASK_WAITING_ON[name] = 'parent_job'
                cancel = jobs_hub._TASK_CANCEL_EVENTS[name]
            cancel.wait(0.2)
            jobs_hub._log_run(
                name, 'skipped', error='dependency parent_job not ready')

        with mock.patch.dict(jobs_hub._TASK_HARD_TIMEOUTS, {name: 0.02}), \
                mock.patch.object(jobs_hub, '_log_run') as log_run, \
                mock.patch.object(jobs_hub, '_notify_dependency_wait') as notify, \
                mock.patch.object(jobs_hub, '_notify_execution_timeout') as execution_notice:
            jobs_hub._run_with_log(name, wait_for_parent)

        statuses = [call.args[1] for call in log_run.call_args_list]
        self.assertIn('skipped', statuses)
        notify.assert_called_once()
        execution_notice.assert_not_called()


class FactorCollectorDateTests(unittest.TestCase):
    def test_snapshot_date_does_not_double_apply_cst_offset(self):
        local_time = datetime(2026, 7, 29, 17, 13)
        self.assertEqual(_snapshot_date(local_time), '2026-07-29')


class AsyncTaskControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = task_control._DB_PATH
        self.original_initialized = task_control._INITIALIZED
        self.original_resolve = task_control._resolve_task
        self.original_stale_check = task_control._LAST_STALE_CHECK
        self.original_connect = task_control.db_connect
        self.original_pg = task_control.USE_POSTGRES
        task_control._DB_PATH = os.path.join(self.tmp.name, 'task_runs.db')
        task_control._INITIALIZED = False
        task_control._LAST_STALE_CHECK = 0.0
        task_control.db_connect = _sqlite_test_connect
        task_control.USE_POSTGRES = False

    def tearDown(self):
        task_control._DB_PATH = self.original_db_path
        task_control._INITIALIZED = self.original_initialized
        task_control._resolve_task = self.original_resolve
        task_control._LAST_STALE_CHECK = self.original_stale_check
        task_control.db_connect = self.original_connect
        task_control.USE_POSTGRES = self.original_pg
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

    def test_legacy_selection_snapshot_is_not_authoritative(self):
        task_control._init_db()
        conn = task_control.db_connect(task_control._DB_PATH)
        conn.execute('''
            INSERT INTO indicator_snapshots(symbol, snapshot_date, indicators)
            VALUES (?, ?, ?)
        ''', ('_last_selection', '2020-01-01', json.dumps({'picks': ['600519']})))
        conn.commit()
        conn.close()

        with mock.patch("data.research_store.ResearchStore") as store_type:
            store_type.return_value.latest_formal_selection.return_value = None
            artifact = task_control.latest_selection_artifact()

        self.assertEqual(artifact['status'], 'missing')
        self.assertEqual(artifact['data']['picks'], [])
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

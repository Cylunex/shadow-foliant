import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

import _bootstrap  # noqa: F401
import db_compat
from core import runtime_health
from jobs import task_control


class ManualDependencyQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = task_control._DB_PATH
        self.old_initialized = task_control._INITIALIZED
        self.old_task_pg = task_control.USE_POSTGRES
        self.old_db_pg = db_compat.USE_POSTGRES
        self.old_connect = task_control.db_connect
        task_control._DB_PATH = os.path.join(self.tmp.name, 'jobs.db')
        task_control._INITIALIZED = False
        task_control.USE_POSTGRES = False
        db_compat.USE_POSTGRES = False
        task_control.db_connect = lambda path, **_kwargs: sqlite3.connect(path)

    def tearDown(self):
        task_control._DB_PATH = self.old_db_path
        task_control._INITIALIZED = self.old_initialized
        task_control.USE_POSTGRES = self.old_task_pg
        db_compat.USE_POSTGRES = self.old_db_pg
        task_control.db_connect = self.old_connect
        self.tmp.cleanup()

    @staticmethod
    def _dependencies(name):
        return ('upstream',) if name == 'downstream' else ()

    def _mark_scheduled(self, name, status):
        conn = task_control.db_connect(task_control._DB_PATH)
        now = datetime.now().astimezone().isoformat(timespec='seconds')
        conn.execute('''
            INSERT INTO job_runs(job_name, started_at, finished_at, status, error)
            VALUES (?, ?, ?, ?, ?)
        ''', (name, now, now, status, None if status == 'success' else 'failed'))
        conn.commit()
        conn.close()

    def test_downstream_auto_queues_and_yields_to_upstream(self):
        with patch.object(task_control, '_resolve_task', return_value=lambda: None), \
                patch.object(task_control, '_task_dependencies', side_effect=self._dependencies), \
                patch.object(task_control, '_dependency_enabled', return_value=True):
            submitted = task_control.submit_task('downstream', idempotency_key='intent-1')
            self.assertEqual(submitted['dependencies'][0]['task_name'], 'upstream')

            first = task_control.claim_next_task('worker-1')
            self.assertEqual(first['task_name'], 'upstream')
            task_control._update_run(
                first['run_id'], status='success', finished_at=task_control._now())
            self._mark_scheduled('upstream', 'success')

            second = task_control.claim_next_task('worker-1')
            self.assertEqual(second['task_name'], 'downstream')

    def test_failed_upstream_skips_downstream(self):
        with patch.object(task_control, '_resolve_task', return_value=lambda: None), \
                patch.object(task_control, '_task_dependencies', side_effect=self._dependencies), \
                patch.object(task_control, '_dependency_enabled', return_value=True):
            submitted = task_control.submit_task('downstream', idempotency_key='intent-2')
            upstream = task_control.claim_next_task('worker-1')
            task_control._update_run(
                upstream['run_id'], status='error', finished_at=task_control._now(),
                error='boom')
            self._mark_scheduled('upstream', 'error')

            self.assertIsNone(task_control.claim_next_task('worker-1'))
            downstream = task_control.get_task_run(submitted['run_id'])
            self.assertEqual(downstream['status'], 'skipped')
            self.assertIn('dependency_failed', downstream['error'])


class RuntimeHealthTests(unittest.TestCase):
    def test_health_snapshot_never_returns_secret_values(self):
        secrets = {
            'DEEPSEEK_API_KEY': 'secret-llm-value',
            'QQ_WEBHOOK_URL': 'https://example.invalid/private-hook',
            'PG_PASSWORD': 'secret-db-value',
        }
        with patch.dict(os.environ, secrets, clear=False), \
                patch.object(runtime_health, '_database_check',
                             return_value={'ok': True, 'backend': 'sqlite'}), \
                patch.object(runtime_health, '_queue_check',
                             return_value={'ok': True, 'queued': 1, 'running': 0}):
            payload = runtime_health.snapshot()
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertTrue(payload['ready'])
        self.assertTrue(payload['features']['llm_configured'])
        self.assertTrue(payload['features']['notification_configured'])
        for value in secrets.values():
            self.assertNotIn(value, encoded)


if __name__ == '__main__':
    unittest.main()

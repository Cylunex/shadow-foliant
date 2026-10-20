"""Regression tests for partial fund runs and legacy PostgreSQL status enums."""
import os
from pathlib import Path
import uuid
from unittest.mock import Mock

import pytest
import _bootstrap
import fund_db
from fund import fund_service
from jobs import jobs_hub as hub


@pytest.mark.parametrize('fresh,total,expected', [(1, 2, 'partial'), (2, 2, 'success')])
def test_nav_refresh_returns_its_actual_outcome(monkeypatch, fresh, total, expected):
    import fund_data
    from fund import fund_ledger
    from portfolio import daily_pnl
    monkeypatch.setattr(hub, '_fund_gate', lambda _: False)
    for name, value in {'init_db': None, 'get_holdings': [], 'get_plans': [],
                        'get_nav_pairs': {}, 'save_portfolio_snapshot': {}}.items():
        monkeypatch.setattr(fund_db, name, Mock(return_value=value))
    monkeypatch.setattr(fund_data, 'get_nav_history', Mock(side_effect=AssertionError('network forbidden')))
    monkeypatch.setattr(fund_ledger, 'list_saved', Mock(return_value=[]))
    monkeypatch.setattr(fund_ledger, 'read', Mock(return_value={'transactions': []}))
    monkeypatch.setattr(daily_pnl, 'calculate_fund_pnl', Mock(return_value={
        'fund_total_count':total, 'fund_fresh_count':fresh, 'fund_count':total,
        'fund_mv':100, 'fund_daily_pnl':1, 'fund_daily_pct':1}))
    monkeypatch.setattr(daily_pnl, 'upsert_fund_pnl', Mock())
    logs = Mock()
    monkeypatch.setattr(hub, '_log_run', logs)
    result = hub.task_fund_nav_refresh()
    assert result['status'] == expected
    assert logs.call_args.args == ('fund_nav_refresh', expected)
    assert f'fresh={fresh}/{total}' in result['error']


@pytest.fixture
def evening(monkeypatch):
    monkeypatch.setattr(hub, '_fund_gate', lambda _: False)
    monkeypatch.setattr(fund_db, 'init_db', lambda: None)
    review = Mock(return_value=[])
    monkeypatch.setattr(fund_service, 'review_week', review)
    logs = Mock()
    monkeypatch.setattr(hub, '_log_run', logs)
    return review, logs


@pytest.mark.parametrize('nav,target,review,expected', [
    ('success', 'success', 'ready', 'success'),
    ('partial', 'success', 'ready', 'partial'),
    ('success', 'success', 'partial', 'partial'),
    ('error', 'success', 'ready', 'error'),
    ('success', 'error', 'partial', 'error'),
])
def test_evening_retains_child_outcomes(evening, monkeypatch, nav, target, review, expected):
    reviewer, logs = evening
    reviewer.return_value = [{'status': review}]
    steps = []
    def step(name, state):
        steps.append(name)
        return {'status': state, 'error': 'fixture'}
    monkeypatch.setattr(hub, 'task_fund_nav_refresh', lambda: step('nav', nav))
    monkeypatch.setattr(hub, 'task_fund_target_check', lambda: step('target', target))
    result = hub.task_fund_evening()
    assert steps == ['nav', 'target']
    assert result['status'] == expected
    assert logs.call_args.args == ('fund_evening', expected)
    assert f'fund_nav_refresh={nav}' in logs.call_args.kwargs['error']
    assert logs.call_args.kwargs['finished_at']


def test_evening_continues_after_escaped_step_error(evening, monkeypatch):
    reviewer, logs = evening
    monkeypatch.setattr(hub, 'task_fund_nav_refresh', Mock(side_effect=RuntimeError('fixture')))
    target = Mock(return_value={'status': 'success'})
    monkeypatch.setattr(hub, 'task_fund_target_check', target)
    assert hub.task_fund_evening()['status'] == 'error'
    target.assert_called_once()
    reviewer.assert_called_once()
    assert logs.call_args.args == ('fund_evening', 'error')


def test_evening_retains_review_exception(evening, monkeypatch):
    reviewer, logs = evening
    reviewer.side_effect = ValueError('fixture')
    monkeypatch.setattr(hub, 'task_fund_nav_refresh', lambda: None)
    monkeypatch.setattr(hub, 'task_fund_target_check', lambda: {'status': 'success'})
    assert hub.task_fund_evening()['status'] == 'partial'
    assert 'fund_weekly_review=partial ValueError' in logs.call_args.kwargs['error']


@pytest.mark.parametrize('state', ['partial', 'degraded'])
def test_runner_does_not_print_success_for_partial_state(monkeypatch, capsys, state):
    from jobs import isolated_runtime
    monkeypatch.setattr(isolated_runtime, 'run_isolated_task', lambda *a, **kw: {'status': 'success'})
    monkeypatch.setattr(hub, '_latest_job_run_since', lambda *a: {'status': state, 'error': 'fixture'})
    hub._run_with_log('fund_evening', lambda: None)
    output = capsys.readouterr().out
    assert '降级完成' in output
    assert '✅' not in output


@pytest.mark.skipif(not (os.getenv('JOB_STATUS_TEST_PG_DSN') or
                        os.getenv('RUN_POSTGRES_INTEGRATION') == '1'),
                    reason='Requires isolated PostgreSQL')
@pytest.mark.parametrize('legacy_enum', [True, False])
def test_job_status_migration_preserves_real_logged_outcomes(monkeypatch, legacy_enum):
    import psycopg2
    from psycopg2 import sql
    from core.db_compat import _PGConnection
    from core.database_settings import DatabaseSettings
    from jobs.task_health import summarize_task_health

    dsn = os.getenv('JOB_STATUS_TEST_PG_DSN')
    conn = (psycopg2.connect(dsn) if dsn else
            psycopg2.connect(**DatabaseSettings.from_env().connect_kwargs()))
    schema = 'job_status_test_' + uuid.uuid4().hex
    cur = conn.cursor()
    cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    cur.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
    if legacy_enum:
        cur.execute("CREATE TYPE job_status_enum AS ENUM ('success','error','skipped','running')")
    cur.execute('CREATE TABLE job_runs (job_name TEXT, started_at TIMESTAMPTZ, '
                'finished_at TIMESTAMPTZ, status ' + ('job_status_enum' if legacy_enum else 'TEXT') + ', error TEXT)')
    cur.execute('CREATE TABLE research_schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT)')
    cur.execute("INSERT INTO job_runs VALUES ('old',now(),now(),'success','retained')")
    migration = (Path(__file__).resolve().parents[1] / 'scripts/migrations/23-job-run-status-text.sql').read_text()
    try:
        cur.execute(migration)
        cur.execute(migration)
        conn.commit()
        wrapped = _PGConnection.__new__(_PGConnection)
        wrapped._conn = conn
        monkeypatch.setattr(hub, '_get_log_db', lambda: wrapped)
        for state in ('partial', 'degraded'):
            hub._log_run(state, state, error='fresh=1/2', finished_at='2026-10-20T22:04:00+08:00')
        cur.execute('SELECT job_name,status,error FROM job_runs ORDER BY job_name')
        assert cur.fetchall() == [('degraded','degraded','fresh=1/2'), ('old','success','retained'),
                                  ('partial','partial','fresh=1/2')]
        health = summarize_task_health([{'name':'fund_evening','enabled':True,
            'last_run':{'status':'partial','error':'fresh=1/2'}}])
        assert health['failed_recent'][0]['status'] == 'partial'
    finally:
        conn.rollback()
        cur.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        conn.commit()
        conn.close()

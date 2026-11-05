"""Opt-in real CLI tests, every command scoped to the fixture's disposable schema."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import _bootstrap  # noqa: F401
from test_trade_import_postgres import database

pytestmark = pytest.mark.skipif(os.getenv('RUN_POSTGRES_INTEGRATION') != '1', reason='isolated PostgreSQL required')
ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT/'agent/skills/foliant-personal-portfolio/scripts/foliant'


def test_cli_import_query_retry_and_personal_parameters(database, tmp_path):
    from db_compat import _PGConnection
    from application.run_repository import RunRepository
    db, connect = database
    def repository_connect(_):
        wrapped = _PGConnection.__new__(_PGConnection)
        wrapped._conn = connect()
        return wrapped
    RunRepository(connect_fn=repository_connect, is_postgres=True, ensure_schema=True)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('select current_schema()'); schema = cur.fetchone()[0]
    config=tmp_path/'personal.json';config.write_text(json.dumps({'actor':'synthetic-cli','allow_writes':True}))
    env={**os.environ,'PGOPTIONS':f'-c search_path={schema}'}
    def call(name,args):
        result=subprocess.run([sys.executable,str(CLI),'--config',str(config),'call',name], input=json.dumps(args),text=True,capture_output=True,env=env)
        payload=json.loads(result.stdout)
        assert result.returncode == 0, payload
        return payload
    row={'code':'600001','name':'合成股票','trade_type':'买入','quantity':100,'price':10,'trade_time':'2026-01-05 10:30:00'}
    args={'rows':[row],'idempotency_key':'synthetic-cli-trade-001'}
    saved=call('trades.import',args)
    assert saved['status']=='complete' and saved['receipt']
    assert call('trades.import',args)['replayed'] is True
    assert db.get_all_stocks()[0]['quantity']==100
    latest=call('trades.list',{'limit':1})['data']['records'][0]
    assert latest['code']=='600001' and latest['quantity']==100
    assert latest['source']=='skill:foliant-personal-portfolio'
    assert call('trades.receipt',{'batch_id':saved['batch_id']})['receipt']==saved['receipt']
    sell=call('trades.import',{'rows':[{**row,'trade_type':'卖出','quantity':200}],'idempotency_key':'synthetic-cli-sell-001'})
    assert sell['status']=='needs_input' and db.get_all_stocks()[0]['quantity']==100
    params={r['key']:r['current'] for r in call('strategy.parameters',{})}
    change={'key':'max_position_pct','value':8,'expected_value':params['max_position_pct'],'reason':'合成参数验证'}
    assert call('strategy.update',change)['status']=='preview'
    assert {r['key']:r['current'] for r in call('strategy.parameters',{})}['max_position_pct']==params['max_position_pct']
    updated=call('strategy.update',{**change,'dry_run':False})
    assert updated['after']==8 and updated['receipt']
    history=call('strategy.history',{})
    assert len(history)==1 and history[0]['receipt']==updated['receipt']
    assert call('strategy.update',{**change,'dry_run':False,'value':7})['status']=='conflict'
    assert {r['key']:r['current'] for r in call('strategy.parameters',{})}['max_position_pct']==8

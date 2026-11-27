"""Opt-in real CLI tests, every command scoped to the fixture's disposable schema."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

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
    reader = tmp_path / 'reader.json'
    reader.write_text(json.dumps({'actor': 'synthetic-cli'}))
    stranger = tmp_path / 'stranger.json'
    stranger.write_text(json.dumps({'actor': 'other-synthetic-cli'}))
    def call(name,args, *, identity=config, error=None):
        result=subprocess.run([sys.executable,str(CLI),'--config',str(identity),'call',name], input=json.dumps(args),text=True,capture_output=True,env=env)
        payload=json.loads(result.stdout)
        if error:
            assert result.returncode == 1 and payload == {'isError': True, 'code': error}
        else:
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
    assert call('trades.get',{'id':latest['id']},identity=reader)['data']['record']==latest
    assert call('trades.get',{'id':latest['id']+10000},identity=reader)['data']['record'] is None
    by_batch=call('trades.receipt',{'batch_id':saved['batch_id']},identity=reader)
    by_key={'idempotency_key': args['idempotency_key']}
    assert by_batch['receipt']==saved['receipt']
    assert call('trades.receipt',by_key,identity=reader)==by_batch
    call('trades.receipt',by_key,identity=stranger,error='trade_review_not_found')
    call('trades.receipt',{'idempotency_key':'synthetic-cli-missing'},identity=reader,error='trade_review_not_found')
    call('trades.import',{**args,'rows':[{**row,'price':20}]},error='idempotency_conflict')
    assert call('trades.receipt',by_key,identity=reader)==by_batch
    assert len(db.get_trades())==1
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


def test_personal_trade_pages_read_old_executions_and_survive_a_removed_anchor(database, monkeypatch):
    from application import personal_skill
    from application.services import ApplicationError, PortfolioAccessService

    db, connect = database
    service = PortfolioAccessService(portfolio_db=db)
    monkeypatch.setattr(personal_skill, 'services', lambda: SimpleNamespace(portfolio=service))
    reader = {'actor': 'synthetic-reader'}

    def page(**args):
        return personal_skill.invoke('trades.page', args, reader)['data']

    assert page() == {'records': [], 'next_cursor': None}
    for invalid in ['bad-token', 'v1.0.0', 'v1.999999999999999999.1', 'v1.0.9223372036854775808']:
        with pytest.raises(ApplicationError) as raised:
            page(cursor=invalid)
        assert raised.value.code == 'invalid_cursor'
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_records(stock_code, trade_type, order_id, trade_time)
            SELECT '600001', CASE WHEN n % 2 = 0 THEN '买入' ELSE '卖出' END, n::text,
                   TIMESTAMPTZ '2026-01-05 10:30:00.000001+08'
                       + CASE WHEN n <= 2 THEN INTERVAL '1 microsecond' ELSE INTERVAL '0' END
            FROM generate_series(1, 504) AS sample(n) ORDER BY sample.n
        """)
        cur.execute("INSERT INTO trade_records(stock_code, trade_type) VALUES ('600001', '调整')")

    first = page(limit=1)
    anchor = first['records'][0]
    assert anchor['order_id'] == '2'
    assert anchor == personal_skill.invoke('trades.get', {'id': anchor['id']}, reader)['data']['record']
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM trade_records WHERE id = %s", (anchor['id'],))
        cur.execute("""
            INSERT INTO trade_records(stock_code, trade_type, trade_time)
            VALUES ('600001', '买入', TIMESTAMPTZ '2026-01-06 10:30:00+08')
        """)

    second = page(cursor=first['next_cursor'], limit=2)
    assert [r['order_id'] for r in second['records']] == ['1', '504']
    older = page(cursor=second['next_cursor'], limit=500)
    assert [r['order_id'] for r in older['records']] == [str(n) for n in range(503, 3, -1)]
    last = page(cursor=older['next_cursor'], limit=1)
    assert [r['order_id'] for r in last['records']] == ['3']
    assert last['next_cursor'] is None

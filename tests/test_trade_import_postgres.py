"""Opt-in PostgreSQL import tests; every write uses a disposable schema."""
import os
from pathlib import Path
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.getenv('RUN_POSTGRES_INTEGRATION') != '1',
                                reason='PostgreSQL integration is opt-in')


@pytest.fixture(params=['UTC', 'Asia/Shanghai'])
def database(request, monkeypatch):
    import psycopg2
    from psycopg2 import sql
    from portfolio import portfolio_db_pg as module

    schema = 'test_trade_time_' + uuid.uuid4().hex
    admin = psycopg2.connect(**module.DB_CONFIG)
    connections = []
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))

    def connect():
        conn = psycopg2.connect(**module.DB_CONFIG)
        connections.append(conn)
        with conn.cursor() as cur:
            cur.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
            cur.execute('SET TIME ZONE %s', (request.param,))
        return conn

    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute((Path(__file__).resolve().parents[1] / 'scripts/init_postgres.sql').read_text())
        monkeypatch.setattr(module, 'get_conn', connect)
        yield module.PortfolioDBPG(), connect
    finally:
        for conn in connections:
            conn.close()
        with admin.cursor() as cur:
            cur.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()


def test_shanghai_import_round_trip_and_idempotency_do_not_depend_on_db_timezone(database, monkeypatch):
    from portfolio.trade_import_service import import_trade_records
    db, connect = database
    row = {'code': '600001', 'name': '测试', 'quantity': 100, 'price': 10,
           'trade_type': '买入', 'trade_time': '2026-09-18 10:30:00'}
    result = import_trade_records([row], portfolio_db=db)
    assert result['imported'] == result['positions_updated'] == 1
    assert db.get_trades()[0]['trade_time'].isoformat() == '2026-09-18T10:30:00+08:00'
    from core import database_pg
    from webui.api_server import portfolio_trade_records
    monkeypatch.setattr(database_pg, 'get_conn', connect)
    response = portfolio_trade_records(days=0)
    assert response['data'][0]['trade_time'] == '2026-09-18 10:30:00'
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_char(trade_time AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'), "
                        "extra->>'trade_time_basis' FROM trade_records WHERE trade_type='买入'")
            assert cur.fetchone() == ('2026-09-18 02:30:00', 'asia_shanghai_v1')
    repeated = import_trade_records([dict(row, trade_time='2026-09-18T02:30:00Z')], portfolio_db=db)
    assert repeated['status'] == 'noop'
    assert repeated['skipped_existing'] == 1
    assert db.get_all_stocks()[0]['quantity'] == 100


def test_direct_import_without_fingerprint_has_canonical_legacy_lookup(database):
    from portfolio.trade_import_service import import_trade_records
    db, _ = database
    row = {'code': '600001', 'name': '测试', 'quantity': 100, 'price': 10,
           'trade_type': '买入', 'trade_time': '2026-09-18T10:30:00+08:00'}
    assert db.import_trades([row], update_position=False)['imported'] == 1
    repeated = import_trade_records([dict(row, trade_time='2026-09-18T02:30:00Z')],
                                    portfolio_db=db, update_position=False)
    assert repeated['status'] == 'noop'


def test_mixed_timezone_batch_is_applied_in_execution_order(database):
    from portfolio.trade_import_service import import_trade_records
    db, _ = database
    base = {'code': '600001', 'name': '测试', 'quantity': 100, 'price': 10}
    result = import_trade_records([
        dict(base, trade_type='卖出', trade_time='2026-09-18T02:31:00Z'),
        dict(base, trade_type='买入', trade_time='2026-09-18 10:30:00'),
    ], portfolio_db=db)
    assert result['imported'] == 2
    assert all(stock['quantity'] == 0 for stock in db.get_all_stocks())

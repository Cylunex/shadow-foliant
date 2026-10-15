"""基金数据库 —— 生产运行统一使用 db_compat 的 PostgreSQL 连接。

表:
  funds             基金基本信息(代码/简称/类型/经理/规模,缓存自数据源)
  fund_nav          净值历史缓存(code+date 唯一)
  fund_holdings     我的持有(份额/成本净值)
  fund_dca_plans    定投计划(周期/金额/策略/止盈/开关)
  fund_transactions 申赎/定投流水 + 持仓快照(对齐股票 trade_records 设计)

申赎流水写入时自动更新 fund_holdings(买入加仓重算移动加权成本、赎回减仓)。
历史 SQL 仍用 `?` 占位符，由 db_compat 转成 PostgreSQL `%s`；非 PG 分支仅供测试注入。
"""

from __future__ import annotations

from typing import Dict, List, Optional

import os
import sys
# 路径引导:作为库被 import 时入口已 import _bootstrap;独立运行本文件时补根目录到 sys.path
if not any(os.path.basename(p) == 'shadow-foliant' for p in sys.path):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402

from db_compat import connect, is_postgres
from fund_dca import moving_cost

_DB_FILE = 'fund.db'
import threading
_INIT_LOCK = threading.RLock()
_INITIALIZED_FOR = None


def _conn():
    return connect(_bootstrap.db_path(_DB_FILE), check_same_thread=False) if not is_postgres() else connect()


# --------------------------------------------------------------------------
# 建表(幂等)。生产使用 PG 类型；另一分支仅供显式注入的测试连接。
# --------------------------------------------------------------------------
def _ddl() -> List[str]:
    if is_postgres():
        pk = 'BIGSERIAL PRIMARY KEY'
        ts = 'TIMESTAMPTZ DEFAULT NOW()'
        num = 'DOUBLE PRECISION'
    else:
        pk = 'INTEGER PRIMARY KEY AUTOINCREMENT'
        ts = "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
        num = 'REAL'
    return [
        f"""CREATE TABLE IF NOT EXISTS funds (
            code TEXT PRIMARY KEY, name TEXT, ftype TEXT,
            manager TEXT, scale {num}, updated_at {ts})""",
        f"""CREATE TABLE IF NOT EXISTS fund_nav (
            code TEXT, nav_date DATE, unit_nav {num}, acc_nav {num},
            daily_return {num}, PRIMARY KEY (code, nav_date))""",
        f"""CREATE TABLE IF NOT EXISTS fund_holdings (
            code TEXT PRIMARY KEY, name TEXT,
            shares {num}, cost_nav {num}, note TEXT, updated_at {ts})""",
        f"""CREATE TABLE IF NOT EXISTS fund_dca_plans (
            id {pk}, code TEXT, name TEXT,
            period TEXT, amount {num}, day_of INTEGER,
            strategy TEXT DEFAULT 'normal', enabled INTEGER DEFAULT 1,
            target_profit_pct {num}, auto_record INTEGER DEFAULT 0,
            note TEXT, created_at {ts})""",
        f"""CREATE TABLE IF NOT EXISTS fund_transactions (
            id {pk}, code TEXT, name TEXT, txn_type TEXT,
            nav {num}, shares {num}, amount {num}, fee {num},
            trade_date DATE,
            pos_shares {num}, pos_cost_nav {num}, delta_shares {num},
            source TEXT DEFAULT 'manual', note TEXT, created_at {ts})""",
        f"""CREATE TABLE IF NOT EXISTS fund_portfolio_snapshots (
            snap_date DATE PRIMARY KEY, total_mv {num}, total_cost {num},
            pnl_pct {num}, n_funds INTEGER, holdings_json TEXT, created_at {ts})""",
    ]


def init_db():
    global _INITIALIZED_FOR
    if _INITIALIZED_FOR is _conn:return
    with _INIT_LOCK:
        if _INITIALIZED_FOR is _conn:return
        conn = _conn()
        try:
            cur = conn.cursor()
            for sql in _ddl():cur.execute(sql)
            from fund.fund_ledger import create_schema
            create_schema(cur)
            conn.commit()
        except Exception:
            conn.rollback();raise
        finally:conn.close()
        _ensure_columns()
        _INITIALIZED_FOR = _conn


def _ensure_columns():
    conn=_conn()
    try:
        cur=conn.cursor()
        if is_postgres():
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='fund_dca_plans'")
            names={r[0] for r in cur.fetchall()}
        else:
            cur.execute('PRAGMA table_info(fund_dca_plans)')
            names={r[1] for r in cur.fetchall()}
        if 'auto_record' not in names:cur.execute('ALTER TABLE fund_dca_plans ADD COLUMN auto_record INTEGER DEFAULT 0')
        conn.commit()
    except Exception:
        conn.rollback();raise
    finally:conn.close()


def has_transaction_on(code: str, trade_date: str, source: str) -> bool:
    """某基金某日某来源是否已有流水(定投自动记账去重用,防重复)。"""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""SELECT 1 FROM fund_transactions
                   WHERE code=? AND trade_date=? AND source=? LIMIT 1""",
                (str(code).zfill(6), trade_date, source))
    hit = cur.fetchone() is not None
    conn.close()
    return hit


# --------------------------------------------------------------------------
# 持有 CRUD
# --------------------------------------------------------------------------
def get_holdings() -> List[Dict]:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT code, name, shares, cost_nav, note FROM fund_holdings ORDER BY code")
    rows = cur.fetchall()
    conn.close()
    return [{'code': r[0], 'name': r[1], 'shares': r[2], 'cost_nav': r[3], 'note': r[4]} for r in rows]


def upsert_holding(code: str, name: str = None, shares: float = None,
                   cost_nav: float = None, note: str = None):
    init_db()
    code = str(code).zfill(6)
    conn = _conn()
    cur = conn.cursor()
    cur.execute('SELECT 1 FROM fund_ledger_openings WHERE code=?',(code,))
    if cur.fetchone():
        conn.close();raise ValueError('已接入账本的基金须通过期初余额迁移或成交流水修改')
    cur.execute("SELECT code FROM fund_holdings WHERE code=?", (code,))
    exists = cur.fetchone()
    if exists:
        cur.execute("""UPDATE fund_holdings SET
            name=COALESCE(?,name), shares=COALESCE(?,shares),
            cost_nav=COALESCE(?,cost_nav), note=COALESCE(?,note)
            WHERE code=?""", (name, shares, cost_nav, note, code))
    else:
        cur.execute("""INSERT INTO fund_holdings (code, name, shares, cost_nav, note)
            VALUES (?,?,?,?,?)""", (code, name, shares or 0, cost_nav or 0, note))
    conn.commit()
    conn.close()


def delete_holding(code: str):
    init_db()
    conn = _conn()
    cur = conn.cursor()
    cur.execute('SELECT 1 FROM fund_ledger_openings WHERE code=?',(str(code).zfill(6),))
    if cur.fetchone():
        conn.close();raise ValueError('已接入账本的持仓不能直接移除，请通过实际赎回或冲正处理')
    cur.execute("DELETE FROM fund_holdings WHERE code=?", (str(code).zfill(6),))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# 申赎/定投流水(自动更新持有)
# --------------------------------------------------------------------------
def add_transaction(code: str, txn_type: str, nav: float, amount=None, shares=None,
                    fee=0., trade_date=None, name=None, source='manual', note=None,
                    update_position=True, **details):
    from fund import fund_ledger as ledger
    row = dict(code=code,txn_type=txn_type,nav=nav,amount=amount,shares=shares,fee=fee,
               trade_date=trade_date,name=name,source=source,note=note,**details)
    if not update_position:
        event=ledger.normalize(row)
        ledger.save('simulation',event['event_id'],event)
        return {**event,'status':'simulated','pos_shares':None}
    result=ledger.commit([row])
    code=str(code).zfill(6)
    position=next((p for p in result['positions'] if p['code']==code and p['account']==details.get('account','default') and p['channel']==details.get('channel','default')), {})
    return {**row,**position,'pos_shares':position.get('shares'),'pos_cost_nav':position.get('cost_nav'),
            'event_id':ledger.normalize(row)['event_id'],'skipped':result['skipped']}


def get_transactions(code: str = None) -> List[Dict]:
    conn = _conn()
    cur = conn.cursor()
    if code:
        cur.execute("""SELECT code,name,txn_type,nav,shares,amount,fee,trade_date,
            pos_shares,pos_cost_nav,delta_shares,source,note FROM fund_transactions
            WHERE code=? ORDER BY trade_date, id""", (str(code).zfill(6),))
    else:
        cur.execute("""SELECT code,name,txn_type,nav,shares,amount,fee,trade_date,
            pos_shares,pos_cost_nav,delta_shares,source,note FROM fund_transactions
            ORDER BY trade_date, id""")
    cols = ['code', 'name', 'txn_type', 'nav', 'shares', 'amount', 'fee', 'trade_date',
            'pos_shares', 'pos_cost_nav', 'delta_shares', 'source', 'note']
    rows = cur.fetchall()
    conn.close()
    old = [{**dict(zip(cols,r)), 'history_status':'legacy_unverified'} for r in rows]
    from fund.fund_ledger import read
    new = read([str(code).zfill(6)] if code else None)['transactions']
    return sorted(old+new,key=lambda r:str(r.get('trade_date') or ''))


# --------------------------------------------------------------------------
# 定投计划
# --------------------------------------------------------------------------
def add_plan(code: str, amount: float, period: str = 'monthly', day_of: int = 1,
             strategy: str = 'normal', name: str = None,
             target_profit_pct: float = None, auto_record: bool = False,
             note: str = None, **settings) -> int:
    from fund.fund_contracts import number, now, code6
    from fund.fund_calendar import scheduled_dates
    code6(code); number(amount,positive=True)
    settings={k:v for k,v in settings.items() if v is not None}
    anchor = settings.get('anchor_date') or now()[:10]
    list(scheduled_dates(anchor,anchor,period,day_of,anchor))
    if strategy not in ('normal','price_position','valuation','value_avg'): raise ValueError('无效策略')
    from fund.fund_ledger import transaction, _save
    with transaction(True) as cur:
        cur.execute('''INSERT INTO fund_dca_plans
            (code,name,period,amount,day_of,strategy,enabled,target_profit_pct,auto_record,note)
            VALUES (?,?,?,?,?,?,1,?,?,?)''',
            (str(code).zfill(6),name,period,amount,day_of,strategy,target_profit_pct,1 if auto_record else 0,note))
        pid=cur.lastrowid
        _save(cur,'plan',str(pid),{'id':pid,'anchor_date':anchor,'account':'default','channel':'default',
                                   'calendar_id':'CN',**settings,'known_at':now()})
        return pid


def get_plans(only_enabled: bool = False) -> List[Dict]:
    conn = _conn()
    cur = conn.cursor()
    sql = """SELECT id,code,name,period,amount,day_of,strategy,enabled,
                    target_profit_pct,auto_record,note
             FROM fund_dca_plans"""
    if only_enabled:
        sql += " WHERE enabled=1"
    sql += " ORDER BY id"
    cur.execute(sql)
    cols = ['id', 'code', 'name', 'period', 'amount', 'day_of', 'strategy',
            'enabled', 'target_profit_pct', 'auto_record', 'note']
    rows = cur.fetchall()
    conn.close()
    from fund.fund_ledger import list_saved
    settings = {str(p['id']):p for p in list_saved('plan')}
    statuses = {str(p['id']):p for p in list_saved('plan_status')}
    return [{**dict(zip(cols,r)),**settings.get(str(r[0]),{}),
             'auto_record_semantics':'pending_only','schedule_status':statuses.get(str(r[0]),{}).get('status','needs_calendar' if settings.get(str(r[0])) else 'needs_anchor')} for r in rows]


def import_transactions(rows, update_position=True, source='import', skip_existing=True,
                        preview_only=False, expected_version=None):
    from fund import fund_ledger as ledger
    init_db()
    items=[{**r,'source':r.get('source') or source} for r in rows]
    if not update_position: raise ValueError('模拟记录请使用模拟入口，不混入真实导入')
    try:
        return ledger.preview(items) if preview_only else ledger.commit(items,expected_version)
    except ValueError as exc:
        return {'imported':0,'skipped':0,'errors':[str(exc)]}


def import_plans(rows, dedup: bool = True) -> Dict:
    """批量导入定投计划。rows: list[dict],字段(中英任一):
      code/代码(必)、amount/金额(必)、period/周期(monthly|weekly|daily,默认monthly)、
      day_of/扣款日(默认1)、strategy/策略(normal|valuation|value_avg,默认normal)、
      name/名称(可选,缺则按 code 补)、target_profit_pct/止盈%(可选)、auto_record/自动记账(可选)、note/备注。
    dedup=True 时跳过已存在同 code 的计划(避免重复导入)。返回 {imported, skipped, errors:[...]}。"""
    init_db()
    identity=lambda p:(str(p['code']).zfill(6),p.get('period','monthly'),float(p['amount']),int(p.get('day_of') or 1),p.get('account','default'),p.get('channel','default'),p.get('anchor_date'),p.get('strategy','normal'))
    existing={identity(p) for p in get_plans()} if dedup else set()
    imported = skipped = 0
    errors = []

    def _g(r, *names):
        for n in names:
            if r.get(n) not in (None, ''):
                return r.get(n)
        return None

    for i, r in enumerate(rows or []):
        try:
            code = str(_g(r, 'code', '代码') or '').strip()
            if not code:
                errors.append(f'行{i}: 缺 code'); continue
            code = code.zfill(6)
            amount = _g(r, 'amount', '金额')
            if amount in (None, ''):
                errors.append(f'行{i}({code}): 缺 amount'); continue
            from fund.fund_contracts import now
            candidate={**r,'anchor_date':r.get('anchor_date') or now()[:10],'code':code,'amount':amount,'period':_g(r,'period','周期') or 'monthly','day_of':_g(r,'day_of','扣款日') or 1}
            if dedup and identity(candidate) in existing:
                skipped += 1; continue
            name = _g(r, 'name', '名称')
            if not name:
                try:
                    from fund_data import fund_name
                    name = fund_name(code)
                except Exception:
                    name = None
            add_plan(
                code=code, amount=float(amount),
                period=str(_g(r, 'period', '周期') or 'monthly'),
                day_of=int(_g(r, 'day_of', '扣款日') or 1),
                strategy=str(_g(r, 'strategy', '策略') or 'normal'),
                name=name,
                target_profit_pct=_f(_g(r, 'target_profit_pct', '止盈%', '止盈')),
                auto_record=bool(_g(r, 'auto_record', '自动记账')),
                note=_g(r, 'note', '备注'),
                **{k:r[k] for k in ('account','channel','anchor_date','calendar_id','open_dates') if r.get(k) is not None},
            )
            existing.add(identity(candidate))
            imported += 1
        except Exception as e:
            errors.append(f'行{i}: {e}')
    return {'imported': imported, 'skipped': skipped, 'errors': errors}


def set_plan_enabled(plan_id: int, enabled: bool):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE fund_dca_plans SET enabled=? WHERE id=?", (1 if enabled else 0, plan_id))
    conn.commit()
    conn.close()


def delete_plan(plan_id: int):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM fund_dca_plans WHERE id=?", (plan_id,))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# 净值缓存
# --------------------------------------------------------------------------
def upsert_nav(code: str, nav_date, unit_nav, acc_nav=None, daily_return=None):
    """单条净值落库(幂等)。供持仓页"现抓即存"复用,下次直接读库。
    nav_date 为空(如货币基金无公布净值日)→ 用今日,避免撞 PK NOT NULL。"""
    if unit_nav is None:
        return
    if not nav_date:
        raise ValueError('确认净值必须包含真实净值日期')
    from fund.fund_contracts import day, number
    nav_date = day(nav_date)
    from fund.fund_contracts import now
    if nav_date>now()[:10]:raise ValueError('未来净值不能作为已确认值')
    number(unit_nav, positive=True)
    code = str(code).zfill(6)
    conn = _conn()
    cur = conn.cursor()
    from core.db_compat import upsert_sql
    keys = ['code', 'nav_date']
    all_cols = ['code', 'nav_date', 'unit_nav', 'acc_nav', 'daily_return']
    vals = (code, str(nav_date)[:10], float(unit_nav),
            float(acc_nav) if acc_nav is not None else None,
            float(daily_return) if daily_return is not None else None)
    sql, ordered = upsert_sql('fund_nav', keys, all_cols, vals)
    cur.execute(sql, ordered)
    conn.commit()
    conn.close()


def get_latest_navs(codes=None) -> Dict[str, Dict]:
    """取每只基金的最新净值(读库,不联网)。返回 {code: {'unit_nav','nav_date','daily_return'}}。
    持仓页用它免去逐只现抓 → 秒开。"""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""SELECT f.code, f.unit_nav, f.nav_date, f.daily_return FROM fund_nav f
        JOIN (SELECT code, MAX(nav_date) md FROM fund_nav GROUP BY code) m
          ON f.code=m.code AND f.nav_date=m.md""")
    out = {}
    for row in cur.fetchall():
        code, nav, d, dr = row[0], row[1], row[2], row[3] if len(row) > 3 else None
        out[str(code)] = {
            'unit_nav': float(nav) if nav is not None else None,
            'nav_date': str(d)[:10] if d else None,
            'daily_return': float(dr) if dr is not None else None,
        }
    conn.close()
    if codes:
        cset = {str(c).zfill(6) for c in codes}
        out = {k: v for k, v in out.items() if k in cset}
    return out


def get_nav_pairs(codes=None, upto_date: str = None) -> Dict[str, Dict]:
    """取截止日期每只基金的最新/上一期净值，供收益按真实净值差精算。"""
    conn = _conn()
    cur = conn.cursor()
    where = 'WHERE nav_date<=?' if upto_date else ''
    params = (str(upto_date)[:10],) if upto_date else ()
    cur.execute(f"""SELECT f.code, f.unit_nav, f.nav_date, f.daily_return,
        (SELECT p.unit_nav FROM fund_nav p
          WHERE p.code=f.code AND p.nav_date<f.nav_date
          ORDER BY p.nav_date DESC LIMIT 1) AS prev_nav
      FROM fund_nav f
      JOIN (SELECT code, MAX(nav_date) md FROM fund_nav {where} GROUP BY code) m
        ON f.code=m.code AND f.nav_date=m.md""", params)
    out = {}
    for code, nav, nav_date, daily_return, prev_nav in cur.fetchall():
        out[str(code)] = {
            'unit_nav': _f(nav),
            'nav_date': str(nav_date)[:10] if nav_date else None,
            'daily_return': _f(daily_return),
            'prev_nav': _f(prev_nav),
        }
    conn.close()
    if codes:
        cset = {str(c).zfill(6) for c in codes}
        out = {k: v for k, v in out.items() if k in cset}
    from fund.fund_ledger import list_saved
    snapshots={r['code']:r for r in list_saved('nav_snapshot')}
    for code,info in out.items():
        snapshot=snapshots.get(code,{})
        record=next((r for r in snapshot.get('records',[]) if str(r['date'])[:10]==info['nav_date']),{})
        info.update({k:record[k] for k in ('cash_dividend','split_ratio','income_per_10000','yield_7d') if k in record})
        info.update({k:snapshot.get('metadata',{}).get(k) for k in ('source','upstream','actions_status','unit_nav_kind')})
        if not info.get('actions_status'):info['actions_status']='unknown'
    return out


def save_nav(code: str, nav_df):
    """把净值 DataFrame[date,unit_nav,acc_nav,daily_return] 落库(幂等 upsert)。"""
    if nav_df is None or len(nav_df) == 0:
        return 0
    code = str(code).zfill(6)
    conn = _conn()
    cur = conn.cursor()
    from core.db_compat import upsert_sql, USE_POSTGRES
    keys = ['code', 'nav_date']
    all_cols = ['code', 'nav_date', 'unit_nav', 'acc_nav', 'daily_return']
    n = 0
    for _, r in nav_df.iterrows():
        from fund.fund_contracts import day,number,now
        try:
            d=day(r['date']);number(r.get('unit_nav'),positive=True)
            if d>now()[:10]:continue
        except (TypeError,ValueError):continue
        vals = (code, str(d)[:10], _f(r.get('unit_nav')), _f(r.get('acc_nav')), _f(r.get('daily_return')))
        sql, ordered = upsert_sql('fund_nav', keys, all_cols, vals)
        # 逐行隔离:单行异常(并发写/脏数据触发约束)不污染整个 PG 事务、不让整批净值刷新失败
        try:
            if USE_POSTGRES:
                cur.execute('SAVEPOINT sp_nav')
                cur.execute(sql, ordered)
                cur.execute('RELEASE SAVEPOINT sp_nav')
            else:
                cur.execute(sql, ordered)
            n += 1
        except Exception:
            if USE_POSTGRES:
                try:
                    cur.execute('ROLLBACK TO SAVEPOINT sp_nav')
                except Exception:
                    pass
    conn.commit()
    conn.close()
    from fund.fund_ledger import save
    from application.results import clean_json
    from fund.fund_contracts import nav_records
    records=nav_records(nav_df)
    save('nav_snapshot',code,clean_json({'code':code,'records':records,'metadata':nav_df.attrs}))
    return n


def _f(v):
    try:
        import math
        return float(v) if v is not None and math.isfinite(float(v)) else None
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------
# 组合净值快照(借 portfolio-tracker 思路:每日落一行,供 UI 画组合净值曲线)
# --------------------------------------------------------------------------
def save_portfolio_snapshot(snap_date: str, nav_lookup=None) -> Optional[Dict]:
    """按当前持有 + 最新净值算组合市值,落一行快照(幂等 upsert)。
    nav_lookup(code)->unit_nav 可传入避免重复抓取;缺省用 fund_data.latest_nav。"""
    import json
    holdings = get_holdings()
    if not holdings:
        return None
    if nav_lookup is None:
        import fund_data
        def nav_lookup(c):
            x = fund_data.latest_nav(c)
            return x['unit_nav'] if x else None
    total_mv = total_cost = 0.0
    snap_rows = []
    for h in holdings:
        nav = nav_lookup(h['code'])
        mv = (h['shares'] or 0) * nav if nav else None
        cost = (h['shares'] or 0) * (h['cost_nav'] or 0)
        total_mv += mv or 0
        total_cost += cost
        snap_rows.append({'code':h['code'],'mv':round(mv,2) if mv is not None else None})
    valued=sum(r['mv'] is not None for r in snap_rows)
    pnl_pct = (total_mv-total_cost)/total_cost if total_cost and valued==len(snap_rows) else None
    conn = _conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM fund_portfolio_snapshots WHERE snap_date=?", (snap_date,))
    cur.execute("""INSERT INTO fund_portfolio_snapshots
        (snap_date, total_mv, total_cost, pnl_pct, n_funds, holdings_json)
        VALUES (?,?,?,?,?,?)""",
        (snap_date, round(total_mv, 2), round(total_cost, 2),
         round(pnl_pct, 4) if pnl_pct is not None else None,
         len(holdings), json.dumps(snap_rows, ensure_ascii=False)))
    conn.commit()
    conn.close()
    from fund.fund_ledger import save
    save('portfolio_snapshot_meta',snap_date,{'snap_date':snap_date,'status':'ready' if valued==len(snap_rows) else 'partial','valued_count':valued,'total_count':len(snap_rows)})
    return {'snap_date': snap_date, 'total_mv': round(total_mv, 2),
            'status':'ready' if valued==len(snap_rows) else 'partial',
            'pnl_pct': round(pnl_pct, 4) if pnl_pct is not None else None}


def get_portfolio_snapshots(limit: int = 365) -> List[Dict]:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""SELECT snap_date, total_mv, total_cost, pnl_pct, n_funds
                   FROM fund_portfolio_snapshots ORDER BY snap_date DESC LIMIT ?""", (limit,))
    cols = ['snap_date', 'total_mv', 'total_cost', 'pnl_pct', 'n_funds']
    rows = cur.fetchall()
    conn.close()
    return [dict(zip(cols, r)) for r in reversed(rows)]


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    init_db()
    print('建表完成。后端 PG?', is_postgres())
    add_transaction('000001', '定投', nav=1.5, amount=1000, fee=1.5, trade_date='2025-01-05', name='测试基金')
    add_transaction('000001', '定投', nav=1.2, amount=1000, fee=1.5, trade_date='2025-02-05')
    print('持有:', get_holdings())
    print('流水:', len(get_transactions('000001')), '条')
    pid = add_plan('000001', 1000, 'monthly', 5, name='测试基金')
    print('计划:', get_plans())
    delete_plan(pid)
    delete_holding('000001')

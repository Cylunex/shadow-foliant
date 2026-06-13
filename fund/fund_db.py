"""基金数据库 —— 走 db_compat 统一层(USE_POSTGRES=true 用 PG,否则 SQLite db/fund.db)。

表:
  funds             基金基本信息(代码/简称/类型/经理/规模,缓存自数据源)
  fund_nav          净值历史缓存(code+date 唯一)
  fund_holdings     我的持有(份额/成本净值)
  fund_dca_plans    定投计划(周期/金额/策略/止盈/开关)
  fund_transactions 申赎/定投流水 + 持仓快照(对齐股票 trade_records 设计)

申赎流水写入时自动更新 fund_holdings(买入加仓重算移动加权成本、赎回减仓)。
SQL 用 SQLite 风格 `?` 占位符(db_compat 自动转 PG 的 %s);DDL 按后端分支。
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


def _conn():
    return connect(_bootstrap.db_path(_DB_FILE), check_same_thread=False) if not is_postgres() else connect()


# --------------------------------------------------------------------------
# 建表(幂等)。PG / SQLite 类型分支。
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
    conn = _conn()
    cur = conn.cursor()
    for sql in _ddl():
        cur.execute(sql)
    conn.commit()
    conn.close()
    _ensure_columns()


def _ensure_columns():
    """幂等补列(老库升级):给 fund_dca_plans 补 auto_record。新库 DDL 已含,这里兜底。"""
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE fund_dca_plans ADD COLUMN auto_record INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        conn.rollback()  # 已存在则忽略
    conn.close()


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
    code = str(code).zfill(6)
    conn = _conn()
    cur = conn.cursor()
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
    conn = _conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM fund_holdings WHERE code=?", (str(code).zfill(6),))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# 申赎/定投流水(自动更新持有)
# --------------------------------------------------------------------------
def add_transaction(code: str, txn_type: str, nav: float, amount: float = None,
                    shares: float = None, fee: float = 0.0, trade_date: str = None,
                    name: str = None, source: str = 'manual', note: str = None,
                    update_position: bool = True) -> Dict:
    """记一笔申赎/定投流水。
    申购/定投:给 amount(申购金额),自动 shares=(amount-fee)/nav;
    赎回:给 shares(赎回份额)或 amount。
    update_position=True 时同步更新 fund_holdings(移动加权成本)。"""
    code = str(code).zfill(6)
    nav = float(nav)
    is_buy = txn_type in ('申购', '定投', '买入')
    if is_buy:
        if amount is None and shares is not None:
            amount = shares * nav + (fee or 0)
        shares = (float(amount) - (fee or 0)) / nav if nav else 0.0
        delta = shares
    else:  # 赎回/卖出
        if shares is None and amount is not None:
            shares = float(amount) / nav if nav else 0.0
        amount = (shares * nav) - (fee or 0)
        delta = -float(shares)

    conn = _conn()
    cur = conn.cursor()
    pos_shares, pos_cost = None, None
    if update_position:
        cur.execute("SELECT shares, cost_nav FROM fund_holdings WHERE code=?", (code,))
        row = cur.fetchone()
        prev_sh = (row[0] if row else 0) or 0
        prev_cost = (row[1] if row else 0) or 0
        if is_buy:
            new_cost = moving_cost(prev_sh, prev_cost, shares, nav)
            new_sh = prev_sh + shares
        else:
            new_sh = max(prev_sh - float(shares), 0.0)
            new_cost = prev_cost  # 赎回不改成本
        pos_shares, pos_cost = new_sh, new_cost
        if row:
            cur.execute("UPDATE fund_holdings SET shares=?, cost_nav=?, name=COALESCE(?,name) WHERE code=?",
                        (new_sh, new_cost, name, code))
        else:
            cur.execute("INSERT INTO fund_holdings (code, name, shares, cost_nav) VALUES (?,?,?,?)",
                        (code, name, new_sh, new_cost))

    cur.execute("""INSERT INTO fund_transactions
        (code, name, txn_type, nav, shares, amount, fee, trade_date,
         pos_shares, pos_cost_nav, delta_shares, source, note)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (code, name, txn_type, nav, round(float(shares), 4), round(float(amount), 2),
         fee or 0, trade_date, pos_shares, pos_cost, round(delta, 4), source, note))
    conn.commit()
    conn.close()
    return {'code': code, 'txn_type': txn_type, 'shares': round(float(shares), 4),
            'amount': round(float(amount), 2), 'pos_shares': pos_shares, 'pos_cost_nav': pos_cost}


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
    return [dict(zip(cols, r)) for r in rows]


# --------------------------------------------------------------------------
# 定投计划
# --------------------------------------------------------------------------
def add_plan(code: str, amount: float, period: str = 'monthly', day_of: int = 1,
             strategy: str = 'normal', name: str = None,
             target_profit_pct: float = None, auto_record: bool = False,
             note: str = None) -> int:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""INSERT INTO fund_dca_plans
        (code, name, period, amount, day_of, strategy, enabled, target_profit_pct, auto_record, note)
        VALUES (?,?,?,?,?,?,1,?,?,?)""",
        (str(code).zfill(6), name, period, amount, day_of, strategy,
         target_profit_pct, 1 if auto_record else 0, note))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
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
    return [dict(zip(cols, r)) for r in rows]


def import_transactions(rows, update_position: bool = True, source: str = 'import',
                        skip_existing: bool = False) -> Dict:
    """批量导入基金申赎/定投流水(按日期升序应用 → 移动加权成本正确)。rows 每项键(中英任一):
      code/代码(必)、txn_type/类型(申购|定投|赎回|买入|卖出,默认申购)、nav/净值(必)、
      amount/金额 或 shares/份额(买入一般给amount,赎回给shares)、fee/费用、trade_date/日期、
      name/名称(缺则按code补)、note/备注。
    skip_existing=True 按(code,日期,source)跳过已存在(防重复导入)。
    ⚠️ 默认增量叠加到当前持仓:勿对同一批重复导入(要么空仓导全量,要么只导新增)。
    返回 {imported, skipped, errors:[...]}。"""
    init_db()

    def _g(r, *names):
        for n in names:
            if r.get(n) not in (None, ''):
                return r.get(n)
        return None

    # 先规整 + 按日期升序(同日按原始顺序),保证移动加权成本按时间正确累计
    items = []
    for i, r in enumerate(rows or []):
        items.append((str(_g(r, 'trade_date', '日期', 'date') or ''), i, r))
    items.sort(key=lambda x: (x[0], x[1]))

    imported = skipped = 0
    errors = []
    for trade_date, i, r in items:
        try:
            code = str(_g(r, 'code', '代码') or '').strip()
            if not code:
                errors.append(f'行{i}: 缺 code'); continue
            code = code.zfill(6)
            nav = _g(r, 'nav', '净值')
            if nav in (None, ''):
                errors.append(f'行{i}({code}): 缺 nav'); continue
            txn_type = str(_g(r, 'txn_type', '类型', 'type') or '申购')
            td = trade_date or None
            if skip_existing and td and has_transaction_on(code, td, source):
                skipped += 1; continue
            name = _g(r, 'name', '名称')
            if not name:
                try:
                    from fund_data import fund_name
                    name = fund_name(code)
                except Exception:
                    name = None
            add_transaction(
                code=code, txn_type=txn_type, nav=float(nav),
                amount=_f(_g(r, 'amount', '金额')), shares=_f(_g(r, 'shares', '份额')),
                fee=_f(_g(r, 'fee', '费用')) or 0.0, trade_date=td, name=name,
                source=source, note=_g(r, 'note', '备注'), update_position=update_position,
            )
            imported += 1
        except Exception as e:
            errors.append(f'行{i}: {e}')
    return {'imported': imported, 'skipped': skipped, 'errors': errors}


def import_plans(rows, dedup: bool = True) -> Dict:
    """批量导入定投计划。rows: list[dict],字段(中英任一):
      code/代码(必)、amount/金额(必)、period/周期(monthly|weekly|daily,默认monthly)、
      day_of/扣款日(默认1)、strategy/策略(normal|valuation|value_avg,默认normal)、
      name/名称(可选,缺则按 code 补)、target_profit_pct/止盈%(可选)、auto_record/自动记账(可选)、note/备注。
    dedup=True 时跳过已存在同 code 的计划(避免重复导入)。返回 {imported, skipped, errors:[...]}。"""
    init_db()
    existing = {p['code'] for p in get_plans()} if dedup else set()
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
            if dedup and code in existing:
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
            )
            existing.add(code)
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
        import datetime
        nav_date = datetime.date.today().isoformat()
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
        d = r['date']
        d = d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d)
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
    return n


def _f(v):
    try:
        return None if v is None else float(v)
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
        mv = (h['shares'] or 0) * nav if nav else 0.0
        cost = (h['shares'] or 0) * (h['cost_nav'] or 0)
        total_mv += mv
        total_cost += cost
        snap_rows.append({'code': h['code'], 'mv': round(mv, 2)})
    pnl_pct = (total_mv - total_cost) / total_cost if total_cost else None
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
    return {'snap_date': snap_date, 'total_mv': round(total_mv, 2),
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

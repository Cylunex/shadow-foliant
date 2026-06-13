"""股票持仓组合净值快照(借鉴 portfolio-tracker 思路)—— 每日落一行组合市值,供画净值曲线。

独立于现有 portfolio_db(PG)与 portfolio_db_pg,走 db_compat 统一 PG/SQLite,
不改动现有持仓逻辑。市值 = Σ(数量 × 实时价),实时价用 a_stock_data_adapter 批量报价,取不到退成本价。
"""

from __future__ import annotations

import os
import sys
import json
from typing import List, Dict, Optional

if not any(os.path.basename(p) == 'shadow-foliant' for p in sys.path):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402

from db_compat import connect, is_postgres  # noqa: E402

_DB_FILE = 'stock_portfolio_snapshots.db'


def _conn():
    return connect(_bootstrap.db_path(_DB_FILE), check_same_thread=False) if not is_postgres() else connect()


def init_db():
    num = 'DOUBLE PRECISION' if is_postgres() else 'REAL'
    ts = 'TIMESTAMPTZ DEFAULT NOW()' if is_postgres() else 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'
    conn = _conn()
    cur = conn.cursor()
    cur.execute(f"""CREATE TABLE IF NOT EXISTS stock_portfolio_snapshots (
        snap_date DATE PRIMARY KEY, total_mv {num}, total_cost {num},
        pnl_pct {num}, n_stocks INTEGER, holdings_json TEXT, daily_mv_change {num} DEFAULT 0, created_at {ts})""")
    # 兼容旧表：加 daily_mv_change 列（已有则忽略）
    try:
        if is_postgres():
            cur.execute("ALTER TABLE stock_portfolio_snapshots ADD COLUMN IF NOT EXISTS daily_mv_change DOUBLE PRECISION DEFAULT 0")
        else:
            cur.execute("ALTER TABLE stock_portfolio_snapshots ADD COLUMN daily_mv_change REAL DEFAULT 0")
    except Exception:
        pass
    conn.commit()
    conn.close()


def save_snapshot(snap_date: str) -> Optional[Dict]:
    """按当前股票持仓 + 实时价算组合市值,落一行(幂等 upsert)。无持仓返回 None。"""
    try:
        from portfolio_db import portfolio_db
        stocks = portfolio_db.get_all_stocks()
    except Exception as e:
        print(f'[portfolio_snapshot] 读取持仓失败: {type(e).__name__}')
        return None
    if not stocks:
        return None
    codes = [str(s.get('code')) for s in stocks if s.get('code')]
    quotes = {}
    try:
        import datahub
        quotes = datahub.quotes(codes)
    except Exception:
        quotes = {}

    total_mv = total_cost = 0.0
    rows = []
    for s in stocks:
        code = str(s.get('code'))
        qty = float(s.get('quantity') or s.get('shares') or 0)
        cost_price = float(s.get('cost_price') or s.get('cost') or 0)
        price = (quotes.get(code) or {}).get('price') or cost_price
        mv = qty * float(price)
        total_mv += mv
        total_cost += qty * cost_price
        rows.append({'code': code, 'mv': round(mv, 2)})
    pnl_pct = (total_mv - total_cost) / total_cost if total_cost else None

    init_db()
    conn = _conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM stock_portfolio_snapshots WHERE snap_date=?", (snap_date,))

    # 算当日涨跌：对比上一次快照
    daily_mv_change = 0.0
    cur.execute("""SELECT total_mv FROM stock_portfolio_snapshots
                   ORDER BY snap_date DESC LIMIT 1""")
    prev = cur.fetchone()
    if prev is not None and prev[0] is not None:
        daily_mv_change = round(total_mv - float(prev[0]), 2)

    cur.execute("""INSERT INTO stock_portfolio_snapshots
        (snap_date, total_mv, total_cost, pnl_pct, n_stocks, holdings_json, daily_mv_change)
        VALUES (?,?,?,?,?,?,?)""",
        (snap_date, round(total_mv, 2), round(total_cost, 2),
         round(pnl_pct, 4) if pnl_pct is not None else None,
         len(stocks), json.dumps(rows, ensure_ascii=False),
         daily_mv_change))
    conn.commit()
    conn.close()
    return {'snap_date': snap_date, 'total_mv': round(total_mv, 2),
            'pnl_pct': round(pnl_pct, 4) if pnl_pct is not None else None,
            'daily_mv_change': daily_mv_change}


def get_snapshots(limit: int = 365) -> List[Dict]:
    init_db()
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""SELECT snap_date, total_mv, total_cost, pnl_pct, n_stocks, daily_mv_change
                   FROM stock_portfolio_snapshots ORDER BY snap_date DESC LIMIT ?""", (limit,))
    cols = ['snap_date', 'total_mv', 'total_cost', 'pnl_pct', 'n_stocks', 'daily_mv_change']
    rows = cur.fetchall()
    conn.close()
    return [dict(zip(cols, r)) for r in reversed(rows)]


if __name__ == '__main__':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    init_db()
    print('建表 OK, 后端 PG?', is_postgres())
    print('快照:', save_snapshot('2026-06-06'))
    print('历史:', get_snapshots())

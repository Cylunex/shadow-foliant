"""股票持仓组合净值快照(借鉴 portfolio-tracker 思路)—— 每日落一行组合市值,供画净值曲线。

独立于现有 portfolio_db(PG)与 portfolio_db_pg,走 db_compat 统一 PG/SQLite,
不改动现有持仓逻辑。市值 = Σ(数量 × 实时价),实时价用 a_stock_data_adapter 批量报价,取不到退成本价。
"""

from __future__ import annotations

import os
import sys
import json
import math
from datetime import datetime, timezone
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
        pnl_pct {num}, n_stocks INTEGER, holdings_json TEXT, daily_mv_change {num} DEFAULT 0,
        quote_count INTEGER DEFAULT 0, fallback_count INTEGER DEFAULT 0,
        anomaly_count INTEGER DEFAULT 0, created_at {ts})""")
    # 兼容旧表：逐列迁移（SQLite 不支持 ADD COLUMN IF NOT EXISTS）。
    migrations = [
        ('daily_mv_change', f'daily_mv_change {num} DEFAULT 0'),
        ('quote_count', 'quote_count INTEGER DEFAULT 0'),
        ('fallback_count', 'fallback_count INTEGER DEFAULT 0'),
        ('anomaly_count', 'anomaly_count INTEGER DEFAULT 0'),
        # 成交 id 水位比成交日期可靠：补录成交可以晚于当日快照，但不能在下一交易日
        # 被当成收益。旧行保留 NULL，首轮由 created_at 反推当时已存在的最大成交 id。
        ('trade_watermark', 'trade_watermark BIGINT'),
    ]
    for name, ddl in migrations:
        try:
            if is_postgres():
                cur.execute(f"ALTER TABLE stock_portfolio_snapshots ADD COLUMN IF NOT EXISTS {ddl}")
            else:
                cur.execute(f"ALTER TABLE stock_portfolio_snapshots ADD COLUMN {ddl}")
        except Exception:
            pass
    conn.commit()
    conn.close()


def _net_trade_flow(after_date: str, upto_date: str) -> float:
    """(after_date, upto_date] 区间真实成交净流入(买入+ / 卖出-),来自 trade_records 成交行。
    走 portfolio_db.get_trades(两后端同构);无记录/异常返回 0(不影响快照主流程)。"""
    try:
        from portfolio_db import portfolio_db
        flow = 0.0
        for t in (portfolio_db.get_trades(limit=1000) or []):
            d = str(t.get('trade_time') or '')[:10]
            if not d or not (after_date < d <= upto_date):
                continue
            amt = float(t.get('amount') or 0)
            flow += amt if t.get('trade_type') == '买入' else -amt
        return round(flow, 2)
    except Exception:
        return 0.0


def _as_utc(value) -> Optional[datetime]:
    """兼容 PG timestamptz、旧库 TEXT 与 SQLite CURRENT_TIMESTAMP。"""
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip().replace('Z', '+00:00')
        try:
            dt = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None
    # 生产旧 trade_records.created_at 是 UTC 文本；SQLite CURRENT_TIMESTAMP 也是 UTC。
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_trade_rows(limit: int = 10000) -> List[Dict]:
    try:
        from portfolio_db import portfolio_db
        return list(portfolio_db.get_trades(limit=limit) or [])
    except Exception:
        return []


def _infer_trade_watermark(trades: List[Dict], snapshot_created_at) -> Optional[int]:
    """反推生成旧快照时数据库中已经存在的最大成交 id。"""
    cutoff = _as_utc(snapshot_created_at)
    if cutoff is None:
        return None
    ids = []
    saw_timestamp = False
    for trade in trades:
        created = _as_utc(trade.get('created_at'))
        if created is None:
            continue
        saw_timestamp = True
        try:
            trade_id = int(trade.get('id'))
        except (TypeError, ValueError):
            continue
        if created <= cutoff:
            ids.append(trade_id)
    return max(ids, default=0) if saw_timestamp else None


def _watermarked_trade_flow(trades: List[Dict], after_id: int, upto_id: int) -> float:
    """按录入 id 水位统计两张快照之间首次进入持仓的真实资金流。"""
    flow = 0.0
    for trade in trades:
        try:
            trade_id = int(trade.get('id'))
        except (TypeError, ValueError):
            continue
        if not (int(after_id or 0) < trade_id <= int(upto_id or 0)):
            continue
        trade_type = trade.get('trade_type')
        if trade_type not in ('买入', '卖出'):
            continue
        try:
            amount = float(trade.get('amount') or 0)
        except (TypeError, ValueError):
            amount = 0.0
        flow += amount if trade_type == '买入' else -amount
    return round(flow, 2)


def _resolve_price(quote_price, previous_price: float, cost_price: float):
    """返回 (采用价格, 来源, 是否异常)。

    相邻快照间单价跨越 3 倍基本不可能由正常涨跌造成；遇到这类值沿用上次有效价。
    这个保护专门防止代码映射错误、字段错位等“有值但不是这只证券”的静默污染。
    """
    try:
        current = float(quote_price)
    except (TypeError, ValueError):
        current = 0.0
    current_ok = math.isfinite(current) and current > 0
    previous_ok = math.isfinite(previous_price) and previous_price > 0
    if current_ok and previous_ok:
        ratio = current / previous_price
        if ratio > 3.0 or ratio < (1.0 / 3.0):
            return previous_price, 'previous_anomaly', True
    if current_ok:
        return current, 'quote', False
    if previous_ok:
        return previous_price, 'previous_missing', False
    return cost_price, 'cost_missing', False


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
    init_db()

    # 先取上一次逐证券市值。旧快照没有 price/qty 时，在持仓数量未变的常见场景下仍可由 mv/qty 反推。
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""SELECT snap_date, holdings_json, total_mv, trade_watermark, created_at
                   FROM stock_portfolio_snapshots
                   WHERE snap_date < ? ORDER BY snap_date DESC LIMIT 1""", (snap_date,))
    prev_row = cur.fetchone()
    conn.close()
    previous = {}
    if prev_row and prev_row[1]:
        try:
            previous = {str(x.get('code')): x for x in json.loads(prev_row[1]) if x.get('code')}
        except (TypeError, ValueError, json.JSONDecodeError):
            previous = {}

    codes = [str(s.get('code')) for s in stocks if s.get('code')]
    quotes = {}
    try:
        import datahub
        quotes = datahub.quotes(codes)
    except Exception:
        quotes = {}

    total_mv = total_cost = 0.0
    quote_count = fallback_count = anomaly_count = 0
    rows = []
    for s in stocks:
        code = str(s.get('code'))
        qty = float(s.get('quantity') or s.get('shares') or 0)
        cost_price = float(s.get('cost_price') or s.get('cost') or 0)
        prev_item = previous.get(code) or {}
        prev_price = float(prev_item.get('price') or 0)
        if prev_price <= 0 and qty > 0:
            prev_price = float(prev_item.get('mv') or 0) / qty
        quote = quotes.get(code) or {}
        price, price_source, anomalous = _resolve_price(quote.get('price'), prev_price, cost_price)
        if price_source == 'quote':
            quote_count += 1
        else:
            fallback_count += 1
        if anomalous:
            anomaly_count += 1
            print(f"[portfolio_snapshot] 异常报价已隔离: {code} quote={quote.get('price')} "
                  f"prev={prev_price:.4f} name={quote.get('name') or '-'}")
        mv = qty * float(price)
        total_mv += mv
        total_cost += qty * cost_price
        rows.append({'code': code, 'qty': qty, 'price': round(float(price), 6),
                     'mv': round(mv, 2), 'price_source': price_source})
    pnl_pct = (total_mv - total_cost) / total_cost if total_cost else None

    trades = _load_trade_rows()
    current_trade_watermark = max(
        (int(t.get('id')) for t in trades if str(t.get('id') or '').isdigit()),
        default=0,
    )

    conn = _conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM stock_portfolio_snapshots WHERE snap_date=?", (snap_date,))

    # 算当日涨跌:对比上一次快照,并**剔除区间内买卖资金流**(2026-07-17 修)——
    # 原来 daily_mv_change = 今日市值-上次市值:当天买入 10 万会被当"+10 万收益"推送、
    # 清仓一只显示巨额假亏损(与 performance.twr 的剔 flow 口径不一致)。
    # 净流 = (prev_date, snap_date] 内 trade_records 成交行的 买入金额-卖出金额;
    # 上次快照缺日(任务失败)时为多日差值,flow 同窗口累计,口径仍一致。
    daily_mv_change = 0.0
    if prev_row is not None and prev_row[2] is not None:
        previous_watermark = prev_row[3]
        if previous_watermark is None:
            previous_watermark = _infer_trade_watermark(trades, prev_row[4])
        if previous_watermark is None:
            # 极老 SQLite 没有 created_at 时保留旧口径，不因迁移破坏可用性。
            flow = _net_trade_flow(str(prev_row[0])[:10], str(snap_date)[:10])
        else:
            flow = _watermarked_trade_flow(
                trades, int(previous_watermark), current_trade_watermark)
        daily_mv_change = round(total_mv - float(prev_row[2]) - flow, 2)

    cur.execute("""INSERT INTO stock_portfolio_snapshots
        (snap_date, total_mv, total_cost, pnl_pct, n_stocks, holdings_json, daily_mv_change,
         quote_count, fallback_count, anomaly_count, trade_watermark)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (snap_date, round(total_mv, 2), round(total_cost, 2),
         round(pnl_pct, 4) if pnl_pct is not None else None,
         len(stocks), json.dumps(rows, ensure_ascii=False),
         daily_mv_change, quote_count, fallback_count, anomaly_count,
         current_trade_watermark))
    conn.commit()
    conn.close()
    return {'snap_date': snap_date, 'total_mv': round(total_mv, 2),
            'pnl_pct': round(pnl_pct, 4) if pnl_pct is not None else None,
            'daily_mv_change': daily_mv_change, 'quote_count': quote_count,
            'fallback_count': fallback_count, 'anomaly_count': anomaly_count,
            'trade_watermark': current_trade_watermark}


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

# -*- coding: utf-8 -*-
"""已实现盈亏 — 成交记录闭环(2026-06-12,5-31 会话遗留)

口径:每笔【卖出】的已实现盈亏 = (卖出价 - 卖出时持仓成本 pos_cost_price) × 数量 - 佣金 - 印花税。
pos_cost_price 是 import_trades 写入的成交时点成本快照(移动加权),为 NULL(卖出未持有股)的算不了,跳过。

  backfill()   — PG: 把算出的盈亏回填 trade_records.profit_loss(建列时就有,从来没填过);幂等
  summary()    — 汇总(总盈亏/胜率/单笔均值/按股票),PG/SQLite 通用(内存计算,不依赖回填)
  format_text()— 推送用文本
"""
import json
import os
from typing import Any, Dict, List, Optional

import _bootstrap  # noqa: F401

USE_PG = os.getenv('USE_POSTGRES', '').lower() in ('1', 'true', 'yes', 'on')


def _fees(extra: Any) -> float:
    """从 extra(JSON 字符串或 dict)取 佣金+印花税"""
    if not extra:
        return 0.0
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            return 0.0
    if not isinstance(extra, dict):
        return 0.0
    total = 0.0
    for k in ('commission', 'tax'):
        try:
            total += float(extra.get(k) or 0)
        except (TypeError, ValueError):
            pass
    return total


def _pl_of(row: Dict) -> Optional[float]:
    """单笔卖出的已实现盈亏;非卖出/缺成本快照返回 None"""
    if row.get('trade_type') != '卖出':
        return None
    cost = row.get('pos_cost_price')
    price = row.get('price')
    qty = row.get('quantity')
    if cost in (None, '') or price in (None, '') or not qty:
        return None
    try:
        return round((float(price) - float(cost)) * float(qty) - _fees(row.get('extra')), 2)
    except (TypeError, ValueError):
        return None


def backfill() -> Dict[str, int]:
    """PG: 为 profit_loss IS NULL 的卖出行回填已实现盈亏。幂等,可重复跑。"""
    if not USE_PG:
        return {'updated': 0, 'skipped': 0, 'note': 'SQLite 模式无 profit_loss 列,summary() 直接内存计算'}
    from core.database_pg import get_conn
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, trade_type, price, quantity, pos_cost_price, extra
        FROM trade_records
        WHERE trade_type = '卖出' AND profit_loss IS NULL
    """)
    rows = cur.fetchall()
    updated = skipped = 0
    for rid, ttype, price, qty, cost, extra in rows:
        pl = _pl_of({'trade_type': ttype, 'price': price, 'quantity': qty,
                     'pos_cost_price': cost, 'extra': extra})
        if pl is None:
            skipped += 1
            continue
        cur.execute("UPDATE trade_records SET profit_loss = %s WHERE id = %s", (pl, rid))
        updated += 1
    conn.commit()
    cur.close()
    conn.close()
    return {'updated': updated, 'skipped': skipped}


def summary(days: Optional[int] = None, limit: int = 5000) -> Dict[str, Any]:
    """已实现盈亏汇总(按卖出笔):total/笔数/胜率/盈亏比/按股票明细。
    通过 portfolio_db.get_trades 取数,PG/SQLite 通用。days=N 只看最近 N 天。"""
    from portfolio_db import portfolio_db
    rows = portfolio_db.get_trades(limit=limit) or []
    if days:
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        rows = [r for r in rows if str(r.get('trade_time') or '')[:10] >= cutoff]

    sells = []
    for r in rows:
        pl = _pl_of(r)
        if pl is not None:
            sells.append({'code': r.get('code'), 'name': r.get('name') or r.get('code'),
                          'pl': pl, 'time': str(r.get('trade_time') or '')[:10]})
    if not sells:
        return {'count': 0, 'total': 0.0, 'win_rate': None, 'by_stock': [], 'days': days}

    wins = [s['pl'] for s in sells if s['pl'] > 0]
    losses = [s['pl'] for s in sells if s['pl'] < 0]
    by_stock: Dict[str, Dict] = {}
    for s in sells:
        b = by_stock.setdefault(s['code'], {'code': s['code'], 'name': s['name'],
                                            'pl': 0.0, 'n': 0})
        b['pl'] = round(b['pl'] + s['pl'], 2)
        b['n'] += 1
    ranked = sorted(by_stock.values(), key=lambda x: x['pl'], reverse=True)

    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    return {
        'count': len(sells),
        'total': round(sum(s['pl'] for s in sells), 2),
        'wins': len(wins), 'losses': len(losses),
        'win_rate': round(len(wins) / len(sells) * 100, 1),
        'avg_win': round(avg_win, 2), 'avg_loss': round(avg_loss, 2),
        'profit_factor': round(avg_win / avg_loss, 2) if avg_loss else None,
        'by_stock': ranked, 'days': days,
    }


def format_text(s: Dict[str, Any], top_n: int = 5) -> str:
    """汇总 → 推送文本"""
    if not s or not s.get('count'):
        rng = f"近{s.get('days')}天" if s and s.get('days') else '累计'
        return f'({rng}无已实现卖出记录)'
    rng = f"近{s['days']}天" if s.get('days') else '累计'
    icon = '📈' if s['total'] >= 0 else '📉'
    lines = [f"{icon} 已实现盈亏({rng}): {s['total']:+,.0f}元 | "
             f"{s['count']}笔 胜率{s['win_rate']}%"
             + (f" 盈亏比{s['profit_factor']}" if s.get('profit_factor') else '')]
    for b in s['by_stock'][:top_n]:
        lines.append(f"  · {b['name']} {b['code']}: {b['pl']:+,.0f}元({b['n']}笔)")
    if len(s['by_stock']) > top_n:
        worst = s['by_stock'][-1]
        if worst['pl'] < 0 and worst not in s['by_stock'][:top_n]:
            lines.append(f"  · 最大亏损: {worst['name']} {worst['code']} {worst['pl']:+,.0f}元")
    return '\n'.join(lines)


if __name__ == '__main__':
    import io, sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_bootstrap.ROOT, '.env'))
    except Exception:
        pass
    print('=== 已实现盈亏自检 ===')
    print('backfill:', backfill())
    s = summary()
    print(f"累计: {s.get('total')}元 / {s.get('count')}笔 / 胜率{s.get('win_rate')}%")
    print(format_text(s))

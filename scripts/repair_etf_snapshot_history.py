#!/usr/bin/env python3
"""修复 563800/563330 市场错配污染的历史组合快照。

默认仅预览；传 ``--apply`` 才更新。应用前会把两张表原始行完整备份到 db/repairs JSON。
修复后按真实 ETF 日收盘价重算组合市值、逐日股票收益及股票+基金合计收益。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402
from db_compat import connect, coerce_json, is_postgres  # noqa: E402
from portfolio.portfolio_snapshot import init_db, _net_trade_flow  # noqa: E402
from portfolio.daily_pnl import init_db as init_pnl_db  # noqa: E402


TARGET_CODES = ('563800', '563330')


def _conn():
    return connect() if is_postgres() else connect(_bootstrap.db_path('stock_portfolio_snapshots.db'))


def _rows(cur, sql, params=()):
    cur.execute(sql, params)
    return list(cur.fetchall())


def _current_quantities():
    from portfolio_db import portfolio_db
    wanted = set(TARGET_CODES)
    return {str(s.get('code')): float(s.get('quantity') or s.get('shares') or 0)
            for s in (portfolio_db.get_all_stocks() or []) if str(s.get('code')) in wanted}


def _historical_closes():
    import datahub
    out = {}
    for code in TARGET_CODES:
        df = datahub.kline(code, '3mo', adjust='raw')
        if df is None or getattr(df, 'empty', True):
            raise RuntimeError(f'{code} 历史日线为空')
        col = 'Close' if 'Close' in df.columns else 'close'
        out[code] = {str(idx)[:10]: float(v) for idx, v in df[col].dropna().items()}
    return out


def _backup(stock_rows, pnl_rows):
    folder = os.path.join(_bootstrap.DB_DIR, 'repairs')
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f'etf_snapshot_before_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json')
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump({'stock_portfolio_snapshots': stock_rows, 'daily_pnl_snapshots': pnl_rows},
                  fh, ensure_ascii=False, indent=2, default=str)
    return path


def run(apply=False):
    init_db()
    init_pnl_db()
    quantities = _current_quantities()
    missing_qty = [c for c in TARGET_CODES if quantities.get(c, 0) <= 0]
    if missing_qty:
        raise RuntimeError(f'当前持仓数量缺失: {missing_qty}')
    closes = _historical_closes()

    conn = _conn()
    cur = conn.cursor()
    stock_rows = _rows(cur, """SELECT snap_date,total_mv,total_cost,pnl_pct,n_stocks,
                                        holdings_json,daily_mv_change
                                 FROM stock_portfolio_snapshots ORDER BY snap_date""")
    pnl_rows = _rows(cur, """SELECT snap_date,stock_count,stock_mv,stock_daily_pnl,stock_daily_pct,
                                      fund_count,fund_mv,fund_daily_pnl,fund_daily_pct,
                                      total_daily_pnl,total_daily_pct
                               FROM daily_pnl_snapshots ORDER BY snap_date""")

    planned = []
    totals = {}
    for row in stock_rows:
        day = str(row[0])[:10]
        total_mv = float(row[1] or 0)
        holdings = coerce_json(row[5]) or []
        changed = []
        for item in holdings:
            code = str(item.get('code') or '')
            if code not in TARGET_CODES or day not in closes[code]:
                continue
            old_mv = float(item.get('mv') or 0)
            qty = quantities[code]
            price = closes[code][day]
            new_mv = round(qty * price, 2)
            total_mv += new_mv - old_mv
            item.update({'qty': qty, 'price': price, 'mv': new_mv,
                         'price_source': 'history_repair'})
            if abs(new_mv - old_mv) >= 0.01:
                changed.append({'code': code, 'old_mv': old_mv, 'new_mv': new_mv, 'price': price})
        totals[day] = round(total_mv, 2)
        if changed:
            cost = float(row[2] or 0)
            planned.append({'date': day, 'old_total': float(row[1] or 0),
                            'new_total': round(total_mv, 2), 'changes': changed,
                            'holdings_json': json.dumps(holdings, ensure_ascii=False),
                            'pnl_pct': round((total_mv - cost) / cost, 4) if cost else None})

    # 所有日期按修复后的总市值重算逐日变化；目标持仓在已有快照区间内没有交易，但仍走通用资金流扣除。
    daily = {}
    prev_day = None
    prev_mv = None
    for row in stock_rows:
        day = str(row[0])[:10]
        mv = totals.get(day, float(row[1] or 0))
        value = 0.0 if prev_mv is None else round(mv - prev_mv - _net_trade_flow(prev_day, day), 2)
        daily[day] = value
        prev_day, prev_mv = day, mv

    print(f'计划修复 {len(planned)}/{len(stock_rows)} 个股票快照')
    for item in planned:
        detail = ', '.join(f"{x['code']}:{x['old_mv']:.2f}->{x['new_mv']:.2f}" for x in item['changes'])
        print(f"  {item['date']} total {item['old_total']:.2f}->{item['new_total']:.2f} | {detail}")
    if not apply:
        conn.close()
        print('DRY-RUN：未修改数据库；确认后加 --apply')
        return {'planned': len(planned), 'applied': False}

    backup_path = _backup(stock_rows, pnl_rows)
    try:
        by_day = {x['date']: x for x in planned}
        for row in stock_rows:
            day = str(row[0])[:10]
            item = by_day.get(day)
            if item:
                cur.execute("""UPDATE stock_portfolio_snapshots
                               SET total_mv=?,pnl_pct=?,holdings_json=?,daily_mv_change=?
                               WHERE snap_date=?""",
                            (item['new_total'], item['pnl_pct'], item['holdings_json'], daily[day], day))
            else:
                cur.execute("UPDATE stock_portfolio_snapshots SET daily_mv_change=? WHERE snap_date=?",
                            (daily[day], day))

        for row in pnl_rows:
            day = str(row[0])[:10]
            if day not in totals:
                continue
            stock_mv = totals[day]
            stock_pnl = daily[day]
            stock_prev = stock_mv - stock_pnl
            fund_mv = float(row[6] or 0)
            fund_pnl = float(row[7] or 0)
            total_pnl = stock_pnl + fund_pnl
            total_prev = stock_prev + (fund_mv - fund_pnl)
            cur.execute("""UPDATE daily_pnl_snapshots
                           SET stock_mv=?,stock_daily_pnl=?,stock_daily_pct=?,
                               total_daily_pnl=?,total_daily_pct=? WHERE snap_date=?""",
                        (round(stock_mv, 2), round(stock_pnl, 2),
                         round(stock_pnl / stock_prev * 100, 4) if stock_prev > 0 else 0,
                         round(total_pnl, 2),
                         round(total_pnl / total_prev * 100, 4) if total_prev > 0 else 0, day))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f'APPLIED：已修复，原始行备份 {backup_path}')
    return {'planned': len(planned), 'applied': True, 'backup': backup_path}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='备份后应用修复；默认只预览')
    args = parser.parse_args()
    run(apply=args.apply)

#!/usr/bin/env python3
"""按成交录入水位修复股票现金流，并精算指定日基金收益。

默认 dry-run；传 ``--apply`` 后先备份原表，再更新快照与合并收益。
晚于当日股票快照才录入的成交不会追补到当日收益，只会在下一张快照中作为资金流剔除。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402
from db_compat import connect, is_postgres  # noqa: E402
from fund import fund_db  # noqa: E402
from portfolio.daily_pnl import (  # noqa: E402
    calculate_fund_pnl,
    init_db as init_pnl_db,
)
from portfolio.portfolio_snapshot import (  # noqa: E402
    _infer_trade_watermark,
    _load_trade_rows,
    _watermarked_trade_flow,
    init_db as init_stock_db,
)


def _conn():
    return connect() if is_postgres() else connect(_bootstrap.db_path('stock_portfolio_snapshots.db'))


def _rows(cur, sql, params=()):
    cur.execute(sql, params)
    return list(cur.fetchall())


def _backup(stock_rows, pnl_rows):
    folder = os.path.join(_bootstrap.DB_DIR, 'repairs')
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(
        folder,
        f'pnl_cashflow_before_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json',
    )
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(
            {'stock_portfolio_snapshots': stock_rows, 'daily_pnl_snapshots': pnl_rows},
            fh,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    return path


def _fund_result(target_date: str):
    holdings = fund_db.get_holdings()
    codes = [h['code'] for h in holdings if float(h.get('shares') or 0) > 0]
    conn = fund_db._conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM fund_transactions WHERE trade_date>?", (target_date,))
    later_transactions = int((cur.fetchone() or [0])[0] or 0)
    conn.close()
    if later_transactions:
        raise RuntimeError(
            f'{target_date} 后存在 {later_transactions} 笔基金交易，不能用当前份额安全回算'
        )
    return calculate_fund_pnl(
        holdings,
        fund_db.get_nav_pairs(codes, upto_date=target_date),
        target_date,
    )


def run(target_date: str, apply: bool = False):
    init_stock_db()
    init_pnl_db()
    fund_db.init_db()
    trades = _load_trade_rows(limit=100000)

    conn = _conn()
    cur = conn.cursor()
    stock_rows = _rows(
        cur,
        """SELECT snap_date,total_mv,total_cost,pnl_pct,n_stocks,holdings_json,
                  daily_mv_change,quote_count,fallback_count,anomaly_count,
                  trade_watermark,created_at
             FROM stock_portfolio_snapshots ORDER BY snap_date""",
    )
    pnl_rows = _rows(
        cur,
        """SELECT snap_date,stock_count,stock_mv,stock_daily_pnl,stock_daily_pct,
                  fund_count,fund_total_count,fund_fresh_count,fund_mv,
                  fund_daily_pnl,fund_daily_pct,total_daily_pnl,total_daily_pct
             FROM daily_pnl_snapshots ORDER BY snap_date""",
    )

    revised = {}
    prev_mv = None
    prev_watermark = None
    for row in stock_rows:
        day = str(row[0])[:10]
        mv = float(row[1] or 0)
        watermark = _infer_trade_watermark(trades, row[11])
        if watermark is None:
            watermark = int(row[10] or 0)
        new_pnl = float(row[6] or 0)
        if prev_mv is not None:
            flow = _watermarked_trade_flow(trades, prev_watermark or 0, watermark)
            new_pnl = round(mv - prev_mv - flow, 2)
        revised[day] = {'watermark': watermark, 'stock_pnl': new_pnl, 'stock_mv': mv}
        prev_mv, prev_watermark = mv, watermark

    fund_result = _fund_result(target_date)
    changes = []
    for row in stock_rows:
        day = str(row[0])[:10]
        old_pnl = float(row[6] or 0)
        new_pnl = revised[day]['stock_pnl']
        if abs(new_pnl - old_pnl) >= 0.01:
            changes.append((day, old_pnl, new_pnl))

    print(f'股票现金流差异 {len(changes)} 日：')
    for day, old, new in changes:
        print(f'  {day}: {old:+.2f} -> {new:+.2f} ({new - old:+.2f})')
    print(
        f'基金 {target_date}: pnl={fund_result["fund_daily_pnl"]:+.2f} '
        f'pct={fund_result["fund_daily_pct"]:+.4f}% '
        f'fresh={fund_result["fund_fresh_count"]}/{fund_result["fund_total_count"]} '
        f'mv={fund_result["fund_mv"]:.2f}'
    )
    if not apply:
        conn.close()
        print('DRY-RUN：未修改收益数据；确认后加 --apply')
        return {'applied': False, 'stock_changes': changes, 'fund': fund_result}

    backup_path = _backup(stock_rows, pnl_rows)
    pnl_by_day = {str(row[0])[:10]: row for row in pnl_rows}
    try:
        for day, result in revised.items():
            cur.execute(
                """UPDATE stock_portfolio_snapshots
                      SET daily_mv_change=?,trade_watermark=? WHERE snap_date=?""",
                (result['stock_pnl'], result['watermark'], day),
            )
            pnl = pnl_by_day.get(day)
            if not pnl:
                continue
            stock_mv = result['stock_mv']
            stock_pnl = result['stock_pnl']
            fund_mv = float(pnl[8] or 0)
            fund_pnl = float(pnl[9] or 0)
            if day == target_date:
                fund_mv = fund_result['fund_mv']
                fund_pnl = fund_result['fund_daily_pnl']
            stock_prev = stock_mv - stock_pnl
            total_pnl = stock_pnl + fund_pnl
            total_prev = stock_prev + (fund_mv - fund_pnl)
            extra_sql = ''
            params = [
                stock_mv,
                stock_pnl,
                round(stock_pnl / stock_prev * 100, 4) if stock_prev > 0 else 0,
                total_pnl,
                round(total_pnl / total_prev * 100, 4) if total_prev > 0 else 0,
            ]
            if day == target_date:
                extra_sql = ",fund_count=?,fund_total_count=?,fund_fresh_count=?,fund_mv=?,fund_daily_pnl=?,fund_daily_pct=?"
                params.extend([
                    fund_result['fund_count'],
                    fund_result['fund_total_count'],
                    fund_result['fund_fresh_count'],
                    fund_result['fund_mv'],
                    fund_result['fund_daily_pnl'],
                    fund_result['fund_daily_pct'],
                ])
            params.append(day)
            cur.execute(
                """UPDATE daily_pnl_snapshots SET
                     stock_mv=?,stock_daily_pnl=?,stock_daily_pct=?,
                     total_daily_pnl=?,total_daily_pct=?""" + extra_sql + " WHERE snap_date=?",
                tuple(params),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f'APPLIED：已修复，原始行备份 {backup_path}')
    return {'applied': True, 'stock_changes': changes, 'fund': fund_result,
            'backup': backup_path}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--fund-date', required=True, help='要精算基金收益的日期 YYYY-MM-DD')
    parser.add_argument('--apply', action='store_true', help='备份后应用；默认只预览')
    args = parser.parse_args()
    run(args.fund_date, apply=args.apply)

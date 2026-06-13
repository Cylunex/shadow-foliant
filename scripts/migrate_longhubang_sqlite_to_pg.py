import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""一次性迁移：longhubang.db (SQLite) → PG aiagents_stock

背景：项目 longhubang 表历史数据存于本地 SQLite (longhubang.db)，
USE_POSTGRES=true 切换 PG 后历史数据没迁过来，导致 PG 中 longhubang_records / stock_tracking 为 0 行。

本脚本做的事：
  1. 读 SQLite (longhubang.db) 3 张表全量
  2. 对应写到 PG 同名表（带 UNIQUE 约束，重复插入会被忽略 / ON CONFLICT 处理）
  3. 输出每张表的 source / inserted / skipped 统计

幂等：可重复执行，不会插入重复数据。

用法（在 shadow-foliant 项目目录下执行）：
    python scripts/migrate_longhubang_sqlite_to_pg.py
    python scripts/migrate_longhubang_sqlite_to_pg.py --sqlite /path/to/longhubang.db --dry-run
"""

import argparse
import os
import sqlite3
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _pg_conn():
    """直接连 PG，不走 db_compat 以避免 lastval 黑魔法干扰批量插入"""
    import psycopg2
    return psycopg2.connect(
        host=os.getenv('PG_HOST', '127.0.0.1'),
        port=int(os.getenv('PG_PORT', '5432') or 5432),
        dbname=os.getenv('PG_DATABASE', 'aiagents_stock'),
        user=os.getenv('PG_USER', 'postgres'),
        password=os.getenv('PG_PASSWORD', ''),
    )


def migrate_longhubang_records(sqlite_path: str, dry_run: bool = False) -> Dict[str, int]:
    """迁移 longhubang_records 表

    UNIQUE 约束：(date, stock_code, youzi_name, yingye_bu)
    """
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    rows = src.execute('''
        SELECT date, stock_code, stock_name, youzi_name, yingye_bu, list_type,
               buy_amount, sell_amount, net_inflow, concepts, created_at
        FROM longhubang_records
    ''').fetchall()
    src.close()

    total = len(rows)
    if dry_run:
        return {'source': total, 'inserted': 0, 'skipped': 0, 'dry_run': True}

    pg = _pg_conn()
    cur = pg.cursor()
    inserted = 0
    for r in rows:
        try:
            cur.execute('''
                INSERT INTO longhubang_records
                  (date, stock_code, stock_name, youzi_name, yingye_bu, list_type,
                   buy_amount, sell_amount, net_inflow, concepts, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, NOW()))
                ON CONFLICT (date, stock_code, youzi_name, yingye_bu) DO NOTHING
            ''', (
                r['date'], r['stock_code'], r['stock_name'],
                r['youzi_name'], r['yingye_bu'], r['list_type'],
                r['buy_amount'], r['sell_amount'], r['net_inflow'],
                r['concepts'], r['created_at'],
            ))
            if cur.rowcount > 0:
                inserted += 1
        except Exception as e:
            print(f"  [longhubang_records] 跳过: {e}")
            pg.rollback()
            pg = _pg_conn()
            cur = pg.cursor()
            continue
    pg.commit()
    cur.close()
    pg.close()
    return {'source': total, 'inserted': inserted, 'skipped': total - inserted}


def migrate_longhubang_analysis(sqlite_path: str, dry_run: bool = False) -> Dict:
    """迁移 longhubang_analysis 表

    PG schema 无 UNIQUE，所以用 (analysis_date + COALESCE(data_date_range)) 防重。
    """
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    rows = src.execute('''
        SELECT analysis_date, data_date_range, analysis_content,
               recommended_stocks, summary, created_at
        FROM longhubang_analysis
    ''').fetchall()
    src.close()

    total = len(rows)
    if dry_run:
        return {'source': total, 'inserted': 0, 'skipped': 0, 'dry_run': True}

    pg = _pg_conn()
    cur = pg.cursor()
    inserted = 0
    skipped = 0
    for r in rows:
        try:
            cur.execute('''
                SELECT 1 FROM longhubang_analysis
                WHERE analysis_date = %s::timestamptz
                  AND COALESCE(data_date_range, '') = COALESCE(%s, '')
                LIMIT 1
            ''', (r['analysis_date'], r['data_date_range']))
            if cur.fetchone():
                skipped += 1
                continue
            cur.execute('''
                INSERT INTO longhubang_analysis
                  (analysis_date, data_date_range, analysis_content,
                   recommended_stocks, summary)
                VALUES (COALESCE(%s::timestamptz, NOW()), %s, %s, %s::jsonb, %s)
            ''', (
                r['analysis_date'], r['data_date_range'], r['analysis_content'],
                r['recommended_stocks'], r['summary'],
            ))
            inserted += 1
        except Exception as e:
            print(f"  [longhubang_analysis] 跳过: {e}")
            pg.rollback()
            pg = _pg_conn()
            cur = pg.cursor()
            continue
    pg.commit()
    cur.close()
    pg.close()
    return {'source': total, 'inserted': inserted, 'skipped': skipped}


def migrate_stock_tracking(sqlite_path: str, dry_run: bool = False) -> Dict:
    """迁移 stock_tracking 表

    SQLite analysis_id 是 SQLite-local 自增 id，迁到 PG 后无法保证 FK 一致。
    简化：迁入时 analysis_id 设为 NULL（保留业务字段）。
    用 (stock_code + recommended_date) 防重复。
    """
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    rows = src.execute('''
        SELECT stock_code, stock_name, recommended_date, recommended_price,
               target_price, stop_loss_price, current_price, profit_loss_pct,
               status, notes, updated_at
        FROM stock_tracking
    ''').fetchall()
    src.close()

    total = len(rows)
    if dry_run:
        return {'source': total, 'inserted': 0, 'skipped': 0, 'dry_run': True}

    pg = _pg_conn()
    cur = pg.cursor()
    inserted = 0
    skipped = 0
    for r in rows:
        try:
            cur.execute('''
                SELECT 1 FROM stock_tracking
                WHERE stock_code = %s AND recommended_date = %s
                LIMIT 1
            ''', (r['stock_code'], r['recommended_date']))
            if cur.fetchone():
                skipped += 1
                continue
            cur.execute('''
                INSERT INTO stock_tracking
                  (stock_code, stock_name, recommended_date, recommended_price,
                   target_price, stop_loss_price, current_price, profit_loss_pct,
                   status, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                r['stock_code'], r['stock_name'], r['recommended_date'],
                r['recommended_price'], r['target_price'], r['stop_loss_price'],
                r['current_price'], r['profit_loss_pct'],
                r['status'], r['notes'],
            ))
            inserted += 1
        except Exception as e:
            print(f"  [stock_tracking] 跳过: {e}")
            pg.rollback()
            pg = _pg_conn()
            cur = pg.cursor()
            continue
    pg.commit()
    cur.close()
    pg.close()
    return {'source': total, 'inserted': inserted, 'skipped': skipped}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sqlite', default=_bootstrap.db_path('longhubang.db'), help='SQLite 文件路径（默认 longhubang.db）')
    ap.add_argument('--dry-run', action='store_true', help='只统计，不写入 PG')
    args = ap.parse_args()

    if not os.path.exists(args.sqlite):
        print(f'❌ SQLite 文件不存在: {args.sqlite}')
        sys.exit(1)

    print(f'=== longhubang SQLite → PG 迁移 ===')
    print(f'  源: {args.sqlite}')
    print(f'  目标: PG {os.getenv("PG_HOST")}:{os.getenv("PG_PORT")}/{os.getenv("PG_DATABASE")}')
    print(f'  模式: {"DRY-RUN" if args.dry_run else "实际写入"}')
    print()

    print('1. longhubang_records ...')
    r1 = migrate_longhubang_records(args.sqlite, args.dry_run)
    print(f'   source={r1["source"]}, inserted={r1["inserted"]}, skipped={r1["skipped"]}')

    print('2. longhubang_analysis ...')
    r2 = migrate_longhubang_analysis(args.sqlite, args.dry_run)
    print(f'   source={r2["source"]}, inserted={r2["inserted"]}, skipped={r2["skipped"]}')

    print('3. stock_tracking ...')
    r3 = migrate_stock_tracking(args.sqlite, args.dry_run)
    print(f'   source={r3["source"]}, inserted={r3["inserted"]}, skipped={r3["skipped"]}')

    total_src = r1['source'] + r2['source'] + r3['source']
    total_ins = r1['inserted'] + r2['inserted'] + r3['inserted']
    print(f'\n=== 完成: 源 {total_src} 条 → 入库 {total_ins} 条 ===')


if __name__ == '__main__':
    main()

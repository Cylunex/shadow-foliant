"""PG → 本地 SQLite 全量备份 —— 把生产 PostgreSQL 所有表落到本地 SQLite 文件归档。

用途:本地离线副本 / 灾备 / 迁移前快照(如装 pgvector 前)。
- 每张 PG 表 → SQLite 同名表(列名一致;SQLite 动态类型,值按 TEXT/REAL/INT 落)。
- JSONB/dict/list → json 字符串;datetime → ISO 字符串;Decimal → float;enum → 文本。
- 整库重建(DROP+CREATE)保证副本与源一致。失败不影响生产(只读 PG)。

用法:
    python jobs/pg_backup.py                 # 备份到 db/pg_backup.db
    python jobs/pg_backup.py path/to/x.db    # 指定输出
由 jobs_hub 的 task_pg_backup 每日调用(开关 pg_backup,默认关)。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, date
from decimal import Decimal

if not any(os.path.basename(p) == 'shadow-foliant' for p in sys.path):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402


def _pg_conn():
    from dotenv import load_dotenv
    load_dotenv()
    import psycopg2
    return psycopg2.connect(
        host=os.getenv('PG_HOST'), port=int(os.getenv('PG_PORT', '5432')),
        dbname=os.getenv('PG_DATABASE'), user=os.getenv('PG_USER'),
        password=os.getenv('PG_PASSWORD'), connect_timeout=15)


def _cell(v):
    """PG 值 → SQLite 可存值。"""
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v)
    if isinstance(v, bool):
        return int(v)
    return v


def backup_pg_to_sqlite(out_path: str = None) -> dict:
    """全量备份。返回 {file, tables, rows, ok}。"""
    out_path = out_path or _bootstrap.db_path('pg_backup.db')
    pg = _pg_conn()
    pcur = pg.cursor()
    pcur.execute("SELECT table_name FROM information_schema.tables "
                 "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name")
    tables = [r[0] for r in pcur.fetchall()]

    tmp = out_path + '.tmp'
    if os.path.exists(tmp):
        os.remove(tmp)
    lite = sqlite3.connect(tmp)
    lcur = lite.cursor()
    total_rows = 0
    done = {}
    for t in tables:
        pcur.execute('SELECT column_name FROM information_schema.columns '
                     'WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position',
                     ('public', t))
        cols = [r[0] for r in pcur.fetchall()]
        if not cols:
            continue
        col_defs = ', '.join(f'"{c}"' for c in cols)
        lcur.execute(f'DROP TABLE IF EXISTS "{t}"')
        lcur.execute(f'CREATE TABLE "{t}" ({col_defs})')   # 动态类型,列名一致
        pcur.execute(f'SELECT {col_defs} FROM "{t}"')
        ph = ','.join('?' * len(cols))
        n = 0
        while True:
            batch = pcur.fetchmany(2000)
            if not batch:
                break
            lcur.executemany(f'INSERT INTO "{t}" VALUES ({ph})',
                             [[_cell(v) for v in row] for row in batch])
            n += len(batch)
        done[t] = n
        total_rows += n
    # 元信息表
    lcur.execute('CREATE TABLE _backup_meta (key TEXT, value TEXT)')
    lcur.executemany('INSERT INTO _backup_meta VALUES (?,?)', [
        ('backed_up_at', datetime.now().isoformat()),
        ('source', f"{os.getenv('PG_HOST')}:{os.getenv('PG_PORT')}/{os.getenv('PG_DATABASE')}"),
        ('tables', str(len(done))), ('rows', str(total_rows)),
    ])
    lite.commit()
    lite.close()
    pg.close()
    # 原子替换
    if os.path.exists(out_path):
        os.replace(out_path, out_path + '.prev')
    os.replace(tmp, out_path)
    return {'file': out_path, 'tables': len(done), 'rows': total_rows, 'ok': True, 'detail': done}


if __name__ == '__main__':
    out = sys.argv[1] if len(sys.argv) > 1 else None
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    r = backup_pg_to_sqlite(out)
    print(f"✅ 备份完成 → {r['file']}  ({r['tables']} 表 / {r['rows']} 行)")
    big = sorted(r['detail'].items(), key=lambda x: -x[1])[:8]
    print('  最大表:', ', '.join(f'{t}={n}' for t, n in big))

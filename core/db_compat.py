"""
统一的 SQLite/PostgreSQL 兼容层

设计目标：让原有 SQLite 代码几乎零修改即可走 PG，关键是：
  1. ? 占位符自动转 %s
  2. INSERT 后用 PG 的 lastval() 模拟 SQLite 的 lastrowid
  3. AUTOCOMMIT 行为对齐
  4. 透明的 cursor / connection 包装

用法：
    from db_compat import connect, USE_POSTGRES
    conn = connect('stock_monitor.db')  # USE_POSTGRES=true 时返回 PG conn
    cur = conn.cursor()
    cur.execute('INSERT INTO x(name) VALUES (?)', (name,))
    new_id = cur.lastrowid  # PG 模式自动用 lastval()
    conn.commit()
    conn.close()
"""

from __future__ import annotations

import os
import sqlite3

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

USE_POSTGRES = os.getenv('USE_POSTGRES', '').lower() in ('1', 'true', 'yes', 'on')

_psycopg2 = None
if USE_POSTGRES:
    try:
        import psycopg2 as _psycopg2
        import psycopg2.extras  # noqa: F401
    except ImportError:
        print('[db_compat] ⚠️ USE_POSTGRES=true 但未装 psycopg2，回退 SQLite')
        USE_POSTGRES = False

PG_CONFIG = {
    'host': os.getenv('PG_HOST', '127.0.0.1'),
    'port': int(os.getenv('PG_PORT', '5432')) if os.getenv('PG_PORT') else 5432,
    'dbname': os.getenv('PG_DATABASE', ''),
    'user': os.getenv('PG_USER', ''),
    'password': os.getenv('PG_PASSWORD', ''),
}


def _convert_placeholders(sql: str) -> str:
    """? → %s（PG）。同时处理 SQLite→PG 常见差异：datetime() 函数。"""
    if not USE_POSTGRES:
        return sql
    sql = sql.replace('?', '%s')
    # SQLite datetime(column) → PG: column (timestamptz 可直接比较)
    sql = sql.replace('datetime(triggered_at)', 'triggered_at')
    # SQLite datetime('now', '-X minutes') → PG: NOW() - INTERVAL
    import re as _re
    sql = _re.sub(
        r"datetime\('now',\s*'-'\s*\|\|\s*(.+?)\s*\|\|\s*'\s*minutes'\)",
        r"NOW() - (COALESCE(CAST(\1 AS INT), 60) * INTERVAL '1 minute')",
        sql
    )
    sql = _re.sub(
        r"datetime\('now',\s*'-'\s*\|\|\s*(.+?)\s*\|\|\s*'\s*hours'\)",
        r"NOW() - (COALESCE(CAST(\1 AS INT), 1) * INTERVAL '1 hour')",
        sql
    )
    sql = _re.sub(
        r"datetime\('now',\s*'-'\s*\|\|\s*(.+?)\s*\|\|\s*'\s*days'\)",
        r"NOW() - (COALESCE(CAST(\1 AS INT), 1) * INTERVAL '1 day')",
        sql
    )
    return sql


class _PGCursor:
    """包装 PG cursor，提供 SQLite 兼容接口"""

    def __init__(self, real_cur):
        self._cur = real_cur
        self._lastrowid = None

    def execute(self, sql, params=()):
        sql = _convert_placeholders(sql)
        self._cur.execute(sql, params)
        # 模拟 SQLite lastrowid：INSERT 后取 lastval()。
        # ⚠️ 坑：对无序列的表(如 TEXT 主键的 fund_holdings)lastval() 会报错,
        #   而 PG 里任一语句报错会**污染整个事务**,导致后续语句全部
        #   "current transaction is aborted"。这曾让 PG 模式下 fund_db.add_transaction
        #   (先 INSERT 无序列的 holdings,再 INSERT 流水)整体失败。
        #   用 SAVEPOINT 隔离 lastval() 的失败:失败则回滚到存点,不波及外层事务。
        if sql.strip().upper().startswith('INSERT'):
            try:
                self._cur.execute('SAVEPOINT _lastval_sp')
                self._cur.execute('SELECT lastval()')
                self._lastrowid = self._cur.fetchone()[0]
                self._cur.execute('RELEASE SAVEPOINT _lastval_sp')
            except Exception:
                self._lastrowid = None
                try:
                    self._cur.execute('ROLLBACK TO SAVEPOINT _lastval_sp')
                except Exception:
                    pass
        return self

    def executemany(self, sql, params_seq):
        self._cur.executemany(_convert_placeholders(sql), params_seq)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def fetchmany(self, size=None):
        return self._cur.fetchmany(size) if size is not None else self._cur.fetchmany()

    @property
    def lastrowid(self):
        return self._lastrowid

    @property
    def description(self):
        return self._cur.description

    @property
    def rowcount(self):
        return self._cur.rowcount

    def close(self):
        return self._cur.close()

    def __iter__(self):
        return iter(self._cur)


class _PGConnection:
    def __init__(self):
        self._conn = _psycopg2.connect(**PG_CONFIG)

    def cursor(self):
        return _PGCursor(self._conn.cursor())

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def execute(self, sql, params=()):
        """有些代码直接在 conn 上调用 execute"""
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    @property
    def row_factory(self):
        return None  # PG 不用 SQLite 的 row_factory

    @row_factory.setter
    def row_factory(self, value):
        # 静默忽略 SQLite 的 row_factory 设置
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def connect(sqlite_path: str = None, **kwargs):
    """主入口

    Args:
        sqlite_path: SQLite 数据库文件路径（PG 模式下忽略）
        **kwargs: 额外参数（如 check_same_thread）仅对 SQLite 有效

    Returns:
        Connection 对象（PG 包装 / SQLite 原生），接口对齐
    """
    if USE_POSTGRES:
        return _PGConnection()
    conn = sqlite3.connect(sqlite_path, **kwargs)
    # SQLite 默认外键不开
    try:
        conn.execute('PRAGMA foreign_keys = ON')
    except Exception:
        pass
    return conn


def is_postgres() -> bool:
    return USE_POSTGRES


def coerce_json(value):
    """智能处理 JSON 字段读取

    PG JSONB 列读出来是 dict/list（psycopg2 自动反序列化）
    SQLite TEXT 列读出来是 str，需要 json.loads
    用 isinstance 判断避免 json.loads(dict) 报错
    """
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        import json
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def pg_config_snapshot() -> dict:
    """供日志/调试用 — 不带密码"""
    return {k: v for k, v in PG_CONFIG.items() if k != 'password'}


def upsert_sql(table: str, keys, all_cols, values):
    """幂等 upsert: PG 用 ON CONFLICT DO UPDATE，SQLite 用 INSERT OR REPLACE。

    table: 表名
    keys: 主键/唯一键列名列表 (如 ['code','nav_date'])
    all_cols: 所有列名列表 (keys + 其他列，顺序对应 values)
    values: 对应 all_cols 的值元组

    返回 (sql, ordered_values)
    """
    if USE_POSTGRES:
        vals_placeholder = [_convert_placeholders('?') for _ in all_cols]
        update_cols = [c for c in all_cols if c not in keys]
        update_set = ', '.join(f'{c}=EXCLUDED.{c}' for c in update_cols) if update_cols else 'DO NOTHING'
        sql = f'INSERT INTO {table} ({", ".join(all_cols)}) VALUES ({", ".join(vals_placeholder)}) ON CONFLICT ({", ".join(keys)}) DO UPDATE SET {update_set}'
        return sql, tuple(values)
    else:
        sql = f'INSERT OR REPLACE INTO {table} ({", ".join(all_cols)}) VALUES ({", ".join(["?" for _ in all_cols])})'
        return sql, tuple(values)


if __name__ == '__main__':
    print(f'USE_POSTGRES: {USE_POSTGRES}')
    if USE_POSTGRES:
        print(f'PG config: {pg_config_snapshot()}')
        try:
            conn = connect('dummy.db')
            cur = conn.cursor()
            cur.execute("SELECT current_database(), version()")
            print('PG 连接成功:', cur.fetchone())
            conn.close()
        except Exception as e:
            print(f'PG 连接失败: {e}')
    else:
        conn = connect(':memory:')
        cur = conn.cursor()
        cur.execute('CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)')
        cur.execute('INSERT INTO t(v) VALUES (?)', ('hello',))
        print('SQLite lastrowid:', cur.lastrowid)
        conn.close()

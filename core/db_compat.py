"""PostgreSQL runtime adapter for legacy qmark-style SQL call sites.

SQLite is no longer a supported runtime. The adapter keeps only the narrow API
surface needed while legacy modules are converted to native PostgreSQL SQL:
  1. ? 占位符自动转 %s
  2. INSERT 后用 PG 的 lastval() 模拟 SQLite 的 lastrowid
  3. AUTOCOMMIT 行为对齐
  4. 透明的 cursor / connection 包装

用法：
    from db_compat import connect
    conn = connect()  # 返回 PostgreSQL 连接
    cur = conn.cursor()
    cur.execute('INSERT INTO x(name) VALUES (?)', (name,))
    new_id = cur.lastrowid  # PG 模式自动用 lastval()
    conn.commit()
    conn.close()
"""

from __future__ import annotations

from database_settings import DatabaseSettings

USE_POSTGRES = True

try:
    import psycopg2 as _psycopg2
    import psycopg2.extras  # noqa: F401
except ImportError:
    _psycopg2 = None

_SETTINGS = DatabaseSettings.from_env()
PG_CONFIG = {
    'host': _SETTINGS.host, 'port': _SETTINGS.port,
    'dbname': _SETTINGS.dbname, 'user': _SETTINGS.user,
    'password': _SETTINGS.password,
}


def _convert_placeholders(sql: str) -> str:
    """Convert qmark parameters without touching quoted text/comments/JSON ``?``."""
    output = []
    index = 0
    state = "code"
    while index < len(sql):
        char = sql[index]
        pair = sql[index:index + 2]
        if state == "code":
            if pair == "--":
                state = "line_comment"; output.append(pair); index += 2; continue
            if pair == "/*":
                state = "block_comment"; output.append(pair); index += 2; continue
            if char == "'":
                state = "single"; output.append(char); index += 1; continue
            if char == '"':
                state = "double"; output.append(char); index += 1; continue
            if char == '?':
                before = sql[:index].rstrip()[-1:] or ""
                after = sql[index + 1:].lstrip()[:1]
                # PostgreSQL JSON existence operator: payload ? 'field'.
                is_json_operator = bool(before and (before.isalnum() or before in ")]_\"")
                                        and after in {"'", '"'})
                output.append('?' if is_json_operator else '%s')
                index += 1; continue
        elif state == "single" and char == "'":
            if index + 1 < len(sql) and sql[index + 1] == "'":
                output.append("''"); index += 2; continue
            state = "code"
        elif state == "double" and char == '"':
            if index + 1 < len(sql) and sql[index + 1] == '"':
                output.append('""'); index += 2; continue
            state = "code"
        elif state == "line_comment" and char in "\r\n":
            state = "code"
        elif state == "block_comment" and pair == "*/":
            output.append(pair); index += 2; state = "code"; continue
        output.append(char)
        index += 1
    sql = ''.join(output)
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
        self._inserted = False

    def execute(self, sql, params=()):
        sql = _convert_placeholders(sql)
        self._cur.execute(sql, params)
        self._inserted = sql.lstrip().upper().startswith('INSERT')
        self._lastrowid = None
        return self

    def _load_lastrowid(self):
        """Query lastval lazily only for the few legacy callers that request it."""
        if self._inserted and self._lastrowid is None:
            try:
                self._cur.execute('SAVEPOINT _lastval_sp')
                self._cur.execute('SELECT lastval()')
                self._lastrowid = self._cur.fetchone()[0]
                self._cur.execute('RELEASE SAVEPOINT _lastval_sp')
            except Exception:
                self._lastrowid = None
                try:
                    self._cur.execute('ROLLBACK TO SAVEPOINT _lastval_sp')
                    self._cur.execute('RELEASE SAVEPOINT _lastval_sp')
                except Exception:
                    pass

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
        self._load_lastrowid()
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
        if _psycopg2 is None:
            raise RuntimeError("PostgreSQL runtime requires psycopg2")
        required = ("dbname", "user")
        missing = [key for key in required if not str(PG_CONFIG.get(key) or "").strip()]
        if missing:
            raise RuntimeError("PostgreSQL configuration is incomplete")
        self._conn = _psycopg2.connect(**PG_CONFIG)

    def cursor(self):
        return _PGCursor(self._conn.cursor(cursor_factory=_psycopg2.extras.DictCursor))

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
        exc_type = a[0] if a else None
        try:
            self.rollback() if exc_type else self.commit()
        finally:
            self.close()


def connect(sqlite_path: str = None, **kwargs):
    """Return a PostgreSQL connection; path/SQLite kwargs are ignored for compatibility."""
    return _PGConnection()


def is_postgres() -> bool:
    return True


def coerce_json(value):
    """智能处理 JSON 字段读取

    PostgreSQL JSONB is normally decoded already; TEXT payloads remain supported
    while schemas are migrated.
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
    """Build an idempotent PostgreSQL ON CONFLICT upsert.

    table: 表名
    keys: 主键/唯一键列名列表 (如 ['code','nav_date'])
    all_cols: 所有列名列表 (keys + 其他列，顺序对应 values)
    values: 对应 all_cols 的值元组

    返回 (sql, ordered_values)
    """
    vals_placeholder = [_convert_placeholders('?') for _ in all_cols]
    update_cols = [c for c in all_cols if c not in keys]
    action = (f'DO UPDATE SET {", ".join(f"{c}=EXCLUDED.{c}" for c in update_cols)}'
              if update_cols else 'DO NOTHING')
    sql = (f'INSERT INTO {table} ({", ".join(all_cols)}) '
           f'VALUES ({", ".join(vals_placeholder)}) '
           f'ON CONFLICT ({", ".join(keys)}) {action}')
    return sql, tuple(values)


if __name__ == '__main__':
    print(f'PG config: {pg_config_snapshot()}')
    try:
        conn = connect()
        cur = conn.cursor()
        cur.execute("SELECT current_database(), version()")
        print('PG 连接成功:', cur.fetchone())
        conn.close()
    except Exception as e:
        print(f'PG 连接失败: {type(e).__name__}')

"""
项目路径引导 —— 代码按功能模块分目录后,保持原有「扁平 import」与「动态 import」不变。

原因:本项目 100+ 模块原为扁平脚本式(`from stock_data import ...`),且存在动态导入
(`__import__(module_name)`)。重构为功能子目录后,只需在进程入口 `import _bootstrap`
一次,将各子目录加入 sys.path,所有原 import 语句与动态导入即可零改动继续工作。

用法:
  - 根目录入口(app.py / run.py / autostart.py):首行 `import _bootstrap`
  - 子目录/scripts 下可独立运行的文件:
        import os, sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import _bootstrap  # noqa
  - 需要项目根路径的模块:`import _bootstrap; _bootstrap.ROOT`
"""

import os
import sys

# 项目根 = 本文件所在目录
ROOT = os.path.dirname(os.path.abspath(__file__))

# Windows 控制台默认 GBK,项目里大量 emoji/中文 print 会触发 UnicodeEncodeError 而崩溃。
# 在最早的引导阶段把 stdout/stderr 切到 utf-8(不可编码字符用 ? 替换,绝不抛异常)。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# 功能模块子目录(代码按功能分组后的目录)
_SUBDIRS = [
    'core', 'data', 'agents', 'analysis', 'selection',
    'longhubang', 'news_flow', 'sector', 'macro',
    'portfolio', 'monitor', 'notify', 'jobs',
    'fund', 'rag',
]


def _ensure_paths():
    # 根目录优先(config.py / instock_strategies 包等仍在根)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    for d in _SUBDIRS:
        p = os.path.join(ROOT, d)
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)


_ensure_paths()


# ---------------------------------------------------------------------------
# 统一加载项目根 .env(2026-06-12 集中到此)
# 此前各模块各自 `load_dotenv()`(不带路径)→ 只从当前 cwd 找 .env;脚本/任务 cwd 不在
# 项目根时读不到 → USE_POSTGRES=None 静默回落 SQLite、PG_PASSWORD 拿到默认 changeme 连不上。
# 在此按"本文件所在的项目根"显式加载一次,override=False(系统/supervisor 注入的真实环境变量优先),
# 幂等。凡入口首行 `import _bootstrap` 的进程(webui/jobs/mcp/scripts)都不再依赖 cwd。
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv as _load_dotenv
    _ENV_FILE = os.path.join(ROOT, '.env')
    if os.path.isfile(_ENV_FILE):
        _load_dotenv(_ENV_FILE, override=False)
except Exception:
    pass


# ---------------------------------------------------------------------------
# psycopg2 NUMERIC → float 全局适配:DB 标准化后价格/金额列改 NUMERIC,
# 若不适配,psycopg2 会返回 Decimal,撞坏大量 `float * Decimal` 运算与 st.number_input
# (交接 §A 记过此坑)。这里全局把 NUMERIC/DECIMAL 读出为 float,代码零改动。
# ---------------------------------------------------------------------------
try:
    import psycopg2.extensions as _pgext  # noqa
    _DEC2FLOAT = _pgext.new_type(
        _pgext.DECIMAL.values, 'DEC2FLOAT',
        lambda v, cur: float(v) if v is not None else None)
    _pgext.register_type(_DEC2FLOAT)
except Exception:
    pass  # 未装 psycopg2(纯 SQLite 环境)忽略


# ---------------------------------------------------------------------------
# 数据库目录:所有 SQLite .db 统一放 ROOT/db/(PG 模式不用,但保留为缓存/归档)
# ---------------------------------------------------------------------------
DB_DIR = os.path.join(ROOT, 'db')
os.makedirs(DB_DIR, exist_ok=True)


def db_path(name: str) -> str:
    """返回 db/ 目录下数据库文件的绝对路径。

    用法:`import _bootstrap; sqlite3.connect(_bootstrap.db_path('xxx.db'))`
    已是绝对路径 / 内存库(:memory:)则原样返回。
    """
    if name == ':memory:' or os.path.isabs(name):
        return name
    return os.path.join(DB_DIR, os.path.basename(name))

# -*- coding: utf-8 -*-
"""兼容 shim —— 真身已于 2026-06-27 阶段3 归位到 data/sources/mootdx.py。
保留本文件让历史导入 `import tdx_mootdx` / `from tdx_mootdx import get_kline` 继续可用。
新代码请直接 `from data.sources import mootdx`。"""
try:
    from data.sources.mootdx import *  # noqa: F401,F403
    from data.sources.mootdx import available, get_kline, get_minute, get_quote  # noqa: F401
except ImportError:                       # data/ 在 path 而项目根不在时
    from sources.mootdx import *          # noqa: F401,F403
    from sources.mootdx import available, get_kline, get_minute, get_quote  # noqa: F401

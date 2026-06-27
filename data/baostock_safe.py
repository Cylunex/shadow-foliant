# -*- coding: utf-8 -*-
"""兼容 shim —— 真身已于 2026-06-27 阶段3 归位到 data/sources/baostock.py。
保留本文件让历史导入 `import baostock_safe` / `from data.baostock_safe import ...` 继续可用。
新代码请直接 `from data.sources import baostock`。"""
try:
    from data.sources.baostock import *  # noqa: F401,F403
    from data.sources.baostock import available, kline  # noqa: F401
except ImportError:                       # data/ 在 path 而项目根不在时
    from sources.baostock import *        # noqa: F401,F403
    from sources.baostock import available, kline  # noqa: F401

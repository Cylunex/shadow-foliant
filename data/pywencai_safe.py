# -*- coding: utf-8 -*-
"""兼容 shim —— 真身已于 2026-06-27 阶段3 归位到 data/sources/pywencai.py。
保留本文件让历史导入 `from data.pywencai_safe import pywencai_get`(全项目 10+ 处)继续可用。
新代码请直接 `from data.sources import pywencai`。"""
try:
    from data.sources.pywencai import *  # noqa: F401,F403
    from data.sources.pywencai import pywencai_get  # noqa: F401
except ImportError:                       # data/ 在 path 而项目根不在时
    from sources.pywencai import *        # noqa: F401,F403
    from sources.pywencai import pywencai_get  # noqa: F401

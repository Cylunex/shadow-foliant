# -*- coding: utf-8 -*-
"""data.sources —— 原子数据源包(一家真 provider 一个模块)。

设计(见 docs/数据源原子化重构计划.md):
  · 每个 sources/<x>.py 只**直连本家 provider**(HTTP/协议/官方库),归一成统一契约后返回。
  · 模块内**禁止** import 任何整合库(akshare/adata/tushare),sources/akshare.py、sources/tushare.py 除外。
  · 模块自身**不做跨源降级、不读缓存**——那是 data/datahub.py 门面(_route + 三级缓存)的职责。
  · 任何异常吞掉返空(空 DataFrame/{}/[]),绝不抛 → 让 datahub._route 切下一个源。

归一工具统一在 data/sources/_common.py(norm_code / em_secid / sina_code / bs_code /
to_ohlcv / http_get_json / throttle / ak_safe)。

⚠️ 当前为**阶段 0 脚手架**:本包已建,但 datahub 尚未切到本包的源(各源仍在
datahub.py / a_stock_data_adapter.py / data_source_manager.py 内联)。阶段 3 才把现有直连
逐 provider 搬进 sources/*.py。本包先提供归一工具收口,供新写直连源即刻复用。
"""

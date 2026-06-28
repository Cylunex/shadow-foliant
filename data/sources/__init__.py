# -*- coding: utf-8 -*-
"""data.sources —— 原子数据源包(一家真 provider 一个模块)。

设计(见 docs/数据源原子化重构计划.md):
  · 每个 sources/<x>.py 只**直连本家 provider**(HTTP/协议/官方库),归一成统一契约后返回。
  · 模块内**禁止** import 任何整合库(akshare/adata/tushare),sources/akshare.py、sources/tushare.py 除外。
  · 模块自身**不做跨源降级、不读缓存**——那是 data/datahub.py 门面(_route + 三级缓存)的职责。
  · 任何异常吞掉返空(空 DataFrame/{}/[]),绝不抛 → 让 datahub._route 切下一个源。

归一工具统一在 data/sources/_common.py(norm_code / em_secid / sina_code / bs_code /
to_ohlcv / http_get_json / throttle / ak_safe)。

现状(阶段 0–3 完成,阶段 4 收尾中,2026-06-28):datahub 各域 `_route` 已直挂本包原子源 ——
sina / tencent / eastmoney / ths / baidu / cls / cninfo / jsl / baostock / mootdx / pywencai 直连真源,
akshare(末位整合库)/ tushare(可选,无 token 不调)。`datahub.kline` raw/qfq 链均为一层直连原子源,
无 fetcher/manager 嵌套。残留:`manager.get_stock_hist_data` 8 源链仅 info/情绪 2 个非热路径消费方仍用,
`akshare.py` 的 sector_ranking_ths/sector_fund_flow_ths 待收。详见 docs/数据源原子化重构计划.md §7。
"""

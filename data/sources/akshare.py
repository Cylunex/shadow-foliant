# -*- coding: utf-8 -*-
"""data.sources.akshare —— akshare 末位兜底层(整合库,**仅此模块 + tushare.py 可 import akshare**)。

定位(见 docs/数据源原子化重构计划.md §2):akshare 是第三方「整合体」(内部偷偷包东财/新浪/
同花顺等真源,实际打哪不可控),故**绝不进主路径**,只作各域 `_route` 的**末位安全网**——
直连真源全挂时还能兜一手。本模块把散落在 datahub/adapter/manager 的 akshare 调用收口到一处,
逐域归一成项目契约后返回。

落地能力(随阶段3⑤/阶段4 增补):
  · kline(code, period, interval, adjust) —— stock_zh_a_hist(个股)/ fund_etf_hist_em(ETF),
        成交量「手」→「股」×100,归一 DatetimeIndex='Date' + 大写 OHLCV。

契约铁律:异常吞掉返空(空 DataFrame / [])、不读缓存、不做跨源降级 —— 那是 datahub._route 的事。
⚠️ akshare 走东财系接口,东财被封时与 east 同死(非真跨源),故排各域**末位**。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from . import _common as C

_PERIOD_DAYS = {"1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "3y": 1095, "5y": 1825}
# ETF / LOF 代码前缀(与 data_source_manager._is_etf_code 同口径):沪 51/56/58、深 15/16。
_ETF_PREFIX = ('51', '56', '58', '15', '16')


def _is_etf(code: str) -> bool:
    c = C.norm_code(code)
    return c.isdigit() and len(c) == 6 and c[:2] in _ETF_PREFIX


def _date_range(period: str):
    """period → (start, end) 'YYYYMMDD'。≤1y 兜底取 ~520 自然日(≈365 交易日)对齐链内主源深度。"""
    days = max(_PERIOD_DAYS.get(period, 365), 520) if period in ('1mo', '3mo', '6mo', '1y') \
        else _PERIOD_DAYS.get(period, 365) + 40
    end = datetime.now()
    return (end - timedelta(days=days)).strftime('%Y%m%d'), end.strftime('%Y%m%d')


def kline(code: str, period: str = "1y", interval: str = "1d", adjust: str = "raw") -> pd.DataFrame:
    """akshare 日线(末位兜底)。adjust='raw' 不复权 / 'qfq' 前复权。仅日线。
    个股走 stock_zh_a_hist、ETF 走 fund_etf_hist_em;成交量「手」→「股」×100。
    返回项目契约 DatetimeIndex='Date' + 大写 OHLCV,或空 DF。"""
    if interval not in ('1d', 'daily', '101'):
        return pd.DataFrame()
    code6 = C.norm_code(code)
    ak_adjust = 'qfq' if str(adjust) == 'qfq' else ''   # ''=不复权(raw)
    start, end = _date_range(period)
    try:
        import akshare as ak
    except Exception:
        return pd.DataFrame()
    try:
        C.throttle('akshare')
        if _is_etf(code6):
            df = C.ak_safe(ak.fund_etf_hist_em, timeout=30, symbol=code6, period='daily',
                           start_date=start, end_date=end, adjust=ak_adjust)
        else:
            df = C.ak_safe(ak.stock_zh_a_hist, timeout=30, symbol=code6, period='daily',
                           start_date=start, end_date=end, adjust=ak_adjust)
    except Exception:
        return pd.DataFrame()
    if df is None or getattr(df, 'empty', True):
        return pd.DataFrame()
    # 东财系 成交量单位「手」→「股」×100(对齐 sina/east/tencent 的「股」口径)。
    return C.to_ohlcv(df, date_col='日期', vol_mult=100.0)


if __name__ == '__main__':
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== data.sources.akshare 末位兜底自检 ===')
    for c in ('600519', '159915'):
        for adj in ('raw', 'qfq'):
            d = kline(c, '3mo', adjust=adj)
            print(f'  {c} {adj}: {len(d)} bars', '' if d.empty else f"last={d.index[-1].date()} C={d['Close'].iloc[-1]} V={d['Volume'].iloc[-1]:.0f}")
    print('OK')

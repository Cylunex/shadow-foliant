# -*- coding: utf-8 -*-
"""data.sources.tushare —— tushare 可选源(平台库 + token,**仅此模块 + akshare.py 可 import 整合库**)。

定位(见 docs/数据源原子化重构计划.md §2 决策):tushare 保留为**可选源**,但
**无 token 时 available()=False、根本不尝试**(不再像旧 manager 无 key 还走降级链空转)。
配了 TUSHARE_TOKEN 才激活,作各域 `_route` 的兜底之一。

落地能力:
  · available() —— 检 TUSHARE_TOKEN(env)+ 库可导入 + pro_api 初始化成功。
  · kline(code, period, interval, adjust) —— 个股日线;raw 走 pro.daily、qfq/hfq 走 ts.pro_bar;
        成交量「手」→「股」×100,归一 DatetimeIndex='Date' + 大写 OHLCV。

契约铁律:异常吞掉返空、不读缓存、不做跨源降级。
⚠️ 本环境无 token,kline 取数路径未经 live 验证(逻辑对齐文档化 tushare API + 旧 manager 口径:
   vol×100 股、amount×1000 元、trade_date 升序);配 token 后由 smoke/直连自检激活校验。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import pandas as pd

from . import _common as C

_PERIOD_DAYS = {"1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "3y": 1095, "5y": 1825}

_PRO = None          # tushare pro_api 单例
_INIT_DONE = False   # 初始化是否已尝试(避免反复 set_token)


def _pro():
    """惰性初始化 tushare pro_api;无 token / 库缺失 / 初始化失败 → None(只试一次)。"""
    global _PRO, _INIT_DONE
    if _INIT_DONE:
        return _PRO
    _INIT_DONE = True
    token = os.getenv('TUSHARE_TOKEN', '').strip()
    if not token:
        return None
    try:
        import tushare as ts
        ts.set_token(token)
        _PRO = ts.pro_api()
    except Exception:
        _PRO = None
    return _PRO


def available() -> bool:
    """有 token 且 pro_api 初始化成功才可用。无 token → False(根本不尝试)。"""
    return _pro() is not None


def _ts_code(code: str) -> str:
    """6 位 → tushare 代码(600519.SH / 000001.SZ / 8xxxxx.BJ)。与 manager._convert_to_ts_code 同口径。"""
    c = C.norm_code(code)
    if c.startswith('6'):
        return f'{c}.SH'
    if c.startswith(('8', '4')) or c[:3] == '920':
        return f'{c}.BJ'
    return f'{c}.SZ'


def _date_range(period: str):
    days = _PERIOD_DAYS.get(period, 365) + 40
    end = datetime.now()
    return (end - timedelta(days=days)).strftime('%Y%m%d'), end.strftime('%Y%m%d')


def kline(code: str, period: str = "1y", interval: str = "1d", adjust: str = "raw") -> pd.DataFrame:
    """tushare 个股日线。adjust='raw' 不复权(pro.daily)/ 'qfq'|'hfq' 复权(ts.pro_bar)。仅日线。
    成交量「手」→「股」×100。无 token/失败/空 → 空 DF。返回项目契约 DatetimeIndex='Date' + 大写 OHLCV。"""
    if interval not in ('1d', 'daily', '101'):
        return pd.DataFrame()
    pro = _pro()
    if pro is None:
        return pd.DataFrame()
    ts_code = _ts_code(code)
    start, end = _date_range(period)
    adj = 'qfq' if str(adjust) == 'qfq' else ('hfq' if str(adjust) == 'hfq' else None)
    try:
        C.throttle('tushare')
        if adj is None:
            df = pro.daily(ts_code=ts_code, start_date=start, end_date=end)
        else:
            import tushare as ts
            df = ts.pro_bar(ts_code=ts_code, freq='D', asset='E', adj=adj,
                            start_date=start, end_date=end)
    except Exception:
        return pd.DataFrame()
    if df is None or getattr(df, 'empty', True):
        return pd.DataFrame()
    try:
        df = df.rename(columns={'trade_date': 'Date', 'vol': 'Volume'})
        # tushare vol 单位「手」→「股」×100(对齐 sina/east/akshare 的「股」口径)。
        return C.to_ohlcv(df, date_col='Date', vol_mult=100.0)
    except Exception:
        return pd.DataFrame()


if __name__ == '__main__':
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== data.sources.tushare 自检 ===')
    print('available():', available())
    if available():
        for adj in ('raw', 'qfq'):
            d = kline('600519', '3mo', adjust=adj)
            print(f'  600519 {adj}: {len(d)} bars', '' if d.empty else f"last={d.index[-1].date()} C={d['Close'].iloc[-1]}")
        print('OK')
    else:
        print('  无 TUSHARE_TOKEN → 源休眠(available()=False),符合「无 token 不尝试」设计。')

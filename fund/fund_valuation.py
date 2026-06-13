"""指数估值分位 + 估值定投信号(阶段二)。

思路:长期定投的择时不靠预测点位,而靠**估值分位**——挂钩宽基指数的 PE/PB 处于历史低位时多投、
高位时少投/暂停。本模块:
  - `index_pe_percentile()` 取指数滚动 PE 历史(akshare 乐咕乐股)算当前分位;
  - `valuation_level()` / `valuation_multiplier()` 纯函数,把分位映射到 档位 / 定投倍数。

倍数表(可调):分位<30% → 2.0x(显著低估,多投);30-50% → 1.5x;50-70% → 1.0x(正常);
70-85% → 0.5x(偏高,少投);>85% → 0.0x(高估,暂停)。
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

try:
    from rate_limiter import throttle
except Exception:
    def throttle(key='akshare', min_interval=None):  # type: ignore
        return 0.0

# 常用宽基指数(乐咕乐股 stock_index_pe_lg 接受的 symbol)
COMMON_INDEXES = ['上证50', '沪深300', '中证500', '中证1000', '创业板指', '科创50']

_LEVELS = [
    (30, '低估', 2.0),
    (50, '正常偏低', 1.5),
    (70, '合理', 1.0),
    (85, '正常偏高', 0.5),
    (101, '高估', 0.0),
]


def valuation_level(percentile: float) -> str:
    """估值分位(0-100)→ 档位文字。"""
    for thr, name, _ in _LEVELS:
        if percentile < thr:
            return name
    return '高估'


def valuation_multiplier(percentile: float) -> float:
    """估值分位(0-100)→ 定投投入倍数(0=暂停)。低估多投、高估暂停。"""
    for thr, _, mult in _LEVELS:
        if percentile < thr:
            return mult
    return 0.0


def index_pe_percentile(index: str = '沪深300', years: Optional[int] = None) -> Optional[dict]:
    """指数当前滚动 PE 在历史中的分位。years 限定回溯年数(None=全历史)。
    返回 {index, pe, percentile(0-100,越小越低估), level, multiplier, source, n} 或 None。"""
    try:
        import akshare as ak
    except Exception:
        return None
    try:
        throttle('akshare')
        df = ak.stock_index_pe_lg(symbol=index)
    except Exception as e:
        print(f'[fund_valuation] index_pe_percentile({index}) 失败: {type(e).__name__}')
        return None
    if df is None or df.empty:
        return None
    col = next((c for c in df.columns if c == '滚动市盈率'), None) \
        or next((c for c in df.columns if '滚动市盈率' in c), None) \
        or next((c for c in df.columns if '市盈率' in c), None)
    if not col or '日期' not in df.columns:
        return None
    s = pd.Series(pd.to_numeric(df[col], errors='coerce').values,
                  index=pd.to_datetime(df['日期'])).dropna()
    if years:
        cutoff = s.index.max() - pd.Timedelta(days=365 * years)
        s = s[s.index >= cutoff]
    if len(s) < 30:
        return None
    cur = float(s.iloc[-1])
    pct = float((s <= cur).mean() * 100)
    return {
        'index': index, 'pe': round(cur, 2), 'percentile': round(pct, 1),
        'level': valuation_level(pct), 'multiplier': valuation_multiplier(pct),
        'source': 'legulegu', 'n': int(len(s)),
        'start': s.index[0].strftime('%Y-%m-%d'), 'end': s.index[-1].strftime('%Y-%m-%d'),
    }


def rolling_percentile_series(values: pd.Series, window: int = 504) -> pd.Series:
    """滚动分位序列(每点 = 当前值在过去 window 个点中的分位 0-100)。
    回测里没有外部估值序列时,用基金自身净值的滚动分位作"低位/高位"代理。"""
    values = pd.to_numeric(values, errors='coerce')

    def _pct(x):
        return (x <= x[-1]).mean() * 100

    return values.rolling(window, min_periods=max(20, window // 5)).apply(_pct, raw=True)


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    # 纯函数自测
    for p in (10, 40, 60, 80, 95):
        print(f'分位{p}% → {valuation_level(p)} / {valuation_multiplier(p)}x')
    print('沪深300 估值:', index_pe_percentile('沪深300'))

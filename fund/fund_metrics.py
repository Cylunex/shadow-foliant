"""净值指标 —— 纯函数,离线可算,无外部依赖(numpy/pandas)。

输入统一为「净值序列」:pandas.Series(index=日期, values=净值) 或可转 Series 的 DataFrame。
输出 dict,字段含义见各函数。用于基金长期业绩与风险评价。
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

TRADING_DAYS = 252  # 年化基准交易日


def _to_series(nav) -> pd.Series:
    """把入参规整成净值 Series(按日期升序,去空)。"""
    if isinstance(nav, pd.Series):
        s = nav.copy()
    elif isinstance(nav, pd.DataFrame):
        col = 'unit_nav' if 'unit_nav' in nav.columns else nav.columns[-1]
        idx = 'date' if 'date' in nav.columns else None
        s = nav.set_index(idx)[col] if idx else nav[col]
    else:
        s = pd.Series(nav)
    s = pd.to_numeric(s, errors='coerce').dropna()
    try:
        s = s.sort_index()
    except Exception:
        pass
    return s


def annualized_return(nav) -> Optional[float]:
    """区间年化收益率(几何),按自然日折算。无法计算返回 None。"""
    s = _to_series(nav)
    if len(s) < 2 or s.iloc[0] <= 0:
        return None
    total = s.iloc[-1] / s.iloc[0]
    try:
        days = (s.index[-1] - s.index[0]).days
    except Exception:
        days = len(s)
    if days <= 0:
        return None
    return float(total ** (365.0 / days) - 1)


def max_drawdown(nav) -> Optional[float]:
    """最大回撤(正数表示回撤幅度,如 0.23 = -23%)。"""
    s = _to_series(nav)
    if len(s) < 2:
        return None
    roll_max = s.cummax()
    dd = (s - roll_max) / roll_max
    return float(-dd.min())


def annualized_volatility(nav) -> Optional[float]:
    """年化波动率(日收益标准差 × sqrt(252))。"""
    s = _to_series(nav)
    if len(s) < 3:
        return None
    ret = s.pct_change().dropna()
    if ret.empty:
        return None
    return float(ret.std() * np.sqrt(TRADING_DAYS))


def sharpe_ratio(nav, rf: float = 0.02) -> Optional[float]:
    """年化夏普(rf 默认 2% 无风险年利率)。"""
    s = _to_series(nav)
    if len(s) < 3:
        return None
    ret = s.pct_change().dropna()
    if ret.empty or ret.std() == 0:
        return None
    excess = ret.mean() * TRADING_DAYS - rf
    return float(excess / (ret.std() * np.sqrt(TRADING_DAYS)))


def calmar_ratio(nav) -> Optional[float]:
    """卡玛比率 = 年化收益 / 最大回撤(回撤越小、收益越高越好)。"""
    ar = annualized_return(nav)
    mdd = max_drawdown(nav)
    if ar is None or not mdd:
        return None
    return float(ar / mdd)


def downside_deviation(nav, mar: float = 0.0) -> Optional[float]:
    """下行波动率(只统计低于最低可接受收益 mar 的部分,年化)。"""
    s = _to_series(nav)
    if len(s) < 3:
        return None
    ret = s.pct_change().dropna()
    downside = ret[ret < mar]
    if downside.empty:
        return 0.0
    return float(np.sqrt((downside ** 2).mean()) * np.sqrt(TRADING_DAYS))


def total_return(nav) -> Optional[float]:
    """区间累计收益率。"""
    s = _to_series(nav)
    if len(s) < 2 or s.iloc[0] <= 0:
        return None
    return float(s.iloc[-1] / s.iloc[0] - 1)


def evaluate(nav, rf: float = 0.02) -> dict:
    """一次性算齐全部指标 + 区间信息。给 UI / MCP / 评价卡用。"""
    s = _to_series(nav)
    out = {
        'n_points': int(len(s)),
        'start': s.index[0].strftime('%Y-%m-%d') if len(s) else None,
        'end': s.index[-1].strftime('%Y-%m-%d') if len(s) else None,
        'total_return': total_return(s),
        'annualized_return': annualized_return(s),
        'max_drawdown': max_drawdown(s),
        'annualized_volatility': annualized_volatility(s),
        'sharpe': sharpe_ratio(s, rf),
        'calmar': calmar_ratio(s),
        'downside_deviation': downside_deviation(s),
    }
    return out


if __name__ == '__main__':
    # 自测:构造一条带波动的净值
    idx = pd.date_range('2021-01-01', periods=500, freq='D')
    rng = np.random.default_rng(42)
    nav = pd.Series((1 + rng.normal(0.0004, 0.012, 500)).cumprod(), index=idx)
    from pprint import pprint
    pprint(evaluate(nav))

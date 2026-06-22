# -*- coding: utf-8 -*-
"""因子库(Factor Zoo)—— 借鉴 Vibe-Trading 因子声明式注册表。

把"因子"标准化成可注册、可批量评估的对象:每个因子 = 一个作用于 K线 DataFrame 的函数,
返回**逐日因子值序列**(pd.Series,与 df 对齐)。这样一次 K线即可在多个历史时点取因子值,
配合 factor_eval 的 IC 评估,科学衡量因子是否真有预测力(防 β 追踪、防过拟合)。

只放**价量类因子**(从单只 K线即可算,无需外部面板),覆盖:动量/反转/波动/量能/位置/流动性。
基本面因子(PE/PB/ROE)已在 multi_factor_screener,口径不同,这里不重复。

加新因子:在 FACTORS 里加一项 {key:(中文名, 类别, 方向, fn)}。
  方向 direction: +1 = 因子越大未来收益越高(正向),-1 = 越大越差(如波动/反转)。
"""
from __future__ import annotations

from typing import Callable, Dict, Tuple

import numpy as np
import pandas as pd


def _close(df):
    for c in ("close", "Close", "收盘"):
        if c in df.columns:
            return df[c].astype(float)
    return None


def _vol(df):
    for c in ("volume", "Volume", "成交量"):
        if c in df.columns:
            return df[c].astype(float)
    return None


def _hi(df):
    for c in ("high", "High", "最高"):
        if c in df.columns:
            return df[c].astype(float)
    return None


def _lo(df):
    for c in ("low", "Low", "最低"):
        if c in df.columns:
            return df[c].astype(float)
    return None


# ── 因子函数:df → pd.Series(逐日因子值)──

def f_mom_20(df):
    c = _close(df); return c / c.shift(20) - 1 if c is not None else None


def f_mom_60(df):
    c = _close(df); return c / c.shift(60) - 1 if c is not None else None


def f_reversal_5(df):
    c = _close(df); return c / c.shift(5) - 1 if c is not None else None  # 方向-1:近期涨多→反转


def f_vol_20(df):
    c = _close(df)
    if c is None:
        return None
    return c.pct_change().rolling(20).std()  # 方向-1:低波动更优


def f_ma_bias_20(df):
    c = _close(df)
    if c is None:
        return None
    ma = c.rolling(20).mean()
    return (c - ma) / ma


def f_rsi_14(df):
    c = _close(df)
    if c is None:
        return None
    d = c.diff()
    up = d.clip(lower=0).rolling(14).mean()
    dn = (-d.clip(upper=0)).rolling(14).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def f_max_ret_20(df):
    c = _close(df)
    if c is None:
        return None
    return c.pct_change().rolling(20).max()  # 方向-1:彩票因子,单日暴涨过的未来差


def f_vol_trend(df):
    v = _vol(df)
    if v is None:
        return None
    return v.rolling(5).mean() / v.rolling(20).mean()  # 量能放大


def f_amihud(df):
    """非流动性(Amihud):|日收益|/成交量,越大越不流动。方向-1。"""
    c, v = _close(df), _vol(df)
    if c is None or v is None:
        return None
    return (c.pct_change().abs() / v.replace(0, np.nan)).rolling(20).mean()


def f_close_position_20(df):
    """20日价格分位:(close-min)/(max-min),越高越靠近区间顶部。"""
    c, h, l = _close(df), _hi(df), _lo(df)
    if c is None:
        return None
    hh = (h if h is not None else c).rolling(20).max()
    ll = (l if l is not None else c).rolling(20).min()
    return (c - ll) / (hh - ll).replace(0, np.nan)


def f_range_20(df):
    """20日日内振幅均值((high-low)/close),方向-1(高振幅常伴随风险)。"""
    c, h, l = _close(df), _hi(df), _lo(df)
    if c is None or h is None or l is None:
        return None
    return ((h - l) / c).rolling(20).mean()


def f_mom_accel(df):
    """动量加速:20日动量 - 前20日的20日动量(动量是否在增强)。"""
    c = _close(df)
    if c is None:
        return None
    m = c / c.shift(20) - 1
    return m - m.shift(20)


def f_high_52w(df):
    """52周高点占比(George-Hwang 2004):close / 252日最高。越接近1越强,方向+1。

    经典「52周高点」异常:股价逼近一年高点时,投资者锚定旧高点而对利好反应不足,
    随后继续上行 → 该比值高的未来收益更好。借鉴 Vibe-Trading academic_high52w。"""
    c = _close(df)
    if c is None:
        return None
    return c / c.rolling(252).max().replace(0, np.nan)


def f_ret_skew(df):
    """收益偏度(Harvey-Siddique 2000):日收益 60日滚动偏度,方向-1。

    特质偏度异常:高(右)偏度的股票像彩票被高估、未来收益更差 → 方向-1。
    借鉴 Vibe-Trading academic_retskew(其取 -skew 后正向,这里用原始偏度 + 方向-1,等价)。"""
    c = _close(df)
    if c is None:
        return None
    return c.pct_change().rolling(60).skew()


# key: (中文名, 类别, 方向, fn)
FACTORS: Dict[str, Tuple[str, str, int, Callable]] = {
    "mom_20":        ("20日动量",     "动量", +1, f_mom_20),
    "mom_60":        ("60日动量",     "动量", +1, f_mom_60),
    "mom_accel":     ("动量加速",     "动量", +1, f_mom_accel),
    "reversal_5":    ("5日反转",      "反转", -1, f_reversal_5),
    "vol_20":        ("20日波动",     "波动", -1, f_vol_20),
    "range_20":      ("20日振幅",     "波动", -1, f_range_20),
    "max_ret_20":    ("彩票(最大日涨)", "波动", -1, f_max_ret_20),
    "ma_bias_20":    ("乖离MA20",     "位置", +1, f_ma_bias_20),
    "close_pos_20":  ("20日价格分位", "位置", +1, f_close_position_20),
    "high_52w":      ("52周高点占比", "位置", +1, f_high_52w),
    "rsi_14":        ("RSI14",        "动量", +1, f_rsi_14),
    "vol_trend":     ("量能趋势",     "量能", +1, f_vol_trend),
    "amihud":        ("非流动性",     "流动性", -1, f_amihud),
    "ret_skew":      ("收益偏度",     "波动", -1, f_ret_skew),
}


def compute(df: pd.DataFrame, keys=None) -> Dict[str, pd.Series]:
    """对一只 K线算指定因子(缺省全算),返回 {key: Series}。"""
    out = {}
    for k in (keys or FACTORS):
        meta = FACTORS.get(k)
        if not meta:
            continue
        try:
            s = meta[3](df)
            if s is not None:
                out[k] = s
        except Exception:
            continue
    return out


def list_factors():
    return [{"key": k, "name": v[0], "category": v[1], "direction": v[2]} for k, v in FACTORS.items()]


if __name__ == "__main__":
    import io
    import os
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print("=== 因子库 ===")
    for f in list_factors():
        print(f"  {f['key']:14s} {f['name']}  [{f['category']}] 方向{f['direction']:+d}")
    print(f"共 {len(FACTORS)} 个价量因子")

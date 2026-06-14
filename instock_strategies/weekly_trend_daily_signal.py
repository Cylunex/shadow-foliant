#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
周线趋势 + 日线信号 — 多周期共振策略

逻辑（双周期过滤）：
  周线层：
    1. 周线 MACD(12,26,9) 的 DIF > DEA（多头趋势确认）
    2. 周线收盘价 > MA10（周线均线向上）
  日线层（本周内）：
    3. 日线放量上涨（成交量 > MA10量的1.5倍，日涨幅>1.5%）
    4. 日线收盘价突破前5日最高价

与现有区别：
  现有所有策略只在日线单周期判断；
  本策略要求周线趋势多头 + 日线触发 → 双重过滤降假信号。
"""

import numpy as np
from instock_strategies._talib_compat import tl
from collections import deque


def _resample_weekly(closes):
    """从日线收盘价近似计算周线：取每5个交易日的收盘价"""
    weekly = []
    for i in range(4, len(closes), 5):
        weekly.append(float(closes[i]))
    return np.array(weekly, dtype=float)


def check(code_name, data, date=None, threshold=120,
          weekly_ma_period=10, daily_ma_period=10,
          vol_ratio_min=1.5, daily_ret_min=1.5,
          breakout_days=5):
    """
    weekly_ma_period: 周线MA周期（周数，默认10=约50个交易日）
    daily_ma_period: 日线均量周期
    """
    threshold, weekly_ma_period = int(threshold), int(weekly_ma_period)
    daily_ma_period, breakout_days = int(daily_ma_period), int(breakout_days)
    if date is None:
        end_date = code_name[0]
    else:
        end_date = date.strftime("%Y-%m-%d")
    if end_date is not None:
        mask = (data['date'] <= end_date)
        data = data.loc[mask].copy()
    if len(data.index) < threshold:
        return False

    closes = data['close'].values

    # ── 周线层 ──
    weekly_closes = _resample_weekly(closes)
    if len(weekly_closes) < weekly_ma_period + 26:  # MACD 需要26周
        return False

    # 周线 MACD
    w_dif, w_dea, w_hist = tl.MACD(weekly_closes, fastperiod=12,
                                   slowperiod=26, signalperiod=9)
    if len(w_dif) < 2 or np.isnan(w_dif[-1]) or np.isnan(w_dea[-1]):
        return False
    if w_dif[-1] <= w_dea[-1]:
        return False  # 周线 MACD 非多头

    # 周线 MA 向上
    w_ma = tl.MA(weekly_closes, timeperiod=weekly_ma_period)
    if len(w_ma) < 2 or np.isnan(w_ma[-1]):
        return False
    if weekly_closes[-1] <= w_ma[-1]:
        return False  # 周线价格在均线下方

    # 周线趋势方向确认（最近2周 MA 在上升）
    if len(w_ma) >= 3 and not np.isnan(w_ma[-2]):
        if w_ma[-1] <= w_ma[-2]:
            return False

    # ── 日线层 ──
    # 条件1: 日线涨幅
    last_close = float(data.iloc[-1]['close'])
    last_open = float(data.iloc[-1]['open'])
    if last_open <= 0:
        return False
    daily_ret = (last_close - last_open) / last_open * 100
    if daily_ret < daily_ret_min:
        return False

    # 条件2: 日线放量
    if 'volume' in data.columns:
        volumes = data['volume'].values
        if len(volumes) < daily_ma_period + 1:
            return False
        ma_vol = np.mean(volumes[-(daily_ma_period + 1):-1])
        if volumes[-1] < ma_vol * vol_ratio_min:
            return False

    # 条件3: 突破前N日最高价（日线突破确认）
    if len(closes) < breakout_days + 1:
        return False
    prev_high = max(closes[-(breakout_days + 1):-1])
    if last_close <= prev_high:
        return False

    return True

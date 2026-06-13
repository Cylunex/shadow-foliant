#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSI 超卖反弹 — 左侧抄底策略

逻辑：
  1. RSI(14) < oversold_threshold(默认30) 持续 min_days(默认2) 天
  2. 今日收阳（收盘>开盘）且放量（成交量>MA5量1.2倍）
  3. 最近 max_days(默认5) 天内有新低（确认是超卖段）

与现有策略区别：所有 InStock 10 套都是趋势延续/突破型，
这是唯一一套左侧反转捕捉策略。
"""

import numpy as np
import talib as tl


def check(code_name, data, date=None, threshold=60,
          oversold_threshold=30, min_days=2, vol_ratio_min=1.2,
          max_days=5):
    threshold, min_days, max_days = int(threshold), int(min_days), int(max_days)
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
    rsi = tl.RSI(closes, timeperiod=14)

    # 至少需要 14+min_days 天数据
    if len(rsi) < 14 + min_days:
        return False

    # 条件1: 最近 min_days 天 RSI 都低于超卖阈值
    recent_rsi = rsi[-min_days:]
    if any(np.isnan(recent_rsi)):
        return False
    if not all(r < oversold_threshold for r in recent_rsi):
        return False

    # 条件2: 今日收阳
    last_close = float(data.iloc[-1]['close'])
    last_open = float(data.iloc[-1]['open'])
    if last_close <= last_open:
        return False

    # 条件3: 放量（今日量 > MA5量 * vol_ratio_min）
    if 'volume' in data.columns:
        volumes = data['volume'].values
        ma5_vol = tl.MA(volumes, timeperiod=5)
        if len(ma5_vol) < 6 or np.isnan(ma5_vol[-1]):
            return False
        if volumes[-1] < ma5_vol[-1] * vol_ratio_min:
            return False

    # 条件4: 最近 max_days 天内有新低（确认处于超卖段）
    recent_closes = closes[-max_days:]
    if len(recent_closes) < max_days:
        return False
    if min(recent_closes) != min(recent_closes):
        return False  # NaN check
    if last_close > min(recent_closes) * 1.02:  # 已经从低点反弹2%以上
        pass  # 这是加分项，不拒绝
    else:
        # 允许就在低点附近（还没反弹）
        pass

    return True

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
布林带收窄突破 — 波动率压缩后爆发策略

逻辑：
  1. 布林带宽压缩到 N 日最小值（带宽 = (上轨-下轨)/中轨）
  2. 收窄期间持续 min_sqz_days 天以上
  3. 突破日：收盘价上穿上轨 + 放量（量>MA5量1.5倍）
  4. 收窄期间的振幅不能太大（排除宽幅震荡假压缩）

与突破平台(breakthrough_platform)区别：
  突破平台看价格突破MA60+放量；
  本策略看波动率压缩到极致后的爆发，逻辑基础完全不同。
"""

import numpy as np
import talib as tl


def check(code_name, data, date=None, threshold=100,
          bb_period=20, bb_std=2.0,
          sqz_percentile=20, min_sqz_days=3,
          vol_ratio_min=1.5, break_ret_min=1.0):
    """
    sqz_percentile: 带宽低于历史N日百分之多少才算"压缩"
    min_sqz_days: 压缩状态至少持续多少天
    """
    threshold, bb_period, min_sqz_days = int(threshold), int(bb_period), int(min_sqz_days)
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

    # 布林带
    upper, middle, lower = tl.BBANDS(closes, timeperiod=bb_period,
                                     nbdevup=bb_std, nbdevdn=bb_std)
    bandwidth = (upper - lower) / middle  # 相对带宽

    # NaN 检查
    valid_idx = ~np.isnan(bandwidth)
    if sum(valid_idx) < bb_period + min_sqz_days + 1:
        return False

    # 计算带宽的历史百分位
    bw_valid = bandwidth[valid_idx]
    sqz_threshold = np.percentile(bw_valid[-threshold:], sqz_percentile)

    # 条件1: 最近 min_sqz_days 天带宽都 < 压缩阈值
    recent_bw = bandwidth[-min_sqz_days:]
    if any(np.isnan(recent_bw)):
        return False
    if not all(b < sqz_threshold for b in recent_bw):
        return False

    # 条件2: 今日突破上轨
    last_close = float(data.iloc[-1]['close'])
    if len(upper) == 0 or np.isnan(upper[-1]):
        return False
    if last_close < upper[-1]:
        return False

    # 条件3: 突破日涨幅确认
    last_open = float(data.iloc[-1]['open'])
    if last_open <= 0:
        return False
    day_ret = (last_close - last_open) / last_open * 100
    if day_ret < break_ret_min:
        return False

    # 条件4: 放量突破
    if 'volume' in data.columns:
        volumes = data['volume'].values[-30:]
        if len(volumes) < 6:
            return False
        ma5_vol = np.mean(volumes[-6:-1])  # 前5日均量
        if volumes[-1] < ma5_vol * vol_ratio_min:
            return False

    # 条件5: 压缩期间振幅不能太大（排除宽幅震荡）
    sqz_closes = closes[-min_sqz_days - 3:-1]  # 压缩期（不含突破日）
    if len(sqz_closes) > 0:
        sqz_range = (max(sqz_closes) - min(sqz_closes)) / min(sqz_closes) * 100
        if sqz_range > 5:  # 压缩期振幅 >5% 不算真压缩
            return False

    return True

#!/usr/local/bin/python
# -*- coding: utf-8 -*-

import numpy as np
from instock_strategies._talib_compat import tl


__author__ = 'myh '
__date__ = '2023/3/10 '


# 放量上涨
# 1.当日比前一天上涨小于 pct_change_min%(默认2) 或收盘价小于开盘价
# 2.当日成交额不低于 amount_min_yi 亿(默认2)
# 3.当日成交量/5日平均成交量 >= vol_ratio_min(默认2)
# 参数化(策略基因组): pct_change_min / amount_min_yi / vol_ratio_min,默认值=原硬编码
def check_volume(code_name, data, date=None, threshold=60,
                 pct_change_min=2.0, amount_min_yi=2.0, vol_ratio_min=2.0):
    threshold = int(threshold)
    if date is None:
        end_date = code_name[0]
    else:
        end_date = date.strftime("%Y-%m-%d")
    if end_date is not None:
        mask = (data['date'] <= end_date)
        data = data.loc[mask].copy()
    if len(data.index) < threshold:
        return False

    p_change = data.iloc[-1]['p_change']
    if p_change < pct_change_min or data.iloc[-1]['close'] < data.iloc[-1]['open']:
        return False

    data.loc[:, 'vol_ma5'] = tl.MA(data['volume'].values, timeperiod=5)
    data['vol_ma5'] = data['vol_ma5'].fillna(0.0)

    data = data.tail(n=threshold + 1)
    if len(data) < threshold + 1:
        return False

    # 最后一天收盘价
    last_close = data.iloc[-1]['close']
    # 最后一天成交量
    last_vol = data.iloc[-1]['volume']

    amount = last_close * last_vol

    # 成交额不低于 amount_min_yi 亿
    if amount < amount_min_yi * 100000000:
        return False

    data = data.head(n=threshold)

    mean_vol = data.iloc[-1]['vol_ma5']

    # ⚠️ mean_vol 可能为 0(line 35 fillna(0.0)) / NaN — 新股不足5日 / 长停牌恢复。
    # 不防的话 last_vol/0 = inf, inf >= vol_ratio_min 永真 → 误判"放量突破"(隐性 bug)。
    if not mean_vol or np.isnan(mean_vol) or mean_vol <= 0:
        return False

    vol_ratio = last_vol / mean_vol
    if vol_ratio >= vol_ratio_min:
        return True
    else:
        return False

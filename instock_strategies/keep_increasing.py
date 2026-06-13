#!/usr/local/bin/python
# -*- coding: utf-8 -*-

import numpy as np
import talib as tl

__author__ = 'myh '
__date__ = '2023/3/10 '


# 持续上涨（均线向上）
# 均线多头
# 1.期初的MA<1/3处MA<2/3处MA<当日MA(默认MA30)
# 2.(当日MA/期初MA) > ratio_min(默认1.2)
# 参数化(策略基因组): ma_period / ratio_min,默认值=原硬编码
def check(code_name, data, date=None, threshold=30, ma_period=30, ratio_min=1.2):
    threshold = int(threshold)
    ma_period = int(ma_period)
    if date is None:
        end_date = code_name[0]
    else:
        end_date = date.strftime("%Y-%m-%d")
    if end_date is not None:
        mask = (data['date'] <= end_date)
        data = data.loc[mask].copy()
    if len(data.index) < max(threshold, ma_period):
        return False

    data.loc[:, 'ma30'] = tl.MA(data['close'].values, timeperiod=ma_period)
    data['ma30'] = data['ma30'].fillna(0.0)

    data = data.tail(n=threshold)

    step1 = round(threshold / 3)
    step2 = round(threshold * 2 / 3)

    if data.iloc[0]['ma30'] < data.iloc[step1]['ma30'] < \
            data.iloc[step2]['ma30'] < data.iloc[-1]['ma30'] and data.iloc[-1]['ma30'] > ratio_min * data.iloc[0]['ma30']:
        return True
    else:
        return False

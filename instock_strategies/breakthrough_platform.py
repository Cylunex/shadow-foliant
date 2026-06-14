#!/usr/local/bin/python
# -*- coding: utf-8 -*-

from datetime import datetime
import numpy as np
from instock_strategies._talib_compat import tl
from instock_strategies import enter

__author__ = 'myh '
__date__ = '2023/3/10 '


# 平台突破策略
# 1.threshold 日内某日收盘价>=ma_period 日均线>开盘价
# 2.且【1】放量上涨
# 3.且【1】间之前时间，任意一天收盘价与均线偏离在 deviation_low%~deviation_high% 之间(默认-5%~20%)
# 参数化(策略基因组): ma_period / deviation_low / deviation_high,默认值=原硬编码
def check(code_name, data, date=None, threshold=60,
          ma_period=60, deviation_low=-5.0, deviation_high=20.0):
    threshold = int(threshold)
    ma_period = int(ma_period)
    origin_data = data
    if date is None:
        end_date = code_name[0]
    else:
        end_date = date.strftime("%Y-%m-%d")
    if end_date is not None:
        mask = (data['date'] <= end_date)
        data = data.loc[mask].copy()
    if len(data.index) < max(threshold, ma_period):
        return False

    data.loc[:, 'ma60'] = tl.MA(data['close'].values, timeperiod=ma_period)
    data['ma60'] = data['ma60'].fillna(0.0)

    data = data.tail(n=threshold)

    breakthrough_row = None
    for _close, _open, _date, _ma60 in zip(data['close'].values, data['open'].values, data['date'].values, data['ma60'].values):
        if _open < _ma60 <= _close:
            if enter.check_volume(code_name, origin_data, date=datetime.date(datetime.strptime(_date, '%Y-%m-%d')), threshold=threshold):
                breakthrough_row = _date
                break

    if breakthrough_row is None:
        return False

    data_front = data.loc[(data['date'] < breakthrough_row) & (data['ma60'] > 0)]
    for _close, _ma60 in zip(data_front['close'].values, data_front['ma60'].values):
        if not (deviation_low / 100 < ((_ma60 - _close) / _ma60) < deviation_high / 100):
            return False

    return True

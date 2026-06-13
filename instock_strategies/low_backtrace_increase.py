#!/usr/local/bin/python
# -*- coding: utf-8 -*-


__author__ = 'myh '
__date__ = '2023/3/10 '


# 无大幅回撤
# 1.当日收盘价比 threshold 日前的收盘价涨幅 ≥ ratio_min(默认0.6=60%)
# 2.最近 threshold 日，不能有单日跌幅超 |max_single_drop|%、高开低走同幅、两日累计跌幅超 |max_two_day_drop|%
# 参数化(策略基因组): ratio_min / max_single_drop / max_two_day_drop,默认值=原硬编码
def check(code_name, data, date=None, threshold=60,
          ratio_min=0.6, max_single_drop=-7.0, max_two_day_drop=-10.0):
    threshold = int(threshold)
    if date is None:
        end_date = code_name[0]
    else:
        end_date = date.strftime("%Y-%m-%d")
    if end_date is not None:
        mask = (data['date'] <= end_date)
        data = data.loc[mask]
    if len(data.index) < threshold:
        return False

    data = data.tail(n=threshold)

    ratio_increase = (data.iloc[-1]['close'] - data.iloc[0]['close']) / data.iloc[0]['close']
    if ratio_increase < ratio_min:
        return False

    # 允许有一次“洗盘”
    previous_p_change = 100.0
    previous_open = -1000000.0
    for _p_change, _close, _open in zip(data['p_change'].values, data['close'].values, data['open'].values):
        # 单日跌幅超限；高开低走超限；两日累计跌幅超限；两日高开低走累计超限
        if _p_change < max_single_drop or (_close - _open) / _open * 100 < max_single_drop \
                or previous_p_change + _p_change < max_two_day_drop \
                or (_close - previous_open)/previous_open * 100 < max_two_day_drop:
            return False
        previous_p_change = _p_change
        previous_open = _open
    return True

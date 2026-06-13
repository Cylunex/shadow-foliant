#!/usr/local/bin/python
# -*- coding: utf-8 -*-


__author__ = 'myh '
__date__ = '2023/3/10 '


# 高而窄的旗形
# 1.必须至少上市交易 threshold 日(默认60)
# 2.当日收盘价/之前24~10日的最低价 >= ratio_min(默认1.9)
# 3.之前24~10日必须连续两天涨幅大于等于 surge_pct_min%(默认9.5)
# 参数化(策略基因组): ratio_min / surge_pct_min,默认值=原硬编码
# ⚠️ istop 原默认 False("龙虎榜须有机构"门槛)使该策略在所有调用路径永不触发(无人传 True)——
#    该数据校验从未接线,2026-06-12 改默认 True 启用纯技术形态判定。
def check_high_tight(code_name, data, date=None, threshold=60, istop=True,
                     ratio_min=1.9, surge_pct_min=9.5):
    # 龙虎榜上必须有机构(调用方可显式传 istop=False 维持旧行为)
    if not istop:
        return False
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

    data = data.tail(n=24)
    data = data.head(n=14)
    low = data['low'].values.min()
    ratio_increase = data.iloc[-1]['high'] / low
    if ratio_increase < ratio_min:
        return False

    # 连续两天涨幅大于等于 surge_pct_min
    previous_p_change = 0.0
    for _p_change in data['p_change'].values:
        if _p_change >= surge_pct_min:
            if previous_p_change >= surge_pct_min:
                return True
            else:
                previous_p_change = _p_change
        else:
            previous_p_change = 0.0

    return False

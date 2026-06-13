# -*- coding: utf-8 -*-
"""可转债"双低"策略选债(A股最经典的可转债策略)。

双低 = 转债现价 + 转股溢价率(%);越低越好——兼顾"债底保护(低价)"与"跟涨弹性(低溢价)"。
数据走 datahub.convertible_bonds()(东财比价表→集思录,自动兜底+缓存)。纯函数,无副作用。

默认护栏(剔除典型陷阱):价格上限、剩余规模下限(剔小盘易操纵/退市)、剔除已触发/退市类。
"""
from __future__ import annotations

import os
import sys
from typing import List, Dict, Optional

# 作为库被 import 时入口已加 root;独立运行(python analysis/convertible_bond.py)时补 root
if not any(os.path.basename(p) == 'shadow-foliant' for p in sys.path):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401,E402


def screen_double_low(top_n: int = 20,
                      max_price: float = 135.0,
                      max_premium: float = 40.0,
                      min_remain_scale_yi: float = 1.0,
                      min_rating: Optional[str] = 'A+',
                      max_double_low: Optional[float] = None) -> List[Dict]:
    """双低选债:按"双低值"升序取 TopN。

    max_price        转债现价上限(默认 135,债性保护;>150 多为强股性,双低意义弱)
    max_premium      转股溢价率上限(%,默认 40;过高=股性弱)
    min_remain_scale_yi 剩余规模下限(亿,默认 1.0;剔小盘易被操纵/面临退市)
    min_rating       债券评级下限(默认 'A+';None=不限)。评级序:AAA>AA+>AA>AA->A+>A>A-...
    max_double_low   双低值上限(可选)
    返回 list[dict](datahub 字段 + 已按双低排序),失败返回 []。
    """
    import datahub
    bonds = datahub.convertible_bonds() or []
    if not bonds:
        return []

    rank = _rating_rank(min_rating) if min_rating else -1
    out = []
    for b in bonds:
        price, prem, dl = b.get('price'), b.get('premium_pct'), b.get('double_low')
        if dl is None or price is None:
            continue
        if max_price and price > max_price:
            continue
        if max_premium is not None and prem is not None and prem > max_premium:
            continue
        if max_double_low is not None and dl > max_double_low:
            continue
        scale = b.get('remain_scale_yi')
        if min_remain_scale_yi and scale is not None and scale < min_remain_scale_yi:
            continue
        if rank >= 0 and _rating_rank(b.get('rating')) < rank:
            continue
        out.append(b)

    out.sort(key=lambda x: x['double_low'])
    return out[:top_n]


# 债券评级序(越大越优),用于评级下限过滤
_RATING_ORDER = ['C', 'B-', 'B', 'B+', 'BB-', 'BB', 'BB+', 'BBB-', 'BBB', 'BBB+',
                 'A-', 'A', 'A+', 'AA-', 'AA', 'AA+', 'AAA']


def _rating_rank(rating: Optional[str]) -> int:
    if not rating:
        return -1
    r = str(rating).strip().upper()
    return _RATING_ORDER.index(r) if r in _RATING_ORDER else -1


def market_summary() -> Dict:
    """全市场可转债概览:数量 / 中位现价 / 中位双低 / 中位溢价率。失败返回 {}。"""
    import datahub
    bonds = [b for b in (datahub.convertible_bonds() or []) if b.get('double_low') is not None]
    if not bonds:
        return {}

    def _med(key):
        vals = sorted(b[key] for b in bonds if b.get(key) is not None)
        n = len(vals)
        return round(vals[n // 2], 2) if n else None

    return {
        'count': len(bonds),
        'median_price': _med('price'),
        'median_double_low': _med('double_low'),
        'median_premium_pct': _med('premium_pct'),
    }


if __name__ == '__main__':
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== 可转债双低自检 ===')
    print('市场概览:', market_summary())
    picks = screen_double_low(top_n=10, min_remain_scale_yi=0)  # 自检放宽规模(集思录匿名样本小)
    print(f'双低 Top{len(picks)}:')
    for b in picks:
        print(f"  {b['code']} {b['name']:<8} 价{b['price']} 溢价{b['premium_pct']}% "
              f"双低{b['double_low']} 评级{b['rating']} {b['stock_name']}")

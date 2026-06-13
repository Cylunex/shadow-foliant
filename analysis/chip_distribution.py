"""筹码分布(CYQ) —— 由 K线本地估算(无需联网,补 akshare 筹码接口在本机不可用的空白)。

借鉴 InStock/通达信筹码概念:把历史每根K线的成交量按价格区间分摊、按时间衰减累积,
得到"成本分布",据此算 获利盘比例 / 平均成本 / 90%(70%)成本区间 / 集中度。

⚠️ 近似实现:用每根K线 [low,high] 区间均匀分摊 + 按距今半衰期衰减(无流通股本做真实换手洗筹),
数值用于"相对判断"(获利盘高低、筹码集中/发散、当前价在成本区位置),不等同券商精确筹码。

输入:DataFrame 含 open/high/low/close/volume(小写,升序)。返回 dict;数据不足返回 {available:False}。
"""

from typing import Dict
import numpy as np
import pandas as pd


def chip_distribution(df: pd.DataFrame, lookback: int = 120,
                      grid: int = 300, half_life: int = 60) -> Dict:
    """估算筹码分布。lookback 取最近N根;grid 价格网格数;half_life 衰减半衰期(根)。"""
    if df is None or len(df) < 20:
        return {'available': False, 'reason': '数据不足'}
    d = df.copy()
    ren = {c: c.lower() for c in d.columns if c.lower() in ('open', 'high', 'low', 'close', 'volume')}
    d = d.rename(columns=ren)
    for c in ('high', 'low', 'close', 'volume'):
        if c not in d.columns:
            return {'available': False, 'reason': f'缺列 {c}'}
        d[c] = pd.to_numeric(d[c], errors='coerce')
    d = d.dropna(subset=['high', 'low', 'close', 'volume']).tail(lookback).reset_index(drop=True)
    if len(d) < 20:
        return {'available': False, 'reason': '有效数据不足'}

    lo, hi = float(d['low'].min()), float(d['high'].max())
    if hi <= lo:
        return {'available': False, 'reason': '价格无波动'}
    edges = np.linspace(lo, hi, grid + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    chips = np.zeros(grid)

    n = len(d)
    lows = d['low'].values
    highs = d['high'].values
    vols = d['volume'].values
    for i in range(n):
        age = n - 1 - i                       # 0=最新
        w = vols[i] * (0.5 ** (age / half_life))   # 越老权重越小
        if w <= 0 or highs[i] <= lows[i]:
            # 单一价位,落到最近网格
            idx = int(np.clip(np.searchsorted(edges, lows[i]) - 1, 0, grid - 1))
            chips[idx] += vols[i] * (0.5 ** (age / half_life))
            continue
        # 在 [low,high] 区间均匀分摊
        lo_idx = int(np.clip(np.searchsorted(edges, lows[i]) - 1, 0, grid - 1))
        hi_idx = int(np.clip(np.searchsorted(edges, highs[i]) - 1, 0, grid - 1))
        span = hi_idx - lo_idx + 1
        chips[lo_idx:hi_idx + 1] += w / span

    total = chips.sum()
    if total <= 0:
        return {'available': False, 'reason': '筹码累计为0'}
    cur = float(d['close'].iloc[-1])

    profit_ratio = float(chips[centers <= cur].sum() / total * 100)     # 获利盘%
    avg_cost = float((centers * chips).sum() / total)                    # 平均成本

    # 中心 90% / 70% 成本区间(累积分布的 5%-95% / 15%-85%)
    order = np.argsort(centers)
    cum = np.cumsum(chips[order]) / total
    cp = centers[order]

    def _range(pl, ph):
        return float(cp[np.searchsorted(cum, pl)]), float(cp[min(np.searchsorted(cum, ph), grid - 1)])

    c90_lo, c90_hi = _range(0.05, 0.95)
    c70_lo, c70_hi = _range(0.15, 0.85)
    concentration_90 = round((c90_hi - c90_lo) / avg_cost * 100, 1) if avg_cost else None  # 越小越集中

    return {
        'available': True,
        'current_price': round(cur, 2),
        'profit_ratio_pct': round(profit_ratio, 1),          # 获利盘比例
        'avg_cost': round(avg_cost, 2),                      # 市场平均成本
        'cost_90_low': round(c90_lo, 2), 'cost_90_high': round(c90_hi, 2),
        'cost_70_low': round(c70_lo, 2), 'cost_70_high': round(c70_hi, 2),
        'concentration_90_pct': concentration_90,            # 90%筹码区间宽度/均价(越小越集中)
        'price_vs_avg_cost_pct': round((cur - avg_cost) / avg_cost * 100, 1) if avg_cost else None,
        'summary': (f"获利盘{profit_ratio:.0f}%,均价{avg_cost:.2f}"
                    f"({'套牢' if cur < avg_cost else '获利'}{abs(cur-avg_cost)/avg_cost*100:.0f}%),"
                    f"90%筹码[{c90_lo:.2f}~{c90_hi:.2f}],"
                    f"{'高度集中' if (concentration_90 or 99) < 15 else ('集中' if (concentration_90 or 99) < 30 else '发散')}"),
    }


if __name__ == '__main__':
    import sys, os, io
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    import _bootstrap  # noqa
    from stock_data import StockDataFetcher
    df = StockDataFetcher().get_stock_data('600519', '1y')
    import json
    print(json.dumps(chip_distribution(df), ensure_ascii=False, indent=2))

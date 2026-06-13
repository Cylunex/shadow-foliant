# -*- coding: utf-8 -*-
"""组合蒙特卡洛预测 —— 借鉴 wealthfolio simulations。

基于当前持仓的**加权历史日收益分布**,bootstrap 重采样模拟未来 H 个交易日的组合价值路径,
给出分位区间(p5/p25/中位/p75/p95)、期望、亏损概率、在险价值(VaR)。回答"按当前组合,
未来一段时间大概率落在什么区间、最坏可能亏多少"。

数据:持仓(portfolio_db)+ 各股 K线(datahub.kline,已磁盘缓存)。取 top-N 持仓(按市值)
控制K线拉取量。纯 numpy。失败返回 {error}。
"""
from __future__ import annotations

from typing import Dict, List, Optional

import _bootstrap  # noqa: F401

import numpy as np


def _portfolio_daily_returns(top_n: int = 20, period: str = "6mo"):
    """构造组合加权日收益序列。返回 (returns_array, total_mv, used_n) 或 (None, 0, 0)。"""
    import datahub
    try:
        from portfolio_db import portfolio_db
        holds = [h for h in (portfolio_db.get_all_stocks() or [])
                 if h.get("code") and float(h.get("quantity") or 0) > 0]
    except Exception:
        return None, 0, 0
    if not holds:
        return None, 0, 0
    # 权重用成本市值(qty×cost),免去为 68 只拉实时 quotes 的耗时;K线收益才是模拟主体
    enriched = []
    for h in holds:
        code = str(h["code"])
        qty = float(h.get("quantity") or 0)
        cost = float(h.get("cost_price") or h.get("cost") or 0)
        mv = qty * cost
        if mv > 0:
            enriched.append((code, mv))
    if not enriched:
        return None, 0, 0
    enriched.sort(key=lambda x: x[1], reverse=True)
    enriched = enriched[:top_n]
    total_mv = sum(mv for _, mv in enriched)

    # 各股日收益对齐
    series = {}
    for code, mv in enriched:
        try:
            df = datahub.kline(code, period)
            if df is None or len(df) < 40:
                continue
            col = next((c for c in ("close", "Close", "收盘") if c in df.columns), None)
            if not col:
                continue
            closes = df[col].astype(float).values
            series[code] = (np.diff(closes) / closes[:-1], mv)
        except Exception:
            continue
    if len(series) < 1:
        return None, total_mv, 0
    n = min(len(v[0]) for v in series.values())
    w_total = sum(mv for _, mv in series.values())
    port_ret = np.zeros(n)
    for code, (rets, mv) in series.items():
        port_ret += rets[-n:] * (mv / w_total)
    return port_ret, total_mv, len(series)


def simulate(horizon: int = 60, n_sims: int = 5000, top_n: int = 20, period: str = "6mo",
             seed: Optional[int] = 42) -> dict:
    """蒙特卡洛模拟。horizon=未来交易日数,n_sims=路径数。
    返回 {start_value, horizon, percentiles{p5,p25,p50,p75,p95}, expected, prob_loss_pct,
    var95_pct, ret_p5_pct, ret_p95_pct, used_n}。"""
    rets, total_mv, used = _portfolio_daily_returns(top_n, period)
    if rets is None or len(rets) < 30 or total_mv <= 0:
        return {"error": "持仓/历史数据不足,无法模拟", "used_n": used}
    rng = np.random.default_rng(seed)
    # bootstrap:每条路径从历史日收益有放回抽样 horizon 个,累乘
    idx = rng.integers(0, len(rets), size=(n_sims, horizon))
    paths = rets[idx]                       # (n_sims, horizon)
    cum = np.prod(1 + paths, axis=1)        # 每条路径的累计收益倍数
    end_vals = total_mv * cum
    pct = lambda q: round(float(np.percentile(end_vals, q)), 0)
    ret_pct = lambda q: round(float(np.percentile(cum, q) - 1) * 100, 1)
    return {
        "start_value": round(total_mv, 0), "horizon": horizon, "n_sims": n_sims, "used_n": used,
        "percentiles": {"p5": pct(5), "p25": pct(25), "p50": pct(50), "p75": pct(75), "p95": pct(95)},
        "expected": round(float(end_vals.mean()), 0),
        "prob_loss_pct": round(float((end_vals < total_mv).mean()) * 100, 1),
        "var95_pct": ret_pct(5),            # 5% 分位收益(最坏5%情形的收益率)
        "ret_p5_pct": ret_pct(5), "ret_p50_pct": ret_pct(50), "ret_p95_pct": ret_pct(95),
    }


def format_text(s: dict) -> str:
    if s.get("error"):
        return f"组合模拟:{s['error']}"
    p = s["percentiles"]
    return (f"🎲 组合蒙特卡洛(未来{s['horizon']}交易日·{s['n_sims']}次,{s['used_n']}只重仓)\n"
            f"  起始 {s['start_value']:,.0f} 元\n"
            f"  中位 {p['p50']:,.0f}({s['ret_p50_pct']:+.1f}%) · "
            f"区间[{p['p5']:,.0f}~{p['p95']:,.0f}]({s['ret_p5_pct']:+.1f}%~{s['ret_p95_pct']:+.1f}%)\n"
            f"  亏损概率 {s['prob_loss_pct']}% · 最坏5%(VaR95) {s['var95_pct']:+.1f}%")


if __name__ == "__main__":
    import io
    import os
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_bootstrap.ROOT, ".env"))
    except Exception:
        pass
    print("=== 组合蒙特卡洛自检 ===")
    print(format_text(simulate(horizon=60, n_sims=5000)))

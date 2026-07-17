# -*- coding: utf-8 -*-
"""组合 vs 基准对比 —— 借鉴 ghostfolio benchmark。

把组合的累计收益曲线(时间加权,剔除出入金)与基准指数(默认沪深300)在同一时间窗对齐,
算超额收益(alpha)。供 webui 画双线 + 周报一行。

数据:组合净值快照(performance 的 TWR 链)+ 指数K线(datahub.kline → akshare 兜底)。
失败返回 {error}/空,不抛异常。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import _bootstrap  # noqa: F401

# 常用基准(代码: 显示名)
BENCHMARKS = {
    "000300": "沪深300",
    "000905": "中证500",
    "000852": "中证1000",
    "399006": "创业板指",
    "000016": "上证50",
}


def _d(s) -> str:
    return str(s or "")[:10]


def _portfolio_cum_series() -> List[Tuple[str, float]]:
    """组合累计收益序列 [(date, cum_return_pct)],基于净值快照的 TWR 子区间链,首日=0。"""
    from portfolio_snapshot import get_snapshots
    from performance import _flows_from_trades
    snaps = sorted(get_snapshots(limit=730) or [], key=lambda s: _d(s.get("snap_date")))
    eq = [(_d(s.get("snap_date")), float(s.get("total_mv") or 0)) for s in snaps if (s.get("total_mv") or 0) > 0]
    if len(eq) < 2:
        return []
    flows, _ = _flows_from_trades()
    out = [(eq[0][0], 0.0)]
    cum = 1.0
    for i in range(1, len(eq)):
        mv_prev, mv_cur = eq[i - 1][1], eq[i][1]
        # flow 累计 (d_prev, d_cur] 区间全部成交(与 performance.twr 同修,2026-07-17):
        # 快照缺日时中段买卖原来匹配不上、被算进收益
        flow = sum(float(v) for d, v in flows.items() if eq[i - 1][0] < d <= eq[i][0])
        r = (mv_cur - mv_prev - flow) / mv_prev if mv_prev > 0 else 0.0
        if -0.5 < r < 0.5:
            cum *= (1 + r)
        out.append((eq[i][0], round((cum - 1) * 100, 2)))
    return out


def _index_prefix(code: str) -> str:
    # 指数代码前缀:沪市指数 000xxx/沪50等用 sh,深市 399xxx 用 sz
    return "sz" if code.startswith("399") else "sh"


def _index_close_map(code: str, start: str) -> Dict[str, float]:
    """指数日收盘 {date: close},start 之后。用 akshare 指数专用接口(指数≠个股,不能走 kline)。
    源:stock_zh_index_daily(新浪) → index_zh_a_hist(东财) 兜底。"""
    sym = _index_prefix(code) + code

    def _sina():
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol=sym)
        if df is None or df.empty:
            return {}
        return {_d(r.get("date")): float(r.get("close")) for _, r in df.iterrows() if _d(r.get("date")) >= start}

    def _em():
        import akshare as ak
        df = ak.index_zh_a_hist(symbol=code, period="daily", start_date=start.replace("-", ""))
        if df is None or df.empty:
            return {}
        return {_d(r.get("日期")): float(r.get("收盘")) for _, r in df.iterrows()}

    for fn in (_sina, _em):
        try:
            m = fn()
            if m:
                return m
        except Exception:
            continue
    return {}


def compare(benchmark_code: str = "000300") -> dict:
    """组合 vs 基准。返回 {dates, portfolio:[cum%], benchmark:[cum%], benchmark_name,
    portfolio_return_pct, benchmark_return_pct, excess_pct} 或 {error}。"""
    port = _portfolio_cum_series()
    if len(port) < 2:
        return {"error": "净值快照不足(需盘后快照积累几日)", "dates": []}
    start = port[0][0]
    idx_map = _index_close_map(benchmark_code, start)
    name = BENCHMARKS.get(benchmark_code, benchmark_code)
    dates = [d for d, _ in port]
    port_cum = [v for _, v in port]

    bench_cum = []
    base_close = None
    if idx_map:
        # 用首个快照日(或之后最近交易日)的指数收盘为基准
        sorted_idx_dates = sorted(idx_map)
        base_close = next((idx_map[d] for d in sorted_idx_dates if d >= start), None)
    for d in dates:
        if base_close and idx_map:
            # 取 ≤d 的最近指数收盘
            close = None
            for id_ in sorted(idx_map):
                if id_ <= d:
                    close = idx_map[id_]
                else:
                    break
            bench_cum.append(round((close / base_close - 1) * 100, 2) if close else None)
        else:
            bench_cum.append(None)

    pr = port_cum[-1]
    br = next((b for b in reversed(bench_cum) if b is not None), None)
    excess = round(pr - br, 2) if br is not None else None
    return {"dates": dates, "portfolio": port_cum, "benchmark": bench_cum,
            "benchmark_code": benchmark_code, "benchmark_name": name,
            "portfolio_return_pct": pr, "benchmark_return_pct": br, "excess_pct": excess}


def format_text(c: dict) -> str:
    if not c or c.get("error") or not c.get("dates"):
        return ""
    s = f"📊 vs {c['benchmark_name']}: 组合 {c['portfolio_return_pct']:+.2f}%"
    if c.get("benchmark_return_pct") is not None:
        s += f" / 基准 {c['benchmark_return_pct']:+.2f}% → 超额 {c['excess_pct']:+.2f}%"
    return s


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
    print("=== 基准对比自检 ===")
    c = compare("000300")
    print(c.get("error") or format_text(c))
    print(f"dates={len(c.get('dates', []))} bench命中={sum(1 for b in c.get('benchmark', []) if b is not None)}")

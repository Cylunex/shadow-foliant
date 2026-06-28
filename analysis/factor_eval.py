# -*- coding: utf-8 -*-
"""因子严格评估 —— 借鉴 Vibe-Trading bench_runner_strict。

科学衡量因子是否真有预测力,防"看起来能赚其实是β/过拟合":
  · IC(信息系数):每个调仓日,横截面上"因子值 vs 未来N日收益"的相关系数(Pearson)
  · Rank-IC:Spearman 秩相关(更稳健,抗异常值)—— 量化界主用
  · IC-IR:mean(IC)/std(IC),衡量稳定性(>0.3 算不错,>0.5 很好)
  · 胜率:IC>0 的调仓日占比
  · 随机对照:把因子值打乱后重算 IC,真因子应远高于随机(否则是噪声)
  · 方向校正:按因子声明方向(±1)调正,正 IC = 该方向有效

数据:对股池每只取一次 K线(datahub.kline),在多个历史调仓点算因子值+未来收益,
横截面聚合。纯 numpy/pandas。股池默认沪深300样本(可传)。慢→建议后台/缓存。
"""
from __future__ import annotations

import random
from typing import Dict, List, Optional

import _bootstrap  # noqa: F401

import numpy as np
import pandas as pd

# 默认评估股池(沪深300代表性样本,均衡行业;可被调用方覆盖)
DEFAULT_UNIVERSE = [
    "600519", "000858", "600036", "601318", "600276", "000333", "300750", "002594",
    "600900", "601012", "000651", "002415", "600030", "601888", "603259", "600887",
    "000001", "002304", "600309", "601166", "300059", "002230", "600585", "601899",
    "603501", "000725", "002475", "600031", "601668", "600048",
]


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    """Rank-IC:平均秩(tie-aware)后再算 Pearson。
    原自实现 argsort 把并列值强排成 0..n-1 的斜坡(且结果依赖输入行序)→ Rank-IC 失真甚至变号;
    pandas .rank() 默认 method='average' 对并列取平均秩,与 multi_factor_screener.factor_ic 口径一致。
    退化(常数)向量经 rank 后仍为常数,_pearson 的 std==0 分支会正确返回 nan(自动剔除噪声因子)。"""
    rx = pd.Series(x).rank().values
    ry = pd.Series(y).rank().values
    return _pearson(rx, ry)


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3:
        return np.nan
    sx, sy = x.std(), y.std()
    if sx == 0 or sy == 0:
        return np.nan
    return float(np.corrcoef(x, y)[0, 1])


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    from selection.instock_strategy_runner import _normalize_df as _n
    try:
        return _n(df)
    except Exception:
        return df


def evaluate(factor_keys: Optional[List[str]] = None, universe: Optional[List[str]] = None,
             horizon: int = 10, rebalance: int = 5, period: str = "2y",
             with_random: bool = True) -> dict:
    """评估因子。返回 {factors:[{key,name,category,direction,ic_mean,rank_ic,ic_ir,win_rate,
       n_periods,random_ic,verdict}], horizon, universe_n, as_of}。"""
    import datahub
    from factor_zoo import FACTORS, compute
    keys = [k for k in (factor_keys or list(FACTORS)) if k in FACTORS]
    uni = universe or DEFAULT_UNIVERSE

    # 1) 每只取一次 K线,算所有因子序列 + 未来收益序列(close[t+h]/close[t]-1)
    panel = {}   # code -> {'fwd': Series, 'factors': {key: Series}, 'dates': Index}
    for code in uni:
        try:
            df = _normalize_df(datahub.kline(code, period, adjust='qfq'))  # 因子用前复权
            if df is None or len(df) < 120:
                continue
            close = df["close"].astype(float).reset_index(drop=True)
            fwd = close.shift(-horizon) / close - 1
            fac = compute(df.reset_index(drop=True), keys)
            if fac:
                panel[code] = {"fwd": fwd, "factors": fac, "n": len(close)}
        except Exception:
            continue
    if len(panel) < 5:
        return {"error": f"有效股池过小({len(panel)}),无法评估", "factors": []}

    # 2) 选调仓点(各股长度可能不同 → 用最短长度的网格,留出 horizon)
    min_len = min(p["n"] for p in panel.values())
    points = list(range(60, min_len - horizon, rebalance))
    if len(points) < 5:
        return {"error": "历史调仓点不足", "factors": []}

    # 3) 逐因子:每个调仓点算横截面 IC / Rank-IC
    results = []
    for key in keys:
        name, cat, direction, _ = FACTORS[key]
        ics, ricks, rand_ics = [], [], []
        for t in points:
            fvals, rets = [], []
            for code, p in panel.items():
                fser = p["factors"].get(key)
                if fser is None or t >= len(fser):
                    continue
                fv = fser.iloc[t]
                rv = p["fwd"].iloc[t]
                if pd.notna(fv) and pd.notna(rv):
                    fvals.append(float(fv)); rets.append(float(rv))
            if len(fvals) >= 5:
                x, y = np.array(fvals), np.array(rets)
                ic = _pearson(x, y)
                ric = _spearman(x, y)
                if not np.isnan(ic):
                    ics.append(ic * direction)        # 方向校正
                    ricks.append(ric * direction)
                if with_random and not np.isnan(ic):
                    xs = x.copy(); np.random.shuffle(xs)
                    r = _pearson(xs, y)
                    if not np.isnan(r):
                        rand_ics.append(r * direction)
        if len(ics) < 5:
            continue
        ic_mean = float(np.mean(ics))
        ic_std = float(np.std(ics)) or 1e-9
        ic_ir = round(ic_mean / ic_std, 2)
        rank_ic = round(float(np.mean(ricks)), 3)
        win = round(sum(1 for v in ics if v > 0) / len(ics) * 100, 1)
        rand_ic = round(float(np.mean(rand_ics)), 3) if rand_ics else None
        # 判定:IC-IR + Rank-IC 综合;且需显著高于随机
        strong = abs(ic_ir) >= 0.3 and abs(rank_ic) >= 0.02
        beats_random = rand_ic is None or abs(rank_ic) > abs(rand_ic) * 2
        verdict = ("✅有效" if (strong and beats_random) else
                   ("⚠️弱" if abs(rank_ic) >= 0.015 and beats_random else "❌噪声"))
        results.append({
            "key": key, "name": name, "category": cat, "direction": direction,
            "ic_mean": round(ic_mean, 3), "rank_ic": rank_ic, "ic_ir": ic_ir,
            "win_rate": win, "n_periods": len(ics), "random_ic": rand_ic, "verdict": verdict,
        })
    results.sort(key=lambda r: abs(r["ic_ir"]), reverse=True)
    return {"factors": results, "horizon": horizon, "rebalance": rebalance,
            "universe_n": len(panel), "n_points": len(points), "period": period}


def format_text(rep: dict, top_n: int = 8) -> str:
    if rep.get("error"):
        return f"因子评估:{rep['error']}"
    L = [f"🔬 因子IC评估(股池{rep['universe_n']}只·未来{rep['horizon']}日·{rep['n_points']}个调仓点)"]
    for r in rep["factors"][:top_n]:
        L.append(f"  {r['verdict']} {r['name']}({r['category']}): "
                 f"RankIC {r['rank_ic']:+.3f} · IC-IR {r['ic_ir']:+.2f} · 胜率{r['win_rate']}%"
                 + (f" · 随机{r['random_ic']:+.3f}" if r['random_ic'] is not None else ""))
    return "\n".join(L)


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
    print("=== 因子IC评估自检(股池较小/较慢)===")
    print(format_text(evaluate(horizon=10, rebalance=10, period="1y")))

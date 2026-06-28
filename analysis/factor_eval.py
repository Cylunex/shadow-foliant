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

# 默认评估股池(沪深300/中证500 代表性样本,跨行业 ~52 只;可被调用方覆盖)。
# 注:扩到 ~50 只是为压低单期横截面 IC 的噪声(n=30 时 null IC 标准差 ~0.19,n=52 时 ~0.14);
# 仍是当前成分快照(有幸存者/成分偏差),理想态是 point-in-time 历史成分——留作后续数据接入。
DEFAULT_UNIVERSE = [
    "600519", "000858", "600036", "601318", "600276", "000333", "300750", "002594",
    "600900", "601012", "000651", "002415", "600030", "601888", "603259", "600887",
    "000001", "002304", "600309", "601166", "300059", "002230", "600585", "601899",
    "603501", "000725", "002475", "600031", "601668", "600048",
    # 扩充(跨行业大/中盘,降低单期截面噪声)
    "600104", "600028", "601857", "600050", "000063", "600690", "000568", "603288",
    "600009", "601985", "600438", "002714", "300760", "000538", "601628", "601398",
    "601288", "600000", "600406", "002352", "601766", "600547",
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


def _bh_fdr(pvals: List[Optional[float]], q: float = 0.10) -> List[bool]:
    """Benjamini-Hochberg 多重比较校正:14 因子同测时控制错误发现率 FDR≤q。
    返回与 pvals 等长的 bool(该因子是否在 FDR=q 下显著);None 视为不显著。"""
    idx = [i for i, p in enumerate(pvals) if p is not None]
    m = len(idx)
    out = [False] * len(pvals)
    if m == 0:
        return out
    ordered = sorted(idx, key=lambda i: pvals[i])
    kmax = 0
    for rank, i in enumerate(ordered, start=1):
        if pvals[i] <= rank / m * q:
            kmax = rank
    for rank, i in enumerate(ordered, start=1):
        if rank <= kmax:
            out[i] = True
    return out


def _perm_pvalue(period_ranks, direction: int, obs_mean: float, n_perm: int = 200):
    """置换检验(取代原"单次洗牌×2"的伪显著性门):每期独立 shuffle 因子秩、与收益秩算相关,
    跨期平均 → 构造 mean-RankIC 的 null 分布;p = P(|null| ≥ |观测|)。
    period_ranks: [(rx, ry), ...] 各期 因子秩/收益秩(numpy,tie-aware)。
    返回 (p_value 双侧, noise_p95=null 的 95% 分位 |值|)。样本不足返回 (None, None)。"""
    per_period = []
    for rx, ry in period_ranks:
        m = len(rx)
        if m < 3:
            continue
        sx, sy = rx.std(), ry.std()
        if sx == 0 or sy == 0:
            continue
        per_period.append((rx, ry - ry.mean(), m * sx * sy))
    if len(per_period) < 5:
        return (None, None)
    null_means = np.zeros(n_perm)
    for rx, ry_c, denom in per_period:
        m = len(rx)
        idx = np.argsort(np.random.rand(n_perm, m), axis=1)   # N 个独立置换
        corrs = (rx[idx] @ ry_c) / denom * direction          # 每行均为 rx 的置换,均值/方差不变
        null_means += corrs
    null_means /= len(per_period)
    p = (1 + int(np.sum(np.abs(null_means) >= abs(obs_mean)))) / (n_perm + 1)
    return (round(p, 4), round(float(np.percentile(np.abs(null_means), 95)), 4))


MIN_CROSS = 15   # 单期横截面最少有效股票数(原 5 太小,单期 IC 噪声极大)
FDR_Q = 0.10     # Benjamini-Hochberg 目标错误发现率


def evaluate(factor_keys: Optional[List[str]] = None, universe: Optional[List[str]] = None,
             horizon: int = 10, rebalance: Optional[int] = None, period: str = "2y",
             with_random: bool = True, n_perm: int = 200) -> dict:
    """评估因子(2026-06-28 统计加固)。返回 {factors:[{key,name,category,direction,ic_mean,
       rank_ic,ic_ir,win_rate,n_periods,random_ic(=噪声p95),p_value,fdr_significant,verdict}],
       horizon, rebalance, universe_n, n_points, period, neutralized, fdr_q}。

    加固点:
      - 非重叠调仓:rebalance 缺省=horizon,相邻期未来收益不再 50% 重叠 → IC-IR 不再被自相关高估;
      - 置换检验:每因子 n_perm 次整段打乱构造 null 分布得 p_value(取代原"比单次洗牌×2"的伪门);
      - 多重比较:14 因子 p 值做 BH-FDR(q=FDR_Q),fdr_significant 才判✅;
      - 横截面下限 MIN_CROSS=15、股池扩到 ~50 → 降单期 IC 噪声。
      ⚠️ 仍**未做行业/市值/Beta 中性化**(neutralized=False):动量/位置类 IC 仍含 β/规模暴露,
         不能据此声称纯 alpha——需接入行业/市值面板,留作后续。"""
    import datahub
    from factor_zoo import FACTORS, compute
    keys = [k for k in (factor_keys or list(FACTORS)) if k in FACTORS]
    uni = universe or DEFAULT_UNIVERSE
    rebalance = rebalance or horizon   # 非重叠:步长≥持有期,消除样本重叠对 IR 的高估

    # 1) 每只取一次 K线,算所有因子序列 + 未来收益序列(close[t+h]/close[t]-1)
    panel = {}
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

    # 3) 逐因子:每个调仓点算横截面 IC / Rank-IC,并收集逐期秩用于置换检验
    results = []
    for key in keys:
        name, cat, direction, _ = FACTORS[key]
        ics, ricks, period_ranks = [], [], []
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
            if len(fvals) >= MIN_CROSS:
                x, y = np.array(fvals), np.array(rets)
                ic = _pearson(x, y)
                if not np.isnan(ic):
                    rx = pd.Series(x).rank().values
                    ry = pd.Series(y).rank().values
                    ric = _pearson(rx, ry)
                    if not np.isnan(ric):
                        ics.append(ic * direction)        # 方向校正
                        ricks.append(ric * direction)
                        period_ranks.append((rx, ry))
        if len(ics) < 5:
            continue
        ic_mean = float(np.mean(ics))
        ic_std = float(np.std(ics)) or 1e-9
        ic_ir = round(ic_mean / ic_std, 2)
        rank_ic = round(float(np.mean(ricks)), 3)
        win = round(sum(1 for v in ics if v > 0) / len(ics) * 100, 1)
        p_value, noise_p95 = (_perm_pvalue(period_ranks, direction, float(np.mean(ricks)), n_perm)
                              if with_random else (None, None))
        results.append({
            "key": key, "name": name, "category": cat, "direction": direction,
            "ic_mean": round(ic_mean, 3), "rank_ic": rank_ic, "ic_ir": ic_ir,
            "win_rate": win, "n_periods": len(ics), "random_ic": noise_p95,
            "p_value": p_value, "fdr_significant": False, "verdict": "❌噪声",
        })

    if not results:
        return {"error": f"无因子可评估(单期有效横截面 < {MIN_CROSS} 只或有效调仓点 < 5)",
                "factors": []}

    # 4) 多重比较 BH-FDR + 终判(FDR 显著 + 效应量下限)
    sig = _bh_fdr([r["p_value"] for r in results], q=FDR_Q)
    for r, s in zip(results, sig):
        r["fdr_significant"] = bool(s)
        ar = abs(r["rank_ic"])
        pv = r["p_value"]
        if s and ar >= 0.02:
            r["verdict"] = "✅有效"
        elif (pv is not None and pv < 0.10 and ar >= 0.015):
            r["verdict"] = "⚠️弱"
        else:
            r["verdict"] = "❌噪声"

    results.sort(key=lambda r: (r["fdr_significant"], abs(r["ic_ir"])), reverse=True)
    return {"factors": results, "horizon": horizon, "rebalance": rebalance,
            "universe_n": len(panel), "n_points": len(points), "period": period,
            "neutralized": False, "fdr_q": FDR_Q, "n_perm": n_perm}


def format_text(rep: dict, top_n: int = 8) -> str:
    if rep.get("error"):
        return f"因子评估:{rep['error']}"
    L = [f"🔬 因子IC评估(股池{rep['universe_n']}只·未来{rep['horizon']}日·{rep['n_points']}个非重叠调仓点·"
         f"置换检验+FDR{rep.get('fdr_q', 0.1)})"]
    for r in rep["factors"][:top_n]:
        pv = r.get("p_value")
        L.append(f"  {r['verdict']} {r['name']}({r['category']}): "
                 f"RankIC {r['rank_ic']:+.3f} · IC-IR {r['ic_ir']:+.2f} · 胜率{r['win_rate']}%"
                 + (f" · p={pv}" if pv is not None else "")
                 + (" · FDR✓" if r.get("fdr_significant") else ""))
    if rep.get("neutralized") is False:
        L.append("  ⚠️ 未做行业/市值中性化:动量/位置类 IC 仍含 β/规模暴露,非纯 alpha。")
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

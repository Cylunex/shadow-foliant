# -*- coding: utf-8 -*-
"""组合权重优化器 —— 借鉴 Vibe-Trading optimizers。

选完股(综合选股/多因子/策略命中)后,给出**怎么配比**。shadow-foliant 原仓位层只做约束
(单票上限/集中度),这里补"最优权重"。纯 numpy(无 scipy 依赖),基于历史日收益协方差:

  · equal           等权(基准)
  · inverse_vol     逆波动率(波动大的少配)—— 风险平价近似,稳健常用
  · min_variance    最小方差(解析解 w ∝ Σ⁻¹·1,带非负+归一)
  · risk_parity     风险平价(迭代,各标的风险贡献相等)

输入:codes(可带 name)+ 历史窗口;数据走 datahub.kline。失败/数据不足的标的自动剔除。
返回 {method: {code: weight%}, used, dropped, period, cov_days}。绝不抛异常。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import _bootstrap  # noqa: F401

import numpy as np


def _returns_matrix(codes: List[str], period: str = "6mo") -> Tuple[np.ndarray, List[str]]:
    """取各 code 的日收益,对齐成矩阵(行=日,列=标的)。返回 (R, used_codes)。"""
    import datahub
    series = {}
    for c in codes:
        try:
            df = datahub.kline(c, period)
            if df is None or len(df) < 30:
                continue
            # 收盘列名兼容多源:close/Close/收盘
            col = next((k for k in ("close", "Close", "收盘") if k in df.columns), None)
            if col is None:
                continue
            closes = df[col].astype(float).values
            rets = np.diff(closes) / closes[:-1]
            if len(rets) >= 20:
                series[c] = rets
        except Exception:
            continue
    if len(series) < 2:
        return np.empty((0, 0)), list(series)
    # 对齐到最短长度(尾部对齐,用最近 N 日)
    n = min(len(v) for v in series.values())
    used = list(series)
    R = np.column_stack([series[c][-n:] for c in used])
    return R, used


def _normalize(w: np.ndarray) -> np.ndarray:
    w = np.clip(w, 0, None)
    s = w.sum()
    return w / s if s > 0 else np.ones_like(w) / len(w)


def _inverse_vol(R: np.ndarray) -> np.ndarray:
    vol = R.std(axis=0)
    vol[vol == 0] = vol[vol > 0].mean() if (vol > 0).any() else 1.0
    return _normalize(1.0 / vol)


def _min_variance(R: np.ndarray) -> np.ndarray:
    cov = np.cov(R, rowvar=False)
    try:
        inv = np.linalg.pinv(cov)
        w = inv.sum(axis=1)
        return _normalize(w)
    except Exception:
        return _inverse_vol(R)


def _risk_parity(R: np.ndarray, iters: int = 500) -> np.ndarray:
    """迭代风险平价:各标的对组合风险贡献相等。"""
    cov = np.cov(R, rowvar=False)
    n = cov.shape[0]
    w = np.ones(n) / n
    for _ in range(iters):
        port_var = w @ cov @ w
        if port_var <= 0:
            break
        mrc = cov @ w                  # 边际风险贡献
        rc = w * mrc                   # 风险贡献
        target = port_var / n
        w = w * (target / np.maximum(rc, 1e-12)) ** 0.5
        w = _normalize(w)
    return _normalize(w)


def optimize(codes: List[str], period: str = "6mo",
             methods: Optional[List[str]] = None,
             names: Optional[Dict[str, str]] = None) -> dict:
    """对 codes 计算各法权重。methods 缺省全算。"""
    codes = [str(c).strip() for c in (codes or []) if c]
    if len(codes) < 2:
        return {"error": "至少需要 2 只标的", "used": [], "dropped": codes}
    methods = methods or ["equal", "inverse_vol", "min_variance", "risk_parity"]
    R, used = _returns_matrix(codes, period)
    dropped = [c for c in codes if c not in used]
    if R.size == 0 or len(used) < 2:
        return {"error": "有效历史数据不足(标的剔除后<2)", "used": used, "dropped": dropped}

    calc = {
        "equal": lambda: np.ones(len(used)) / len(used),
        "inverse_vol": lambda: _inverse_vol(R),
        "min_variance": lambda: _min_variance(R),
        "risk_parity": lambda: _risk_parity(R),
    }
    out = {"used": used, "dropped": dropped, "period": period, "cov_days": R.shape[0],
           "names": names or {}, "weights": {}}
    for m in methods:
        if m not in calc:
            continue
        try:
            w = calc[m]()
            out["weights"][m] = {used[i]: round(float(w[i]) * 100, 1) for i in range(len(used))}
        except Exception as e:
            out["weights"][m] = {"_error": str(e)}
    # 附组合层面指标(用风险平价权重举例)
    try:
        cov = np.cov(R, rowvar=False)
        for m, wd in out["weights"].items():
            if "_error" in wd:
                continue
            w = np.array([wd[c] / 100 for c in used])
            pv = float(w @ cov @ w)
            out.setdefault("portfolio_vol_pct", {})[m] = round(float(np.sqrt(pv) * np.sqrt(244) * 100), 1)
    except Exception:
        pass
    return out


def format_text(o: dict, method: str = "risk_parity") -> str:
    if o.get("error"):
        return f"组合优化:{o['error']}"
    w = (o.get("weights") or {}).get(method, {})
    if not w or "_error" in w:
        return f"组合优化({method}):无结果"
    names = o.get("names") or {}
    items = sorted(w.items(), key=lambda x: -x[1])
    L = [f"⚖️ 建议配比({method},{len(o['used'])}只,{o['cov_days']}日协方差)"]
    for code, pct in items:
        L.append(f"  {names.get(code, code)} {code}: {pct}%")
    vol = (o.get("portfolio_vol_pct") or {}).get(method)
    if vol is not None:
        L.append(f"  组合年化波动≈{vol}%")
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
    print("=== 组合优化器自检 ===")
    o = optimize(["600519", "000001", "300750", "600036"], "6mo")
    print(format_text(o, "risk_parity"))
    print(format_text(o, "inverse_vol"))

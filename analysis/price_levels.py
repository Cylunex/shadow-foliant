"""关键价位计算 —— 纯函数,无 IO / 无存储 / 零新依赖(pandas/numpy)。

借鉴 tickflow-stock-panel 的 11 类关键价位思路,按本项目栈(pandas + 大写 OHLCV)重写。
原版是 Polars + enriched 列;此处直接吃 datahub.kline 的 OHLCV,内部自算所需指标
(MA20/60/120 / ATR14 / BOLL),不依赖外部 enriched 表,也不引入 Polars。

输入: datahub.kline(code, ..., adjust='qfq') 返回的日 K DataFrame(DatetimeIndex +
      大写列 Open/High/Low/Close/Volume)。技术价位用前复权口径(除权跳空会毁形态)。
输出: 11 类结构化价位点 {分组key: [{value,label,type,side,strength,rank?}...]},供:
  - 个股分析页图表画水平价格线(前端按 type 分组显隐)
  - AI 技术分析 prompt 的"关键技术位"上下文(summarize_levels 紧凑文本)

设计原则(沿用原版):纯函数 + 向量化,毫秒级;NaN/Inf 全过滤,空数据返回空列表,不抛异常。
"""
from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np
import pandas as pd

# 价位分组 → 中文标签。前端按这个 type 显隐。
LEVEL_TYPES = {
    "sr": "压力支撑",          # 成交密集区(Volume Profile POC + 高成交密集区)
    "pivot": "枢轴点",          # 经典 Pivot P/R/S
    "extreme": "前高前低",      # 60/250 日极值 + 近期 swing 高低点
    "boll": "布林带",           # MA20 ± 2σ,标准差波动带(参考性,非真实支撑压力)
    "keltner_s": "Keltner短期",  # MA20 ± 2×ATR
    "keltner_m": "Keltner中期",  # MA60 ± 2.5×ATR
    "keltner_l": "Keltner长期",  # MA120 ± 3×ATR(牛熊趋势边界)
    "atr_stop": "ATR止损",      # close ± n×ATR 动态止盈止损
    "gap": "缺口位",            # 未回补跳空缺口
    "fib": "斐波那契",          # 回撤位 0.236~0.786
    "round": "整数关口",        # 心理整数位
}


# ================================================================
# 内部工具
# ================================================================

def _ok(v: Any) -> bool:
    """数值有效(非空/非 NaN/非 Inf/正数)。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f) and f > 0


def _side(level: float, close: float) -> str:
    """价位相对当前价的方向。"""
    if level > close * 1.001:
        return "resistance"
    if level < close * 0.999:
        return "support"
    return "neutral"


def _aggregate_levels(values: list[float], tol: float) -> list[float]:
    """把相近的价位聚合(±tol),返回去重后的代表值(保留更近期的)。"""
    if not values:
        return []
    values = sorted(values)
    out: list[float] = [values[0]]
    for v in values[1:]:
        if out[-1] > 0 and abs(v - out[-1]) / out[-1] <= tol:
            out[-1] = v  # 聚合到更新(更近期)的值
        else:
            out.append(v)
    return out


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    """补齐价位计算所需的辅助列(下划线前缀,非破坏性):MA20/60/120 / ATR14 / BOLL。

    本项目 datahub.kline 只给 OHLCV;此处自算所需指标,使模块对"是否已 enriched"无要求。
    ATR 优先用 MyTT(与项目其余 ATR 口径一致),不可用时退化为 TR 简单均值。
    """
    out = df.copy()
    close = out["Close"]
    out["_ma20"] = close.rolling(20).mean()
    out["_ma60"] = close.rolling(60).mean()
    out["_ma120"] = close.rolling(120).mean()

    # ATR(14)
    atr = None
    try:
        from MyTT import ATR as _ATR  # 项目自带,零额外依赖
        atr = pd.Series(_ATR(close.values, out["High"].values, out["Low"].values, N=14), index=out.index)
    except Exception:
        prev_c = close.shift(1)
        tr = pd.concat([
            out["High"] - out["Low"],
            (out["High"] - prev_c).abs(),
            (out["Low"] - prev_c).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
    out["_atr"] = atr

    # 布林带(20, 2σ)
    std20 = close.rolling(20).std()
    out["_bb_mid"] = out["_ma20"]
    out["_bb_up"] = out["_ma20"] + 2 * std20
    out["_bb_low"] = out["_ma20"] - 2 * std20
    return out


def _last(df: pd.DataFrame, col: str) -> Optional[float]:
    """取某列最后一个有效值。"""
    if col not in df.columns or df.empty:
        return None
    v = df[col].iloc[-1]
    return float(v) if _ok(v) else None


# ================================================================
# 1. 压力位 / 支撑位 —— 成交量分布 (Volume Profile)
# ================================================================

def _support_resistance(df: pd.DataFrame, bins: int = 40) -> list[dict]:
    """成交量分布(Volume Profile)—— 真正基于价+量的支撑/压力位。

    每根 K 的价格中点((H+L)/2)按价格分桶,以成交量加权累计,取高成交密集区:
      - POC(控制点):成交量最大的桶 → strong
      - 其他高于均值的高成交区:按量降序取 2 个 → medium
    """
    if df.empty or "Volume" not in df.columns or len(df) < 20:
        return []
    hi = float(df["High"].max())
    lo = float(df["Low"].min())
    if not (hi > lo > 0):
        return []
    mid = ((df["High"] + df["Low"]) / 2).to_numpy()
    vol = df["Volume"].to_numpy(dtype=float)
    mask = np.isfinite(mid) & np.isfinite(vol)
    mid, vol = mid[mask], vol[mask]
    if mid.size == 0 or vol.sum() <= 0:
        return []

    edges = np.linspace(lo, hi, bins + 1)
    hist, _ = np.histogram(mid, bins=edges, weights=vol)
    if hist.sum() <= 0:
        return []
    centers = (edges[:-1] + edges[1:]) / 2
    mean_vol = float(hist.mean())
    close = float(df["Close"].iloc[-1])

    out: list[dict] = []
    poc = int(np.argmax(hist))
    out.append({"value": round(float(centers[poc]), 2), "label": "成交密集区(POC)",
                "type": "sr", "side": _side(centers[poc], close), "strength": "strong"})
    cands = [(i, hist[i]) for i in range(len(hist)) if hist[i] > mean_vol and i != poc]
    cands.sort(key=lambda x: x[1], reverse=True)
    for i, _v in cands[:2]:
        out.append({"value": round(float(centers[i]), 2), "label": "成交密集区",
                    "type": "sr", "side": _side(centers[i], close), "strength": "medium"})
    return out


# ================================================================
# 2. 枢轴点 (Pivot Point) —— 经典公式,基于最近完整交易日
# ================================================================

def _pivot_points(df: pd.DataFrame) -> list[dict]:
    """经典 Pivot:P=(H+L+C)/3, R1/R2/R3, S1/S2/S3。基准取最后一根 K。"""
    if df.empty:
        return []
    h, l, c = df["High"].iloc[-1], df["Low"].iloc[-1], df["Close"].iloc[-1]
    if not (_ok(h) and _ok(l) and _ok(c)):
        return []
    h, l, c = float(h), float(l), float(c)
    p = (h + l + c) / 3
    r1, s1 = 2 * p - l, 2 * p - h
    r2, s2 = p + (h - l), p - (h - l)
    r3, s3 = h + 2 * (p - l), l - 2 * (h - p)

    def lv(v, label, side, strength, rank):
        # rank:档位标记,前端据此按"显示到第几档"过滤(0=P, 1=R1/S1, 2=R2/S2, 3=R3/S3)
        return {"value": round(v, 2), "label": label, "type": "pivot",
                "side": side, "strength": strength, "rank": rank}

    return [
        lv(p, "枢轴位 P", "neutral", "strong", 0),
        lv(r1, "压力位 R1", "resistance", "medium", 1),
        lv(r2, "压力位 R2", "resistance", "medium", 2),
        lv(r3, "压力位 R3", "resistance", "weak", 3),
        lv(s1, "支撑位 S1", "support", "medium", 1),
        lv(s2, "支撑位 S2", "support", "medium", 2),
        lv(s3, "支撑位 S3", "support", "weak", 3),
    ]


# ================================================================
# 3. 前高 / 前低 —— 60 / 250 日极值 + 近期 swing 高低点
# ================================================================

def _extreme_levels(df: pd.DataFrame) -> list[dict]:
    """历史极值(60/250 日,跳过 120 避免冗余)+ 近期 swing 高低点(每侧取最近 2 个)。"""
    if df.empty:
        return []
    close = float(df["Close"].iloc[-1])
    out: list[dict] = []

    for n in (60, 250):
        if len(df) < n:
            continue
        sub = df.tail(n)
        hi, lo = float(sub["High"].max()), float(sub["Low"].min())
        if _ok(hi):
            out.append({"value": round(hi, 2), "label": f"{n}日新高",
                        "type": "extreme", "side": "resistance", "strength": "strong"})
        if _ok(lo):
            out.append({"value": round(lo, 2), "label": f"{n}日新低",
                        "type": "extreme", "side": "support", "strength": "strong"})

    # 近期 swing 高低点(window=5 的局部极值)
    win = 5
    if len(df) > win * 2 and close:
        highs = df["High"].to_numpy()
        lows = df["Low"].to_numpy()
        swing_h, swing_l = [], []
        for i in range(win, len(highs) - win):
            if highs[i] == highs[i - win:i + win + 1].max():
                swing_h.append(float(highs[i]))
            if lows[i] == lows[i - win:i + win + 1].min():
                swing_l.append(float(lows[i]))
        agg_h = [v for v in _aggregate_levels(swing_h, 0.01) if v > close * 1.001]
        agg_h.sort(key=lambda v: abs(v - close))
        for v in agg_h[:2]:
            out.append({"value": round(v, 2), "label": "前高",
                        "type": "extreme", "side": "resistance", "strength": "medium"})
        agg_l = [v for v in _aggregate_levels(swing_l, 0.01) if v < close * 0.999]
        agg_l.sort(key=lambda v: abs(v - close))
        for v in agg_l[:2]:
            out.append({"value": round(v, 2), "label": "前低",
                        "type": "extreme", "side": "support", "strength": "medium"})
    return out


# ================================================================
# 4. 波动通道 —— 布林带 + Keltner 三档
# ================================================================

def _boll_channel(df: pd.DataFrame) -> list[dict]:
    """布林带上下轨 + 中轨(MA20 ± 2σ)。统计波动带,非真实支撑压力,仅作边界参考。"""
    if df.empty:
        return []
    close = _last(df, "Close")
    bu, bl, mid = _last(df, "_bb_up"), _last(df, "_bb_low"), _last(df, "_bb_mid")
    if not close or bu is None or bl is None:
        return []
    out = [
        {"value": round(bu, 2), "label": "布林上轨", "type": "boll",
         "side": _side(bu, close), "strength": "medium"},
        {"value": round(bl, 2), "label": "布林下轨", "type": "boll",
         "side": _side(bl, close), "strength": "medium"},
    ]
    if mid is not None:
        out.append({"value": round(mid, 2), "label": "布林中轨(MA20)", "type": "boll",
                    "side": _side(mid, close), "strength": "medium"})
    return out


def _keltner_band(df: pd.DataFrame, ma_col: str, n: float,
                  label_short: str, type_key: str) -> list[dict]:
    """单档 Keltner 通道:均线 ± n×ATR(ATR 自适应,通道随波动收缩/扩张)。"""
    close, atr, ma_val = _last(df, "Close"), _last(df, "_atr"), _last(df, ma_col)
    if not close or atr is None or ma_val is None:
        return []
    upper, lower = ma_val + n * atr, ma_val - n * atr
    return [
        {"value": round(upper, 2), "label": f"{label_short}通道上轨", "type": type_key,
         "side": _side(upper, close), "strength": "medium"},
        {"value": round(lower, 2), "label": f"{label_short}通道下轨", "type": type_key,
         "side": _side(lower, close), "strength": "medium"},
    ]


# ================================================================
# 5. ATR 止损位 —— close ± n × ATR,动态止盈止损
# ================================================================

def _atr_stops(df: pd.DataFrame) -> list[dict]:
    """基于 ATR(14) 的动态止损/止盈位:close ± 1.5/2×ATR(交易者最常用的止损算法)。"""
    close, atr = _last(df, "Close"), _last(df, "_atr")
    if not close or atr is None:
        return []

    def lv(v, label, side, strength):
        return {"value": round(v, 2), "label": label, "type": "atr_stop",
                "side": side, "strength": strength}

    return [
        lv(close + 2 * atr, "ATR 止盈(+2)", "resistance", "medium"),
        lv(close + 1.5 * atr, "ATR 上轨(+1.5)", "resistance", "weak"),
        lv(close - 1.5 * atr, "ATR 下轨(-1.5)", "support", "weak"),
        lv(close - 2 * atr, "ATR 止损(-2)", "support", "medium"),
    ]


# ================================================================
# 6. 缺口位 (Gap) —— 未回补的跳空缺口
# ================================================================

def _gap_levels(df: pd.DataFrame, lookback: int = 120) -> list[dict]:
    """近期未回补的向上/向下跳空缺口(天然支撑/阻力)。每方向取距当前价最近 3 个。"""
    if df.empty or len(df) < 5:
        return []
    sub = df.tail(lookback) if len(df) > lookback else df
    close = float(df["Close"].iloc[-1])
    highs = sub["High"].to_numpy()
    lows = sub["Low"].to_numpy()

    up_gaps, dn_gaps = [], []  # (缺口低点, 缺口高点)
    for i in range(1, len(highs)):
        if all(_ok(x) for x in (highs[i], lows[i], highs[i - 1], lows[i - 1])):
            if lows[i] > highs[i - 1]:        # 向上缺口
                up_gaps.append((highs[i - 1], lows[i]))
            elif highs[i] < lows[i - 1]:      # 向下缺口
                dn_gaps.append((highs[i], lows[i - 1]))

    def unfilled(gaps, is_up):
        mids = []
        for g_lo, g_hi in gaps:
            if is_up and close >= g_hi:       # 价站缺口上方 = 未回补
                mids.append((g_lo + g_hi) / 2)
            elif not is_up and close <= g_lo:  # 价处缺口下方 = 未回补
                mids.append((g_lo + g_hi) / 2)
        agg = _aggregate_levels(mids, 0.005)
        agg.sort(key=lambda v: abs(v - close))
        return agg[:3]

    out: list[dict] = []
    for m in unfilled(up_gaps, True):
        out.append({"value": round(m, 2), "label": "向上缺口", "type": "gap",
                    "side": _side(m, close), "strength": "medium"})
    for m in unfilled(dn_gaps, False):
        out.append({"value": round(m, 2), "label": "向下缺口", "type": "gap",
                    "side": _side(m, close), "strength": "medium"})
    return out


# ================================================================
# 7. 斐波那契回撤 —— 基于近期波段
# ================================================================

def _fibonacci_levels(df: pd.DataFrame, window: int = 120) -> list[dict]:
    """近 window 日波段的斐波那契回撤位(0.236/0.382/0.5/0.618/0.786)。

    高点在低点之后=上涨波段(从高回撤);反之=下跌波段(从低回撤)。
    """
    if df.empty or len(df) < 10:
        return []
    sub = df.tail(window) if len(df) > window else df
    close = float(df["Close"].iloc[-1])
    highs = sub["High"].to_numpy()
    lows = sub["Low"].to_numpy()
    hi_pos, lo_pos = int(np.argmax(highs)), int(np.argmin(lows))
    hi_val, lo_val = float(highs[hi_pos]), float(lows[lo_pos])
    if not (_ok(hi_val) and _ok(lo_val)) or hi_val <= lo_val:
        return []
    rng = hi_val - lo_val
    up_trend = hi_pos > lo_pos
    out: list[dict] = []
    for r in (0.236, 0.382, 0.5, 0.618, 0.786):
        val = hi_val - rng * r if up_trend else lo_val + rng * r
        out.append({"value": round(val, 2), "label": f"Fib {r * 100:.1f}%", "type": "fib",
                    "side": _side(val, close), "strength": "medium"})
    return out


# ================================================================
# 8. 整数关口 —— 心理支撑/阻力位
# ================================================================

def _round_numbers(df: pd.DataFrame, pct: float = 0.10, max_count: int = 8) -> list[dict]:
    """当前价附近的心理整数关口(按价格量级自适应步长)。过滤距当前价 <1% 的。"""
    if df.empty:
        return []
    close = float(df["Close"].iloc[-1])
    if not _ok(close):
        return []
    if close < 10:
        step = 0.5
    elif close < 20:
        step = 1.0
    elif close < 100:
        step = 5.0
    elif close < 500:
        step = 10.0
    else:
        step = 50.0

    lo, hi = close * (1 - pct), close * (1 + pct)
    start = (int(lo / step) + (1 if lo % step > 0 else 0)) * step
    cands = []
    v = start
    while v <= hi:
        if v > 0:
            cands.append(round(v, 2))
        v += step
    cands.sort(key=lambda x: abs(x - close))
    out: list[dict] = []
    for v in cands[:max_count]:
        if abs(v - close) / close < 0.01:   # 太近,无分析价值
            continue
        out.append({"value": round(v, 2), "label": f"整数关口 {v:g}", "type": "round",
                    "side": _side(v, close), "strength": "weak"})
    return out


# ================================================================
# 对外入口
# ================================================================

def compute_levels(df: pd.DataFrame) -> dict[str, list[dict]]:
    """计算 11 类价位点,返回 {分组key: [点位...]}(key 同 LEVEL_TYPES)。

    df: datahub.kline 的日 K(DatetimeIndex + 大写 OHLCV);内部自算 MA/ATR/BOLL。
    任一步异常或空数据 → 该组返回空列表,绝不抛异常。
    """
    if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
        return {k: [] for k in LEVEL_TYPES}
    try:
        d = _prep(df)
    except Exception:
        return {k: [] for k in LEVEL_TYPES}

    def _safe(fn, *a):
        try:
            return fn(*a)
        except Exception:
            return []

    return {
        "sr": _safe(_support_resistance, d),
        "pivot": _safe(_pivot_points, d),
        "extreme": _safe(_extreme_levels, d),
        "boll": _safe(_boll_channel, d),
        "keltner_s": _safe(_keltner_band, d, "_ma20", 2.0, "短期", "keltner_s"),
        "keltner_m": _safe(_keltner_band, d, "_ma60", 2.5, "中期", "keltner_m"),
        "keltner_l": _safe(_keltner_band, d, "_ma120", 3.0, "长期", "keltner_l"),
        "atr_stop": _safe(_atr_stops, d),
        "gap": _safe(_gap_levels, d),
        "fib": _safe(_fibonacci_levels, d),
        "round": _safe(_round_numbers, d),
    }


def summarize_levels(levels: dict[str, list[dict]], close: Optional[float]) -> str:
    """生成给 AI 提示词的价位摘要(紧凑单行,每组取距当前价最近 2 个)。"""
    if not close:
        return "无价位数据"
    parts = [f"当前价 {close:.2f}"]
    for key, label in LEVEL_TYPES.items():
        pts = levels.get(key, [])
        if not pts:
            continue
        ranked = sorted(pts, key=lambda p: abs(p["value"] - close))[:2]
        desc = "、".join(f"{p['label']}={p['value']}" for p in ranked)
        parts.append(f"{label}: {desc}")
    return " · ".join(parts)


def analyze_levels(df: pd.DataFrame, code: Optional[str] = None) -> dict:
    """统一分析入口(对齐 analyze_chan 风格):返回 levels + summary + 元信息。

    供 API(stock_insights)/ MCP(price_levels)/ AI prompt 调用。
    """
    levels = compute_levels(df)
    close = None
    if df is not None and not getattr(df, "empty", True) and "Close" in df.columns:
        try:
            close = float(df["Close"].iloc[-1])
        except Exception:
            close = None
    total = sum(len(v) for v in levels.values())
    return {
        "code": code,
        "close": round(close, 2) if close else None,
        "levels": levels,
        "groups": LEVEL_TYPES,
        "count": total,
        "summary": summarize_levels(levels, close),
    }

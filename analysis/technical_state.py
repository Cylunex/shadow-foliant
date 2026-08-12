"""确定性技术状态汇总：量价/OBV、唐奇安突破、ADX 与趋势质量。

只消费本地日 K，不拉外部数据、不调用 LLM。所有突破线都用 ``shift(1)`` 或显式
排除当前 K 线，避免把当天高低点算进自身突破阈值。输出既供交易计划解释，也以
小幅封顶分数参与综合 TOP15 → TOP5 的二次优选。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _frame(data: pd.DataFrame) -> pd.DataFrame:
    """统一 OHLCV 列名并去掉无效行；不修改调用方 DataFrame。"""
    if data is None or getattr(data, "empty", True):
        return pd.DataFrame()
    aliases = {
        "Open": "open", "High": "high", "Low": "low", "Close": "close",
        "Volume": "volume", "开盘": "open", "最高": "high", "最低": "low",
        "收盘": "close", "成交量": "volume",
    }
    df = data.rename(columns={k: v for k, v in aliases.items() if k in data.columns}).copy()
    required = ("high", "low", "close")
    if not all(col in df.columns for col in required):
        return pd.DataFrame()
    for col in (*required, "open", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=list(required))


def analyze_volume_price(data: pd.DataFrame) -> Dict[str, Any]:
    """识别 8 日价格方向、3/5 日量能变化、OBV 背离与放量突破。"""
    df = _frame(data)
    if len(df) < 21 or "volume" not in df.columns:
        return {"available": False, "reason": "至少需要21根含成交量的日K"}
    close = df["close"]
    volume = df["volume"].fillna(0).clip(lower=0)
    price_return = _finite(close.iloc[-1] / close.iloc[-8] - 1) or 0.0
    recent_volume = _finite(volume.iloc[-3:].mean()) or 0.0
    prior_volume = _finite(volume.iloc[-8:-3].mean()) or 0.0
    volume_change = recent_volume / prior_volume - 1 if prior_volume > 0 else 0.0

    price_direction = "up" if price_return > 0.02 else ("down" if price_return < -0.02 else "flat")
    volume_direction = "up" if volume_change > 0.30 else ("down" if volume_change < -0.30 else "flat")
    state_map = {
        ("up", "up"): ("price_up_volume_up", "价涨量增"),
        ("up", "down"): ("price_up_volume_down", "价涨量缩"),
        ("down", "up"): ("price_down_volume_up", "价跌量增"),
        ("down", "down"): ("price_down_volume_down", "价跌量缩"),
    }
    state, state_cn = state_map.get((price_direction, volume_direction),
                                    ("balanced", "量价平稳"))

    signed_volume = np.sign(close.diff().fillna(0).to_numpy()) * volume.to_numpy()
    obv = pd.Series(signed_volume, index=df.index).cumsum()
    obv_window = obv.iloc[-8:]
    volume_sum = float(volume.iloc[-8:].sum())
    obv_change = ((float(obv_window.iloc[-1]) - float(obv_window.iloc[0])) / volume_sum
                  if volume_sum > 0 else 0.0)
    divergence = "none"
    divergence_cn = "无明显背离"
    if price_return > 0.02 and obv_change < -0.08:
        divergence, divergence_cn = "bearish", "价格上涨但OBV走弱"
    elif price_return < -0.02 and obv_change > 0.08:
        divergence, divergence_cn = "bullish", "价格下跌但OBV走强"

    prior20 = volume.iloc[-21:-1]
    volume_ratio20 = float(volume.iloc[-1] / prior20.mean()) if float(prior20.mean()) > 0 else 0.0
    volume_breakout = bool(volume_ratio20 >= 1.5 and volume.iloc[-1] >= prior20.max())
    return {
        "available": True,
        "state": state,
        "state_cn": state_cn,
        "price_direction": price_direction,
        "volume_direction": volume_direction,
        "price_return_8d_pct": round(price_return * 100, 2),
        "volume_change_pct": round(volume_change * 100, 2),
        "obv_change_8": round(obv_change, 4),
        "obv_divergence": divergence,
        "obv_divergence_cn": divergence_cn,
        "volume_ratio20": round(volume_ratio20, 2),
        "volume_breakout": volume_breakout,
    }


def _atr(df: pd.DataFrame, period: int = 20) -> pd.Series:
    previous = df["close"].shift(1)
    tr = pd.concat((df["high"] - df["low"],
                    (df["high"] - previous).abs(),
                    (df["low"] - previous).abs()), axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def analyze_donchian(data: pd.DataFrame) -> Dict[str, Any]:
    """20/55 日唐奇安状态及 N 风险线；通道均排除当前日。"""
    df = _frame(data)
    if len(df) < 56:
        return {"available": False, "reason": "至少需要56根日K"}
    high, low, close = df["high"], df["low"], df["close"]
    current = float(close.iloc[-1])
    upper20 = float(high.iloc[-21:-1].max())
    upper55 = float(high.iloc[-56:-1].max())
    lower10 = float(low.iloc[-11:-1].min())
    lower20 = float(low.iloc[-21:-1].min())
    n_value = _finite(_atr(df, 20).iloc[-1])

    rolling_upper20 = high.shift(1).rolling(20).max()
    breakout_series = close > rolling_upper20
    recent_breakout = bool(breakout_series.iloc[-6:-1].fillna(False).any())
    if current < lower20:
        status, status_cn, direction = "exit_20", "跌破20日退出线", "bearish"
    elif current < lower10:
        status, status_cn, direction = "exit_10", "跌破10日退出线", "bearish"
    elif current > upper55:
        status, status_cn, direction = "breakout_55", "突破55日通道", "bullish"
    elif current > upper20:
        status, status_cn, direction = "breakout_20", "突破20日通道", "bullish"
    elif recent_breakout and current <= upper20:
        status, status_cn, direction = "failed_breakout", "近期突破后回落通道内", "bearish"
    else:
        status, status_cn, direction = "inside", "通道内运行", "neutral"

    return {
        "available": True,
        "status": status,
        "status_cn": status_cn,
        "direction": direction,
        "upper20": round(upper20, 2),
        "upper55": round(upper55, 2),
        "exit10": round(lower10, 2),
        "exit20": round(lower20, 2),
        "breakout_distance_pct": round((current / upper20 - 1) * 100, 2),
        "n20": round(n_value, 3) if n_value is not None else None,
        "stop_2n": round(max(0.01, current - 2 * n_value), 2) if n_value is not None else None,
        "add_05n": round(current + 0.5 * n_value, 2) if n_value is not None else None,
        "auto_pyramid": False,
    }


def analyze_trend_quality(data: pd.DataFrame) -> Dict[str, Any]:
    """ADX/DMI 与标准化回归斜率、R²，衡量趋势方向和顺滑程度。"""
    df = _frame(data)
    if len(df) < 40:
        return {"available": False, "reason": "至少需要40根日K"}
    high, low, close = df["high"], df["low"], df["close"]
    period = 14
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    atr14 = _atr(df, period)
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr14
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr14
    denominator = (plus_di + minus_di).replace(0, np.nan)
    dx = (plus_di - minus_di).abs() / denominator * 100
    adx = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    window = close.iloc[-20:].to_numpy(dtype=float)
    x = np.arange(len(window), dtype=float)
    slope, intercept = np.polyfit(x, window, 1)
    fitted = slope * x + intercept
    ss_res = float(np.square(window - fitted).sum())
    ss_tot = float(np.square(window - window.mean()).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    slope_pct = slope / float(window.mean()) * 100 if float(window.mean()) else 0.0
    adx_value = _finite(adx.iloc[-1])
    pdi_value = _finite(plus_di.iloc[-1])
    mdi_value = _finite(minus_di.iloc[-1])
    if adx_value is None:
        regime, regime_cn = "unknown", "趋势强度不可用"
    elif adx_value >= 25 and (pdi_value or 0) > (mdi_value or 0):
        regime, regime_cn = "strong_up", "强上升趋势"
    elif adx_value >= 25:
        regime, regime_cn = "strong_down", "强下降趋势"
    elif adx_value < 18:
        regime, regime_cn = "sideways", "震荡趋势"
    else:
        regime, regime_cn = "developing", "趋势形成中"
    return {
        "available": True,
        "regime": regime,
        "regime_cn": regime_cn,
        "adx14": round(adx_value, 2) if adx_value is not None else None,
        "pdi14": round(pdi_value, 2) if pdi_value is not None else None,
        "mdi14": round(mdi_value, 2) if mdi_value is not None else None,
        "slope_pct_20": round(float(slope_pct), 4),
        "r2_20": round(max(0.0, min(1.0, r2)), 4),
    }


def analyze_risk_quality(data: pd.DataFrame) -> Dict[str, Any]:
    """20 日低波质量、回撤和整理结构；全部为尺度归一化指标。"""
    df = _frame(data)
    if len(df) < 21:
        return {"available": False, "reason": "至少需要21根日K"}
    recent = df.tail(20)
    previous = df.iloc[-21:-1]
    close = recent["close"]
    returns = close.pct_change().dropna()
    volatility = float(returns.std() * math.sqrt(252) * 100) if len(returns) >= 2 else None
    running_high = close.cummax()
    drawdown = close / running_high - 1.0
    max_drawdown = float(min(drawdown.min() * 100, 0.0))
    last_close = float(close.iloc[-1])
    atr_value = _finite(_atr(df, 20).iloc[-1])
    atr_pct = atr_value / last_close * 100 if atr_value is not None and last_close > 0 else None

    open_price = _finite(df["open"].iloc[-1]) if "open" in df.columns else None
    body_pct = ((last_close / open_price - 1.0) * 100
                if open_price is not None and open_price > 0 else None)
    ma20 = float(df["close"].tail(20).mean())
    pullback_pct = (last_close / ma20 - 1.0) * 100 if ma20 > 0 else None
    low_min = float(recent["low"].min())
    range_pct = ((float(recent["high"].max()) / low_min - 1.0) * 100
                 if low_min > 0 else None)

    consolidation_days = 0
    for days in range(min(len(previous), 20), 1, -1):
        window = previous.tail(days)
        low = float(window["low"].min())
        span = ((float(window["high"].max()) / low - 1.0) * 100
                if low > 0 else None)
        if span is not None and span <= 12.0:
            consolidation_days = days
            break

    low_vol_quality = bool(
        volatility is not None and volatility <= 32.0
        and max_drawdown >= -8.0
        and atr_pct is not None and atr_pct <= 4.5
    )
    high_risk = bool(
        (volatility is not None and volatility > 45.0)
        or max_drawdown < -15.0
        or (atr_pct is not None and atr_pct > 7.0)
    )
    return {
        "available": True,
        "volatility_20d_pct": round(volatility, 2) if volatility is not None else None,
        "max_drawdown_20d_pct": round(max_drawdown, 2),
        "atr_20_pct": round(atr_pct, 2) if atr_pct is not None else None,
        "range_20d_pct": round(range_pct, 2) if range_pct is not None else None,
        "body_pct": round(body_pct, 2) if body_pct is not None else None,
        "pullback_to_ma20_pct": round(pullback_pct, 2) if pullback_pct is not None else None,
        "consolidation_days_20d": consolidation_days,
        "low_vol_quality": low_vol_quality,
        "high_risk": high_risk,
    }


def analyze_technical_state(data: pd.DataFrame) -> Dict[str, Any]:
    """汇总互补技术证据并生成封顶在 [-8, 8] 的透明软评分。"""
    volume_price = analyze_volume_price(data)
    donchian = analyze_donchian(data)
    trend = analyze_trend_quality(data)
    risk_quality = analyze_risk_quality(data)
    try:
        from analysis.pattern_recognition import PatternDetector
        patterns = PatternDetector().detect_all(data, lookback=5)
    except Exception as exc:
        patterns = {"error": f"{type(exc).__name__}: {str(exc)[:60]}"}

    score = 0.0
    positives, risks = [], []
    if volume_price.get("available"):
        state = volume_price.get("state")
        if state == "price_up_volume_up":
            score += 2
            positives.append("价涨量增")
        elif state == "price_up_volume_down":
            score -= 2
            risks.append("价涨量缩")
        elif state == "price_down_volume_up":
            score -= 3
            risks.append("价跌量增")
        if volume_price.get("obv_divergence") == "bullish":
            score += 2
            positives.append("OBV底背离")
        elif volume_price.get("obv_divergence") == "bearish":
            score -= 3
            risks.append("OBV顶背离")
        if volume_price.get("volume_breakout"):
            score += 1
            positives.append("成交量突破")

    if donchian.get("available"):
        status = donchian.get("status")
        if status == "breakout_55":
            score += 4
            positives.append("55日通道突破")
        elif status == "breakout_20":
            score += 3
            positives.append("20日通道突破")
        elif status in {"failed_breakout", "exit_10", "exit_20"}:
            score -= 4 if status == "failed_breakout" else 5
            risks.append(donchian.get("status_cn"))

    if trend.get("available"):
        regime = trend.get("regime")
        if regime == "strong_up":
            score += 2
            positives.append("ADX确认上升趋势")
        elif regime == "strong_down":
            score -= 3
            risks.append("ADX确认下降趋势")
        elif regime == "sideways" and donchian.get("status") in {"breakout_20", "breakout_55"}:
            score -= 2
            risks.append("ADX偏低，突破趋势性不足")
        if (trend.get("slope_pct_20") or 0) > 0 and (trend.get("r2_20") or 0) >= 0.65:
            score += 1
            positives.append("20日上涨路径平稳")

    if risk_quality.get("available"):
        if risk_quality.get("low_vol_quality"):
            score += 2
            positives.append("低波动且20日回撤可控")
        elif risk_quality.get("high_risk"):
            score -= 3
            risks.append(
                f"波动/回撤偏高(波动{risk_quality.get('volatility_20d_pct')}%、"
                f"回撤{risk_quality.get('max_drawdown_20d_pct')}%)"
            )
        if (donchian.get("status") in {"breakout_20", "breakout_55"}
                and risk_quality.get("consolidation_days_20d", 0) >= 8
                and (risk_quality.get("body_pct") or 0) >= 0.5
                and (volume_price.get("volume_ratio20") or 0) >= 1.3):
            score += 2
            positives.append("整理后放量实体突破")

    confirmed_patterns = []
    if isinstance(patterns, dict) and "error" not in patterns:
        # 经典蜡烛线仍保留给 Agent 展示，但只让更完整、带突破/失效位的复合结构计分。
        confirmed_patterns = [p for p in patterns.values() if isinstance(p, dict)
                              and p.get("source") == "composite"
                              and p.get("found") and p.get("status") == "confirmed"]
        bullish = [p for p in confirmed_patterns if p.get("direction") == "bullish"]
        bearish = [p for p in confirmed_patterns if p.get("direction") == "bearish"]
        if bullish:
            score += min(4, 2 * len(bullish))
            positives.append("确认看涨形态:" + "、".join(str(p.get("name")) for p in bullish[:2]))
        if bearish:
            score -= min(6, 3 * len(bearish))
            risks.append("确认看跌形态:" + "、".join(str(p.get("name")) for p in bearish[:2]))

    score = round(max(-8.0, min(8.0, score)), 2)
    return {
        "available": any(x.get("available") for x in (volume_price, donchian, trend, risk_quality)),
        "score": score,
        "grade": "positive" if score >= 3 else ("caution" if score <= -3 else "neutral"),
        "positives": list(dict.fromkeys(x for x in positives if x)),
        "risks": list(dict.fromkeys(x for x in risks if x)),
        "volume_price": volume_price,
        "donchian": donchian,
        "trend_quality": trend,
        "risk_quality": risk_quality,
        "patterns": patterns,
        "confirmed_patterns": confirmed_patterns,
        "method": "price-volume+OBV+Donchian(ex-current)+ADX+normalized-slope+low-vol-risk+confirmed-patterns",
    }

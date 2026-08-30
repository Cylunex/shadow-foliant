"""面向 Agent 的确定性交易计划汇总层。

本模块不调用 LLM。它把现有的周日共振、关键价位、ATR/VaR、组合动作和最新
决策信号收口为统一合同，明确给出通过理由与阻断项，避免 Agent 自行拼接口径。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, Optional

import pandas as pd

from analysis.multi_timeframe import evaluate as evaluate_multi_timeframe
from analysis.position_sizer import suggest_new_buy_pct
from analysis.price_levels import analyze_levels
from analysis.stress_testing import analyze_risk
from analysis.technical_state import analyze_technical_state
from core.action_decision import resolve_action


ACTION_CN = {
    "buy": "买入", "add": "增持", "hold": "持有", "reduce": "减持",
    "sell": "卖出", "watch": "观望", "avoid": "回避", "alert": "预警",
}


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _close_series(df: pd.DataFrame) -> pd.Series:
    if df is None or getattr(df, "empty", True):
        return pd.Series(dtype=float)
    col = next((c for c in df.columns if str(c).lower() == "close"), None)
    return pd.to_numeric(df[col], errors="coerce").dropna() if col is not None else pd.Series(dtype=float)


def _atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    if df is None or getattr(df, "empty", True):
        return None
    cols = {str(c).lower(): c for c in df.columns}
    if not all(k in cols for k in ("high", "low", "close")):
        return None
    high = pd.to_numeric(df[cols["high"]], errors="coerce")
    low = pd.to_numeric(df[cols["low"]], errors="coerce")
    close = pd.to_numeric(df[cols["close"]], errors="coerce")
    previous = close.shift(1)
    tr = pd.concat((high - low, (high - previous).abs(), (low - previous).abs()), axis=1).max(axis=1)
    value = tr.rolling(period).mean().iloc[-1] if len(tr) >= period else None
    return _finite(value)


def _nearby(levels: Dict[str, Iterable[dict]], close: float, side: str) -> list[float]:
    values = []
    for points in (levels or {}).values():
        for point in points or []:
            value = _finite(point.get("value"))
            if value is None:
                continue
            if side == "support" and value < close:
                values.append(value)
            elif side == "resistance" and value > close:
                values.append(value)
    return sorted(set(values), reverse=(side == "support"))


def build_trade_plan(code: str, df: pd.DataFrame, *, name: str = "",
                     market_signal: Optional[Dict[str, Any]] = None,
                     latest_signal: Optional[Dict[str, Any]] = None,
                     technical_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """从本地行情和既有结构化结论生成统一交易计划。"""
    closes = _close_series(df)
    if closes.empty:
        return {
            "available": False, "code": str(code), "name": name,
            "action": "hold", "action_cn": "不动",
            "blockers": ["无可用日 K，不能计算交易计划"],
        }

    close = float(closes.iloc[-1])
    atr = _atr(df) or close * 0.025
    timeframe = evaluate_multi_timeframe(df)
    technical = (technical_state if isinstance(technical_state, dict)
                 else analyze_technical_state(df))
    level_result = analyze_levels(df, str(code))
    levels = level_result.get("levels") or {}
    supports = _nearby(levels, close, "support")
    resistances = _nearby(levels, close, "resistance")

    # 入场区间以当前价到半个 ATR 的回踩区间为基线；附近结构支撑可抬高下沿。
    entry_high = close
    entry_low = close - atr * 0.5
    near_support = next((v for v in supports if v >= close * 0.94), None)
    if near_support is not None:
        entry_low = max(entry_low, near_support)
    entry_low = min(entry_low, entry_high)
    entry_mid = (entry_low + entry_high) / 2

    # 止损至少留一个 ATR，最多容忍约 8%；结构位存在时放在结构位下方。
    atr_stop = close - min(atr * 2.0, close * 0.08)
    stop_loss = atr_stop
    if near_support is not None:
        structure_stop = min(near_support - atr * 0.25, close - atr)
        stop_loss = max(atr_stop, structure_stop)
    stop_loss = max(0.01, stop_loss)

    # 目标取第一个有实际空间的压力位，不用固定“+10%”伪造目标。
    target_price = next((v for v in resistances if v >= close + atr * 0.8), None)
    if target_price is None and len(closes) >= 20:
        historical_high = float(closes.tail(60).max())
        if historical_high >= close + atr * 0.8:
            target_price = historical_high

    # 形态测算目标只作为第二目标展示，绝不替代附近结构压力或参与首目标盈亏比。
    bullish_pattern_targets = [
        _finite(item.get("measured_target"))
        for item in (technical.get("confirmed_patterns") or [])
        if item.get("direction") == "bullish"
    ]
    bullish_pattern_targets = [value for value in bullish_pattern_targets
                               if value is not None and value > close]
    measured_pattern_target = min(bullish_pattern_targets) if bullish_pattern_targets else None
    target_price_2 = (measured_pattern_target if target_price is not None
                      and measured_pattern_target is not None
                      and measured_pattern_target > target_price else None)

    risk_amount = entry_mid - stop_loss
    reward_amount = (target_price - entry_mid) if target_price is not None else None
    rr = (reward_amount / risk_amount) if reward_amount is not None and risk_amount > 0 else None

    risk = analyze_risk(df)
    var95 = _finite(risk.get("var_hist")) if isinstance(risk, dict) else None
    position_pct = suggest_new_buy_pct(var95)
    market = market_signal or {}
    market_action = str(market.get("action") or "unknown")
    if market_action == "strong_buy":
        position_pct = min(12.0, position_pct + 2.0)
    elif market_action == "buy":
        position_pct = min(12.0, position_pct + 1.0)

    evidence = []
    blockers = []
    if timeframe.get("weekly_regime") == "bullish":
        evidence.append("周线趋势多头")
    elif timeframe.get("weekly_regime") == "bearish":
        blockers.append("周线仍为空头环境")
    if timeframe.get("daily_trigger"):
        evidence.append("日线已突破并站上均线")
    else:
        blockers.append("日线尚未形成有效触发")
    if rr is None:
        blockers.append("上方缺少可验证目标位，无法计算盈亏比")
    elif rr >= 2.0:
        evidence.append(f"预期盈亏比 {rr:.2f}，通过 2.0 门槛")
    else:
        blockers.append(f"预期盈亏比 {rr:.2f}，低于 2.0 门槛")
    if market_action in {"strong_buy", "buy"}:
        evidence.append(f"组合环境允许{market.get('action_cn') or '买入'}")
    elif market_action in {"reduce", "sell"}:
        blockers.append(f"组合环境要求{market.get('action_cn') or '降低仓位'}")
    evidence.extend(str(x) for x in (technical.get("positives") or [])[:4])
    blockers.extend(str(x) for x in (technical.get("risks") or [])[:4])

    candidate_action = "watch"
    if (timeframe.get("resonance") == "confirmed" and rr is not None and rr >= 2.0
            and market_action not in {"reduce", "sell"}
            and technical.get("grade") != "caution"):
        candidate_action = "buy"
    if timeframe.get("resonance") == "blocked":
        candidate_action = "avoid"
    prior_action = str((latest_signal or {}).get("action") or "")
    if prior_action in {"sell", "reduce", "avoid", "alert"}:
        blockers.append(f"最新有效决策信号为{ACTION_CN.get(prior_action, prior_action)}")

    action_evidence = [{
        "source": "formal_signal", "action": candidate_action,
        "reason": evidence[0] if candidate_action == "buy" and evidence else (
            blockers[0] if blockers else "尚未形成明确加仓条件"
        ),
    }]
    if market_action in {"reduce", "sell"}:
        action_evidence.append({
            "source": "portfolio_risk", "action": market_action,
            "reason": f"组合环境要求{market.get('action_cn') or '降低仓位'}",
        })
    if prior_action in {"sell", "reduce", "avoid", "alert"}:
        action_evidence.append({
            "source": "hard_risk", "action": prior_action,
            "reason": f"最新有效决策信号为{ACTION_CN.get(prior_action, prior_action)}",
        })
    action_decision = resolve_action(action_evidence)
    action = action_decision["action"]

    return {
        "available": True,
        "code": str(code),
        "name": name,
        "action": action,
        "action_cn": action_decision["action_text"],
        "action_decision": action_decision,
        "candidate_action": candidate_action,
        "entry_low": round(entry_low, 2),
        "entry_high": round(entry_high, 2),
        "stop_loss": round(stop_loss, 2),
        "target_price": round(target_price, 2) if target_price is not None else None,
        "target_price_2": round(target_price_2, 2) if target_price_2 is not None else None,
        "measured_pattern_target": (round(measured_pattern_target, 2)
                                    if measured_pattern_target is not None else None),
        "risk_reward_ratio": round(rr, 2) if rr is not None else None,
        "suggested_position_pct": position_pct if action == "add" else 0.0,
        "horizon": "swing",
        "horizon_cn": "波段（约 2～4 周）",
        "current_price": round(close, 2),
        "atr14": round(atr, 3),
        "var95_pct": round(var95 * 100, 2) if var95 is not None else None,
        "multi_timeframe": timeframe,
        "technical_state": technical,
        "technical_score": technical.get("score"),
        "donchian_exit10": (technical.get("donchian") or {}).get("exit10"),
        "donchian_exit20": (technical.get("donchian") or {}).get("exit20"),
        "donchian_stop_2n": (technical.get("donchian") or {}).get("stop_2n"),
        "market_action": market_action,
        "evidence": list(dict.fromkeys(evidence)),
        "blockers": list(dict.fromkeys(blockers)),
        "levels_summary": level_result.get("summary"),
        "method": "calendar-week+structure-level+ATR+VaR+RR2.0+technical-confluence",
    }


def build_for_code(code: str, *, name: str = "",
                   market_signal: Optional[Dict[str, Any]] = None,
                   latest_signal: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """便捷入口：走 datahub 缓存取前复权日 K，其余仍为纯本地计算。"""
    import datahub

    df = datahub.kline(str(code), "1y", adjust="qfq")
    quality = datahub.kline_quality(df)
    if not quality.get("actionable"):
        return {
            "available": False, "code": str(code), "name": name,
            "action": "hold", "action_cn": "不动",
            "blockers": [f"日 K 不可用于决策：{quality.get('reason') or 'unknown'}"],
            "data_quality": quality,
        }
    return build_trade_plan(code, df, name=name, market_signal=market_signal,
                            latest_signal=latest_signal)

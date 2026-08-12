"""日线/自然交易周共振分析。

所有函数只消费本地 DataFrame，不取行情、不写存储。周线固定按周五结束的自然周
聚合 OHLCV，避免“每 5 根日 K 当一周”在节假日、停牌时发生边界漂移。
"""

from __future__ import annotations

from typing import Any, Dict

import pandas as pd


def _ohlcv_frame(data: pd.DataFrame) -> pd.DataFrame:
    """归一为 DatetimeIndex + Open/High/Low/Close/Volume。"""
    if data is None or getattr(data, "empty", True):
        return pd.DataFrame()
    df = data.copy()
    columns = {str(c).lower(): c for c in df.columns}
    date_col = next((columns[k] for k in ("date", "日期") if k in columns), None)
    if date_col is not None:
        dates = pd.to_datetime(df.pop(date_col), errors="coerce")
    else:
        dates = pd.to_datetime(df.index, errors="coerce")
    rename = {}
    for canonical in ("Open", "High", "Low", "Close", "Volume"):
        source = columns.get(canonical.lower())
        if source is not None:
            rename[source] = canonical
    df = df.rename(columns=rename)
    if "Close" not in df.columns:
        return pd.DataFrame()
    df.index = dates
    df = df.loc[~df.index.isna()].sort_index()
    df = df.loc[~df.index.duplicated(keep="last")]
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def resample_calendar_week(data: pd.DataFrame) -> pd.DataFrame:
    """将日 K 按自然交易周（周五结束）聚合为周 K。

    当前尚未结束的一周也会保留；它只包含调用时已经存在的日 K，不引入未来数据。
    """
    df = _ohlcv_frame(data)
    if df.empty:
        return pd.DataFrame()
    rules = {}
    if "Open" in df.columns:
        rules["Open"] = "first"
    if "High" in df.columns:
        rules["High"] = "max"
    if "Low" in df.columns:
        rules["Low"] = "min"
    rules["Close"] = "last"
    if "Volume" in df.columns:
        rules["Volume"] = "sum"
    weekly = df.resample("W-FRI", label="right", closed="right").agg(rules)
    return weekly.dropna(subset=["Close"])


def evaluate(data: pd.DataFrame, weekly_ma_period: int = 10,
             daily_ma_period: int = 20, breakout_days: int = 5) -> Dict[str, Any]:
    """返回周线环境、日线触发和共振结论，数据不足时明确降级。"""
    df = _ohlcv_frame(data)
    weekly = resample_calendar_week(df)
    result: Dict[str, Any] = {
        "available": False,
        "weekly_regime": "unknown",
        "weekly_regime_cn": "数据不足",
        "daily_trigger": False,
        "resonance": "unknown",
        "resonance_cn": "数据不足",
        "weekly_rows": int(len(weekly)),
        "reason": "周线样本不足",
    }
    need = max(int(weekly_ma_period) + 2, 35)
    if df.empty or len(weekly) < need:
        return result

    wc = weekly["Close"].astype(float)
    ma = wc.rolling(int(weekly_ma_period)).mean()
    dif = wc.ewm(span=12, adjust=False).mean() - wc.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    latest = float(wc.iloc[-1])
    ma_latest = float(ma.iloc[-1])
    ma_prev = float(ma.iloc[-2])
    macd_bull = bool(dif.iloc[-1] > dea.iloc[-1])
    above_ma = bool(latest > ma_latest)
    ma_rising = bool(ma_latest > ma_prev)
    if macd_bull and above_ma and ma_rising:
        regime, regime_cn = "bullish", "周线多头"
    elif (not macd_bull) and (not above_ma) and (not ma_rising):
        regime, regime_cn = "bearish", "周线空头"
    else:
        regime, regime_cn = "neutral", "周线震荡"

    daily_trigger = False
    daily_above_ma = False
    breakout = False
    volume_confirmed = None
    if len(df) >= max(int(daily_ma_period), int(breakout_days)) + 1:
        close = df["Close"].astype(float)
        daily_ma = close.rolling(int(daily_ma_period)).mean()
        daily_above_ma = bool(close.iloc[-1] > daily_ma.iloc[-1])
        breakout = bool(close.iloc[-1] > close.iloc[-(int(breakout_days) + 1):-1].max())
        if "Volume" in df.columns:
            volume = df["Volume"].astype(float)
            base = float(volume.iloc[-(int(daily_ma_period) + 1):-1].mean())
            volume_confirmed = bool(base > 0 and volume.iloc[-1] >= base * 1.2)
        daily_trigger = daily_above_ma and breakout and volume_confirmed is not False

    if regime == "bullish" and daily_trigger:
        resonance, resonance_cn = "confirmed", "周日共振"
    elif regime == "bearish" and not daily_above_ma:
        resonance, resonance_cn = "blocked", "周日同弱"
    else:
        resonance, resonance_cn = "waiting", "等待日线确认"

    result.update({
        "available": True,
        "weekly_regime": regime,
        "weekly_regime_cn": regime_cn,
        "daily_trigger": bool(daily_trigger),
        "daily_above_ma": bool(daily_above_ma),
        "daily_breakout": bool(breakout),
        "volume_confirmed": volume_confirmed,
        "resonance": resonance,
        "resonance_cn": resonance_cn,
        "weekly_close": round(latest, 3),
        "weekly_ma": round(ma_latest, 3),
        "weekly_macd_bull": macd_bull,
        "reason": f"{regime_cn}；{resonance_cn}",
    })
    return result

"""zzshare 原子数据源。

Token 仅从环境变量注入并由 SDK 放入请求头；本适配层不记录异常正文、请求头或返回数据。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import logging
import os
import threading
from typing import Dict, List, Optional

import pandas as pd


_lock = threading.RLock()
_api = None

_MINUTE_FREQ = {
    "1m": "1min", "1min": "1min",
    "5m": "5min", "5min": "5min",
    "15m": "15min", "15min": "15min",
    "30m": "30min", "30min": "30min",
    "60m": "60min", "60min": "60min", "1h": "60min",
}


def _enabled() -> bool:
    return os.getenv("ZZSHARE_ENABLED", "true").lower() not in {"0", "false", "no"}


def _positive_int(value, default: int) -> int:
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return default


def available() -> bool:
    if not _enabled() or not os.getenv("ZZSHARE_TOKEN", "").strip():
        return False
    try:
        import zzshare  # noqa: F401
        return True
    except Exception:
        return False


def _get_api():
    global _api
    if _api is not None:
        return _api
    if not available():
        return None
    try:
        from zzshare import DataApi

        kwargs = {
            "token": os.environ["ZZSHARE_TOKEN"],
            "timeout": _positive_int(os.getenv("ZZSHARE_TIMEOUT"), 10),
        }
        # 留空时使用 SDK 自带端点；仓库示例不保存真实服务地址。
        custom_url = os.getenv("ZZSHARE_BASE_URL", "").strip()
        if custom_url:
            kwargs["http_url"] = custom_url
        _api = DataApi(**kwargs)
        return _api
    except Exception:
        return None


@contextmanager
def _quiet_sdk_logs():
    """SDK 会记录响应正文；调用期间禁用其 logger，避免数据或鉴权细节落盘。"""
    logger = logging.getLogger("zzshare")
    old_disabled = logger.disabled
    logger.disabled = True
    try:
        yield
    finally:
        logger.disabled = old_disabled


def _ts_code(symbol: str) -> str:
    raw = str(symbol).upper().strip()
    digits = "".join(ch for ch in raw if ch.isdigit())[-6:]
    if len(digits) != 6:
        return ""
    if raw.endswith((".SH", ".SZ", ".BJ")):
        return raw
    if digits.startswith(("4", "8", "92")):
        suffix = "BJ"
    elif digits.startswith(("0", "2", "3")):
        suffix = "SZ"
    else:
        suffix = "SH"
    return f"{digits}.{suffix}"


def _volume_multiplier(df: pd.DataFrame, volume_col: str) -> float:
    """用成交额/量/价自校验“手/股”量纲；样本不足时按 Tushare 风格的“手”处理。"""
    if "amount" not in df.columns:
        return 100.0
    try:
        volume = pd.to_numeric(df[volume_col], errors="coerce")
        amount = pd.to_numeric(df["amount"], errors="coerce")
        close = pd.to_numeric(df["close"], errors="coerce")
        valid = (volume > 0) & (amount > 0) & (close > 0)
        if int(valid.sum()) >= 3:
            ratio = float((amount[valid] / volume[valid] / close[valid]).median())
            return 1.0 if abs(ratio - 1.0) < abs(ratio - 100.0) else 100.0
    except Exception:
        pass
    return 100.0


def _standardize(df: Optional[pd.DataFrame], *, time_col: str, volume_col: str) -> pd.DataFrame:
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()
    required = {time_col, "open", "high", "low", "close", volume_col}
    if not required.issubset(df.columns):
        return pd.DataFrame()
    mult = _volume_multiplier(df, volume_col)
    out = pd.DataFrame({
        "date": pd.to_datetime(df[time_col], errors="coerce"),
        "open": pd.to_numeric(df["open"], errors="coerce"),
        "high": pd.to_numeric(df["high"], errors="coerce"),
        "low": pd.to_numeric(df["low"], errors="coerce"),
        "close": pd.to_numeric(df["close"], errors="coerce"),
        "volume": pd.to_numeric(df[volume_col], errors="coerce") * mult,
    })
    if "amount" in df.columns:
        out["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    if time_col == "trade_date":
        out["date"] = out["date"].dt.normalize()
    return (out.dropna(subset=["date"]).drop_duplicates(subset=["date"], keep="last")
            .sort_values("date").reset_index(drop=True))


def get_kline(symbol: str, period_days: int = 365, frequency: str = "1d",
              adjust: str = "raw", count: Optional[int] = None) -> pd.DataFrame:
    """获取日线或分钟线，输出统一小写 OHLCVA 格式。"""
    code = _ts_code(symbol)
    api = _get_api()
    if not code or api is None:
        return pd.DataFrame()
    freq = str(frequency).strip().lower()
    try:
        with _lock, _quiet_sdk_logs():
            if freq in _MINUTE_FREQ:
                wanted = min(_positive_int(count, 1000), 1000)
                df = api.stk_mins(ts_code=code, freq=_MINUTE_FREQ[freq], count=wanted)
                return _standardize(df, time_col="trade_time", volume_col="vol").tail(wanted)
            if freq not in {"1d", "day", "daily", "d", "101"}:
                return pd.DataFrame()
            end = datetime.now().date()
            start = end - timedelta(days=_positive_int(period_days, 365) + 10)
            df = api.daily(
                ts_code=code,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adj="qfq" if adjust == "qfq" else "",
            )
            return _standardize(df, time_col="trade_date", volume_col="vol")
    except Exception:
        return pd.DataFrame()


def _quote_dict(row: dict) -> dict:
    price = float(row.get('close') or 0)
    last = float(row.get('pre_close') or 0)
    high = float(row.get('high') or 0)
    low = float(row.get('low') or 0)
    change = price - last
    return {
        'name': str(row.get('name') or ''), 'price': price, 'last_close': last,
        'open': float(row.get('open') or 0), 'high': high, 'low': low,
        'change_amt': round(change, 4),
        'change_pct': round(change / last * 100, 4) if last else 0.0,
        'amount_wan': float(row.get('amount') or 0) / 1e4,
        'turnover_pct': float(row.get('turnover_rate') or 0),
        'pe_ttm': float(row.get('ttm_pe_rate') or 0),
        'amplitude_pct': round((high - low) / last * 100, 4) if last else 0.0,
        'mcap_yi': float(row.get('market_value') or 0) / 1e8,
        'float_mcap_yi': float(row.get('circulation_value') or 0) / 1e8,
        'pb': 0.0, 'limit_up': float(row.get('high_limit') or 0),
        'limit_down': float(row.get('low_limit') or 0),
        'vol_ratio': 0.0, 'pe_static': 0.0,
    }


def get_quotes(symbols: List[str]) -> Dict[str, dict]:
    pairs = [(str(symbol), _ts_code(symbol)) for symbol in symbols]
    pairs = [(symbol, code) for symbol, code in pairs if code]
    api = _get_api()
    if not pairs or api is None:
        return {}
    try:
        with _lock, _quiet_sdk_logs():
            df = api.rt_k(ts_code=','.join(code for _, code in pairs), fields='all')
        if df is None or df.empty:
            return {}
        out = {}
        for _, row in df.iterrows():
            code = ''.join(ch for ch in str(row.get('ts_code') or '') if ch.isdigit())[-6:]
            if code:
                try:
                    out[code] = _quote_dict(row.to_dict())
                except (TypeError, ValueError):
                    continue
        return out
    except Exception:
        return {}


def get_quote(symbol: str) -> Optional[dict]:
    code = ''.join(ch for ch in str(symbol) if ch.isdigit())[-6:]
    return get_quotes([symbol]).get(code)


def _reset_for_tests() -> None:
    global _api
    with _lock:
        _api = None

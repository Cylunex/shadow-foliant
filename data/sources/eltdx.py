"""生产兼容的 TDX 原子源，基于纯 Python 的 eltdx。

本模块只负责连接生命周期、分页与字段归一。节点可由仓库外环境变量指定；错误返回空值，
不记录节点、响应正文或行情内容。
"""

from __future__ import annotations

import os
import threading
from typing import Dict, List, Optional

import pandas as pd


_FREQUENCIES = {
    "1m": "1m", "1min": "1m",
    "5m": "5m", "5min": "5m",
    "15m": "15m", "15min": "15m",
    "30m": "30m", "30min": "30m",
    "60m": "60m", "60min": "60m", "1h": "60m",
    "1d": "day", "day": "day", "daily": "day", "d": "day", "101": "day",
    "1w": "week", "week": "week", "weekly": "week", "w": "week",
    "1mo": "month", "month": "month", "monthly": "month",
}
_INTRADAY = {"1m", "5m", "15m", "30m", "60m"}

_lock = threading.RLock()
_client = None
_hard_unavailable = False


def _enabled() -> bool:
    return os.getenv("TDX_USE_ELTDX", "true").lower() not in {"0", "false", "no"}


def _positive_int(value, default: int) -> int:
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return default


def _positive_float(value, default: float) -> float:
    try:
        return max(float(value), 0.1)
    except (TypeError, ValueError):
        return default


def _sdk_symbol(symbol: str) -> str:
    raw = str(symbol).lower().strip()
    digits = "".join(ch for ch in raw if ch.isdigit())[-6:]
    if len(digits) != 6:
        return ""
    if raw.startswith(("sh", "sz", "bj")):
        return raw[:2] + digits
    if digits.startswith(("4", "8", "92")):
        prefix = "bj"
    elif digits.startswith(("0", "2", "3")):
        prefix = "sz"
    else:
        prefix = "sh"
    return prefix + digits


def available() -> bool:
    """只检查开关与依赖，不为公共健康检查建立 TCP 连接。"""
    if not _enabled():
        return False
    try:
        from eltdx.client import TdxClient  # noqa: F401
        return True
    except Exception:
        return False


def _new_client():
    global _hard_unavailable
    if _hard_unavailable or not _enabled():
        return None
    try:
        from eltdx.client import TdxClient
    except Exception:
        _hard_unavailable = True
        return None

    kwargs = {
        "timeout": _positive_float(os.getenv("ELTDX_TIMEOUT"), 8.0),
        "pool_size": _positive_int(os.getenv("ELTDX_POOL_SIZE"), 2),
        "batch_size": min(_positive_int(os.getenv("ELTDX_BATCH_SIZE"), 80), 80),
    }
    hosts = [item.strip() for item in os.getenv("ELTDX_HOSTS", "").split(",")
             if item.strip()]
    if hosts:
        kwargs["hosts"] = hosts
    client = None
    try:
        client = TdxClient(**kwargs)
        client.connect()
        return client
    except Exception:
        try:
            if client is not None:
                client.close()
        except Exception:
            pass
        return None


def _get_client():
    global _client
    if _client is None:
        _client = _new_client()
    return _client


def _drop_client() -> None:
    global _client
    old, _client = _client, None
    if old is not None:
        try:
            old.close()
        except Exception:
            pass


def _standardize(rows, *, intraday: bool) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    try:
        dates = pd.to_datetime([row.time for row in rows], errors="coerce")
        # eltdx 返回 Asia/Shanghai aware datetime；DataHub 既有契约使用本地 naive 时间。
        try:
            dates = dates.tz_localize(None)
        except (TypeError, AttributeError):
            pass
        out = pd.DataFrame({
            "date": dates,
            "open": pd.to_numeric([row.open_price for row in rows], errors="coerce"),
            "high": pd.to_numeric([row.high_price for row in rows], errors="coerce"),
            "low": pd.to_numeric([row.low_price for row in rows], errors="coerce"),
            "close": pd.to_numeric([row.close_price for row in rows], errors="coerce"),
            # eltdx 0.5.1 实测日线与分钟线 volume 为“手”。
            "volume": pd.to_numeric([row.volume for row in rows], errors="coerce") * 100,
            "amount": pd.to_numeric([row.amount for row in rows], errors="coerce"),
        })
        if not intraday:
            out["date"] = out["date"].dt.normalize()
        return (out.dropna(subset=["date"]).drop_duplicates(subset=["date"], keep="last")
                .sort_values("date").reset_index(drop=True))
    except Exception:
        return pd.DataFrame()


def get_kline(symbol: str, frequency: str = "day", count: int = 800) -> pd.DataFrame:
    freq = _FREQUENCIES.get(str(frequency).strip().lower())
    code = _sdk_symbol(symbol)
    if freq is None or not code:
        return pd.DataFrame()
    max_bars = _positive_int(os.getenv("MARKET_DATA_MAX_BARS"), 3200)
    wanted = min(_positive_int(count, 800), max_bars)

    with _lock:
        client = _get_client()
        if client is None:
            return pd.DataFrame()
        try:
            rows = []
            for start in range(0, wanted, 800):
                size = min(800, wanted - start)
                response = client.get_kline(freq, code, start=start, count=size)
                chunk = list(getattr(response, "items", ()) or ())
                rows.extend(chunk)
                if len(chunk) < size:
                    break
            if not rows:
                _drop_client()
                return pd.DataFrame()
            return _standardize(rows, intraday=freq in _INTRADAY).tail(wanted).reset_index(drop=True)
        except Exception:
            _drop_client()
            return pd.DataFrame()


def _quote_dict(row) -> dict:
    price = float(getattr(row, "last_price", 0) or 0)
    last = float(getattr(row, "last_close_price", 0) or 0)
    high = float(getattr(row, "high_price", 0) or 0)
    low = float(getattr(row, "low_price", 0) or 0)
    change = price - last
    return {
        "name": "", "price": price, "last_close": last,
        "open": float(getattr(row, "open_price", 0) or 0), "high": high, "low": low,
        "change_amt": round(change, 4),
        "change_pct": round(change / last * 100, 4) if last else 0.0,
        "amount_wan": float(getattr(row, "amount", 0) or 0) / 1e4,
        "turnover_pct": 0.0, "pe_ttm": 0.0,
        "amplitude_pct": round((high - low) / last * 100, 4) if last else 0.0,
        "mcap_yi": 0.0, "float_mcap_yi": 0.0, "pb": 0.0,
        "limit_up": 0.0, "limit_down": 0.0, "vol_ratio": 0.0, "pe_static": 0.0,
    }


def get_quotes(symbols: List[str]) -> Dict[str, dict]:
    codes = [_sdk_symbol(symbol) for symbol in symbols]
    codes = [code for code in codes if code]
    if not codes:
        return {}
    with _lock:
        client = _get_client()
        if client is None:
            return {}
        try:
            rows = client.get_quote(codes)
            out = {}
            for row in rows or ():
                code = "".join(ch for ch in str(getattr(row, "code", "")) if ch.isdigit())[-6:]
                if not code:
                    continue
                try:
                    out[code] = _quote_dict(row)
                except (TypeError, ValueError):
                    continue
            return out
        except Exception:
            _drop_client()
            return {}


def get_quote(symbol: str) -> Optional[dict]:
    code = "".join(ch for ch in str(symbol) if ch.isdigit())[-6:]
    return get_quotes([symbol]).get(code)


def _reset_for_tests() -> None:
    global _hard_unavailable
    with _lock:
        _drop_client()
        _hard_unavailable = False

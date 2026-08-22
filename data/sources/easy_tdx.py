"""通达信公网行情原子源，基于 easy-tdx 的进程内客户端。

本模块只负责协议连接、分页和字段归一；跨源降级与缓存仍由 datahub 负责。
服务器地址由 easy-tdx 自带目录或仓库外环境变量提供，仓库不保存生产节点。
"""

from __future__ import annotations

import os
import threading
from typing import Dict, List, Optional

import _bootstrap
import pandas as pd


_INTERVALS = {
    "1m": "MIN_1", "1min": "MIN_1",
    "5m": "MIN_5", "5min": "MIN_5",
    "15m": "MIN_15", "15min": "MIN_15",
    "30m": "MIN_30", "30min": "MIN_30",
    "60m": "MIN_60", "60min": "MIN_60", "1h": "MIN_60",
    "1d": "DAY", "day": "DAY", "daily": "DAY", "d": "DAY", "101": "DAY",
    "1w": "WEEK", "week": "WEEK", "weekly": "WEEK", "w": "WEEK",
    "1mo": "MONTH", "month": "MONTH", "monthly": "MONTH",
}

_lock = threading.RLock()
_client = None
_hard_unavailable = False


def _enabled() -> bool:
    # 旧协议实现只作显式兼容；当前环境实测可连接但返回空数据，不能默认抢占源链。
    return os.getenv("TDX_USE_EASY_TDX", "false").lower() not in {"0", "false", "no"}


def _positive_int(value, default: int) -> int:
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return default


def _positive_float(value, default: float, minimum: float = 0.1) -> float:
    try:
        return max(float(value), minimum)
    except (TypeError, ValueError):
        return default


def _market_for(code: str):
    from easy_tdx import Market

    c = "".join(ch for ch in str(code) if ch.isdigit())[-6:]
    if c.startswith(("4", "8", "92")):
        return Market.BJ
    if c.startswith(("0", "2", "3")):
        return Market.SZ
    return Market.SH


def _new_client():
    """构造并连接客户端；配置缓存写入应用数据目录而非仓库或用户主目录。"""
    global _hard_unavailable
    if not _enabled() or _hard_unavailable:
        return None

    os.environ.setdefault(
        "EASY_TDX_CONFIG_DIR", os.path.join(_bootstrap.DB_DIR, "easy_tdx")
    )
    try:
        from easy_tdx import TdxClient
    except ImportError:
        _hard_unavailable = True
        return None

    timeout = _positive_float(os.getenv("EASY_TDX_TIMEOUT"), 8.0, 1.0)
    ping_timeout = _positive_float(os.getenv("EASY_TDX_PING_TIMEOUT"), 0.8)
    client = None
    try:
        if os.getenv("EASY_TDX_HOST", "").strip():
            client = TdxClient(timeout=timeout, auto_reconnect=True)
        else:
            client = TdxClient.from_best_host(
                timeout=timeout, ping_timeout=ping_timeout, auto_reconnect=True
            )
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


def available() -> bool:
    """只检查开关与依赖，不为健康检查建立外网连接。"""
    if not _enabled():
        return False
    try:
        import easy_tdx  # noqa: F401
        return True
    except Exception:
        return False


def _standardize(df: Optional[pd.DataFrame], *, intraday: bool) -> pd.DataFrame:
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()
    time_col = "datetime" if intraday else "date"
    required = {time_col, "open", "high", "low", "close", "vol"}
    if not required.issubset(df.columns):
        return pd.DataFrame()
    out = pd.DataFrame({
        "date": pd.to_datetime(df[time_col], errors="coerce"),
        "open": pd.to_numeric(df["open"], errors="coerce"),
        "high": pd.to_numeric(df["high"], errors="coerce"),
        "low": pd.to_numeric(df["low"], errors="coerce"),
        "close": pd.to_numeric(df["close"], errors="coerce"),
        # easy-tdx 的 SecurityBar.vol 契约已经是“股”。
        "volume": pd.to_numeric(df["vol"], errors="coerce"),
    })
    if "amount" in df.columns:
        out["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    if not intraday:
        out["date"] = out["date"].dt.normalize()
    return (out.dropna(subset=["date"]).drop_duplicates(subset=["date"], keep="last")
            .sort_values("date").reset_index(drop=True))


def get_kline(symbol: str, frequency: str = "day", count: int = 800,
              *, bar_time: str = "end") -> pd.DataFrame:
    """获取 K 线并自动分页；分钟时间默认使用 bar 右端点语义。"""
    key = str(frequency).strip().lower()
    category_name = _INTERVALS.get(key)
    if category_name is None or bar_time not in {"start", "end"}:
        return pd.DataFrame()
    max_bars = _positive_int(os.getenv("MARKET_DATA_MAX_BARS"), 3200)
    wanted = min(_positive_int(count, 800), max_bars)
    code = "".join(ch for ch in str(symbol) if ch.isdigit())[-6:]
    if len(code) != 6:
        return pd.DataFrame()

    with _lock:
        client = _get_client()
        if client is None:
            return pd.DataFrame()
        try:
            from easy_tdx import KlineCategory
            category = getattr(KlineCategory, category_name)
            market = _market_for(code)
            pages = []
            for start in range(0, wanted, 800):
                size = min(800, wanted - start)
                page = client.get_security_bars(
                    market, code, category, start, size, bar_time=bar_time
                )
                if page is None or page.empty:
                    break
                pages.append(page)
                if len(page) < size:
                    break
            if not pages:
                _drop_client()
                return pd.DataFrame()
            raw = pd.concat(pages, ignore_index=True)
            intraday = category_name.startswith("MIN_")
            return _standardize(raw, intraday=intraday).tail(wanted).reset_index(drop=True)
        except Exception:
            _drop_client()
            return pd.DataFrame()


def _quote_dict(row: dict) -> dict:
    price = float(row.get('price') or 0)
    last = float(row.get('pre_close') or 0)
    high = float(row.get('high') or 0)
    low = float(row.get('low') or 0)
    change = price - last
    return {
        'name': '', 'price': price, 'last_close': last,
        'open': float(row.get('open') or 0), 'high': high, 'low': low,
        'change_amt': round(change, 4),
        'change_pct': round(change / last * 100, 4) if last else 0.0,
        'amount_wan': float(row.get('amount') or 0) / 1e4,
        'turnover_pct': 0.0, 'pe_ttm': 0.0,
        'amplitude_pct': round((high - low) / last * 100, 4) if last else 0.0,
        'mcap_yi': 0.0, 'float_mcap_yi': 0.0, 'pb': 0.0,
        'limit_up': float(row.get('limit_up') or 0),
        'limit_down': float(row.get('limit_down') or 0),
        'vol_ratio': 0.0, 'pe_static': 0.0,
    }


def get_quotes(symbols: List[str]) -> Dict[str, dict]:
    """批量快照（协议每批最多 80 只）并归一为 DataHub quote 契约。"""
    codes = ["".join(ch for ch in str(s) if ch.isdigit())[-6:] for s in symbols]
    codes = [code for code in codes if len(code) == 6]
    if not codes:
        return {}
    with _lock:
        client = _get_client()
        if client is None:
            return {}
        try:
            out = {}
            for pos in range(0, len(codes), 80):
                batch = codes[pos:pos + 80]
                df = client.get_security_quotes([(_market_for(code), code) for code in batch])
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    code = str(row.get('code') or '')
                    if code:
                        try:
                            out[code] = _quote_dict(row.to_dict())
                        except (TypeError, ValueError):
                            continue
            return out
        except Exception:
            _drop_client()
            return {}


def get_quote(symbol: str) -> Optional[dict]:
    code = "".join(ch for ch in str(symbol) if ch.isdigit())[-6:]
    return get_quotes([code]).get(code)


def _reset_for_tests() -> None:
    """测试隔离用，不属于业务 API。"""
    global _hard_unavailable
    with _lock:
        _drop_client()
        _hard_unavailable = False

"""zzshare 原子数据源。

Token 仅从环境变量注入并由 SDK 放入请求头；本适配层不记录异常正文、请求头或返回数据。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import logging
import os
import threading
import time
from typing import Callable, Dict, List, Optional

import pandas as pd

from data.source_contracts import get_contract, source_call


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


def _safe_frame(endpoint: str, call: Callable[[], object]) -> pd.DataFrame:
    """Call the SDK inside the executable limit boundary without logging payloads."""
    contract = get_contract("zzshare", endpoint)
    for attempt in range(contract.retries + 1):
        try:
            with source_call("zzshare", endpoint), _lock, _quiet_sdk_logs():
                result = call()
            if isinstance(result, pd.DataFrame):
                return result
            if isinstance(result, dict):
                data = result.get("data", result)
                if isinstance(data, dict):
                    data = data.get("list", data.get("items", data.get("rows",
                        data.get("trade_days", data.get("dates", [])))))
                if isinstance(data, list) and data and not isinstance(data[0], dict):
                    data = [{"value": item} for item in data]
                return pd.DataFrame(data or [])
            return pd.DataFrame(result or [])
        except Exception:
            if attempt >= contract.retries:
                break
            time.sleep(min(2 ** attempt, 4))
    return pd.DataFrame()


def _with_provenance(df: pd.DataFrame, *, as_of: str, adjustment: str = "not_applicable",
                     unit: str = "provider_native", quality_status: str = "ok") -> pd.DataFrame:
    out = df.copy()
    out.attrs["provenance"] = {
        "provider": "zzshare",
        "origin": "provider_api",
        "as_of": str(as_of),
        "effective_at": str(as_of),
        "retrieved_at": datetime.now().astimezone().isoformat(),
        "adjustment": adjustment,
        "unit": unit,
        "schema_version": "1",
        "quality_status": quality_status,
    }
    return out


def get_kline(symbol: str, period_days: int = 365, frequency: str = "1d",
              adjust: str = "raw", count: Optional[int] = None) -> pd.DataFrame:
    """获取日线或分钟线，输出统一小写 OHLCVA 格式。"""
    code = _ts_code(symbol)
    api = _get_api()
    if not code or api is None:
        return pd.DataFrame()
    freq = str(frequency).strip().lower()
    if freq in _MINUTE_FREQ:
        wanted = min(_positive_int(count, 1000), 1000)
        df = _safe_frame(
            "minute", lambda: api.stk_mins(
                ts_code=code, freq=_MINUTE_FREQ[freq], count=wanted
            )
        )
        return _standardize(df, time_col="trade_time", volume_col="vol").tail(wanted)
    if freq not in {"1d", "day", "daily", "d", "101"}:
        return pd.DataFrame()
    end = datetime.now().date()
    start = end - timedelta(days=_positive_int(period_days, 365) + 10)
    wanted = min(
        _positive_int(count, _positive_int(os.getenv("MARKET_DATA_MAX_BARS"), 3200)),
        _positive_int(os.getenv("MARKET_DATA_MAX_BARS"), 3200),
    )
    contract = get_contract("zzshare", "daily_symbol")
    page_size = contract.page_size or 1000
    pages = []
    for offset in range(0, wanted, page_size):
        limit = min(page_size, wanted - offset)
        page = _safe_frame(
            "daily_symbol", lambda offset=offset, limit=limit: api.daily(
                ts_code=code,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                offset=offset,
                limit=limit,
                adj=adjust if adjust in {"qfq", "hfq"} else "",
            )
        )
        if page.empty:
            break
        pages.append(page)
        if len(page) < limit:
            break
    if not pages:
        return pd.DataFrame()
    return _standardize(
        pd.concat(pages, ignore_index=True), time_col="trade_date", volume_col="vol"
    ).tail(wanted)


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
    df = _safe_frame(
        "realtime", lambda: api.rt_k(
            ts_code=','.join(code for _, code in pairs), fields='all'
        )
    )
    if df.empty:
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


def get_quote(symbol: str) -> Optional[dict]:
    code = ''.join(ch for ch in str(symbol) if ch.isdigit())[-6:]
    return get_quotes([symbol]).get(code)


def get_security_master() -> pd.DataFrame:
    """Return the listed A-share universe, normalized but otherwise lossless."""
    api = _get_api()
    if api is None:
        return pd.DataFrame()
    df = _safe_frame(
        "security_master", lambda: api.stock_basic(
            exchange="ALL", list_status="L",
            fields="ts_code,symbol,name,area,industry,market,exchange,list_status,list_date,delist_date,is_hs"
        )
    )
    if df.empty:
        return df
    out = df.copy()
    code_col = next((c for c in ("ts_code", "code", "symbol") if c in out.columns), None)
    if not code_col:
        return pd.DataFrame()
    out["ts_code"] = out[code_col].map(_ts_code)
    out = out[out["ts_code"] != ""].drop_duplicates("ts_code", keep="last")
    return _with_provenance(out.reset_index(drop=True), as_of=datetime.now().date().isoformat())


def get_trade_days(start_date: str, end_date: str) -> List[str]:
    """Return normalized A-share trading dates within an inclusive range."""
    api = _get_api()
    if api is None:
        return []
    frame = _safe_frame(
        "trade_calendar", lambda: api.trade_days(
            day_start=str(start_date), day_end=str(end_date)
        )
    )
    if frame.empty:
        return []
    candidates = []
    for column in ("trade_date", "date", "day", "cal_date", "value"):
        if column in frame.columns:
            candidates.extend(frame[column].tolist())
    if not candidates and len(frame.columns) == 1:
        candidates.extend(frame.iloc[:, 0].tolist())
    out = {_iso for value in candidates if (_iso := _normalize_date(value))}
    return sorted(out)


def _normalize_date(value: object) -> str:
    try:
        return pd.Timestamp(value).date().isoformat()
    except Exception:
        return ""


def get_market_daily(trade_date: str, *, adjust: str = "qfq") -> pd.DataFrame:
    """Fetch one whole-market trading-day snapshot in a single bounded request."""
    api = _get_api()
    if api is None:
        return pd.DataFrame()
    date_text = str(trade_date).replace("-", "")
    contract = get_contract("zzshare", "daily_market")
    df = _safe_frame(
        "daily_market", lambda: api.daily(
            trade_date=date_text,
            offset=0,
            limit=contract.page_size,
            fields="all",
            adj=adjust if adjust in {"qfq", "hfq"} else "",
            export_all=True,
        )
    )
    if df.empty:
        return df
    if contract.hard_max_rows and len(df) >= contract.hard_max_rows:
        return _with_provenance(
            df, as_of=str(trade_date), adjustment=adjust, unit="price/currency/shares",
            quality_status="possibly_truncated",
        )
    return _with_provenance(
        df, as_of=str(trade_date), adjustment=adjust, unit="price/currency/shares"
    )


def get_valuation(trade_date: str) -> pd.DataFrame:
    """Fetch a whole-market valuation snapshot for a trading day."""
    api = _get_api()
    if api is None:
        return pd.DataFrame()
    df = _safe_frame("valuation", lambda: api.finance_valuation(str(trade_date)))
    return _with_provenance(df, as_of=str(trade_date), unit="provider_documented")


_FINANCE_TABLES = {"valuation", "indicator", "income", "balance", "cash_flow"}


def get_finance_pit(table: str, trade_date: str, codes: Optional[List[str]] = None) -> pd.DataFrame:
    """Fetch point-in-time fundamentals and independently enforce publication cutoff."""
    table = str(table).strip().lower()
    if table not in _FINANCE_TABLES:
        raise ValueError(f"unsupported finance table: {table}")
    api = _get_api()
    if api is None:
        return pd.DataFrame()
    normalized_codes = [_ts_code(code) for code in (codes or [])]
    kwargs = {"codes": ",".join(code for code in normalized_codes if code)} if codes else {}
    df = _safe_frame(
        "finance_pit", lambda: api.finance_pit(
            table=table, trade_date=str(trade_date), **kwargs
        )
    )
    if df.empty:
        return _with_provenance(df, as_of=str(trade_date))
    out = df.copy()
    pub_col = next((c for c in ("pubDate", "pub_date", "publish_date") if c in out.columns), None)
    if pub_col and table != "valuation":
        cutoff = pd.Timestamp(str(trade_date)).normalize()
        published = pd.to_datetime(out[pub_col], errors="coerce").dt.normalize()
        out = out[published.notna() & (published <= cutoff)].copy()
    return _with_provenance(out.reset_index(drop=True), as_of=str(trade_date))


def _reset_for_tests() -> None:
    global _api
    with _lock:
        _api = None

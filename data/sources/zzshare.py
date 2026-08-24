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


def _volume_multiplier(df: pd.DataFrame, volume_col: str) -> Optional[float]:
    """Validate shares/lots from amount ÷ volume ÷ price; never guess unknown units."""
    amount_col = next((name for name in ("amount", "turnover") if name in df.columns), None)
    if amount_col is None:
        return None
    try:
        volume = pd.to_numeric(df[volume_col], errors="coerce")
        amount = pd.to_numeric(df[amount_col], errors="coerce")
        close = pd.to_numeric(df["close"], errors="coerce")
        valid = (volume > 0) & (amount > 0) & (close > 0)
        if int(valid.sum()) >= 3:
            ratio = float((amount[valid] / volume[valid] / close[valid]).median())
            if 0.5 <= ratio <= 2.0:
                return 1.0
            if 50.0 <= ratio <= 200.0:
                return 100.0
    except Exception:
        pass
    return None


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
        "volume": (pd.to_numeric(df[volume_col], errors="coerce") * mult
                   if mult is not None else pd.Series(float("nan"), index=df.index)),
    })
    if "amount" in df.columns:
        out["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    elif "turnover" in df.columns:
        out["amount"] = pd.to_numeric(df["turnover"], errors="coerce")
    if time_col == "trade_date":
        out["date"] = out["date"].dt.normalize()
    out = (out.dropna(subset=["date"]).drop_duplicates(subset=["date"], keep="last")
           .sort_values("date").reset_index(drop=True))
    out.attrs["quality_status"] = "ok" if mult is not None else "unknown_unit"
    out.attrs["volume_unit"] = "shares" if mult is not None else "unknown"
    return out


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
            if isinstance(result, (list, tuple)):
                data = list(result)
                if data and not isinstance(data[0], dict):
                    data = [{"value": item} for item in data]
                return pd.DataFrame(data)
            return pd.DataFrame(result or [])
        except Exception:
            if attempt >= contract.retries:
                break
            time.sleep(min(2 ** attempt, 4))
    return pd.DataFrame()


def _call_shortcut_or_query(api, method: str, *, path: str,
                            kwargs: Optional[dict] = None,
                            query_kwargs: Optional[dict] = None):
    """Use a released shortcut when present, otherwise the SDK's generic query.

    zzshare documents several newer endpoints before every PyPI build exposes
    their generated shortcut methods.  Keeping the fallback here avoids a Git
    dependency while still using the SDK's authenticated, retrying transport.
    """
    params = dict(kwargs or {})
    shortcut = getattr(api, method, None)
    if callable(shortcut):
        return shortcut(**params)
    query = getattr(api, "query", None)
    if not callable(query):
        return []
    return query(path, params=dict(query_kwargs) if query_kwargs is not None else params)


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
        "schema_version": "3",
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
    # stock_basic was requested with list_status="L", but released SDK/API
    # combinations may encode an active listing as integer/string 1. Preserve
    # the provider value in the payload and expose one canonical store value.
    if "list_status" in out.columns:
        out["provider_list_status"] = out["list_status"]
    out["list_status"] = "L"
    out = out[out["ts_code"] != ""].drop_duplicates("ts_code", keep="last")
    return _with_provenance(out.reset_index(drop=True), as_of=datetime.now().date().isoformat())


def get_trade_calendar_evidence(start_date: str, end_date: str) -> List[tuple[str, bool]]:
    """Return only provider-explicit open/closed calendar observations.

    Absence is deliberately not converted into a closed day. Some released API
    variants return open days only, while others include an ``is_open`` column.
    """
    api = _get_api()
    if api is None:
        return []
    frame = _safe_frame(
        "trade_calendar", lambda: _call_shortcut_or_query(
            api, "trade_days", path="market/trade/days",
            kwargs={"day_start": str(start_date), "day_end": str(end_date)},
        )
    )
    if frame.empty:
        return []
    date_column = next(
        (column for column in ("trade_date", "date", "day", "cal_date", "value")
         if column in frame.columns),
        None,
    )
    if not date_column and len(frame.columns) == 1:
        date_column = str(frame.columns[0])
    if not date_column:
        return []
    open_column = next(
        (column for column in ("is_open", "is_trading_day", "open") if column in frame.columns),
        None,
    )
    rows: dict[str, bool] = {}
    for _, item in frame.iterrows():
        day = _normalize_date(item.get(date_column))
        if not day:
            continue
        if open_column:
            raw = str(item.get(open_column, "")).strip().lower()
            if raw in {"1", "true", "yes", "y", "open", "交易"}:
                rows[day] = True
            elif raw in {"0", "false", "no", "n", "closed", "休市"}:
                rows[day] = False
        else:
            rows[day] = True
    return sorted(rows.items())


def get_trade_days(start_date: str, end_date: str) -> List[str]:
    """Compatibility view containing provider-explicit open days only."""
    return [day for day, is_open in get_trade_calendar_evidence(start_date, end_date)
            if is_open]


def _normalize_date(value: object) -> str:
    try:
        parsed = pd.Timestamp(value)
        if pd.isna(parsed):
            return ""
        return parsed.date().isoformat()
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
    out = df.copy()
    volume_col = next((name for name in ("volume", "vol") if name in out.columns), None)
    multiplier = _volume_multiplier(out, volume_col) if volume_col else None
    if volume_col:
        out["volume"] = (pd.to_numeric(out[volume_col], errors="coerce") * multiplier
                         if multiplier is not None else float("nan"))
    quality_status = "ok" if multiplier is not None else "unknown_unit"
    unit = "price/currency/shares" if multiplier is not None else "price/currency/unknown"
    if contract.hard_max_rows and len(df) >= contract.hard_max_rows:
        return _with_provenance(
            out, as_of=str(trade_date), adjustment=adjust, unit=unit,
            quality_status="possibly_truncated",
        )
    return _with_provenance(
        out, as_of=str(trade_date), adjustment=adjust, unit=unit,
        quality_status=quality_status,
    )


def get_valuation(trade_date: str) -> pd.DataFrame:
    """Fetch a whole-market valuation snapshot for a trading day."""
    api = _get_api()
    if api is None:
        return pd.DataFrame()
    day = _normalize_date(trade_date)
    if not day:
        return pd.DataFrame()
    df = _safe_frame(
        "valuation", lambda: _call_shortcut_or_query(
            api, "finance_valuation",
            path=f"v3/fundamentals/valuation/{day}",
            kwargs={"date_value": day},
            query_kwargs={},
        )
    )
    if df.empty:
        return _with_provenance(df, as_of=day, unit="provider_documented")
    out = df.copy()
    effective_column = next(
        (column for column in (
            "trade_date", "tradeDate", "date", "as_of", "effective_date",
        ) if column in out.columns),
        None,
    )
    if effective_column:
        out["provider_effective_as_of"] = out[effective_column].map(_normalize_date)
        valid = out["provider_effective_as_of"].eq(day)
        out = out[valid].copy()
        quality = "ok" if not out.empty else "effective_date_mismatch"
    else:
        out["provider_effective_as_of"] = ""
        quality = "unknown_effective_date"
    return _with_provenance(
        out.reset_index(drop=True), as_of=day, unit="provider_documented",
        quality_status=quality,
    )


_FINANCE_TABLES = {"valuation", "indicator", "income", "balance", "cash_flow"}


def get_finance_pit(table: str, trade_date: str, codes: Optional[List[str]] = None) -> pd.DataFrame:
    """Fetch point-in-time fundamentals and independently enforce publication cutoff."""
    table = str(table).strip().lower()
    if table not in _FINANCE_TABLES:
        raise ValueError(f"unsupported finance table: {table}")
    api = _get_api()
    if api is None:
        return pd.DataFrame()
    day = _normalize_date(trade_date)
    if not day:
        return pd.DataFrame()
    normalized_codes = [_ts_code(code) for code in (codes or [])]
    query_params = {"codes": ",".join(code for code in normalized_codes if code)} if codes else {}
    shortcut_params = {"table": table, "trade_date": day, **query_params}
    df = _safe_frame(
        "finance_pit", lambda: _call_shortcut_or_query(
            api, "finance_pit", path=f"v3/fundamentals/{table}/pit/{day}",
            kwargs=shortcut_params,
            query_kwargs=query_params,
        )
    )
    if df.empty:
        return _with_provenance(df, as_of=day)
    out = df.copy()
    pub_col = next((c for c in ("pubDate", "pub_date", "publish_date") if c in out.columns), None)
    if pub_col and table != "valuation":
        cutoff = pd.Timestamp(day).normalize()
        published = pd.to_datetime(out[pub_col], errors="coerce").dt.normalize()
        out = out[published.notna() & (published <= cutoff)].copy()
    return _with_provenance(out.reset_index(drop=True), as_of=day)


def _reset_for_tests() -> None:
    global _api
    with _lock:
        _api = None

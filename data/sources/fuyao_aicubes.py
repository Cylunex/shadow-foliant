"""扶摇同花顺官方金融数据 REST 原子源。

API Key 只从运行时环境或受限文件读取；本模块不会记录请求头、响应正文、供应商
message 或密钥值。跨源路由和持久缓存仍由 ``datahub`` 负责。
"""
from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
import random
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from data.source_contracts import source_call


PROVIDER = "fuyao_aicubes"
DEFAULT_BASE_URL = "https://fuyao.aicubes.cn"
TZ = ZoneInfo("Asia/Shanghai")
_THSCODE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$", re.I)
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{6,80}$")
_RETRYABLE_CODES = {4001, 5001, 5002, 5003}
_EMPTY_CODES = {3001, 3002, 3004}
_PERMISSION_CODES = {2001, 2003}
_CACHE: Dict[tuple, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()
_SESSION = None
_LAST_STATUS: Dict[str, dict] = {}
_STATUS_LOCK = threading.Lock()


class FuyaoError(RuntimeError):
    """不含响应正文与凭据的安全错误基类。"""

    def __init__(self, category: str, *, code: Optional[int] = None,
                 request_id: Optional[str] = None,
                 http_status: Optional[int] = None):
        self.category = str(category)
        self.code = code
        self.request_id = _safe_request_id(request_id)
        self.http_status = int(http_status) if http_status is not None else None
        suffix = f":code={code}" if code is not None else ""
        super().__init__(f"fuyao_aicubes:{self.category}{suffix}")


class FuyaoAuthenticationError(FuyaoError):
    pass


class FuyaoPermissionError(FuyaoError):
    pass


class FuyaoRateLimitError(FuyaoError):
    pass


class FuyaoServiceError(FuyaoError):
    pass


class FuyaoContractError(FuyaoError):
    pass


def _safe_request_id(value: object) -> Optional[str]:
    text = str(value or "").strip()
    return text if _SAFE_REQUEST_ID.fullmatch(text) else None


def _flag(name: str, default: str = "true") -> bool:
    return str(os.getenv(name, default)).strip().lower() not in {
        "0", "false", "no", "off"
    }


def _read_secret_file(path_value: str, key_name: str) -> str:
    """Read one dotenv-style key without ever surfacing file content."""
    if not path_value:
        return ""
    try:
        path = Path(path_value).expanduser()
        if not path.is_file():
            return ""
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            separator = "=" if "=" in line else ":" if ":" in line else ""
            if not separator:
                continue
            key, value = line.split(separator, 1)
            if key.strip() == key_name:
                return value.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def api_key() -> str:
    direct = str(os.getenv("FUYAO_AICUBES_API_KEY") or "").strip()
    if direct:
        return direct
    return _read_secret_file(
        str(os.getenv("FUYAO_AICUBES_API_KEY_FILE") or "").strip(),
        str(os.getenv("FUYAO_AICUBES_SECRET_KEY", "fuyao-aicubes")).strip()
        or "fuyao-aicubes",
    )


def available() -> bool:
    return _flag("FUYAO_AICUBES_ENABLED") and bool(api_key())


def to_thscode(symbol: str) -> str:
    """项目证券代码/带前缀代码 → 官方完整 thscode。"""
    raw = str(symbol or "").strip().upper()
    match = _THSCODE.fullmatch(raw)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    for prefix in ("SH", "SZ", "BJ"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    digits = "".join(ch for ch in raw if ch.isdigit())[-6:]
    if len(digits) != 6:
        return ""
    if digits.startswith(("4", "8", "92")):
        suffix = "BJ"
    elif digits.startswith(("0", "1", "2", "3")):
        suffix = "SZ"
    else:
        suffix = "SH"
    return f"{digits}.{suffix}"


def from_thscode(thscode: str) -> str:
    match = _THSCODE.fullmatch(str(thscode or "").strip().upper())
    return match.group(1) if match else ""


def _base_url() -> str:
    configured = str(os.getenv("FUYAO_AICUBES_BASE_URL") or "").strip()
    return (configured or DEFAULT_BASE_URL).rstrip("/")


def _session():
    global _SESSION
    if _SESSION is None:
        import requests
        session = requests.Session()
        session.trust_env = False
        _SESSION = session
    return _SESSION


def _timeout() -> tuple[float, float]:
    def number(name: str, default: float) -> float:
        try:
            return min(60.0, max(0.1, float(os.getenv(name, str(default)))))
        except (TypeError, ValueError):
            return default
    return number("FUYAO_AICUBES_CONNECT_TIMEOUT_SECONDS", 5.0), number(
        "FUYAO_AICUBES_READ_TIMEOUT_SECONDS", 15.0
    )


def _cache_ttl(endpoint: str) -> int:
    defaults = {
        "calendar": 21600, "snapshot": 10, "historical": 43200,
        "valuation": 1800, "financials": 86400, "financial_indicators": 86400,
        "auction": 300, "special_data": 1800,
    }
    try:
        return max(0, min(86400, int(os.getenv(
            f"FUYAO_AICUBES_{endpoint.upper()}_CACHE_TTL_SECONDS",
            str(defaults.get(endpoint, 300)),
        ))))
    except (TypeError, ValueError):
        return defaults.get(endpoint, 300)


def _cache_get(key: tuple, ttl: int) -> Optional[dict]:
    if ttl <= 0:
        return None
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if item and time.monotonic() - item[0] < ttl:
            return dict(item[1])
    return None


def _cache_put(key: tuple, value: dict) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= 512:
            oldest = min(_CACHE, key=lambda entry: _CACHE[entry][0])
            _CACHE.pop(oldest, None)
        _CACHE[key] = (time.monotonic(), dict(value))


def _status(endpoint: str, status: str, **detail: object) -> None:
    safe = {key: value for key, value in detail.items()
            if key in {"code", "request_id", "as_of", "rows", "latency_ms",
                       "failure_category", "http_status"} and value is not None}
    with _STATUS_LOCK:
        _LAST_STATUS[endpoint] = {
            "status": status,
            "observed_at": datetime.now(TZ).isoformat(timespec="seconds"),
            **safe,
        }


def capability_status() -> dict:
    with _STATUS_LOCK:
        observed = {key: dict(value) for key, value in _LAST_STATUS.items()}
    return {
        "provider": PROVIDER,
        "configured": bool(api_key()),
        "enabled": _flag("FUYAO_AICUBES_ENABLED"),
        "capabilities": observed,
        "capital_flow": {
            "status": "degraded",
            "reason": "official_documentation_marks_external_access_unavailable",
        },
    }


def _retry_delay(attempt: int, response=None) -> float:
    retry_after = None
    try:
        retry_after = float((response.headers or {}).get("Retry-After"))
    except (AttributeError, TypeError, ValueError):
        retry_after = None
    base = max(0.5 * (2 ** attempt), retry_after or 0.0)
    return min(30.0, base + random.uniform(0.0, min(0.25, base * 0.2)))


def _request(endpoint: str, path: str, params: Optional[dict] = None,
             *, use_cache: bool = True) -> dict:
    key_value = api_key()
    if not _flag("FUYAO_AICUBES_ENABLED"):
        _status(endpoint, "disabled", failure_category="disabled")
        raise FuyaoPermissionError("disabled")
    if not key_value:
        _status(endpoint, "degraded", code=2001,
                failure_category="not_configured")
        raise FuyaoAuthenticationError("not_configured", code=2001)
    normalized_params = tuple(sorted((str(k), str(v)) for k, v in (params or {}).items()))
    cache_key = (endpoint, path, normalized_params)
    cached = _cache_get(cache_key, _cache_ttl(endpoint)) if use_cache else None
    if cached is not None:
        return cached
    from data.source_contracts import get_contract
    contract = get_contract(PROVIDER, endpoint)
    started = time.monotonic()
    last_error: Optional[Exception] = None
    # One logical call owns one admission slot for its entire bounded retry
    # sequence. This prevents an intermediate 429 from opening a cooldown that
    # suppresses the already-authorized backoff retry.
    try:
        with source_call(PROVIDER, endpoint):
            for attempt in range(contract.retries + 1):
                response = None
                try:
                    response = _session().get(
                        _base_url() + path,
                        params=params or {},
                        headers={"X-api-key": key_value, "Accept": "application/json"},
                        timeout=_timeout(),
                    )
                    status_code = int(getattr(response, "status_code", 0) or 0)
                    if status_code in {401, 403}:
                        error_type = (FuyaoAuthenticationError if status_code == 401
                                      else FuyaoPermissionError)
                        raise error_type("http_permission", code=status_code,
                                         http_status=status_code)
                    if status_code == 429:
                        raise FuyaoRateLimitError("http_rate_limited", code=4001,
                                                  http_status=status_code)
                    if status_code < 200 or status_code >= 300:
                        if status_code >= 500:
                            raise FuyaoServiceError("http_status", code=status_code,
                                                    http_status=status_code)
                        raise FuyaoContractError("http_status", code=status_code,
                                                 http_status=status_code)
                    try:
                        payload = response.json()
                    except Exception as exc:
                        raise FuyaoContractError(
                            "invalid_json", http_status=status_code
                        ) from exc
                    if not isinstance(payload, dict) or not isinstance(payload.get("code"), int):
                        raise FuyaoContractError(
                            "invalid_envelope", http_status=status_code
                        )
                    code = int(payload["code"])
                    request_id = _safe_request_id(payload.get("request_id"))
                    if code == 2001:
                        raise FuyaoAuthenticationError("authentication", code=code,
                                                       request_id=request_id,
                                                       http_status=status_code)
                    if code == 2003:
                        raise FuyaoPermissionError("permission", code=code,
                                                   request_id=request_id,
                                                   http_status=status_code)
                    if code in _EMPTY_CODES:
                        _status(endpoint, "degraded", code=code, request_id=request_id,
                                failure_category="empty_business_result",
                                http_status=status_code)
                        return {"code": code, "request_id": request_id, "data": None}
                    if code == 4001:
                        raise FuyaoRateLimitError("business_rate_limited", code=code,
                                                  request_id=request_id,
                                                  http_status=status_code)
                    if code in {5001, 5002, 5003}:
                        raise FuyaoServiceError("upstream_unavailable", code=code,
                                                request_id=request_id,
                                                http_status=status_code)
                    if code != 0:
                        raise FuyaoContractError("business_error", code=code,
                                                 request_id=request_id,
                                                 http_status=status_code)
                    data = payload.get("data")
                    if data is not None and not isinstance(data, dict):
                        raise FuyaoContractError(
                            "invalid_data", request_id=request_id,
                            http_status=status_code,
                        )
                    safe_payload = {"code": 0, "request_id": request_id,
                                    "data": dict(data or {})}
                    elapsed = round((time.monotonic() - started) * 1000)
                    _status(endpoint, "ok", request_id=request_id,
                            rows=len((safe_payload["data"].get("item") or [])),
                            latency_ms=elapsed, http_status=status_code)
                    if use_cache:
                        _cache_put(cache_key, safe_payload)
                    return safe_payload
                except (_PERMISSION_ERROR_TYPES) as exc:
                    _status(endpoint, "degraded", code=exc.code,
                            request_id=exc.request_id,
                            failure_category=exc.category,
                            http_status=exc.http_status)
                    raise
                except (FuyaoRateLimitError, FuyaoServiceError) as exc:
                    last_error = exc
                    _status(endpoint, "degraded", code=exc.code,
                            request_id=exc.request_id,
                            failure_category=exc.category,
                            http_status=exc.http_status)
                    if attempt >= contract.retries:
                        raise
                    time.sleep(_retry_delay(attempt, response))
                except FuyaoError as exc:
                    _status(endpoint, "degraded", code=exc.code,
                            request_id=exc.request_id,
                            failure_category=exc.category,
                            http_status=exc.http_status)
                    raise
                except Exception as exc:
                    # A requests exception string can contain URL parameters.
                    if exc.__class__.__name__ in {
                        "Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError"
                    }:
                        last_error = FuyaoServiceError("transport_unavailable")
                        _status(endpoint, "degraded",
                                failure_category="transport_unavailable")
                        if attempt < contract.retries:
                            time.sleep(_retry_delay(attempt, response))
                            continue
                        raise last_error from None
                    last_error = exc
                    _status(endpoint, "degraded", failure_category="unexpected_error")
                    raise
    except FuyaoError:
        raise
    except Exception:
        _status(endpoint, "degraded",
                failure_category="source_admission_or_unexpected_error")
        raise
    raise last_error or FuyaoServiceError("unavailable")


_PERMISSION_ERROR_TYPES = (FuyaoAuthenticationError, FuyaoPermissionError)


def _safe_call(endpoint: str, path: str, params: Optional[dict] = None,
               *, use_cache: bool = True) -> Optional[dict]:
    try:
        return _request(endpoint, path, params, use_cache=use_cache)
    except FuyaoError:
        return None
    except Exception:
        _status(endpoint, "degraded", failure_category="unexpected_error")
        return None


def _timestamp(value: object) -> Optional[pd.Timestamp]:
    try:
        stamp = pd.to_datetime(value, unit="ms", utc=True)
        return stamp.tz_convert(TZ)
    except (TypeError, ValueError, OverflowError):
        return None


def _timestamp_iso(value: object) -> str:
    stamp = _timestamp(value)
    return stamp.isoformat() if stamp is not None else ""


def snapshot_freshness(timestamp_ms: object, *, now: Optional[datetime] = None) -> dict:
    """盘中按年龄判断；当日收盘快照在盘后始终属于 closing_current。"""
    now = now or datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    else:
        now = now.astimezone(TZ)
    stamp = _timestamp(timestamp_ms)
    if stamp is None:
        return {"freshness": "unknown", "stale": True, "market_as_of": None}
    market_as_of = stamp.date().isoformat()
    if stamp.date() == now.date() and stamp.hour >= 15 and now.hour >= 15:
        return {"freshness": "closing_current", "stale": False,
                "market_as_of": market_as_of}
    trading = now.weekday() < 5 and (
        (now.hour == 9 and now.minute >= 15) or 10 <= now.hour < 15
        or (now.hour == 15 and now.minute <= 5)
    )
    age = max(0.0, (now - stamp.to_pydatetime()).total_seconds())
    limit = 300.0 if trading else 1800.0
    stale = age > limit
    return {"freshness": "stale" if stale else "live_current", "stale": stale,
            "market_as_of": market_as_of}


def _chunks(values: List[str], size: int = 100) -> Iterable[List[str]]:
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def get_quotes(symbols: List[str], *, use_cache: bool = True) -> Dict[str, dict]:
    codes = list(dict.fromkeys(to_thscode(value) for value in symbols))
    codes = [code for code in codes if code]
    output: Dict[str, dict] = {}
    for chunk in _chunks(codes):
        envelope = _safe_call("snapshot", "/api/a-share/prices/snapshot",
                              {"thscodes": ",".join(chunk)}, use_cache=use_cache)
        if not envelope or envelope.get("code") != 0:
            continue
        data = envelope.get("data") or {}
        stamp_ms = data.get("timestamp")
        source_time = _timestamp_iso(stamp_ms)
        fresh = snapshot_freshness(stamp_ms)
        request_id = envelope.get("request_id")
        for item in data.get("item") or []:
            if not isinstance(item, dict):
                continue
            code = from_thscode(item.get("thscode")) or str(item.get("ticker") or "")
            if len(code) != 6 or code in output:
                continue
            try:
                last_price = float(item.get("last_price"))
            except (TypeError, ValueError):
                continue
            previous = item.get("prev_price")
            high = item.get("high_price")
            low = item.get("low_price")
            def number(value: object, default: float = 0.0) -> float:
                try:
                    return float(value) if value is not None else default
                except (TypeError, ValueError):
                    return default
            previous_value = number(previous)
            high_value, low_value = number(high), number(low)
            output[code] = {
                "code": code, "name": "", "price": last_price,
                "last_close": previous_value, "open": number(item.get("open_price")),
                "high": high_value, "low": low_value,
                "change_amt": number(item.get("price_change")),
                "change_pct": number(item.get("price_change_ratio_pct")),
                "volume": number(item.get("volume")),
                "amount": number(item.get("turnover")),
                "amount_wan": number(item.get("turnover")) / 1e4,
                "turnover_pct": 0.0, "pe_ttm": 0.0, "pb": 0.0,
                "mcap_yi": 0.0, "float_mcap_yi": 0.0, "vol_ratio": 0.0,
                "amplitude_pct": ((high_value - low_value) / previous_value * 100
                                  if previous_value else 0.0),
                "provider": PROVIDER, "source": PROVIDER,
                "request_id": request_id, "source_timestamp": source_time,
                "quote_time": source_time, "currency": "CNY", "adjustment": "raw",
                **fresh,
            }
    return {from_thscode(code): output[from_thscode(code)] for code in codes
            if from_thscode(code) in output}


_PERIOD_DAYS = {"1mo": 31, "3mo": 93, "6mo": 186, "1y": 366,
                "2y": 731, "3y": 1096, "5y": 1827}


def get_kline(symbol: str, period: str = "1y", interval: str = "1d",
              adjust: str = "raw", *, use_cache: bool = True) -> pd.DataFrame:
    thscode = to_thscode(symbol)
    if not thscode or str(interval).lower() not in {"1d", "day", "daily", "101"}:
        return pd.DataFrame()
    end = datetime.now(TZ)
    days = _PERIOD_DAYS.get(str(period), 366)
    start = end - timedelta(days=min(days + 10, 3652))
    adjustment = {"raw": "none", "qfq": "forward", "hfq": "backward"}.get(
        str(adjust).lower(), "none"
    )
    envelope = _safe_call("historical", "/api/a-share/prices/historical", {
        "thscode": thscode, "interval": "1d",
        "start": int(start.timestamp() * 1000), "end": int(end.timestamp() * 1000),
        "adjust": adjustment,
    }, use_cache=use_cache)
    if not envelope or envelope.get("code") != 0:
        return pd.DataFrame()
    data = envelope.get("data") or {}
    rows = data.get("item") or []
    if not rows:
        return pd.DataFrame()
    raw = pd.DataFrame(rows)
    required = {"date_ms", "open_price", "high_price", "low_price", "close_price", "volume"}
    if not required.issubset(raw.columns):
        return pd.DataFrame()
    out = pd.DataFrame({
        "Date": pd.to_datetime(raw["date_ms"], unit="ms", utc=True,
                               errors="coerce").dt.tz_convert(TZ).dt.tz_localize(None).dt.normalize(),
        "Open": pd.to_numeric(raw["open_price"], errors="coerce"),
        "High": pd.to_numeric(raw["high_price"], errors="coerce"),
        "Low": pd.to_numeric(raw["low_price"], errors="coerce"),
        "Close": pd.to_numeric(raw["close_price"], errors="coerce"),
        "Volume": pd.to_numeric(raw["volume"], errors="coerce"),
    }).dropna(subset=["Date", "Close"]).drop_duplicates("Date", keep="last")
    out = out.set_index("Date").sort_index()
    out.index.name = "Date"
    source_time = _timestamp_iso(data.get("timestamp"))
    out.attrs["provenance"] = {
        "provider": PROVIDER, "request_id": envelope.get("request_id"),
        "source_timestamp": source_time,
        "market_as_of": out.index.max().date().isoformat() if not out.empty else None,
        "adjustment": adjustment, "currency": "CNY", "freshness": "historical",
        "quality_status": "ok", "volume_unit": "shares",
    }
    return out


def get_trade_calendar_evidence(start_date: str, end_date: str,
                                *, use_cache: bool = True) -> List[tuple[str, bool]]:
    envelope = _safe_call("calendar", "/api/a-share/calendar/trading-days",
                          use_cache=use_cache)
    if not envelope or envelope.get("code") != 0:
        return []
    open_days = set()
    for item in (envelope.get("data") or {}).get("item") or []:
        if not isinstance(item, dict):
            continue
        try:
            open_days.add(pd.Timestamp(str(item.get("date"))).date().isoformat())
        except (TypeError, ValueError):
            continue
    try:
        requested = pd.date_range(pd.Timestamp(start_date).date(),
                                  pd.Timestamp(end_date).date(), freq="D")
    except (TypeError, ValueError):
        return []
    if not open_days:
        return []
    # Only open days are returned. A one-week closed-day margin avoids rejecting
    # a request that begins or ends on a weekend, without inventing evidence
    # outside the documented rolling-year window.
    earliest = pd.Timestamp(min(open_days)).date() - timedelta(days=7)
    latest = pd.Timestamp(max(open_days)).date() + timedelta(days=7)
    today = datetime.now(TZ).date()
    if requested[0].date() < earliest or requested[-1].date() > min(latest, today):
        return []
    return [(day.date().isoformat(), day.date().isoformat() in open_days) for day in requested]


def get_valuations(symbols: List[str], *, use_cache: bool = True) -> pd.DataFrame:
    codes = list(dict.fromkeys(to_thscode(value) for value in symbols))
    codes = [code for code in codes if code]
    frames = []
    for chunk in _chunks(codes):
        envelope = _safe_call("valuation", "/api/a-share/valuations/snapshot",
                              {"thscodes": ",".join(chunk)}, use_cache=use_cache)
        if not envelope or envelope.get("code") != 0:
            continue
        data = envelope.get("data") or {}
        stamp = _timestamp(data.get("timestamp"))
        day = stamp.date().isoformat() if stamp is not None else ""
        rows = []
        for item in data.get("item") or []:
            if not isinstance(item, dict):
                continue
            rows.append({
                "symbol": from_thscode(item.get("thscode")) or item.get("ticker"),
                "trade_date": day, "provider_effective_as_of": day,
                "observed_at": stamp.isoformat() if stamp is not None else "",
                "name": item.get("name"), "pe_ttm": item.get("pe_ttm"),
                "pe_mrq": item.get("pe_mrq"), "pb": item.get("pb_mrq"),
                "ps": item.get("ps_ttm"), "pcf": item.get("pcf_ttm"),
                "pe_basis": "TTM", "market_cap_unit": "CNY_100M",
                "currency": "CNY", "request_id": envelope.get("request_id"),
            })
        if rows:
            frames.append(pd.DataFrame(rows))
    result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    result.attrs["provenance"] = {
        "provider": PROVIDER, "quality_status": "ok" if not result.empty else "unavailable",
        "origin": "provider_api", "adjustment": "not_applicable",
        "currency": "CNY", "schema_version": "1",
    }
    return result


_FINANCIAL_PATHS = {
    "lrb": "/api/a-share/financials/income-statements",
    "income": "/api/a-share/financials/income-statements",
    "fzb": "/api/a-share/financials/balance-sheets",
    "balance": "/api/a-share/financials/balance-sheets",
    "llb": "/api/a-share/financials/cash-flow-statements",
    "cash_flow": "/api/a-share/financials/cash-flow-statements",
}


def get_financials(symbol: str, report_type: str = "lrb", *, period: str = "quarterly",
                   limit: int = 8, use_cache: bool = True) -> List[dict]:
    thscode = to_thscode(symbol)
    path = _FINANCIAL_PATHS.get(str(report_type).lower())
    if not thscode or not path or period not in {"annual", "quarterly"}:
        return []
    envelope = _safe_call("financials", path, {
        "thscode": thscode, "period": period, "limit": min(20, max(1, int(limit))),
    }, use_cache=use_cache)
    if not envelope or envelope.get("code") != 0:
        return []
    source_timestamp = _timestamp_iso((envelope.get("data") or {}).get("timestamp"))
    output = []
    for item in (envelope.get("data") or {}).get("item") or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row.update({
            "code": from_thscode(item.get("thscode")) or item.get("ticker"),
            "provider": PROVIDER, "request_id": envelope.get("request_id"),
            "source_timestamp": source_timestamp, "currency": item.get("currency") or "CNY",
            "quality_status": "ok",
        })
        output.append(row)
    return output


def get_financial_indicators(symbol: str, report: str,
                             *, use_cache: bool = True) -> dict:
    thscode = to_thscode(symbol)
    if not thscode or not re.fullmatch(r"\d{4}-[1-4]", str(report)):
        return {"status": "invalid_request", "data": {}}
    envelope = _safe_call("financial_indicators",
                          "/api/a-share/financials/indicators",
                          {"thscode": thscode, "report": report},
                          use_cache=use_cache)
    if not envelope or envelope.get("code") != 0:
        return {"status": "degraded", "data": {}}
    data = envelope.get("data") or {}
    flat = {}
    for ability in data.get("abilities") or []:
        for item in ability.get("indicators") or [] if isinstance(ability, dict) else []:
            if isinstance(item, dict) and item.get("index_id"):
                flat[str(item["index_id"])] = item.get("value")
    return {
        "status": "ok", "provider": PROVIDER, "request_id": envelope.get("request_id"),
        "thscode": data.get("thscode"), "report": data.get("report"), "data": flat,
    }


def get_auction_snapshot(symbols: List[str], *, stage: str = "final",
                         use_cache: bool = True) -> dict:
    codes = list(dict.fromkeys(to_thscode(value) for value in symbols))
    codes = [code for code in codes if code]
    if not codes or stage not in {"live", "final"}:
        return {"status": "invalid_request", "items": []}
    items, statuses, requests = [], [], []
    for chunk in _chunks(codes):
        envelope = _safe_call("auction", "/api/a-share/auction/snapshot", {
            "thscodes": ",".join(chunk), "stage": stage,
        }, use_cache=use_cache)
        if not envelope or envelope.get("code") != 0:
            statuses.append("degraded")
            continue
        data = envelope.get("data") or {}
        statuses.append(str(data.get("data_status") or "unknown"))
        if envelope.get("request_id"):
            requests.append(envelope["request_id"])
        stamp = _timestamp_iso(data.get("timestamp"))
        for item in data.get("item") or []:
            if isinstance(item, dict):
                items.append({**item, "code": from_thscode(item.get("thscode")),
                              "provider": PROVIDER, "source_timestamp": stamp,
                              "currency": "CNY"})
    status = "ok" if items else (statuses[0] if statuses else "degraded")
    return {"status": status, "stage": stage, "items": items,
            "request_ids": requests}


def get_special_data(kind: str, **params: object) -> dict:
    allowed = {
        "limit_up_pool": "limit-up-pool", "limit_down_pool": "limit-down-pool",
        "limit_break_pool": "limit-break-pool", "limit_up_ladder": "limit-up-ladder",
        "hot_stock_list": "hot-stock-list", "skyrocket_list": "skyrocket-list",
        "hot_stock_list_history": "hot-stock-list-history",
        "hot_stock_rank_trend": "hot-stock-rank-trend",
        "anomaly_analysis_list": "anomaly-analysis-list",
        "dragon_tiger_list": "dragon-tiger-list",
    }
    slug = allowed.get(str(kind))
    if not slug:
        return {"status": "unsupported", "items": []}
    envelope = _safe_call("special_data", f"/api/a-share/special-data/{slug}",
                          {k: v for k, v in params.items() if v is not None})
    if not envelope or envelope.get("code") != 0:
        return {"status": "degraded", "items": []}
    data = envelope.get("data") or {}
    items = data.get("item")
    if items is None and slug == "dragon-tiger-list":
        items = [*(data.get("stock_items") or []), *(data.get("hot_money_items") or [])]
    return {"status": "ok", "provider": PROVIDER,
            "request_id": envelope.get("request_id"),
            "source_timestamp": _timestamp_iso(data.get("timestamp")),
            "items": items or [], "pagination": data.get("pagination")}


def _reset_for_tests() -> None:
    global _SESSION
    _SESSION = None
    with _CACHE_LOCK:
        _CACHE.clear()
    with _STATUS_LOCK:
        _LAST_STATUS.clear()

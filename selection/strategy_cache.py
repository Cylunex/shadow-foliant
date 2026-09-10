"""问财策略选股结果的「当日文件缓存」—— 供盘前预热 + 09:45 综合选股读暖,避开问财高峰/熔断。

仿 main_force_selector 的当日缓存,但通用于 低价擒牛 / 小市值 / 净利增长 / 低估值 等问财策略
(主力资金已有自带缓存,不走这里)。key = 策略名 + 当日日期 → 跨交易日自然失效(不做历史回退:
隔日的选股结论会误导,与 K线历史 bar 不同,故不像 datahub.kline 那样"失败用历史")。

用法:
  - 盘前预热:cached(name, fetch_fn, use_cache=False)  强制现取 + 回写当日缓存
  - 09:45 选股:cached(name, fetch_fn, use_cache=True)  命中当日缓存即返回,不在高峰现调问财
  fetch_fn() 须返回 (ok: bool, df: DataFrame|None, msg: str),与各选股器 get_*_stocks 同形。
"""
import json
import os
import pickle
from datetime import date, datetime

try:
    import _bootstrap  # noqa: F401  路径引导(项目根)
except Exception:
    _bootstrap = None


def _cache_dir() -> str:
    try:
        if _bootstrap is not None:
            d = _bootstrap.db_path('strategy_cache')
        else:
            raise RuntimeError
    except Exception:
        d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'db', 'strategy_cache')
    os.makedirs(d, exist_ok=True)
    return d


def _key(name: str) -> str:
    return f"{name}_{date.today().isoformat()}"


def load(name: str):
    """命中当日缓存返回 DataFrame,否则 None。任何异常吞掉返回 None。"""
    try:
        p = os.path.join(_cache_dir(), _key(name) + '.pkl')
        if os.path.isfile(p):
            with open(p, 'rb') as f:
                return pickle.load(f)
    except Exception:
        pass
    return None


def save(name: str, df) -> bool:
    try:
        if df is None or not hasattr(df, 'empty') or df.empty:
            return False
        with open(os.path.join(_cache_dir(), _key(name) + '.pkl'), 'wb') as f:
            pickle.dump(df, f)
        failure_path = os.path.join(_cache_dir(), _key(name) + '.failure.json')
        if os.path.isfile(failure_path):
            os.unlink(failure_path)
        return True
    except Exception:
        return False


def classify_failure(value) -> str:
    """将外部问财异常压缩成稳定失败码，不存储请求参数或凭据。"""
    text = f"{type(value).__name__}:{value}".lower()
    if "缓存缺失" in text or "cache" in text and "miss" in text:
        return "cache_missing"
    if "403" in text or "forbidden" in text:
        return "http_403"
    if "429" in text or "too many" in text or "rate limit" in text:
        return "http_429"
    if "401" in text or "unauthorized" in text:
        return "http_401"
    if "熔断" in text or "circuit" in text:
        return "circuit_open"
    if "上次请求仍未结束" in text or "inflight" in text:
        return "inflight_busy"
    if "超时" in text or "timeout" in text:
        return "timeout"
    if "未安装" in text or "modulenotfound" in text or "importerror" in text:
        return "dependency_missing"
    if "无数据" in text or "empty" in text:
        return "empty_result"
    return "provider_error"


def record_failure(name: str, value, *, code: str = "") -> dict:
    """持久化当日最后一次失败，供 09:45 缓存只读阶段还原真实原因。"""
    payload = {
        "strategy": str(name),
        "trade_date": date.today().isoformat(),
        "failure_code": code or classify_failure(value),
        "detail": str(value or "")[:300],
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    path = os.path.join(_cache_dir(), _key(name) + ".failure.json")
    temporary = path + f".{os.getpid()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        os.replace(temporary, path)
    except Exception:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except Exception:
            pass
    return payload


def load_failure(name: str):
    try:
        path = os.path.join(_cache_dir(), _key(name) + ".failure.json")
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        if value.get("trade_date") == date.today().isoformat():
            return value
    except Exception:
        pass
    return None


def clear_failure(name: str) -> None:
    try:
        path = os.path.join(_cache_dir(), _key(name) + ".failure.json")
        if os.path.isfile(path):
            os.unlink(path)
    except Exception:
        pass


def cached(name: str, fetch_fn, use_cache: bool = True, cache_only: bool = False):
    """返回 (ok, df, msg)。
    use_cache=True 且当日缓存命中 → 直接返回缓存；否则默认调 fetch_fn() 现取并回写。
    cache_only=True 用于 09:45 主选股：盘前 09:15/09:30 已尝试两轮，缓存冷时不在高峰第三次
    请求问财，避免重复 403、熔断和误告警。"""
    if use_cache:
        df = load(name)
        if df is not None and hasattr(df, 'empty') and not df.empty:
            return True, df, f'{name} 当日缓存命中({len(df)}只)'
    if cache_only:
        failure = load_failure(name) or {}
        suffix = (f";last_failure={failure.get('failure_code')}:{failure.get('detail')}"
                  if failure else "")
        return False, None, f'{name} 当日缓存缺失(09:45不重复请求外部源){suffix}'
    try:
        ok, df, msg = fetch_fn()
    except Exception as exc:
        record_failure(name, exc)
        raise
    if ok:
        save(name, df)
    else:
        record_failure(name, msg or "empty_result")
    return ok, df, msg

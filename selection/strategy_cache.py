"""问财策略选股结果的「当日文件缓存」—— 供盘前预热 + 09:45 综合选股读暖,避开问财高峰/熔断。

仿 main_force_selector 的当日缓存,但通用于 低价擒牛 / 小市值 / 净利增长 / 低估值 等问财策略
(主力资金已有自带缓存,不走这里)。key = 策略名 + 当日日期 → 跨交易日自然失效(不做历史回退:
隔日的选股结论会误导,与 K线历史 bar 不同,故不像 datahub.kline 那样"失败用历史")。

用法:
  - 盘前预热:cached(name, fetch_fn, use_cache=False)  强制现取 + 回写当日缓存
  - 09:45 选股:cached(name, fetch_fn, use_cache=True)  命中当日缓存即返回,不在高峰现调问财
  fetch_fn() 须返回 (ok: bool, df: DataFrame|None, msg: str),与各选股器 get_*_stocks 同形。
"""
import os
import pickle
from datetime import date

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
        return True
    except Exception:
        return False


def cached(name: str, fetch_fn, use_cache: bool = True):
    """返回 (ok, df, msg)。
    use_cache=True 且当日缓存命中 → 直接返回缓存;否则调 fetch_fn() 现取并(成功则)回写当日缓存。"""
    if use_cache:
        df = load(name)
        if df is not None and hasattr(df, 'empty') and not df.empty:
            return True, df, f'{name} 当日缓存命中({len(df)}只)'
    ok, df, msg = fetch_fn()
    if ok:
        save(name, df)
    return ok, df, msg

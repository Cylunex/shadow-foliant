"""
自限流器 —— 给易被封的外部接口(东财/腾讯/新浪/同花顺/巨潮/akshare 等)做客户端节流。

目的:批量扫描/盯盘会高频请求同一数据源,容易触发风控封 IP。本模块在**调用方主动**
按"每个数据源最小请求间隔"排队,平滑请求节奏,降低封禁风险。

特性:
  - 线程安全(autostart 会起多个后台线程并发请求)
  - 按 host/source 分键,各源独立间隔
  - 间隔可用环境变量覆盖:RATE_LIMIT_<KEY>(秒),或 RATE_LIMIT_DEFAULT
  - 带随机抖动,避免请求整齐撞车
  - 提供 throttle(key) / throttled_session(session) / @throttled 装饰器

用法:
    from rate_limiter import throttle, throttled_session
    throttle('eastmoney')              # 阻塞到距上次 eastmoney 请求满最小间隔
    throttled_session(my_requests_session)   # 包装后所有 .get/.post 自动按 host 限流
"""

from __future__ import annotations
import os
import time
import random
import threading
from urllib.parse import urlparse
from functools import wraps

# 各数据源默认最小请求间隔(秒)。偏保守,可被环境变量覆盖。
_DEFAULTS = {
    'eastmoney': 0.35,   # 东财 push2 / datacenter
    'tencent': 0.35,     # qt.gtimg.cn
    'sina': 0.5,         # hq.sinajs.cn / 财务
    'ths': 0.6,          # 同花顺 10jqka
    'cninfo': 0.6,       # 巨潮
    'akshare': 0.4,      # akshare(底层多为东财/新浪)
    'tushare': 0.5,
    'pywencai': 1.0,     # 问财较敏感,放慢
    'eastmoney_saas': 1.0,  # 妙想 ai-saas
    'default': 0.3,
}

_last: dict = {}
_locks: dict = {}
_registry_lock = threading.Lock()


def _interval(key: str) -> float:
    env = os.getenv(f'RATE_LIMIT_{key.upper()}')
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    if key not in _DEFAULTS:
        d = os.getenv('RATE_LIMIT_DEFAULT')
        if d:
            try:
                return float(d)
            except ValueError:
                pass
    return _DEFAULTS.get(key, _DEFAULTS['default'])


def _lock_for(key: str) -> threading.Lock:
    with _registry_lock:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.Lock()
        return lk


def throttle(key: str = 'default', min_interval: float = None) -> float:
    """阻塞直到距同一 key 上次放行满最小间隔。返回实际 sleep 的秒数。"""
    iv = min_interval if min_interval is not None else _interval(key)
    if iv <= 0:
        return 0.0
    lk = _lock_for(key)
    with lk:
        now = time.monotonic()
        last = _last.get(key, 0.0)
        wait = iv - (now - last)
        slept = 0.0
        if wait > 0:
            slept = wait + random.uniform(0, iv * 0.25)  # 抖动,避免整齐撞车
            time.sleep(slept)
        _last[key] = time.monotonic()
        return slept


# host 关键字 -> 数据源 key
_HOST_MAP = (
    ('ai-saas', 'eastmoney_saas'),  # 妙想,须在 eastmoney 之前匹配
    ('eastmoney', 'eastmoney'), ('10jqka', 'ths'), ('hexin', 'ths'),
    ('iwencai', 'pywencai'), ('sinajs', 'sina'), ('sina.com', 'sina'),
    ('gtimg', 'tencent'), ('qq.com', 'tencent'), ('cninfo', 'cninfo'),
)


def host_key(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return 'default'
    for frag, key in _HOST_MAP:
        if frag in host:
            return key
    return host or 'default'


def throttled_session(session, key: str = None):
    """就地包装 requests.Session:之后所有 .get/.post 自动按 host(或指定 key)限流。"""
    for method in ('get', 'post'):
        orig = getattr(session, method, None)
        if orig is None or getattr(orig, '_throttled', False):
            continue

        def _make(orig):
            @wraps(orig)
            def wrapped(url, *args, **kwargs):
                throttle(key or host_key(url))
                return orig(url, *args, **kwargs)
            wrapped._throttled = True
            return wrapped

        setattr(session, method, _make(orig))
    return session


def throttled(key: str = 'default'):
    """装饰器:函数每次调用前先按 key 限流(用于包 akshare/tushare 等库调用)。"""
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            throttle(key)
            return fn(*a, **kw)
        return wrapper
    return deco


if __name__ == '__main__':
    import io, sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    print('host_key 测试:')
    for u in ['https://push2.eastmoney.com/api', 'http://qt.gtimg.cn/q=', 'http://ms.10jqka.com.cn/x',
              'https://ai-saas.eastmoney.com/proxy', 'http://hq.sinajs.cn/list']:
        print(f'  {u[:40]:<42} -> {host_key(u)}')
    print('\n连续 throttle(eastmoney) 3 次,观察节流(间隔≈0.35s+抖动):')
    t0 = time.monotonic()
    for i in range(3):
        s = throttle('eastmoney')
        print(f'  第{i+1}次 sleep={s:.3f}s 累计={time.monotonic()-t0:.3f}s')

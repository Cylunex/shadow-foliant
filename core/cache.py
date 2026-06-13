"""Redis 缓存层(优雅降级)—— 跨进程共享缓存 + 分布式锁。

用途:webui / jobs / monitor 大量重复抓同一外部数据(实时报价/基金估值/因子),
用 Redis 短 TTL 缓存可显著减少外部请求(降封禁风险、提速),且跨进程/重启共享。

特性:
  - 懒连接单例;Redis 不可用 → 自动降级(get 返 None / set 静默 / lock 直接放行),**绝不拖垮主流程**。
  - key 统一加 'aiagents:' 命名空间(与他人共用同一 Redis 不冲突)。
  - cache_get/set(JSON 序列化)、@cached(prefix, ttl) 装饰器、lock(name, ttl) 分布式锁上下文。

配置(.env,可选,缺省连 localhost:6379):
  REDIS_URL=redis://your_redis_host:6379/0   或   REDIS_HOST / REDIS_PORT / REDIS_DB
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from functools import wraps

_NS = 'aiagents:'
_client = None
_tried = False
_down_until = 0.0   # 连接失败后冷却,避免每次都卡超时


def _redis():
    """懒连接 Redis 单例;不可用返回 None(并冷却 30s)。"""
    global _client, _tried, _down_until
    if _client is not None:
        return _client
    now = time.time()
    if _down_until and now < _down_until:
        return None
    try:
        import redis
        url = os.getenv('REDIS_URL')
        if url:
            c = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2, decode_responses=True)
        else:
            c = redis.Redis(host=os.getenv('REDIS_HOST', ''),
                            port=int(os.getenv('REDIS_PORT', '6379')),
                            db=int(os.getenv('REDIS_DB', '0')),
                            socket_timeout=2, socket_connect_timeout=2, decode_responses=True)
        c.ping()
        _client = c
        return c
    except Exception as e:
        if not _tried:
            print(f'[cache] Redis 不可用,降级运行: {type(e).__name__}')
        _tried = True
        _down_until = now + 30
        return None


def available() -> bool:
    return _redis() is not None


def cache_get(key: str):
    c = _redis()
    if not c:
        return None
    try:
        v = c.get(_NS + key)
        return json.loads(v) if v is not None else None
    except Exception:
        return None


def cache_set(key: str, value, ttl: int = 60):
    c = _redis()
    if not c:
        return False
    try:
        c.set(_NS + key, json.dumps(value, ensure_ascii=False, default=str), ex=ttl)
        return True
    except Exception:
        return False


def cache_del(key: str):
    c = _redis()
    if c:
        try:
            c.delete(_NS + key)
        except Exception:
            pass


def cached(prefix: str, ttl: int = 60):
    """缓存装饰器:key = prefix:arg1:arg2...。Redis 不可用时直接调原函数。
    仅用于返回值可 JSON 化的纯查询函数。"""
    def deco(fn):
        @wraps(fn)
        def wrap(*args, **kwargs):
            key = prefix + ':' + ':'.join(str(a) for a in args)
            if kwargs:
                key += ':' + ':'.join(f'{k}={v}' for k, v in sorted(kwargs.items()))
            hit = cache_get(key)
            if hit is not None:
                return hit
            res = fn(*args, **kwargs)
            if res is not None:
                cache_set(key, res, ttl)
            return res
        return wrap
    return deco


@contextmanager
def lock(name: str, ttl: int = 300, wait: float = 0):
    """分布式锁(SET NX EX)。拿到 yield True,拿不到 yield False。Redis 不可用直接 yield True(单机退化)。
    用法:
        with lock('job:pg_backup') as ok:
            if not ok: return   # 别处在跑,跳过
            ...
    """
    c = _redis()
    if not c:
        yield True
        return
    key = _NS + 'lock:' + name
    token = str(time.time())
    deadline = time.time() + wait
    got = False
    try:
        while True:
            got = bool(c.set(key, token, nx=True, ex=ttl))
            if got or time.time() >= deadline:
                break
            time.sleep(0.2)
        yield got
    finally:
        if got:
            try:
                if c.get(key) == token:
                    c.delete(key)
            except Exception:
                pass


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('Redis available:', available())
    cache_set('selftest', {'a': 1}, ttl=20)
    print('roundtrip:', cache_get('selftest'))
    with lock('selftest_lock', ttl=10) as ok:
        print('lock acquired:', ok)

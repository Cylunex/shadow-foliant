"""
请求频率限制器 — 同一数据源两次 HTTP 调用之间保持最小间隔(按源可配)。

用法:
    from rate_limiter import throttle, throttled_session

    throttle('tencent')          # 调用前等待至距上次 ≥ 该源最小间隔
    throttle('akshare')          # akshare 反爬严, 最小间隔 3s
    throttled_session(session)   # 给 requests.Session 挂上自限流 hook
"""
import time
import threading
from collections import defaultdict

# ⚠️ 2026-06-30 大坑修复:此前是全局唯一 `_lock`,所有源(tencent/sina/east/akshare/eastmoney_saas/
# tickflow/pywencai...)共享一把锁 + 锁里 `time.sleep(wait)` ——
# 一个 tickflow 调用 sleep 6s 期间, 其他全部源的调用都被挡在 `with _lock:` 外面排队,
# 多线程并发(strategy_prefetch 16 worker + datahub-route 32 worker)被串行成 90s+ 假超时。
# py-spy 实证 webui 几乎所有线程都堵在 rate_limiter.py:58 `with _lock:`。
# 修法:per-key 独立锁(_lock_for(source)) → 不同源互不阻塞, 同源仍按 _gap 串行(原意保留)。
# 与 core/rate_limiter.py 实现对齐。
_registry_lock = threading.Lock()             # 只保护 _locks dict 增减(秒级释放)
_locks: dict[str, threading.Lock] = {}
_last_call: dict[str, float] = defaultdict(float)


def _lock_for(source: str) -> threading.Lock:
    """取该源的独立锁(首次访问按需创建)。"""
    with _registry_lock:
        lk = _locks.get(source)
        if lk is None:
            lk = _locks[source] = threading.Lock()
        return lk

# 惯例基线(秒): 任何源的最小间隔都不得低于此值。
# ⭐ 间隔只增不减: _gap_for 最终返回 max(_DEFAULT_GAP, 该源配置), 杜绝任何源低于 1s。
_DEFAULT_GAP = 1.0

# 按源覆盖最小间隔(秒)。键 = throttle(source) 传入的 source 名(支持 host 前缀近似匹配)。
# 只填"按惯例需要比 1s 更宽"的源; 低于 1s 的值会被下限钳制无效。
# 2026-06-23: akshare 反爬最严 3s; 同花顺(pywencai)次之 2s; 实时行情/其余走 1s 惯例。
# 2026-06-25: 东财机房 IP 反爬严(整域被封 RemoteDisconnected)→ eastmoney 1s→3s 降调用量;
#   子串匹配:throttled_session 按 host 的 push2/datacenter/reportapi.eastmoney.com 都命中 'eastmoney'→3s。
#   tickflow 免费版 10次/分 → 6s。
_GAP_BY_SOURCE: dict[str, float] = {
    'akshare': 3.0,
    'pywencai': 2.0,
    'ths': 2.0,          # 同花顺直连
    '10jqka': 2.0,       # 同花顺域名
    'eastmoney': 3.0,    # 东财(含 push2/datacenter/reportapi 各子域, 子串匹配)
    'tickflow': 6.0,     # TickFlow 免费版 10次/分
}


# host 子串 → 规范源键(把同一 provider 的多个子域折进同一限流预算)。
# ⚠️ 2026-07-17 修:throttled_session 原来按裸 netloc 分锁/分计时器,东财 8+ 子域
# (push2/push2his/datacenter-web/data/reportapi/search-api-web/...)虽然 _gap_for 都命中 3s,
# 但**各拿独立锁+独立时钟** → 对东财单一机房 IP 段实际约 N× 于预期速率(注释自述"共享3s"落空),
# 正是要防的"海量调用→整域被封 RemoteDisconnected"。折叠后同 provider 共享一把锁+一个 3s 预算。
_HOST_FOLD = (
    ('ai-saas', 'eastmoney_saas'),   # 妙想:须在 eastmoney 之前匹配
    ('eastmoney', 'eastmoney'),
    ('10jqka', 'ths'), ('hexin', 'ths'),
    ('gtimg', 'tencent'), ('qq.com', 'tencent'),
    ('sinajs', 'sina'), ('sina.com', 'sina'), ('sina.cn', 'sina'),
    ('iwencai', 'pywencai'),
    ('cninfo', 'cninfo'),
)


def _fold_host(host: str) -> str:
    """把 host 折叠成规范源键(子域归一到同一 provider 的限流预算);无匹配返回原 host。"""
    h = (host or '').lower()
    for frag, key in _HOST_FOLD:
        if frag in h:
            return key
    return host or 'default'


def _gap_for(source: str) -> float:
    """取某源的最小间隔: 精确命中 > 子串命中(host 场景) > 默认; 并以 _DEFAULT_GAP 为下限。
    ⭐ 只增不减: 配置值若 < 1s, 一律按 1s 处理(惯例基线不可破)。"""
    gap = _DEFAULT_GAP
    if source in _GAP_BY_SOURCE:
        gap = _GAP_BY_SOURCE[source]
    else:
        s = source.lower()
        for key, g in _GAP_BY_SOURCE.items():
            if key in s:
                gap = g
                break
    return max(_DEFAULT_GAP, gap)


def throttle(source: str = "default") -> float:
    """若距上次 source 调用不足该源最小间隔则阻塞等待，返回实际等待秒数。
    akshare ≥3s / pywencai ≥2s / 其余 ≥1s(见 _GAP_BY_SOURCE)。
    ⭐ per-key 独立锁:同源仍串行(原意),不同源互不阻塞(防一个慢源拖垮全部)。"""
    min_gap = _gap_for(source)
    lk = _lock_for(source)
    with lk:
        now = time.monotonic()
        gap = now - _last_call[source]
        wait = min_gap - gap
        if wait > 0:
            time.sleep(wait)
            _last_call[source] = time.monotonic()
            return wait
        _last_call[source] = now
        return 0.0


def throttled_session(session):
    """给 requests.Session 挂上请求前自限流的 hook。
    按 host 区分，同一 host 两次请求至少间隔 1 秒。"""
    from urllib.parse import urlparse

    def _hook(resp, *args, **kwargs):
        # response hook 是事后限流（保证下次请求前等了 1s）
        pass

    def _pre_hook(prepared_request):
        host = _fold_host(urlparse(prepared_request.url or "").netloc)
        throttle(host)

    # 使用 Session.hooks['response'] 做「本次请求后等待」不太精准，
    # 但我们无法拦截 send()。替代方案：包装 send
    original_send = session.send

    def _throttled_send(request, **kwargs):
        # 折叠子域到规范源键:东财 push2/push2his/datacenter… 共享同一把锁+同一 3s 预算(2026-07-17)
        host = _fold_host(urlparse(request.url or "").netloc)
        min_gap = _gap_for(host)
        lk = _lock_for(host)               # ⭐ per-host 锁,不再全局阻塞
        with lk:
            now = time.monotonic()
            gap = now - _last_call[host]
            wait = min_gap - gap
            if wait > 0:
                time.sleep(wait)
            _last_call[host] = time.monotonic()
        return original_send(request, **kwargs)

    session.send = _throttled_send
    return session

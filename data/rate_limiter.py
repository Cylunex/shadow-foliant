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

_lock = threading.Lock()
_last_call: dict[str, float] = defaultdict(float)

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
    akshare ≥3s / pywencai ≥2s / 其余 ≥1s(见 _GAP_BY_SOURCE)。"""
    min_gap = _gap_for(source)
    now = time.monotonic()
    with _lock:
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
        host = urlparse(prepared_request.url or "").netloc or "default"
        throttle(host)

    # 使用 Session.hooks['response'] 做「本次请求后等待」不太精准，
    # 但我们无法拦截 send()。替代方案：包装 send
    original_send = session.send

    def _throttled_send(request, **kwargs):
        host = urlparse(request.url or "").netloc or "default"
        min_gap = _gap_for(host)
        now = time.monotonic()
        with _lock:
            gap = now - _last_call[host]
            wait = min_gap - gap
            if wait > 0:
                time.sleep(wait)
            _last_call[host] = time.monotonic()
        return original_send(request, **kwargs)

    session.send = _throttled_send
    return session

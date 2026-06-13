"""
请求频率限制器 — 同一数据源 1 秒内最多 1 次 HTTP 调用。

用法:
    from rate_limiter import throttle, throttled_session

    throttle('tencent')          # 调用前等待至距上次 ≥1s
    throttled_session(session)   # 给 requests.Session 挂上自限流 hook
"""
import time
import threading
from collections import defaultdict

_lock = threading.Lock()
_last_call: dict[str, float] = defaultdict(float)
_MIN_GAP = 1.0  # 秒


def throttle(source: str = "default") -> float:
    """若距上次 source 调用不足 1 秒则阻塞等待，返回实际等待秒数。"""
    now = time.monotonic()
    with _lock:
        gap = now - _last_call[source]
        wait = _MIN_GAP - gap
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
        now = time.monotonic()
        with _lock:
            gap = now - _last_call[host]
            wait = _MIN_GAP - gap
            if wait > 0:
                time.sleep(wait)
            _last_call[host] = time.monotonic()
        return original_send(request, **kwargs)

    session.send = _throttled_send
    return session

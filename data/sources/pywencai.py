# -*- coding: utf-8 -*-
"""pywencai 安全封装(2026-06-15)

pywencai.get(query, loop=True) 在网络抽风 / 同花顺反爬时可永久卡死底层 socket,
没有整体 timeout 参数, 历史上多次拖垮 jobs_hub 线程池(僵尸进程, supervisor 看 PID
活着不重启 → 整套定时任务静默瘫痪)。本模块用独立线程池 + future.result(timeout=)
把所有调用强制套上硬超时, 全项目统一从这里调 pywencai。

用法:
    from data.pywencai_safe import pywencai_get
    df = pywencai_get(query, timeout=90)             # 默认 loop=True
    df = pywencai_get(query, timeout=30, loop=False) # 单页查询
超时即抛 TimeoutError, 调用方按原本的 except 路径走降级即可(不要静默 swallow)。
"""
from __future__ import annotations
import concurrent.futures as _cf
import importlib as _importlib
import os as _os
import threading as _threading
import time as _time
import pywencai

# 不能用 `with ThreadPoolExecutor()` —— __exit__ 会 shutdown(wait=True) 阻塞等
# 卡死的孤儿线程跑完, 失去超时意义。同 api_server._DEADLINE_POOL 的处理方式。
# max_workers=12(2026-06-24: 4→12): 死源挂满 timeout 时小池易饱和 → 排队被算成假超时。
_POOL = _cf.ThreadPoolExecutor(max_workers=12, thread_name_prefix='pywencai-safe')

# ⚡ 全源熔断(2026-06-25):问财不走 datahub._route, 故在此自带熔断, 否则问财整体不可达时
# 每只逐只仍吃满 timeout(collect_factors 焐热 391 只 × 30s) → kline_prefetch/factor_collection
# 被拖到 1800s/1200s 硬超时切断 + 误报"⚠️任务异常"(K线明明 2 分钟已全焐完)。
# 连续失败 _BREAK_FAILS 次后进入冷却:冷却期内直接抛 TimeoutError(0 成本), 冷却过期放行一次探活,
# 成功即自愈。与 datahub._route 全源熔断同构。仅统计"连不通"(超时/异常), 返回空 df ≠ 失败(问财在线只是无数据)。
# 盘前有 4 条彼此独立的策略问句；阈值为 2 时，前一条策略的“统一入口+原
# selector 重试”可直接熔断整批。selector 已改成每条只请求一次，阈值同步调到
# 4：确保各策略至少有一次独立机会；真正全源故障最多多付出两次调用，随后仍会熔断。
_BREAK_FAILS = 4
_BREAK_COOLDOWN = 120.0   # 秒
_streak_fail = 0
_last_fail = 0.0
_BREAK_LOG_LAST = 0.0
_BREAK_LOG_GAP = 60.0


class _HttpsRequestsProxy:
    """仅修正 pywencai 0.13.1 内部遗留的问财 HTTP 地址。"""

    _shadow_iwencai_https_proxy = True
    _HTTP_PREFIX = 'http://www.iwencai.com/'
    _HTTPS_PREFIX = 'https://www.iwencai.com/'

    def __init__(self, requests_module):
        self._requests = requests_module
        self._state = _threading.local()

    def request(self, *args, **kwargs):
        args = list(args)
        if 'url' in kwargs:
            url = kwargs['url']
            if isinstance(url, str) and url.startswith(self._HTTP_PREFIX):
                kwargs = dict(kwargs)
                kwargs['url'] = self._HTTPS_PREFIX + url[len(self._HTTP_PREFIX):]
        elif len(args) >= 2:
            url = args[1]
            if isinstance(url, str) and url.startswith(self._HTTP_PREFIX):
                args[1] = self._HTTPS_PREFIX + url[len(self._HTTP_PREFIX):]
        response = self._requests.request(*args, **kwargs)
        self._state.last_status = getattr(response, 'status_code', None)
        return response

    def reset_status(self):
        self._state.last_status = None

    def last_status(self):
        return getattr(self._state, 'last_status', None)


class PyWencaiRequestRejected(RuntimeError):
    """问财明确拒绝请求（鉴权、反爬或限流），不是业务查询空结果。"""


# 不修改全局 requests，仅替换 pywencai 两个内部模块各自持有的引用。
_pywencai_core = _importlib.import_module('pywencai.wencai')
_pywencai_convert = _importlib.import_module('pywencai.convert')
if getattr(_pywencai_core.rq, '_shadow_iwencai_https_proxy', False):
    _HTTPS_REQUESTS = _pywencai_core.rq
else:
    _HTTPS_REQUESTS = _HttpsRequestsProxy(_pywencai_core.rq)
_pywencai_core.rq = _HTTPS_REQUESTS
_pywencai_convert.rq = _HTTPS_REQUESTS


def _invoke_pywencai(query, loop, kwargs):
    """在 pywencai 吞掉底层异常后，保留明确的 HTTP 拒绝分类。"""
    _HTTPS_REQUESTS.reset_status()
    try:
        result = pywencai.get(query=query, loop=loop, **kwargs)
    except AttributeError as exc:
        status = _HTTPS_REQUESTS.last_status()
        if status in (401, 403, 429):
            raise PyWencaiRequestRejected(
                f'问财请求被拒绝(HTTP {status}，可能为鉴权/反爬/限流)'
            ) from exc
        raise
    status = _HTTPS_REQUESTS.last_status()
    if result is None and status in (401, 403, 429):
        raise PyWencaiRequestRejected(
            f'问财请求被拒绝(HTTP {status}，可能为鉴权/反爬/限流)'
        )
    return result


def cookie_configured() -> bool:
    """是否已配置问财登录 Cookie；只返回布尔值，禁止日志输出 Cookie 内容。"""
    return bool(
        _os.getenv('PYWENCAI_COOKIE', '').strip()
        or _os.getenv('WENCAI_COOKIE', '').strip()
    )


def breaker_open() -> bool:
    """问财熔断是否生效中(连续失败达阈值且仍在冷却期)。供任务超时通知"具体到问财"。"""
    import time as _t
    return _streak_fail >= _BREAK_FAILS and (_t.time() - _last_fail) < _BREAK_COOLDOWN


def pywencai_get(query: str, timeout: int = 90, loop: bool = True, **kwargs):
    """带硬超时 + 熔断的 pywencai.get 包装。

    Args:
        query: 问财查询语句
        timeout: 整体超时(秒), 默认 90s。loop=True 翻多页时给宽点, 单页 30s 够。
        loop: 透传给 pywencai.get, 是否翻全分页
        **kwargs: 其他参数透传

    Returns:
        与原生 pywencai.get 相同(通常是 DataFrame, 也可能 dict/None)

    Raises:
        TimeoutError: 超时, 或熔断冷却期内直接短路(上层按既有 except 路径降级)
        其它异常: 与原生 pywencai.get 一致, 上层按原路径处理
    """
    global _streak_fail, _last_fail, _BREAK_LOG_LAST
    now = _time.time()
    # 熔断:连续失败达阈值且仍在冷却期 → 不再 submit, 直接短路(避免逐只吃满 timeout)
    if _streak_fail >= _BREAK_FAILS and (now - _last_fail) < _BREAK_COOLDOWN:
        if now - _BREAK_LOG_LAST >= _BREAK_LOG_GAP:
            _BREAK_LOG_LAST = now
            print(f'[pywencai] ⚡ 问财连续失败熔断中, {_BREAK_COOLDOWN:.0f}s 内直接短路降级'
                  f'(60s 内仅提示一次)', flush=True)
        raise TimeoutError('pywencai 熔断中(连续失败), 短路降级')
    # Cookie 是可选稳定性增强，不是匿名查询的硬前提。若配置则安全透传，
    # 绝不打印值。
    if 'cookie' not in kwargs:
        cookie = (
            _os.getenv('PYWENCAI_COOKIE', '').strip()
            or _os.getenv('WENCAI_COOKIE', '').strip()
        )
        if cookie:
            kwargs['cookie'] = cookie
    # 上游默认 retry=10/sleep=0，会把一次拒绝瞬间放大成 10 次请求。
    kwargs.setdefault('retry', 2)
    kwargs.setdefault('sleep', 1)
    fut = _POOL.submit(_invoke_pywencai, query, loop, kwargs)
    try:
        r = fut.result(timeout=timeout)
        _streak_fail = 0   # 连通即复位(返回空 df 也算连通, 问财只是无数据)
        return r
    except _cf.TimeoutError:
        # 孤儿线程留给底层自然结束/退出; cancel() 多数情况无效(任务已开始), 但不阻塞
        fut.cancel()
        _streak_fail += 1
        _last_fail = _time.time()
        raise TimeoutError(f'pywencai 查询超时 {timeout}s')
    except AttributeError as e:
        # ⚠️ pywencai 0.13.1(2025-05 最新版)bug:wencai.py:185 `params.get('data')` 没校验
        # get_robot_data() 返 None,直接 `.get()` 崩 AttributeError。
        # 触发条件:超长复杂 query / 同花顺反爬返坏结构。库已升级到最新仍未修。
        # 对策:转 None 视同问财无数据,调用方原本就处理 None(if result is None: continue)。
        if "NoneType" in str(e) and "get" in str(e):
            _streak_fail += 1
            _last_fail = _time.time()
            return None
        _streak_fail += 1
        _last_fail = _time.time()
        raise
    except Exception:
        _streak_fail += 1
        _last_fail = _time.time()
        raise

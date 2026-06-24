# -*- coding: utf-8 -*-
"""akshare 调用安全封装(2026-06-18)

akshare 大部分函数内部用 requests.get(...) 但不传 timeout 参数, 服务器慢响应/
反爬/限流时会**永久卡住底层 socket**。多次拖垮 fund_valuation_signal /
portfolio_indicator_snapshot / unified_selection 等任务 → 被 deadman 30 分钟超时
强杀整个 jobs_hub 进程, 受牵连任务一起死, 后续任务也错过窗口。

本模块用独立线程池 + future.result(timeout=) 强制超时, 让 akshare 调用必返回:
    from data.akshare_safe import call as ak_call
    df = ak_call(ak.stock_index_pe_lg, timeout=30, symbol='沪深300')

跟 data/pywencai_safe.py 同思路。
"""
from __future__ import annotations
import concurrent.futures as _cf
from typing import Any, Callable

# 不能用 `with ThreadPoolExecutor()` —— __exit__ 会 shutdown(wait=True) 阻塞等
# 卡死的孤儿线程跑完, 失去超时意义。
# max_workers=24(2026-06-24: 8→24): 外网抽风时死源挂满 timeout, 8 槽易被占满 →
# 后续 submit 排队也被 result(timeout) 算成假超时级联。线程只是 IO 等待, 给足。
_POOL = _cf.ThreadPoolExecutor(max_workers=24, thread_name_prefix='akshare-safe')


def call(fn: Callable[..., Any], *args, timeout: int = 30, **kwargs) -> Any:
    """带硬超时的 akshare 调用包装。超时即抛 TimeoutError, 上层 try-except 走降级。

    Args:
        fn: akshare 函数对象, 例如 ak.stock_index_pe_lg
        *args, **kwargs: 透传给 fn 的参数
        timeout: 整体超时(秒), 默认 30s

    Returns:
        fn(*args, **kwargs) 的返回值

    Raises:
        TimeoutError: 超时
        其它异常: 与原生 fn 一致
    """
    fut = _POOL.submit(fn, *args, **kwargs)
    try:
        return fut.result(timeout=timeout)
    except _cf.TimeoutError:
        fut.cancel()  # 孤儿线程靠底层自然结束;cancel 已 running 任务无效但不阻塞
        raise TimeoutError(f'akshare 调用超时 {timeout}s ({getattr(fn, "__name__", fn)})')

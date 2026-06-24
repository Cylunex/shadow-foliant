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
import pywencai

# 不能用 `with ThreadPoolExecutor()` —— __exit__ 会 shutdown(wait=True) 阻塞等
# 卡死的孤儿线程跑完, 失去超时意义。同 api_server._DEADLINE_POOL 的处理方式。
# max_workers=12(2026-06-24: 4→12): 死源挂满 timeout 时小池易饱和 → 排队被算成假超时。
_POOL = _cf.ThreadPoolExecutor(max_workers=12, thread_name_prefix='pywencai-safe')


def pywencai_get(query: str, timeout: int = 90, loop: bool = True, **kwargs):
    """带硬超时的 pywencai.get 包装。

    Args:
        query: 问财查询语句
        timeout: 整体超时(秒), 默认 90s。loop=True 翻多页时给宽点, 单页 30s 够。
        loop: 透传给 pywencai.get, 是否翻全分页
        **kwargs: 其他参数透传

    Returns:
        与原生 pywencai.get 相同(通常是 DataFrame, 也可能 dict/None)

    Raises:
        TimeoutError: 超时
        其它异常: 与原生 pywencai.get 一致, 上层按原路径处理
    """
    fut = _POOL.submit(pywencai.get, query=query, loop=loop, **kwargs)
    try:
        return fut.result(timeout=timeout)
    except _cf.TimeoutError:
        # 孤儿线程留给底层自然结束/退出; cancel() 多数情况无效(任务已开始), 但不阻塞
        fut.cancel()
        raise TimeoutError(f'pywencai 查询超时 {timeout}s')

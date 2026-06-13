"""阈值穿越检测 + 去抖(借鉴 leek-fund 的差量提醒思路)。

价格盯盘告警的常见噪声:价格停在阈值之上时每轮都触发。改用"**穿越**"语义——
仅当价格从阈值另一侧穿过时才算事件,配合冷却去抖,显著降噪。纯函数,易测。
"""

from __future__ import annotations

from typing import Optional


def crossed_up(prev: Optional[float], cur: float, level: float) -> bool:
    """上穿:上一价 < 阈值 ≤ 当前价。"""
    return prev is not None and prev < level <= cur


def crossed_down(prev: Optional[float], cur: float, level: float) -> bool:
    """下穿:上一价 > 阈值 ≥ 当前价。"""
    return prev is not None and prev > level >= cur


def entered_band(prev: Optional[float], cur: float, lo: float, hi: float) -> bool:
    """进入区间 [lo,hi]:当前在区间内,且上一价在区间外(或无上一价时视为新进入)。"""
    inside_now = lo <= cur <= hi
    inside_prev = prev is not None and lo <= prev <= hi
    return inside_now and not inside_prev


class Debouncer:
    """按 key 的冷却去抖:同一 key 在 cooldown 秒内只放行一次。
    now 由调用方传入(运行时用 time.time()),便于测试可复现。"""

    def __init__(self, cooldown_sec: float = 180):
        self.cooldown = cooldown_sec
        self._last: dict = {}

    def allow(self, key: str, now: float) -> bool:
        last = self._last.get(key)
        if last is not None and (now - last) < self.cooldown:
            return False
        self._last[key] = now
        return True


if __name__ == '__main__':
    # 自测:价格停在阈值上方不应反复触发
    assert crossed_up(9.0, 10.5, 10.0) is True
    assert crossed_up(10.2, 10.5, 10.0) is False   # 已在上方,非穿越
    assert crossed_down(11.0, 9.5, 10.0) is True
    assert entered_band(8.0, 9.5, 9.0, 10.0) is True
    assert entered_band(9.2, 9.5, 9.0, 10.0) is False
    d = Debouncer(60)
    assert d.allow('k', 100) is True
    assert d.allow('k', 130) is False   # 60s 内
    assert d.allow('k', 200) is True
    print('alert_signals 自测通过')

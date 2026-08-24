"""统一保存夜间与周末批任务时间，避免调度、Web/MCP 展示和文档口径漂移。"""

from datetime import datetime

# 夜间任务按硬超时预算后仍应在 23:30 前结束；24:00 是绝对上限。
EVENING_TIMES = {
    'rag_ingest': '20:15',          # 默认关闭；启用时 60 分钟预算，21:15 前收尾
    'fund_evening': '22:00',        # 20 分钟预算，22:20 前收尾
    'daily_pnl_snapshot': '22:30',  # 默认 10 分钟预算，22:40 前收尾
}

EVENING_MAX_RUNTIME_MINUTES = {
    'rag_ingest': 60,
    'fund_evening': 20,
    'daily_pnl_snapshot': 10,
    'weekly_db_cleanup': 10,
}

# 周末高 token LLM 禁用窗口：09:00–12:00、14:00–18:00。
# mx_weekend_outlook 默认 10 分钟预算；weekend_portfolio 默认 90 分钟预算。
WEEKEND_TIMES = {
    'mx_weekend_outlook': ('sunday', '08:00'),
    'weekend_portfolio': ('sunday', '12:05'),
    'wf_weekly_backtest': ('sunday', '20:00'),       # 无 LLM，保留原时间
    'ai_eval_weekly': ('sunday', '20:30'),           # 无 LLM，保留原时间
    'strategy_policy_weekly': ('sunday', '21:00'),   # 单次有界 LLM 策略委员会
    'weekly_db_cleanup': ('sunday', '23:15'),
}

WEEKEND_LLM_JOBS = ('mx_weekend_outlook', 'weekend_portfolio', 'strategy_policy_weekly')
WEEKEND_LLM_BLACKOUTS = ((9 * 60, 12 * 60), (14 * 60, 18 * 60))


def minute_of_day(hhmm: str) -> int:
    hour, minute = (int(part) for part in hhmm.split(':', 1))
    return hour * 60 + minute


def interval_overlaps(start_minute: int, duration_minutes: int,
                      blocked_start: int, blocked_end: int) -> bool:
    """半开区间相交判断；边界 12:00/18:00 可以运行。"""
    return start_minute < blocked_end and start_minute + duration_minutes > blocked_start


def weekend_llm_allowed(now: datetime = None, estimated_minutes: int = 1) -> bool:
    """平日始终放行；周末阻止 LLM 任务进入两个禁用窗口。

    ``estimated_minutes`` 让即将跨入禁用窗口的长任务提前跳过，而不只是检查启动点。
    """
    current = now or datetime.now()
    if current.weekday() < 5:
        return True
    start = current.hour * 60 + current.minute
    duration = max(1, int(estimated_minutes or 1))
    return not any(interval_overlaps(start, duration, left, right)
                   for left, right in WEEKEND_LLM_BLACKOUTS)

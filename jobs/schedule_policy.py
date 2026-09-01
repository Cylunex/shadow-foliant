"""统一保存夜间与周末批任务时间，避免调度、Web/MCP 展示和文档口径漂移。"""

from datetime import datetime

# BaoStock 的日线与复权因子不是收盘即完成。盘后数据链统一在这里留出
# 可见的安全缓冲，避免 jobs_hub、Web 开关页和文档各自维护一套时间。
#
# 其余已知发布时间仅作边界说明：分钟线 20:00、周线周六 17:30、
# 月线每月 1 日 17:30、其它财务报告次日 01:30。当前项目没有依赖
# BaoStock 这些批次的定时入库任务，不能用它们约束 zzshare/实时源任务。
BAOSTOCK_READY_TIMES = {
    'daily_kline': '17:30',
    'adjustment_factor': '18:00',
    'minute_kline': '20:00',
    'financial_reports_next_day': '01:30',
    'weekly_kline_saturday': '17:30',
    'monthly_kline_day_1': '17:30',
}

MARKET_DATA_TIMES = {
    # 若前夜估值源晚到，开盘前先补一次；09:45 正式选股仍保留最终的
    # fail-closed 自愈，不会用陈旧估值生成候选。
    'research_data_sync_premarket_retry': '08:35',
    # 正式 PIT 数据优先：18:00 复权因子完成后先同步。估值提供方偶尔
    # 晚于日线发布，因此 20:05 做一次不含财务/主力资金的轻量补跑。
    'research_data_sync': '18:05',
    'research_data_sync_retry': '20:05',
    # 大批量预热放在 PIT 同步之后，避免并发争用 BaoStock 单会话/熔断器。
    'kline_prefetch': '18:35',
    # 下游仍有显式依赖；这些时间只是进入 deferred 队列的最早时刻。
    'factor_collection': '18:40',
    'portfolio_indicator_snapshot': '18:45',
    'eod_outcomes': '18:50',
    # 给最长 45 分钟的预热留出窗口；未完成时继续由依赖队列等待。
    'daily_backtest': '19:30',
}

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

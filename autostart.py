"""
autostart.py — 项目启动时自动拉起所有后台服务和定时调度

由服务入口显式调用 `autostart()` 激活。

设计：
  * Module-level `_STARTED` flag 防止意外重复启动。
  * 每个服务独立 try/except — 单个失败不影响其他。
  * 旧入口默认关闭；生产生命周期统一交给 Supervisor。

控制开关（.env）：
    AUTOSTART_ENABLED=false           总开关
    AUTOSTART_MONITOR=false           价格监测（含交易时段调度）
    AUTOSTART_JOBS_HUB=false          已废弃，Jobs Hub 由 Supervisor 管理
    AUTOSTART_NEWS_FLOW=false         新闻流量调度器
    AUTOSTART_SECTOR_STRATEGY=false   智策板块每日（默认关，AI 调用成本高）
    AUTOSTART_PORTFOLIO=false         持仓定时分析（同上）
"""

import _bootstrap  # noqa: F401  路径引导
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


_STARTED = False


def _on(env_key: str, default: str = 'true') -> bool:
    return os.getenv(env_key, default).lower() in ('1', 'true', 'yes', 'on')


def _emit(level: str, msg: str):
    """输出统一格式的服务启动日志。"""
    print(f'[autostart] {level} {msg}', flush=True)


def _start_monitor():
    """价格监测：直接启服务 + 装上交易时段自动启停调度器"""
    try:
        from monitor_service import monitor_service
        from monitor_scheduler import get_scheduler

        # 1. 启动监测服务本身（无论是否交易时段都启起来；非交易时段也保留监测列表，
        #    只是不轮询；scheduler 会管理"交易时段才主动检查"逻辑）
        if not monitor_service.running:
            monitor_service.start_monitoring()
        _emit('✅', 'monitor_service started')

        # 2. 启动交易时段调度器（按 09:30/13:00 自动启停 monitor_service）
        scheduler = get_scheduler(monitor_service)
        if scheduler and not scheduler.running:
            scheduler.start_scheduler()
        _emit('✅', 'monitor_scheduler started')
    except Exception as e:
        _emit('❌', f'monitor_service/scheduler failed: {e}')


def _start_jobs_hub():
    """Legacy compatibility: Jobs Hub has one owner, the Supervisor service."""
    _emit('⏭️', 'AUTOSTART_JOBS_HUB 已废弃；请使用 Supervisor stock-jobs-hub')


def _start_news_flow():
    """新闻流量调度（热点同步 30min / 预警 60min / 深度分析 120min — 后者耗 token）"""
    try:
        from news_flow_scheduler import news_flow_scheduler as nfs
        if not getattr(nfs, 'running', False):
            nfs.start()
        _emit('✅', 'news_flow_scheduler started')
    except Exception as e:
        _emit('❌', f'news_flow_scheduler failed: {e}')


def _start_sector_strategy():
    """智策板块每日分析（默认关 — AI token 消耗高，由用户在 UI 主动开）"""
    try:
        from sector_strategy_scheduler import sector_strategy_scheduler as sss
        schedule_time = os.getenv('SECTOR_STRATEGY_TIME', '09:00')
        sss.start(schedule_time)
        _emit('✅', f'sector_strategy_scheduler started @ {schedule_time}')
    except Exception as e:
        _emit('❌', f'sector_strategy_scheduler failed: {e}')


def _start_portfolio():
    """持仓定时分析（默认关 — 消耗 AI token，建议手动）"""
    try:
        from portfolio_scheduler import portfolio_scheduler
        if not portfolio_scheduler.is_running():
            portfolio_scheduler.start()
        _emit('✅', 'portfolio_scheduler started')
    except Exception as e:
        _emit('❌', f'portfolio_scheduler failed: {e}')


def autostart():
    """主入口：根据 .env 开关启动所有服务（幂等，重复调用安全）"""
    global _STARTED
    if _STARTED:
        return
    _STARTED = True

    if not _on('AUTOSTART_ENABLED', 'false'):
        _emit('⏭️', 'AUTOSTART_ENABLED=false, all skipped')
        return

    _emit('🚀', '正在启动后台服务和定时调度...')

    if _on('AUTOSTART_MONITOR', 'false'):
        _start_monitor()
    if _on('AUTOSTART_JOBS_HUB', 'false'):
        _start_jobs_hub()
    if _on('AUTOSTART_NEWS_FLOW', 'false'):
        _start_news_flow()
    if _on('AUTOSTART_SECTOR_STRATEGY', 'false'):
        _start_sector_strategy()
    if _on('AUTOSTART_PORTFOLIO', 'false'):
        _start_portfolio()

    _emit('✅', 'autostart 完成')


if __name__ == '__main__':
    autostart()

"""
autostart.py — 项目启动时自动拉起所有后台服务和定时调度

在 app.py 顶部 `import autostart` 即可激活。

设计：
  * Module-level `_STARTED` flag — Streamlit 每次 re-run app.py 时都会 import，
    但 Python 模块缓存确保只第一次执行；同时显式 flag 防止意外重复。
  * 每个服务独立 try/except — 单个失败不影响其他。
  * 全部由 .env 开关控制，默认全开。

控制开关（.env）：
    AUTOSTART_ENABLED=true            总开关
    AUTOSTART_MONITOR=true            价格监测（含交易时段调度）
    AUTOSTART_LOW_PRICE_BULL=false    低价擒牛策略监测
    AUTOSTART_JOBS_HUB=true           Jobs Hub（盘前预热+盘后快照+策略扫描）
    AUTOSTART_NEWS_FLOW=false         新闻流量调度器
    AUTOSTART_SECTOR_STRATEGY=false   智策板块每日（默认关，AI 调用成本高）
    AUTOSTART_PORTFOLIO=false         持仓定时分析（同上）
"""

import _bootstrap  # noqa: F401  路径引导
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


_STARTED = False


def _on(env_key: str, default: str = 'true') -> bool:
    return os.getenv(env_key, default).lower() in ('1', 'true', 'yes', 'on')


def _emit(level: str, msg: str):
    """统一格式的日志输出（流到 streamlit 启动控制台）"""
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


def _start_low_price_bull():
    """低价擒牛策略监测（持仓状态下自动判断卖出）"""
    try:
        from low_price_bull_service import low_price_bull_service as lpb_svc
        if not getattr(lpb_svc, 'running', False):
            lpb_svc.start()
        _emit('✅', 'low_price_bull_service started')
    except Exception as e:
        _emit('❌', f'low_price_bull_service failed: {e}')


def _start_jobs_hub():
    """看门狗管理：优先启动独立守护进程，不创建 daemon thread

    1. 检测看门狗 PID → 如果活着，跳过
    2. 如果看门狗没跑，启动看门狗（它管理 jobs_hub 的独立进程）
    3. 绝不直接在当前进程启动 daemon thread
    """
    pid_file = os.path.expanduser('~/.openclaw/workspace/.jobs_hub.pid')
    watchdog_script = os.path.expanduser('~/.openclaw/workspace/scripts/jobs_hub-watchdog.sh')

    # 检查看门狗管理的 jobs_hub 是否活着
    if os.path.exists(pid_file):
        with open(pid_file) as f:
            pid_str = f.read().strip()
        if pid_str:
            try:
                os.kill(int(pid_str), 0)
                _emit('⏭️', f'jobs_hub 守护进程已运行 (PID {pid_str})，跳过')
                return
            except (OSError, ValueError):
                pass  # 进程已死

    # 看门狗没跑 → 启动它
    if os.path.exists(watchdog_script):
        import subprocess
        try:
            subprocess.Popen(
                ['bash', watchdog_script, 'start'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            _emit('🐶', 'jobs_hub 看门狗已启动')
        except Exception as e:
            _emit('❌', f'启动看门狗失败: {e}')
    else:
        _emit('❌', f'看门狗脚本不存在: {watchdog_script}')


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

    if not _on('AUTOSTART_ENABLED', 'true'):
        _emit('⏭️', 'AUTOSTART_ENABLED=false, all skipped')
        return

    _emit('🚀', '正在启动后台服务和定时调度...')

    if _on('AUTOSTART_MONITOR', 'true'):
        _start_monitor()
    if _on('AUTOSTART_JOBS_HUB', 'true'):
        _start_jobs_hub()
    if _on('AUTOSTART_LOW_PRICE_BULL', 'false'):
        _start_low_price_bull()
    if _on('AUTOSTART_NEWS_FLOW', 'false'):
        _start_news_flow()
    if _on('AUTOSTART_SECTOR_STRATEGY', 'false'):
        _start_sector_strategy()
    if _on('AUTOSTART_PORTFOLIO', 'false'):
        _start_portfolio()

    _emit('✅', 'autostart 完成')


# import 时立即执行
autostart()

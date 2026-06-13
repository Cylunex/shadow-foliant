"""
Bot 命令分发框架 — 借鉴 daily_stock_analysis/bot/dispatcher.py

支持 4 种命令源：
  1. Telegram Bot（webhook 模式或 polling）
  2. 钉钉 @ 机器人（接收 outgoing webhook）
  3. 企业微信群（接收回调）
  4. CLI 直接调用（测试用）

命令注册示例：
    @command('analyze')
    def cmd_analyze(args, ctx):
        return f'分析 {args[0]}...'

通过 FastAPI/Flask 暴露 webhook 接口接收外部命令，或者用 polling 模式拉 Telegram。
这里只提供分发核心，外层接入留给用户按需选 Web 框架。
"""

import os
import re
import shlex
import time
import logging
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger(__name__)


# =============================================================================
# 命令注册表
# =============================================================================

_COMMANDS: Dict[str, Dict[str, Any]] = {}
_RATE_LIMITS: Dict[Tuple[str, str], float] = {}  # (user_id, cmd) -> last_call_ts
_RATE_LIMIT_SEC = 3  # 同用户同命令 3 秒内只能调用一次


def command(name: str, description: str = '', admin_only: bool = False):
    """装饰器：注册命令"""
    def deco(func: Callable):
        _COMMANDS[name] = {
            'func': func,
            'description': description,
            'admin_only': admin_only,
            'name': name,
        }
        return func
    return deco


def list_commands() -> List[Dict[str, Any]]:
    return [{'name': c['name'], 'description': c['description'],
             'admin_only': c['admin_only']} for c in _COMMANDS.values()]


# =============================================================================
# 命令解析与分发
# =============================================================================

def parse_command(text: str) -> Tuple[Optional[str], List[str]]:
    """解析 '@bot analyze 600519 1y' → ('analyze', ['600519', '1y'])"""
    text = (text or '').strip()
    if not text:
        return None, []
    # 去掉常见 @bot 前缀
    text = re.sub(r'^@?\w+\s+', '', text, count=1) if text.startswith('@') else text
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    if not parts:
        return None, []
    cmd = parts[0].lstrip('/')  # 兼容 /analyze 这种风格
    return cmd, parts[1:]


def dispatch(text: str, user_id: str = 'cli', is_admin: bool = False) -> str:
    """分发命令到对应处理函数"""
    cmd, args = parse_command(text)
    if cmd is None:
        return '空命令'
    if cmd not in _COMMANDS:
        return f'未知命令: {cmd}\n可用命令：{", ".join(_COMMANDS.keys())}'
    entry = _COMMANDS[cmd]
    if entry['admin_only'] and not is_admin:
        return f'命令 {cmd} 需要管理员权限'
    # 限频
    key = (user_id, cmd)
    now = time.time()
    last = _RATE_LIMITS.get(key, 0)
    if now - last < _RATE_LIMIT_SEC:
        return f'命令 {cmd} 触发太频繁（限 {_RATE_LIMIT_SEC} 秒/次），请稍候'
    _RATE_LIMITS[key] = now
    try:
        return str(entry['func'](args, {'user_id': user_id, 'is_admin': is_admin}))
    except Exception as e:
        log.exception('command failed')
        return f'命令执行失败: {e}'


# =============================================================================
# 内置命令
# =============================================================================

@command('help', '显示帮助')
def _cmd_help(args, ctx):
    lines = ['可用命令：']
    for c in _COMMANDS.values():
        suffix = ' (admin)' if c['admin_only'] else ''
        lines.append(f"  /{c['name']}{suffix} — {c['description']}")
    return '\n'.join(lines)


@command('analyze', '深度分析单只股票  用法: /analyze 600519 [period]')
def _cmd_analyze(args, ctx):
    if not args:
        return '用法: /analyze <股票代码> [period=1y/6mo/3mo/1mo]'
    symbol = args[0]
    period = args[1] if len(args) > 1 else '1y'
    try:
        from agent_tool_groups import collect
        ctx_data = collect(['base', 'kline_technical'], symbol, period=period)
        ind = ctx_data.get('kline_technical', {}).get('indicators', {})
        if not ind:
            return f'{symbol}: 数据采集失败 — {ctx_data}'
        return (f'{symbol} 快速指标:\n'
                f'  价格: {ind.get("price")}\n'
                f'  RSI: {ind.get("rsi")}\n'
                f'  ADX: {ind.get("adx")}（>25 强趋势）\n'
                f'  CCI: {ind.get("cci")}\n'
                f'  ATR: {ind.get("atr")}（止损 ≈ 2*ATR）\n'
                f'\n如需完整 AI 决策，请到 Streamlit 主程序。')
    except Exception as e:
        return f'指标采集失败: {e}'


@command('snapshot', '查看股票今日指标快照  用法: /snapshot 600519')
def _cmd_snapshot(args, ctx):
    if not args:
        return '用法: /snapshot <股票代码>'
    try:
        from jobs_hub import get_indicator_snapshot
        snap = get_indicator_snapshot(args[0])
        if not snap:
            return f'{args[0]}: 当日快照不存在（jobs_hub 是否启动并跑过 15:30 任务？）'
        lines = [f'{args[0]} 今日快照:']
        for k in ('price', 'rsi', 'macd', 'adx', 'atr', 'cci', 'bias_6'):
            if k in snap:
                lines.append(f'  {k}: {snap[k]}')
        return '\n'.join(lines)
    except Exception as e:
        return f'读取快照失败: {e}'


@command('market', '查看大盘当日快照（北向资金等）')
def _cmd_market(args, ctx):
    try:
        from jobs_hub import get_market_snapshot
        snap = get_market_snapshot()
        if not snap:
            return '大盘快照未生成（jobs_hub 是否启动并跑过 15:35 任务？）'
        nf = snap.get('north_flow', [])
        dt = snap.get('dragon_tiger', [])
        return f'今日大盘:\n  北向资金记录数: {len(nf)}\n  龙虎榜上榜股票数: {len(dt)}'
    except Exception as e:
        return f'读取快照失败: {e}'


@command('jobs', '列出已注册的后台任务')
def _cmd_jobs(args, ctx):
    try:
        from jobs_hub import hub
        jobs = hub.list_jobs()
        if not jobs:
            return '未注册任务'
        return '已注册任务:\n' + '\n'.join(
            f"  {j['name']} @ {j['when']} → 下次: {j['next_run']}" for j in jobs
        )
    except Exception as e:
        return f'读取失败: {e}'


@command('runs', '查看最近的任务运行记录')
def _cmd_runs(args, ctx):
    try:
        from jobs_hub import hub
        limit = int(args[0]) if args and args[0].isdigit() else 10
        runs = hub.list_recent_runs(limit)
        if not runs:
            return '无运行记录'
        lines = [f'最近 {len(runs)} 条任务记录:']
        for r in runs:
            lines.append(f"  [{r['status']}] {r['job_name']} {r['started_at']} {r.get('error') or ''}")
        return '\n'.join(lines)
    except Exception as e:
        return f'读取失败: {e}'


@command('channels', '查看可用的推送渠道')
def _cmd_channels(args, ctx):
    try:
        from notification_router import list_available_channels
        chs = list_available_channels()
        return '可用推送渠道: ' + (', '.join(chs) if chs else '(无 — 检查 .env 配置)')
    except Exception as e:
        return f'读取失败: {e}'


# =============================================================================
# Telegram polling 模式（可选 — 不需要公网 webhook）
# =============================================================================

class TelegramPoller:
    """轮询 Telegram getUpdates 接口拉命令，本地运行即可"""

    def __init__(self, bot_token: str = None, allowed_user_ids: List[str] = None,
                 admin_user_ids: List[str] = None):
        self.token = bot_token or os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
        self.allowed = set(allowed_user_ids or [])
        self.admins = set(admin_user_ids or [])
        self._offset = 0
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def _send(self, chat_id: str, text: str):
        if not self.token:
            return
        import requests
        url = f'https://api.telegram.org/bot{self.token}/sendMessage'
        try:
            requests.post(url, json={'chat_id': chat_id, 'text': text}, timeout=10)
        except Exception as e:
            log.warning(f'telegram reply failed: {e}')

    def _loop(self):
        if not self.token:
            log.warning('TELEGRAM_BOT_TOKEN 未配置，poller 退出')
            return
        import requests
        api = f'https://api.telegram.org/bot{self.token}/getUpdates'
        while self._running:
            try:
                r = requests.get(api, params={'offset': self._offset, 'timeout': 30}, timeout=35)
                data = r.json()
                for upd in data.get('result', []):
                    self._offset = upd['update_id'] + 1
                    msg = upd.get('message') or upd.get('edited_message') or {}
                    text = msg.get('text', '')
                    chat = msg.get('chat', {})
                    user_id = str(msg.get('from', {}).get('id', ''))
                    chat_id = str(chat.get('id', ''))
                    if self.allowed and user_id not in self.allowed:
                        continue
                    if not text:
                        continue
                    is_admin = user_id in self.admins
                    reply = dispatch(text, user_id=user_id, is_admin=is_admin)
                    self._send(chat_id, reply)
            except Exception as e:
                log.warning(f'telegram poll error: {e}')
                time.sleep(5)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False


# =============================================================================
# CLI 自检 — 命令行直接调用命令
# =============================================================================

if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('=== Bot Dispatcher 自检 ===')
        print()
        print(dispatch('/help'))
        print()
        print('--- 试调 /jobs ---')
        print(dispatch('/jobs'))
        print()
        print('--- 试调 /channels ---')
        print(dispatch('/channels'))
        sys.exit(0)
    # 执行命令行参数作为命令
    print(dispatch(' '.join(sys.argv[1:])))

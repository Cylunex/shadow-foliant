"""
通知路由层 — 借鉴 daily_stock_analysis/notification_routing.py 设计

特性：
  1. 多目的地：单条消息可同时发到多个渠道
  2. 分类路由：按 category（alert / report / system_error / daily_summary）
     路由到不同目的地，支持降噪和优先级
  3. 新增渠道：企业微信 / Telegram / Discord / Slack / Pushover / Ntfy
     已有邮件 + 钉钉 + 飞书沿用 NotificationService

环境变量配置（在 .env 中）：
  WECHAT_WORK_WEBHOOK   企业微信群机器人 Webhook
  TELEGRAM_BOT_TOKEN    Telegram Bot Token
  TELEGRAM_CHAT_ID      Telegram 接收对话 ID
  DISCORD_WEBHOOK_URL   Discord 频道 Webhook
  SLACK_WEBHOOK_URL     Slack Incoming Webhook

  NOTIFICATION_ROUTE_<CATEGORY>  逗号分隔的渠道列表（默认所有可用）
  例: NOTIFICATION_ROUTE_ALERT=dingtalk,telegram
       NOTIFICATION_ROUTE_REPORT=email,wechat_work
"""

import os
import json
import logging
import requests
from datetime import datetime
from typing import Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger(__name__)


# =============================================================================
# 新增渠道实现
# =============================================================================

def _send_wechat_work(title: str, content: str) -> Tuple[bool, str]:
    """企业微信群机器人"""
    url = os.getenv('WECHAT_WORK_WEBHOOK', '').strip()
    if not url:
        return False, 'WECHAT_WORK_WEBHOOK 未配置'
    payload = {
        'msgtype': 'markdown',
        'markdown': {'content': f'## {title}\n\n{content}'},
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        ret = r.json()
        if ret.get('errcode') == 0:
            return True, 'ok'
        return False, str(ret)
    except Exception as e:
        return False, str(e)


def _send_telegram(title: str, content: str) -> Tuple[bool, str]:
    """Telegram Bot"""
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat_id:
        return False, 'TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未配置'
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    payload = {
        'chat_id': chat_id,
        'text': f'*{title}*\n\n{content}',
        'parse_mode': 'Markdown',
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        ret = r.json()
        if ret.get('ok'):
            return True, 'ok'
        return False, str(ret)
    except Exception as e:
        return False, str(e)


def _send_discord(title: str, content: str) -> Tuple[bool, str]:
    """Discord Webhook"""
    url = os.getenv('DISCORD_WEBHOOK_URL', '').strip()
    if not url:
        return False, 'DISCORD_WEBHOOK_URL 未配置'
    payload = {
        'embeds': [{
            'title': title,
            'description': content,
            'color': 0x00b894,
            'timestamp': datetime.utcnow().isoformat(),
        }]
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code in (200, 204):
            return True, 'ok'
        return False, f'HTTP {r.status_code}: {r.text[:200]}'
    except Exception as e:
        return False, str(e)


def _send_slack(title: str, content: str) -> Tuple[bool, str]:
    """Slack Incoming Webhook"""
    url = os.getenv('SLACK_WEBHOOK_URL', '').strip()
    if not url:
        return False, 'SLACK_WEBHOOK_URL 未配置'
    payload = {
        'text': f'*{title}*',
        'attachments': [{'text': content, 'color': '#36a64f'}],
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code in (200, 204):
            return True, 'ok'
        return False, f'HTTP {r.status_code}: {r.text[:200]}'
    except Exception as e:
        return False, str(e)


def _send_dingtalk_via_legacy(title: str, content: str) -> Tuple[bool, str]:
    """复用现有 NotificationService 的钉钉/飞书发送（避免重写）"""
    try:
        from notification_service import NotificationService
        ns = NotificationService()
        cfg = ns.config
        if not cfg.get('webhook_enabled'):
            return False, 'webhook 未启用'
        if cfg.get('webhook_type') != 'dingtalk':
            return False, '当前 webhook_type 不是 dingtalk'
        url = cfg.get('webhook_url', '').strip()
        if not url:
            return False, 'WEBHOOK_URL 未配置'
        keyword = cfg.get('webhook_keyword', '')
        safe_title = f'{keyword} {title}' if keyword else title
        payload = {
            'msgtype': 'markdown',
            'markdown': {'title': safe_title, 'text': f'# {title}\n\n{content}'},
        }
        r = requests.post(url, json=payload, timeout=10)
        ret = r.json()
        if ret.get('errcode') == 0 or ret.get('ok') == True:
            return True, 'ok'
        return False, str(ret)
    except Exception as e:
        return False, str(e)


def _send_feishu_via_legacy(title: str, content: str) -> Tuple[bool, str]:
    """飞书机器人 — 复用现有配置"""
    try:
        from notification_service import NotificationService
        ns = NotificationService()
        cfg = ns.config
        if not cfg.get('webhook_enabled') or cfg.get('webhook_type') != 'feishu':
            return False, 'feishu 未启用'
        url = cfg.get('webhook_url', '').strip()
        if not url:
            return False, 'WEBHOOK_URL 未配置'
        payload = {
            'msg_type': 'interactive',
            'card': {
                'header': {'title': {'tag': 'plain_text', 'content': title}},
                'elements': [{'tag': 'markdown', 'content': content}],
            }
        }
        r = requests.post(url, json=payload, timeout=10)
        ret = r.json()
        if ret.get('StatusCode') == 0 or ret.get('code') == 0:
            return True, 'ok'
        return False, str(ret)
    except Exception as e:
        return False, str(e)


def _send_email_via_legacy(title: str, content: str) -> Tuple[bool, str]:
    """邮件 — 用通用 send_email(subject,content)。不复用股票监测专用接口(那个要 name/symbol 会 KeyError)。"""
    try:
        from notification_service import NotificationService
        ns = NotificationService()
        if not ns.config.get('email_enabled'):
            return False, 'EMAIL_ENABLED 未开'
        if not (ns.config.get('email_password') or '').strip():
            return False, 'EMAIL_PASSWORD 未配置(QQ邮箱需填授权码)'
        if hasattr(ns, 'send_email'):
            ok = ns.send_email(title, content)
            return (bool(ok), 'ok' if ok else 'send failed')
        return False, 'NotificationService 无 send_email'
    except Exception as e:
        return False, str(e)


def _send_qq(title: str, content: str) -> Tuple[bool, str]:
    """QQ 机器人 Webhook"""
    url = os.getenv('QQ_WEBHOOK_URL', '').strip()
    if not url:
        return False, 'QQ_WEBHOOK_URL 未配置'
    payload = {
        'msgtype': 'markdown',
        'markdown': {'title': title, 'text': content},
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code in (200, 204):
            return True, 'ok'
        return False, f'HTTP {r.status_code}: {r.text[:200]}'
    except Exception as e:
        return False, str(e)


# =============================================================================
# 渠道注册表
# =============================================================================

CHANNELS = {
    'email':       _send_email_via_legacy,
    'dingtalk':    _send_dingtalk_via_legacy,
    'feishu':      _send_feishu_via_legacy,
    'wechat_work': _send_wechat_work,
    'telegram':    _send_telegram,
    'discord':     _send_discord,
    'slack':       _send_slack,
    'qq':          _send_qq,
}


def list_available_channels() -> List[str]:
    """返回当前环境配置已就绪的渠道（环境变量已填）"""
    avail = []
    for name in CHANNELS:
        if name == 'email' and os.getenv('EMAIL_ENABLED', '').lower() == 'true':
            avail.append(name)
        elif name == 'dingtalk' and os.getenv('WEBHOOK_TYPE', '').lower() == 'dingtalk' \
                and os.getenv('WEBHOOK_ENABLED', '').lower() == 'true':
            avail.append(name)
        elif name == 'feishu' and os.getenv('WEBHOOK_TYPE', '').lower() == 'feishu' \
                and os.getenv('WEBHOOK_ENABLED', '').lower() == 'true':
            avail.append(name)
        elif name == 'wechat_work' and os.getenv('WECHAT_WORK_WEBHOOK'):
            avail.append(name)
        elif name == 'telegram' and os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID'):
            avail.append(name)
        elif name == 'discord' and os.getenv('DISCORD_WEBHOOK_URL'):
            avail.append(name)
        elif name == 'slack' and os.getenv('SLACK_WEBHOOK_URL'):
            avail.append(name)
        elif name == 'qq' and os.getenv('QQ_WEBHOOK_URL'):
            avail.append(name)
    return avail


# =============================================================================
# 路由配置(2026-06-12 重构:webhook 与邮件分离,默认 webhook,邮件只发存档类)
# =============================================================================

CATEGORIES = ['alert', 'report', 'archive', 'system_error', 'daily_summary']

# 内置默认路由 — 全部默认走 QQ webhook;只有 archive(长文存档:周报/AI评估等)走 邮件+QQ。
# 每个类别可用 env NOTIFICATION_ROUTE_<CATEGORY> 覆盖。
DEFAULT_ROUTES: Dict[str, List[str]] = {
    'alert':         ['qq'],            # 即时告警(监控触发/任务失败/风险预警)
    'report':        ['qq'],            # 日常报告(晨报/早盘/尾盘/选股/进化日报)
    'archive':       ['email', 'qq'],   # 长文存档(周报/AI评估周报等,需要留档检索的)
    'system_error':  ['qq'],
    'daily_summary': ['qq'],
}


def _get_routes_for(category: str) -> List[str]:
    """获取某类别的目的渠道列表:env 覆盖 > 内置默认 > qq 兜底"""
    env_key = f'NOTIFICATION_ROUTE_{category.upper()}'
    raw = os.getenv(env_key, '').strip()
    if raw:
        return [c.strip() for c in raw.split(',') if c.strip() in CHANNELS]
    routes = DEFAULT_ROUTES.get(category)
    if routes:
        # 只保留已配置就绪的渠道;一个都不可用时仍返回原列表(让失败显式暴露,而非静默)
        avail = set(list_available_channels())
        ready = [c for c in routes if c in avail]
        return ready or list(routes)
    return ['qq']


def send(category: str, title: str, content: str,
         only_channels: Optional[List[str]] = None,
         fallback: Optional[str] = 'email') -> Dict[str, Tuple[bool, str]]:
    """统一发送入口(所有业务推送都应走这里,不要在业务代码里直连 webhook)

    Args:
        category: alert / report / archive / system_error / daily_summary
        title: 消息标题
        content: 消息内容（支持 markdown）
        only_channels: 强制使用某些渠道（覆盖路由配置）
        fallback: 主渠道全部失败时的兜底渠道(默认 email;传 None 关闭兜底)

    Returns:
        {channel_name: (ok, message)} 每个渠道的发送结果
    """
    targets = only_channels or _get_routes_for(category)
    results: Dict[str, Tuple[bool, str]] = {}
    for ch in targets:
        sender = CHANNELS.get(ch)
        if sender is None:
            results[ch] = (False, 'unknown channel')
            continue
        try:
            results[ch] = sender(title, content)
        except Exception as e:
            results[ch] = (False, str(e))

    # 兜底:主渠道全军覆没(如 QQ 代理没开)→ 试邮件,保证消息不丢
    if (fallback and fallback in CHANNELS and fallback not in results
            and results and not any(ok for ok, _ in results.values())):
        try:
            results[fallback] = CHANNELS[fallback](title, content)
        except Exception as e:
            results[fallback] = (False, str(e))
    return results


# =============================================================================
# 命令行自检
# =============================================================================

if __name__ == '__main__':
    print('=== Notification Router 自检 ===')
    print('可用渠道:', list_available_channels())
    print()
    for cat in CATEGORIES:
        print(f'  {cat} -> {_get_routes_for(cat)}')
    print()
    print('Channels 注册表:')
    for k in CHANNELS:
        print(f'  - {k}')

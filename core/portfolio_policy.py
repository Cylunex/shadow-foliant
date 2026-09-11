"""组合级动作总闸：让所有自动决策遵守当前仓位策略。"""

import json
import os
from datetime import datetime, timedelta
from typing import Dict, Optional
from zoneinfo import ZoneInfo

import _bootstrap
from db_compat import connect as db_connect

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _coerce_ttl(raw_ttl: object, default: int) -> int:
    """将 TTL 配置转成正整数；非法值回退到默认。"""
    try:
        ttl = int(str(raw_ttl).strip())
    except Exception:
        return max(1, default)
    return max(1, ttl)


BUILTIN_TTL_MINUTES = _coerce_ttl(os.getenv("MARKET_ADD_SIGNAL_TTL_MINUTES"), 120)


BUY_ACTIONS = {'buy', 'add'}


def mode() -> str:
    value = os.getenv('PORTFOLIO_POSITION_MODE', 'normal').strip().lower()
    return value if value in {'normal', 'high'} else 'normal'


def latest_market_add_signal() -> Optional[Dict]:
    try:
        today = datetime.now(SHANGHAI).strftime('%Y-%m-%d')
        conn = db_connect(_bootstrap.db_path('jobs_snapshots.db'))
        cur = conn.cursor()
        cur.execute('''SELECT indicators FROM indicator_snapshots
                       WHERE symbol=? AND snapshot_date=? LIMIT 1''',
                    ('_market_add_signal', today))
        raw = cur.fetchone()
        conn.close()
        if raw:
            value = raw[0]
            row = value if isinstance(value, dict) else json.loads(value)
            if isinstance(row, dict) and str(row.get('date') or '') == today:
                row = dict(row)
                row['fresh'] = is_fresh_market_add_signal(row)
                return row
    except Exception:
        pass
    return None


def _coerce_datetime(value: object) -> Optional[datetime]:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed


def is_fresh_market_add_signal(signal: Optional[Dict], *, now: Optional[datetime] = None,
                               ttl_minutes: Optional[int] = None) -> bool:
    if not isinstance(signal, dict):
        return False
    ttl = _coerce_ttl(os.getenv("MARKET_ADD_SIGNAL_TTL_MINUTES"), BUILTIN_TTL_MINUTES)
    if ttl_minutes is not None:
        ttl = ttl_minutes
    ttl = max(1, ttl)
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    current = current.astimezone(SHANGHAI)

    updated_at = _coerce_datetime(signal.get('updated_at'))
    if updated_at is None:
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=SHANGHAI)
    updated_at = updated_at.astimezone(SHANGHAI)
    if str(signal.get('date') or '') != current.date().isoformat():
        return False
    return timedelta(0) <= current - updated_at <= timedelta(minutes=ttl)


def guard(action: str, source_type: str = 'analysis', reason: str = '') -> Dict:
    """高仓位时，自动买入/加仓必须有当天“强力买入”信号，否则降为观察。

    manual 明确代表用户操作，不做拦截；数据/判断缺失一律 fail-closed。
    """
    action = str(action or 'hold').lower()
    result = {'mode': mode(), 'original_action': action, 'action': action,
              'blocked': False, 'reason': reason or ''}
    if result['mode'] != 'high' or action not in BUY_ACTIONS or source_type == 'manual':
        return result
    add_signal = latest_market_add_signal()
    signal_action = (add_signal or {}).get('action')
    if (add_signal and (add_signal.get('fresh') or is_fresh_market_add_signal(add_signal))
            and (signal_action == 'strong_buy' or add_signal.get('must_add') is True)):
        result['market_add_signal'] = add_signal
        return result
    result.update({
        'action': 'watch', 'blocked': True,
        'market_add_signal': add_signal,
        'reason': ('[高仓位总闸] 原建议为%s；今日组合动作不是“强力买入”，自动降为观察。%s'
                   % ('买入' if action == 'buy' else '增持', (' ' + reason) if reason else '')),
    })
    return result


def status() -> Dict:
    signal = latest_market_add_signal()
    is_fresh = is_fresh_market_add_signal(signal) if signal else False
    if signal is not None:
        signal = dict(signal)
        if not is_fresh:
            signal['action'] = 'unknown'
            signal['action_cn'] = '数据不足·默认持有'
            signal['must_add'] = False
            signal['level'] = 'unknown'
        signal['fresh'] = is_fresh
        signal['stale'] = not is_fresh
    return {
        'position_mode': mode(),
        'buy_gate': 'strong_buy_only' if mode() == 'high' else 'normal',
        'market_add_signal': signal,
        'fail_closed': mode() == 'high' and not is_fresh,
    }

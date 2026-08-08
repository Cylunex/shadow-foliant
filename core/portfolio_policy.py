"""组合级动作总闸：让所有自动决策遵守当前仓位策略。"""

import json
import os
from datetime import datetime
from typing import Dict, Optional

import _bootstrap
from db_compat import connect as db_connect


BUY_ACTIONS = {'buy', 'add'}


def mode() -> str:
    value = os.getenv('PORTFOLIO_POSITION_MODE', 'normal').strip().lower()
    return value if value in {'normal', 'high'} else 'normal'


def latest_market_add_signal() -> Optional[Dict]:
    try:
        today = datetime.now().strftime('%Y-%m-%d')
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
                return row
    except Exception:
        pass
    return None


def guard(action: str, source_type: str = 'analysis', reason: str = '') -> Dict:
    """高仓位时，自动买入/加仓必须有当天“必须加仓”信号，否则降为观察。

    manual 明确代表用户操作，不做拦截；数据/判断缺失一律 fail-closed。
    """
    action = str(action or 'hold').lower()
    result = {'mode': mode(), 'original_action': action, 'action': action,
              'blocked': False, 'reason': reason or ''}
    if result['mode'] != 'high' or action not in BUY_ACTIONS or source_type == 'manual':
        return result
    add_signal = latest_market_add_signal()
    if add_signal and add_signal.get('must_add') is True:
        result['market_add_signal'] = add_signal
        return result
    result.update({
        'action': 'watch', 'blocked': True,
        'market_add_signal': add_signal,
        'reason': ('[高仓位总闸] 原建议为%s；今天不是“必须加仓”窗口，自动降为观察。%s'
                   % ('买入' if action == 'buy' else '增持', (' ' + reason) if reason else '')),
    })
    return result


def status() -> Dict:
    signal = latest_market_add_signal()
    return {
        'position_mode': mode(),
        'buy_gate': 'must_add_only' if mode() == 'high' else 'normal',
        'market_add_signal': signal,
        'fail_closed': mode() == 'high' and not signal,
    }

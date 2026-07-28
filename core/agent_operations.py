"""Agent/Web 共用的管理写操作。

所有破坏性操作默认 dry_run=True；返回统一 envelope 和变更前后信息。
"""

from typing import Any, Dict, Optional

from agent_contract import envelope


def list_monitors() -> Dict[str, Any]:
    from monitor_db import monitor_db
    rows = monitor_db.get_monitored_stocks() or []
    return envelope({'count': len(rows), 'items': rows}, sources=['monitor_db'])


def upsert_monitor(code: str, name: str = '', rating: str = '持有',
                   entry_low: Optional[float] = None,
                   entry_high: Optional[float] = None,
                   take_profit: Optional[float] = None,
                   stop_loss: Optional[float] = None,
                   check_interval: int = 60,
                   notification_enabled: bool = True,
                   trading_hours_only: bool = True,
                   dry_run: bool = True) -> Dict[str, Any]:
    from monitor_db import monitor_db
    code = ''.join(ch for ch in str(code or '') if ch.isdigit())[-6:]
    if len(code) != 6:
        return envelope(None, status='failed', warnings=['code 必须是 6 位 A 股代码'])
    before = monitor_db.get_monitor_by_code(code)
    old_range = (before or {}).get('entry_range') or {}
    entry_low = entry_low if entry_low is not None else (
        old_range.get('min', old_range.get('low')))
    entry_high = entry_high if entry_high is not None else (
        old_range.get('max', old_range.get('high')))
    take_profit = take_profit if take_profit is not None else (before or {}).get('take_profit')
    stop_loss = stop_loss if stop_loss is not None else (before or {}).get('stop_loss')
    if entry_low is None or entry_high is None:
        return envelope(
            {'before': before}, status='failed',
            missing_fields=['entry_low', 'entry_high'],
            warnings=['新增监控必须提供进场区间'],
        )
    if float(entry_low) > float(entry_high):
        entry_low, entry_high = entry_high, entry_low
    proposed = {
        'symbol': code,
        'name': name or (before or {}).get('name') or code,
        'rating': rating or (before or {}).get('rating') or '持有',
        'entry_range': {'min': float(entry_low), 'max': float(entry_high)},
        'take_profit': float(take_profit) if take_profit is not None else None,
        'stop_loss': float(stop_loss) if stop_loss is not None else None,
        'check_interval': max(10, int(check_interval)),
        'notification_enabled': bool(notification_enabled),
        'trading_hours_only': bool(trading_hours_only),
    }
    if dry_run:
        return envelope(
            {'dry_run': True, 'operation': 'update' if before else 'add',
             'before': before, 'after': proposed},
            sources=['monitor_db'],
        )
    if before:
        ok = monitor_db.update_monitored_stock(
            before['id'], proposed['rating'], proposed['entry_range'],
            proposed['take_profit'], proposed['stop_loss'], proposed['check_interval'],
            proposed['notification_enabled'], proposed['trading_hours_only'],
            name=proposed['name'])
        monitor_id = before['id']
        operation = 'update'
    else:
        monitor_id = monitor_db.add_monitored_stock(
            proposed['symbol'], proposed['name'], proposed['rating'],
            proposed['entry_range'], proposed['take_profit'], proposed['stop_loss'],
            proposed['check_interval'], proposed['notification_enabled'],
            proposed['trading_hours_only'])
        ok = bool(monitor_id)
        operation = 'add'
    after = monitor_db.get_monitor_by_code(code)
    return envelope(
        {'dry_run': False, 'operation': operation, 'updated': bool(ok),
         'monitor_id': monitor_id, 'before': before, 'after': after},
        status='success' if ok else 'failed', sources=['monitor_db'],
    )


def remove_monitor(code: str, dry_run: bool = True) -> Dict[str, Any]:
    from monitor_db import monitor_db
    code = ''.join(ch for ch in str(code or '') if ch.isdigit())[-6:]
    before = monitor_db.get_monitor_by_code(code)
    if not before:
        return envelope({'removed': False, 'before': None}, status='missing',
                        warnings=[f'未找到监控股票 {code}'], sources=['monitor_db'])
    if dry_run:
        return envelope({'dry_run': True, 'operation': 'remove', 'before': before},
                        sources=['monitor_db'])
    ok = monitor_db.remove_monitored_stock(before['id'])
    return envelope(
        {'dry_run': False, 'removed': bool(ok), 'before': before},
        status='success' if ok else 'failed', sources=['monitor_db'],
    )


def list_recommendations(code: str = '', monitored_only: bool = False,
                         limit: int = 100) -> Dict[str, Any]:
    from ai_recommendation_monitor import list_active
    rows = list_active(symbol=code or None, only_monitored=monitored_only,
                       limit=max(1, min(int(limit), 500)))
    return envelope({'count': len(rows), 'items': rows},
                    sources=['ai_recommendations'])


def enable_recommendation_monitor(rec_id: int, dry_run: bool = True) -> Dict[str, Any]:
    from ai_recommendation_monitor import _get, enable_monitor
    before = _get(int(rec_id))
    if not before:
        return envelope(None, status='missing', warnings=[f'推荐 {rec_id} 不存在'])
    if dry_run:
        return envelope({'dry_run': True, 'operation': 'enable_monitor', 'before': before},
                        sources=['ai_recommendations', 'monitor_db'])
    ok = enable_monitor(int(rec_id))
    return envelope(
        {'dry_run': False, 'enabled': bool(ok), 'recommendation': before},
        status='success' if ok else 'failed',
        sources=['ai_recommendations', 'monitor_db'],
    )


def close_recommendation(rec_id: int, reason: str = 'manual',
                         close_price: Optional[float] = None,
                         dry_run: bool = True) -> Dict[str, Any]:
    from ai_recommendation_monitor import _get, close_recommendation as _close
    before = _get(int(rec_id))
    if not before:
        return envelope(None, status='missing', warnings=[f'推荐 {rec_id} 不存在'])
    if dry_run:
        return envelope(
            {'dry_run': True, 'operation': 'close', 'reason': reason,
             'close_price': close_price, 'before': before},
            sources=['ai_recommendations'],
        )
    result = _close(int(rec_id), reason=reason, close_price=close_price)
    return envelope(result, status='success' if result.get('closed') else 'failed',
                    sources=['ai_recommendations'])


def set_signal_status(signal_id: int, status: str,
                      dry_run: bool = True) -> Dict[str, Any]:
    from decision_signal import get_signal, update_status, TERMINAL_STATUS
    before = get_signal(int(signal_id))
    if not before:
        return envelope(None, status='missing', warnings=[f'决策信号 {signal_id} 不存在'])
    if status not in TERMINAL_STATUS:
        return envelope(
            {'before': before}, status='failed',
            warnings=[f'status 仅允许: {", ".join(TERMINAL_STATUS)}'],
        )
    if dry_run:
        return envelope(
            {'dry_run': True, 'operation': 'set_status', 'status': status,
             'before': before}, sources=['decision_signals'])
    ok = update_status(int(signal_id), status)
    return envelope(
        {'dry_run': False, 'updated': bool(ok), 'status': status, 'before': before},
        status='success' if ok else 'failed', sources=['decision_signals'],
    )

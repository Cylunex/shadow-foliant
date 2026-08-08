"""盘后非紧急通知缓冲。

盘前/盘中通知不经过本模块；盘后风险告警仍即时发送。普通盘后报告先按日期落盘，
由 22:30 盈亏任务合成一条，既避免刷屏，也保留各任务完整产物供 Agent/日志查询。
"""

import json
import os
import threading
from datetime import datetime
from typing import Dict, List, Optional

import _bootstrap


_LOCK = threading.Lock()
_DIR = os.path.join(_bootstrap.ROOT, 'db', 'postmarket_digest')


def _day(day: Optional[str] = None) -> str:
    return day or datetime.now().strftime('%Y-%m-%d')


def _path(day: Optional[str] = None) -> str:
    return os.path.join(_DIR, f'{_day(day)}.json')


def add_section(key: str, title: str, text: str, *, max_chars: int = 1200,
                day: Optional[str] = None) -> None:
    """新增/覆盖一个盘后栏目。相同 key 重跑时覆盖，避免重复推送。"""
    body = str(text or '').strip()
    if not body:
        return
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + '\n…（完整内容请由 Agent 查询任务产物）'
    with _LOCK:
        os.makedirs(_DIR, exist_ok=True)
        path = _path(day)
        payload: Dict = {'day': _day(day), 'sections': {}}
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                old = json.load(fh)
            if isinstance(old, dict):
                payload.update(old)
        except Exception:
            pass
        sections = payload.setdefault('sections', {})
        sections[str(key)] = {
            'title': str(title or key), 'text': body,
            'updated_at': datetime.now().astimezone().isoformat(),
        }
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def load_sections(day: Optional[str] = None) -> List[Dict[str, str]]:
    with _LOCK:
        try:
            with open(_path(day), 'r', encoding='utf-8') as fh:
                payload = json.load(fh)
        except Exception:
            return []
    rows = (payload or {}).get('sections') or {}
    return [dict(value, key=key) for key, value in rows.items()
            if isinstance(value, dict) and value.get('text')]


def format_digest(day: Optional[str] = None) -> str:
    sections = load_sections(day)
    if not sections:
        return ''
    order = {'pnl': 0, 'mx_close': 10, 'sector_rotation': 20,
             'eod_info': 30, 'strategy_scan': 40, 'strategy_evolution': 50}
    sections.sort(key=lambda row: (order.get(row.get('key'), 100),
                                   row.get('updated_at') or ''))
    return '\n\n'.join(
        f"━━ {row.get('title') or row['key']} ━━\n{row['text'].strip()}"
        for row in sections
    )

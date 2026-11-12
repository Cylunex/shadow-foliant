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
    # Keep the original intact. Mobile budgets belong to the renderer, not the
    # source archive; cutting here also used to sever numbered citations.
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


def format_notification_digest(day: Optional[str] = None) -> str:
    from notify.stock_report import report, text
    from notify.report_details import history_reference
    import re
    sections = load_sections(day)
    if not sections:
        return ''
    order = {'pnl': 0, 'mx_close': 10, 'sector_rotation': 20,
             'eod_info': 30, 'strategy_scan': 40, 'strategy_evolution': 50}
    rendered = []
    for section in sorted(sections, key=lambda s: order.get(s.get('key'), 100)):
        key = section['key']
        lines = [line.strip() for line in section['text'].splitlines() if line.strip()]
        if key == 'pnl':
            summary = [text(line, 80) for line in lines[:3]]
        elif key == 'mx_close':
            # Numbered model citations are not verified source URLs. Retain the
            # raw research in the archive without promoting its units/actions.
            summary = ['模型复盘已生成，来源和指标口径待核验；原稿仅供研究，持仓动作以同日尾盘复核为准。']
        elif key == 'strategy_scan':
            summary = [text(line, 95) for line in lines if line.startswith(('股票池:', '1.'))][:2]
        elif key == 'sector_rotation':
            summary = [text(line, 95) for line in lines if '看多:' in line or '看空:' in line][:2]
        elif key == 'strategy_evolution':
            score = next((line for line in lines if re.search(r'触发\d+/\d+只', line)), '')
            pool = re.search(r'触发\d+/(\d+)只', score)
            summary = [f'历史回测样本池{pool.group(1)}只；非全市场实盘收益。' if pool else '历史样本评估，非实盘收益。',
                       text(score, 95)]
        else:
            useful = [line for line in lines if not line.startswith(('━━', '【', '📢', '📑', '===', '#'))]
            summary = [text(line, 100) for line in useful[:2]]
        rendered.append((text(section['title'], 18),
                         [text(line, 70) for line in summary if line][:3 if key == 'pnl' else 2]
                         or ['本轮无可核验摘要，详见完整报告。']))
    return report('盘后汇总', _day(day), rendered[:6], [history_reference()])

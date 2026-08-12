"""个股在当前题材/行业中的位置。

只消费 research_stock 已经取得的基础信息、概念归属和热点列表，不再联网。
没有历史题材序列时明确返回 phase=unknown，避免把一次横截面热度伪装成升温趋势。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional


def _text(value: Any) -> str:
    return str(value or '').strip()


def _code(value: Any) -> str:
    digits = ''.join(ch for ch in _text(value) if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _items(value: Any) -> List[str]:
    if isinstance(value, str):
        value = re.split(r'[+,，、;/\s]+', value)
    if not isinstance(value, (list, tuple, set)):
        return []
    out = []
    for item in value:
        text = _text(item.get('name') if isinstance(item, dict) else item)
        if text and text not in out:
            out.append(text)
    return out


def _base_info(context: Dict[str, Any]) -> Dict[str, Any]:
    base = context.get('base') if isinstance(context.get('base'), dict) else {}
    info = base.get('info') if isinstance(base.get('info'), dict) else {}
    return info


def _matches(left: str, right: str) -> bool:
    left = re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]', '', _text(left)).lower()
    right = re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]', '', _text(right)).lower()
    return bool(left and right and (left in right or right in left))


def _hot_theme_rows(sentiment: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for index, item in enumerate(sentiment.get('hot_themes') or [], 1):
        if not isinstance(item, dict) or not _text(item.get('theme')):
            continue
        rows.append({
            'theme': _text(item.get('theme')),
            'count': _int(item.get('count')),
            'rank': index,
        })
    return rows


def _stock_is_hot(code: str, rows: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for index, item in enumerate(rows or [], 1):
        if not isinstance(item, dict):
            continue
        raw_code = next((item.get(key) for key in (
            'code', 'symbol', '股票代码', '证券代码', '股票代码_1') if item.get(key)), '')
        if _code(raw_code) == code:
            return {'rank': index, 'row': item}
    return None


def build_market_structure(context: Dict[str, Any], code: str) -> Dict[str, Any]:
    """从已有上下文判断题材匹配与龙头/跟随角色。"""
    context = context if isinstance(context, dict) else {}
    code = _code(code)
    info = _base_info(context)
    sentiment = context.get('sentiment') if isinstance(context.get('sentiment'), dict) else {}
    blocks = sentiment.get('concept_blocks') if isinstance(sentiment.get('concept_blocks'), dict) else {}

    industries = _items(blocks.get('industry'))
    if not industries:
        industries = _items([info.get('industry') or info.get('sector')])
    concepts = _items(blocks.get('concept') or blocks.get('concept_tags'))
    boards = list(dict.fromkeys([*industries, *concepts]))
    hot_themes = _hot_theme_rows(sentiment)

    matches = []
    for hot in hot_themes:
        matched_boards = [board for board in boards if _matches(board, hot['theme'])]
        if matched_boards:
            matches.append({**hot, 'matched_boards': matched_boards})
    matches.sort(key=lambda item: (item['rank'], -item['count'], item['theme']))
    primary = matches[0] if matches else None
    hot_stock = _stock_is_hot(code, sentiment.get('hot_stocks') or [])

    hot_stock_text = ' '.join(
        _text(value) for value in ((hot_stock or {}).get('row') or {}).values())
    hot_stock_matches_primary = bool(
        hot_stock and primary
        and (_matches(primary['theme'], hot_stock_text)
             or any(_matches(board, hot_stock_text) for board in primary['matched_boards']))
    )
    if hot_stock_matches_primary:
        role = 'leader' if hot_stock['rank'] <= 10 else 'follower'
        role_cn = '热点前排' if role == 'leader' else '热点跟随'
    elif primary:
        role, role_cn = 'follower', '题材内跟随，未进入热点前排'
    else:
        role, role_cn = 'unknown', '缺少热点榜匹配证据'

    limitations = []
    if not boards:
        limitations.append('缺少行业/概念归属')
    if not hot_themes:
        limitations.append('缺少当前热点题材榜')
    limitations.append('缺少历史题材热度序列，阶段不做推测')
    status = 'available' if primary else ('partial' if boards or hot_themes else 'missing')
    return {
        'status': status,
        'primary_theme': primary['theme'] if primary else None,
        'theme_rank': primary['rank'] if primary else None,
        'theme_heat_count': primary['count'] if primary else None,
        'matched_boards': primary['matched_boards'] if primary else [],
        'related_industries': industries,
        'related_concepts': concepts,
        'stock_role': role,
        'stock_role_cn': role_cn,
        'theme_phase': 'unknown',
        'theme_phase_cn': '缺少历史序列，不判断升温/降温',
        'limitations': limitations,
        'method': 'existing-context-only:no-extra-network',
    }


__all__ = ['build_market_structure']

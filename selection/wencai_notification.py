"""Read the current premarket OpenAPI sample for selection notifications only."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from selection.reference_display import openapi_trial_reference as _openapi_trial_reference
from selection.wencai_query_contract import ORDER


def verified_picks(group: dict) -> list[dict]:
    """Use only usable group results, deduplicated in provider order."""
    if group.get('status') not in {'ready', 'trial_verified'}:
        return []
    picks, seen = [], set()
    for row in group.get('picks') or []:
        code = str(row.get('symbol') or row.get('code') or '')
        if not re.fullmatch(r'\d{6}', code) or code in seen:
            continue
        seen.add(code)
        picks.append({**row, 'symbol': code})
        if len(picks) == 5:
            break
    return picks


def reference_top5(reference: dict) -> list[dict]:
    """Display-only consensus: group hits, best group rank, then stock code.

    This is a project summary of the five groups, not an official Wencai rank
    and never an input to the formal selection or trading decisions.
    """
    candidates = {}
    for name in ORDER:
        group = (reference.get('strategies') or {}).get(name) or {}
        for rank, pick in enumerate(verified_picks(group), 1):
            code = pick['symbol']
            row = candidates.setdefault(code, {
                'symbol': code, 'name': pick.get('name') or '',
                'group_count': 0, 'best_rank': rank,
            })
            row['name'] = row['name'] or pick.get('name') or ''
            row['group_count'] += 1
            row['best_rank'] = min(row['best_rank'], rank)
    return sorted(candidates.values(), key=lambda row: (
        -row['group_count'], row['best_rank'], row['symbol'],
    ))[:5]


def load_premarket_reference(path: Path, *, selection_date: str,
                             expected_market_as_of: str) -> dict:
    """Never fetch the provider or fall back to a previous day's sample."""
    failure = 'premarket_cache_missing'
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        sampled = datetime.fromisoformat(payload['executed_at'])
        groups = payload.get('groups') or []
        if (sampled.tzinfo is None
                or sampled.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat() != selection_date
                or payload.get('sampling_slot') != 'premarket'
                or not expected_market_as_of
                or payload.get('expected_market_as_of') != expected_market_as_of
                or len(groups) != len(ORDER)
                or {row.get('name') for row in groups} != set(ORDER)):
            raise ValueError('invalid_premarket_sample')
        reference = _openapi_trial_reference(payload)
        reference['market_as_of'] = expected_market_as_of
        return reference
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        failure = 'premarket_cache_invalid'
    reference = _openapi_trial_reference(None)
    reference['availability_reason'] = failure
    return reference


def format_reference_lines(reference: dict) -> list[str]:
    """One bounded line per group; unverified rows never appear as valid picks."""
    from notify.plain_language import plain_text

    lines = []
    for name in ORDER:
        row = (reference.get('strategies') or {}).get(name) or {}
        status = row.get('status')
        if status == 'trial_verified':
            picks = '、'.join(
                str(pick.get('symbol') or '') + ' ' + plain_text(pick.get('name'), 10)
                for pick in (row.get('picks') or [])[:3]
            ).strip()
            detail = '✅已核验·' + (picks or '无命中')
        elif status == 'trial_unverified':
            detail = '⚠️条件或时点待核验'
        elif status == 'pending':
            detail = ('⚠️盘前缓存无效' if reference.get('availability_reason') == 'premarket_cache_invalid'
                      else '⚠️盘前缓存缺失')
        else:
            detail = '⚠️外部源暂不可用'
        lines.append(plain_text(f'问财参考·{name}：{detail}（仅供参考）', 90))
    return lines

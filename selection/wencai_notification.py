"""Read the current premarket OpenAPI sample for selection notifications only."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from application.services import _openapi_trial_reference
from selection.wencai_query_contract import ORDER


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

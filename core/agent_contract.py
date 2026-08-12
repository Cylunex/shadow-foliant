"""Agent 高层工具的统一返回契约。底层历史工具保持兼容，新工作流统一走这里。

除统一 envelope 外，本模块还负责把各工具组的可用性压成低敏、确定性的
数据质量合同。Agent 不应因为某个字段非空就假定整块证据完整，也不应在核心
行情/K 线降级时继续给出高置信方向性结论。
"""

import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional


VALID_STATUS = {
    'success', 'partial', 'degraded', 'stale', 'missing', 'failed',
    'queued', 'running', 'skipped', 'interrupted',
}

_QUALITY_STATUS_SCORES = {
    'available': 100,
    'partial': 75,
    'fallback': 65,
    'stale': 50,
    'missing': 35,
    'fetch_failed': 25,
}
_QUALITY_WEIGHTS = {
    'base': 20,
    'kline_technical': 30,
    'fund_flow': 15,
    'fundamentals': 15,
    'sentiment': 10,
    'chipset': 5,
    'risk': 5,
    'chan_theory': 10,
    'macro_us': 5,
}
_CORE_GROUPS = {'base', 'kline_technical'}
_NON_PAYLOAD_KEYS = {'symbol', 'period', 'errors', 'error', '_meta', 'data_quality'}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def envelope(data: Any = None, *, status: str = 'success',
             warnings: Optional[Iterable[str]] = None,
             sources: Optional[Iterable[str]] = None,
             missing_fields: Optional[Iterable[str]] = None,
             trace_id: str = '', **meta) -> Dict[str, Any]:
    warnings = [str(x) for x in (warnings or []) if x]
    missing = [str(x) for x in (missing_fields or []) if x]
    status = status if status in VALID_STATUS else 'failed'
    return {
        'ok': status not in ('failed', 'interrupted'),
        'status': status,
        'data': data,
        'meta': {
            'as_of': meta.pop('as_of', now_iso()),
            'sources': list(dict.fromkeys(sources or [])),
            'missing_fields': missing,
            'warnings': warnings,
            'trace_id': trace_id or uuid.uuid4().hex,
            **meta,
        },
    }


def context_warnings(context: Dict[str, Any]) -> List[str]:
    """汇总 agent_tool_groups 各域 errors/error，供 status=partial 判定。"""
    out: List[str] = []
    for group, value in (context or {}).items():
        if not isinstance(value, dict):
            continue
        if value.get('error'):
            out.append(f'{group}: {value["error"]}')
        for err in value.get('errors') or []:
            if err:
                out.append(f'{group}: {err}')
    return out


def _has_payload(value: Any) -> bool:
    """判断工具组是否真的取得了业务数据，而不是只有错误/元数据外壳。"""
    if value is None:
        return False
    if hasattr(value, 'empty'):
        try:
            return not bool(value.empty)
        except Exception:
            return False
    if isinstance(value, dict):
        return any(
            key not in _NON_PAYLOAD_KEYS and _has_payload(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return len(value) > 0
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _group_quality(group: str, value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        status = 'fetch_failed' if value is None else 'available'
        return {'status': status, 'score': _QUALITY_STATUS_SCORES[status],
                'limitations': []}

    errors = []
    if value.get('error'):
        errors.append(str(value['error']))
    errors.extend(str(item) for item in (value.get('errors') or []) if item)
    payload = _has_payload(value)
    quality = value.get('data_quality') if isinstance(value.get('data_quality'), dict) else {}

    if group == 'kline_technical' and quality:
        if not quality.get('usable', True):
            status = 'fetch_failed'
        elif not quality.get('actionable', True) or quality.get('stale'):
            status = 'stale'
        elif errors:
            status = 'partial'
        else:
            status = 'available'
    elif errors and payload:
        status = 'partial'
    elif errors:
        status = 'fetch_failed'
    elif not payload:
        status = 'missing'
    else:
        status = 'available'

    limitations = []
    if status != 'available':
        reason = quality.get('reason') if quality else None
        limitations.append(f'{group}:{reason or status}')
    return {
        'status': status,
        'score': _QUALITY_STATUS_SCORES[status],
        'limitations': limitations,
        'duration_ms': (value.get('_meta') or {}).get('duration_ms')
        if isinstance(value.get('_meta'), dict) else None,
    }


def context_quality(context: Dict[str, Any]) -> Dict[str, Any]:
    """生成分块数据质量、置信度护栏和 Agent 可读阶段摘要。

    总分只按本次实际请求的工具组归一化，quick/deep 两种研究深度因此可直接比较，
    不会因为没有请求筹码或情绪组而被错误扣分。
    """
    blocks: Dict[str, Dict[str, Any]] = {}
    weighted_sum = 0.0
    total_weight = 0.0
    for group, value in (context or {}).items():
        if str(group).startswith('_'):
            continue
        block = _group_quality(group, value)
        blocks[group] = block
        weight = float(_QUALITY_WEIGHTS.get(group, 5))
        weighted_sum += block['score'] * weight
        total_weight += weight

    overall = round(weighted_sum / total_weight) if total_weight else 0
    core_degraded = any(
        blocks.get(group, {}).get('status') not in (None, 'available')
        for group in _CORE_GROUPS if group in blocks
    )
    core_unusable = any(
        blocks.get(group, {}).get('status') in ('stale', 'missing', 'fetch_failed')
        for group in _CORE_GROUPS if group in blocks
    )
    limitations = [
        item
        for block in blocks.values()
        for item in block.get('limitations', [])
    ]
    if overall >= 85:
        level = 'high'
    elif overall >= 65:
        level = 'medium'
    else:
        level = 'low'
    stages = [
        {
            'stage': group,
            'status': block['status'],
            'duration_ms': block.get('duration_ms'),
        }
        for group, block in blocks.items()
    ]
    return {
        'overall_score': overall,
        'level': level,
        'core_degraded': core_degraded,
        'core_unusable': core_unusable,
        'blocks': blocks,
        'limitations': limitations,
        'guardrails': {
            'confidence_cap': 'medium' if core_degraded else 'high',
            'directional_action_allowed': not core_unusable,
            'reason': ('核心行情或日线技术数据不可用，方向结论须降置信并优先观望'
                       if core_unusable else
                       '核心行情或日线技术数据部分降级，高置信结论已受限'
                       if core_degraded else ''),
        },
        'stages': stages,
    }

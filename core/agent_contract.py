"""Agent 高层工具的统一返回契约。底层历史工具保持兼容，新工作流统一走这里。"""

import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional


VALID_STATUS = {
    'success', 'partial', 'degraded', 'stale', 'missing', 'failed',
    'queued', 'running', 'skipped', 'interrupted',
}


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

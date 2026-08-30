"""晨间策略 LLM 结果校验与规则兜底。

晨报的数据采集与 LLM 分析是两层能力：模型超时/空响应时，已经采到的行情仍然
有效。本模块把二者解耦，保证任何时候都不会把解析失败伪装成“（无）”空报告。
"""

from __future__ import annotations

import json
import re
import statistics
from typing import Any, Dict, Iterable, List, Optional, Tuple


_EMPTY_MARKERS = ('无数据', '暂无数据', '无持仓扫描数据', '拉取失败', '扫描失败',
                  '不可用', 'n/a')
_FAILURE_MARKERS = ('api调用失败', 'api返回空响应', '[llm-router]', 'emptyresponse',
                    'request timed out', 'timeout')
_ALIASES = {
    'lazy_summary': ('lazy_summary', 'summary', '今日一句话'),
    'market_direction': ('market_direction', 'direction', '市场方向'),
    'position_action': ('position_action', 'action', '仓位动作'),
    'open_strategy': ('open_strategy', 'opening_strategy', '开盘策略'),
    'external_impact': ('external_impact', '外部影响'),
    'hot_sectors': ('hot_sectors', 'hot_sector', '热点板块'),
    'risk_warning': ('risk_warning', 'risks', '风险提示'),
    'candidate_stocks': ('candidate_stocks', 'candidates', '候选股票'),
    'position_advice': ('position_advice', '持仓建议'),
    'confidence': ('confidence', '数据置信度'),
}


def _meaningful(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip().lower()
    return bool(text and text not in {'（无）', '(无)', '无', '未知', 'none', 'null', '[]'})


def _source_available(value: Any) -> bool:
    if not _meaningful(value):
        return False
    text = str(value).strip().lower()
    return not any(marker in text for marker in _EMPTY_MARKERS)


def _balanced_json_objects(text: str) -> Iterable[str]:
    """从后向前产出花括号配平的 JSON 候选，忽略字符串内部的花括号。"""
    if not text:
        return
    stack: List[int] = []
    pairs: List[Tuple[int, int]] = []
    in_string = False
    escaped = False
    for i, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == '{':
            stack.append(i)
        elif char == '}' and stack:
            start = stack.pop()
            pairs.append((start, i + 1))
    for start, end in reversed(pairs):
        yield text[start:end]


def parse_diagnosis(raw: Any) -> Optional[Dict[str, Any]]:
    """兼容纯 JSON、Markdown 代码块、思考过程和外层 data/result 包装。"""
    if isinstance(raw, dict):
        parsed: Any = raw
    else:
        text = str(raw or '').strip()
        if not text or any(marker in text.lower() for marker in _FAILURE_MARKERS):
            return None
        parsed = None
        try:
            parsed = json.loads(text)
        except Exception:
            for candidate in _balanced_json_objects(text):
                try:
                    value = json.loads(candidate)
                except Exception:
                    continue
                if isinstance(value, dict):
                    parsed = value
                    break
    if not isinstance(parsed, dict):
        return None
    for wrapper in ('data', 'result', 'diagnosis'):
        if isinstance(parsed.get(wrapper), dict):
            parsed = parsed[wrapper]
            break

    normalized: Dict[str, Any] = {}
    for target, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in parsed:
                normalized[target] = parsed[alias]
                break
    return normalized or None


def _pct_values(text: Any) -> List[float]:
    values = []
    for raw in re.findall(r'([+-]?\d+(?:\.\d+)?)\s*%', str(text or '')):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if -30 <= value <= 30:
            values.append(value)
    return values


def _compact_line(text: Any, limit: int = 100) -> str:
    lines = [re.sub(r'^\s+', '', line).strip() for line in str(text or '').splitlines()
             if line.strip()]
    return '；'.join(lines[:2])[:limit]


def _hot_sectors(sources: Dict[str, Any]) -> List[str]:
    sector = str(sources.get('sector_summary') or '')
    match = re.search(r'强势\s*[:：]\s*([^\n]+)', sector)
    if match:
        items = [item.strip() for item in re.split(r'[、,，；;]', match.group(1)) if item.strip()]
        if items:
            return items[:3]
    themes = str(sources.get('themes_summary') or '')
    if _source_available(themes):
        items = [re.sub(r'^\s*[•·-]?\s*', '', line).strip()
                 for line in themes.splitlines() if line.strip()]
        if items:
            return items[:3]
    return ['暂无可靠板块信号，开盘后观察量价确认']


def _confidence(available: int, total: int) -> str:
    ratio = available / total if total else 0
    if ratio >= 0.7:
        return '中'  # 规则兜底不冒充 AI 的“高置信”判断
    return '中' if ratio >= 0.4 else '低'


def build_rule_fallback(sources: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """只基于已采集数据生成保守晨报，不推荐个股、不虚构热点。"""
    source_names = {
        'dragon_tiger_summary': '龙虎榜', 'us_summary': '美股', 'news_summary': '新闻',
        'hot_summary': '强势股', 'themes_summary': '题材',
        'fred_summary': '宏观', 'cn_index_summary': 'A股指数',
        'sector_summary': '行业板块', 'hold_summary': '持仓扫描',
    }
    available_keys = [key for key in source_names if _source_available(sources.get(key))]
    missing = [label for key, label in source_names.items() if key not in available_keys]
    total = len(source_names)
    available = len(available_keys)

    cn_values = _pct_values(sources.get('cn_index_summary'))
    cn_median = statistics.median(cn_values) if cn_values else None
    if cn_median is not None and cn_median <= -1.2:
        market_direction = '看跌'
        position_action = '减仓'
        open_strategy = ('最近可用的 A 股核心指数整体偏弱。开盘先看是否继续放量下跌；'
                         '未止跌不急着加仓，出现企稳再分批处理。')
    elif cn_median is not None and cn_median >= 1.2:
        market_direction = '看涨'
        position_action = '加仓'
        open_strategy = ('最近可用的 A 股核心指数整体偏强。高开不追，先等开盘量价确认；'
                         '高仓位以持有和去弱留强为主。')
    else:
        market_direction = '震荡'
        position_action = '不动'
        open_strategy = ('按震荡方案应对：先观察开盘量价，高开不追、普通小跌不操作；'
                         '只有放量破位或明确止跌信号出现时再调整仓位。')

    external_parts = []
    for key, label in (('us_summary', '隔夜美股'), ('fred_summary', '海外宏观')):
        value = sources.get(key)
        if _source_available(value):
            external_parts.append(f'{label}：{_compact_line(value)}')
    external_impact = '；'.join(external_parts[:2]) or '外部数据暂不完整，开盘以 A 股自身走势为准。'

    risks = ['当前仓位较高，避免追高；盘前数据只能定基调，最终以开盘后的量价为准。']
    if missing:
        risks.append(f"本次 {available}/{total} 类数据可用，缺少{'、'.join(missing[:4])}"
                     + ('等。' if len(missing) > 4 else '。'))
    fallback = {
        'lazy_summary': ('AI 分析暂不可用，本次已自动切换为规则版。' + open_strategy),
        'market_direction': market_direction,
        'position_action': position_action,
        'open_strategy': open_strategy,
        'external_impact': external_impact,
        'hot_sectors': _hot_sectors(sources),
        'risk_warning': ''.join(risks),
        'candidate_stocks': [],
        'confidence': _confidence(available, total),
    }
    return fallback, {'available': available, 'total': total, 'missing': missing}


def _text(value: Any) -> Optional[str]:
    if isinstance(value, (list, tuple)):
        value = '；'.join(str(item).strip() for item in value if _meaningful(item))
    if not _meaningful(value):
        return None
    return str(value).strip()


def _list(value: Any) -> List[str]:
    if isinstance(value, str):
        value = re.split(r'[\n；;]', value)
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if _meaningful(item)][:5]


def build_diagnosis(raw: Any, sources: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """合并经过校验的 AI 字段与规则兜底，并返回可观测元数据。"""
    fallback, coverage = build_rule_fallback(sources)
    parsed = parse_diagnosis(raw)
    diagnosis = dict(fallback)
    used_ai_fields: List[str] = []
    if parsed:
        for key in ('lazy_summary', 'open_strategy', 'external_impact',
                    'risk_warning', 'position_advice'):
            value = _text(parsed.get(key))
            if value:
                diagnosis[key] = value
                used_ai_fields.append(key)
        direction = _text(parsed.get('market_direction'))
        if direction:
            from notify.plain_language import normalize_direction
            diagnosis['market_direction'] = normalize_direction(direction)
            used_ai_fields.append('market_direction')
        action = _text(parsed.get('position_action'))
        if action:
            from notify.plain_language import normalize_action
            diagnosis['position_action'] = normalize_action(action)
            used_ai_fields.append('position_action')
        sectors = _list(parsed.get('hot_sectors'))
        if sectors:
            diagnosis['hot_sectors'] = sectors
            used_ai_fields.append('hot_sectors')
        candidates = parsed.get('candidate_stocks')
        if isinstance(candidates, list):
            diagnosis['candidate_stocks'] = [item for item in candidates if isinstance(item, dict)][:10]
        confidence = _text(parsed.get('confidence'))
        if confidence:
            if '高' in confidence:
                confidence = '高'
            elif '低' in confidence:
                confidence = '低'
            else:
                confidence = '中'
            if coverage['available'] < 7 and confidence == '高':
                confidence = '中'
            diagnosis['confidence'] = confidence

    core = {'open_strategy', 'external_impact', 'hot_sectors', 'risk_warning'}
    used_core = core.intersection(used_ai_fields)
    if len(used_core) == len(core):
        mode, reason = 'ai', 'ok'
    elif used_ai_fields:
        mode, reason = 'hybrid', 'partial_json'
    else:
        mode = 'rules'
        raw_lower = str(raw or '').lower()
        if 'timed out' in raw_lower or 'timeout' in raw_lower:
            reason = 'timeout'
        elif '空响应' in raw_lower or 'emptyresponse' in raw_lower or not str(raw or '').strip():
            reason = 'empty_response'
        else:
            reason = 'invalid_json'

    # 不完整的模型输出不能把孤立候选股送入自动推荐池；混合/规则版也不冒充高置信。
    if mode != 'ai':
        diagnosis['candidate_stocks'] = []
        if diagnosis.get('confidence') == '高':
            diagnosis['confidence'] = '中'

    return diagnosis, {
        'mode': mode, 'reason': reason, 'ai_fields': sorted(used_ai_fields),
        'coverage': coverage,
    }


def format_plain_morning_notification(diagnosis: Dict[str, Any], *,
                                      market: Any = '', holdings: Any = '',
                                      as_of: Any = '') -> Tuple[str, str]:
    """晨报即时通知只保留方向、动作和两句依据。"""
    from notify.plain_language import build_market_message

    position = diagnosis.get('position_advice') or holdings
    reason = diagnosis.get('open_strategy') or diagnosis.get('lazy_summary') or ''
    return build_market_message(
        label='早盘判断',
        direction=diagnosis.get('market_direction'),
        action=diagnosis.get('position_action'),
        market=market,
        holdings=position,
        reason=reason,
        as_of=as_of,
    )

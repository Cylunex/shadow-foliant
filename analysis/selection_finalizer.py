"""综合选股 TOP15 的二次优选。

只消费 unified_selection 已取得的规则分、来源、行情和红蓝结论，不再拉数据或调用 LLM。
红蓝失败时仍可按规则分和多源共振稳定产出；明确否决的候选不会进入最终名单。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


_VERDICT_BONUS = {'买入': 18.0, '谨慎': -6.0, None: 0.0, '': 0.0}
_CONFIDENCE_BONUS = {'高': 4.0, '中': 2.0, '低': 0.0, None: 0.0, '': 0.0}


def _number(value: Any) -> Optional[float]:
    try:
        return float(value) if value not in (None, '', '?', 'N/A') else None
    except (TypeError, ValueError):
        return None


def _chase_penalty(change_pct: Any) -> float:
    """09:45 已上涨超过 3% 后逐步扣分，避免最终名单被短线冲高股占满。"""
    change = _number(change_pct)
    if change is None or change <= 3.0:
        return 0.0
    return round(min(10.0, (change - 3.0) * 2.0), 2)


def finalize_selection(rows: Iterable[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    """从综合候选中选最终 TOP N，返回带 final_score/final_reason 的新字典。

    评分刻意透明：规则分×10 + 来源数×4 + 红蓝结论/置信度 - 追高扣分。
    原始 rank 作为最后的稳定排序键，保证同分结果可复现。
    """
    ranked: List[Dict[str, Any]] = []
    for raw in rows or []:
        row = dict(raw or {})
        code = str(row.get('code') or '').strip()
        verdict = row.get('debate_verdict')
        if not code or verdict == '否决':
            continue
        rule_score = _number(row.get('score')) or 0.0
        sources = list(dict.fromkeys(str(s) for s in (row.get('sources') or []) if s))
        confidence = row.get('debate_confidence')
        chase_penalty = _chase_penalty(row.get('change_pct'))
        final_score = (rule_score * 10.0 + min(len(sources), 4) * 4.0
                       + _VERDICT_BONUS.get(verdict, 0.0)
                       + (_CONFIDENCE_BONUS.get(confidence, 0.0) if verdict == '买入' else 0.0)
                       - chase_penalty)

        reasons = []
        if verdict == '买入':
            reasons.append(f'红蓝可买{("·" + str(confidence) + "置信") if confidence else ""}')
        elif verdict == '谨慎':
            reasons.append('红蓝谨慎')
        else:
            reasons.append('规则优选·待AI')
        reasons.append(f'{len(sources)}源共振' if len(sources) > 1 else '单源命中')
        reasons.append(f'规则分{rule_score:g}')
        if chase_penalty:
            reasons.append(f'已涨{_number(row.get("change_pct")):.1f}%扣追高分')

        row.update({
            'sources': sources,
            'final_score': round(final_score, 2),
            'final_reason': '；'.join(reasons),
            'chase_penalty': chase_penalty,
        })
        ranked.append(row)

    verdict_order = {'买入': 0, '谨慎': 1, None: 2, '': 2}
    ranked.sort(key=lambda row: (
        -float(row['final_score']),
        verdict_order.get(row.get('debate_verdict'), 2),
        -len(row.get('sources') or []),
        int(row.get('rank') or 9999),
        str(row.get('code') or ''),
    ))
    return ranked[:max(0, int(limit))]


def format_final_selection(rows: Iterable[Dict[str, Any]]) -> str:
    rows = list(rows or [])
    lines = [
        '从综合 TOP15 再筛一层：规则强度 + 多源共振 + 红蓝复核 + 追高风险。',
        '是否提高总仓位仍以 10:05 的组合动作分级为准。',
        '',
    ]
    for index, row in enumerate(rows, 1):
        held = '💼' if row.get('held') else ''
        price = _number(row.get('price'))
        change = _number(row.get('change_pct'))
        market = ''
        if price is not None:
            market += f' ¥{price:g}'
        if change is not None:
            market += f' {change:+.2f}%'
        lines.append(f'{index}. {held}{row.get("code", "")} {row.get("name") or ""}{market}')
        lines.append(f'   优选分 {row.get("final_score", 0):.1f}｜{row.get("final_reason", "")}')
        debate_reason = str(row.get('debate_reason') or '').strip()
        if debate_reason:
            lines.append(f'   关键判断：{debate_reason[:80]}')
    return '\n'.join(lines).rstrip()

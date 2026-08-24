"""正式本地融合产物终结器，以及旧二次优选的兼容实现。

生产正式 TOP15/TOP5 只调用 ``finalize_local_selection``。旧 ``finalize_selection``
保留给历史测试与兼容入口，不得用于改变当前正式成员或排序。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple


_VERDICT_BONUS = {'买入': 18.0, '谨慎': -6.0, None: 0.0, '': 0.0}
_CONFIDENCE_BONUS = {'高': 4.0, '中': 2.0, '低': 0.0, None: 0.0, '': 0.0}
_RESONANCE_BONUS = {'confirmed': 12.0, 'waiting': 0.0, 'blocked': -16.0,
                    'unknown': 0.0, None: 0.0, '': 0.0}
_PORTFOLIO_BUCKETS = {
    '金融': ('券商', '证券', '银行', '保险', '金融'),
    '地产链': ('地产', '房地产', '建材', '家居', '物业'),
    '新能源': ('新能源', '光伏', '锂电', '电池', '储能'),
    'AI算力': ('ai算力', '算力', '数据中心', '服务器', '光模块'),
    '消费': ('白酒', '食品', '家电', '零售', '消费'),
    '医药': ('医药', '医疗', '创新药'),
    '半导体': ('半导体', '芯片'),
    '周期': ('钢铁', '煤炭', '有色', '化工'),
    '机器人': ('机器人', '减速器', '执行器', '伺服'),
}


def _number(value: Any) -> Optional[float]:
    try:
        if value is None or str(value).strip().lower() in ('', '?', 'n/a', 'none', 'nan', '<na>'):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _chase_penalty(change_pct: Any) -> float:
    """09:45 已上涨超过 3% 后逐步扣分，避免最终名单被短线冲高股占满。"""
    change = _number(change_pct)
    if change is None or change <= 3.0:
        return 0.0
    return round(min(10.0, (change - 3.0) * 2.0), 2)


def _sector_bucket(row: Dict[str, Any]) -> str:
    labels = []
    for key in ('industry', 'sector', 'primary_theme', 'theme'):
        value = row.get(key)
        label = str(value).strip() if value is not None else ''
        if label.lower() not in ('', 'none', 'nan', 'n/a', '<na>'):
            labels.append(label)
    text = ' '.join(labels)
    lowered = text.lower()
    for bucket, needles in _PORTFOLIO_BUCKETS.items():
        if any(needle.lower() in lowered for needle in needles):
            return bucket
    return labels[0] if labels else ''


def _portfolio_exposure(portfolio: Iterable[Dict[str, Any]]) -> Tuple[set, Dict[str, float]]:
    holdings = [dict(item or {}) for item in (portfolio or []) if isinstance(item, dict)]
    held_codes = {str(item.get('code') or item.get('symbol') or '').strip() for item in holdings}
    raw_weights = []
    for item in holdings:
        value = _number(item.get('weight'))
        if value is None:
            value = _number(item.get('market_value'))
        if value is None:
            value = _number(item.get('mv'))
        raw_weights.append(max(0.0, value or 0.0))
    total = sum(raw_weights)
    if total <= 0 and holdings:
        raw_weights = [1.0] * len(holdings)
        total = float(len(holdings))
    sectors: Dict[str, float] = {}
    for item, value in zip(holdings, raw_weights):
        bucket = _sector_bucket(item)
        if bucket and total > 0:
            sectors[bucket] = sectors.get(bucket, 0.0) + value / total
    return held_codes, sectors


def _apply_portfolio_overlay(ranked: List[Dict[str, Any]],
                             portfolio: Iterable[Dict[str, Any]]) -> None:
    """在基础分排序后做一次确定性软惩罚，不因缺行业字段而猜测。"""
    held_codes, sector_exposure = _portfolio_exposure(portfolio)
    candidate_counts: Dict[str, int] = {}
    for row in sorted(ranked, key=lambda item: -float(item['final_score'])):
        penalty = 0.0
        reasons = []
        code = str(row.get('code') or '')
        if code in held_codes:
            penalty += 3.0
            reasons.append('已持有软扣3')

        bucket = _sector_bucket(row)
        if bucket:
            exposure = sector_exposure.get(bucket, 0.0)
            if exposure >= 0.25:
                exposure_penalty = round(min(6.0, (exposure - 0.15) * 16.0), 2)
                penalty += exposure_penalty
                reasons.append(f'{bucket}持仓暴露{exposure * 100:.0f}%扣{exposure_penalty:g}')
            previous = candidate_counts.get(bucket, 0)
            if previous >= 1:
                repeat_penalty = min(8.0, previous * 4.0)
                penalty += repeat_penalty
                reasons.append(f'{bucket}候选重复扣{repeat_penalty:g}')
            candidate_counts[bucket] = previous + 1

        row['portfolio_penalty'] = round(penalty, 2)
        row['portfolio_bucket'] = bucket or None
        row['portfolio_reasons'] = reasons
        if penalty:
            row['final_score'] = round(float(row['final_score']) - penalty, 2)
            row['final_reason'] += '；' + '；'.join(reasons)


def finalize_selection(rows: Iterable[Dict[str, Any]], limit: int = 5,
                       portfolio: Optional[Iterable[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """从综合候选中选最终 TOP N，返回带 final_score/final_reason 的新字典。

    评分刻意透明：规则分×10 + 来源数×4 + 红蓝结论/置信度 + 周日共振
    + 技术共振软评分（封顶±8）- 追高扣分。
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
        timeframe = row.get('multi_timeframe') if isinstance(row.get('multi_timeframe'), dict) else {}
        resonance = timeframe.get('resonance') or row.get('resonance')
        resonance_bonus = _RESONANCE_BONUS.get(resonance, 0.0)
        technical = (row.get('technical_state')
                     if isinstance(row.get('technical_state'), dict) else {})
        technical_bonus = max(-8.0, min(8.0, _number(technical.get('score')) or 0.0))
        final_score = (rule_score * 10.0 + min(len(sources), 4) * 4.0
                       + _VERDICT_BONUS.get(verdict, 0.0)
                       + (_CONFIDENCE_BONUS.get(confidence, 0.0) if verdict == '买入' else 0.0)
                       + resonance_bonus
                       + technical_bonus
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
        if resonance == 'confirmed':
            reasons.append('周日共振+12')
        elif resonance == 'blocked':
            reasons.append('周日同弱-16')
        elif timeframe.get('available'):
            reasons.append(str(timeframe.get('resonance_cn') or '等待日线确认'))
        if technical_bonus:
            label = '技术共振' if technical_bonus > 0 else '技术风险'
            reasons.append(f'{label}{technical_bonus:+g}')
        if chase_penalty:
            reasons.append(f'已涨{_number(row.get("change_pct")):.1f}%扣追高分')

        row.update({
            'sources': sources,
            'final_score': round(final_score, 2),
            'final_reason': '；'.join(reasons),
            'chase_penalty': chase_penalty,
            'resonance_bonus': resonance_bonus,
            'technical_bonus': technical_bonus,
        })
        ranked.append(row)

    _apply_portfolio_overlay(ranked, portfolio or [])

    verdict_order = {'买入': 0, '谨慎': 1, None: 2, '': 2}
    ranked.sort(key=lambda row: (
        -float(row['final_score']),
        verdict_order.get(row.get('debate_verdict'), 2),
        -len(row.get('sources') or []),
        int(row.get('rank') or 9999),
        str(row.get('code') or ''),
    ))
    return ranked[:max(0, int(limit))]


def finalize_local_selection(rows: Iterable[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    """Create the immutable deterministic TOP N from local PIT policy only.

    Quotes, LLM verdicts, online technical calls and current holdings are deliberately
    excluded from ranking. Fusion rows are already ordered by the deterministic lane
    policy; legacy PIT-only rows retain score sorting for replay compatibility.
    """
    ranked: List[Dict[str, Any]] = []
    for raw in rows or []:
        source = dict(raw or {})
        code = str(source.get("code") or source.get("symbol") or "").strip()
        score = _number(source.get(
            "lane_score_raw",
            source.get("local_score", source.get("score", source.get("total_score"))),
        ))
        if not code or score is None:
            continue
        required = (
            "run_id", "snapshot_id", "selection_date", "market_as_of",
            "valuation_as_of", "financial_as_of", "rule_version",
            "policy_hash", "manifest_id",
        )
        missing = [key for key in required if not source.get(key)]
        if missing:
            raise ValueError(f"formal selection row missing: {','.join(missing)}")
        is_fusion = bool(source.get("assigned_lane"))
        assigned_lane = source.get("assigned_lane") or "core"
        strategy_name = source.get("primary_strategy_name") or "本地PIT"
        row = {
            "run_id": source.get("run_id"),
            "snapshot_id": source.get("snapshot_id"),
            "selection_date": source.get("selection_date"),
            "market_as_of": source.get("market_as_of"),
            "valuation_as_of": source.get("valuation_as_of"),
            "financial_as_of": source.get("financial_as_of"),
            "manifest_id": source.get("manifest_id"),
            "code": code,
            "symbol": code,
            "rank": int(source.get("rank") or 9999),
            "selection_priority": int(
                source.get("selection_priority") or source.get("rank") or 9999
            ),
            "local_score": round(score, 4),
            "lane_score_raw": round(score, 4),
            "score_components": dict(source.get("score_components") or {}),
            "technical_state": source.get("technical_state") or source.get("local_state"),
            "data_quality": _number(
                source.get("data_quality", source.get("data_coverage"))
            ),
            "rule_version": source.get("rule_version"),
            "policy_hash": source.get("policy_hash"),
            "final_score": round(score, 4),
            "final_reason": (
                f"{strategy_name}赛道确定性入选" if is_fusion
                else "本地PIT总分确定性排序"
            ),
            "ranking_source": "local_fusion_policy" if is_fusion else "local_pit_snapshot",
            "assigned_lane": assigned_lane,
            "primary_strategy": source.get("primary_strategy") or "local_pit_v4",
            "primary_strategy_name": strategy_name,
            "lane_rank": int(source.get("lane_rank") or source.get("rank") or 9999),
            "strategy_priority_weight": _number(source.get("strategy_priority_weight")),
            "supporting_nominations": list(source.get("supporting_nominations") or []),
            "source_labels": list(source.get("source_labels") or []),
        }
        ranked.append(row)
    if any(row.get("ranking_source") == "local_fusion_policy" for row in ranked):
        ranked.sort(key=lambda row: (
            int(row.get("selection_priority") or 9999), str(row.get("code") or "")
        ))
    else:
        ranked.sort(key=lambda row: (
            -float(row["final_score"]), int(row.get("rank") or 9999),
            str(row.get("code") or "")
        ))
    return ranked[:max(0, int(limit))]


def format_final_selection(rows: Iterable[Dict[str, Any]]) -> str:
    rows = list(rows or [])
    lines = [
        '正式 TOP5 由本地多赛道政策确定；实时行情、持仓和红蓝结论仅作为下方显示复核，不改变成员或顺序。',
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
        plan = row.get('trade_plan') if isinstance(row.get('trade_plan'), dict) else {}
        if plan.get('available'):
            rr = plan.get('risk_reward_ratio')
            rr_text = f'｜盈亏比 {rr:g}' if rr is not None else '｜盈亏比待确认'
            lines.append(
                f"   计划：{plan.get('action_cn', '观望')}｜入场 "
                f"{plan.get('entry_low', '-')}~{plan.get('entry_high', '-')}｜"
                f"止损 {plan.get('stop_loss', '-')}｜目标 {plan.get('target_price') or '-'}{rr_text}"
            )
            if plan.get('target_price_2') is not None:
                lines.append(f"   形态第二目标：{plan['target_price_2']}（不参与首目标盈亏比）")
            technical = plan.get('technical_state') or {}
            positives = technical.get('positives') or []
            if positives:
                lines.append(f"   技术确认：{'；'.join(str(x) for x in positives[:3])}")
            blockers = plan.get('blockers') or []
            if blockers:
                lines.append(f"   阻断：{'；'.join(str(x) for x in blockers[:2])}")
    return '\n'.join(lines).rstrip()

"""阶段决策护栏 —— LLM 决策后的程序化纠偏(借鉴 daily_stock_analysis 的 phase_decision_guardrail)。

目的:让决策"随行情/交易阶段调整",而不是只靠 prompt 文字祈祷。
当前在盘前/非交易时段给出"立即买入/卖出"时,降为"计划+开盘确认"并下调信心度;
未来可扩展:核心数据缺失→封顶信心、盘中盘后口吻替换等。

只读改 decision dict 的展示性字段(operation_advice/confidence_level/phase),
不翻转 rating(避免加重"保守偏置")。返回 (decision, adjustments)。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


def _market_phase(now: datetime) -> str:
    """premarket / intraday / lunch / postmarket / non_trading(节假日感知)。"""
    try:
        from jobs_hub import _is_trading_day  # 节假日感知(akshare 交易日历)
        if not _is_trading_day(now):
            return 'non_trading'
    except Exception:
        if now.weekday() >= 5:
            return 'non_trading'
    hm = now.hour * 60 + now.minute
    if hm < 9 * 60 + 30:
        return 'premarket'
    if hm < 11 * 60 + 30:
        return 'intraday'
    if hm < 13 * 60:
        return 'lunch'
    if hm <= 15 * 60:
        return 'intraday'
    return 'postmarket'


def _downgrade_conf(c: Any, step: int = 2) -> Optional[Any]:
    """信心度(1-10,可能是 '8' / '8分' / 8)下调 step,下限 1。无法解析返回原值。"""
    if c is None:
        return None
    import re
    m = re.search(r'\d+', str(c))
    if not m:
        return c
    v = max(1, int(m.group()) - step)
    return str(c).replace(m.group(), str(v), 1)


def apply_phase_guardrail(decision: Dict[str, Any], now: Optional[datetime] = None) -> Tuple[Dict[str, Any], List[str]]:
    """对 make_final_decision 的输出做阶段纠偏。返回 (decision, adjustments)。"""
    adjustments: List[str] = []
    if not isinstance(decision, dict):
        return decision, adjustments
    now = now or datetime.now()
    phase = _market_phase(now)
    decision['phase'] = phase

    rating = str(decision.get('rating', '') or '').lower()
    is_action = any(k in rating for k in ('买', '卖', 'buy', 'sell'))
    if phase in ('premarket', 'non_trading') and is_action:
        caveat = ('【盘前:此为计划,开盘确认后再执行,勿追高】' if phase == 'premarket'
                  else '【非交易日:仅供参考,以下一交易日开盘为准】')
        decision['phase_caveat'] = caveat
        decision['operation_advice'] = caveat + str(decision.get('operation_advice', '') or '')
        nc = _downgrade_conf(decision.get('confidence_level'))
        if nc is not None and nc != decision.get('confidence_level'):
            decision['confidence_level'] = nc
            adjustments.append(f'confidence_downgraded_{phase}')
        adjustments.append(f'phase_caveat_{phase}')
    if adjustments:
        decision['guardrail_adjustments'] = adjustments
    return decision, adjustments


if __name__ == '__main__':
    from datetime import datetime as _dt
    for t in [_dt(2026, 6, 1, 8, 0), _dt(2026, 6, 1, 10, 0), _dt(2026, 6, 6, 10, 0)]:
        d, adj = apply_phase_guardrail({'rating': '买入', 'confidence_level': '8分',
                                        'operation_advice': '逢低买入'}, now=t)
        print(t, '->', d.get('phase'), '| adj=', adj, '| conf=', d.get('confidence_level'))

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""清仓决策助手 —— 回答"持仓太多、不知道什么时候清"。

把"该不该清、先清哪只"做成**可执行的清仓优先级清单**:对每只持仓按多触发器打"清仓紧迫分",
排序给出明确动作(清仓/减仓锁利/破位减仓/长期效率观察/继续持有)+ 一句话理由;持仓过多(过度分散)
时给"目标持仓数"建议并指出该先清掉哪几只瘦身到目标。规则层定量打分(可靠) + AI 层给整体瘦身策略。

触发器(每只综合):
  ① 割肉止损:浮亏深(≤-12%)或浮亏+技术破位                → 清仓
  ② 止盈锁定:浮盈大(≥30%)但动能转弱(今日跌/破MA)        → 减仓锁利
  ③ 破位减仓:风险分高(破MA20/MA60/VaR/看跌形态)           → 减仓
  ④ 效率观察:有可信建仓日且持有久(≥90天)、横盘(|浮盈亏|<8%) → 仅观察
  ⑤ 健康保留:浮盈且趋势未坏                                → 持有

清仓/减仓结论写 decision_signal(source_type='exit_advice', action=sell/reduce)→ 16:10 方向后验
(卖出期望跌→命中),让"清仓建议"也进可量化胜率环。

接口:run_exit_advice(target_positions=15, record_signals=True) -> dict
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from core.action_decision import resolve_action


def _holding_days(created_at) -> Optional[int]:
    if not created_at:
        return None
    try:
        if isinstance(created_at, datetime):
            opened = created_at.date()
        else:
            opened = datetime.fromisoformat(
                str(created_at).strip().replace("Z", "+00:00")
            ).date()
        return max(0, (datetime.now().date() - opened).days)
    except (TypeError, ValueError):
        return None


def _holding_age_evidence(holding: Dict[str, Any]) -> Dict[str, Any]:
    """Use only an explicit acquisition/open date as holding-age evidence.

    ``portfolio_stocks.created_at`` is often a bulk-import/tracking timestamp and
    must never be presented as the date the investor acquired the position.
    """
    explicit = next((
        holding.get(key) for key in (
            "holding_since", "position_opened_at", "acquired_at",
        ) if holding.get(key)
    ), None)
    return {
        "holding_days": _holding_days(explicit),
        "tracking_days": _holding_days(holding.get("created_at")),
        "basis": "explicit_position_open_date" if explicit else "unconfirmed_tracking_date",
    }


def _exit_score(scan: Dict[str, Any], hold_days: Optional[int]):
    """返回 (exit_score 0-100, category, action, reasons[])。分越高越该清。"""
    pnl = scan.get('pnl')
    from application.stock_budget import _finite
    pnl = _finite(pnl)
    change = _finite(scan.get('change'))
    sell_score = int(scan.get('sell_score') or 0)
    reasons: List[str] = []
    score = 0.0

    # ① 割肉止损
    if pnl is not None and (pnl <= -15):
        score += 55; reasons.append(f'浮亏{pnl:.0f}%已破位')
    elif pnl is not None and (pnl <= -10 and sell_score >= 1):
        score += 45; reasons.append(f'浮亏{pnl:.0f}%+技术破位')
    elif pnl is not None and (pnl <= -8 and sell_score >= 2):
        score += 35; reasons.append(f'浮亏{pnl:.0f}%+多重风险')

    # ② 止盈锁定(大浮盈 + 动能转弱)
    if pnl is not None and (pnl >= 50 and ((change is not None and change < 0) or sell_score >= 1)):
        score += 35; reasons.append(f'浮盈{pnl:.0f}%但动能转弱,锁利')
    elif pnl is not None and (pnl >= 30 and ((change is not None and change < -1) or sell_score >= 1)):
        score += 25; reasons.append(f'浮盈{pnl:.0f}%遇阻,落袋部分')

    # ③ 破位减仓(风险信号)
    if sell_score >= 2:
        score += sell_score * 12; reasons.append('风险信号:' + '/'.join((scan.get('sell_reasons') or [])[:3]))
    elif sell_score == 1:
        score += 10; reasons.append('单一风险:' + '/'.join((scan.get('sell_reasons') or [])[:2]))

    # ④ 长期效率观察（只有明确建仓日期才能进入；本身不构成卖出依据）
    if pnl is not None and (hold_days is not None and hold_days >= 90 and -8 < pnl < 8):
        score += 22; reasons.append(f'明确持有{hold_days}天且横盘({pnl:+.0f}%),仅列入效率观察')

    score = min(100.0, score)
    # 归类(取最主导的触发)
    if pnl is not None and (pnl <= -12 or (pnl <= -8 and sell_score >= 2)):
        cat, act = '割肉止损', 'sell'
    elif pnl is not None and (pnl >= 30 and score >= 25):
        cat, act = '止盈锁定', 'reduce'
    elif sell_score >= 2:
        cat, act = '破位减仓', 'reduce'
    elif pnl is not None and hold_days is not None and hold_days >= 90 and -8 < pnl < 8 and score >= 20:
        # Time-in-position plus flat P&L is an efficiency observation, not a
        # liquidation fact. It may inform a later reviewed rebalance only.
        cat, act = '长期效率观察', 'hold'
    else:
        cat, act = '健康保留', 'hold'
    if pnl is None and act == 'hold':
        cat = '证据待核'
        reasons.append('浮盈亏待核，不以零收益判定健康或长期横盘')
    if not reasons:
        reasons.append(f'浮盈亏{pnl:+.0f}%,趋势未坏' if pnl >= 0 else f'浮亏{pnl:.0f}%,暂未破位')
    return score, cat, act, reasons


_ACT_TAG = {'sell': '🟢清仓', 'reduce': '🟢减仓', 'hold': '⚪持有'}


def run_exit_advice(target_positions: int = 15, record_signals: bool = True) -> Dict[str, Any]:
    """对全部持仓打清仓紧迫分并排序 + 过度分散瘦身建议 + AI 整体策略。返回 {ok, items, text, ...}。"""
    out = {'ok': False, 'items': [], 'text': '', 'summary': ''}
    from application.portfolio_daily import LIMIT, load_account_counts, position_count_line
    target_positions = min(LIMIT, max(1, int(target_positions)))
    try:
        from portfolio_db import portfolio_db
        # quantity NULL=未填数量(保留),0=清仓(DB 层已滤);同 eod_review(2026-07-17 修)
        holdings = [h for h in (portfolio_db.get_all_stocks() or [])
                    if h.get('quantity', h.get('shares')) is None
                    or float(h.get('quantity') or h.get('shares') or 0) > 0]
    except Exception as e:
        out['summary'] = f'持仓读取失败: {type(e).__name__}: {str(e)[:50]}'
        return out
    if not holdings:
        out['summary'] = '当前无持仓'
        return out
    try:
        from jobs_hub import _scan_holdings_with_snapshot
        scans = {str(s.get('code')): s for s in (_scan_holdings_with_snapshot() or [])}
    except Exception:
        scans = {}
    items = []
    for h in holdings:
        code = str(h.get('code'))
        scan = scans.get(code) or {'code': code, 'name': h.get('name', ''), 'pnl': None, 'sell_score': 0}
        age = _holding_age_evidence(h)
        hd = age["holding_days"]
        sc, cat, act, reasons = _exit_score(scan, hd)
        decision = resolve_action([{
            'source': 'hard_risk' if act == 'sell' else (
                'portfolio_risk' if act == 'reduce' else 'position_truth'
            ),
            'action': act,
            'reason': '；'.join(reasons)[:240],
        }])
        items.append({
            'code': code, 'name': scan.get('name') or h.get('name', ''),
            'exit_score': round(sc, 1), 'category': cat,
            'action': decision['action'], 'action_text': decision['action_text'],
            'action_decision': decision,
            'pnl': scan.get('pnl'), 'holding_days': hd,
            'tracking_days': age["tracking_days"], 'holding_age_basis': age["basis"],
            'reason': '；'.join(reasons)[:80],
            'price': scan.get('price'),
        })
    items.sort(key=lambda x: -x['exit_score'])

    n = len(items)
    counts = load_account_counts(holdings)
    over = counts['equity_count'] > target_positions
    # 紧迫清仓:分≥40;瘦身候选:过度分散时,分最高的几只(清到目标数)
    urgent = [it for it in items if it['exit_score'] >= 40]
    trim_to_target = [it for it in items if it['code'] in counts['equity_symbols'] and it['action'] == 'sell'][:counts['excess']] if over else []

    ai_summary = _ai_narrative(items, n, target_positions, over)

    # 写决策信号(清仓/减仓)进后验
    if record_signals:
        for it in items:
            if it['action'] in ('sell', 'reduce'):
                try:
                    from decision_signal import create_signal
                    p = it.get('price')
                    create_signal(code=it['code'], name=it['name'], action='reduce' if it['action'] == 'reduce' else 'sell',
                                  source_type='exit_advice', source_ref='exit_advisor',
                                  confidence='高' if it['exit_score'] >= 55 else '中',
                                  horizon='swing', ref_price=float(p) if p not in (None, '', '?') else None,
                                  reason=f"清仓建议[{it['category']}]:{it['reason']}")
                except Exception:
                    pass

    out['ok'] = True
    out['items'] = items
    out['n_holdings'] = n
    out['target'] = target_positions
    out['over_diversified'] = over
    out['urgent'] = urgent
    out['summary'] = position_count_line(counts, target_positions) + f'，紧迫清仓{len(urgent)}只' + ('·过度分散' if over else '')
    out['position_counts'] = counts
    out['text'] = position_count_line(counts, target_positions) + '\n' + _format(items, n, target_positions, over, trim_to_target, ai_summary)
    return out


def _ai_narrative(items, n, target, over) -> str:
    top = items[:min(12, n)]
    lines = [f"{it['code']} {it['name']} 紧迫分{it['exit_score']}|{it['category']}|"
             f"浮盈亏{str(it['pnl'])+'%' if it['pnl'] is not None else '待核'}|持有{it.get('holding_days') if it.get('holding_days') is not None else '未知'}天|{it['reason']}"
             for it in top]
    prompt = f"""你是持仓瘦身顾问。该账户证券合计 {n} 项。用户个股上限{target}，ETF另计；本次已分类个股{'超限' if over else '未确认超限'}，不得以证券合计数量替代个股数。
下面是按"清仓紧迫分"排序的持仓(分越高越该清):

{chr(10).join(lines)}

请给**整体瘦身策略**(≤180字),务实点名到具体持仓:
1. {'当前持仓过多,建议把组合收敛到约 ' + str(target) + ' 只——优先清掉哪几只(点名)?为什么是它们?' if over else '当前持仓数尚可,重点是处理紧迫分高的几只。'}
2. 必须立即处理的(割肉/锁利/破位):点名 + 动作。
3. 一句话纪律提醒(避免"舍不得割/死扛/全仓乱买")。
只给可执行结论,不空谈。"""
    try:
        from deepseek_client import DeepSeekClient
        return (DeepSeekClient().call_api(
            [{'role': 'system', 'content': '用户偏左侧交易。只解释既定风险动作和持仓结构，未知成本或持有日期保持待核，不因下跌或个股数量直接创造清仓动作。'},
             {'role': 'user', 'content': prompt}], max_tokens=900, call_type='exit_advice') or '').strip()
    except Exception as e:
        return f'(AI 策略生成失败: {type(e).__name__})'


def _format(items, n, target, over, trim, ai_summary) -> str:
    lines = [f"🧹 清仓决策助手 — 证券 {n} 项" + (f"(个股目标≤{target}，ETF另计)" if over else "")]
    if ai_summary:
        lines.append('\n' + ai_summary + '\n')
    show = [it for it in items if it['action'] in ('sell', 'reduce')][:10]
    if show:
        lines.append('━━ 建议处理(按紧迫度)━━')
        for it in show:
            pnl = f"{it['pnl']:+.0f}%" if it.get('pnl') is not None else '—'
            lines.append(f"  {_ACT_TAG.get(it['action'])} {it['name']} {it['code']} "
                         f"[{it['category']}·紧迫{it['exit_score']:.0f}] 浮盈亏{pnl}\n      {it['reason']}")
    else:
        lines.append('尚无证据充分的清仓/减仓动作；缺失数据继续待核。')
    return '\n'.join(lines)


if __name__ == '__main__':
    import io, json
    _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== 清仓助手 自检(打分逻辑) ===')
    for scan, hd in [({'pnl': -16, 'sell_score': 2, 'sell_reasons': ['破MA60', 'VaR高'], 'change': -2}, 40),
                     ({'pnl': 42, 'sell_score': 1, 'sell_reasons': ['破MA20'], 'change': -1.5}, 30),
                     ({'pnl': 2, 'sell_score': 0, 'sell_reasons': [], 'change': 0.3}, 130),
                     ({'pnl': 12, 'sell_score': 0, 'sell_reasons': [], 'change': 1}, 20)]:
        s, cat, act, r = _exit_score(scan, hd)
        print(f'  pnl{scan["pnl"]:+}% ss{scan["sell_score"]} hd{hd} → 分{s:.0f} {cat}/{act}: {"；".join(r)}')

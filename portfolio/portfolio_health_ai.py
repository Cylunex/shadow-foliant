import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""持仓 AI 体检官 —— 把散落的规则信号融合成单股可执行动作建议 + 自动进后验胜率环。

当前缺口:早/尾盘持仓任务只把 _scan_holdings_with_snapshot 的规则分(破MA/VaR/浮亏/缠论/异动)
拼成文本直推,无推理融合——"破MA60 + 浮亏-12% + 缠论转弱 到底该割还是该扛"没人回答。

本模块对持仓(优先风险分≥1 或浮亏的子集,控 token)做一段 AI 体检:融合每只的结构化信号,
给出 持有/减仓/清仓 的明确动作 + 一句话理由 + 信心度。**复用现成数据,零额外取数**。

闭环:每条 AI 动作写一条 decision_signal(source_type='portfolio_health',action=hold/reduce/sell),
16:10 decision_signal_outcomes 用 K线方向后验自动累积"持仓体检"动作的真实命中率
(outcome_stats('source_type') 可查),让持仓 AI 从"只管发建议"变"对建议负责"。

接口:run_health_check(scans, max_stocks=15, record_signals=True) -> dict
"""

import re
from typing import Any, Dict, List, Optional

# 中文动作 → decision_signal 8 态
_ACT_MAP = {'清仓': 'sell', '卖出': 'sell', '减仓': 'reduce', '减持': 'reduce',
            '持有': 'hold', '观望': 'watch', '加仓': 'add', '增持': 'add'}


def _pick_targets(scans: List[Dict[str, Any]], max_stocks: int) -> List[Dict[str, Any]]:
    """挑要深析的子集:风险分≥1 或浮亏 优先,控 token。无风险则取浮亏最深的几只。"""
    risky = [s for s in scans if (s.get('sell_score') or 0) >= 1 or (s.get('pnl') or 0) < 0]
    if not risky:
        risky = sorted(scans, key=lambda x: (x.get('pnl') or 0))[:max(3, max_stocks // 3)]
    risky.sort(key=lambda x: ((x.get('sell_score') or 0), -(x.get('pnl') or 0)), reverse=True)
    return risky[:max_stocks]


def _build_prompt(targets: List[Dict[str, Any]]) -> str:
    lines = []
    for s in targets:
        reasons = '/'.join(s.get('sell_reasons') or []) or '无'
        lines.append(
            f"{s.get('code')} {s.get('name','')}: 现价{s.get('price','?')} 今日{(s.get('change') or 0):+.2f}% "
            f"浮盈亏{(s.get('pnl') or 0):+.2f}% 风险分{s.get('sell_score',0)}(信号:{reasons}) 市值{s.get('mv',0):.0f}")
    body = '\n'.join(lines)
    return f"""你是持仓风控官。下面是某账户**当前持仓**的多维信号(技术破位/量化风险/浮盈亏/异动)。
请逐只融合判断,给出明确动作。**只看风险与持有性价比,不做无依据乐观**。

持仓信号:
{body}

对**每一只**输出一行,严格格式(动作只能是 清仓/减仓/持有/加仓 之一):
代码 | 动作:X | 信心:高/中/低 | 理由:一句话(≤30字,点明关键依据)

要求:破位+浮亏扩大+风险信号多 → 倾向减仓/清仓;浮盈且趋势未坏 → 持有;不要每只都"持有"敷衍。"""


def _parse(answer: str) -> Dict[str, Dict[str, str]]:
    """解析 LLM 的逐行结论 → {code: {action_cn, confidence, reason}}。"""
    out: Dict[str, Dict[str, str]] = {}
    for line in (answer or '').splitlines():
        m = re.match(r'\s*(\d{6})\D.*?动作[:：]\s*([清减持加][仓有])', line)
        if not m:
            continue
        code = m.group(1)
        act_cn = m.group(2)
        conf = (re.search(r'信心[:：]\s*([高中低])', line) or [None, '中'])[1]
        rsn = (re.search(r'理由[:：]\s*(.+)$', line) or [None, ''])[1].strip()[:40]
        out[code] = {'action_cn': act_cn, 'confidence': conf, 'reason': rsn}
    return out


def run_health_check(scans: Optional[List[Dict[str, Any]]] = None, max_stocks: int = 15,
                     record_signals: bool = True) -> Dict[str, Any]:
    """对持仓做 AI 体检。scans 为 _scan_holdings_with_snapshot 输出;返回 {ok, summary, items, text}。
    record_signals=True 时把每条动作写 decision_signal(source_type='portfolio_health')进后验环。"""
    out = {'ok': False, 'items': [], 'text': '', 'summary': ''}
    if not scans:
        out['summary'] = '无持仓数据'
        return out
    targets = _pick_targets(scans, max_stocks)
    if not targets:
        out['summary'] = '无需体检的持仓'
        return out
    try:
        from deepseek_client import DeepSeekClient
        client = DeepSeekClient()
        messages = [
            {'role': 'system', 'content': '你是严谨的持仓风控官,只给基于信号的明确动作,不空谈。'},
            {'role': 'user', 'content': _build_prompt(targets)},
        ]
        answer = client.call_api(messages, max_tokens=1500, call_type='portfolio_health')
    except Exception as e:
        out['summary'] = f'AI 体检调用失败: {type(e).__name__}: {str(e)[:60]}'
        return out

    parsed = _parse(answer)
    items, n_reduce, n_sell = [], 0, 0
    for s in targets:
        code = str(s.get('code'))
        v = parsed.get(code)
        if not v:
            continue
        act_cn = v['action_cn']
        action = _ACT_MAP.get(act_cn, 'hold')
        items.append({'code': code, 'name': s.get('name', ''), 'action': action,
                      'action_cn': act_cn, 'confidence': v['confidence'],
                      'reason': v['reason'], 'pnl': s.get('pnl'), 'price': s.get('price')})
        if action == 'reduce':
            n_reduce += 1
        elif action == 'sell':
            n_sell += 1
        # 写决策信号 → 16:10 自动方向后验(reduce/sell 期望跌、hold 中性)
        if record_signals and action in ('reduce', 'sell', 'hold'):
            try:
                from decision_signal import create_signal
                create_signal(code=code, name=s.get('name', ''), action=action,
                              source_type='portfolio_health', source_ref='ai_health',
                              confidence=v['confidence'], horizon='swing',
                              ref_price=_safe_float(s.get('price')),
                              reason=f"持仓体检:{v['reason']}")
            except Exception:
                pass

    out['ok'] = True
    out['items'] = items
    out['summary'] = f'体检 {len(items)} 只:清仓建议 {n_sell}、减仓 {n_reduce}'
    out['text'] = _format(items)
    return out


def _safe_float(v):
    try:
        return float(v) if v not in (None, '', '?') else None
    except (TypeError, ValueError):
        return None


_ACT_EMOJI = {'sell': '🔴清仓', 'reduce': '🟠减仓', 'hold': '⚪持有', 'add': '🔴加仓', 'watch': '⚪观望'}


def _format(items: List[Dict[str, Any]]) -> str:
    if not items:
        return ''
    # 动作重的排前面
    order = {'sell': 0, 'reduce': 1, 'add': 2, 'hold': 3, 'watch': 4}
    rows = sorted(items, key=lambda x: order.get(x['action'], 5))
    lines = ['🧠 持仓 AI 体检']
    for it in rows:
        tag = _ACT_EMOJI.get(it['action'], it['action_cn'])
        pnl = f" 浮盈亏{it['pnl']:+.1f}%" if it.get('pnl') is not None else ''
        lines.append(f"  {tag} {it['name']} {it['code']}（信心{it['confidence']}）{pnl}\n      {it['reason']}")
    return '\n'.join(lines)


if __name__ == '__main__':
    import io
    _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== 持仓体检官 自检(解析逻辑) ===')
    fake_ans = ("600519 | 动作:持有 | 信心:中 | 理由:浮盈趋势未坏,继续持有\n"
                "000858 | 动作:减仓 | 信心:高 | 理由:破MA20且浮亏扩大,先减半仓\n"
                "002415 | 动作:清仓 | 信心:中 | 理由:风险信号多,趋势转弱止损")
    import json
    print(json.dumps(_parse(fake_ans), ensure_ascii=False, indent=1))

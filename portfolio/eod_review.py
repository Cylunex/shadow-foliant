import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""尾盘持仓总结 —— 把"尾盘持仓分析 + 持仓 AI 体检 + 清仓助手"三条重叠推送**优化整合**成一条。

这三者都在回答同一问题"持仓尾盘该怎么处理",此前各推一条 → 同一只票在三条里出现三次、三套口径
(规则风险分 / AI体检动作 / 清仓紧迫分),又碎又互相打架。本模块**融合**:
  - 一次取数(_scan_holdings_with_snapshot)、一次规则打分(复用 exit_advisor 的紧迫分/归类/死钱)、
  - **一次 LLM 调用**同时产出"整体瘦身策略 + 每只最终动作",每只票**只出现一次、一个融合结论**,
  - 一条报告:总策略 → 按紧迫度的处理清单(去重) → 尾盘强势机会;一组决策信号(source_type='eod_review')。
比原来 3 任务 / 2 次 LLM / 3 条推送 更省、更不矛盾、更好看。

接口:run_eod_review(target_positions=20, record_signals=True) -> dict
"""

import re
from typing import Any, Dict, List, Optional

_ACT_MAP = {'清仓': 'sell', '卖出': 'sell', '减仓': 'reduce', '减持': 'reduce',
            '持有': 'hold', '观望': 'watch', '加仓': 'add', '增持': 'add'}
_ACT_TAG = {'sell': '🔴清仓', 'reduce': '🟠减仓', 'add': '🔴加仓', 'hold': '⚪持有', 'watch': '⚪观望'}
_ACT_ORDER = {'sell': 0, 'reduce': 1, 'add': 2, 'hold': 3, 'watch': 4}


def run_eod_review(target_positions: int = 20, record_signals: bool = True) -> Dict[str, Any]:
    out = {'ok': False, 'items': [], 'text': '', 'summary': ''}
    try:
        from portfolio_db import portfolio_db
        holdings = [h for h in (portfolio_db.get_all_stocks() or [])
                    if float(h.get('quantity') or h.get('shares') or 0) > 0]
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
    from exit_advisor import _exit_score, _holding_days
    created = {str(h.get('code')): h.get('created_at') for h in holdings}

    # 1) 规则层:每只融合打分(割肉/止盈/破位/死钱),复用 exit_advisor
    rows = []
    for h in holdings:
        code = str(h.get('code'))
        scan = scans.get(code) or {'code': code, 'name': h.get('name', ''), 'pnl': None, 'sell_score': 0}
        hd = _holding_days(created.get(code))
        sc, cat, act, reasons = _exit_score(scan, hd)
        rows.append({'code': code, 'name': scan.get('name') or h.get('name', ''),
                     'exit_score': round(sc, 1), 'category': cat, 'rule_action': act,
                     'pnl': scan.get('pnl'), 'holding_days': hd, 'price': scan.get('price'),
                     'rule_reason': '；'.join(reasons)[:60],
                     'sell_score': scan.get('sell_score', 0),
                     'sell_reasons': scan.get('sell_reasons') or []})
    rows.sort(key=lambda x: -x['exit_score'])
    n = len(rows)
    over = n > target_positions

    # 2) 一次 LLM:整体瘦身策略 + 逐只最终动作(只让 AI 决策紧迫分高/有风险的子集,控 token)
    decide = [r for r in rows if r['exit_score'] >= 15 or (r['sell_score'] or 0) >= 1][:18]
    ai_strategy, ai_verdict = _ai_fuse(decide, n, target_positions, over)

    # 3) 融合:AI 给了动作用 AI 的,否则用规则;紧迫分继续做排序与紧迫度
    items = []
    for r in rows:
        v = ai_verdict.get(r['code'])
        if v:
            action, reason = v['action'], v['reason'] or r['rule_reason']
        else:
            action, reason = r['rule_action'], r['rule_reason']
        items.append({**r, 'action': action, 'reason': reason})

    n_sell = sum(1 for it in items if it['action'] == 'sell')
    n_reduce = sum(1 for it in items if it['action'] == 'reduce')

    if record_signals:
        for it in items:
            if it['action'] in ('sell', 'reduce'):
                try:
                    from decision_signal import create_signal
                    p = it.get('price')
                    create_signal(code=it['code'], name=it['name'],
                                  action='reduce' if it['action'] == 'reduce' else 'sell',
                                  source_type='eod_review', source_ref='eod',
                                  confidence='高' if it['exit_score'] >= 55 else '中',
                                  horizon='swing', ref_price=float(p) if p not in (None, '', '?') else None,
                                  reason=f"尾盘[{it['category']}]:{it['reason']}")
                except Exception:
                    pass

    out['ok'] = True
    out['items'] = items
    out['summary'] = f'持仓{n}只(目标{target_positions}),清仓{n_sell}/减仓{n_reduce}' + ('·过度分散' if over else '')
    out['text'] = _format(items, scans, n, target_positions, over, ai_strategy)
    return out


def _ai_fuse(decide: List[Dict[str, Any]], n: int, target: int, over: bool):
    """一次 LLM:整体瘦身策略 + 逐只最终动作。返回 (strategy_text, {code:{action,reason}})。"""
    if not decide:
        return ('', {})
    lines = [f"{r['code']} {r['name']} 紧迫{r['exit_score']}|{r['category']}|"
             f"浮盈亏{(r['pnl'] if r['pnl'] is not None else 0):+.0f}%|持有{r.get('holding_days','?')}天|"
             f"风险分{r['sell_score']}({'/'.join(r['sell_reasons'][:3]) or '无'})" for r in decide]
    prompt = f"""你是持仓瘦身顾问。该账户持有 {n} 只{'(明显过多,健康零售组合通常 5-10 只)' if over else ''}。
下面是按"清仓紧迫分"排序、需要决断的持仓(已融合 割肉止损/止盈锁定/破位/死钱 多维信号):

{chr(10).join(lines)}

请输出两部分:
①【总策略】≤150字:{'持仓过多,建议收敛到约 ' + str(target) + ' 只——先清掉哪几只(点名)、为什么。' if over else '重点处理紧迫分高的几只。'}一句纪律提醒(别舍不得割/死扛)。
②【逐只】对**上面每一只**给最终动作(清仓/减仓/持有/加仓 之一)+ 一句话理由,严格格式一行一只:
代码 | 动作:X | 理由:≤25字

要求:同一只只给一个结论,综合所有信号(深亏破位→清仓;大浮盈动能弱→减仓锁利;久横盘死钱→清仓换仓;趋势未坏→持有)。"""
    try:
        from deepseek_client import DeepSeekClient
        ans = DeepSeekClient().call_api(
            [{'role': 'system', 'content': '你是冷静的持仓瘦身顾问,敢让用户割肉与收敛持仓,逐只给唯一可执行结论。'},
             {'role': 'user', 'content': prompt}], max_tokens=1400, call_type='eod_review') or ''
    except Exception as e:
        print(f'[eod_review] LLM 失败: {type(e).__name__}: {str(e)[:50]}')
        return ('', {})
    # 拆策略(【总策略】到【逐只】之间)与逐只
    strategy = ''
    m = re.search(r'【总策略】\s*(.+?)(?:【逐只】|代码\s*\||$)', ans, re.S)
    if m:
        # 去掉 AI 可能留在末尾的 ①②/逐只 等分节标记
        strategy = re.sub(r'\s*[①②③]?\s*【?逐只?】?\s*$', '', m.group(1).strip()).rstrip('②①③ \n　')[:300]
    verdict = {}
    for line in ans.splitlines():
        mm = re.search(r'(\d{6})\D.*?动作[:：]\s*([清减持加][仓有])', line)
        if mm:
            reason = (re.search(r'理由[:：]\s*(.+)$', line) or [None, ''])[1].strip()[:30]
            verdict[mm.group(1)] = {'action': _ACT_MAP.get(mm.group(2), 'hold'), 'reason': reason}
    return (strategy, verdict)


def _format(items, scans, n, target, over, strategy) -> str:
    lines = [f"🧹 尾盘持仓总结 — 持仓 {n} 只" + (f"(目标~{target},过度分散建议瘦身)" if over else "")]
    if strategy:
        lines.append('\n🧠 瘦身策略\n' + strategy + '\n')
    act = sorted([it for it in items if it['action'] in ('sell', 'reduce', 'add')],
                 key=lambda x: (_ACT_ORDER.get(x['action'], 9), -x['exit_score']))[:10]
    if act:
        lines.append('━━ 建议处理(按紧迫度)━━')
        for it in act:
            pnl = f"{it['pnl']:+.0f}%" if it.get('pnl') is not None else '—'
            lines.append(f"  {_ACT_TAG.get(it['action'])} {it['name']} {it['code']} "
                         f"[{it['category']}·紧迫{it['exit_score']:.0f}] 浮盈亏{pnl}\n      {it['reason']}")
        hold_n = n - len(act)
        if hold_n > 0:
            lines.append(f"  …其余 {hold_n} 只暂持有")
    else:
        lines.append('当前无紧迫清仓/减仓信号,持仓相对健康。')
    # 尾盘强势机会(复用 scan 的当日涨幅)
    buys = sorted([s for s in scans.values() if (s.get('change') or 0) > 3],
                  key=lambda x: -(x.get('change') or 0))[:5]
    if buys:
        lines.append('\n🟢 尾盘强势(持仓内)')
        for s in buys:
            lines.append(f"  • {s.get('name')} {s.get('code')} ¥{s.get('price','')} 尾盘 {s.get('change'):+.1f}%")
    return '\n'.join(lines)


if __name__ == '__main__':
    import io, json
    _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== 尾盘持仓总结 自检 ===')
    r = run_eod_review(record_signals=False)
    print('summary:', r['summary'])
    print((r.get('text') or '')[:500])

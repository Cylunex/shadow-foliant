import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""尾盘持仓总结 —— 把"尾盘持仓分析 + 持仓 AI 体检 + 清仓助手"三条重叠推送**优化整合**成一条。

这三者都在回答同一问题"持仓尾盘该怎么处理",此前各推一条 → 同一只票在三条里出现三次、三套口径
(规则风险分 / AI体检动作 / 清仓紧迫分),又碎又互相打架。本模块**融合**:
  - 一次取数(_scan_holdings_with_snapshot)、一次规则打分(复用 exit_advisor 的紧迫分/归类/死钱)、
  - **一次 LLM 调用**只判断重点持仓的最终动作，每只票只有一个融合结论；
  - 一条极简报告：一句总览 + 最重要的 5 个动作；完整 items 留给 Agent 查询；
    同时保存一组决策信号(source_type='eod_review')。
比原来 3 任务 / 2 次 LLM / 3 条推送 更省、更不矛盾、更好看。

接口:run_eod_review(target_positions=20, record_signals=True) -> dict
"""

import re
from typing import Any, Dict, List, Optional

_ACT_MAP = {'清仓': 'sell', '卖出': 'sell', '减仓': 'reduce', '减持': 'reduce',
            '持有': 'hold', '观望': 'watch', '加仓': 'add', '增持': 'add'}
_ACT_ORDER = {'sell': 0, 'reduce': 1, 'add': 2, 'hold': 3, 'watch': 4}


def run_eod_review(target_positions: int = 20, record_signals: bool = True) -> Dict[str, Any]:
    out = {'ok': False, 'items': [], 'text': '', 'summary': ''}
    try:
        from portfolio_db import portfolio_db
        # 与 DB 层语义对齐(2026-07-17 修):quantity=0=已清仓(get_all_stocks 已过滤),
        # NULL=未填数量(代码记账用户)须保留 —— 原 `or 0` 把 NULL 当 0 踢掉,
        # 全 NULL 用户尾盘总结恒"当前无持仓"(早午盘任务却照跑,同一持仓两套口径)。
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

    # 2) 一次 LLM 只把确定性动作解释成人话；不得改变动作。
    decide = [r for r in rows if r['exit_score'] >= 15 or (r['sell_score'] or 0) >= 1][:18]
    _, ai_explanations = _ai_fuse(decide, n, target_positions, over)

    # 3) 唯一动作裁决：硬风险/组合约束/正式规则优先，LLM 只可解释同一动作。
    from core.action_decision import resolve_action
    items = []
    for r in rows:
        rule_source = (
            'hard_risk' if r['rule_action'] in ('sell', 'reduce')
            and ((r.get('sell_score') or 0) >= 3 or r.get('category') == '割肉止损')
            else 'formal_signal'
        )
        evidence = [{
            'source': rule_source, 'action': r['rule_action'],
            'reason': r['rule_reason'], 'code': r['code'],
        }]
        explanation = ai_explanations.get(r['code'])
        if explanation:
            evidence.append({
                'source': 'llm', 'action': r['rule_action'], 'reason': explanation,
                'code': r['code'], 'advisory_only': True,
            })
        decision = resolve_action(evidence)
        items.append({
            **r, 'action': decision['action'], 'reason': decision['reason'],
            'decision_source': decision['source'],
        })

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
    out['text'] = _format(items, n, target_positions, over)
    return out


def _ai_fuse(decide: List[Dict[str, Any]], n: int, target: int, over: bool):
    """Ask the LLM only for a plain explanation of an already fixed action."""
    if not decide:
        return ('', {})
    lines = [f"{r['code']} {r['name']}|既定动作:{r['rule_action']}|紧迫{r['exit_score']}|{r['category']}|"
             f"浮盈亏{(r['pnl'] if r['pnl'] is not None else 0):+.0f}%|持有{r.get('holding_days','?')}天|"
             f"风险分{r['sell_score']}({'/'.join(r['sell_reasons'][:3]) or '无'})" for r in decide]
    prompt = f"""你是持仓瘦身顾问。该账户持有 {n} 只{'，持仓偏多' if over else ''}。
下面动作已经由确定性风控规则决定：

{chr(10).join(lines)}

不得修改动作，只逐只把原因改写成一句通俗解释，严格一行一只，不要开场白:
代码 | 理由:≤25字

要求:不要输出技术指标缩写、目标价、仓位比例或新的买卖建议。"""
    try:
        from deepseek_client import DeepSeekClient
        ans = DeepSeekClient().call_api(
            [{'role': 'system', 'content': '你只负责把既定持仓动作解释成简短人话，不得改变动作或补充新建议。'},
             {'role': 'user', 'content': prompt}], max_tokens=800, call_type='eod_review') or ''
    except Exception as e:
        print(f'[eod_review] LLM 失败: {type(e).__name__}: {str(e)[:50]}')
        return ('', {})
    verdict = {}
    for line in ans.splitlines():
        mm = re.search(r'(\d{6})\D.*?理由[:：]\s*(.+)$', line)
        if mm:
            verdict[mm.group(1)] = mm.group(2).strip()[:30]
    return ('', verdict)


def _plain_reason(item: Dict[str, Any]) -> str:
    """把指标/流派术语压成人话，通知里只保留一个最主要的理由。"""
    reason = str(item.get('reason') or '').strip()
    reason = re.sub(r'风险信号[:：]?', '', reason)
    reason = re.sub(r'破MA(20|60)(?:\([^)]*\))?', r'跌破\1日均线', reason)
    reason = re.sub(r'缠论[一二三]卖', '走势转弱', reason)
    reason = re.sub(r'VaR95\s*[\d.]+%', '短期波动较大', reason, flags=re.I)
    reason = re.sub(r'年回撤-?[\d.]+%', '历史回撤较大', reason)
    reason = re.sub(r'浮亏-?([\d.]+)%', r'已亏约\1%', reason)
    reason = reason.replace('死钱占仓', '长期横盘').replace('清仓换仓', '腾出仓位')
    parts = []
    for part in re.split(r'[/；;，,]+', reason):
        part = part.strip(' 。')
        if part and part not in parts:
            parts.append(part)
    if parts:
        return '，'.join(parts[:2])[:42]
    return {
        '割肉止损': '亏损较大，走势也在转弱',
        '止盈锁定': '已有较多盈利，先落袋一部分',
        '死钱调出': '长期横盘，资金利用率较低',
        '破位减仓': '走势转弱，先降低风险',
    }.get(str(item.get('category') or ''), '风险变大，先少拿一点')


def _format(items, n, target, over) -> str:
    """极简尾盘通知：一句总览 + 最多五个动作，完整明细留给 Agent 查询。"""
    n_sell = sum(1 for it in items if it['action'] == 'sell')
    n_reduce = sum(1 for it in items if it['action'] == 'reduce')
    n_add = sum(1 for it in items if it['action'] == 'add')
    action_n = n_sell + n_reduce + n_add
    if action_n:
        verdict = '，'.join(s for s in (
            f'卖掉 {n_sell} 只' if n_sell else '',
            f'减一点 {n_reduce} 只' if n_reduce else '',
            f'可加一点 {n_add} 只' if n_add else '') if s)
        lines = [f"🧹 尾盘只看这几件事", f"持仓 {n} 只：{verdict}，其余先不动。"]
    else:
        lines = ["🧹 尾盘持仓", f"持仓 {n} 只，今天没有必须处理的，先不动。"]
    if over:
        lines.append(f"仓位比较分散，后面慢慢收敛到约 {target} 只，不用一天卖完。")
    act = sorted([it for it in items if it['action'] in ('sell', 'reduce', 'add')],
                 key=lambda x: (_ACT_ORDER.get(x['action'], 9), -x['exit_score']))[:5]
    if act:
        lines.append('')
        action_text = {'sell': '卖掉', 'reduce': '减一点', 'add': '可加一点'}
        for idx, it in enumerate(act, 1):
            pnl = it.get('pnl')
            if pnl is None:
                pnl_txt = '盈亏不明'
            elif pnl > 0:
                pnl_txt = f'赚{pnl:.0f}%'
            elif pnl < 0:
                pnl_txt = f'亏{abs(pnl):.0f}%'
            else:
                pnl_txt = '持平'
            lines.append(
                f"{idx}. {action_text.get(it['action'], '先不动')} {it['name']}（{it['code']}）："
                f"{pnl_txt}，{_plain_reason(it)}"
            )
        hidden = action_n - len(act)
        if hidden > 0:
            lines.append(f"另外 {hidden} 只暂不展开，需要时让 Agent 查完整明细。")
    return '\n'.join(lines)


if __name__ == '__main__':
    import io, json
    _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== 尾盘持仓总结 自检 ===')
    r = run_eod_review(record_signals=False)
    print('summary:', r['summary'])
    print((r.get('text') or '')[:500])

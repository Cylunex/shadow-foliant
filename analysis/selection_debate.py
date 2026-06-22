import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""选股红蓝对抗辩论 —— 给综合选股候选加一道"证伪闸门"。

缺口:unified_selection 的 TOP 候选只带规则打分、零 AI 证伪。"被 3 个 source 命中"≠"值得买"
(可能 3 个 source 都基于同一个已被 price-in 的利好)。

本模块对当日综合选股 TOP 逐只跑结构化红蓝对抗:多头列做多逻辑、空头专攻(估值透支/题材退潮/
财务雷/筹码松动/利好已兑现)、裁判给"对抗后结论 + 置信 + 主要风险点"。结论写 decision_signal
(source_type='selection_debate',买入→buy / 谨慎→watch / 否决→avoid)→ 16:10 方向后验,
可与"未经对抗"的选股 source 做胜率对比,量化验证这道闸门是否真有增量价值。

接口:run_selection_debate(codes, max_stocks=10, record_signals=True) -> dict
"""

import re
from typing import Any, Dict, List, Optional

_VERDICT_ACTION = {'买入': 'buy', '谨慎': 'watch', '否决': 'avoid'}


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, '', '?', 'N/A') else None
    except (TypeError, ValueError):
        return None


def _context(code: str) -> Dict[str, Any]:
    try:
        import datahub
        q = datahub.quote(code) or {}
        return {'name': q.get('name', ''), 'price': _f(q.get('price')),
                'change': _f(q.get('change_pct') or q.get('change')),
                'pe': _f(q.get('pe') or q.get('pe_ttm')), 'mcap': q.get('market_cap')}
    except Exception:
        return {'name': '', 'price': None, 'change': None, 'pe': None, 'mcap': None}


def _debate_one(code: str, ctx: Dict[str, Any]) -> Optional[Dict[str, str]]:
    prompt = f"""对候选股 {code} {ctx.get('name','')} 跑一场**红蓝对抗**(它今日被综合选股命中入选)。
基本信息:现价{ctx.get('price','?')} 今日{(ctx.get('change') or 0):+.2f}% PE{ctx.get('pe','?')}。

请依次:
①【多头】用 2-3 点列出最强做多逻辑。
②【空头】**专门攻击**:估值是否透支?题材是否退潮/利好已兑现?有无财务雷/筹码松动?入选信号是否同源(都基于一个已被 price-in 的利好)?
③【裁判】对抗后给结论。

最后一行严格输出(结论只能是 买入/谨慎/否决):
结论:X | 置信:高/中/低 | 主因:一句话(空头最致命的一点或多头最硬的支撑)"""
    try:
        from deepseek_client import DeepSeekClient
        ans = DeepSeekClient().call_api(
            [{'role': 'system', 'content': '你是选股质检官,空头视角要狠,裁判要中立,宁可错杀脆弱标的。'},
             {'role': 'user', 'content': prompt}], max_tokens=1200, call_type='selection_debate')
    except Exception as e:
        print(f'[selection_debate] {code} LLM 失败: {type(e).__name__}: {str(e)[:50]}')
        return None
    m = re.search(r'结论[:：]\s*(买入|谨慎|否决)', ans or '')
    if not m:
        return None
    verdict = m.group(1)
    conf = (re.search(r'置信[:：]\s*([高中低])', ans) or [None, '中'])[1]
    why = (re.search(r'主因[:：]\s*(.+)$', (ans or '').strip().splitlines()[-1]) or [None, ''])[1].strip()[:40]
    return {'verdict': verdict, 'confidence': conf, 'reason': why}


def run_selection_debate(codes: List[str], max_stocks: int = 10,
                         record_signals: bool = True) -> Dict[str, Any]:
    """对 codes 逐只红蓝对抗。返回 {ok, items, text, summary}。"""
    out = {'ok': False, 'items': [], 'text': '', 'summary': ''}
    codes = [str(c).strip() for c in (codes or []) if c][:max_stocks]
    if not codes:
        out['summary'] = '无候选'
        return out
    items, n_pass, n_reject = [], 0, 0
    for code in codes:
        ctx = _context(code)
        v = _debate_one(code, ctx)
        if not v:
            continue
        action = _VERDICT_ACTION.get(v['verdict'], 'watch')
        items.append({'code': code, 'name': ctx.get('name', ''), 'verdict': v['verdict'],
                      'action': action, 'confidence': v['confidence'], 'reason': v['reason']})
        if v['verdict'] == '买入':
            n_pass += 1
        elif v['verdict'] == '否决':
            n_reject += 1
        if record_signals:
            try:
                from decision_signal import create_signal
                create_signal(code=code, name=ctx.get('name', ''), action=action,
                              source_type='selection_debate', source_ref='red_blue',
                              confidence=v['confidence'], horizon='swing', ref_price=ctx.get('price'),
                              reason=f"红蓝对抗[{v['verdict']}]:{v['reason']}")
            except Exception:
                pass
    out['ok'] = True
    out['items'] = items
    out['summary'] = f'对抗 {len(items)} 只:通过 {n_pass}、否决 {n_reject}'
    out['text'] = _format(items)
    return out


def _format(items: List[Dict[str, Any]]) -> str:
    if not items:
        return ''
    order = {'否决': 0, '谨慎': 1, '买入': 2}
    rows = sorted(items, key=lambda x: order.get(x['verdict'], 3))
    lines = ['⚔️ 选股红蓝对抗(证伪闸门)']
    tag = {'买入': '🔴通过', '谨慎': '🟡谨慎', '否决': '🟢否决'}
    for it in rows:
        lines.append(f"  {tag.get(it['verdict'], it['verdict'])} {it['name']} {it['code']}"
                     f"（置信{it['confidence']}）\n      {it['reason']}")
    return '\n'.join(lines)


if __name__ == '__main__':
    import io, json
    _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== 红蓝对抗 自检(真实辩论 1 只) ===')
    r = run_selection_debate(['600519'], max_stocks=1, record_signals=False)
    print('summary:', r['summary'])
    print(json.dumps(r['items'], ensure_ascii=False, indent=1))

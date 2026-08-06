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
import os
from typing import Any, Dict, List, Optional

# 否决→sell(期望跌):让"证伪闸门"的否决可被方向后验(否对了=该股随后跌),从而量化闸门增量价值;
# 谨慎→watch(中性,不计胜负)。买入→buy。
_VERDICT_ACTION = {'买入': 'buy', '谨慎': 'watch', '否决': 'sell'}


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, '', '?', 'N/A') else None
    except (TypeError, ValueError):
        return None


def _parse_verdict(answer: str) -> Optional[Dict[str, str]]:
    """解析并校验裁判行；买入结论与明显风险主因冲突时保守降级。"""
    m = re.search(r'结论[:：]\s*(买入|谨慎|否决)', answer or '')
    if not m:
        return None
    verdict = m.group(1)
    conf = (re.search(r'置信[:：]\s*([高中低])', answer or '') or [None, '中'])[1]
    last_line = (answer or '').strip().splitlines()[-1] if (answer or '').strip() else ''
    why = (re.search(r'主因[:：]\s*(.+)$', last_line) or [None, ''])[1]
    why = why.strip().strip('"\'').replace('" But "', '').strip()[:60]
    if len(why) < 4:
        return None
    bearish = re.search(r'亏损|风险|利空|减值|不构成|缺乏|无新增|退潮|透支|松动|下滑|恶化|高估', why)
    if verdict == '买入' and bearish:
        verdict, conf = '谨慎', '低'
        why = f'结论与风险主因冲突，自动降级：{why}'[:60]
    return {'verdict': verdict, 'confidence': conf, 'reason': why}


def _context(code: str) -> Dict[str, Any]:
    try:
        import datahub
        q = datahub.quote(code) or {}
        name = q.get('name') or ''
        if not name:                       # 行情没给名 → 走持久名缓存,别让报告退化成裸代码
            try:
                name = datahub.stock_name(code)
            except Exception:
                name = ''
        return {'name': name, 'price': _f(q.get('price')),
                'change': _f(q.get('change_pct') or q.get('change')),
                'pe': _f(q.get('pe') or q.get('pe_ttm')), 'mcap': q.get('mcap_yi')}
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
    return _parse_verdict(ans)


def _debate_with_ctx(code: str) -> Optional[Dict[str, Any]]:
    """单只票的对抗 + ctx 一次性返回(便于并发)。"""
    ctx = _context(code)
    v = _debate_one(code, ctx)
    if not v:
        return None
    return {'code': code, 'ctx': ctx, 'v': v}


def run_selection_debate(codes: List[str], max_stocks: int = 10,
                         record_signals: bool = True) -> Dict[str, Any]:
    """对 codes 逐只红蓝对抗。返回 {ok, items, text, summary}。
    ⚡ 2026-06-30 改并发:此前串行 10 只 LLM 调用 ≈ 150s,改并发 5 → ≈ 30s。
    DeepSeek API 支持并发(rate-limit 宽松),decision_signal 写 PG 是 row-level 锁,无竞争。"""
    out = {'ok': False, 'items': [], 'text': '', 'summary': ''}
    codes = [str(c).strip() for c in (codes or []) if c][:max_stocks]
    if not codes:
        out['summary'] = '无候选'
        return out

    # 先一次批量取上下文；旧实现每只单独 datahub.quote，会把 10 只放大成 10 轮行情源请求。
    contexts = {}
    try:
        import datahub
        quotes = datahub.quotes(codes) or {}
        names = datahub.stock_names(codes) or {}
        for code in codes:
            q = quotes.get(code) or {}
            contexts[code] = {
                'name': q.get('name') or names.get(code) or '',
                'price': _f(q.get('price')),
                'change': _f(q.get('change_pct') or q.get('change')),
                'pe': _f(q.get('pe') or q.get('pe_ttm')),
                'mcap': q.get('mcap_yi'),
            }
    except Exception:
        contexts = {code: _context(code) for code in codes}

    def _run(code):
        ctx = contexts.get(code) or _context(code)
        v = _debate_one(code, ctx)
        return {'code': code, 'ctx': ctx, 'v': v} if v else None

    import concurrent.futures as _cf
    items, n_pass, n_reject = [], 0, 0
    # 生产实测 5 并发会持续触发 DeepSeek EmptyResponse；默认降到 2，仍可在 6 分钟护栏内完成。
    workers = max(1, min(int(os.getenv('SELECTION_DEBATE_WORKERS', '2')), 3))
    with _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix='selection-debate') as pool:
        results = list(pool.map(_run, codes))

    for r in results:
        if not r:
            continue
        code, ctx, v = r['code'], r['ctx'], r['v']
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
    lines = ['⚔️ 选股红蓝对抗 — 给今日入选股做多空辩论,筛掉「看着好其实脆」的']
    tag = {'买入': '🔴 可买', '谨慎': '🟡 观望', '否决': '🟢 别买·避开'}
    for it in rows:
        nm = it.get('name') or it['code']
        lines.append(f"  {tag.get(it['verdict'], it['verdict'])} {nm} {it['code']}"
                     f"（置信{it['confidence']}）\n      {it['reason']}")
    return '\n'.join(lines)


if __name__ == '__main__':
    import io, json
    _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== 红蓝对抗 自检(真实辩论 1 只) ===')
    r = run_selection_debate(['600519'], max_stocks=1, record_signals=False)
    print('summary:', r['summary'])
    print(json.dumps(r['items'], ensure_ascii=False, indent=1))

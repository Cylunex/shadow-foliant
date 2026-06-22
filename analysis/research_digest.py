import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""研报增量解读 —— 把已有但零调用的研报端点变成 AI 驱动的基本面事件信号。

缺口:datahub.stock_reports(code) 端点已存在却无任何 jobs 调用、无 AI 加工;基本面 agent 也不读
研报正文。研报评级变更/目标价是 A股最强基本面催化之一,现在完全靠人读。

本模块对 持仓 + 当日综合选股 的票,拉近 N 天券商研报:
  · 规则层:近 N 天研报数、主流评级(买入/增持…计数)、隐含目标空间(预测EPS×PE vs 现价中位)
  · AI 层:对有研报的票批量提炼『核心催化逻辑 + 评级方向(强烈看多/看多/中性/看空)』
强看多 + 隐含空间>阈值 → 写 decision_signal(source_type='research')→ 16:10 自动方向后验,
让"券商上调评级"这个 source 第一次进可量化反馈环(outcome_stats('source_type'))。

接口:run_research_digest(codes, days=10, max_llm=24, record_signals=True) -> dict
"""

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

_POS_RATINGS = ('买入', '增持', '强烈推荐', '推荐', '强推', '跑赢行业', '优于大市')


def _f(v) -> Optional[float]:
    try:
        x = float(str(v).strip())
        return x if x not in (0.0,) else None
    except (TypeError, ValueError):
        return None


def _recent_reports(code: str, days: int) -> List[Dict[str, Any]]:
    try:
        import datahub
        rows = datahub.stock_reports(code, max_pages=1) or []
    except Exception:
        return []
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    out = []
    for r in rows:
        pub = str(r.get('publishDate') or '')[:10]
        if pub and pub >= cutoff:
            out.append(r)
    return out


def _summarize_one(code: str, reports: List[Dict[str, Any]], price: Optional[float]) -> Dict[str, Any]:
    """规则层:某股近 N 天研报的结构化摘要。"""
    ratings: Dict[str, int] = {}
    orgs, targets = [], []
    for r in reports:
        rt = str(r.get('emRatingName') or r.get('sRatingName') or '').strip()
        if rt:
            ratings[rt] = ratings.get(rt, 0) + 1
        org = str(r.get('orgSName') or '').strip()
        if org:
            orgs.append(org)
        eps, pe = _f(r.get('predictThisYearEps')), _f(r.get('predictThisYearPe'))
        if eps and pe and eps > 0 and pe > 0:
            targets.append(eps * pe)
    pos = sum(c for rt, c in ratings.items() if any(p in rt for p in _POS_RATINGS))
    implied_upside = None
    if targets and price and price > 0:
        targets.sort()
        med = targets[len(targets) // 2]
        implied_upside = round((med / price - 1) * 100, 1)
    return {
        'code': code, 'n': len(reports),
        'ratings': ratings, 'pos_ratings': pos,
        'orgs': list(dict.fromkeys(orgs))[:5],
        'implied_upside_pct': implied_upside,
        'latest_title': str((reports[0] or {}).get('title') or '')[:40] if reports else '',
        'industry': str((reports[0] or {}).get('indvInduName') or '') if reports else '',
    }


def _llm_batch(summaries: List[Dict[str, Any]], names: Dict[str, str]) -> Dict[str, Dict[str, str]]:
    """AI 层:批量提炼核心逻辑 + 评级方向。返回 {code:{logic, direction}}。"""
    lines = []
    for s in summaries:
        rt = '、'.join(f'{k}×{v}' for k, v in s['ratings'].items()) or '无'
        up = f"隐含空间{s['implied_upside_pct']:+}%" if s['implied_upside_pct'] is not None else '空间未知'
        lines.append(f"{s['code']} {names.get(s['code'], '')}（{s['industry']}）: 近期研报{s['n']}篇,"
                     f"评级[{rt}],机构{('/'.join(s['orgs']))},{up};最新:{s['latest_title']}")
    prompt = f"""你是基本面研究主管。下面是若干股票**近期券商研报的结构化汇总**。
请逐只判断:研报观点的核心催化逻辑(一句话≤25字) + 综合评级方向。

研报汇总:
{chr(10).join(lines)}

对**每一只**输出一行,严格格式(方向只能是 强烈看多/看多/中性/看空 之一):
代码 | 方向:X | 逻辑:一句话核心催化

判断依据:买入/强推/增持评级多 + 隐含空间大 → 看多;评级分歧/中性/无空间 → 中性;减持/卖出 → 看空。"""
    try:
        from deepseek_client import DeepSeekClient
        ans = DeepSeekClient().call_api(
            [{'role': 'system', 'content': '你是严谨的卖方研究主管,只据研报事实判断,不臆测。'},
             {'role': 'user', 'content': prompt}], max_tokens=1800, call_type='research')
    except Exception as e:
        print(f'[research_digest] LLM 失败: {type(e).__name__}: {str(e)[:60]}')
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for line in (ans or '').splitlines():
        m = re.search(r'(\d{6})\D.*?方向[:：]\s*(强烈看多|看多|中性|看空)', line)
        if not m:
            continue
        logic = (re.search(r'逻辑[:：]\s*(.+)$', line) or [None, ''])[1].strip()[:30]
        out[m.group(1)] = {'direction': m.group(2), 'logic': logic}
    return out


_DIR_ACTION = {'强烈看多': 'buy', '看多': 'add', '中性': 'hold', '看空': 'avoid'}


def run_research_digest(codes: List[str], days: int = 10, max_llm: int = 24,
                        record_signals: bool = True) -> Dict[str, Any]:
    """对 codes 拉近 days 天研报 → 规则摘要 + AI 解读 → 强看多写决策信号。返回 {ok, items, text, summary}。"""
    out = {'ok': False, 'items': [], 'text': '', 'summary': ''}
    codes = [str(c).strip() for c in (codes or []) if c]
    if not codes:
        out['summary'] = '无标的'
        return out
    import datahub
    names: Dict[str, str] = {}
    summaries = []
    for c in dict.fromkeys(codes):
        reps = _recent_reports(c, days)
        if not reps:
            continue
        try:
            q = datahub.quote(c)
            price = _f(q.get('price')) if isinstance(q, dict) else None
            names[c] = q.get('name', '') if isinstance(q, dict) else ''
        except Exception:
            price = None
        summaries.append(_summarize_one(c, reps, price))
    if not summaries:
        out['ok'] = True
        out['summary'] = '覆盖标的近期无新研报'
        return out
    # 研报多的优先送 LLM,控 token
    summaries.sort(key=lambda x: (x['n'], x['pos_ratings']), reverse=True)
    llm = _llm_batch(summaries[:max_llm], names)

    items, n_bull = [], 0
    for s in summaries[:max_llm]:
        code = s['code']
        v = llm.get(code, {})
        direction = v.get('direction', '中性')
        action = _DIR_ACTION.get(direction, 'hold')
        it = {'code': code, 'name': names.get(code, ''), 'n_reports': s['n'],
              'direction': direction, 'logic': v.get('logic', ''),
              'implied_upside_pct': s['implied_upside_pct'], 'ratings': s['ratings']}
        items.append(it)
        if direction in ('强烈看多', '看多'):
            n_bull += 1
        # 强看多 + 隐含空间>8% → 写决策信号(方向后验)
        if record_signals and direction in ('强烈看多', '看多') and (s['implied_upside_pct'] or 0) > 8:
            try:
                from decision_signal import create_signal
                price = None
                try:
                    price = _f((datahub.quote(code) or {}).get('price'))
                except Exception:
                    pass
                create_signal(code=code, name=names.get(code, ''), action='buy',
                              source_type='research', source_ref='research_digest',
                              confidence=('高' if direction == '强烈看多' else '中'),
                              horizon='swing', ref_price=price,
                              reason=f"研报{s['n']}篇看多({direction},隐含{s['implied_upside_pct']:+}%):{v.get('logic','')}")
            except Exception:
                pass

    out['ok'] = True
    out['items'] = items
    out['summary'] = f'覆盖有研报 {len(summaries)} 只,AI 解读 {len(items)},看多 {n_bull}'
    out['text'] = _format(items)
    return out


def _format(items: List[Dict[str, Any]]) -> str:
    if not items:
        return ''
    order = {'强烈看多': 0, '看多': 1, '中性': 2, '看空': 3}
    rows = sorted(items, key=lambda x: order.get(x['direction'], 4))
    lines = ['📑 研报增量解读']
    for it in rows:
        up = f" 隐含{it['implied_upside_pct']:+}%" if it.get('implied_upside_pct') is not None else ''
        tag = {'强烈看多': '🔴强看多', '看多': '🔴看多', '中性': '⚪中性', '看空': '🟢看空'}.get(it['direction'], it['direction'])
        lines.append(f"  {tag} {it['name']} {it['code']}（研报{it['n_reports']}篇{up}）\n      {it['logic']}")
    return '\n'.join(lines)


if __name__ == '__main__':
    import io, json
    _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== 研报解读 自检(真实拉茅台+五粮液) ===')
    r = run_research_digest(['600519', '000858'], days=30, record_signals=False)
    print('summary:', r['summary'])
    print(json.dumps(r['items'][:2], ensure_ascii=False, indent=1))

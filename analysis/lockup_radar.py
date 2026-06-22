import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""持仓解禁雷达 —— 提前预警限售解禁下杀,补持仓风控最大盲区(事件型风险)。

缺口:持仓股临近大额解禁时,现有 MA/VaR 技术面根本看不到,等破位 already 晚了。解禁是 A股最典型
**可预知**的下杀。本模块用 `datahub.lockup_expiry`(东财 datacenter,结构化、快,非 pywencai)查持仓股
未来 N 天的解禁,按解禁比例筛重大事件,结合当前浮盈让 AI 给"解禁前是否减仓"研判 → 即时告警 +
decision_signal(action='reduce', source_type='lockup_risk')进方向后验。

接口:run_lockup_radar(codes, forward_days=60, min_ratio=0.03, record_signals=True) -> dict
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional


def _days_to(date_str: str) -> Optional[int]:
    try:
        d = datetime.strptime(str(date_str)[:10], '%Y-%m-%d')
        return (d - datetime.now()).days
    except Exception:
        return None


def _upcoming(code: str, forward_days: int, min_ratio: float) -> List[Dict[str, Any]]:
    try:
        import datahub
        d = datahub.lockup_expiry(code, forward_days=forward_days) or {}
    except Exception:
        return []
    out = []
    for u in (d.get('upcoming') or []):
        days = _days_to(u.get('date'))
        ratio = float(u.get('ratio') or 0)
        if days is not None and 0 <= days <= forward_days and ratio >= min_ratio:
            out.append({'date': str(u.get('date'))[:10], 'days': days, 'ratio': round(ratio * 100, 1),
                        'type': str(u.get('type') or '')})
    return sorted(out, key=lambda x: x['days'])


def _ai_review(flagged: List[Dict[str, Any]]) -> Dict[str, str]:
    lines = [f"{f['code']} {f['name']}: {f['days']}天后解禁 占总股本{f['ratio']}%"
             f"(浮盈亏{(f['pnl'] if f['pnl'] is not None else 0):+.0f}%)" for f in flagged]
    prompt = f"""你是持仓事件风控官。下面持仓股**临近限售解禁**(解禁=大量股份可卖,常引发抛压下杀)。
请逐只给"解禁前是否减仓"的明确建议。

{chr(10).join(lines)}

对每只输出一行(动作只能 减仓/清仓/持有观察):
代码 | 动作:X | 理由:一句话(结合解禁比例/天数/当前浮盈,≤25字)
解禁比例大(>5%)且临近(<30天)、又有浮盈 → 倾向解禁前减;比例小或已深套可观察。"""
    try:
        from deepseek_client import DeepSeekClient
        ans = DeepSeekClient().call_api(
            [{'role': 'system', 'content': '你是持仓事件风控官,解禁抛压不可忽视,给点名到票的减仓决策。'},
             {'role': 'user', 'content': prompt}], max_tokens=900, call_type='lockup_radar')
    except Exception as e:
        print(f'[lockup_radar] AI 失败: {type(e).__name__}: {str(e)[:50]}')
        return {}
    out = {}
    for line in (ans or '').splitlines():
        m = re.match(r'\s*(\d{6})\D.*?动作[:：]\s*(减仓|清仓|持有观察)', line)
        if m:
            why = (re.search(r'理由[:：]\s*(.+)$', line) or [None, ''])[1].strip()[:30]
            out[m.group(1)] = {'action_cn': m.group(2), 'reason': why}
    return out


_ACT = {'清仓': 'sell', '减仓': 'reduce', '持有观察': 'watch'}


def run_lockup_radar(codes: List[str], forward_days: int = 60, min_ratio: float = 0.03,
                     record_signals: bool = True) -> Dict[str, Any]:
    """查 codes 未来 forward_days 天解禁(占比≥min_ratio)→ AI 研判减仓。返回 {ok, items, text, summary}。"""
    out = {'ok': False, 'items': [], 'text': '', 'summary': ''}
    codes = list(dict.fromkeys([str(c).strip() for c in (codes or []) if c]))
    if not codes:
        out['summary'] = '无标的'
        return out
    import datahub
    # 取浮盈上下文(尽量复用持仓扫描)
    pnl_map = {}
    try:
        from jobs_hub import _scan_holdings_with_snapshot
        pnl_map = {str(s.get('code')): s.get('pnl') for s in (_scan_holdings_with_snapshot() or [])}
    except Exception:
        pnl_map = {}

    flagged = []
    for c in codes:
        ups = _upcoming(c, forward_days, min_ratio)
        if not ups:
            continue
        try:
            nm = (datahub.quote(c) or {}).get('name', '')
        except Exception:
            nm = ''
        nearest = ups[0]
        flagged.append({'code': c, 'name': nm, 'date': nearest['date'], 'days': nearest['days'],
                        'ratio': nearest['ratio'], 'pnl': pnl_map.get(c)})
    if not flagged:
        out['ok'] = True
        out['summary'] = f'覆盖 {len(codes)} 只,未来{forward_days}天无重大解禁'
        return out

    review = _ai_review(flagged)
    items = []
    for f in flagged:
        v = review.get(f['code'], {'action_cn': '持有观察', 'reason': ''})
        action = _ACT.get(v['action_cn'], 'watch')
        it = {**f, 'action': action, 'action_cn': v['action_cn'], 'reason': v.get('reason', '')}
        items.append(it)
        if record_signals and action in ('reduce', 'sell'):
            try:
                from decision_signal import create_signal
                price = None
                try:
                    price = (datahub.quote(f['code']) or {}).get('price')
                    price = float(price) if price not in (None, '', '?') else None
                except Exception:
                    pass
                create_signal(code=f['code'], name=f['name'], action='reduce' if action == 'reduce' else 'sell',
                              source_type='lockup_risk', source_ref='lockup', confidence='高',
                              horizon='short', ref_price=price,
                              reason=f"{f['days']}天后解禁{f['ratio']}%:{v.get('reason','')}")
            except Exception:
                pass

    out['ok'] = True
    out['items'] = items
    out['summary'] = f'解禁预警 {len(items)} 只(未来{forward_days}天)'
    out['text'] = _format(items)
    return out


def _format(items: List[Dict[str, Any]]) -> str:
    if not items:
        return ''
    rows = sorted(items, key=lambda x: x['days'])
    lines = ['⏳ 持仓解禁雷达']
    tag = {'sell': '🔴清仓', 'reduce': '🟠减仓', 'watch': '⚪观察'}
    for it in rows:
        pnl = f" 浮盈亏{it['pnl']:+.0f}%" if it.get('pnl') is not None else ''
        lines.append(f"  {tag.get(it['action'])} {it['name']} {it['code']} — {it['days']}天后解禁 {it['ratio']}%{pnl}\n      {it['reason']}")
    return '\n'.join(lines)


if __name__ == '__main__':
    import io, json
    _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== 解禁雷达 自检 ===')
    r = run_lockup_radar(['000158', '600519'], forward_days=180, min_ratio=0.0, record_signals=False)
    print('summary:', r['summary'])
    print(json.dumps(r['items'][:3], ensure_ascii=False, indent=1))

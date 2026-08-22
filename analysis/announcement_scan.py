import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""公告事件抽取与重大性分级 —— 持仓/监控池的"黑天鹅预警"。

缺口:`datahub.announcements(code)` 端点存在却零调用,公告未进任何分析。利空公告(商誉减值/大额减持/
立案调查/业绩暴雷)是 A股最典型可预知下杀,现有 risk agent 只算 VaR 不读公告。

本模块对持仓+监控池拉近 N 天新公告，用 LLM 对标题做初筛。标题级判断只用于待确认
提示，不创建交易信号，也不写入正式 EventRecord；正文数字与重大性规则确认后才可进入评分。

接口:run_announcement_scan(codes, days=5, max_llm=40, record_signals=False) -> dict
"""

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


def _parse_date(v) -> Optional[str]:
    """公告 date 多为毫秒时间戳;也兼容 'YYYY-MM-DD'。返回 'YYYY-MM-DD' 或 None。"""
    if v is None or v == '':
        return None
    s = str(v)
    if s.isdigit() and len(s) >= 12:
        try:
            return datetime.fromtimestamp(int(s) / 1000).strftime('%Y-%m-%d')
        except Exception:
            return None
    return s[:10]


def _recent_records(code: str, days: int, cap: int = 8) -> List[dict]:
    try:
        import datahub
        anns = datahub.announcements(code) or []
    except Exception:
        return []
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    out = []
    for a in anns:
        d = _parse_date(a.get('date'))
        if d and d >= cutoff:
            t = str(a.get('title') or '').strip()
            if t:
                url = str(a.get('url') or '')
                document_id = (re.search(r'annoId=([^&]+)', url) or [None, ''])[1]
                out.append({'title': t, 'date': d, 'type': str(a.get('type') or ''),
                            'url': url, 'document_id': document_id})
    return out[:cap]


def _recent_titles(code: str, days: int, cap: int = 8) -> List[str]:
    return [item['title'] for item in _recent_records(code, days, cap)]


def _llm_classify(blocks: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    lines = []
    for b in blocks:
        titles = ' || '.join(b['titles'])
        lines.append(f"{b['code']} {b['name']}: {titles}")
    prompt = f"""你是公告事件分析师。下面是若干股票**近期公告标题**。请逐只判断**最具市场影响的一条事件**:
事件类型(业绩预告/增减持/股权激励/重组并购/解禁/诉讼立案/商誉减值/高管变动/中标合同/回购/分红 等)、
方向(利好/利空/中性)、强度(1-5,5=重大)。

公告:
{chr(10).join(lines)}

对**每一只**输出一行,严格格式:
代码 | 类型:X | 方向:利好/利空/中性 | 强度:N | 摘要:一句话(≤25字)
没有实质事件的输出 方向:中性 强度:1。"""
    try:
        from deepseek_client import DeepSeekClient
        ans = DeepSeekClient().call_api(
            [{'role': 'system', 'content': '你是严谨的公告事件分析师,只据公告标题判断,商誉减值/大额减持/立案=利空高分。'},
             {'role': 'user', 'content': prompt}], max_tokens=1800, call_type='announcement')
    except Exception as e:
        print(f'[announcement_scan] LLM 失败: {type(e).__name__}: {str(e)[:60]}')
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for line in (ans or '').splitlines():
        m = re.search(r'(\d{6})\D', line)
        if not m:
            continue
        code = m.group(1)
        typ = (re.search(r'类型[:：]\s*([^|｜\n]+)', line) or [None, ''])[1].strip()[:12]
        direction = (re.search(r'方向[:：]\s*(利好|利空|中性)', line) or [None, '中性'])[1]
        strength = int((re.search(r'强度[:：]\s*([1-5])', line) or [None, '1'])[1])
        summ = (re.search(r'摘要[:：]\s*(.+)$', line) or [None, ''])[1].strip()[:30]
        out[code] = {'type': typ, 'direction': direction, 'strength': strength, 'summary': summ}
    return out


def run_announcement_scan(codes: List[str], days: int = 5, max_llm: int = 40,
                          record_signals: bool = False) -> Dict[str, Any]:
    """扫描标题并输出待正文确认的参考事件；``record_signals`` 仅保留调用兼容。"""
    out = {'ok': False, 'items': [], 'alerts': [], 'text': '', 'summary': ''}
    codes = list(dict.fromkeys([str(c).strip() for c in (codes or []) if c]))[:max_llm]
    if not codes:
        out['summary'] = '无标的'
        return out
    import datahub
    blocks, names = [], {}
    for c in codes:
        announcements = _recent_records(c, days)
        if not announcements:
            continue
        try:
            q = datahub.quote(c)
            names[c] = q.get('name', '') if isinstance(q, dict) else ''
        except Exception:
            names[c] = ''
        blocks.append({'code': c, 'name': names[c],
                       'titles': [item['title'] for item in announcements],
                       'announcements': announcements})
    if not blocks:
        out['ok'] = True
        out['summary'] = '覆盖标的近期无新公告'
        return out
    cls = _llm_classify(blocks)

    items, alerts = [], []
    for b in blocks:
        code = b['code']
        v = cls.get(code)
        if not v or v['strength'] < 2:
            continue
        event_date = max((item['date'] for item in b.get('announcements', []) if item.get('date')),
                         default=datetime.now().strftime('%Y-%m-%d'))
        it = {'code': code, 'name': names.get(code, ''), 'event_date': event_date,
              'source': 'cninfo', 'source_family': 'official_disclosure',
              'source_origin': 'cninfo', 'official': True,
              'document_id': next((item.get('document_id') for item in b['announcements']
                                   if item.get('document_id')), ''),
              'confirmation_status': 'title_only', 'materiality': None,
              'surprise': None, 'novelty': None, 'entity_impact': 'issuer', **v}
        items.append(it)
        is_bad = v['direction'] == '利空' and v['strength'] >= 3
        if is_bad:
            alerts.append(it)

    out['ok'] = True
    out['items'] = items
    out['alerts'] = alerts
    out['summary'] = f'扫公告 {len(blocks)} 只,事件 {len(items)},利空预警 {len(alerts)}'
    out['text'] = _format(items)
    return out


def _format(items: List[Dict[str, Any]]) -> str:
    if not items:
        return ''
    order = {'利空': 0, '利好': 1, '中性': 2}
    rows = sorted(items, key=lambda x: (order.get(x['direction'], 3), -x['strength']))
    lines = ['📢 公告事件分级']
    tag = {'利空': '🟢利空', '利好': '🔴利好', '中性': '⚪中性'}
    for it in rows:
        lines.append(f"  {tag.get(it['direction'])}{it['strength']} {it['name']} {it['code']}"
                     f"[{it['type']}]\n      {it['summary']}")
    return '\n'.join(lines)


if __name__ == '__main__':
    import io, json
    _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== 公告分级 自检(真实拉 1 只) ===')
    r = run_announcement_scan(['000158'], days=30, record_signals=False)
    print('summary:', r['summary'])
    print(json.dumps(r['items'], ensure_ascii=False, indent=1))

"""
事件驱动打分 —— 借鉴 SkillHub「事件驱动策略」skill。

把(新闻/公告/事件 + LLM 情绪分)按「指数时间衰减」聚合为个股事件信号分,
用于新闻流量/事件驱动选股的量化打分。补"新闻情绪无时间衰减聚合"的空白。

纯计算,零依赖。LLM 情绪打分在上层完成,这里只做衰减聚合与映射。
"""

from __future__ import annotations
from typing import List, Dict, Any
import math


def decay_weight(days_ago: float, half_life: float = 3.0) -> float:
    """指数时间衰减权重:half_life 天后权重减半。"""
    if days_ago < 0:
        days_ago = 0
    return 0.5 ** (days_ago / half_life)


def aggregate_event_score(events: List[Dict[str, Any]], half_life: float = 3.0) -> Dict[str, Any]:
    """聚合事件情绪为综合信号分。

    events: [{'sentiment': -1..1, 'days_ago': float, 'weight': 可选重要性(默认1), 'title': 可选}]
    返回: {'score': -100..100, 'signal': 看多/中性/看空, 'n': 事件数, 'top': 影响最大的事件}
    """
    if not events:
        return {'score': 0.0, 'signal': '中性', 'n': 0, 'top': None}
    num = 0.0
    den = 0.0
    contrib = []
    for e in events:
        s = float(e.get('sentiment', 0))
        w = decay_weight(float(e.get('days_ago', 0)), half_life) * float(e.get('weight', 1))
        num += s * w
        den += w
        contrib.append((abs(s) * w, e))
    raw = (num / den) if den else 0.0          # 加权平均情绪 [-1,1]
    score = round(raw * 100, 1)
    signal = '看多' if score >= 20 else ('看空' if score <= -20 else '中性')
    top = max(contrib, key=lambda x: x[0])[1] if contrib else None
    return {
        'score': score,
        'signal': signal,
        'n': len(events),
        'half_life': half_life,
        'top': {'title': (top or {}).get('title'), 'sentiment': (top or {}).get('sentiment'),
                'days_ago': (top or {}).get('days_ago')} if top else None,
        'summary': f"事件驱动信号 {score:+.0f}({signal}),基于 {len(events)} 条事件(半衰期{half_life}天)" +
                   (f",主导事件:{(top or {}).get('title')}" if top and top.get('title') else "") + "。",
    }


if __name__ == '__main__':
    import io, sys, json
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    evs = [
        {'sentiment': 0.8, 'days_ago': 0, 'weight': 2, 'title': '中标大订单'},
        {'sentiment': 0.3, 'days_ago': 2, 'title': '机构调研'},
        {'sentiment': -0.6, 'days_ago': 8, 'title': '股东减持(较早)'},
    ]
    r = aggregate_event_score(evs)
    print(json.dumps(r, ensure_ascii=False, indent=1))

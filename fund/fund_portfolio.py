"""基金组合诊断(阶段三)—— 资产类型配置 / 集中度 / 持仓穿透重叠 / 简单建议。

输入持有列表(fund_db.get_holdings 的输出,或带 market_value 的 dict 列表)。
缺市值时按最新净值×份额估算。重仓股穿透重叠为可选(需联网,慢),默认关。
"""

from __future__ import annotations

from collections import defaultdict
from typing import List, Dict, Optional

import fund_data

# 基金类型 → 大类资产
_ASSET_CLASS = {
    '股票': '权益', '混合': '权益', '指数': '权益', 'QDII': '权益', 'LOF': '权益', 'ETF': '权益',
    '债券': '固收', '货币': '现金', 'FOF': '配置',
}


def _asset_class(ftype: Optional[str]) -> str:
    if not ftype:
        return '未知'
    for k, v in _ASSET_CLASS.items():
        if k in ftype:
            return v
    return '其他'


def _class_from_name(name: Optional[str]) -> str:
    """由基金名称推断大类(免逐只联网 fund_type)。现金/固收关键词优先判,避免
    "同业存单指数"等被"指数"误判为权益。"""
    n = name or ''
    if '货币' in n or '同业存单' in n:
        return '现金'
    if '债' in n:
        return '固收'
    if 'FOF' in n:
        return '配置'
    if any(k in n for k in ('股票', '混合', '指数', 'QDII', 'LOF', 'ETF', '成长', '价值', '精选')):
        return '权益'
    return '其他'


def diagnose(holdings: List[Dict], with_overlap: bool = False) -> dict:
    """组合诊断。holdings: [{code, name, shares, cost_nav, [market_value]}]。
    返回 类型/大类配置权重、集中度(HHI/top1/top3)、(可选)重仓股重叠、建议。"""
    if not holdings:
        return {'error': '组合为空'}

    rows = []
    for h in holdings:
        code = str(h['code']).zfill(6)
        mv = h.get('market_value')
        if mv is None:
            latest = fund_data.latest_nav(code)
            nav = latest['unit_nav'] if latest else None
            mv = (h.get('shares') or 0) * nav if nav else 0.0
        ftype = h.get('ftype') or fund_data.fund_type(code)
        rows.append({'code': code, 'name': h.get('name'), 'ftype': ftype,
                     'asset': _asset_class(ftype), 'mv': float(mv or 0)})

    total = sum(r['mv'] for r in rows) or 1.0
    for r in rows:
        r['weight'] = r['mv'] / total

    # 大类 / 类型 配置
    by_asset = defaultdict(float)
    by_type = defaultdict(float)
    for r in rows:
        by_asset[r['asset']] += r['weight']
        by_type[(r['ftype'] or '未知').split('-')[0]] += r['weight']

    # 集中度
    weights = sorted((r['weight'] for r in rows), reverse=True)
    hhi = sum(w * w for w in weights)
    top1 = weights[0] if weights else 0
    top3 = sum(weights[:3])

    advice = []
    if top1 > 0.5:
        advice.append(f'单只占比 {top1:.0%} 偏高,集中度风险大,考虑分散。')
    if by_asset.get('权益', 0) > 0.85:
        advice.append('权益类占比 >85%,波动较大,可配置部分固收/货币平滑回撤。')
    if len(rows) < 3:
        advice.append('持有基金过少,分散不足。')
    if hhi < 0.25 and len(rows) >= 5 and not advice:
        advice.append('分散度良好。')

    out = {
        'n_funds': len(rows), 'total_mv': round(total, 2),
        'asset_allocation': {k: round(v, 4) for k, v in by_asset.items()},
        'type_allocation': {k: round(v, 4) for k, v in by_type.items()},
        'concentration': {'hhi': round(hhi, 4), 'top1': round(top1, 4), 'top3': round(top3, 4)},
        'holdings': [{'code': r['code'], 'name': r['name'], 'ftype': r['ftype'],
                      'asset': r['asset'], 'weight': round(r['weight'], 4)} for r in rows],
        'advice': advice or ['组合结构合理。'],
    }

    if with_overlap:
        out['stock_overlap'] = _holdings_overlap(rows)
    return out


_OVERLAP_MAX_FUNDS = 15   # 重仓穿透只取权重最大的前 N 只基金,避免对几十只基金逐只打东财季报接口


def _holdings_overlap(rows: List[Dict]) -> List[Dict]:
    """重仓股穿透重叠:统计被多只基金共同重仓的股票(按持仓占比×基金权重 加权)。慢,需联网。
    ⚠️ 2026-06-27 防东财封禁:① 只穿透权重最大的前 _OVERLAP_MAX_FUNDS 只基金(组合再大也封顶);
    ② 盘中(交易时段)对 get_stock_holdings 传 cache_only=True —— 只读缓存、冷则跳过,绝不盘中逐只现拉。
    重仓是季报数据(get_stock_holdings 自带 1 天文件缓存),盘后焐一次盘中复用。"""
    try:
        from datahub import _is_trading_hours
        _cache_only = _is_trading_hours()
    except Exception:
        _cache_only = False
    rows = sorted(rows, key=lambda r: r.get('weight', 0), reverse=True)[:_OVERLAP_MAX_FUNDS]
    stock_w = defaultdict(float)
    stock_funds = defaultdict(set)
    for r in rows:
        hold = fund_data.get_stock_holdings(r['code'], cache_only=_cache_only)
        if hold is None or len(hold) == 0:
            continue
        name_col = next((c for c in hold.columns if '股票名称' in c or '名称' in c), None)
        pct_col = next((c for c in hold.columns if '占净值比例' in c or '占比' in c), None)
        if not name_col:
            continue
        import pandas as pd
        for _, hr in hold.iterrows():
            sname = str(hr[name_col])
            pct = float(pd.to_numeric(hr.get(pct_col, 0), errors='coerce') or 0) / 100 if pct_col else 0
            stock_w[sname] += r['weight'] * pct
            stock_funds[sname].add(r['code'])
    overlap = [{'stock': s, 'in_funds': len(stock_funds[s]), 'combined_weight': round(w, 4)}
               for s, w in stock_w.items() if len(stock_funds[s]) >= 2]
    return sorted(overlap, key=lambda x: x['combined_weight'], reverse=True)[:15]


def combined_asset_view() -> dict:
    """股票 + 基金 大类资产合并视图(**成本口径**,offline,best-effort)。
    股票来自 portfolio_db.get_all_stocks(成本=数量×成本价);基金来自 fund_db(成本=份额×成本净值)。
    任一侧缺失/异常都不影响另一侧。返回各大类金额与占比。"""
    buckets = defaultdict(float)
    detail = {'stock': [], 'fund': []}
    # 股票侧
    try:
        from portfolio_db import portfolio_db
        for s in portfolio_db.get_all_stocks():
            qty = s.get('quantity') or s.get('shares') or 0
            cost = s.get('cost_price') or s.get('cost') or 0
            val = float(qty) * float(cost)
            buckets['股票'] += val
            detail['stock'].append({'code': s.get('code'), 'name': s.get('name'), 'value': round(val, 2)})
    except Exception as e:
        detail['stock_error'] = f'{type(e).__name__}'
    # 基金侧(按大类细分)
    try:
        import fund_db
        fund_db.init_db()
        for h in fund_db.get_holdings():
            val = float(h.get('shares') or 0) * float(h.get('cost_nav') or 0)
            cls = _class_from_name(h.get('name'))   # ⚡ 名称推断,免逐只联网 fund_type(53只省~7s)
            buckets[f'基金-{cls}'] += val
            detail['fund'].append({'code': h['code'], 'name': h.get('name'),
                                   'asset': cls, 'value': round(val, 2)})
    except Exception as e:
        detail['fund_error'] = f'{type(e).__name__}'

    total = sum(buckets.values()) or 1.0
    return {
        'basis': '成本口径(数量×成本)',
        'total': round(total, 2),
        'allocation': {k: round(v / total, 4) for k, v in sorted(buckets.items(), key=lambda x: -x[1])},
        'amounts': {k: round(v, 2) for k, v in buckets.items()},
        'detail': detail,
    }


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    demo = [
        {'code': '110011', 'name': '易方达优质精选', 'shares': 1000, 'market_value': 4380, 'ftype': '混合型'},
        {'code': '000001', 'name': '华夏成长', 'shares': 2000, 'market_value': 3000, 'ftype': '混合型'},
        {'code': '000011', 'name': '某债基', 'shares': 5000, 'market_value': 6000, 'ftype': '债券型'},
    ]
    from pprint import pprint
    pprint(diagnose(demo))

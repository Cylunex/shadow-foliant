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

    from fund.fund_contracts import product
    rows = []
    for h in holdings:
        code=str(h['code']).zfill(6)
        profile=h.get('profile') or product(code,h.get('name'),h.get('ftype'))
        mv=h.get('market_value',h.get('mv'))
        rows.append({'code':code,'name':h.get('name'),'ftype':h.get('ftype') or profile.get('ftype'),
                     'asset':profile.get('asset','未知'),'mv':float(mv) if mv is not None else 0.,
                     'valued':mv is not None,'profile':profile,'nav_date':h.get('nav_date')})

    total = sum(r['mv'] for r in rows)
    for r in rows:
        r['weight'] = r['mv'] / total if total else 0.

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
        advice.append('持有基金较少，需结合底层资产覆盖判断分散程度。')
    if hhi < 0.25 and len(rows) >= 5 and not advice:
        advice.append('分散度良好。')

    out = {
        'n_funds': len(rows), 'total_mv': round(total, 2),
        'asset_allocation': {k: round(v, 4) for k, v in by_asset.items()},
        'type_allocation': {k: round(v, 4) for k, v in by_type.items()},
        'concentration': {'hhi': round(hhi, 4), 'top1': round(top1, 4), 'top3': round(top3, 4)},
        'holdings': [{'code': r['code'], 'name': r['name'], 'ftype': r['ftype'],
                      'asset': r['asset'], 'weight': round(r['weight'], 4)} for r in rows],
        'advice': advice or ['请结合目标配置、数据覆盖和费用复核。'],
        'valuation_coverage':sum(r['valued'] for r in rows)/len(rows),
        'valuation_dates':{r['code']:r['nav_date'] for r in rows},
        'status':'ready' if all(r['valued'] for r in rows) else 'partial',
        'exposure':exposure_summary(rows),
    }

    if with_overlap:
        detail = disclosed_exposure(rows)
        out['disclosure'] = detail
        out['stock_overlap'] = [x for x in detail['stocks'] if x['in_funds']>=2]
    return out


_OVERLAP_MAX_FUNDS = 15   # 重仓穿透只取权重最大的前 N 只基金,避免对几十只基金逐只打东财季报接口


def _holdings_overlap(rows):
    return disclosed_exposure(rows)['stocks']


def exposure_summary(rows):
    result={}
    for field in ('asset','region','currency','manager','family_id'):
        bucket=defaultdict(float)
        for r in rows:
            key=r.get('asset') if field=='asset' else r.get('profile',{}).get(field)
            bucket[str(key or '未知')]+=r['weight']
        result[field]=dict(bucket)
    industry=defaultdict(float)
    for r in rows:
        weights=r.get('profile',{}).get('industry_weights') or {}
        known=0.
        for key,value in weights.items():
            weight=float(value)
            if weight<0 or known+weight>1.000001:raise ValueError('行业暴露合计须不超过 1')
            industry[key]+=r['weight']*weight;known+=weight
        industry['未知']+=r['weight']*(1-known)
    result['industry']=dict(industry)
    return result


def disclosed_exposure(rows):
    from fund.fund_contracts import now
    stocks={}; reports=[]; covered=0.
    for r in sorted(rows,key=lambda r:-r['weight'])[:_OVERLAP_MAX_FUNDS]:
        top=r.get('profile',{}).get('top_holdings')
        if not top:
            # Existing disk cache is used without any per-fund online fan-out.
            frame=fund_data.get_stock_holdings(r['code'],cache_only=True)
            if frame is None or frame.empty: continue
            top=fund_data.normalize_holdings(frame)
        total=0.
        for h in top.get('holdings',[]):
            if not h.get('code') or h.get('pct') is None: continue
            pct=float(h['pct'])/100
            if not 0<=pct<=1: continue
            market=h.get('market')
            # Unknown market is explicit, never merged by company name alone.
            identity=str(market or 'unknown')+':'+str(h['code'])
            item=stocks.setdefault(identity,{'security_id':identity,'stock':h.get('name') or h['code'],
                'combined_weight':0.,'funds':set(),'market':market})
            item['combined_weight']+=r['weight']*pct;item['funds'].add(r['code']);total+=pct
        fraction=min(total,1.)
        covered+=r['weight']*fraction
        reports.append({'code':r['code'],'quarter':top.get('quarter'),'published_at':top.get('published_at'),
                        'first_seen_at':top.get('first_seen_at'),'disclosed_fraction':fraction})
    result=[]
    for item in stocks.values():
        item['in_funds']=len(item.pop('funds'));result.append(item)
    return {'stocks':sorted(result,key=lambda x:-x['combined_weight']),'reports':reports,
            'disclosed_weight':min(covered,1.),'unknown_weight':max(0,1-covered),
            'basis':'已披露前十大持仓，非完整资产穿透','observed_at':now()}


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


def rebalance_preview(snapshot, targets, available_cash, *, fee_rules=None, allow_redemptions=False):
    from fund.fund_contracts import code6, number
    fee_rules=fee_rules or {}
    cash=number(available_cash)
    if cash<0: raise ValueError('可用现金不能为负')
    weights={code6(k):number(v) for k,v in targets.items()}
    if any(v<0 or v>1 for v in weights.values()) or sum(weights.values())>1.000001:
        raise ValueError('目标权重须介于 0～1，合计不得超过 1')
    if not weights: return {'status':'needs_targets','orders':[],'reason':'请先设置目标配置'}
    holdings=snapshot['holdings']
    if any(h.get('mv') is None for h in holdings):
        return {'status':'partial','orders':[],'reason':'存在缺失估值，不能生成调仓数量'}
    current={h['code']:h for h in holdings}
    total=sum(h['mv'] for h in holdings)+cash
    orders=[]
    for code in sorted(set(current)|set(weights)):
        h=current.get(code,{})
        diff=total*weights.get(code,0)-h.get('mv',0)
        rules=fee_rules.get(code,{})
        blockers=[]
        profile=dict(h.get('profile',{}))
        for field in ('subscription_status','redemption_status'):
            if profile.get(field) is None and rules.get(field) is not None:profile[field]=rules[field]
        if profile.get('execution') not in (None,'off_exchange'): blockers.append('需要场内成交价格与溢折价模型')
        if diff<0 and not allow_redemptions: blockers.append('本次优先新增资金，未启用赎回预览')
        if profile.get('subscription_status') in ('closed','suspended') and diff>0: blockers.append('暂停申购')
        if profile.get('redemption_status') in ('closed','suspended') and diff<0: blockers.append('暂停赎回')
        rate=rules.get('purchase_rate' if diff>0 else 'redemption_rate')
        if rate is None: blockers.append('费率待确认')
        elif not 0<=number(rate)<1:raise ValueError('渠道费率须介于 0～1')
        if diff>0 and profile.get('subscription_status') is None: blockers.append('申购状态待核验')
        if diff<0 and profile.get('redemption_status') is None: blockers.append('赎回状态待核验')
        if diff<0 and not rules.get('holding_period_verified'): blockers.append('持有期与赎回费阶梯待核验')
        if abs(diff)<.005:continue
        value=abs(diff)
        fee_detail=None
        if diff<0 and rules.get('redemption_schedule'):
            if not h.get('nav') or not h.get('lots'):blockers.append('缺少持有批次或净值')
            else:
                from fund.fund_fees import redemption_charge
                left=min(float(h['shares']),value/float(h['nav']))
                selling=[]
                for lot in h['lots']:
                    take=min(left,float(lot['shares']))
                    if take:selling.append({**lot,'shares':take});left-=take
                try:fee_detail=redemption_charge(selling,h['nav'],snapshot['as_of'],rules['redemption_schedule'])
                except ValueError:blockers.append('批次持有期未核验')
        executable=not blockers
        orders.append({'code':code,'action':'subscribe' if diff>0 else 'redeem',
                       'current_weight':h.get('mv',0)/total if total else 0,'target_weight':weights.get(code,0),
                       'gross_amount':round(value,2),'fee':fee_detail['fee'] if fee_detail else round(value*number(rate),2) if rate is not None else None,'fee_details':fee_detail,
                       'executable':executable,'blockers':blockers})
    needed=sum(x['gross_amount'] for x in orders if x['action']=='subscribe' and x['executable'])
    scale=min(1,cash/needed) if needed else 1
    remaining=__import__('math').floor(cash*100+1e-8)/100
    for x in orders:
        if x['action']=='subscribe':
            x['budgeted_amount']=min(remaining,round(x['gross_amount']*scale,2)) if x['executable'] else 0
            remaining=round(remaining-x['budgeted_amount'],2)
            if x['executable'] and x['fee'] is not None: x['budgeted_fee']=round(x['fee']*scale,2)
    return {'status':'ready' if all(x['executable'] for x in orders) else 'partial',
            'basis':'新增现金优先；预计费用需与渠道成交回执核对','available_cash':cash,
            'estimated_cash_used':sum(x.get('budgeted_amount',0) for x in orders),
            'orders':orders,'as_of':snapshot['as_of'],'execution':'preview_only'}


def merge_market_values(funds,stocks,stock_date,as_of):
    """Combine dated valuations; ambiguous cross-book duplicates stay out of totals."""
    funds_by_code={h['code']:h for h in funds}
    amounts=defaultdict(float); details=[]; excluded=[];missing=[]
    for h in funds:
        if h.get('mv') is None:missing.append(h['code']);continue
        asset=h.get('profile',{}).get('asset','未知')
        amounts['基金-'+asset]+=h['mv']
        details.append({'code':h['code'],'value':h['mv'],'as_of':h.get('nav_date'),'basis':'confirmed_nav'})
    for h in stocks:
        code=str(h['code'])
        if code in funds_by_code and (code.startswith(('15','16','5')) or h.get('asset_type')=='fund'):
            excluded.append({'code':code,'reason':'基金与股票账本存在同标的，需核对账户身份后合并'});continue
        if h.get('price_source') not in (None,'quote') or h.get('mv') is None:
            missing.append(code);continue
        amounts['股票']+=float(h['mv'])
        details.append({'code':code,'value':float(h['mv']),'as_of':stock_date,'basis':'market_snapshot'})
    total=sum(amounts.values())
    return {'basis':'按各自注明日期的确认净值/市价估值，不使用成本补市值','as_of':as_of,
            'stock_as_of':stock_date,'total':round(total,2),'amounts':dict(amounts),
            'allocation':{k:v/total if total else 0 for k,v in amounts.items()},'detail':details,
            'excluded_duplicates':excluded,'unvalued':missing,
            'status':'partial' if missing or excluded or (stocks and stock_date!=as_of) or any(h.get('nav_date')!=as_of for h in funds) else 'ready'}


def combined_market_view():
    import json
    from fund.fund_service import holdings_snapshot
    snap=holdings_snapshot()
    rows=[];stock_date=None;reason=None
    try:
        from portfolio import portfolio_snapshot
        conn=portfolio_snapshot._conn()
        try:
            cur=conn.cursor()
            cur.execute('SELECT snap_date,holdings_json FROM stock_portfolio_snapshots WHERE snap_date<=? ORDER BY snap_date DESC LIMIT 1',(snap['as_of'],))
            row=cur.fetchone()
            if row:stock_date=str(row[0])[:10];rows=json.loads(row[1])
            else:reason='股票市值快照缺失'
        finally:conn.close()
    except Exception:reason='股票市值快照不可用'
    result=merge_market_values(snap['holdings'],rows,stock_date,snap['as_of'])
    if reason:result.update(status='partial',stock_reason=reason)
    return result

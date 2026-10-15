"""基金综合评价 + AI 研判。

score_fund   纯规则打分卡(业绩+风险+稳定性)0-100,长期/定投视角,离线可算。
ai_research  复用项目 LLM(llm_router)做基金 AI 研判:是否适合长期持有/定投、
             风险点、定投节奏建议。⚠️ 需配置至少一个 LLM provider(DEEPSEEK_API_KEY 等),
             耗 token、单次数秒;无 key 时返回提示文案不崩。
"""

from __future__ import annotations

from typing import Optional

import fund_data
import fund_metrics


def _clip(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _lin(x, x0, x1):
    """把 x 从 [x0,x1] 线性映射到 [0,100](支持 x0>x1 表示越小越好)。"""
    if x is None:
        return None
    if x1 == x0:
        return 50.0
    return _clip((x - x0) / (x1 - x0) * 100)


def score_fund(code, nav_df=None, rf=.02, with_extras=False, profile=None, benchmark_df=None):
    from fund.fund_contracts import audit_nav, product
    from fund.fund_returns import return_frame
    import pandas as pd
    code=str(code).zfill(6)
    frame=fund_data.get_nav_history(code) if nav_df is None else nav_df
    if frame is None or len(frame)<2: return {'code':code,'status':'partial','error':'净值不足','grade':None}
    profile=profile or product(code,fund_data.fund_name(code),fund_data.fund_type(code))
    m=fund_metrics.evaluate(frame,rf)
    quality=audit_nav(frame)
    basis=return_frame(frame).attrs['return_basis']
    category=profile['category']
    base={'code':code,'name':profile.get('name'),'ftype':profile.get('ftype'),'category':category,
          'metrics':m,'quality':quality,'score':None,'grade':None,'subscores':{},'peer_rank':None,
          'status':'partial','advice':'历史、权益事件或必要基准不足，仅展示已知区间表现。'}
    if category=='money':
        return {**base,'status':'ready' if 'income_per_10000' in frame and quality['status']=='ready' else 'partial',
                'income':{'date':str(frame.iloc[-1]['date'])[:10],'per_10000':float(frame.iloc[-1]['income_per_10000']) if 'income_per_10000' in frame else None,
                          'yield_7d':float(frame.iloc[-1]['yield_7d']) if 'yield_7d' in frame else None},
                'advice':'货币基金展示万份收益和收益结转，不套用权益评分。'}
    if not m['long_history_ready'] or quality['status']!='ready' or basis=='unadjusted_unit_nav': return base
    excess=None
    if benchmark_df is not None and audit_nav(benchmark_df)['status']=='ready' and return_frame(benchmark_df).attrs['return_basis']!='unadjusted_unit_nav':
        fund=return_frame(frame).set_index('date').total_return_nav
        bench=return_frame(benchmark_df).set_index('date').total_return_nav
        pair=pd.concat([fund,bench],axis=1,join='inner').dropna()
        if len(pair)>=200 and (pair.index[-1]-pair.index[0]).days>=365:
            active=pair.iloc[:,0].pct_change()-pair.iloc[:,1].pct_change()
            excess={'total_excess':float(pair.iloc[-1,0]/pair.iloc[0,0]-pair.iloc[-1,1]/pair.iloc[0,1]),
                    'tracking_error':float(active.std()*252**.5),
                    'rolling_win_rate':float((pair.iloc[:,0].pct_change(60).iloc[60:]>pair.iloc[:,1].pct_change(60).iloc[60:]).mean()),
                    'start':str(pair.index[0].date()),'end':str(pair.index[-1].date())}
    if category in ('index','qdii') and (excess is None or not profile.get('benchmark') or not profile.get('currency')):
        return {**base,'benchmark_metrics':excess,'advice':'指数/QDII 评价需同币种基准、跟踪数据及足够历史。'}
    limits={'bond':(-.03,.08,.15,.005,.12,.01),
            'equity':(-.1,.25,.5,.05,.4,.05),
            'index':(-.1,.20,.5,.05,.35,.05),
            'qdii':(-.1,.20,.5,.05,.4,.05),'allocation':(-.05,.15,.3,.03,.25,.03)}
    if category not in limits: return base
    a,b,c,d,e,f=limits[category]
    sub={'annualized_return':_lin(m['annualized_return'],a,b),
         'drawdown_control':_lin(m['max_drawdown'],c,d),
         'sharpe':_lin(m['sharpe'],-.5,2.),'low_volatility':_lin(m['annualized_volatility'],e,f)}
    if category=='index':
        sub={'tracking_error':_lin(excess['tracking_error'],.1,0.),
             'tracking_difference':_lin(abs(excess['total_excess']),.15,0.)}
    if any(v is None for v in sub.values()):return {**base,'subscores':sub,'advice':'必要风险收益指标不可计算，暂不评级。'}
    model_weights={'bond':[.15,.35,.25,.25],'equity':[.35,.25,.25,.15],'index':[.5,.5],
                   'qdii':[.3,.3,.2,.2],'allocation':[.25,.3,.25,.2]}
    weights=dict(zip(sub,model_weights[category]))
    score=sum(sub[k]*w for k,w in weights.items())
    curve=return_frame(frame).set_index('date').total_return_nav
    rolling={str(n): {'observations':int(curve.pct_change(n).notna().sum()),
                     'positive_rate':float((curve.pct_change(n).dropna()>0).mean()) if len(curve)>n else None}
             for n in (60,120,252)}
    peer=fund_data.get_peer_rank_percentile(code) if with_extras else None
    return {**base,'status':'ready','score':round(score,1) if score is not None else None,
            'grade':('A' if score>=75 else 'B' if score>=60 else 'C') if score is not None else None,
            'subscores':sub,'weights':weights,'scoring_version':'fund-category-v2','peer_rank':peer,'benchmark_metrics':excess,'rolling':rolling,
            'advice':'同类型历史表现参考，须结合经理任期、费用、账户约束与最新披露复核；不直接生成买入结论。',
            'limitations':['未披露的久期/信用或风格数据保持未知','历史评分不代表未来收益']}


def _build_context(code: str, nav_df=None) -> dict:
    """汇集 AI 研判所需的结构化上下文(尽量离线/少抓)。"""
    if nav_df is None:nav_df=fund_data.get_nav_history(code)
    sc = score_fund(code, nav_df)
    ctx = {
        'code': code,
        'name': fund_data.fund_name(code),
        'type': fund_data.fund_type(code),
        'quality':sc.get('quality'),
        'snapshot_id':sc.get('quality',{}).get('snapshot_id'),
        'data_basis':'当前已观察资料，公开时间未知的资料不能认定历史可用',
        'score_card': {k: sc.get(k) for k in ('score', 'grade', 'subscores', 'metrics')} if 'score' in sc else sc,
    }
    try:
        ctx['basic'] = fund_data.get_fund_basic(code)
    except Exception:
        ctx['basic'] = None
    return ctx


_AI_CACHE = {}


def ai_research(code: str, nav_df=None, temperature: float = 0.5) -> dict:
    """基金 AI 研判。返回 {code, opinion, provider, context}。
    opinion 为大模型给出的「是否适合长期/定投 + 风险点 + 定投节奏建议」文本。"""
    code = str(code).zfill(6)
    ctx = _build_context(code, nav_df)
    from fund.fund_contracts import fingerprint
    import time
    key=fingerprint({'context':ctx,'temperature':temperature,'version':'fund-ai-v2'})
    cached=_AI_CACHE.get(key)
    if cached and time.monotonic()-cached[0]<3600:return dict(cached[1])
    try:
        from llm_router import get_router
    except Exception as e:
        return {'code': code, 'opinion': f'[未接入 LLM] {e}', 'provider': 'none', 'context': ctx}

    import json
    sys_prompt = (
        "你是专注长期投资与基金定投的投顾。基于给定的基金客观指标与基本信息,"
        "给出简明研判:① 是否适合长期持有/定投(给出理由)② 主要风险点 ③ 定投节奏建议"
        "(普通定投/估值定投/暂停的判断,以及止盈思路)。务必基于数据、克制,不做收益承诺,"
        "不足之处直说数据不够。中文,300字内,分点输出。"
    )
    user_prompt = "基金研判上下文(JSON):\n" + json.dumps(ctx, ensure_ascii=False, default=str)
    try:
        text, provider = get_router().call(
            [{'role': 'system', 'content': sys_prompt},
             {'role': 'user', 'content': user_prompt}],
            temperature=temperature, max_tokens=900)
    except Exception as e:
        text, provider = f'[LLM 调用失败] {type(e).__name__}: {e}', 'none'
    result={'code': code, 'opinion': text, 'provider': provider, 'context': ctx, 'context_hash':key}
    if provider!='none':_AI_CACHE[key]=(time.monotonic(),result)
    return result


def compare_funds(codes, lookback_days: Optional[int] = None, rf: float = 0.02,
                  curve_points: int = 150) -> dict:
    """多只基金并排对比(同一**共同时间窗**口径,公平可比)。

    取各基金净值的交集区间 [max(起), min(止)](可再用 lookback_days 截近段),
    各自归一到 1.0,算窗口内 年化/总收益/最大回撤/夏普/卡玛/年化波动,并给归一净值叠加曲线。
    返回 {common_start, common_end, n_days, funds:[{code,name,metrics,curve}], skipped}。
    """
    import pandas as pd
    from concurrent.futures import ThreadPoolExecutor
    navs, names, skipped = {}, {}, []
    uniq = list(dict.fromkeys(str(c).zfill(6) for c in codes))   # 去重保序

    def _fetch(c):
        df = fund_data.get_nav_history(c)
        if df is None or len(df) < 30 or 'date' not in df.columns:
            return c, None, None
        from fund.fund_returns import return_frame
        from fund.fund_contracts import audit_nav
        returns = return_frame(df)
        if audit_nav(df)['status']!='ready' or returns.attrs['return_basis']=='unadjusted_unit_nav':
            return c,None,None
        s = returns.set_index('date')['total_return_nav']
        s.index = pd.to_datetime(s.index)
        s = s.sort_index()
        if len(s) < 2:
            return c, None, None
        return c, s, (fund_data.fund_name(c) or c)

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(uniq)))) as ex:
        for c, s, nm in ex.map(_fetch, uniq):
            if s is None:
                skipped.append(c)
            else:
                navs[c], names[c] = s, nm
    if not navs:
        return {'error': '无有效基金净值', 'funds': [], 'skipped': skipped}

    common_start = max(s.index[0] for s in navs.values())
    common_end = min(s.index[-1] for s in navs.values())
    if lookback_days:
        common_start = max(common_start, common_end - pd.Timedelta(days=int(lookback_days)))
    if common_start >= common_end:
        return {'error': '各基金无重叠时间区间(成立时间差异过大)', 'funds': [], 'skipped': skipped}

    common_dates = None
    for s in navs.values():
        dates=s.index[(s.index>=common_start)&(s.index<=common_end)]
        common_dates=dates if common_dates is None else common_dates.intersection(dates)
    if len(common_dates)<2: return {'error':'无足够共同观测日期','funds':[],'skipped':skipped}
    funds = []
    for c, s in navs.items():
        w = s.reindex(common_dates)
        if len(w) < 2 or w.iloc[0] <= 0:
            skipped.append(c)
            continue
        norm = w / w.iloc[0]
        # 降采样曲线(叠加图传输用)
        if curve_points and len(norm) > curve_points:
            step = len(norm) / curve_points
            idxs = sorted(set([int(i * step) for i in range(curve_points)] + [len(norm) - 1]))
            pts = [(norm.index[i], norm.iloc[i]) for i in idxs]
        else:
            pts = list(norm.items())
        funds.append({
            'code': c, 'name': names[c],
            'metrics': fund_metrics.evaluate(w, rf),
            'curve': [{'date': d.strftime('%Y-%m-%d'), 'nav': round(float(v), 4)} for d, v in pts],
        })
    # 按窗口总收益降序(最能赚的排前)
    funds.sort(key=lambda f: f['metrics'].get('total_return') if f['metrics'].get('total_return') is not None else float('-inf'), reverse=True)
    return {
        'common_start': common_start.strftime('%Y-%m-%d'),
        'common_end': common_end.strftime('%Y-%m-%d'),
        'n_days': int((common_end - common_start).days),
        'funds': funds, 'skipped': skipped, 'common_observations':len(common_dates),
        'comparison_basis':'共同日期总回报；跨类型仅供观察，不构成同类排名',
    }


_PANEL_ROLES = {
    '业绩评估': "你是基金业绩分析师。只评估收益质量:年化/累计、同类排名分位、收益的可持续性。",
    '风险评估': "你是基金风险分析师。只评估风险:最大回撤、波动、夏普/卡玛、下行风险、极端情形承受度。",
    '定投适配': "你是定投顾问。只评估该基金作为**长期定投标的**是否合适:波动是否利于摊薄成本、"
                "是否适合估值/普通定投、止盈思路与节奏建议。",
}


def ai_research_panel(code: str, nav_df=None, temperature: float = 0.4) -> dict:
    """多角色 AI 研判面板(复用 llm_router):业绩/风险/定投适配 三个角色各出一段,
    再综合成结论。返回 {code, roles:{角色:意见}, synthesis, provider, context}。需 LLM key。"""
    import json
    code = str(code).zfill(6)
    ctx = _build_context(code, nav_df)
    try:
        from llm_router import get_router
    except Exception as e:
        return {'code': code, 'roles': {}, 'synthesis': f'[未接入 LLM] {e}', 'provider': 'none', 'context': ctx}
    router = get_router()
    ctx_text = json.dumps(ctx, ensure_ascii=False, default=str)
    roles_out, provider = {}, 'none'
    for role, sys_p in _PANEL_ROLES.items():
        try:
            txt, provider = router.call(
                [{'role': 'system', 'content': sys_p + ' 基于数据、150字内、分点、克制不承诺收益。'},
                 {'role': 'user', 'content': '基金上下文(JSON):\n' + ctx_text}],
                temperature=temperature, max_tokens=500)
        except Exception as e:
            txt = f'[{role} 失败] {type(e).__name__}'
        roles_out[role] = txt
    # 综合
    try:
        merged = '\n\n'.join(f'【{k}】{v}' for k, v in roles_out.items())
        synthesis, provider = router.call(
            [{'role': 'system', 'content': '你是基金投委会主持人。综合以下三位分析师意见,给出'
              '最终结论:是否适合长期定投(适合/谨慎/不适合)+ 一句话理由 + 定投节奏建议。200字内。'},
             {'role': 'user', 'content': merged}],
            temperature=temperature, max_tokens=600)
    except Exception as e:
        synthesis = f'[综合失败] {type(e).__name__}'
    return {'code': code, 'roles': roles_out, 'synthesis': synthesis, 'provider': provider, 'context': ctx}


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    import numpy as np
    import pandas as pd
    idx = pd.date_range('2021-01-01', periods=500, freq='D')
    rng = np.random.default_rng(1)
    nav = pd.DataFrame({'date': idx, 'unit_nav': (1 + rng.normal(0.0005, 0.011, 500)).cumprod()})
    from pprint import pprint
    pprint(score_fund('000001', nav))

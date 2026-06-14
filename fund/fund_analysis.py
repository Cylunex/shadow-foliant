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


def score_fund(code: str, nav_df=None, rf: float = 0.02, with_extras: bool = False) -> dict:
    """长期/定投视角综合打分(0-100)+ 等级 + 一句话建议。
    基础 5 维:年化收益(35%)+ 回撤控制(25%)+ 夏普(20%)+ 卡玛(10%)+ 低波动(10%)。
    with_extras=True 时额外接入 **同类排名分位**(雪球,需联网,慢)并重新归一化权重;
    取不到该维则自动忽略(借鉴 eastmoney 5 维诊断思路,经理任期维待稳定字段映射后再补)。"""
    code = str(code).zfill(6)
    if nav_df is None:
        nav_df = fund_data.get_nav_history(code)
    if nav_df is None or len(nav_df) < 30:
        return {'code': code, 'error': '净值数据不足,无法评分', 'n_points': 0 if nav_df is None else len(nav_df)}

    m = fund_metrics.evaluate(nav_df, rf)
    sub = {
        'annualized_return': _lin(m['annualized_return'], -0.10, 0.25),   # -10%→0,25%→100
        'drawdown_control': _lin(m['max_drawdown'], 0.50, 0.05),          # 回撤越小越好
        'sharpe': _lin(m['sharpe'], -0.5, 2.0),
        'calmar': _lin(m['calmar'], 0.0, 3.0),
        'low_volatility': _lin(m['annualized_volatility'], 0.40, 0.05),   # 波动越小越好
    }
    weights = {'annualized_return': 0.35, 'drawdown_control': 0.25,
               'sharpe': 0.20, 'calmar': 0.10, 'low_volatility': 0.10}
    peer = None
    if with_extras:
        peer = fund_data.get_peer_rank_percentile(code)  # {period,rank,total,percentile}
        if peer:
            sub['peer_rank'] = float(peer['percentile'])  # 已是 0-100
            weights['peer_rank'] = 0.15                    # 占 15%,其余按比例缩放
    valid = {k: v for k, v in sub.items() if v is not None}
    if not valid:
        return {'code': code, 'error': '指标不可计算'}
    wsum = sum(weights[k] for k in valid)
    total = sum(valid[k] * weights[k] for k in valid) / wsum

    grade = ('A+' if total >= 85 else 'A' if total >= 75 else 'B' if total >= 60
             else 'C' if total >= 45 else 'D')
    advice = {
        'A+': '业绩与风控俱佳,适合作为定投核心标的长期持有。',
        'A': '综合优秀,可纳入定投组合。',
        'B': '中规中矩,适合搭配,关注回撤与稳定性。',
        'C': '一般,定投需谨慎,建议结合估值择时。',
        'D': '风险收益不佳,不建议作为定投主力。',
    }[grade]

    return {
        'code': code, 'name': fund_data.fund_name(code), 'ftype': fund_data.fund_type(code),
        'score': round(total, 1), 'grade': grade, 'advice': advice,
        'subscores': {k: round(v, 1) for k, v in sub.items() if v is not None},
        'peer_rank': peer,
        'metrics': m,
    }


def _build_context(code: str, nav_df=None) -> dict:
    """汇集 AI 研判所需的结构化上下文(尽量离线/少抓)。"""
    sc = score_fund(code, nav_df)
    ctx = {
        'code': code,
        'name': fund_data.fund_name(code),
        'type': fund_data.fund_type(code),
        'score_card': {k: sc.get(k) for k in ('score', 'grade', 'subscores', 'metrics')} if 'score' in sc else sc,
    }
    try:
        ctx['basic'] = fund_data.get_fund_basic(code)
    except Exception:
        ctx['basic'] = None
    return ctx


def ai_research(code: str, nav_df=None, temperature: float = 0.5) -> dict:
    """基金 AI 研判。返回 {code, opinion, provider, context}。
    opinion 为大模型给出的「是否适合长期/定投 + 风险点 + 定投节奏建议」文本。"""
    code = str(code).zfill(6)
    ctx = _build_context(code, nav_df)
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
    return {'code': code, 'opinion': text, 'provider': provider, 'context': ctx}


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
        s = pd.to_numeric(df.set_index('date')['unit_nav'], errors='coerce').dropna()
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

    funds = []
    for c, s in navs.items():
        w = s[(s.index >= common_start) & (s.index <= common_end)]
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
    funds.sort(key=lambda f: (f['metrics'].get('total_return') or -1), reverse=True)
    return {
        'common_start': common_start.strftime('%Y-%m-%d'),
        'common_end': common_end.strftime('%Y-%m-%d'),
        'n_days': int((common_end - common_start).days),
        'funds': funds, 'skipped': skipped,
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

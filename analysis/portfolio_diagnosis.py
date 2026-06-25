"""
组合诊断 —— 借鉴 SkillHub「投资组合诊断 / 资产配置与组合优化」skill。

对现有持仓做体检:集中度(HHI/单票/前三)、行业集中度、(可选)相关性与组合波动,
给出风险提示与再平衡建议。补项目「持仓只有逐票分析、无组合层面风险」的空白。

纯计算,零依赖。可吃 portfolio 的持仓列表(权重或市值)。
"""

from __future__ import annotations
from typing import Dict, List, Optional, Any
import math


def _weights(holdings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """归一化为权重(支持传 weight 或 market_value)。"""
    if not holdings:
        return []
    if all('weight' in h for h in holdings):
        tot = sum(h['weight'] for h in holdings) or 1.0
        return [{**h, 'w': h['weight'] / tot} for h in holdings]
    tot = sum(float(h.get('market_value', 0)) for h in holdings) or 1.0
    return [{**h, 'w': float(h.get('market_value', 0)) / tot} for h in holdings]


def concentration(holdings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """集中度:HHI、单票最大、前三占比、有效持仓数。"""
    hs = _weights(holdings)
    if not hs:
        return {}
    ws = sorted((h['w'] for h in hs), reverse=True)
    hhi = sum(w * w for w in ws)
    return {
        'n_holdings': len(hs),
        'hhi': round(hhi, 4),
        'effective_n': round(1 / hhi, 1) if hhi else None,  # 有效分散度
        'top1': round(ws[0], 3),
        'top3': round(sum(ws[:3]), 3),
        'level': ('高度集中' if hhi > 0.25 else '偏集中' if hhi > 0.15 else '较分散'),
    }


def sector_concentration(holdings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """行业集中度(需持仓含 sector 字段)。"""
    hs = _weights(holdings)
    sec: Dict[str, float] = {}
    for h in hs:
        s = h.get('sector')
        if s:
            sec[s] = sec.get(s, 0) + h['w']
    if not sec:
        return {}
    top = sorted(sec.items(), key=lambda x: -x[1])
    return {
        'by_sector': {k: round(v, 3) for k, v in top},
        'top_sector': top[0][0],
        'top_sector_weight': round(top[0][1], 3),
        'warning': ('单一行业占比>50%,行业风险集中' if top[0][1] > 0.5 else None),
    }


def portfolio_volatility(holdings: List[Dict[str, Any]],
                         cov: Optional[Dict] = None) -> Optional[float]:
    """若提供个股年化波动与相关阵,估算组合年化波动(可选)。
    holdings 每项需含 'vol';cov 为 {(a,b): corr}。缺失返回 None。
    """
    hs = _weights(holdings)
    if not all('vol' in h for h in hs):
        return None
    var = 0.0
    for i, a in enumerate(hs):
        for j, b in enumerate(hs):
            corr = 1.0 if i == j else (cov or {}).get((a['symbol'], b['symbol']),
                                                       (cov or {}).get((b['symbol'], a['symbol']), 0.3))
            var += a['w'] * b['w'] * a['vol'] * b['vol'] * corr
    return round(math.sqrt(var), 4)


def diagnose_portfolio(holdings: List[Dict[str, Any]], cov: Optional[Dict] = None) -> Dict[str, Any]:
    """组合体检主入口。"""
    if not holdings:
        return {'available': False, 'error': '无持仓数据'}
    conc = concentration(holdings)
    sec = sector_concentration(holdings)
    vol = portfolio_volatility(holdings, cov)
    suggestions = []
    if conc.get('top1', 0) > 0.3:
        suggestions.append(f"单票最大权重 {conc['top1']*100:.0f}%>30%,建议适度减配以控单一标的风险")
    if conc.get('level') == '高度集中':
        suggestions.append(f"HHI={conc['hhi']}(高度集中),有效持仓仅 {conc.get('effective_n')} 只,建议增加分散")
    if sec.get('warning'):
        suggestions.append(sec['warning'] + ',建议跨行业再平衡')
    # 过度分散提示(原诊断只对"集中"告警,对持仓过多/有效持仓低的"过度分散"是盲区):
    # 持仓 >25 只 或 有效持仓数明显低于实际只数(大量零碎尾仓)→ 提示瘦身。
    _n = conc.get('n_holdings', 0)
    _eff = conc.get('effective_n')
    if _n > 25:
        _msg = f"持仓 {_n} 只过度分散(精力/跟踪/成本都摊薄),建议用清仓助手瘦身到 ~20 只核心"
        if _eff and _eff < _n * 0.5:
            _msg += f";且有效持仓仅 {_eff} 只(大量零碎尾仓拖后腿,可优先清掉)"
        suggestions.append(_msg)
    if not suggestions:
        suggestions.append('集中度与行业分布在合理区间')
    out = {'available': True, 'concentration': conc, 'sector': sec,
           'portfolio_vol': vol, 'suggestions': suggestions}
    out['summary'] = _summary(out)
    return out


def _summary(r: Dict[str, Any]) -> str:
    c = r.get('concentration', {})
    parts = [f"【组合诊断】{c.get('n_holdings')}只持仓,集中度{c.get('level')}"
             f"(HHI={c.get('hhi')},单票最大{(c.get('top1') or 0)*100:.0f}%,前三{(c.get('top3') or 0)*100:.0f}%)。"]
    sec = r.get('sector', {})
    if sec.get('top_sector'):
        parts.append(f"行业最重:{sec['top_sector']} {sec['top_sector_weight']*100:.0f}%。")
    if r.get('portfolio_vol') is not None:
        parts.append(f"组合年化波动≈{r['portfolio_vol']*100:.1f}%。")
    parts.append('建议:' + '；'.join(r.get('suggestions', [])) + '。')
    return ' '.join(parts)


if __name__ == '__main__':
    import io, sys, json
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    demo = [
        {'symbol': '600519', 'market_value': 50, 'sector': '白酒'},
        {'symbol': '300750', 'market_value': 25, 'sector': '新能源'},
        {'symbol': '600036', 'market_value': 15, 'sector': '银行'},
        {'symbol': '000001', 'market_value': 10, 'sector': '银行'},
    ]
    r = diagnose_portfolio(demo)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    print('\nSUMMARY:', r['summary'])

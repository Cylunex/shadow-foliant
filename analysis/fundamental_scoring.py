"""基本面 8 因子加权打分

借鉴 InStock 的基本面规则（PE≤20+PB≤10+ROE≥15%）+ a-stock-data 的 PEG/前向PE 框架，
扩展为 8 因子加权打分系统：给任意股票打 0-100 分并分级（A/B/C/D）。

因子（默认权重）：
  1. forward_pe (15%) — 前向 PE（越低越好）
  2. peg        (15%) — PEG（越低越好，<1 满分）
  3. pb         (10%) — 市净率
  4. roe        (20%) — 净资产收益率（越高越好）
  5. net_profit_growth (15%) — 净利同比增速
  6. debt_ratio (10%) — 资产负债率（越低越好）
  7. dividend_yield (10%) — 股息率（越高越好）
  8. ocf_ratio   (5%)  — 经营现金流 / 净利润（>1 满分）

数据来源：
  - 主：pywencai（一次性拉所有因子）
  - 备：full_valuation（前向PE/PEG）+ 同花顺 EPS

接口：
  score_one(symbol) -> {symbol, score, grade, factors: {fid: {raw, sub_score, weight}}, action}
"""

from typing import Dict, Any, Optional, List

DEFAULT_WEIGHTS = {
    'forward_pe':         0.15,
    'peg':                0.15,
    'pb':                 0.10,
    'roe':                0.20,
    'net_profit_growth':  0.15,
    'debt_ratio':         0.10,
    'dividend_yield':     0.10,
    'ocf_ratio':          0.05,
}


def _piecewise(x: Optional[float], breakpoints: List[tuple]) -> float:
    """分段评分：breakpoints = [(threshold, score), ...] 按 x 升序

    例：[(5, 100), (10, 90), (20, 75), (30, 50), (50, 25), (1e9, 10)]
    x<=5 给 100；5<x<=10 给 90；...；x>50 给 10。
    """
    if x is None:
        return 0.0
    try:
        x = float(x)
    except (ValueError, TypeError):
        return 0.0
    for thr, sc in breakpoints:
        if x <= thr:
            return sc
    return breakpoints[-1][1]


def _piecewise_desc(x: Optional[float], breakpoints: List[tuple]) -> float:
    """分段评分（高值优先）：breakpoints = [(threshold, score), ...] 按 x 降序

    例：[(20, 100), (15, 90), (10, 75), (5, 50), (0, 25), (-1e9, 0)]
    x>=20 给 100；15<=x<20 给 90；...；x<0 给 0。
    """
    if x is None:
        return 0.0
    try:
        x = float(x)
    except (ValueError, TypeError):
        return 0.0
    for thr, sc in breakpoints:
        if x >= thr:
            return sc
    return breakpoints[-1][1]


def _score_forward_pe(pe: Optional[float]) -> float:
    return _piecewise(pe, [(15, 100), (25, 85), (40, 65), (60, 40), (100, 20), (1e9, 5)])


def _score_peg(peg: Optional[float]) -> float:
    return _piecewise(peg, [(0.8, 100), (1.0, 90), (1.5, 70), (2.0, 50), (3.0, 25), (1e9, 5)])


def _score_pb(pb: Optional[float]) -> float:
    return _piecewise(pb, [(1.0, 100), (2.0, 85), (3.5, 65), (5.0, 40), (8.0, 20), (1e9, 5)])


def _score_roe(roe: Optional[float]) -> float:
    return _piecewise_desc(roe, [(20, 100), (15, 88), (10, 70), (5, 45), (0, 20), (-1e9, 0)])


def _score_net_profit_growth(g: Optional[float]) -> float:
    return _piecewise_desc(g, [(50, 100), (30, 88), (15, 72), (5, 55), (0, 35), (-20, 15), (-1e9, 0)])


def _score_debt_ratio(d: Optional[float]) -> float:
    return _piecewise(d, [(30, 100), (50, 80), (65, 55), (80, 30), (95, 10), (1e9, 0)])


def _score_dividend_yield(y: Optional[float]) -> float:
    return _piecewise_desc(y, [(5, 100), (3, 85), (2, 70), (1, 50), (0.1, 20), (-1e9, 0)])


def _score_ocf_ratio(r: Optional[float]) -> float:
    return _piecewise_desc(r, [(1.2, 100), (1.0, 85), (0.8, 65), (0.5, 40), (0, 20), (-1e9, 0)])


SUB_SCORERS = {
    'forward_pe':        _score_forward_pe,
    'peg':               _score_peg,
    'pb':                _score_pb,
    'roe':               _score_roe,
    'net_profit_growth': _score_net_profit_growth,
    'debt_ratio':        _score_debt_ratio,
    'dividend_yield':    _score_dividend_yield,
    'ocf_ratio':         _score_ocf_ratio,
}


def _grade(score: float) -> str:
    if score >= 80:
        return 'A (优质)'
    if score >= 65:
        return 'B (良好)'
    if score >= 50:
        return 'C (中性)'
    if score >= 35:
        return 'D (偏弱)'
    return 'E (回避)'


def _action(score: float) -> str:
    if score >= 80:
        return '基本面优质 — 中长线可重点关注'
    if score >= 65:
        return '基本面良好 — 可纳入观察池'
    if score >= 50:
        return '基本面中性 — 看其他维度决策'
    if score >= 35:
        return '基本面偏弱 — 建议规避或减仓'
    return '基本面较差 — 不建议持有'


def collect_factors(symbol: str, use_cache: bool = True) -> Dict[str, Optional[float]]:
    """聚合 8 因子原始值，优先 a-stock full_valuation + 同花顺 EPS，备 pywencai

    返回的 dict 即使部分字段缺也返回，缺字段值为 None。
    ⭐ 整体缓存 1 天(2026-06-25):基本面/一致预期/财务指标几天才变,但每只内部调 full_valuation
    (同花顺,慢)+ pywencai(问财,限流2s+30s超时,**不受 datahub 全源熔断保护**)→ 盘中 60 只逐只现调
    是取因子"累计156s"+池耗尽雪崩的真主因。盘后 kline_prefetch 焐热,盘中读缓存 0 调慢源。
    """
    if use_cache:
        try:
            import datahub
            _c = datahub._cache_get(f"factors:{symbol}", 86400)
            if isinstance(_c, dict):
                return _c
        except Exception:
            pass
    factors: Dict[str, Optional[float]] = {k: None for k in SUB_SCORERS}

    try:
        import datahub
        fv = datahub.full_valuation(symbol)
        if fv:
            factors['forward_pe'] = fv.get('pe_fwd')
            factors['peg'] = fv.get('peg')
            factors['pb'] = fv.get('pb')
    except Exception:
        pass

    try:
        from data.pywencai_safe import pywencai_get
        query = (
            f"{symbol} 市盈率(动态) 市净率 净资产收益率 "
            f"净利润同比增长率 资产负债率 股息率 经营活动产生的现金流量净额 净利润"
        )
        df = pywencai_get(query, timeout=30, loop=False)
        if df is not None and hasattr(df, 'empty') and not df.empty:
            row = df.iloc[0]
            def _g(*keys):
                for k in keys:
                    if k in row.index:
                        v = row.get(k)
                        if v not in (None, '', '--'):
                            try:
                                return float(str(v).replace('%', '').replace(',', ''))
                            except (ValueError, TypeError):
                                continue
                return None
            if factors['forward_pe'] is None:
                factors['forward_pe'] = _g('市盈率(动态)', '市盈率')
            if factors['pb'] is None:
                factors['pb'] = _g('市净率')
            factors['roe'] = _g('净资产收益率', '净资产收益率(%)')
            factors['net_profit_growth'] = _g('净利润同比增长率', '净利润同比增长率(%)')
            factors['debt_ratio'] = _g('资产负债率', '资产负债率(%)')
            factors['dividend_yield'] = _g('股息率', '股息率(%)')
            ocf = _g('经营活动产生的现金流量净额')
            net_profit = _g('净利润')
            if ocf is not None and net_profit not in (None, 0):
                factors['ocf_ratio'] = ocf / net_profit
    except Exception as e:
        pass

    # 至少有一个因子非空才缓存(全空多是慢源/问财一起挂,别把"空"缓存1天)
    if use_cache and any(v is not None for v in factors.values()):
        try:
            import datahub
            datahub._cache_put(f"factors:{symbol}", factors, 86400)
        except Exception:
            pass
    return factors


def score_one(symbol: str, weights: Optional[Dict[str, float]] = None,
              factors: Optional[Dict[str, Optional[float]]] = None) -> Dict[str, Any]:
    """对单只股票做 8 因子加权打分

    Args:
        symbol: 股票代码
        weights: 自定义权重，None 用 DEFAULT_WEIGHTS
        factors: 直接传入因子（跳过采集，用于回测/批量）
    """
    w = weights or DEFAULT_WEIGHTS
    fac = factors if factors is not None else collect_factors(symbol)

    detail: Dict[str, Dict[str, Any]] = {}
    weight_sum_covered = 0.0
    score_sum = 0.0
    for fid, weight in w.items():
        raw = fac.get(fid)
        scorer = SUB_SCORERS.get(fid)
        if scorer is None:
            continue
        sub = scorer(raw)
        detail[fid] = {'raw': raw, 'sub_score': round(sub, 1), 'weight': weight}
        if raw is not None:
            score_sum += sub * weight
            weight_sum_covered += weight

    coverage = weight_sum_covered  # 0~1
    final_score = score_sum / weight_sum_covered if weight_sum_covered > 0 else 0.0
    final_score = round(final_score, 1)

    # 低覆盖警告：缺失关键因子时分数不可信
    low_coverage = coverage < 0.5
    grade = _grade(final_score) if not low_coverage else 'N/A (因子覆盖不足)'
    action = _action(final_score) if not low_coverage else f'因子覆盖率仅 {coverage*100:.0f}% — 数据不足，建议看其他维度'

    return {
        'symbol': symbol,
        'score': final_score if not low_coverage else None,
        'grade': grade,
        'coverage': round(coverage, 2),
        'low_coverage': low_coverage,
        'action': action,
        'factors': detail,
    }


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== 基本面 8 因子打分自检 ===')
    for code in ['600519', '000858', '000001']:
        r = score_one(code)
        print(f"\n{code}: 分数={r['score']} 等级={r['grade']} 覆盖={r['coverage']}")
        print(f"  行动: {r['action']}")
        for fid, d in r['factors'].items():
            raw = d['raw']
            raw_s = f"{raw:.2f}" if isinstance(raw, (int, float)) else 'N/A'
            print(f"    {fid:20s}: 原值={raw_s:10s} 子分={d['sub_score']:5.1f} 权重={d['weight']:.2f}")

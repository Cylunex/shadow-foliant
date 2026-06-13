"""情景化压力测试 —— 命名宏观情景 + 因子敏感度(借鉴 eastmoney stress_test 思路,自研实现)。

区别于 stress_testing.py(VaR/蒙特卡洛,基于历史分布):本模块是**前瞻情景**——
给定"加息/贬值/指数暴跌/行业冲击"等命名情景,按每个持仓的 β(对大盘)、行业暴露、汇率暴露
估算冲击,汇总组合损益。纯函数、离线、股票与基金组合通用。

持仓 position: {code, name, market_value, [beta], [sector], [asset_type], [fx_exposure]}
  asset_type: 'stock'/'fund_equity'/'fund_bond'/'cash'/'qdii'(决定默认 β/汇率暴露)
"""

from __future__ import annotations

from typing import List, Dict, Optional

# 命名情景:已把宏观冲击折算成"大盘收益冲击 index_shock"(+ 可选行业/汇率冲击)
SCENARIOS: Dict[str, dict] = {
    'rate_hike_50bp':       {'name': '加息50bp', 'index_shock': -0.020},
    'rate_cut_50bp':        {'name': '降息50bp', 'index_shock': +0.015},
    'cny_depreciation_2pct': {'name': '人民币贬值2%', 'index_shock': -0.010, 'fx_shock': -0.020},
    'index_crash_10pct':    {'name': '大盘暴跌10%', 'index_shock': -0.100},
    'index_rally_5pct':     {'name': '大盘大涨5%', 'index_shock': +0.050},
    'liquidity_crisis':     {'name': '流动性危机', 'index_shock': -0.070,
                             'sector_shocks': {'券商': -0.10, '证券': -0.10, '地产': -0.08, '银行': -0.04}},
    'semiconductor_crash':  {'name': '半导体重挫', 'index_shock': -0.020,
                             'sector_shocks': {'半导体': -0.12, '芯片': -0.12, '电子': -0.06}},
    'consumer_boom':        {'name': '消费复苏', 'index_shock': +0.010,
                             'sector_shocks': {'白酒': +0.08, '食品': +0.05, '消费': +0.05}},
}

# 大类资产默认 β(对大盘) 与 汇率暴露
_DEFAULT_BETA = {
    'stock': 1.0, 'fund_equity': 0.85, 'qdii': 0.6,
    'fund_bond': 0.10, 'cash': 0.0, 'fund': 0.85,
}
_DEFAULT_FX = {'qdii': 1.0}  # QDII 对汇率敏感;其余默认 0


def _beta(pos: dict) -> float:
    if pos.get('beta') is not None:
        return float(pos['beta'])
    return _DEFAULT_BETA.get(pos.get('asset_type', 'stock'), 1.0)


def _fx_exposure(pos: dict) -> float:
    if pos.get('fx_exposure') is not None:
        return float(pos['fx_exposure'])
    return _DEFAULT_FX.get(pos.get('asset_type', ''), 0.0)


def _sector_shock(pos: dict, sector_shocks: dict) -> float:
    sec = pos.get('sector') or ''
    for k, v in (sector_shocks or {}).items():
        if k in sec:
            return v
    return 0.0


def stress_one(pos: dict, scenario: dict) -> dict:
    """单持仓在某情景下的冲击。返回 {code, impact_pct, pnl}。"""
    mv = float(pos.get('market_value') or 0)
    impact = _beta(pos) * scenario.get('index_shock', 0.0)
    impact += _sector_shock(pos, scenario.get('sector_shocks'))
    impact += _fx_exposure(pos) * scenario.get('fx_shock', 0.0)
    return {'code': pos.get('code'), 'name': pos.get('name'),
            'impact_pct': round(impact, 4), 'pnl': round(mv * impact, 2)}


def stress_test(positions: List[Dict], scenario_key: str) -> dict:
    """组合在指定命名情景下的压力测试。
    返回 {scenario, total_mv, total_pnl, total_pnl_pct, worst[], positions[]}。"""
    if scenario_key not in SCENARIOS:
        return {'error': f'未知情景 {scenario_key}', 'available': list(SCENARIOS)}
    sc = SCENARIOS[scenario_key]
    rows = [stress_one(p, sc) for p in positions]
    total_mv = sum(float(p.get('market_value') or 0) for p in positions)
    total_pnl = sum(r['pnl'] for r in rows)
    worst = sorted(rows, key=lambda x: x['pnl'])[:5]
    return {
        'scenario': sc['name'], 'scenario_key': scenario_key,
        'total_mv': round(total_mv, 2), 'total_pnl': round(total_pnl, 2),
        'total_pnl_pct': round(total_pnl / total_mv, 4) if total_mv else None,
        'worst': worst, 'positions': rows,
    }


def stress_all(positions: List[Dict]) -> List[dict]:
    """跑全部命名情景,按组合损益从坏到好排序(快速体检)。"""
    out = [stress_test(positions, k) for k in SCENARIOS]
    return sorted(out, key=lambda x: (x.get('total_pnl_pct') if x.get('total_pnl_pct') is not None else 0))


def build_portfolio_positions(include_funds: bool = True) -> List[Dict]:
    """从股票持仓(portfolio_db)+ 基金持仓(fund_db)构建压力测试持仓(best-effort)。
    股票市值用实时报价×数量(取不到退成本),基金市值用最新净值×份额。任一侧异常不影响另一侧。"""
    positions: List[Dict] = []
    # 股票侧
    try:
        from portfolio_db import portfolio_db
        stocks = portfolio_db.get_all_stocks()
        codes = [s.get('code') for s in stocks if s.get('code')]
        quotes = {}
        try:
            import datahub
            quotes = datahub.quotes(codes) if codes else {}
        except Exception:
            quotes = {}
        for s in stocks:
            code = str(s.get('code'))
            qty = float(s.get('quantity') or s.get('shares') or 0)
            price = (quotes.get(code) or {}).get('price') or s.get('cost_price') or s.get('cost') or 0
            positions.append({
                'code': code, 'name': s.get('name'),
                'market_value': qty * float(price),
                'sector': s.get('industry') or s.get('sector'),
                'asset_type': 'stock',
            })
    except Exception as e:
        print(f'[scenario_stress] 股票持仓读取失败: {type(e).__name__}')
    # 基金侧
    if include_funds:
        try:
            import fund_db
            fund_db.init_db()
            holdings = fund_db.get_holdings()
            # ⚡ 净值批量读库(不再逐只联网 latest_nav);类型由名称推断(免逐只 fund_type 联网)。
            # 旧实现对 N 只基金做 2N 次串行网络请求,N=53 时 ~100s。
            db_navs = fund_db.get_latest_navs([h['code'] for h in holdings]) if holdings else {}
            for h in holdings:
                code = str(h['code'])
                nav = (db_navs.get(code) or {}).get('unit_nav') or h.get('cost_nav') or 0
                name = h.get('name') or ''
                atype = ('qdii' if 'QDII' in name or 'qdii' in name
                         else 'fund_bond' if ('债' in name or '货币' in name)
                         else 'fund_equity')
                positions.append({
                    'code': code, 'name': name,
                    'market_value': float(h.get('shares') or 0) * float(nav),
                    'asset_type': atype,
                })
        except Exception as e:
            print(f'[scenario_stress] 基金持仓读取失败: {type(e).__name__}')
    return positions


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    demo = [
        {'code': '600519', 'name': '贵州茅台', 'market_value': 50000, 'sector': '白酒', 'asset_type': 'stock'},
        {'code': '000001', 'name': '平安银行', 'market_value': 30000, 'sector': '银行', 'asset_type': 'stock'},
        {'code': '110011', 'name': '易方达QDII', 'market_value': 20000, 'asset_type': 'qdii'},
    ]
    for r in stress_all(demo)[:4]:
        print(f"{r['scenario']:10s} 组合损益 {r['total_pnl_pct']:+.2%} ({r['total_pnl']:+.0f})")

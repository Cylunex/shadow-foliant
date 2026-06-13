"""
DCF / DDM 估值 —— 借鉴 SkillHub「估值模型方法论 / 现金流折现估值模型」skill。

两阶段自由现金流折现(高速增长 N 年 + 永续增长),输出每股内在价值与相对现价的安全边际,
并给出对 折现率×永续增速 的敏感性表。补项目「只有静态 PE/PB」的估值短板。

纯计算,零依赖。FCF 缺失时可用 净利润 作近似(注明)。
"""

from __future__ import annotations
from typing import Dict, List, Optional, Any


def two_stage_dcf(base_fcf: float, high_growth: float, high_years: int,
                  terminal_growth: float, discount_rate: float,
                  shares: float, net_debt: float = 0.0,
                  current_price: Optional[float] = None) -> Dict[str, Any]:
    """两阶段 DCF。

    Args:
        base_fcf: 最近一年自由现金流(或净利润近似),单位元。
        high_growth: 高速期年增速(如 0.15)。
        high_years: 高速期年数。
        terminal_growth: 永续增速(如 0.03,须 < discount_rate)。
        discount_rate: 折现率/WACC(如 0.10)。
        shares: 总股本(股)。
        net_debt: 净负债(有息负债-现金),股权价值=企业价值-净负债。
        current_price: 现价(算安全边际)。
    """
    if discount_rate <= terminal_growth:
        return {'error': '折现率必须大于永续增速'}
    pv_explicit = 0.0
    fcf = base_fcf
    flows = []
    for t in range(1, high_years + 1):
        fcf = fcf * (1 + high_growth)
        pv = fcf / ((1 + discount_rate) ** t)
        pv_explicit += pv
        flows.append({'year': t, 'fcf': round(fcf, 2), 'pv': round(pv, 2)})
    # 永续价值(Gordon)
    terminal_fcf = fcf * (1 + terminal_growth)
    terminal_value = terminal_fcf / (discount_rate - terminal_growth)
    pv_terminal = terminal_value / ((1 + discount_rate) ** high_years)

    enterprise_value = pv_explicit + pv_terminal
    equity_value = enterprise_value - net_debt
    per_share = equity_value / shares if shares else None

    out: Dict[str, Any] = {
        'pv_explicit': round(pv_explicit, 2),
        'pv_terminal': round(pv_terminal, 2),
        'terminal_pct': round(pv_terminal / enterprise_value, 3) if enterprise_value else None,
        'enterprise_value': round(enterprise_value, 2),
        'equity_value': round(equity_value, 2),
        'intrinsic_per_share': round(per_share, 2) if per_share else None,
        'assumptions': {'high_growth': high_growth, 'high_years': high_years,
                        'terminal_growth': terminal_growth, 'discount_rate': discount_rate},
    }
    if current_price and per_share:
        out['current_price'] = current_price
        out['margin_of_safety'] = round(per_share / current_price - 1, 3)  # >0 低估
        out['verdict'] = ('低估(有安全边际)' if per_share > current_price * 1.2 else
                          '大致合理' if per_share > current_price * 0.9 else '高估')
    if out.get('terminal_pct') and out['terminal_pct'] > 0.75:
        out['caution'] = '永续价值占比>75%,估值高度依赖远期假设,敏感性大'
    return out


def sensitivity(base_fcf: float, high_growth: float, high_years: int,
                shares: float, net_debt: float, current_price: Optional[float],
                wacc_range: List[float], tg_range: List[float]) -> Dict[str, Any]:
    """对 折现率×永续增速 的每股内在价值敏感性表。"""
    table = []
    for w in wacc_range:
        row = {'wacc': w, 'cells': []}
        for tg in tg_range:
            r = two_stage_dcf(base_fcf, high_growth, high_years, tg, w, shares, net_debt)
            row['cells'].append({'tg': tg, 'value': r.get('intrinsic_per_share')})
        table.append(row)
    return {'sensitivity': table}


def analyze_dcf(base_fcf: float, shares: float, current_price: float = None,
                high_growth: float = 0.10, high_years: int = 5,
                terminal_growth: float = 0.03, discount_rate: float = 0.10,
                net_debt: float = 0.0, fcf_is_proxy: bool = False) -> Dict[str, Any]:
    """便捷入口:给一组保守默认假设,产出 DCF 结果 + 摘要。"""
    r = two_stage_dcf(base_fcf, high_growth, high_years, terminal_growth,
                      discount_rate, shares, net_debt, current_price)
    if 'error' in r:
        return r
    r['fcf_is_proxy'] = fcf_is_proxy
    r['summary'] = _summary(r, fcf_is_proxy)
    return r


def _summary(r: Dict[str, Any], proxy: bool) -> str:
    a = r['assumptions']
    base = (f"【DCF估值】两阶段(高速{int(a['high_years'])}年@{a['high_growth']*100:.0f}%、"
            f"永续{a['terminal_growth']*100:.0f}%、折现{a['discount_rate']*100:.0f}%)"
            f"得每股内在价值 ≈ {r.get('intrinsic_per_share')}元")
    if proxy:
        base += "(注:用净利润近似FCF,偏乐观)"
    if 'margin_of_safety' in r:
        base += f";现价 {r['current_price']},安全边际 {r['margin_of_safety']*100:+.0f}% → {r.get('verdict')}"
    if r.get('caution'):
        base += f";{r['caution']}"
    return base + '。'


if __name__ == '__main__':
    import io, sys, json
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    # 示例:某公司年 FCF 100亿,股本 50亿股,现价 25
    r = analyze_dcf(base_fcf=100e8, shares=50e8, current_price=25,
                    high_growth=0.12, high_years=5, terminal_growth=0.03, discount_rate=0.10)
    print(json.dumps({k: v for k, v in r.items() if k != 'assumptions'}, ensure_ascii=False, indent=1))
    print('\nSUMMARY:', r['summary'])

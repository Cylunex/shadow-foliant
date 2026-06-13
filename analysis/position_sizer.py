# -*- coding: utf-8 -*-
"""仓位层 — 组合结构诊断 + 仓位建议(2026-06-12)

纯函数模块:输入持仓扫描结果(jobs_hub._scan_holdings_with_snapshot 的输出),
输出 总仓位建议(按市场环境)/单票超限/高波动重仓/新买入建议仓位,文本可直接注入 AI prompt。
不连库、不取行情,零外部依赖 → 不会拖慢调用方,挂了也只是少一段建议。

规则(简单可解释,宁可保守):
  - 单票上限 SINGLE_MAX=15%;超限提示减仓
  - 高波动重仓:VaR95>5% 且权重>10% → 提示
  - 目标总仓位按环境: trending_up 80% / 震荡|未知 60% / trending_down 35%
  - 新买入建议仓位:5%~12%,按波动率倒数缩放(高波动给小仓)
"""
from typing import Any, Dict, List, Optional

SINGLE_MAX = 0.15          # 单票权重上限
HIGH_VOL_VAR = 0.05        # 高波动阈值(日VaR95)
HIGH_VOL_WEIGHT = 0.10     # 高波动票的权重提示线

REGIME_TARGET = {
    'trending_up': 0.80,
    'range': 0.60,
    'volatile': 0.50,
    'trending_down': 0.35,
}


def analyze(scans: List[Dict[str, Any]], regime: Optional[str] = None) -> Dict[str, Any]:
    """组合结构 + 仓位建议。scans 需含 mv/var95/name/code(缺 mv 的持仓不参与权重)。"""
    pos = [s for s in (scans or []) if s.get('mv')]
    out: Dict[str, Any] = {
        'total_mv': 0.0, 'n': len(pos), 'regime': regime,
        'target_position_pct': round(REGIME_TARGET.get(regime or '', 0.60) * 100),
        'top3_pct': None, 'hhi': None,
        'over_limit': [], 'high_vol_heavy': [], 'new_buy_pct': (5, 12),
    }
    if not pos:
        return out

    total = sum(float(s['mv']) for s in pos)
    out['total_mv'] = round(total, 0)
    ws = sorted(((float(s['mv']) / total, s) for s in pos), reverse=True, key=lambda x: x[0])
    out['top3_pct'] = round(sum(w for w, _ in ws[:3]) * 100, 1)
    out['hhi'] = round(sum(w * w for w, _ in ws), 3)  # 赫芬达尔指数,>0.15 算集中

    for w, s in ws:
        if w > SINGLE_MAX:
            out['over_limit'].append({'code': s['code'], 'name': s.get('name') or s['code'],
                                      'weight_pct': round(w * 100, 1)})
        v = s.get('var95')
        if v is not None and v > HIGH_VOL_VAR and w > HIGH_VOL_WEIGHT:
            out['high_vol_heavy'].append({'code': s['code'], 'name': s.get('name') or s['code'],
                                          'weight_pct': round(w * 100, 1),
                                          'var95_pct': round(v * 100, 1)})
    return out


def suggest_new_buy_pct(var95: Optional[float] = None) -> float:
    """新买入建议仓位%:基准 8%,按波动调整(VaR95 2%→12%,5%→5%),夹在 5~12。"""
    if var95 is None or var95 <= 0:
        return 8.0
    pct = 8.0 * (0.03 / max(var95, 0.015))
    return round(max(5.0, min(12.0, pct)), 1)


def format_for_ai(a: Dict[str, Any]) -> str:
    """analyze() 结果 → 注入 AI prompt 的一段文本"""
    if not a or not a.get('n'):
        return ''
    L = [f"仓位约束建议: 当前{a['n']}只 总市值{a['total_mv'] / 10000:.1f}万"
         f" 前3集中度{a['top3_pct']}%"
         + (f" 环境{a['regime']}" if a.get('regime') else '')
         + f" → 建议总仓位≤{a['target_position_pct']}%,单票≤{int(SINGLE_MAX * 100)}%,"
           f"新买入单票 {a['new_buy_pct'][0]}~{a['new_buy_pct'][1]}%(高波动取下限)"]
    if a['over_limit']:
        L.append("超限(建议减至15%以内): " + '、'.join(
            f"{x['name']}{x['weight_pct']}%" for x in a['over_limit']))
    if a['high_vol_heavy']:
        L.append("高波动重仓(VaR95>5%且权重>10%): " + '、'.join(
            f"{x['name']}{x['weight_pct']}%(VaR{x['var95_pct']}%)" for x in a['high_vol_heavy']))
    if a.get('hhi') and a['hhi'] > 0.15:
        L.append(f"组合集中度偏高(HHI={a['hhi']}),建议分散")
    return '\n'.join(L)


def format_brief(a: Dict[str, Any]) -> str:
    """一行版(早盘持仓分析页脚用)"""
    if not a or not a.get('n'):
        return ''
    s = (f"💼 仓位: {a['n']}只/{a['total_mv'] / 10000:.0f}万 前3占{a['top3_pct']}% "
         f"建议总仓≤{a['target_position_pct']}%")
    if a['over_limit']:
        s += " ⚠️超限:" + '、'.join(f"{x['name']}{x['weight_pct']}%" for x in a['over_limit'][:3])
    return s


if __name__ == '__main__':
    import io, sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    demo = [
        {'code': '600519', 'name': '茅台', 'mv': 200000, 'var95': 0.02},
        {'code': '300750', 'name': '宁德', 'mv': 90000, 'var95': 0.06},
        {'code': '000001', 'name': '平安', 'mv': 50000, 'var95': 0.03},
        {'code': '600036', 'name': '招行', 'mv': 30000, 'var95': 0.025},
    ]
    a = analyze(demo, regime='trending_down')
    print(format_for_ai(a))
    print()
    print(format_brief(a))
    print(f"\n新买入建议: 低波动 {suggest_new_buy_pct(0.02)}% / 高波动 {suggest_new_buy_pct(0.06)}%")

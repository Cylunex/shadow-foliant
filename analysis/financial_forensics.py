"""
财务排雷 —— 借鉴 SkillHub「财务报表深度解读 / 上市公司财报体检」skill。

提供:
  - 杜邦分解(ROE = 净利率 × 资产周转 × 权益乘数)
  - 盈利质量:净利润 vs 经营现金流(应计背离)
  - 财务造假红旗清单(基于可得指标的启发式判定)
  - 给 LLM 基本面分析师注入的「排雷方法论」文本(RED_FLAG_GUIDE)

纯计算 + 方法论,零依赖。可吃 collect_factors / 季报派生的指标 dict。
"""

from __future__ import annotations
from typing import Dict, List, Optional, Any


def dupont(roe: Optional[float], net_margin: Optional[float] = None,
           asset_turnover: Optional[float] = None,
           equity_multiplier: Optional[float] = None) -> Dict[str, Any]:
    """杜邦三因子分解。给定其中部分,尽量还原并定位 ROE 的驱动来源。

    ROE = 净利率(net_margin) × 总资产周转率(asset_turnover) × 权益乘数(equity_multiplier)
    """
    out: Dict[str, Any] = {'roe': roe, 'net_margin': net_margin,
                           'asset_turnover': asset_turnover, 'equity_multiplier': equity_multiplier}
    parts = [net_margin, asset_turnover, equity_multiplier]
    known = [p for p in parts if p is not None]
    if len(known) == 3:
        out['roe_reconstructed'] = round(net_margin * asset_turnover * equity_multiplier, 4)
    # 驱动定位
    if all(p is not None for p in parts):
        driver = max(
            [('盈利能力(净利率)', net_margin), ('运营效率(周转)', asset_turnover),
             ('财务杠杆(权益乘数)', equity_multiplier)],
            key=lambda x: x[1] if x[1] else 0)
        out['main_driver'] = driver[0]
        if equity_multiplier and equity_multiplier > 3:
            out['warning'] = 'ROE 高度依赖财务杠杆(权益乘数>3),需警惕高负债风险'
    return out


def earnings_quality(net_profit: Optional[float], ocf: Optional[float] = None,
                     ocf_ratio: Optional[float] = None) -> Dict[str, Any]:
    """盈利质量:经营现金流是否覆盖净利润(应计背离)。

    ocf_ratio = 经营现金流 / 净利润。<1 说明利润"含现金量"低,>0.8~1 健康。
    """
    if ocf_ratio is None and net_profit and ocf is not None and net_profit != 0:
        ocf_ratio = ocf / net_profit
    out: Dict[str, Any] = {'ocf_ratio': round(ocf_ratio, 3) if ocf_ratio is not None else None}
    if ocf_ratio is None:
        out['verdict'] = '数据不足'
    elif ocf_ratio < 0:
        out['verdict'] = '⚠️ 经营现金流为负,盈利质量差(利润可能是纸面富贵)'
    elif ocf_ratio < 0.7:
        out['verdict'] = '⚠️ 现金含量偏低(OCF/净利<0.7),警惕应计利润/激进确认'
    elif ocf_ratio <= 1.5:
        out['verdict'] = '✓ 现金含量健康'
    else:
        out['verdict'] = '现金含量很高(可能折旧大/重资产)'
    return out


# 红旗判定:指标键 → (判定函数, 描述)
def red_flags(m: Dict[str, Optional[float]]) -> List[str]:
    """基于可得指标的财务红旗启发式。m 可含:
    ocf_ratio, debt_ratio, net_profit_growth, revenue_growth, gross_margin,
    roe, receivable_growth, goodwill_ratio。缺失项自动跳过。
    """
    flags: List[str] = []
    g = m.get
    if g('ocf_ratio') is not None and g('ocf_ratio') < 0.7:
        flags.append('盈利质量:OCF/净利<0.7,利润含现金量低')
    if g('debt_ratio') is not None and g('debt_ratio') > 70:
        flags.append('偿债:资产负债率>70%,杠杆偏高')
    np_g, rev_g = g('net_profit_growth'), g('revenue_growth')
    if np_g is not None and rev_g is not None:
        if np_g > 30 and rev_g < 0:
            flags.append('增长背离:增利不增收(净利↑但营收↓),警惕非经常性损益/操纵')
        if rev_g > 30 and np_g < 0:
            flags.append('增长背离:增收不增利,警惕成本失控/低价冲量')
    if g('receivable_growth') is not None and rev_g is not None and g('receivable_growth') > rev_g + 30:
        flags.append('应收异常:应收账款增速远超营收,警惕放宽信用/虚增收入')
    if g('gross_margin') is not None and g('gross_margin') > 70 and (g('ocf_ratio') or 1) < 0.5:
        flags.append('毛利异常:超高毛利却现金回收差,需核实收入真实性')
    if g('goodwill_ratio') is not None and g('goodwill_ratio') > 30:
        flags.append('商誉:商誉占净资产>30%,警惕减值地雷')
    return flags


def analyze_forensics(metrics: Dict[str, Optional[float]]) -> Dict[str, Any]:
    """综合财务排雷,返回结构化结果 + 中文摘要。"""
    du = dupont(metrics.get('roe'), metrics.get('net_margin'),
                metrics.get('asset_turnover'), metrics.get('equity_multiplier'))
    eq = earnings_quality(metrics.get('net_profit'), metrics.get('ocf'), metrics.get('ocf_ratio'))
    flags = red_flags(metrics)
    out = {'dupont': du, 'earnings_quality': eq, 'red_flags': flags,
           'flag_count': len(flags)}
    out['summary'] = format_forensics_summary(out)
    return out


def format_forensics_summary(r: Dict[str, Any]) -> str:
    parts = ['【财务排雷】']
    eq = r.get('earnings_quality', {})
    if eq.get('verdict'):
        parts.append(f"盈利质量:{eq['verdict']}。")
    du = r.get('dupont', {})
    if du.get('main_driver'):
        parts.append(f"ROE 主要由「{du['main_driver']}」驱动。")
    if du.get('warning'):
        parts.append(du['warning'] + '。')
    flags = r.get('red_flags', [])
    if flags:
        parts.append(f"触发 {len(flags)} 项红旗:" + '；'.join(flags) + '。')
    else:
        parts.append('未触发明显财务红旗(基于可得指标)。')
    return ' '.join(parts)


# 注入基本面分析师 prompt 的方法论(财报深度解读 / 造假识别)
RED_FLAG_GUIDE = """【财务排雷方法论(请据此审视上方三表/财务数据)】
1. 杜邦分解:把 ROE 拆成 净利率×资产周转×权益乘数,判断高 ROE 是真本事还是靠加杠杆。
2. 盈利质量:对比净利润与经营现金流——利润持续>>经营现金流是首要造假/激进确认信号。
3. 三表勾稽:利润与现金流、应收/存货与营收增速是否匹配;应收/存货增速远超营收=危险。
4. 造假红旗:增利不增收/增收不增利、毛利率异常高于同业、商誉占净资产过高、
   频繁非经常性损益、关联交易占比高、审计意见非标。
5. 偿债与质押:资产负债率、有息负债、大股东股权质押比例。
请在分析中明确指出命中的红旗与盈利质量结论。"""


if __name__ == '__main__':
    import io, sys, json
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    demo = {'roe': 22, 'net_margin': 0.25, 'asset_turnover': 0.6, 'equity_multiplier': 1.5,
            'ocf_ratio': 0.45, 'debt_ratio': 75, 'net_profit_growth': 40, 'revenue_growth': -5,
            'gross_margin': 75}
    r = analyze_forensics(demo)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    print('\nSUMMARY:', r['summary'])

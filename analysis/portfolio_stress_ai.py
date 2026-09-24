import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""组合压力情景叙事官 —— 把 scenario_stress 的损益矩阵翻译成可执行风险预案。

缺口:analysis/scenario_stress.py 已实现 8 个命名宏观情景(加息/贬值/大盘暴跌/流动性危机/半导体重挫…)
按 β+行业暴露聚合组合损益,但纯数字、无人定时调用、无解读——"大盘暴跌10% 组合-8.7%"没转成
"哪几只重灾区、先减哪个、对冲什么"的剧本。

本模块组装持仓 → 跑全 8 情景 → 加组合集中度 → LLM 输出:最脆弱情景、贡献最大的持仓、
可操作的减仓/对冲建议。复用已写好的情景引擎,只差 AI 加工;token 极省(矩阵紧凑)。

接口:run_stress_narrative() -> dict
"""

from typing import Any, Dict, List


def _concentration(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    mvs = sorted((float(p.get('market_value') or 0) for p in positions), reverse=True)
    total = sum(mvs) or 1.0
    return {
        'n': len(positions),
        'total_mv': round(total, 0),
        'top1_pct': round(mvs[0] / total * 100, 1) if mvs else 0,
        'top3_pct': round(sum(mvs[:3]) / total * 100, 1) if mvs else 0,
        'hhi': round(sum((m / total) ** 2 for m in mvs), 4),
    }


def run_stress_narrative(include_funds: bool = True) -> Dict[str, Any]:
    """跑组合全情景压力 + 集中度 → AI 风险预案。返回 {ok, text, summary, matrix, concentration}。"""
    out = {'ok': False, 'text': '', 'summary': ''}
    try:
        from scenario_stress import build_portfolio_positions, stress_all, SCENARIOS  # noqa: F401
        positions = build_portfolio_positions(include_funds=include_funds)
    except Exception as e:
        out['summary'] = f'持仓组装失败: {type(e).__name__}: {str(e)[:60]}'
        return out
    if not positions:
        out['summary'] = '无持仓'
        return out
    matrix = stress_all(positions)
    conc = _concentration(positions)
    out['matrix'] = matrix
    out['concentration'] = conc

    # 组装紧凑矩阵喂 LLM(最坏 4 情景 + 各自重灾 top3)
    lines = []
    for sc in matrix[:4]:
        pct = sc.get('total_pnl_pct')
        worst = '、'.join(f"{w.get('name','')}({w.get('pnl',0):+.0f})" for w in (sc.get('worst') or [])[:3])
        lines.append(f"【{sc.get('scenario')}】组合 {(pct * 100 if pct is not None else 0):+.1f}%;重灾:{worst}")
    prompt = f"""你是组合风险官。下面是某账户在 8 个宏观情景下的压力测试结果(最坏 4 个)和集中度。

集中度:持仓{conc['n']}只,总市值{conc['total_mv']:.0f},第一大{conc['top1_pct']}%、前三{conc['top3_pct']}%,HHI={conc['hhi']}。
压力矩阵(组合损益% + 各情景重灾持仓):
{chr(10).join(lines)}

请输出**仅供研究的压力情景解释**(≤180字),包含:
1. 最脆弱情景 + 一句话原因(集中/高β/行业暴露)
2. 跨情景反复出现的风险暴露持仓及其原因
3. 数据假设与尚待验证的风险缓释方向
不得给具体减仓比例、期权或期货委托方案；没有核验交易权限、成本和用户授权。"""
    try:
        from deepseek_client import DeepSeekClient
        narr = DeepSeekClient().call_api(
            [{'role': 'system', 'content': '你是组合压力情景研究员，只解释风险来源与待核验缓释方向，不下交易指令。'},
             {'role': 'user', 'content': prompt}], max_tokens=900, call_type='stress_narrative')
    except Exception as e:
        out['summary'] = f'AI 叙事失败: {type(e).__name__}: {str(e)[:60]}'
        return out

    worst_sc = matrix[0] if matrix else {}
    wp = worst_sc.get('total_pnl_pct')
    head = (f"🛡️ 组合压力预案 — 最脆弱:{worst_sc.get('scenario','?')} "
            f"({(wp * 100 if wp is not None else 0):+.1f}%) · 前三集中{conc['top3_pct']}%")
    out['ok'] = True
    out['summary'] = head
    narrative = (narr or '').strip()
    if any(word in narrative for word in ('认沽', '期权', '期货', 'IF空头', '减仓', '卖出', '买入')):
        narrative = '模型解释含未经权限和成本核验的交易动作，本期仅保留压力矩阵与风险暴露供研究。'
    out['text'] = (head + '\n\n' + narrative +
                   '\n仅研究情景；执行任何调整前须核验权限、成本、时点并由用户自行决定。')
    return out


if __name__ == '__main__':
    import io
    _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== 组合压力叙事 自检 ===')
    r = run_stress_narrative()
    print(r.get('summary'))
    print((r.get('text') or '')[:400])

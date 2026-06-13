"""Agent 复杂度自动路由 — 借鉴 go-stock 设计

按问题特征自动分类为：
  - react       (简单) 一次工具组采集 + 一次 LLM 调用  — 响应快
  - plan_execute(复杂) 多工具组并行采集 + 拆步骤推理 + 汇总 — 准确度高

分类依据：
  - 字数：<30 倾向 react，>80 倾向 plan_execute
  - 关键词：含"今天/价格/代码/查/是多少" → react
            含"全面分析/投资建议/对比/赛道/深度/为什么/原因/估值/前景" → plan_execute
  - 显式工具数（如调用方已指定）≥4 强制 plan_execute

接口：
  classify(question) -> 'react' | 'plan_execute'
  run(question, symbol=None, groups=None, prefer_mode=None) -> {mode, answer, used_groups, used_llm}
"""

from typing import Dict, List, Optional, Any, Tuple

REACT_KEYWORDS = {
    '价格', '多少', '今天', '查', '代码', '名称', '收盘', '开盘', '涨跌',
    '股息', '市盈率', '行情', '盘口', '看下', '是不是',
}
PLAN_EXECUTE_KEYWORDS = {
    '全面分析', '深度', '深入', '为什么', '原因', '前景', '估值', '对比',
    '赛道', '产业链', '机会', '风险评估', '投资建议', '组合', '配置',
    '宏观', '行业地位', '竞争', '护城河', '催化', '逻辑',
}


def classify(question: str, tool_count: int = 0) -> str:
    """根据问题特征 + 工具组数量自动分类"""
    q = (question or '').strip()
    if not q:
        return 'react'
    if tool_count >= 4:
        return 'plan_execute'
    if any(k in q for k in PLAN_EXECUTE_KEYWORDS):
        return 'plan_execute'
    if any(k in q for k in REACT_KEYWORDS):
        return 'react'
    if len(q) < 30:
        return 'react'
    if len(q) > 80:
        return 'plan_execute'
    return 'react'


REACT_DEFAULT_GROUPS = ['base', 'kline_technical']
PLAN_EXECUTE_DEFAULT_GROUPS = ['base', 'kline_technical', 'fund_flow',
                               'fundamentals', 'sentiment', 'chipset', 'macro_us']


def _react_run(question: str, symbol: Optional[str],
               groups: Optional[List[str]] = None) -> Dict[str, Any]:
    """ReAct 简化版：一次工具组采集 → 一次 LLM 调用"""
    used_groups = groups or REACT_DEFAULT_GROUPS
    ctx = {}
    if symbol:
        try:
            from agent_tool_groups import collect
            ctx = collect(used_groups, symbol)
        except Exception as e:
            ctx = {'_collect_error': str(e)}

    prompt_lines = [f'你是 A 股 AI 助手，请简洁回答以下问题。\n\n问题：{question}']
    if symbol:
        prompt_lines.append(f'\n标的：{symbol}')
        prompt_lines.append(f'\n参考数据（JSON）：\n{_compact_json(ctx)}')
    prompt_lines.append('\n要求：3-5 句话精炼回答，引用数据时标明来源字段。')

    from deepseek_client import DeepSeekClient
    client = DeepSeekClient()
    text = client.call_api(
        messages=[
            {'role': 'system', 'content': '你是 A 股资深分析师，回答精炼、有数据支撑。'},
            {'role': 'user', 'content': '\n'.join(prompt_lines)},
        ],
        temperature=0.5, max_tokens=800,
    )
    return {
        'mode': 'react',
        'answer': text,
        'used_groups': used_groups,
        'used_llm': getattr(client, 'last_used_provider', None),
    }


def _plan_execute_run(question: str, symbol: Optional[str],
                      groups: Optional[List[str]] = None) -> Dict[str, Any]:
    """PlanExecute 简化版：多工具组并行采集 → 拆步骤推理 → 汇总"""
    used_groups = groups or PLAN_EXECUTE_DEFAULT_GROUPS
    ctx = {}
    if symbol:
        try:
            from agent_tool_groups import collect
            ctx = collect(used_groups, symbol)
        except Exception as e:
            ctx = {'_collect_error': str(e)}

    plan_prompt = [
        f'你是 A 股顶级分析师。请对以下问题给出**深度分析**。',
        f'问题：{question}',
        f'标的：{symbol or "N/A"}',
        f'已采集多维数据（JSON）：',
        _compact_json(ctx),
        '',
        '请按以下结构作答：',
        '1. 【关键事实】列出最重要的 3-5 条数据观察（带字段引用）',
        '2. 【分维度判断】技术/资金/基本面/情绪/筹码/宏观 — 每维度 1-2 句',
        '3. 【综合结论】买/卖/观望 + 理由 + 信心度（高/中/低）',
        '4. 【风险提示】最大潜在风险',
    ]

    from deepseek_client import DeepSeekClient
    client = DeepSeekClient()
    text = client.call_api(
        messages=[
            {'role': 'system', 'content': '你是 A 股顶级策略分析师，擅长综合多维数据做投资判断。'},
            {'role': 'user', 'content': '\n'.join(plan_prompt)},
        ],
        temperature=0.6, max_tokens=2500,
        thinking=False,
    )
    return {
        'mode': 'plan_execute',
        'answer': text,
        'used_groups': used_groups,
        'used_llm': getattr(client, 'last_used_provider', None),
        'thinking': False,
    }


def _compact_json(obj: Any, max_chars: int = 4000) -> str:
    """压缩 JSON 表示（去掉大数组 df_tail / 太长列表）"""
    import json
    def _trim(o):
        if isinstance(o, dict):
            r = {}
            for k, v in o.items():
                if k == 'df_tail':
                    r[k] = f'<{len(v) if v else 0} rows omitted>'
                else:
                    r[k] = _trim(v)
            return r
        if isinstance(o, list) and len(o) > 20:
            return o[:10] + [f'... ({len(o)-10} more omitted)']
        if isinstance(o, list):
            return [_trim(x) for x in o]
        return o
    try:
        s = json.dumps(_trim(obj), ensure_ascii=False, default=str)
        if len(s) > max_chars:
            s = s[:max_chars] + '... (truncated)'
        return s
    except Exception:
        return str(obj)[:max_chars]


def run(question: str, symbol: Optional[str] = None,
        groups: Optional[List[str]] = None,
        prefer_mode: Optional[str] = None) -> Dict[str, Any]:
    """自动路由 → 选 mode 执行 → 返回 answer + 元信息"""
    mode = prefer_mode or classify(question, tool_count=len(groups) if groups else 0)
    runner = _plan_execute_run if mode == 'plan_execute' else _react_run
    result = runner(question, symbol, groups)
    result['classified_as'] = mode
    return result


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== Agent Router 分类自检 ===')
    tests = [
        ('茅台今天价格多少？', None),
        ('600519 全面分析估值前景', None),
        ('短线突破策略是什么', None),
        ('深入分析 000670 的产业链地位、护城河和潜在风险', None),
        ('看下', None),
    ]
    for q, _ in tests:
        m = classify(q)
        print(f'  [{m:>12s}] {q}')
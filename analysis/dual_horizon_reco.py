"""AI 双层推荐(短线/长期)—— 候选池 + 市场环境 → 严格 JSON 输出(借鉴 eastmoney 思路,自研)。

对一批候选股,组织"候选池数据表 + 市场环境"喂 LLM,产出**短线(动量,≥70分)**与
**长期(基本面,≥75分)**两层推荐,JSON 结构化、要求带具体数据支撑。复用 llm_router。
区别于现有 final_decision(单股深析):这是**候选池横向初筛**,轻量、批量。
"""

from __future__ import annotations

import json
import re
from typing import List, Dict, Optional

_PROMPT = """你是A股投研助手。基于下方候选池数据与市场环境,分别给出**短线**与**长期**推荐。

## 候选池({n} 只)
{table}

## 市场环境
{market}

## 输出要求(严格 JSON,不要任何额外文字/解释/markdown 代码块标记)
{{
  "short_term": [   // 动量/事件驱动,持有数日~数周,评分≥70才入选
    {{"code":"600519","name":"贵州茅台","score":75,"target_pct":8,"stop_pct":-5,
      "horizon":"2-4周","logic":"用具体数据说明(涨幅/PE/资金流等)","risks":["..."],"confidence":"高/中/低"}}
  ],
  "long_term": [    // 基本面/估值,持有数月+,评分≥75才入选
    {{"code":"...","name":"...","score":80,"target_pct":20,"horizon":"6-12月",
      "logic":"估值/增长/护城河,带数据","risks":["..."],"confidence":"高/中/低"}}
  ],
  "market_view":"一句话市场判断(含指数/情绪)",
  "risk_warning":"主要风险点"
}}
要求:logic 必须引用候选池里的具体数字,不许写"表现优秀"这类空话;不达分数线就不要放进对应列表;只输出 JSON。"""


def _candidate_table(codes: List[str]) -> str:
    """组织候选池数据表(代码/名称/现价/涨跌%/PE/PB/换手/市值亿)。"""
    quotes = {}
    try:
        import datahub
        quotes = datahub.quotes(codes)
    except Exception:
        quotes = {}
    lines = ["| 代码 | 名称 | 现价 | 涨跌% | PE | PB | 换手% | 市值(亿) |",
             "|---|---|---|---|---|---|---|---|"]
    for c in codes:
        q = quotes.get(re.sub(r'\D', '', c)) or quotes.get(c) or {}
        lines.append(f"| {c} | {q.get('name','?')} | {q.get('price','-')} | "
                     f"{q.get('change_pct','-')} | {q.get('pe_ttm','-')} | {q.get('pb','-')} | "
                     f"{q.get('turnover_pct','-')} | {q.get('mcap_yi','-')} |")
    return "\n".join(lines)


def _market_context() -> str:
    """简要市场环境(best-effort:上证指数 + 涨跌家数,取不到给占位)。"""
    try:
        import datahub
        q = datahub.quotes(['sh000001'])
        idx = q.get('000001') or {}
        if idx:
            return f"上证指数 {idx.get('price','?')}({idx.get('change_pct','?')}%)。"
    except Exception:
        pass
    return "(市场环境数据暂缺,请基于候选池自身研判。)"


def _parse_json(text: str) -> Optional[dict]:
    """从 LLM 文本里稳健抽取 JSON(剥 ```json 围栏、取首个 {...})。"""
    if not text:
        return None
    t = re.sub(r'```(?:json)?', '', text).strip()
    m = re.search(r'\{.*\}', t, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def recommend(codes: List[str], market: str = None, temperature: float = 0.4) -> dict:
    """对候选股出双层推荐。返回 {short_term, long_term, market_view, risk_warning, provider, raw?}。"""
    codes = [str(c).strip() for c in codes if str(c).strip()]
    if not codes:
        return {'error': '候选池为空'}
    table = _candidate_table(codes)
    market = market or _market_context()
    prompt = _PROMPT.format(n=len(codes), table=table, market=market)
    try:
        from llm_router import get_router
        text, provider = get_router().call(
            [{'role': 'system', 'content': '你只输出严格 JSON,不输出任何额外文字。'},
             {'role': 'user', 'content': prompt}],
            temperature=temperature, max_tokens=2000)
    except Exception as e:
        return {'error': f'LLM 调用失败: {type(e).__name__}: {e}', 'provider': 'none'}
    parsed = _parse_json(text)
    if parsed is None:
        return {'error': 'JSON 解析失败', 'provider': provider, 'raw': text[:1000]}
    parsed['provider'] = provider
    parsed.setdefault('short_term', [])
    parsed.setdefault('long_term', [])
    return parsed


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print(_candidate_table(['600519', '000001']))

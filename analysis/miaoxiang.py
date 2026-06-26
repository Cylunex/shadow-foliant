"""妙想(东方财富 AI SaaS)外部服务包 —— 自然语言问句 → 成品分析 / 数据 / 报告。

定位:**可选的"第二意见 / 外部数据"来源**,与项目自研多智能体互补(非替代、非核心)。
统一端点 `https://ai-saas.eastmoney.com/proxy/`,认证 header `em_api_key`,body `{字段: 问句}`,
结果在 `data.displayData`(assistant 类)或 `data.llmSearchResponse`(搜索类)。

实现:仅 stdlib(urllib),零新依赖。供 MCP 调用(见 mcp_server.py 的 `mx_*` 工具),不接 UI。

⚠️ 三方 SaaS:问句/数据会发往东财服务器(数据出境/合规请自行评估),敏感分析慎用。
⚠️ EM_API_KEY:默认内置 demo key(很可能限流/临时),生产请在 `.env` 配 `EM_API_KEY=<自有key>`。
"""

import json
import os
from urllib import request as _req, error as _err

BASE = "https://ai-saas.eastmoney.com/proxy/"
_DEMO_KEY = "em_2MutTwTdZO2LjFnrMJA59NUV4YLV590L"  # 来自官方 skill 的共享 demo key,生产请覆盖
TIMEOUT = 90

# 技能注册:name -> (endpoint 路径, 入参字段)。新增能力只需在此登记一行。
SKILLS = {
    # —— assistant 类(返回 data.displayData,markdown 成品)——
    'stock_diagnosis': ('app-robo-advisor-api/assistant/stock-analysis', 'question'),   # A股个股综合诊断
    'fund_diagnosis':  ('app-robo-advisor-api/assistant/fund-analysis', 'question'),    # 公募基金诊断
    'comparable':      ('app-robo-advisor-api/assistant/comparable-company-analysis', 'question'),  # 可比公司/同业估值
    'hotspot':         ('app-robo-advisor-api/assistant/hotspot-discovery', 'question'),  # 市场热点发现
    'ask':             ('app-robo-advisor-api/assistant/ask', 'question'),              # 七合一金融问答(总入口)
    'industry_report': ('app-robo-advisor-api/assistant/write/industry/research', 'query'),  # 行业研报
    'topic_report':    ('app-robo-advisor-api/assistant/write/thematic/research', 'query'),   # 专题/事件研报
    'kb_search':       ('app-robo-advisor-api/assistant/private-domain-search', 'query'),     # 私域知识库检索
    # —— b/mcp/tool 类(返回 data.llmSearchResponse / 表格结构)——
    'finance_search':  ('b/mcp/tool/searchNews', 'query'),         # NL 搜公告/研报/新闻/政策
    'finance_data':    ('b/mcp/tool/searchData', 'query'),         # NL 查行情/财务/估值(表格)
    'macro_data':      ('b/mcp/tool/searchMacroData', 'query'),    # NL 查宏观(GDP/CPI/货币…)
}


def _api_key() -> str:
    return (os.getenv('EM_API_KEY') or _DEMO_KEY).strip()


def using_demo_key() -> bool:
    return not os.getenv('EM_API_KEY')


def _extract(raw):
    """从妙想响应里抽可读正文。兼容 data/result 包裹 + 多种字段;取不到则返回空串。"""
    if not isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False) if raw else ''
    for wrap in ('data', 'result'):
        inner = raw.get(wrap)
        if isinstance(inner, dict):
            got = _extract(inner)
            if got:
                return got
    for key in ('displayData', 'llmSearchResponse', 'searchResponse', 'content', 'answer', 'summary', 'message'):
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (list, dict)) and v:
            return json.dumps(v, ensure_ascii=False, indent=2)
    return ''


def query(skill: str, text: str, timeout: int = TIMEOUT) -> dict:
    """通用调用。skill ∈ SKILLS 键;text 为自然语言问句。

    返回 {skill, content, using_demo_key} 或 {error, skill}。
    """
    if skill not in SKILLS:
        return {'error': f'未知 skill: {skill};可选 {list(SKILLS)}'}
    text = (text or '').strip()
    if not text:
        return {'error': 'text 为空', 'skill': skill}
    path, field = SKILLS[skill]
    try:  # 与 screen() 同源限流(1s 最小间隔):防 mx_selection_review(10:30 盘中)逐只
        from rate_limiter import throttle as _throttle   # 诊断 top10 背靠背连打东财妙想 SaaS 触发封禁
        _throttle('eastmoney_saas')
    except Exception:
        pass
    body = json.dumps({field: text}, ensure_ascii=False).encode('utf-8')
    req = _req.Request(BASE + path, data=body, method='POST',
                       headers={'Content-Type': 'application/json', 'em_api_key': _api_key()})
    try:
        with _req.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode('utf-8', 'replace'))
    except _err.HTTPError as e:
        msg = (e.read().decode('utf-8', 'replace')[:200] if e.fp else '') or f'HTTP {e.code}'
        return {'error': f'妙想API失败: {msg}', 'skill': skill}
    except Exception as e:
        return {'error': f'妙想API失败: {e}', 'skill': skill}
    content = _extract(raw if isinstance(raw, dict) else {'data': raw})
    out = {'skill': skill, 'content': content or '(无内容返回)', 'using_demo_key': using_demo_key()}
    if not content:
        out['raw'] = raw  # 没抽到正文时附原始响应,便于排查
    return out


# —— 妙想智能选股(mx-stocks-screener / selectSecurity)——
# 与上面 SKILLS 不同:body 是 {query, selectType, toolContext}、结果在 data.allResults.result.dataList。
# 这是**非问财的独立选股源**(数据来自东财妙想大模型),能按自然语言条件海选出结构化候选清单。
_SCREENER_PATH = 'b/mcp/tool/selectSecurity'
SELECT_TYPES = ('A股', '港股', '美股', '基金', 'ETF', '可转债', '板块')


def screen(query_text: str, select_type: str = 'A股', timeout: int = 40):
    """妙想自然语言选股 → pandas.DataFrame(列含 '代码'/'名称' + 中文指标列)。失败/无结果返回空 DF。

    query_text 例:'主力资金净流入排名前20的A股'、'市盈率最低的50只创业板'、'半导体板块市值前20';
    select_type ∈ A股/港股/美股/基金/ETF/可转债/板块。纯旁路源:任何异常吞掉返回空 DF,不抛。"""
    import uuid as _uuid
    try:
        import pandas as _pd
    except Exception:
        return None
    text = (query_text or '').strip()
    if not text:
        return _pd.DataFrame()
    try:  # 与其他妙想调用同源限流(1s 最小间隔)
        from rate_limiter import throttle as _throttle
        _throttle('eastmoney_saas')
    except Exception:
        pass
    meta = {'query': text, 'selectType': select_type or 'A股',
            'toolContext': {'callId': f'call_{_uuid.uuid4().hex[:8]}',
                            'userInfo': {'userId': f'user_{_uuid.uuid4().hex[:8]}'}}}
    body = json.dumps(meta, ensure_ascii=False).encode('utf-8')
    req = _req.Request(BASE + _SCREENER_PATH, data=body, method='POST',
                       headers={'Content-Type': 'application/json', 'em_api_key': _api_key()})
    try:
        with _req.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode('utf-8', 'replace'))
    except Exception:
        return _pd.DataFrame()
    data = raw.get('data') if isinstance(raw, dict) else None
    if not isinstance(data, dict):
        return _pd.DataFrame()
    res = ((data.get('allResults') or {}).get('result') or {})
    data_list = res.get('dataList') if isinstance(res, dict) else None
    if not isinstance(data_list, list) or not data_list:
        return _pd.DataFrame()
    # 列名 key → 中文 title 映射(SECURITY_CODE→代码、SECURITY_SHORT_NAME→名称…)
    cmap = {}
    for c in (res.get('columns') or []):
        if isinstance(c, dict):
            k = c.get('field') or c.get('name') or c.get('key')
            t = c.get('displayName') or c.get('title') or c.get('label') or k
            if k:
                cmap[str(k)] = str(t)
    rows = [{cmap.get(str(k), str(k)): v for k, v in r.items()}
            for r in data_list if isinstance(r, dict)]
    return _pd.DataFrame(rows)


def screen_codes(query_text: str, select_type: str = 'A股', top_n: int = 0, timeout: int = 40):
    """screen() 便捷封装:直接返回 6 位股票代码列表(去重保序,可截断 top_n)。失败返回 []。"""
    df = screen(query_text, select_type, timeout)
    if df is None or df.empty or '代码' not in df.columns:
        return []
    seen, out = set(), []
    for c in df['代码'].tolist():
        s = str(c).strip()
        if not s:
            continue
        s = s.split('.')[0].zfill(6)[-6:]   # 去交易所后缀/补零取后6位
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out[:top_n] if (top_n and top_n > 0) else out


# —— 便捷命名包装(MCP 工具直接调这些)——
def stock_diagnosis(question: str) -> dict: return query('stock_diagnosis', question)
def finance_ask(question: str) -> dict:     return query('ask', question)
def hotspot(question: str) -> dict:         return query('hotspot', question)
def comparable(question: str) -> dict:      return query('comparable', question)
def finance_search(q: str) -> dict:         return query('finance_search', q)
def macro_data(q: str) -> dict:             return query('macro_data', q)
def industry_report(q: str) -> dict:        return query('industry_report', q)
def topic_report(q: str) -> dict:           return query('topic_report', q)
def fund_diagnosis(q: str) -> dict:         return query('fund_diagnosis', q)


if __name__ == '__main__':
    import sys
    sk = sys.argv[1] if len(sys.argv) > 1 else 'ask'
    tx = sys.argv[2] if len(sys.argv) > 2 else '今天A股市场整体怎么样'
    print(json.dumps(query(sk, tx), ensure_ascii=False, indent=2))

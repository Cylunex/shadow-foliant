# -*- coding: utf-8 -*-
"""自定义 AI 工作流引擎(两层:并行分析 → 综合)。

概念:
  - DataProvider:数据块(包平台现有数据函数),按 scope 声明参数。
  - AgentDef:智能体 {名称, 模型, system, user模板, inputs(看哪些data), output}。
  - Workflow:{name, scope, params, data:[key], analysts:[AgentDef], synthesizer:AgentDef}。
  - 执行:取数(并发) → 各 analyst 填模板调 LLM(并发) → synthesizer 汇总 → 结果。
模板占位符:{{ctx.code}} {{data.<key>}} {{analyst.<名称>}}(JSON 数据自动字符串化)。
所有数据块失败只返回 {error},不影响整体。
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Callable

import _bootstrap  # noqa: F401  注入 sys.path


# ============================ 数据块注册表 ============================
def _stock_df(code, period="1y"):
    # 走 datahub.kline(复用磁盘缓存);失败返回 {"error"} 以保持各 provider 的 isinstance(df, dict) 错误契约
    # 前复权 qfq:AI 工作流数据块多用于技术分析,防除权跳空
    import datahub
    df = datahub.kline(code, period, adjust='qfq')
    return df if (df is not None and not getattr(df, "empty", True)) else {"error": "无行情"}


def _p_quote(ctx):
    import datahub
    return datahub.stock_info(ctx["code"])


def _p_fundamental(ctx):
    from fundamental_scoring import score_one
    return score_one(ctx["code"])


def _p_forensics(ctx):
    from fundamental_scoring import collect_factors
    from financial_forensics import analyze_forensics
    return analyze_forensics(collect_factors(ctx["code"]) or {})


def _p_chan(ctx):
    from chan_theory import analyze_chan
    df = _stock_df(ctx["code"])
    return analyze_chan(df, ctx["code"]) if not isinstance(df, dict) else {"error": "无行情"}


def _p_chip(ctx):
    from chip_distribution import chip_distribution as cyq
    df = _stock_df(ctx["code"])
    return cyq(df) if not isinstance(df, dict) else {"error": "无行情"}


def _p_signals(ctx):
    from strategy_signals import shrink_pullback, bottom_volume, emotion_top_warning, detect_regime
    df = _stock_df(ctx["code"])
    if isinstance(df, dict):
        return {"error": "无行情"}
    return {"regime": detect_regime(df), "shrink_pullback": shrink_pullback(df),
            "bottom_volume": bottom_volume(df), "emotion_top_warning": emotion_top_warning(df)}


def _p_flow(ctx):
    import datahub
    return datahub.capital_flow_adata(ctx["code"])


def _p_kline(ctx):
    df = _stock_df(ctx["code"])
    if isinstance(df, dict):
        return {"error": "无行情"}
    df = df.reset_index()
    cc = "Close" if "Close" in df.columns else "close"
    closes = [float(c) for c in df[cc].tail(30) if c == c]
    return {"近30日收盘": [round(c, 2) for c in closes],
            "区间": [round(min(closes), 2), round(max(closes), 2)] if closes else None}


def _p_indices(ctx):
    # 统一走 datahub.indices(新浪→腾讯源链兜底);保留本块"名:值(涨跌%)"字符串契约供 AI 提示词/晨报用
    import datahub
    return {x["name"]: f"{x['value']:.2f} ({x['change_pct']:+.2f}%)" for x in datahub.indices()}


def _p_news(ctx):
    import datahub
    return [n.get("title", "") for n in datahub.market_news(25)]


def _p_sector(ctx):
    import datahub
    s = datahub.sector_spot(top_n=8, bottom_n=5)
    return {"涨幅榜": s.get("top", []), "跌幅榜": s.get("bottom", [])}


def _p_lhb(ctx):
    import akshare as ak
    import datetime
    end = datetime.date.today()
    start = end - datetime.timedelta(days=14)
    df = ak.stock_lhb_jgmmtj_em(start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
    rows = [{"名称": r.get("名称"), "代码": r.get("代码"),
             "机构净买亿": round(float(r.get("机构买入净额") or 0) / 1e8, 2),
             "涨跌幅": r.get("涨跌幅")} for _, r in df.head(20).iterrows()]
    return sorted(rows, key=lambda x: x["机构净买亿"], reverse=True)


def _p_macro(ctx):
    from macro_cycle_data import MacroCycleDataFetcher
    return MacroCycleDataFetcher().get_all_macro_data()


def _p_holdings(ctx):
    from portfolio_db import portfolio_db
    import fund_db
    stocks = portfolio_db.get_all_stocks() or []
    funds = fund_db.get_holdings() or []
    return {"股票": [{"code": s.get("code"), "name": s.get("name")} for s in stocks],
            "基金数": len(funds)}


def _p_fund_score(ctx):
    import fund_analysis, fund_data
    code = ctx.get("code", "")
    return {"score": fund_analysis.score_fund(code), "name": fund_data.fund_name(code)}


def _p_rag(ctx):
    from rag.service import semantic_search
    return semantic_search(ctx.get("query") or ctx.get("code") or "", top_k=6)


# 注册表:key → {name, scope, params, fn}。scope=参数需求标签。
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "quote":        {"name": "实时行情", "scope": "stock", "fn": _p_quote},
    "kline":        {"name": "K线摘要(30日)", "scope": "stock", "fn": _p_kline},
    "fundamental":  {"name": "基本面评分", "scope": "stock", "fn": _p_fundamental},
    "forensics":    {"name": "财务排雷", "scope": "stock", "fn": _p_forensics},
    "chan":         {"name": "缠论", "scope": "stock", "fn": _p_chan},
    "chip":         {"name": "筹码分布", "scope": "stock", "fn": _p_chip},
    "signals":      {"name": "策略信号", "scope": "stock", "fn": _p_signals},
    "flow":         {"name": "资金流", "scope": "stock", "fn": _p_flow},
    "rag":          {"name": "RAG语义检索", "scope": "stock", "fn": _p_rag},
    "indices":      {"name": "大盘指数", "scope": "market", "fn": _p_indices},
    "news":         {"name": "财经快讯", "scope": "market", "fn": _p_news},
    "sector":       {"name": "板块强弱", "scope": "market", "fn": _p_sector},
    "lhb":          {"name": "龙虎榜机构", "scope": "market", "fn": _p_lhb},
    "macro":        {"name": "宏观快照", "scope": "market", "fn": _p_macro},
    "holdings":     {"name": "我的持仓", "scope": "portfolio", "fn": _p_holdings},
    "fund_score":   {"name": "基金评分", "scope": "fund", "fn": _p_fund_score},
}


def list_providers() -> List[Dict[str, str]]:
    return [{"key": k, "name": v["name"], "scope": v["scope"]} for k, v in PROVIDERS.items()]


# ============================ 模板渲染 ============================
def _stringify(v: Any) -> str:
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False, indent=1, default=str)[:6000]
    except Exception:
        return str(v)[:6000]


def render(template: str, ctx: Dict, data: Dict, analysts: Dict) -> str:
    """替换 {{ctx.x}} {{data.x}} {{analyst.x}}。未知占位符留空。"""
    def repl(m):
        ns, key = m.group(1), m.group(2)
        if ns == "ctx":
            return str(ctx.get(key, ""))
        if ns == "data":
            return _stringify(data.get(key, ""))
        if ns == "analyst":
            return _stringify(analysts.get(key, ""))
        return m.group(0)
    return re.sub(r"\{\{\s*(ctx|data|analyst)\.([^}\s]+)\s*\}\}", repl, template or "")


# ============================ 执行引擎 ============================
def _llm(system: str, user: str, model: str = None, max_tokens: int = 1500, temperature: float = 0.5):
    from llm_router import get_router
    kwargs = {"temperature": temperature, "max_tokens": max_tokens}
    if model:
        kwargs["model"] = model
    text, provider = get_router().call(
        [{"role": "system", "content": system or "你是A股投研助手,输出简洁中文要点。"},
         {"role": "user", "content": user}], **kwargs)
    return (text or "").strip(), provider


def fetch_data(keys: List[str], ctx: Dict) -> Dict[str, Any]:
    """并发取数据块;每块失败只记 {error}。"""
    keys = [k for k in (keys or []) if k in PROVIDERS]

    def one(k):
        try:
            return k, PROVIDERS[k]["fn"](ctx)
        except Exception as e:
            return k, {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    if not keys:
        return {}
    out = {}
    with ThreadPoolExecutor(max_workers=min(8, len(keys))) as ex:
        for k, v in ex.map(one, keys):
            out[k] = v
    return out


def run_workflow(config: Dict, params: Dict) -> Dict:
    """执行工作流。config 见模块 docstring;params 提供 ctx(如 {code}).
    返回 {ctx, data_keys, analysts:[{name,output,provider}], final, provider, error?}。"""
    ctx = dict(params or {})
    data_keys = config.get("data") or []
    analysts_cfg = config.get("analysts") or []
    synth_cfg = config.get("synthesizer") or {}

    data = fetch_data(data_keys, ctx)

    def run_analyst(a):
        # analyst 默认看自己 inputs 指定的 data;未指定则看全部
        inputs = a.get("inputs") or data_keys
        sub = {k: data.get(k) for k in inputs}
        user = render(a.get("user", ""), ctx, sub, {})
        if not user.strip():
            user = f"请基于以下数据分析:\n{_stringify(sub)}"
        try:
            text, prov = _llm(a.get("system", ""), user, a.get("model"),
                              max_tokens=a.get("max_tokens", 1200))
            return {"name": a.get("name", "分析"), "output": text, "provider": prov}
        except Exception as e:
            return {"name": a.get("name", "分析"), "output": f"(失败: {type(e).__name__}: {str(e)[:100]})", "provider": None}

    analyst_results = []
    if analysts_cfg:
        with ThreadPoolExecutor(max_workers=min(6, len(analysts_cfg))) as ex:
            analyst_results = list(ex.map(run_analyst, analysts_cfg))

    amap = {a["name"]: a["output"] for a in analyst_results}
    final, provider = "", None
    if synth_cfg and synth_cfg.get("user") or synth_cfg.get("system"):
        suser = render(synth_cfg.get("user", ""), ctx, data, amap)
        if not suser.strip():
            suser = "综合以下各分析师意见,给出最终结论:\n" + _stringify(amap)
        try:
            final, provider = _llm(synth_cfg.get("system", ""), suser, synth_cfg.get("model"),
                                   max_tokens=synth_cfg.get("max_tokens", 1500))
        except Exception as e:
            final = f"(综合失败: {type(e).__name__}: {str(e)[:100]})"

    return {"ctx": ctx, "data_keys": data_keys, "analysts": analyst_results,
            "final": final, "provider": provider}


# ============================ 持久化(PG/SQLite via db_compat) ============================
def _conn():
    from db_compat import connect
    return connect(_bootstrap.db_path("ai_workflow.db"))


def _is_pg():
    from db_compat import is_postgres
    return is_postgres()


def init_db():
    pk = "BIGSERIAL PRIMARY KEY" if _is_pg() else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts = "TIMESTAMP DEFAULT CURRENT_TIMESTAMP" if _is_pg() else "TEXT DEFAULT CURRENT_TIMESTAMP"
    conn = _conn()
    cur = conn.cursor()
    cur.execute(f"""CREATE TABLE IF NOT EXISTS ai_workflows (
        id {pk}, name TEXT NOT NULL, scope TEXT, description TEXT,
        config TEXT NOT NULL, created_at {ts}, updated_at {ts})""")
    cur.execute(f"""CREATE TABLE IF NOT EXISTS ai_workflow_runs (
        id {pk}, workflow_id INTEGER, name TEXT, params TEXT, result TEXT, created_at {ts})""")
    conn.commit()
    conn.close()


def list_workflows() -> List[Dict]:
    init_db()
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, scope, description, config FROM ai_workflows ORDER BY updated_at DESC")
    rows = cur.fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            cfg = json.loads(r[4])
        except Exception:
            cfg = {}
        out.append({"id": r[0], "name": r[1], "scope": r[2], "description": r[3], "config": cfg})
    return out


def save_workflow(wf_id, name, scope, description, config) -> int:
    init_db()
    conn = _conn()
    cur = conn.cursor()
    cfg = json.dumps(config, ensure_ascii=False)
    if wf_id:
        cur.execute("""UPDATE ai_workflows SET name=?, scope=?, description=?, config=?,
            updated_at=CURRENT_TIMESTAMP WHERE id=?""", (name, scope, description, cfg, wf_id))
        rid = wf_id
    else:
        cur.execute("""INSERT INTO ai_workflows (name, scope, description, config)
            VALUES (?,?,?,?)""", (name, scope, description, cfg))
        rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def delete_workflow(wf_id):
    init_db()
    conn = _conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM ai_workflows WHERE id=?", (wf_id,))
    conn.commit()
    conn.close()


def list_runs(workflow_id=None, limit=40) -> List[Dict]:
    """运行历史(元数据 + final 预览)。"""
    init_db()
    conn = _conn()
    cur = conn.cursor()
    if workflow_id:
        cur.execute("""SELECT id, workflow_id, name, params, result, created_at FROM ai_workflow_runs
            WHERE workflow_id=? ORDER BY id DESC LIMIT ?""", (workflow_id, limit))
    else:
        cur.execute("""SELECT id, workflow_id, name, params, result, created_at FROM ai_workflow_runs
            ORDER BY id DESC LIMIT ?""", (limit,))
    rows = cur.fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            res = json.loads(r[4]) if r[4] else {}
        except Exception:
            res = {}
        final = (res.get("final") or "")
        out.append({"id": r[0], "workflow_id": r[1], "name": r[2],
                    "params": _safe_json(r[3]), "created_at": str(r[5]),
                    "final_preview": final[:120], "n_analysts": len(res.get("analysts") or [])})
    return out


def get_run(run_id) -> Dict:
    """单次运行完整结果。"""
    init_db()
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, params, result, created_at FROM ai_workflow_runs WHERE id=?", (run_id,))
    r = cur.fetchone()
    conn.close()
    if not r:
        return {}
    return {"id": r[0], "name": r[1], "params": _safe_json(r[2]),
            "result": _safe_json(r[3]), "created_at": str(r[4])}


def _safe_json(s):
    try:
        return json.loads(s) if s else {}
    except Exception:
        return {}


def save_run(workflow_id, name, params, result):
    try:
        init_db()
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""INSERT INTO ai_workflow_runs (workflow_id, name, params, result)
            VALUES (?,?,?,?)""", (workflow_id, name, json.dumps(params, ensure_ascii=False),
                                  json.dumps(result, ensure_ascii=False, default=str)[:200000]))
        conn.commit()
        conn.close()
    except Exception:
        pass


# 种子工作流(首次无数据时给用户起点)
SEED_WORKFLOWS = [
    {
        "name": "个股双层研判(技术+基本面→首席)", "scope": "stock", "description": "示例:并行技术/基本面分析 → 首席综合评级",
        "config": {
            "scope": "stock", "params": ["code"],
            "data": ["quote", "kline", "fundamental", "chan", "chip", "signals"],
            "analysts": [
                {"name": "技术派", "system": "你是A股技术分析师,只看技术面。", "inputs": ["quote", "kline", "chan", "chip", "signals"],
                 "user": "股票 {{ctx.code}}。\n行情:{{data.quote}}\nK线:{{data.kline}}\n缠论:{{data.chan}}\n筹码:{{data.chip}}\n信号:{{data.signals}}\n给出技术面研判:趋势/关键位/买卖点/风险,3-5要点。"},
                {"name": "基本面派", "system": "你是A股基本面分析师。", "inputs": ["quote", "fundamental"],
                 "user": "股票 {{ctx.code}}。\n行情:{{data.quote}}\n基本面:{{data.fundamental}}\n给出估值与质量研判,3-5要点。"},
            ],
            "synthesizer": {"name": "首席", "system": "你是首席策略师,综合多方意见给可执行结论。",
                            "user": "技术派:\n{{analyst.技术派}}\n\n基本面派:\n{{analyst.基本面派}}\n\n请综合给出:评级(买入/持有/减持)、目标价、止损位、一句话核心逻辑、主要风险。"},
        },
    },
    {
        "name": "大盘盘前研判(指数+板块+龙虎+快讯→策略)", "scope": "market", "description": "示例:并行解读市场各面 → 盘前策略",
        "config": {
            "scope": "market", "params": [],
            "data": ["indices", "sector", "lhb", "news"],
            "analysts": [
                {"name": "情绪面", "system": "你是市场情绪分析师。", "inputs": ["indices", "news"],
                 "user": "指数:{{data.indices}}\n快讯:{{data.news}}\n判断当前市场情绪与主要驱动,3要点。"},
                {"name": "资金面", "system": "你是资金面分析师。", "inputs": ["sector", "lhb"],
                 "user": "板块:{{data.sector}}\n龙虎榜机构:{{data.lhb}}\n判断资金主线与机构动向,3要点。"},
            ],
            "synthesizer": {"name": "盘前策略", "system": "你是A股盘前策略师。",
                            "user": "情绪面:\n{{analyst.情绪面}}\n\n资金面:\n{{analyst.资金面}}\n\n给出今日盘前策略:市场判断/主攻方向/规避方向/仓位建议。"},
        },
    },
    {
        "name": "个股排雷+估值研判(财务侦探→风控)", "scope": "stock", "description": "重排雷:财务红旗 + 估值安全边际 → 能否买入",
        "config": {
            "scope": "stock", "params": ["code"],
            "data": ["quote", "fundamental", "forensics", "kline"],
            "analysts": [
                {"name": "财务侦探", "system": "你是财务排雷专家,专找造假红旗与盈利质量问题。", "inputs": ["fundamental", "forensics"],
                 "user": "股票 {{ctx.code}}。\n基本面:{{data.fundamental}}\n排雷:{{data.forensics}}\n列出财务红旗 + 盈利质量(净利vs现金流)评估,3-5点。"},
                {"name": "估值派", "system": "你是估值分析师。", "inputs": ["quote", "fundamental", "kline"],
                 "user": "股票 {{ctx.code}}。\n行情:{{data.quote}}\n基本面:{{data.fundamental}}\nK线:{{data.kline}}\n判断估值贵不贵 + 安全边际,3点。"},
            ],
            "synthesizer": {"name": "风控结论", "system": "你是风控负责人,谨慎为先。",
                            "user": "财务侦探:\n{{analyst.财务侦探}}\n\n估值派:\n{{analyst.估值派}}\n\n给出:是否存在暴雷风险(高/中/低)、估值评级、能否买入及理由。"},
        },
    },
    {
        "name": "游资题材风口(龙虎+板块+快讯→风口)", "scope": "market", "description": "识别游资主攻方向 + 题材主线 → 今日风口与核心标的",
        "config": {
            "scope": "market", "params": [],
            "data": ["lhb", "sector", "news"],
            "analysts": [
                {"name": "游资派", "system": "你是龙虎榜资金分析师,擅长识别游资与机构风格。", "inputs": ["lhb"],
                 "user": "龙虎榜机构买卖:{{data.lhb}}\n识别游资/机构主攻方向与重点个股,3点。"},
                {"name": "题材派", "system": "你是题材主线分析师。", "inputs": ["sector", "news"],
                 "user": "板块:{{data.sector}}\n快讯:{{data.news}}\n提炼今日最强题材主线与催化,3点。"},
            ],
            "synthesizer": {"name": "风口研判", "system": "你是游资风口研判官。",
                            "user": "游资派:\n{{analyst.游资派}}\n\n题材派:\n{{analyst.题材派}}\n\n给出:今日最强风口、核心标的(代码)、参与逻辑、风险与纪律。"},
        },
    },
    {
        "name": "持仓体检(集中度+市场环境→调仓)", "scope": "portfolio", "description": "对当前股票+基金持仓做风险体检与调仓建议",
        "config": {
            "scope": "portfolio", "params": [],
            "data": ["holdings", "indices", "sector"],
            "analysts": [
                {"name": "风险官", "system": "你是组合风险官。", "inputs": ["holdings"],
                 "user": "我的持仓:{{data.holdings}}\n评估集中度/行业暴露/主要风险点,3点。"},
                {"name": "市场官", "system": "你是市场环境分析师。", "inputs": ["indices", "sector"],
                 "user": "大盘:{{data.indices}}\n板块:{{data.sector}}\n当前市场环境对该持仓的影响与机会,2点。"},
            ],
            "synthesizer": {"name": "体检结论", "system": "你是投顾,给可执行建议。",
                            "user": "风险官:\n{{analyst.风险官}}\n\n市场官:\n{{analyst.市场官}}\n\n给出持仓体检结论 + 具体调仓建议(加仓/减仓/保留方向)。"},
        },
    },
    {
        "name": "个股多空辩论(多头vs空头→裁判)", "scope": "stock", "description": "多头与空头各执一词 → 中立裁判权衡定调",
        "config": {
            "scope": "stock", "params": ["code"],
            "data": ["quote", "kline", "chan", "signals", "fundamental", "chip"],
            "analysts": [
                {"name": "多头", "system": "你是坚定多头,只找看涨理由,但必须基于给定数据,不许空话。", "inputs": ["quote", "kline", "chan", "signals", "fundamental"],
                 "user": "股票 {{ctx.code}}。\n行情:{{data.quote}}\nK线:{{data.kline}}\n缠论:{{data.chan}}\n信号:{{data.signals}}\n基本面:{{data.fundamental}}\n给出3条最有力的看涨理由(带数据)。"},
                {"name": "空头", "system": "你是谨慎空头,只找看跌与风险,必须基于给定数据。", "inputs": ["quote", "kline", "chan", "chip", "signals"],
                 "user": "股票 {{ctx.code}}。\n行情:{{data.quote}}\nK线:{{data.kline}}\n缠论:{{data.chan}}\n筹码:{{data.chip}}\n信号:{{data.signals}}\n给出3条最有力的看跌/风险理由(带数据)。"},
            ],
            "synthesizer": {"name": "裁判", "system": "你是中立裁判,权衡多空给出定调。",
                            "user": "多头观点:\n{{analyst.多头}}\n\n空头观点:\n{{analyst.空头}}\n\n作为中立裁判,权衡双方,给出:倾向(偏多/偏空/中性)、核心依据、操作建议与止损。"},
        },
    },
    {
        "name": "基金研判(业绩+定投适配→评级)", "scope": "fund", "description": "对一只基金做业绩/风险 + 定投适配研判",
        "config": {
            "scope": "fund", "params": ["code"],
            "data": ["fund_score"],
            "analysts": [
                {"name": "业绩派", "system": "你是基金业绩与风险分析师。", "inputs": ["fund_score"],
                 "user": "基金 {{ctx.code}} 评分:{{data.fund_score}}\n评估业绩、回撤与风险,3点。"},
                {"name": "定投派", "system": "你是定投策略师。", "inputs": ["fund_score"],
                 "user": "基金 {{ctx.code}} 评分:{{data.fund_score}}\n是否适合定投 + 节奏建议,2点。"},
            ],
            "synthesizer": {"name": "基金结论", "system": "你是基金投顾。",
                            "user": "业绩派:\n{{analyst.业绩派}}\n\n定投派:\n{{analyst.定投派}}\n\n给出综合评级 + 是否纳入定投组合 + 理由。"},
        },
    },
]


def ensure_seeds():
    """按名字幂等灌种子:已存在的不重复,新增模板补进来。"""
    init_db()
    existing = {w["name"] for w in list_workflows()}
    for s in SEED_WORKFLOWS:
        if s["name"] not in existing:
            save_workflow(None, s["name"], s["scope"], s["description"], s["config"])


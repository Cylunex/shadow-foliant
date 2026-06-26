"""
shadow-foliant MCP Server —— 把项目业务能力暴露为 MCP 工具,供 Agent 调用。

设计:不改动任何业务逻辑,只 import 现有纯函数包成 tool(详见 AGENT_SKILL.md)。
仅 A 股(沪深京 6 位代码)。部分工具需 DEEPSEEK_API_KEY / pywencai / PG。

依赖:pip install mcp   (官方 MCP Python SDK,提供 FastMCP)
启动:python mcp_server.py            # stdio 传输,供 Claude/OpenClaw 等接入
     (MCP 客户端配置:command=python, args=[<本文件绝对路径>])
"""

import _bootstrap  # noqa: F401  路径引导(各业务模块在子目录,靠它上 sys.path)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    raise SystemExit("缺少 MCP SDK,请先: pip install mcp")

from typing import List, Dict, Any, Optional

mcp = FastMCP("shadow-foliant")


# ---------------------------------------------------------------------------
# 内部helper:按代码取 K线 DataFrame(分析类工具复用)
# ---------------------------------------------------------------------------
def _fetch_df(code: str, period: str = '1y'):
    from stock_data import StockDataFetcher
    return StockDataFetcher().get_stock_data(code, period)


# =========================== 数据查询 ===========================
@mcp.tool()
def stock_info(code: str) -> Dict[str, Any]:
    """查询A股个股基础信息:名称/现价/涨跌幅/PE/PB/市值/行业。code 为6位代码如 600519。"""
    import datahub
    return datahub.stock_info(code)


@mcp.tool()
def latest_indicators(code: str, period: str = '1y') -> Dict[str, Any]:
    """个股最新技术指标(MA/RSI/MACD/KDJ/BOLL + 通达信 DMI/ATR/TRIX/ROC/CCI/BIAS/WR)。"""
    import datahub
    from stock_data import StockDataFetcher
    df = datahub.kline(code, period)   # 走 datahub:复用磁盘缓存
    if df is None or getattr(df, "empty", True):
        return {"error": "无行情数据"}
    f = StockDataFetcher()
    return f.get_latest_indicators(f.calculate_technical_indicators(df))


@mcp.tool()
def capital_flow(code: str) -> Any:
    """个股资金流向(主力/超大单等)。"""
    import datahub
    return datahub.capital_flow_adata(code)


@mcp.tool()
def north_flow(days: int = 30) -> Any:
    """北向资金近 N 日大盘数据。"""
    import datahub
    return datahub.north_flow(days)


@mcp.tool()
def dragon_tiger_today() -> Any:
    """当日龙虎榜明细。"""
    import datahub
    return datahub.dragon_tiger()


# =========================== 个股分析(纯计算) ===========================
@mcp.tool()
def chan_signal(code: str) -> Dict[str, Any]:
    """缠论分析:分型/笔/中枢/背驰 + 一二三类买卖点 + 中文摘要。"""
    from chan_theory import analyze_chan
    df = _fetch_df(code)
    if isinstance(df, dict):
        return df
    return analyze_chan(df, code)


@mcp.tool()
def stock_risk(code: str, beta: float = 1.0) -> Dict[str, Any]:
    """量化风险:VaR/CVaR/最大回撤/夏普/蒙特卡洛/压力情景 + 摘要。"""
    from stress_testing import analyze_risk
    df = _fetch_df(code)
    if isinstance(df, dict):
        return df
    return analyze_risk(df, beta=beta)


@mcp.tool()
def fundamental_score(code: str) -> Dict[str, Any]:
    """基本面 8 因子加权评分(0-100)+ 等级 + action(PE/PEG/PB/ROE/增速/负债/股息/现金流)。"""
    from fundamental_scoring import score_one
    return score_one(code)


@mcp.tool()
def financial_forensics(code: str) -> Dict[str, Any]:
    """财务排雷:杜邦分解 + 盈利质量(净利vs现金流)+ 造假红旗清单(基于可得指标)。"""
    from fundamental_scoring import collect_factors
    from financial_forensics import analyze_forensics
    return analyze_forensics(collect_factors(code) or {})


@mcp.tool()
def stock_context(code: str, groups: Optional[List[str]] = None) -> Dict[str, Any]:
    """一次性聚合某股多域 context(为 Agent 设计)。
    groups 默认 ['base','kline_technical','chan_theory','fund_flow','fundamentals','risk'];
    可选还含 sentiment/chipset/macro_us。"""
    from agent_tool_groups import collect
    g = groups or ['base', 'kline_technical', 'chan_theory', 'fund_flow', 'fundamentals', 'risk']
    return collect(g, code)


# =========================== 选股 ===========================
@mcp.tool()
def multi_factor_screen(index_code: str = '000300', n: int = 15, limit: int = 60) -> Dict[str, Any]:
    """多因子横截面选股:股票池=指数成分∪行业龙头,返回综合分 TopN。
    index_code: 000300沪深300/000905中证500;limit 控制实算只数(防慢)。"""
    from multi_factor_screener import screen_index
    return screen_index(index_code=index_code, n=n, limit=limit, add_sector_leaders=True)


@mcp.tool()
def backtest_strategy(code: str, strategy: str = 'enter', hold_days: int = 10,
                      stop_pct: float = 8.0, target_pct: float = 15.0,
                      lookback_days: int = 365) -> Dict[str, Any]:
    """单股策略回测(双收益):触发后持有N日的胜率/平均收益 + 带止损止盈的纪律收益/触发率。
    strategy ∈ parking_apron/high_tight_flag/breakthrough_platform/turtle_trade/keep_increasing/
    backtrace_ma250/enter/climax_limitdown/low_backtrace_increase/low_atr。
    用于验证某策略在该股近段历史的有效性 + 止损止盈位是否合理。"""
    from backtest_engine import backtest_one
    from datetime import datetime, timedelta
    df = _fetch_df(code, '2y')
    if isinstance(df, dict):
        return df
    end = datetime.now().strftime('%Y-%m-%d')
    start = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
    r = backtest_one(code, strategy, df, start, end, hold_days=hold_days,
                     stop_pct=stop_pct, target_pct=target_pct)
    return {'symbol': code, 'strategy': strategy, 'period': r.get('period'),
            'summary': r.get('summary', {})}


@mcp.tool()
def portfolio_backtest(codes: List[str], strategy: str = 'enter',
                       start: str = '', end: str = '',
                       hold_days: int = 10, stop_pct: float = 8.0, target_pct: float = 15.0,
                       max_positions: int = 5, initial_cash: float = 1000000.0,
                       allocation: str = 'equal', use_live: bool = False,
                       benchmark: str = '000300', attribution: bool = False) -> Dict[str, Any]:
    """组合级回测(实盘口径,优于单股回测):给一组股票按**一个现金账户**逐日撮合——
    并发持仓上限 max_positions、每根bar先卖(止损/止盈/到期)再买、含佣金+印花税+滑点、
    无前视(信号次日开盘建仓)。输出组合 CAGR/最大回撤/夏普/年化波动/胜率/盈亏比/净值曲线,
    并对比沪深300(超额收益)。
    codes: 6位代码列表(如 ['600519','000858']);start/end 空=近2年。
    allocation: equal等权 / signal按信号强度;use_live=True 用策略基因组进化出的最优参数集。
    attribution=True 附**分层归因**(交易归因/β回归α显著性/市况/蒙特卡洛)判断业绩是真本事还是运气。
    单股回测(backtest_strategy)各信号独立全仓→系统性高估;要评估"这套策略实际能赚多少"用本工具。"""
    from datetime import datetime, timedelta
    from portfolio_backtest import portfolio_backtest as _pbt, portfolio_backtest_live as _pbt_live
    stocks = [(c, '') for c in (codes or []) if c]
    if not stocks:
        return {'error': '请提供股票代码列表 codes'}
    end = end or datetime.now().strftime('%Y-%m-%d')
    start = start or (datetime.now() - timedelta(days=730)).strftime('%Y-%m-%d')
    common = dict(hold_days=hold_days, stop_pct=stop_pct, target_pct=target_pct,
                  max_positions=max_positions, initial_cash=initial_cash,
                  allocation=allocation, benchmark=benchmark or None, curve_points=60)
    r = (_pbt_live(stocks, start, end, **common) if use_live
         else _pbt(stocks, start, end, strategy_id=strategy, **common))
    # MCP 返回精简:摘要 + 配置 + 最近10笔(完整曲线对 Agent 噪声大)
    out = {'summary': r.get('summary', {}), 'config': r.get('config', {}),
           'recent_trades': r.get('trades', [])[-10:]}
    if attribution and not r.get('error'):
        try:
            from backtest_attribution import attribute
            out['attribution'] = attribute(r)
        except Exception as ae:
            out['attribution'] = {'ok': False, 'error': str(ae)[:120]}
    return out


@mcp.tool()
def dcf_valuation(code: str, growth: float = 0.10, years: int = 5,
                  terminal: float = 0.03, discount: float = 0.10) -> Dict[str, Any]:
    """两阶段 DCF 内在价值估值(补 PE/PB 之外的绝对估值)。自市值/现价/PE 推导 股本与基准FCF(净利近似)。
    growth 高速期年增速(0.10=10%)、years 高速年数、terminal 永续增速、discount 折现率/WACC。
    返回每股内在价值/安全边际/看法 + 永续占比 + 折现×永续 敏感性表。判断"现价相对内在价值贵不贵"。"""
    import datahub, dcf_valuation as dcf
    info = datahub.stock_info(code) or {}
    mc, px, pe = info.get('market_cap'), info.get('current_price'), info.get('pe_ratio')
    if not (mc and px and px > 0):
        return {'error': '无法获取市值/现价', 'code': code}
    if not (pe and pe > 0):
        return {'error': '该股 PE 不可用(亏损或缺数据),无法用净利润近似 FCF', 'code': code}
    r = dcf.analyze_dcf(base_fcf=mc / pe, shares=mc / px, current_price=px,
                        high_growth=growth, high_years=int(years), terminal_growth=terminal,
                        discount_rate=discount, fcf_is_proxy=True)
    r['code'], r['name'] = code, info.get('name')
    return r


@mcp.tool()
def chip_distribution(code: str) -> Dict[str, Any]:
    """筹码分布(由K线本地估算):获利盘比例/平均成本/90%(70%)成本区间/集中度/当前价vs均价。
    判断底部套牢盘、上方套牢压力、筹码集中度。"""
    from chip_distribution import chip_distribution as _cyq
    df = _fetch_df(code, '1y')
    if isinstance(df, dict):
        return df
    return _cyq(df)


@mcp.tool()
def stock_signals(code: str) -> Dict[str, Any]:
    """稳健买点/风险信号 + 行情阶段(防接飞刀):
    regime(trending_up/down/sideways/volatile)、缩量回踩、底部放量、情绪顶预警。"""
    from strategy_signals import shrink_pullback, bottom_volume, emotion_top_warning, detect_regime
    df = _fetch_df(code, '1y')
    if isinstance(df, dict):
        return df
    return {
        'regime': detect_regime(df),
        'shrink_pullback': shrink_pullback(df),
        'bottom_volume': bottom_volume(df),
        'emotion_top_warning': emotion_top_warning(df),
    }


@mcp.tool()
def screen_recipe(recipe: str, codes: List[str]) -> Dict[str, Any]:
    """对给定股票池跑内置选股配方。
    recipe ∈ 主升浪起涨/超跌反弹/强势突破/低估值蓝筹/缠论一买/均线金叉起步;
    codes 为候选股票代码列表(如持仓或自选)。"""
    from screener_engine import screen_recipe as _sr, RECIPES
    if recipe not in RECIPES:
        return {'error': f'未知配方,可选: {list(RECIPES.keys())}'}
    return _sr(recipe, codes, verbose=False)


# =========================== 组合 / 持仓 / 成交 ===========================
@mcp.tool()
def list_holdings() -> List[Dict[str, Any]]:
    """列出当前持仓。"""
    from portfolio_db import portfolio_db
    return portfolio_db.get_all_stocks() or []


@mcp.tool()
def import_holdings(rows: List[Dict[str, Any]], mode: str = 'upsert') -> Dict[str, Any]:
    """批量导入持仓。rows: [{code,name,cost_price,quantity,note}];mode: upsert/add/replace。"""
    from portfolio_db import portfolio_db
    return portfolio_db.bulk_import(rows, mode=mode, source='mcp')


@mcp.tool()
def import_trades(rows: List[Dict[str, Any]], update_position: bool = True) -> Dict[str, Any]:
    """批量导入成交记录(真实买卖流水),默认**自动更新持仓**(买入加仓重算均价/卖出减仓,变动记录带成交时间)。
    rows: [{code, name, trade_type(买入/卖出), quantity, price, amount?, trade_time?, note?, commission?, tax?}]。"""
    from portfolio_db import portfolio_db
    return portfolio_db.import_trades(rows, update_position=update_position)


@mcp.tool()
def list_trades(code: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    """查询已导入的成交记录,按时间倒序。"""
    from portfolio_db import portfolio_db
    return portfolio_db.get_trades(code, limit)


@mcp.tool()
def portfolio_diagnosis() -> Dict[str, Any]:
    """组合诊断:集中度(HHI/单票/前三)、行业集中度、再平衡建议(基于当前持仓市值)。"""
    from portfolio_db import portfolio_db
    from portfolio_diagnosis import diagnose_portfolio
    holds = portfolio_db.get_all_stocks() or []
    rows = []
    for h in holds:
        mv = (h.get('cost_price') or 0) * (h.get('quantity') or 0)
        if mv > 0:
            rows.append({'symbol': h.get('code'), 'market_value': mv, 'sector': h.get('industry')})
    if not rows:
        return {'available': False, 'error': '无有效持仓市值'}
    return diagnose_portfolio(rows)


@mcp.tool()
def portfolio_trading_habits(since_days: int = 90) -> Dict[str, Any]:
    """持仓交易习惯洞察(纯库读):持有时长分布、交易频次/买卖比/日均、近N天变动时间线与最活跃股。
    看清"我是短炒还是长持、买卖是否频繁、最近在折腾哪些股"。"""
    import portfolio_insights as pi
    return {
        'duration': pi.holding_duration_distribution(),
        'frequency': pi.trading_frequency_analysis(since_days=since_days),
        'timeline': pi.portfolio_change_timeline(since_days=since_days, limit=200),
    }


@mcp.tool()
def portfolio_ai_diagnose() -> Dict[str, Any]:
    """AI 持仓诊断(LLM):综合 估值/交易频次/持有时长/变动 给交易习惯+风险+改进建议+风险/纪律评分。
    ⚠️ 需 LLM key,失败仍返回 context(规则报表)。回答"我的持仓/交易习惯有什么问题、怎么改"。"""
    import portfolio_insights as pi
    return pi.diagnose_portfolio()


@mcp.tool()
def portfolio_action_signals(limit: int = 40) -> Dict[str, Any]:
    """持仓操作信号:加仓审核(跌幅触发→仓位/加仓次数/基本面硬约束 approve/reject)+
    减仓信号(30/60/100%阶梯止盈 + 跌破MA20减半/MA60清仓 + 情绪过热)。回答"现在该加/减仓哪些"。
    limit 只扫市值前 N 只(默认40,避冷门小仓卡顿)。"""
    import position_guardian as pg
    import position_profit_taker as pt
    add = pg.evaluate_all_triggered(limit=limit, with_fundamental=False)
    reduce = pt.evaluate_all(limit=limit)
    return {'add': add, 'reduce': reduce, 'add_count': len(add), 'reduce_count': len(reduce)}


@mcp.tool()
def portfolio_classify(limit: int = 30, with_fundamental: bool = False) -> Dict[str, Any]:
    """持仓自动分级:健康🟢/观察🟡/警报🔴/数据不足⚪,基于 持仓盈亏+趋势(MA)+看跌反转形态。
    limit 只扫市值最大前 N 只(默认30,已并发)。with_fundamental=True 才加基本面评分
    (每只2-4s且依赖pywencai/F10,弱网/未配置返回N/A纯耗时,默认关→快)。锁定该重点关注/可能止损的持仓。"""
    import portfolio_classifier as pc
    by = pc.classify_all(limit=limit, with_fundamental=with_fundamental)
    return {'counts': {k: len(by.get(k, [])) for k in ('healthy', 'watch', 'alert', 'na')},
            'by_class': by}


# =========================== 多智能体深度分析(⚠️ 耗 token) ===========================
@mcp.tool()
def deep_analysis(code: str) -> Dict[str, Any]:
    """⚠️ 重工具:多智能体深度分析(技术+基本面+风险 → 讨论 → 决策含投资哲学透镜)。
    单次多 LLM 调用、数十秒、耗 token、需 DEEPSEEK_API_KEY。仅在深度研判时用。"""
    from stock_data import StockDataFetcher
    from ai_agents import StockAnalysisAgents
    f = StockAnalysisAgents()
    fetch = StockDataFetcher()
    info = fetch.get_stock_info(code)
    df = fetch.get_stock_data(code)
    if isinstance(df, dict):
        return {'error': '无行情数据'}
    ind = fetch.get_latest_indicators(fetch.calculate_technical_indicators(df))
    res = {
        'technical': f.technical_analyst_agent(info, df, ind),
        'fundamental': f.fundamental_analyst_agent(info, fetch.get_financial_data(code), None),
        'risk_management': f.risk_management_agent(info, ind, None, stock_data=df),
    }
    disc = f.conduct_team_discussion(res, info)
    dec = f.make_final_decision(disc, info, ind)
    return {'decision': dec, 'discussion': disc[:2000]}


# =========================== 妙想(东财 AI SaaS)外部服务 — 第二意见/外部数据 ===========================
# ⚠️ 三方 SaaS:问句发往东财服务器;需 .env 配 EM_API_KEY(否则用 demo key,易限流)。
# 定位:与自研多智能体互补的"第二意见 / 外部成品",不是核心决策来源。
@mcp.tool()
def mx_stock_diagnosis(question: str) -> Dict[str, Any]:
    """妙想·A股个股综合诊断(基本面+资金+风险,东财成品)。
    作自研多智能体诊股的【第二意见/交叉验证】。question 用自然语言,如「诊断下贵州茅台」。"""
    from miaoxiang import stock_diagnosis
    return stock_diagnosis(question)


@mcp.tool()
def mx_ask(question: str) -> Dict[str, Any]:
    """妙想·七合一金融问答(数据/资讯/宏观/选股/百科/分析/热点的总入口)。
    通用自然语言提问,适合数据/资讯类杂问;深度个股决策仍用自研 deep_analysis。"""
    from miaoxiang import finance_ask
    return finance_ask(question)


@mcp.tool()
def mx_hotspot(question: str = "今日A股市场热点") -> Dict[str, Any]:
    """妙想·A股市场热点发现(资讯/事件/热股)。与自研 news_flow 热点互为交叉验证。"""
    from miaoxiang import hotspot
    return hotspot(question)


@mcp.tool()
def mx_comparable(question: str) -> Dict[str, Any]:
    """妙想·可比公司/同业横向分析(经营+估值)。如「比亚迪和同业可比公司估值对比」。"""
    from miaoxiang import comparable
    return comparable(question)


@mcp.tool()
def mx_finance_search(query: str) -> Dict[str, Any]:
    """妙想·自然语言搜公告/研报/新闻/政策(全球)。补强项目资讯源。"""
    from miaoxiang import finance_search
    return finance_search(query)


@mcp.tool()
def mx_macro(query: str) -> Dict[str, Any]:
    """妙想·查中国宏观数据(GDP/CPI/PMI/货币/财政/贸易/就业…)。补项目宏观偏美国 FRED 的空白。"""
    from miaoxiang import macro_data
    return macro_data(query)


@mcp.tool()
def mx_industry_report(query: str) -> Dict[str, Any]:
    """妙想·行业深度研报(东财库)。如「锂电池行业研究报告」。强化板块/产业链研究。"""
    from miaoxiang import industry_report
    return industry_report(query)


# =========================== 基金(长期/定投) ===========================
@mcp.tool()
def fund_info(code: str) -> Dict[str, Any]:
    """场外基金基础信息:简称/类型 + 最新净值。code 为6位基金代码如 000001。"""
    import fund_data
    return {
        'code': str(code).zfill(6),
        'name': fund_data.fund_name(code),
        'type': fund_data.fund_type(code),
        'latest_nav': fund_data.latest_nav(code),
    }


@mcp.tool()
def fund_nav_history(code: str, start: Optional[str] = None, end: Optional[str] = None) -> Any:
    """基金历史净值(单位/累计/日增长率)。start/end 形如 '2023-01-01'。返回记录列表。"""
    import fund_data
    df = fund_data.get_nav_history(code, start, end)
    if df is None or df.empty:
        return {'error': '净值获取失败', 'code': code}
    df = df.copy()
    df['date'] = df['date'].dt.strftime('%Y-%m-%d')
    return df.to_dict('records')


@mcp.tool()
def fund_metrics(code: str) -> Dict[str, Any]:
    """基金净值指标:年化/最大回撤/夏普/卡玛/年化波动/下行风险。"""
    import fund_data, fund_metrics as fm
    df = fund_data.get_nav_history(code)
    if df is None or df.empty:
        return {'error': '净值获取失败', 'code': code}
    return fm.evaluate(df)


@mcp.tool()
def fund_score(code: str, with_extras: bool = False) -> Dict[str, Any]:
    """基金综合评价打分卡(0-100 + 等级 + 建议),长期/定投视角(业绩+风控+稳定性)。
    with_extras=True 额外接入同类排名分位(雪球,需联网较慢)。"""
    import fund_analysis
    return fund_analysis.score_fund(code, with_extras=with_extras)


@mcp.tool()
def import_fund_transactions(rows: List[Dict[str, Any]], update_position: bool = True,
                             skip_existing: bool = False) -> Dict[str, Any]:
    """批量导入基金申赎/定投流水(按日期升序应用→移动加权成本正确)。rows 每项键(中英任一):
    code/代码(必), txn_type/类型(申购|定投|赎回|买入|卖出,默认申购), nav/净值(必),
    amount/金额 或 shares/份额(买入给amount,赎回给shares), fee/费用, trade_date/日期, name/名称, note/备注。
    skip_existing=True 按(code,日期,source)跳过已存在防重复。⚠️ 默认增量叠加,勿对同批重复导入。
    返回 {imported, skipped, errors}。"""
    import fund_db
    return fund_db.import_transactions(rows, update_position=update_position, skip_existing=skip_existing)


@mcp.tool()
def import_fund_plans(rows: List[Dict[str, Any]], dedup: bool = True) -> Dict[str, Any]:
    """批量导入基金定投计划。rows 每项键(中英任一):code/代码(必), amount/金额(必),
    period/周期(monthly|weekly|daily,默认monthly), day_of/扣款日(默认1),
    strategy/策略(normal|valuation|value_avg,默认normal), name/名称(缺则按code补),
    target_profit_pct/止盈%(可选), auto_record/自动记账(可选), note/备注(可选)。
    dedup=True 跳过已存在同 code 的计划。返回 {imported, skipped, errors}。"""
    import fund_db
    return fund_db.import_plans(rows, dedup=dedup)


@mcp.tool()
def fund_dca_backtest(code: str, amount: float = 1000, period: str = 'monthly',
                      day: int = 1, fee_rate: float = 0.0015, strategy: str = 'normal',
                      start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, Any]:
    """定投回测:历史净值上模拟定投,输出投入/份额/市值/收益/年化IRR/最大回撤,对比一次性买入。
    period: daily/weekly/biweekly/monthly;strategy: normal(定期定额)/valuation(估值智能定投,
    低估多投高估暂停)/value_avg(价值平均法)。返回不含逐期明细以省篇幅。"""
    import fund_data, fund_dca
    df = fund_data.get_nav_history(code, start, end)
    if df is None or df.empty:
        return {'error': '净值获取失败', 'code': code}
    r = fund_dca.dca_backtest(df, amount, period, day=day, fee_rate=fee_rate, strategy=strategy)
    r.pop('trades', None)
    r.pop('equity_curve', None)
    return r


@mcp.tool()
def fund_index_valuation(index: str = '沪深300', years: Optional[int] = None) -> Dict[str, Any]:
    """宽基指数估值分位(滚动PE)→ 估值档位 + 定投倍数(低估多投/高估暂停)。
    index: 上证50/沪深300/中证500/中证1000/创业板指/科创50。驱动估值定投择时。"""
    import fund_valuation
    r = fund_valuation.index_pe_percentile(index, years)
    return r if r else {'error': '估值获取失败', 'index': index}


@mcp.tool()
def fund_screen(fund_type: str = '股票型', sort_by: str = 'r_1y', top_n: int = 20,
                min_1y: Optional[float] = None, min_3y: Optional[float] = None,
                max_fee: Optional[float] = None) -> Any:
    """基金同类排行筛选。fund_type: 全部/股票型/混合型/债券型/指数型/QDII/LOF/FOF;
    sort_by: r_1w/r_1m/r_3m/r_6m/r_1y/r_2y/r_3y/r_ytd/r_since;可加近1年/近3年收益%下限、手续费%上限。"""
    import fund_screener
    return fund_screener.screen_funds(fund_type, sort_by, top_n, min_1y, min_3y, max_fee)


@mcp.tool()
def fund_portfolio_diagnose(with_overlap: bool = False) -> Dict[str, Any]:
    """诊断我的基金组合:大类/类型配置、集中度(HHI/top1/top3)、(可选)重仓股穿透重叠 + 建议。"""
    import fund_db, fund_portfolio
    fund_db.init_db()
    holdings = fund_db.get_holdings()
    if not holdings:
        return {'error': '无持有基金'}
    # 用库内净值算市值,避免 diagnose 内逐只 latest_nav 联网
    navs = fund_db.get_latest_navs([str(h.get('code')) for h in holdings])
    enriched = [{**h, 'market_value': round(float(h.get('shares') or 0) *
                float((navs.get(str(h.get('code'))) or {}).get('unit_nav') or h.get('cost_nav') or 0), 2)}
                for h in holdings]
    return fund_portfolio.diagnose(enriched, with_overlap=with_overlap)


@mcp.tool()
def fund_detail(code: str) -> Dict[str, Any]:
    """单只基金档案:前十大重仓股(最新季度,占净值比)+ 评级摘要(基金经理/公司/各机构星级/费率)+ 基本信息
    (成立时间/规模/投资目标等)。看清一只基金"持什么、谁管、几星、多大"。"""
    import fund_data
    return {'top_holdings': fund_data.top_holdings(code, 10),
            'rating': fund_data.rating_summary(code),
            'basic': fund_data.get_fund_basic(code)}


@mcp.tool()
def fund_compare(codes: List[str], lookback_days: Optional[int] = None) -> Dict[str, Any]:
    """多只基金并排对比(同一共同时间窗,公平可比):年化/总收益/最大回撤/夏普/卡玛/波动。
    codes 为基金代码列表(2-6 只);lookback_days 只比最近 N 天(缺省=最大重叠区间)。选基金时用。"""
    import fund_analysis
    if not codes or len(codes) < 2:
        return {'error': '请至少提供 2 只基金代码'}
    r = fund_analysis.compare_funds(codes, lookback_days=lookback_days, curve_points=0)
    for f in r.get('funds', []):
        f.pop('curve', None)   # MCP 省去逐点曲线
    return r


@mcp.tool()
def fund_ai_panel(code: str) -> Dict[str, Any]:
    """基金多角色 AI 研判面板(业绩/风险/定投适配 三角色 + 综合结论)。⚠️ 需 LLM key、耗 token、数秒。"""
    import fund_analysis
    return fund_analysis.ai_research_panel(code)


@mcp.tool()
def asset_overview() -> Dict[str, Any]:
    """股票 + 基金 大类资产合并视图(成本口径):各大类金额与占比。跨持仓总览。"""
    import fund_portfolio
    return fund_portfolio.combined_asset_view()


@mcp.tool()
def portfolio_stress_scenario(scenario_key: str = 'index_crash_10pct',
                              include_funds: bool = True) -> Dict[str, Any]:
    """对(股票+基金)组合跑命名宏观情景压力测试。scenario_key 可选:
    rate_hike_50bp/rate_cut_50bp/cny_depreciation_2pct/index_crash_10pct/index_rally_5pct/
    liquidity_crisis/semiconductor_crash/consumer_boom。返回组合损益 + 最差持仓。"""
    import scenario_stress
    pos = scenario_stress.build_portfolio_positions(include_funds=include_funds)
    if not pos:
        return {'error': '无持仓'}
    return scenario_stress.stress_test(pos, scenario_key)


@mcp.tool()
def dual_horizon_reco(codes: List[str]) -> Dict[str, Any]:
    """AI 双层推荐:对候选股池产出短线(动量,≥70)与长期(基本面,≥75)两层推荐(严格JSON)。
    候选池横向初筛,轻量批量。⚠️ 需 LLM key、耗 token。codes 为6位代码列表。"""
    import dual_horizon_reco as dhr
    return dhr.recommend(codes)


@mcp.tool()
def daily_pnl(days: int = 30) -> Dict[str, Any]:
    """组合每日收益(股票+基金合并):最新一日盈亏 + 本月累计 + 近N日区间累计/胜率/最佳最差日 + 序列。
    数据来自盘后 daily_pnl_snapshot(收盘口径)。回答"今天/本月赚了多少"用这个。"""
    from portfolio.daily_pnl import get_summary, get_recent
    return {"summary": get_summary(max(days, 60)), "series": get_recent(days)}


@mcp.tool()
def convertible_bond_screen(top_n: int = 20, max_price: float = 135.0,
                            max_premium: float = 40.0, min_rating: str = 'A+') -> Any:
    """可转债"双低"选债(双低=现价+转股溢价率,越低越好,兼顾债底保护+跟涨弹性)。
    按双低值升序取 TopN,默认护栏:价≤135、溢价率≤40%、剩余规模≥1亿、评级≥A+。返回 list[dict]。"""
    from convertible_bond import screen_double_low
    return screen_double_low(top_n=top_n, max_price=max_price,
                             max_premium=max_premium, min_rating=min_rating)


@mcp.tool()
def stock_portfolio_curve(limit: int = 90) -> Any:
    """股票组合净值快照曲线(每日总市值 + 浮盈%)。由盘后任务 portfolio_indicator_snapshot 落点。"""
    import portfolio_snapshot
    return portfolio_snapshot.get_snapshots(limit)


@mcp.tool()
def portfolio_stress_all(include_funds: bool = True) -> Any:
    """对(股票+基金)组合跑全部命名情景,按损益从坏到好排序(压力体检)。"""
    import scenario_stress
    pos = scenario_stress.build_portfolio_positions(include_funds=include_funds)
    if not pos:
        return {'error': '无持仓'}
    return [{'scenario': r['scenario'], 'total_pnl_pct': r['total_pnl_pct'],
             'total_pnl': r['total_pnl']} for r in scenario_stress.stress_all(pos)]


@mcp.tool()
def fund_ai_research(code: str) -> Dict[str, Any]:
    """基金 AI 研判(是否适合长期/定投 + 风险点 + 节奏建议)。⚠️ 需 LLM key、耗 token、数秒。"""
    import fund_analysis
    return fund_analysis.ai_research(code)


@mcp.tool()
def fund_holdings() -> Any:
    """我的基金持有列表(份额/成本净值)。"""
    import fund_db
    fund_db.init_db()
    return fund_db.get_holdings()


# =========================== 向量语义检索(RAG) ===========================
@mcp.tool()
def semantic_search(query: str, top_k: int = 8, source_types: Optional[List[str]] = None) -> Any:
    """语义检索本地知识库(历史分析/新闻/推荐/研报):BGE-M3 嵌入 → pgvector 余弦召回 → TEI rerank 精排。
    source_types 可选过滤:analysis/news/reco/report。⚠️ 需嵌入(Ollama)+rerank(TEI)+pgvector 在线;
    任一不可用返回空(不影响其它功能)。返回 [{score,source_type,title,content,meta}]。"""
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rag'))
        import service
        hits = service.semantic_search(query, top_k=top_k, source_types=source_types)
        return [{'score': h.get('score'), 'source_type': h['source_type'],
                 'title': h.get('title'), 'content': (h.get('content') or '')[:300],
                 'meta': h.get('meta')} for h in hits]
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}', 'hits': []}


# =========================== 任务触发(手动跑定时任务) ===========================
_TASKS = {
    'morning_strategy':         ('📊 晨间市场报告(AI研判+昨日收益)', '09:00'),
    'fund_dca_reminder':        ('📌 定投提醒', '08:55'),
    'fund_valuation_signal':    ('📈 基金估值信号', '09:05'),
    'unified_selection':        ('🎯 综合选股 TOP15(进化参数+来源标签)', '09:45'),
    'morning_portfolio':        ('☀️ 早盘持仓分析(买卖提示+浮盈+异动)', '09:50'),
    'mx_selection_review':      ('🔍 妙想第二意见', '10:30'),
    'noon_report':              ('☀️ 午间报告', '12:00'),
    'ai_rec_check':             ('🤖 AI推荐检查', 'every:30m'),
    'stock_monitor_check':      ('👀 股价监控检查', 'every:5m'),
    'afternoon_portfolio':      ('📊 尾盘持仓分析', '14:30'),
    'portfolio_indicator_snapshot': ('📸 组合指标快照', '15:45'),
    'daily_market_snapshot':    ('📷 每日市场快照', '15:50'),
    'factor_collection':        ('🧬 因子收集', '15:55'),
    'dragon_tiger_archive':     ('🐉 龙虎榜归档', '16:00'),
    'daily_backtest':           ('📐 盘后策略回测', '16:30'),
    'mx_daily_analysis':        ('🌙 妙想收盘复盘', '17:00'),
    'daily_pnl_snapshot':       ('💰 当日盈亏快照', '22:30'),
    'fund_nav_refresh':         ('🔄 基金净值刷新', '22:00'),
    'fund_target_check':        ('🎯 基金目标检查', '22:05'),
    'pg_backup':                ('💾 数据库备份', '02:00'),
    'rag_ingest':               ('📚 知识库更新', '02:30'),
    'weekly_analysis':          ('📊 周日持仓综合周报', '周日 15:00'),
    'wf_weekly_backtest':       ('⏪ 每周回测', '周日 20:00'),
    'weekly_db_cleanup':        ('🧹 每周数据清理', '周一 03:00'),
    'ai_eval_weekly':           ('📈 AI推荐周评估', '周一 09:30'),
    # 2026-06-12 任务整合:晨报并入 morning_strategy;预热/风险/形态并入 portfolio_indicator_snapshot;
    # 策略扫描→daily_backtest尾部、候选池→unified_selection尾部、止盈阶梯→afternoon_portfolio尾部、
    # 加仓审核→stock_monitor_check尾部(均受原开关控制);其余冗余任务已删除。
}


# 注册名 → 任务函数名(少数不等于 task_{name} 的别名,与 webui 一致)
_JOB_FN_ALIAS = {'wf_weekly_backtest': 'task_weekly_backtest'}


def _run_task(name: str):
    """导入并执行指定任务函数(同步,返回结果)。.env 已由顶部 import _bootstrap 加载。
    2026-06-12 修:原 `_bootstrap.bootstrap()` 是不存在的函数(必 AttributeError),手动触发从来跑不通。"""
    from jobs import jobs_hub
    fn = getattr(jobs_hub, _JOB_FN_ALIAS.get(name, f'task_{name}'), None)
    if fn is None or not callable(fn):
        return {'error': f'未知任务: {name}'}
    try:
        fn()
        return {'ok': True, 'task': name}
    except Exception as e:
        import traceback
        return {'ok': False, 'task': name, 'error': str(e), 'trace': traceback.format_exc()}


@mcp.tool()
def list_tasks() -> Dict[str, Any]:
    """列出所有可手动触发的定时任务(名称+描述+计划时间)。"""
    return {
        'count': len(_TASKS),
        'tasks': [
            {'name': k, 'description': v[0], 'schedule': v[1]}
            for k, v in _TASKS.items()
        ]
    }


@mcp.tool()
def trigger_task(task_name: str) -> Dict[str, Any]:
    """手动触发指定的定时任务。task_name 来自 list_tasks 返回的 name 字段。
    常用: morning_strategy(晨间报告,含昨日收益+持仓买卖提示), unified_selection(综合选股),
    morning_portfolio(早盘持仓), afternoon_portfolio(尾盘持仓), daily_pnl_snapshot(当日盈亏),
    fund_nav_refresh(基金净值), mx_daily_analysis(收盘复盘)。"""
    if task_name not in _TASKS:
        return {'error': f'无效任务名: {task_name}，请用 list_tasks 查看可用任务'}
    desc, schedule = _TASKS[task_name]
    return _run_task(task_name)


# =========================== 市场上下文 + 观测 ===========================
@mcp.tool()
def market_indices() -> Any:
    """主要大盘指数实时:上证/深证/创业板/科创50/沪深300/恒生(名/点位/涨跌额/涨跌%)。"""
    import datahub
    return datahub.indices()


@mcp.tool()
def sector_board(sector_type: str = 'industry', top_n: int = 15) -> Any:
    """板块涨跌排名(强弱)。sector_type: industry 行业 / concept 概念。返回 {top,bottom,total}。"""
    import datahub
    return datahub.sector_ranking(sector_type=sector_type, top_n=top_n)


@mcp.tool()
def market_news(page_size: int = 30) -> Any:
    """全市场财经快讯(东财全球快讯→财联社兜底)。list[dict] 键:title/content/time/url。"""
    import datahub
    return datahub.market_news(page_size)


@mcp.tool()
def stock_news(code: str, page_size: int = 20) -> Any:
    """个股新闻。code 为6位代码。list[dict] 键:title/time/url 等。"""
    import datahub
    return datahub.stock_news(code, page_size)


@mcp.tool()
def portfolio_performance() -> Dict[str, Any]:
    """组合绩效:TWR(时间加权)/XIRR(资金加权年化)/波动/最大回撤/夏普/盈亏归因。全用净值快照+成交,无外部拉取。"""
    from performance import summary
    return summary()


@mcp.tool()
def datahub_health() -> Dict[str, Any]:
    """数据层观测:各外部源健康度(成功率/延迟/冷却)+ 三级缓存命中统计。排查"数据为何异常/慢"时用。"""
    import datahub
    return {'sources': datahub.source_stats(), 'cache': datahub.cache_stats()}


@mcp.tool()
def llm_token_usage(days: int = 30) -> Dict[str, Any]:
    """LLM Token 用量遥测:近 N 天总量 + 按 model/call_type/天分布 + 最近调用。看多智能体烧了多少 token、走了哪个 provider。"""
    import llm_usage
    return llm_usage.summary(days=days)


@mcp.tool()
def recommendation_winrate(dimension: str = 'source', days: int = 90) -> Dict[str, Any]:
    """AI 推荐胜率按维度分桶(dimension: source/confidence/horizon/outcome/month)。回答"哪个来源/哪种信心度/哪种持有周期真正赚钱"。⚠️需 PG。"""
    from ai_evaluation import evaluate_by, format_buckets, VALID_DIMENSIONS
    if dimension not in VALID_DIMENSIONS:
        return {'error': f'不支持的维度 {dimension};可用:{VALID_DIMENSIONS}'}
    results = evaluate_by(dimension, days=days)
    return {
        'dimension': dimension, 'days': days,
        'report': format_buckets(results, dimension),
        'buckets': [{
            'bucket': k, 'sample': r.sample_size, 'score': r.score, 'grade': r.grade,
            'win_rate_pct': r.metrics.get('win_rate_pct'),
            'avg_return_pct': r.metrics.get('avg_return_pct'),
            'profit_factor': r.metrics.get('profit_factor'),
        } for k, r in results.items()],
    }


@mcp.tool()
def selection_debate(codes: List[str], max_stocks: int = 10) -> Dict[str, Any]:
    """选股红蓝对抗:对候选股逐只跑 多头/空头/裁判 结构化对抗(空头专攻估值透支/题材退潮/财务雷),
    给"对抗后结论(买入/谨慎/否决)+置信+主因"。结论写决策信号(source_type=selection_debate)进后验。"""
    from selection_debate import run_selection_debate
    return run_selection_debate([c for c in (codes or []) if c], max_stocks=max_stocks, record_signals=True)


@mcp.tool()
def portfolio_stress_narrative() -> Dict[str, Any]:
    """组合压力情景叙事:跑全 8 宏观情景(加息/贬值/大盘暴跌/流动性危机…)压力 + 集中度 →
    AI 风险预案(最脆弱情景/跨情景风险担当持仓/具体减仓对冲建议)。复用 scenario_stress 引擎。"""
    from portfolio_stress_ai import run_stress_narrative
    return run_stress_narrative(include_funds=True)


@mcp.tool()
def announcement_scan(codes: List[str], days: int = 5) -> Dict[str, Any]:
    """公告事件分级:对给定股票拉近 days 天公告 → AI 提炼最具影响事件 + 方向(利好/利空/中性)+ 强度(1-5)。
    利空强→reduce 信号(source_type=announcement_risk)、利好强→buy 信号,进方向后验。黑天鹅预警。"""
    from announcement_scan import run_announcement_scan
    return run_announcement_scan([c for c in (codes or []) if c], days=days, record_signals=True)


@mcp.tool()
def lockup_radar(codes: List[str], forward_days: int = 60) -> Dict[str, Any]:
    """持仓解禁雷达:查给定股票未来 forward_days 天限售解禁(datahub.lockup_expiry,占比≥3%)→
    AI 给解禁前减仓研判。减仓/清仓写决策信号(source_type=lockup_risk)。补持仓事件型风控盲区。"""
    from lockup_radar import run_lockup_radar
    return run_lockup_radar([c for c in (codes or []) if c], forward_days=forward_days, record_signals=True)


@mcp.tool()
def research_digest(codes: List[str], days: int = 10) -> Dict[str, Any]:
    """研报增量解读:对给定股票拉近 days 天券商研报 → AI 提炼核心催化逻辑 + 评级方向(强看多/看多/中性/看空)
    + 隐含目标空间。强看多会写决策信号(source_type=research)进方向后验环。"""
    from research_digest import run_research_digest
    return run_research_digest([c for c in (codes or []) if c], days=days, record_signals=True)


@mcp.tool()
def exit_advice(target_positions: int = 20) -> Dict[str, Any]:
    """清仓决策助手:对全部持仓打"清仓紧迫分"(割肉止损/止盈锁定/破位减仓/死钱调出)排序,
    持仓过度分散时给"目标持仓数"瘦身建议(先清哪几只),并给 AI 整体瘦身策略。
    回答"买太多了不知道什么时候清"。清仓/减仓建议写决策信号(source_type=exit_advice)进后验。"""
    from exit_advisor import run_exit_advice
    return run_exit_advice(target_positions=target_positions, record_signals=True)


@mcp.tool()
def portfolio_health_check(max_stocks: int = 15) -> Dict[str, Any]:
    """持仓 AI 体检:融合每只持仓的破位/风险/浮亏/异动信号 → 单股 持有/减仓/清仓 动作 + 理由 + 信心。
    只对风险/浮亏子集做(token 可控)。动作会写决策信号(source_type=portfolio_health)进方向后验环。"""
    from jobs_hub import _scan_holdings_with_snapshot
    from portfolio_health_ai import run_health_check
    scans = _scan_holdings_with_snapshot()
    if not scans:
        return {'ok': False, 'summary': '无持仓数据'}
    return run_health_check(scans, max_stocks=max_stocks, record_signals=True)


@mcp.tool()
def decision_signals(code: str = '', status: str = 'active', action: str = '',
                     source_type: str = '', days: int = 0, limit: int = 50) -> Any:
    """决策信号列表(统一信号层:每次分析/选股/盯盘的结构化操作建议,8态动作+生命周期+去重)。
    code 指定股票;status 空=全部;action∈buy/add/hold/reduce/sell/watch/avoid/alert;days>0 限近N天。"""
    from decision_signal import list_signals
    return list_signals(code=code or None, status=status or None, action=action or None,
                        source_type=source_type or None, days=days or None, limit=limit)


@mcp.tool()
def decision_signal_latest(code: str) -> Any:
    """某股最新活跃决策信号(看 AI 当前对该股的操作主张:动作/进出场/止损/信心/理由)。"""
    from decision_signal import get_latest_active
    return get_latest_active(code) or {'note': f'{code} 无活跃决策信号'}


@mcp.tool()
def decision_signal_outcomes(days: int = 60, force: bool = False) -> Any:
    """后验校验决策信号:对近 days 天、已过持有周期的信号用 K线判 hit/miss/neutral 并落库。"""
    from decision_signal import run_outcomes
    return run_outcomes(days=days, force=force)


@mcp.tool()
def decision_signal_winrate(dimension: str = 'action', days: int = 180) -> Any:
    """决策信号已评胜率按维度分桶(dimension: action/source_type/horizon)。看"哪类信号真准"。"""
    from decision_signal import outcome_stats
    return outcome_stats(dimension=dimension, days=days)


@mcp.tool()
def industry_reports(industry_code: str = '*', pages: int = 5, begin: str = '2024-01-01') -> Any:
    """东财行业研报列表(走 datahub:industry_reports)。industry_code='*' 取全行业,或传具体行业代码。每条含发布日/行业/标题/机构/评级。"""
    import datahub
    rows = datahub.industry_reports(industry_code=industry_code, max_pages=pages, begin=begin)
    return {'industry_code': industry_code, 'count': len(rows or []), 'reports': rows or []}


if __name__ == '__main__':
    mcp.run()

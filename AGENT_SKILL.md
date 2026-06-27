---
name: shadow-foliant
description: A股多智能体分析平台的 Agent 技能文档。提供个股深度分析(技术/基本面/资金/风险/缠论)、多因子选股、条件选股漏斗、量化风险(VaR/压力测试)、估值(DCF)、财务排雷、组合诊断、行情/财务/资金/龙虎/北向/新闻数据查询、批量持仓导入、**基金长期定投(净值评价/定投回测/综合评分/AI研判)**等能力。仅 A 股(沪深京)+ 场外基金,部分能力需 DeepSeek API key。当用户要分析某只A股、选股、查行情财务资金数据、评估个股/组合风险、做基金净值评价/定投回测时使用。
---

# shadow-foliant · Agent 技能文档

> 给 Agent(OpenClaw/Claude 等)调用本项目能力用。
> 配套 MCP 服务见文末「MCP 适配」。所有代码靠 `_bootstrap` 加路径,入口先 `import _bootstrap`。

---

## 一、MCP 适配评估(哪些能 / 哪些不能)

### ✅ 适合做 MCP 工具(纯函数、清晰入参出参、无 UI)
- **数据查询**:`data_source_manager` / `StockDataFetcher` 的取数方法 — 行情/K线/财务/资金流/北向/龙虎榜/新闻/估值。
- **个股分析**:缠论、形态识别、技术指标、基本面打分、财务排雷、DCF 估值、量化风险/压力测试。
- **选股**:多因子横截面、条件选股漏斗(290 条件 + 配方)、5 套问财选股器。
- **组合**:批量导入持仓、组合诊断、组合压力。
- **聚合**:`agent_tool_groups.collect(groups, symbol)` —— 一次拿某股的多域 context(本身就是为 Agent 设计的)。

### ⚠️ 能 MCP 但需谨慎
- **多智能体分析** `run_multi_agent_analysis` — 强大但**耗 DeepSeek token、单次几十秒**;适合做"深度分析"重工具,不宜高频。
- **miniQMT 实盘交易** `miniqmt_interface` — **真实下单,危险**。若暴露必须加确认/沙箱/白名单,默认**不建议**开放。

### ❌ 不适合 MCP
- **所有 `*_ui.py` / `app.py`(46 个文件含 streamlit)** — 是 Streamlit 界面渲染,无请求/响应语义,无法 MCP 化。
- **调度器/守护进程**:`jobs_hub` / `monitor_service` / `*_scheduler` / `autostart` — 是常驻后台 daemon,不是一次性调用。
- **通知服务** `notification_service`(可包一个"发送"工具,但非核心)。

---

## 二、能力清单(可调函数 = MCP 工具候选)

> 调用前置:`import _bootstrap`(项目根)。返回多为 dict / DataFrame。仅 A 股(6位代码,如 `600519`)。

### A. 行情 / 数据(无需 LLM)
```python
from stock_data import StockDataFetcher
f = StockDataFetcher()
f.get_stock_info('600519')          # 名称/价/PE/PB/市值/涨跌幅
f.get_stock_data('600519','1y')     # 日K DataFrame(Date索引, OHLCV)
di = f.calculate_technical_indicators(df)   # 加 MA/RSI/MACD/KDJ/BOLL + MyTT 通达信指标
f.get_latest_indicators(di)         # 最新一根的全部指标 dict
f.get_financial_data('600519')      # 三表/财务比率

from data_source_manager import data_source_manager as M
M.get_capital_flow_a_data('600519') # 个股资金流
M.get_north_flow_a_data(days=30)    # 北向资金
M.get_dragon_tiger_detail_a_data()  # 当日龙虎榜
M.get_margin_trading_a_stock('600519')  # 融资融券
M.get_stock_news_a_stock('600519')  # 个股新闻
M.get_hot_stocks_a_stock()          # 热门股+题材
M.get_full_valuation_a_stock('600519')  # PEG/消化年数
```

### B. 个股分析(无需 LLM,纯计算)
```python
from chan_theory import analyze_chan
analyze_chan(df, '600519')          # 缠论:分型/笔/中枢/背驰/一二三类买卖点 + summary

from pattern_recognition import PatternDetector
PatternDetector().detect_all(df)    # 61 种K线形态

from fundamental_scoring import score_one, collect_factors
score_one('600519')                 # 8因子加权评分 0-100 + 等级 + action
collect_factors('600519')           # 原始因子 dict(PE/PEG/PB/ROE/增速/负债/股息/OCF)

from financial_forensics import analyze_forensics
analyze_forensics({'roe':22,'ocf_ratio':0.45,'debt_ratio':75,...})  # 杜邦+造假红旗

from dcf_valuation import analyze_dcf
analyze_dcf(base_fcf=100e8, shares=50e8, current_price=25)  # 两阶段DCF + 安全边际

from stress_testing import analyze_risk
analyze_risk(df, beta=1.1)          # VaR/CVaR/最大回撤/夏普/蒙特卡洛/压力情景 + summary
```

### C. 选股(部分用 pywencai/akshare,A股)
```python
from multi_factor_screener import screen_index, rank_topn
screen_index('000300', n=20, add_sector_leaders=True)  # 指数∪龙头 多因子TopN

from screener_engine import screen, screen_recipe, RECIPES
screen(['多头排列','macd金叉','换手率大于等于3%小于等于5%'], universe)  # 条件AND选股
screen_recipe('缠论一买', universe)  # 内置配方:主升浪起涨/超跌反弹/强势突破/低估值蓝筹/缠论一买/均线金叉起步
from screener_conditions import REGISTRY, find   # 290 个可用条件

# 5 套问财选股器(需 pywencai):
from low_price_bull_selector import LowPriceBullSelector   # 低价擒牛
from value_stock_selector import ValueStockSelector        # 低估值
from main_force_selector import MainForceStockSelector     # 主力资金
# small_cap_selector / profit_growth_selector 同理
```

### D. 组合 / 持仓
```python
from portfolio_db import portfolio_db
portfolio_db.get_all_stocks()       # 当前持仓
portfolio_db.bulk_import(stocks, mode='upsert')  # ✅ 批量导入持仓(upsert/add/replace)
# stocks=[{'code':'600519','name':'茅台','shares':100,'cost':1600}, ...]

from portfolio_diagnosis import diagnose_portfolio
diagnose_portfolio([{'symbol':'600519','market_value':50,'sector':'白酒'}, ...])  # 集中度/行业/波动

# ✅ 批量导入成交记录(真实买卖流水)→ 自动更新持仓(买入加仓重算均价/卖出减仓)
portfolio_db.import_trades([
  {'code':'600519','trade_type':'买入','quantity':100,'price':1600,'trade_time':'2025-01-15'},
], update_position=True)            # 返回 {imported, failed, positions_updated}
portfolio_db.get_trades('600519')   # 查成交记录(含 pos_quantity/pos_cost_price/delta_qty 持仓快照)
# 注:trade_records 是「成交记录+变动日志」合并表;成交记录行自带持仓快照(一行=成交+持仓变化)

import portfolio_insights as pi
pi.holding_duration_distribution(); pi.trading_frequency_analysis(90); pi.portfolio_change_timeline(90)  # 交易习惯洞察(纯库读)
pi.diagnose_portfolio()             # AI 持仓诊断(LLM;无 key 降级返回规则报表)
import portfolio_classifier as pc
pc.classify_all(limit=15, with_fundamental=False)  # 持仓分级 健康/观察/警报(盈亏+趋势+形态)
import position_guardian, position_profit_taker
position_guardian.evaluate_all_triggered(limit=20, with_fundamental=False)  # 加仓审核(跌幅触发→质地/仓位约束)
position_profit_taker.evaluate_all(limit=20)        # 减仓信号(阶梯止盈+破位保护)

from portfolio_backtest import portfolio_backtest, portfolio_backtest_live
portfolio_backtest([('600519','茅台'),...], '2024-01-01','2025-12-31', strategy_id='enter', max_positions=5)  # 组合级回测(实盘口径)
```
- MCP 工具(组合/持仓):`list_holdings`/`import_holdings`/`portfolio_diagnosis`/`portfolio_performance`/`portfolio_stress_scenario`/`portfolio_stress_all`/`stock_portfolio_curve` + 新增 `portfolio_trading_habits`/`portfolio_classify`/`portfolio_action_signals`/`portfolio_ai_diagnose`/`portfolio_backtest`。
- MCP 工具(个股,补 B 节):`dcf_valuation`(两阶段DCF,自市值/PE推导)。

### E. 聚合 context(Agent 首选入口)
```python
from agent_tool_groups import collect, TOOL_GROUP_META
collect(['base','kline_technical','chan_theory','fund_flow','fundamentals','risk'], '600519')
# 工具组: base/kline_technical/chan_theory/fund_flow/fundamentals/sentiment/chipset/macro_us/risk
```

### F. 多智能体深度分析(⚠️ 耗 token,需 DEEPSEEK_API_KEY)
```python
from ai_agents import StockAnalysisAgents
A = StockAnalysisAgents()
res = A.run_multi_agent_analysis(stock_info, stock_data_df, indicators,
                                 financial_data=..., enabled_analysts={'technical':True,...})
disc = A.conduct_team_discussion(res, stock_info)
A.make_final_decision(disc, stock_info, indicators)  # 含投资哲学透镜,输出评级/目标价/止盈止损(含盈亏比≥2硬约束)
```

### G. 妙想外部服务(东财 AI SaaS,第二意见/外部数据,⚠️需 EM_API_KEY)
附加功能包 `analysis/miaoxiang.py`,把东财「妙想」大模型的 NL「数据+分析+报告」能力包成 MCP 工具。
**定位:与自研多智能体互补的"第二意见 / 外部成品",非核心决策来源。** MCP 工具(`mx_*`):
- `mx_stock_diagnosis(question)` — 个股综合诊断(自研诊股的交叉验证)
- `mx_ask(question)` — 七合一金融问答(数据/资讯/宏观/选股/热点 总入口)
- `mx_hotspot(question)` — 市场热点发现(与 news_flow 交叉)
- `mx_comparable(question)` — 可比公司/同业估值横向
- `mx_finance_search(query)` — NL 搜公告/研报/新闻/政策
- `mx_macro(query)` — 中国宏观数据(补 FRED 偏美国的空白)
- `mx_industry_report(query)` — 行业深度研报
> 全部 11 个妙想技能见 `miaoxiang.SKILLS`(还含 fund_diagnosis/topic_report/kb_search/finance_data);
> 用 `miaoxiang.query(skill, text)` 通用调用。⚠️ 三方 SaaS,问句发往东财服务器(合规自评);
> 未配 `EM_API_KEY` 时用内置 demo key(易限流),返回里 `using_demo_key=True` 提示。

### H. 基金(长期 / 定投,场外开放式为主)
模块在 `fund/`,数据源 akshare(东财/雪球,免费)。**仅场外开放式基金 6 位代码(如 000001/110011)**。
```python
import fund_data, fund_metrics, fund_dca, fund_analysis, fund_db
fund_data.get_nav_history('110011')        # 历史净值 DataFrame[date,unit_nav,acc_nav,daily_return]
fund_data.latest_nav('110011')             # 最新确认净值 dict(akshare 失败回退 fundgz dwjz)
fund_data.get_realtime_estimate('110011')  # 盘中估值(fundgz 直连):dwjz/gsz/gszzl/gztime
fund_data.get_peer_rank_percentile('110011')  # 同类排名分位(雪球)
fund_metrics.evaluate(nav_df)              # 年化/最大回撤/夏普/卡玛/年化波动/下行风险
fund_dca.dca_backtest(nav_df, amount=1000, period='monthly', day=5, strategy='valuation')
                                           # ⭐定投回测:投入/份额/市值/收益/年化IRR/最大回撤 + 对比一次性买入
                                           # strategy: normal(定额)/valuation(估值智能,低估多投高估暂停)/value_avg(价值平均法)
fund_valuation.index_pe_percentile('沪深300')  # 宽基指数估值分位 → 档位 + 定投倍数(估值定投择时)
fund_screener.screen_funds('股票型', sort_by='r_1y', top_n=20, min_1y=0)  # 同类排行筛选
fund_portfolio.diagnose(holdings)          # 组合诊断:大类/类型配置 + 集中度HHI + (可选)重仓股重叠
fund_portfolio.combined_asset_view()       # 🌐 股票+基金 大类资产合并视图(成本口径)
fund_analysis.ai_research_panel('110011')  # 🧑‍⚖️ 多角色AI研判(业绩/风险/定投适配 + 综合)⚠️需LLM key
fund_analysis.score_fund('110011', with_extras=True)  # 综合评分 0-100 + 等级 + 建议(with_extras 加同类排名分位维)
fund_analysis.compare_funds(['110011','005827'], lookback_days=1095)  # 多只并排对比(共同窗归一:年化/回撤/夏普/卡玛 + 叠加曲线)
fund_data.top_holdings('110011'); fund_data.rating_summary('110011')  # 前十大重仓股(最新季度)/ 评级摘要(经理/公司/机构星级/费率)
# 名单已主源直连东财 fundcode_search.js(0.4s,快于 akshare fund_name_em ~7s),akshare 兜底
fund_analysis.ai_research('110011')        # 🤖 AI研判(适合长期/定投? 风险点 + 节奏建议)⚠️需LLM key、耗token
fund_db.get_holdings(); fund_db.add_transaction('110011','定投',nav=4.38,amount=1000)  # 持有+申赎流水(自动移动成本)
fund_db.add_plan('110011', 1000, 'monthly', day_of=5)   # 定投计划
```
- 定位:**长期持有 + 定投为主**,区别于股票模块的短线择时。阶段一含基础定投+回测;估值智能定投/价值平均法/止盈在阶段二。
- 后台任务(`jobs_hub`,默认全关):`fund_nav_refresh`(盘后净值入库+组合快照)/`fund_dca_reminder`(定投到期**自动记账/提醒**)/`fund_target_check`(止盈检查)/`fund_valuation_signal`(宽基估值分位播报)。
- MCP 工具(14):`fund_info`/`fund_nav_history`/`fund_metrics`/`fund_score`/`fund_dca_backtest`/`fund_index_valuation`/`fund_screen`/`fund_portfolio_diagnose`/`fund_ai_panel`/`fund_ai_research`/`fund_holdings`/`asset_overview` + 新增 `fund_compare`/`fund_detail`。

### I. 决策信号 + AI 赋能(2026-06;每个 AI 结论都进 decision_signal 方向后验)
**决策信号统一层**——全项目 AI 信号的后验底座。MCP 工具:
- `decision_signals(code,status,action,source_type,days)` — 信号列表(8 态动作/生命周期)
- `decision_signal_latest(code)` — 某股最新活跃信号
- `decision_signal_outcomes(days,force)` — 跑 K线方向后验(hit/miss)
- `decision_signal_winrate(dimension,days)` — 按 action/source_type/horizon 分桶真实胜率

**持仓 / 卖出侧 AI**(回答"该卖什么、何时清"):
- `exit_advice(target_positions)` — 清仓决策助手:全持仓清仓紧迫分(割肉/止盈/破位/死钱)+ 过度分散瘦身 + AI 策略
- `portfolio_health_check(max_stocks)` — 持仓 AI 体检:单股 持有/减仓/清仓 + 理由
- `lockup_radar(codes,forward_days)` — 持仓解禁雷达:未来解禁 → 解禁前减仓研判
- `announcement_scan(codes,days)` — 公告事件分级:利好/利空 + 重大性,利空预警
- `portfolio_stress_narrative()` — 组合压力情景叙事:8 情景风险预案
> 注:`exit_advice`/`portfolio_health_check` 的定时版已并入"尾盘持仓总结"(14:30 `afternoon_portfolio`),这俩仍供按需调用。

**选股 / 研报 / 观测**:
- `research_digest(codes,days)` — 研报增量解读:评级方向 + 隐含目标空间
- `selection_debate(codes,max_stocks)` — 选股红蓝对抗证伪(多空裁判)
- `recommendation_winrate(dimension,days)` — AI 推荐胜率按维度分桶
- `llm_token_usage(days)` — LLM token 用量(按 model/call_type/天)
- `industry_reports(industry_code,pages)` — 东财行业研报列表

---

## 三、使用约束(给 Agent 的提示)
- **仅 A 股**(沪深京 6 位代码);港股/美股能力不全。
- **需 key**:多智能体分析、智策/龙虎/新闻 的 AI 部分需 `DEEPSEEK_API_KEY`;问财选股需 `pywencai`。
- **限流**:所有外部抓取已内置自限流(防封),批量调用会稍慢,**不要并发猛拉**。
- **成本**:`run_multi_agent_analysis` 单次多次 LLM 调用、几十秒、耗 token —— 仅在"深度研判"时用;日常用 B/C/E 的纯计算函数。
- **数据时效**:行情可能延迟;盘后数据(龙虎榜/北向)收盘后才全。

---

## 四、MCP 适配落地(✅ 已建)
MCP server 已实现:`mcp_server.py`(FastMCP)。启动 `python mcp_server.py`(stdio),
客户端配 `{ "command":"python", "args":["<项目根>/mcp_server.py"] }`,env 传 `DEEPSEEK_API_KEY`/`EM_API_KEY`/`USE_POSTGRES` 等。
已封装:A–E 的数据/计算/选股/组合函数 + F(`deep_analysis` 重工具)+ G(妙想 `mx_*` 外部服务)+ H(基金 `fund_*`/`asset_*` 14 工具)+ I(决策信号 + AI 赋能 ~15 工具)。共 **80+ 工具**。
- miniQMT 实盘下单**未**暴露;UI/调度器**不封**。

## 五、相关待办
- ✅ **批量导入成交记录**:已实现(`import_trades`,自动更新持仓 + 持仓快照)。详见 [交接说明.md](交接说明.md) §6。
- ⏳ 未做:成交→已实现盈亏计算;成交记录列名中文化;SQLite 兜底版的持仓自动更新;`event_scoring`/`report_templates` 接入业务。

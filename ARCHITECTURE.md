# 📈 shadow-foliant — 项目架构与功能全景

> **定位**：基于 **Streamlit + DeepSeek + akshare + adata + a-stock HTTP** 的 A 股多智能体分析系统。
> 单 Web 应用集成 选股 / 技术分析 / 龙虎榜 / 板块策略 / 宏观分析 / 持仓盯盘 / AI 决策 / 实时监测 / 自动推送 / 后台调度。
>
> **数据流**：`数据源（HTTP/akshare/adata/tushare/yfinance）→ DataSourceManager 多源融合 → StockDataFetcher + MyTT 指标 → AI Agents (DeepSeek) → 决策落库 (PG/SQLite) → 邮件/Webhook 推送`

---

## 📦 文件统计

| 类别 | 数量 | 说明 |
|---|---|---|
| Python 模块 | 106 | 全部 import 测试通过（101 主模块 + 5 命令行/入口） |
| SQLite DB | 11 | 本地缓存与历史归档（PG 模式下仍保留为备份） |
| 文档 | 5+ | README.md / ARCHITECTURE.md / docs/* |
| 总文件数 | 135 | |

---

## 🏗️ 整体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                    Streamlit Web UI (app.py)                     │
│  ┌─────────────────┬──────────────────┬─────────────────────┐    │
│  │ 选股板块         │ 策略分析         │ 投资管理 / 监测      │    │
│  └─────────────────┴──────────────────┴─────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
        │                  │                       │
┌───────▼────────┐ ┌───────▼──────────┐  ┌─────────▼──────────┐
│  AI Agents     │ │  Data Layer      │  │  Background        │
│ (deepseek)     │ │ (multi-source)   │  │  Services          │
│                │ │                  │  │                    │
│ • 技术分析师    │ │ • a-stock HTTP   │  │ • monitor_service  │
│ • 基本面        │ │   (东财/腾讯)    │  │ • low_price_bull   │
│ • 资金面        │ │ • akshare        │  │ • jobs_hub (10任务)│
│ • 风险管理      │ │ • adata          │  │ • news_flow_sched  │
│ • 市场情绪      │ │ • tushare        │  │ • portfolio_sched  │
│ • 新闻分析      │ │ • yfinance       │  │ • sector_sched     │
│ • 首席策略      │ │ • pywencai       │  │                    │
└────────────────┘ └──────────────────┘  └────────────────────┘
        │                  │                       │
        └──────────────────┼───────────────────────┘
                           ▼
            ┌──────────────────────────────┐
            │   持久层（PG / SQLite 切换） │
            │ • analysis_records           │
            │ • portfolio_stocks           │
            │ • longhubang_records         │
            │ • sector_strategy / news_flow│
            │ • smart_monitor / monitor    │
            │ • jobs_snapshots             │
            └──────────────────────────────┘
                           │
            ┌──────────────────────────────┐
            │     通知层 (7 渠道)          │
            │ Email / 钉钉 / 飞书 / 企微   │
            │ Telegram / Discord / Slack   │
            └──────────────────────────────┘
```

---

## 🎯 功能模块清单

### 1. 选股板块 (5 套)
| 模块 | 入口文件 | 核心策略 | 数据源 |
|---|---|---|---|
| 💰 主力选股 | `main_force_*.py` | 主力资金净流入 TOP 100 | pywencai |
| 🐂 低价擒牛 | `low_price_bull_*.py` | 股价<10 + 净利↑100% + 沪深 | pywencai + TDX |
| 📊 小市值策略 | `small_cap_*.py` | 总市值<50亿 + 营收↑10% + 净利↑100% | pywencai |
| 📈 净利增长 | `profit_growth_*.py` | 净利↑10% + 深圳主板 | pywencai |
| 💎 低估值策略 | `value_stock_*.py` | PE≤20 + PB≤1.5 + 股息≥1% + 负债≤30% | pywencai |

### 2. 策略分析 (4 套)
| 模块 | 入口文件 | AI Agents |
|---|---|---|
| 🎯 智策板块 | `sector_strategy_*.py` | 宏观/板块诊断/资金/情绪 4 位 |
| 🐉 智瞰龙虎 | `longhubang_*.py` | 游资/潜力/题材/风险/首席 5 位 |
| 📰 新闻流量 | `news_flow_*.py` | 新闻情感 + 预警 + 深度 |
| 🌏 宏观分析 / 🧭 宏观周期 | `macro_analysis_*.py` / `macro_cycle_*.py` | 康波周期 + 美林时钟 + 政策 |

### 3. 投资管理 (3 套)
| 模块 | 入口文件 | 功能 |
|---|---|---|
| 📊 持仓分析 | `portfolio_*.py` | 持仓清单 + 批量分析 + 定时跟踪 |
| 🤖 AI 盯盘 | `smart_monitor_*.py` | DeepSeek 自动决策 + miniQMT 交易 |
| 📡 实时监测 | `monitor_*.py` | 价格阈值告警 + 进场/止盈/止损 |

### 4. 基础设施
| 模块 | 功能 |
|---|---|
| `stock_data.py` | 行情数据获取 + 技术指标计算（TA + MyTT 12 个通达信指标） |
| `data_source_manager.py` | 多源数据切换（a-stock HTTP > akshare > tushare > adata） |
| `a_stock_data_adapter.py` | 直连东财 push2 + 腾讯 + 新浪 + 同花顺 + 巨潮 |
| `deepseek_client.py` | DeepSeek API 封装（含 prompt 工程） |
| `ai_agents.py` | 多 Agent 协同分析框架 |
| `database.py` / `portfolio_db.py` / `longhubang_db.py` | 持久层（SQLite/PG 自动切换） |
| `notification_service.py` | 邮件 + 钉钉 + 飞书 通知（7 渠道） |
| `MyTT.py` | 麦语言通达信指标库（DMI/ATR/TRIX/ROC/CCI/BIAS/WR 等） |
| `agent_tool_groups.py` | 6 类业务域数据采集器（base/kline/fund/fund_amentals/sentiment/risk） |
| `portfolio_insights.py` | 持仓估值/变动时间线/持有时长/交易频次 4 大报表 + AI 诊断 |
| `db_compat.py` | SQLite/PG 透明路由（? 自动转 %s + lastrowid 模拟） |
| `jobs_hub.py` | 后台任务调度中心（10 个默认任务） |
| `notification_router.py` | 多渠道推送路由 |
| `bot_dispatcher.py` | Bot 命令分发（含 Telegram poller） |
| `autostart.py` | 项目启动时自动拉起后台服务和调度 |

---

## 🗄️ 数据库 Schema

### PostgreSQL 表（USE_POSTGRES=true 时主用）

**核心业务数据**

| 表名 | 用途 | 数据量级 |
|---|---|---|
| `analysis_records` | AI 股票分析（仅存关键摘要，原 prompt 大段文本已剥离） | 中 |
| `portfolio_stocks` | 持仓股票清单 | 小（数十只） |
| `portfolio_analysis_history` | 持仓分析记录 | 中 |
| `portfolio_changes` | **持仓变动历史**（add/update/delete/bulk_import 自动记录） | 中（每次变动 1 行） |
| `longhubang_records` | 龙虎榜原始数据 | 大（每日 100+ 行） |
| `longhubang_analysis` | 龙虎榜分析报告 | 小 |
| `stock_tracking` | 龙虎榜推荐股票追踪 | 小 |

**AI 盯盘**

| 表名 | 用途 |
|---|---|
| `monitor_tasks` | 盯盘任务配置 |
| `ai_decisions` | AI 决策历史（买/卖/持仓） |
| `smart_monitor_notifications` | 盯盘通知历史 |
| `trade_records` | 交易记录 |
| `position_monitor` | 持仓监测当前状态 |
| `smart_monitor_logs` | 盯盘系统日志 |

**实时监测**

| 表名 | 用途 |
|---|---|
| `monitored_stocks` | 监测股票清单 + 进场/止盈/止损位 |
| `monitor_notifications` | 价格触发通知历史 |
| `price_history` | 历史价格记录（按需保留） |

**任务调度**

| 表名 | 用途 |
|---|---|
| `indicator_snapshots` | 持仓股票当日 MyTT 指标快照 |
| `market_snapshots` | 大盘+北向+龙虎榜列表当日快照 |
| `job_runs` | 后台任务运行历史 |

**策略分析**

| 表名 | 用途 |
|---|---|
| `sector_analysis_reports` | 智策板块分析报告 |
| `sector_tracking` | 板块推荐追踪 |
| `flow_snapshots` | 新闻流量快照 |
| `ai_analysis` | 新闻 AI 深度分析 |
| `flow_alerts` | 新闻预警 |
| `sentiment_records` | 情绪记录 |
| `scheduler_logs` | 新闻调度日志 |
| `batch_analysis_history` | 主力选股批量分析历史 |

**建表脚本**：[scripts/init_postgres.sql](scripts/init_postgres.sql)（26 张表的完整 DDL）

**透明路由**：所有 db 模块通过 [db_compat.py](db_compat.py) 自动根据 `USE_POSTGRES` 选 PG 或 SQLite，**SQL 占位符 `?` 自动转 `%s`**，**INSERT 后 lastrowid 用 PG `lastval()` 模拟**。无需为每个 db 写双份代码。

### SQLite 本地 DB

**PG 模式下不再使用（数据已落 PG）**：以下文件保留在项目目录作为历史/迁移参考：
- `stock_analysis.db`, `portfolio_stocks.db`, `longhubang.db`
- `smart_monitor.db`, `stock_monitor.db`, `jobs_snapshots.db`
- `news_flow.db`（部分表迁 PG，原始 platform_news/hot_topics 留 SQLite）
- `sector_strategy.db`（部分表迁 PG，原始 sector_raw_data/sector_news 留 SQLite）
- `main_force_batch.db`

**始终用 SQLite（数据量小、不涉及多端访问）**：
- `low_price_bull_monitor.db` — 低价擒牛策略监测列表
- `profit_growth_monitor.db` — 净利增长策略监测列表

迁移现有数据到 PG（一次性）：参考 `scripts/` 目录可写一个 `migrate_sqlite_to_pg.py` 工具（用户按需）。

---

## 📊 持仓数据洞察（新增）

侧边栏 → **📊 持仓分析** → 现含 6 个 tab：

| Tab | 功能 |
|---|---|
| 📝 持仓管理 | 手动添加/编辑/删除（自动写 `portfolio_changes`） |
| 📤 **批量导入** | CSV / Excel / 粘贴文本 → 一键导入；3 种模式（upsert / add / replace） |
| 📊 **数据洞察** | 5 个子 tab：估值 / 变动时间线 / 持有时长 / 交易频次 / AI 诊断 |
| 🔄 批量分析 | AI 对持仓做批量深度分析（耗 token） |
| ⏰ 定时任务 | 持仓定时分析调度（按需开启） |
| 📈 分析历史 | 历史 AI 分析记录回放 |

### 📊 数据洞察子功能

1. **💰 估值汇总**：当前持仓总成本 vs 当前市值 vs 浮动盈亏（拉 a-stock 实时报价）
2. **📜 变动时间线**：最近 N 天的所有 add/update/delete/bulk_import 记录，含 delta_qty
3. **⏱ 持有时长**：每只股票的持有天数 + 分布桶 (<7d / 7-30d / 30-90d / 90-180d / >180d)
4. **🔄 交易频次**：买入次数 / 卖出次数 / 买卖比 / 日均变动
5. **🤖 AI 诊断**：DeepSeek 综合 4 类报表 → 输出结构化诊断
   - `summary`：1-2 句总评
   - `problems`：发现的问题清单
   - `suggestions`：可操作的改进建议
   - `risk_score` / `discipline_score`：风险倾向 + 交易纪律 0-10 分

### 持仓变动自动记录

所有写操作都会自动写一行到 `portfolio_changes` 表：

```python
portfolio_db.add_stock('600519', '贵州茅台', cost_price=1800, quantity=100)
# → portfolio_stocks 新增 1 行 + portfolio_changes 自动记录 (change_type='add', delta_qty=+100)

portfolio_db.update_stock(id, quantity=200)
# → portfolio_stocks 更新 + portfolio_changes 记录 (change_type='update', delta_qty=+100)

portfolio_db.delete_stock(id)
# → portfolio_stocks 删除 + portfolio_changes 记录 (change_type='delete', delta_qty=-200)

portfolio_db.bulk_import([...], mode='upsert')
# → portfolio_stocks 批量插入/更新 + portfolio_changes 每条都记录
```

变动数据来源 `source` 字段：`ui_manual` / `ui_bulk_upsert` / `ui_bulk_replace` / `bulk_import` / `ai_auto` / `api` 等，便于后续追溯。

---

## 🧠 AI 分析存 PG 的精简策略

为节省 PG 空间 + 提速查询，`database_pg.save_analysis()` 自动剥离大段 prompt 文本：

| 字段 | 处理 |
|---|---|
| `stock_info` | 保留 9 个关键字段（symbol/name/price/change/PE/PB/cap 等） |
| `agents_results` | 每个 agent 只存 name + analysis_summary (截断 400 字) + focus_areas |
| `discussion_result` | 仅存 summary (截断 800 字) + key_points |
| `final_decision` | **完整保留**（已是结构化的 rating/target/entry/stop/take_profit） |

SQLite 模式（USE_POSTGRES=false）仍存完整数据。

---

## ⏰ 定时任务表（10 个默认任务）

由 `jobs_hub` 自动调度，**仅交易日触发**（周末/节假日 skip）。

| 时间 | 任务 | 来源 | 内容 | 调 AI |
|---|---|---|---|---|
| **06:30** | **`overnight_ai_strategy`** | **jobs_hub** | **🌙 综合昨日龙虎榜+美股隔夜+新闻+北向资金 AI 分析今日开盘策略** | ✅ |
| 08:00 | `morning_warmup` | jobs_hub | 持仓+监测股票指标预热（MyTT 12 项） | ❌ |
| 08:30 | `dragon_tiger_report` | scripts/daily_signal_scan.py | 🐉 龙虎榜盘前邮件报告 | ❌ |
| 09:10 | `strategy_screening` | jobs_hub | 4 大策略扫描汇总 → 邮件 | ❌ |
| 09:45 | `morning_picks` | scripts/daily_signal_scan.py | 🔍 早盘 10 只精选 + 持仓 → 邮件 | ❌ |
| 12:00 | `noon_report` | scripts/daily_signal_scan.py | 📊 午盘市场简报 → 邮件 | ❌ |
| 14:30 | `afternoon_picks` | scripts/daily_signal_scan.py | 🔍 尾盘 10 只精选 + 持仓 → 邮件 | ❌ |
| 15:30 | `portfolio_indicator_snapshot` | jobs_hub | 持仓当日指标快照存库 | ❌ |
| 15:35 | `daily_market_snapshot` | jobs_hub | 大盘+北向+龙虎榜列表快照 | ❌ |
| 16:00 | `dragon_tiger_archive` | jobs_hub | 龙虎榜数据归档入库 | ❌ |
| 周一 03:00 | `weekly_db_cleanup` | jobs_hub | 数据库清理 + VACUUM | ❌ |
| **周一 09:00** | **`weekly_portfolio_analysis`** | **jobs_hub** | **AI 批量分析持仓 + 自动同步监测列表** | ✅ |

**额外的独立调度器**（按需手动启用，AI 调用消耗 token）：
- `news_flow_scheduler` — 30/60/120 分钟轮询热点/预警/深度
- `portfolio_scheduler` — 持仓 AI 批量分析（用户配置时间点）
- `sector_strategy_scheduler` — 智策板块 AI 分析（每日一次）
- `monitor_scheduler` — 实时监测在交易时段（09:30-11:30 / 13:00-15:00）自动启停

---

## ⚙️ 配置说明 (`.env`)

### 必填
```env
DEEPSEEK_API_KEY=sk-...               # DeepSeek API 密钥
DEEPSEEK_BASE_URL=https://ark.cn-beijing.volces.com/api/coding/v3
DEFAULT_MODEL_NAME=deepseek-v4-pro      # AI 模型（支持任意 OpenAI 兼容）
```

### 可选数据源
```env
TUSHARE_TOKEN=                        # tushare 备用源
TDX_BASE_URL=http://127.0.0.1:8181 # 本地 TDX API（低价擒牛策略用）
```

### PostgreSQL 后端（推荐）
```env
USE_POSTGRES=true                     # 总开关
PG_HOST=127.0.0.1
PG_PORT=55432
PG_DATABASE=aiagents_stock
PG_USER=aiagents_stock
PG_PASSWORD=...
```

### 通知配置
```env
EMAIL_ENABLED=true
SMTP_SERVER=smtp.qq.com
SMTP_PORT=587
EMAIL_FROM=...
EMAIL_PASSWORD=...     # SMTP 授权码（非登录密码）
EMAIL_TO=...

WEBHOOK_ENABLED=true
WEBHOOK_TYPE=dingtalk  # dingtalk / feishu
WEBHOOK_URL=...
WEBHOOK_KEYWORD=aiagents通知

# 新增 4 渠道（按需配）
WECHAT_WORK_WEBHOOK=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DISCORD_WEBHOOK_URL=
SLACK_WEBHOOK_URL=

# 按消息类别路由（可选）
# NOTIFICATION_ROUTE_ALERT=dingtalk,telegram
# NOTIFICATION_ROUTE_REPORT=email,wechat_work
```

### 启动时自动拉起服务
```env
AUTOSTART_ENABLED=true            # 总开关
AUTOSTART_MONITOR=true            # 价格监测（含交易时段调度）
AUTOSTART_JOBS_HUB=true           # 10 个默认任务
AUTOSTART_LOW_PRICE_BULL=false    # 低价擒牛策略监测
AUTOSTART_NEWS_FLOW=false         # 新闻流量（深度分析耗 token）
AUTOSTART_SECTOR_STRATEGY=false   # 智策板块（耗 AI token）
AUTOSTART_PORTFOLIO=false         # 持仓定时分析（耗 AI token）
```

### MiniQMT 量化（可选）
```env
MINIQMT_ENABLED=false
MINIQMT_ACCOUNT_ID=
MINIQMT_HOST=127.0.0.1
MINIQMT_PORT=58610
```

---

## 🚀 启动方式

### 标准启动
```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 .env（参考 .env.example）

# 3. 启动 (默认端口 8503)
python run.py
# 或
streamlit run app.py
```

### Docker 启动
```bash
docker-compose up -d
# 国内源加速版
docker build -f Dockerfile国内源版 -t shadow-foliant .
```

### 命令行入口
```bash
# 单个定时任务（cron 可调）
python scripts/daily_signal_scan.py dragon_tiger
python scripts/daily_signal_scan.py morning_picks
python scripts/daily_signal_scan.py noon_report
python scripts/daily_signal_scan.py afternoon_picks

# Bot 命令调试
python bot_dispatcher.py /help
python bot_dispatcher.py /jobs
python bot_dispatcher.py /runs 20
python bot_dispatcher.py /analyze 600519

# 测试 TDX 接口
python test_tdx_api.py
```

---

## 🔌 扩展点

### 1. 加新数据源
在 `data_source_manager.py` 加一个 `get_xxx_a_data()` 方法 + 在 `__init__` 加 `xxx_available` flag。

### 2. 加新 AI Agent
继承 `StockAnalysisAgents` 的设计：
- 在 `deepseek_client.py` 加一个新 prompt 方法（如 `mood_analysis()`）
- 在 `ai_agents.py` 加一个 `xxx_agent()` 方法
- 调用 `agent_tool_groups.collect(['base', 'sentiment'], symbol)` 收集 context

### 3. 加新定时任务
在 `jobs_hub.py`:
```python
def task_xxx():
    if _skip_if_not_trading('xxx'):
        return
    # ... 业务逻辑
    _log_run('xxx', 'success', ...)

# 在 register_default_jobs() 中
hub.register('xxx', '10:30', task_xxx)
```

### 4. 加新推送渠道
在 `notification_router.py` 的 `CHANNELS` 注册表加一个 `_send_xxx()` 函数。

### 5. 加新 Bot 命令
在 `bot_dispatcher.py`:
```python
@command('xxx', '说明')
def _cmd_xxx(args, ctx):
    return '结果'
```

---

## 🆕 主要改动记录

### 1. 模块合并
- 4 套业务模块整合到位：`macro_analysis_*` / `small_cap_*` / `value_stock_*` / `smart_monitor_*`（17 个文件）
- 保留 a-stock HTTP 直连扩展（20+ 方法，零依赖）
- 修复 sector_strategy 模块

### 2. PostgreSQL 全面切换
- **核心数据**（已上 PG，**默认主存储**）：分析记录 / 持仓 / 龙虎榜 / AI 盯盘 / 实时监测 / 任务快照 / 板块分析报告 / 主力选股历史 / 新闻流量分析
- **统一路由层** [db_compat.py](db_compat.py)：所有 _db.py 通过 `from db_compat import connect`，根据 `USE_POSTGRES` 自动选 PG/SQLite。SQL 占位符 `?` 自动转 `%s`，INSERT 后 `lastrowid` 用 PG `lastval()` 模拟
- 完整建表 SQL：[scripts/init_postgres.sql](scripts/init_postgres.sql)（26 张表）
- 改 `.env` 一行即可切换

### 3. MyTT 通达信指标接入
- 新增 12 个指标：DMI（PDI/MDI/ADX）/ ATR / TRIX / ROC / CCI / BIAS（6/12/24） / WR
- `stock_data.calculate_technical_indicators()` 自动计算
- `deepseek_client.technical_analysis()` prompt 自动注入 AI
- 库源：[MyTT.py](MyTT.py)（8.6K，纯 numpy/pandas 实现，零额外依赖）

### 4. adata 数据源接入
- 北向资金（沪深港通日度）
- 龙虎榜每日详情
- 概念资金流（备用）
- 融资融券
- 通过 `pip install adata` 引入

### 5. agent_tool_groups (6 类业务域)
- base / kline_technical / fund_flow / fundamentals / sentiment / risk
- Agent 可按域批量拉数据，避免冗余

### 6. jobs_hub (10 个默认任务)
- 盘前预热 / 盘中报告 / 盘后快照 / 龙虎榜归档 / 周清理
- 复用现有 `scripts/daily_signal_scan.py`
- 双层非交易日判断

### 7. notification_router (7 渠道)
- 老 3 渠道：email / dingtalk / feishu
- 新 4 渠道：wechat_work / telegram / discord / slack
- 按消息类别路由

### 8. bot_dispatcher (Bot 命令)
- 7 个内置命令：`/help` / `/analyze` / `/snapshot` / `/market` / `/jobs` / `/runs` / `/channels`
- 自带 Telegram polling（无需公网 webhook）

### 9. autostart.py (启动时自动拉起)
- 监测服务 + jobs_hub 默认自动启
- AI 耗 token 的几个默认关
- Streamlit re-run 幂等

### 10. 修复的 bug
- `stock_data.py` 缺 `import os` + `_saved_proxy` 未初始化（导致 000100 等数据全 N/A）
- `a_stock_data_adapter.py` 用 `_session.get` 替代 `requests.get`（trust_env=False，绕过代理）
- `get_stock_info_detailed` 缺 name 合并
- `notification_service` 缺 `send_analysis_result/send_email/send_webhook` 通用方法
- `longhubang_engine` 分析完成没推送（已加 `_notify_analysis_complete`）
- `main_force_selector` `timedelta(days=None)` 报错
- `pattern_recognition` 缺 talib 时 NameError 'tl'
- `.env` 加载时机错误导致 USE_POSTGRES=true 失效

---

## ⚠️ 已知限制 / 后续优化方向

1. **东财 push2 接口 TLS 指纹**：Python requests + OpenSSL 在某些 Windows 网络环境会被东财识别为爬虫断连。已通过腾讯接口兜底（覆盖 PE/PB/市值/中文名），可考虑后续接入 `curl_cffi` impersonate 模式。

2. **国家统计局接口 403**：M2 / CPI / PMI 等 macro_analysis_data 接口在某些 IP 下被屏蔽。建议加 IP 白名单或换数据源（如 wind/ifind）。

3. **指标快照存 SQLite 不存 PG**：`jobs_hub.indicator_snapshots` 仍在本地 SQLite。如需上 PG，扩展 `database_pg.py` 加对应 schema。

4. ~~**节假日识别**：当前 `_is_trading_day()` 只判断周末。~~ ✅ 已修(2026-05-31):`jobs_hub._is_trading_day()` 接入 akshare `tool_trade_date_hist_sina()` 官方交易日历 + 进程内按天缓存,失败/超范围回退"只判周末"(绝不误杀真实交易日)。注:`monitor_scheduler`/`scripts` 下另有同名简版仍只判周末,如需一致可统一抽取。

5. **streamlit re-run 性能**：每次页面刷新都会触发 `import autostart`。虽有 `_STARTED` flag 守卫，但模块本身的 import 链路仍会扫描。如要做高并发，考虑改 FastAPI + 独立后台 worker。

6. **AI Agent 工具分组未真正集成**：`agent_tool_groups.py` 提供了 `collect_*` 函数和 `AGENT_RECOMMENDED_GROUPS` 映射，但现有 `ai_agents.py` 还在用旧的传参模式。可以逐步迁移让 Agent 主动按域拉数据。

7. **Bot 命令多平台**：当前只内置 Telegram polling。钉钉/企微的 outgoing webhook 接收需要再加一个 FastAPI 入口。

8. **Webhook 失败重试**：当前推送失败 silent。考虑加重试队列（如用 jobs_snapshots.db 存重试任务）。

---

## 📁 关键文件索引

```
shadow-foliant/
├── app.py                          # Streamlit 主入口
├── run.py                          # 启动脚本
├── config.py                       # 配置加载
├── autostart.py                    # 启动时自动拉起后台
├── 
├── # 数据层
├── stock_data.py                   # 行情+指标
├── data_source_manager.py          # 多源切换
├── a_stock_data_adapter.py         # HTTP 直连东财/腾讯/新浪/同花顺
├── MyTT.py                         # 通达信指标
├── fund_flow_akshare.py            # 资金流
├── market_sentiment_data.py        # 市场情绪
├── news_announcement_data.py       # 新闻公告
├── quarterly_report_data.py        # 季报
├── risk_data_fetcher.py            # 风险数据
├── qstock_news_data.py             # qstock 新闻
├── 
├── # AI 层
├── ai_agents.py                    # Agents 集合
├── deepseek_client.py              # DeepSeek API
├── model_config.py                 # 模型配置
├── agent_tool_groups.py            # 业务域工具组
├── 
├── # 持久层
├── database.py / database_pg.py    # 分析记录
├── portfolio_db.py / portfolio_db_pg.py # 持仓
├── longhubang_db.py                # 龙虎榜（内置工厂）
├── sector_strategy_db.py
├── news_flow_db.py
├── smart_monitor_db.py
├── monitor_db.py
├── main_force_batch_db.py
├── 
├── # 业务模块（每套有 _data / _agents / _engine / _ui / _pdf / _scheduler）
├── longhubang_*                    # 智瞰龙虎（5+ 文件）
├── sector_strategy_*               # 智策板块
├── news_flow_*                     # 新闻流量
├── main_force_*                    # 主力选股
├── low_price_bull_*                # 低价擒牛
├── small_cap_*                     # 小市值
├── profit_growth_*                 # 净利增长
├── value_stock_*                   # 低估值
├── macro_cycle_*                   # 宏观周期
├── macro_analysis_*                # 宏观分析
├── portfolio_*                     # 持仓分析
├── smart_monitor_*                 # AI 盯盘
├── monitor_*                       # 实时监测
├── 
├── # 通知 / 后台 / 命令
├── notification_service.py         # 通知服务（邮件+钉钉+飞书）
├── notification_router.py          # 7 渠道路由
├── jobs_hub.py                     # 后台任务调度
├── bot_dispatcher.py               # Bot 命令分发
├── pattern_recognition.py          # K线形态识别
├── stock_selection.py / stock_strategies.py # 综合策略
├── 
├── # PDF / 导出
├── pdf_generator.py / pdf_generator_fixed.py / pdf_generator_pandoc.py
├── *_pdf.py (按业务模块)
├── 
├── # 入口脚本
├── scripts/
│   ├── daily_signal_scan.py        # 4 个时间点定时报告（被 jobs_hub 调）
│   ├── run_batch_analysis.py
│   ├── run_batch_scan.py
│   ├── stock_alert.py
│   ├── init_postgres.sql           # PG 建表 SQL
│   └── webdav-helper.sh
├── 
├── # 配置
├── requirements.txt
├── .env / .env.example
├── .streamlit/config.toml          # 主题配置（含暗黑适配）
├── docker-compose.yml
├── Dockerfile / Dockerfile国际源版
├── 
└── # 数据库 (11 个 .db, 见上表)
```

---

## 📊 当前运行状态（迁出快照）

| 项目 | 状态 |
|---|---|
| 总文件数 | 135 |
| Python 模块 | 106（101 主模块 + 5 命令行） |
| 全模块 import 体检 | ✅ 101/101 OK |
| Streamlit 服务 | http://127.0.0.1:8503（运行中） |
| PostgreSQL | ✅ 127.0.0.1:55432, 持仓 40 条已读到 |
| jobs_hub | ✅ 10 个任务已注册，自动启动 |
| monitor_service | ✅ autostart 已接入，交易时段自动启停 |

---

## 🎓 给后续维护者的建议

1. **改代码前先跑 health check**：
   ```bash
   # 全模块 import 测试
   python -c "import importlib; [importlib.import_module(f[:-3]) for f in __import__('os').listdir('.') if f.endswith('.py') and not f.startswith('_')]"
   ```

2. **改 .env 后必须重启 streamlit**（dotenv 仅在 import 时加载）。

3. **添加新模块时**：
   - 顶级代码必须包在 `if __name__ == '__main__':` 块
   - 文件名不要以 `_` 开头（被 health_check 跳过）
   - 在 ARCHITECTURE.md 的"文件索引"区添加位置

4. **修 bug 优先级**：
   - 数据获取 > AI prompt > UI 显示 > 文档
   - 用户实际能感知的（如 000100 数据全 N/A）优先级最高

5. **PG 切换的坑**：
   - 切换前必须确认表已建好（运行 `scripts/init_postgres.sql`）
   - `database_pg.py` 的接口必须与 `database.py` 完全对齐（方法名 + 参数 + 返回值）
   - 测试切换时观察启动日志的 `[Database] 已切换到 PostgreSQL 后端` 消息

6. **新功能切忌动 `requirements.txt` 引入重型依赖**：MyTT 8K + adata 几 MB 已是上限，再大的依赖（如 TensorFlow）建议拆独立微服务。

---

*生成于 2026-05-24，对应本轮整合的最终状态。迁回 OpenClaw 后此文档可作为 Agent 上下文索引使用。*

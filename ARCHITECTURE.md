# Shadow Foliant 架构

## 运行边界

- `stock-webui`：FastAPI API 与 Vue3 SPA，浏览器通过 OIDC Code + PKCE 登录。
- `stock-jobs-hub`：数据同步、选股、研究、监测与通知任务。
- Foliant Agent/MCP：使用 audience=`foliant` 的 Service/Agent Bearer；机器接口不使用浏览器 Cookie。
- PostgreSQL：唯一生产业务与服务端会话数据库。
- Redis：缓存、分布式锁和跨进程外部接口速率槽；不可用时接口仍有进程内限流。

生产由 Supervisor 管理两个进程。TLS、反向代理与统一 Identity 位于外部受限基础设施，仓库不保存真实域名、上游地址或凭据。Platform SDK 在 Foliant 进程内完成 Agent、LLM 与可选 Media 接入，Platform 不转发业务流量。

## 应用与插件边界

```text
Web API -----------------+
Agent HTTP API ----------+--> application/* --> 领域分析/服务 --> PostgreSQL repository
MCP compatibility -------+
```

`mcp_server.py` 只是迁移期适配器；机器 HTTP 不导入 MCP。Agent 创建的研究、选股和
回测全部落独立 `foliant_runs(mode=preview)`，不能覆盖正式 selection artifact。仓库中的
`shadow-plugin.yaml`、`agent/manifest.yaml` 和 `contracts/agent.openapi.yaml` 是运行时
无关合同；Foliant 不安装 DSH 或 Cordis，Platform 只据此构建目标 Profile 的通用 Bundle。
Run 完成与元数据 Outbox 同事务提交；后台 publisher 只依赖注入的消息适配器，不把具体
Runtime 或消息总线 SDK 带入应用层。

普通 `shadow-finance-research` Profile 只有市场、研究、选股、回测 preview 和 Run 查询。
持仓、成交、配置、运维和券商执行不进入该 Profile。股票成交由 Stock Web 管理员在服务端
预览并确认后录入；未来 Agent 写入须等待 delegated actor、资源授权和完整
`ConfirmationReceipt` 闭环。

## 研究主链

```text
zzshare 全市场主源
  + BaoStock qfq 精确日期修复
  + TDX 原始行情独立校验/报价
        ↓
本地 PIT 研究仓（证券快照、不可变财报修订、qfq 日线、估值、真实主力资金、确认事件）
        ↓
统一可投资快照（交易状态、流动性、财务覆盖、正净资产、数据日期）
        ↓
PIT 综合核心 + 本地五策略 + 全市场技术基因组
        ↓
多策略提名事实 → 去重 → 赛道配额 + 行业/相关性约束
        ↓
local-fusion-v2 TOP15 + 完整候选集内独立确定性复排 TOP5

问财 / 妙想 / Agent / 盘中报价 → 独立参考与复核，不改变正式成员或排名
        ↓
1/3/5/10/20 日后验 → 周度 LLM 提案 → 确定性政策控制器
```

选择日期使用其之前最近一个已完成交易日。行情日期不符、单位未知、市场覆盖不足或财务覆盖不足时返回 `incomplete`，不启用在线候选兜底。
正式 TOP15 默认 PIT 至少 8 只、本地五策略最多 5 只、技术基因组最多 2 只；同一行业最多 5 只，卫星未达门槛的名额自动归还 PIT。策略委员会不能修改 PIT 核心下限、代码、因子或外部参考边界。

## 盘前事实流

```text
多平台热榜（传播热度） ───────────────┐
                                      ├─> 09:00 晨报解释与风险提示
CLS 加红电报（可追溯事实）            │
  → source_event_id 去重与内容修订     │
  → 昨日 08:30–15:00 背景窗口          │
  → 昨日 15:00–今日 08:30 主窗口       │
  → 逐条本地结构标签与稳定题材线程 ────┘
```

`premarket-facts-v1` 不调用 LLM，不产生股票代码、目标价、仓位或买卖指令。每个事实和
题材线程必须引用本次输入事件 ID，源失败时读取已持久化窗口缓存并公开降级阶段；缓存也没有
数据时，晨报继续使用原有多源快讯兜底。所有题材输出固定为 `reference_only`，不能新增候选
或改变正式 TOP15/TOP5。

## 数据与安全

- 证券主数据按日期保存，不用当前状态回放历史。
- 财报按报告期、发布日期、首次可见日、供应商与修订保存。
- 公告标题只能形成待确认线索；正文确认并完成结构化字段后才进入事件评分。
- Token、Cookie、提示词正文、持仓、成交和完整模型响应不得写入统计或日志。
- 所有持仓、成交、任务和环境写操作由服务端执行管理员授权。

## 部署与验证

部署入口是 `scripts/deploy.sh`，必须传入已经推送的 `EXPECTED_COMMIT`。公开 `/healthz` 无状态且不访问数据库；受保护 `/readyz` 才检查依赖。基础设施备份使用 PostgreSQL、Nginx、Identity 与 Supervisor 各自的原生工具，不由应用创建 SQLite 归档。

详细变更规格见 [openspec](openspec/)，A 股接口与选股口径见 [docs/A股本地选股与数据接口.md](docs/A股本地选股与数据接口.md)。

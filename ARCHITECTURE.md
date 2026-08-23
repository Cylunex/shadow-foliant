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
本地 PIT 研究仓（证券快照、不可变财报修订、qfq 日线、估值、确认事件）
        ↓
基本面/估值门槛 → 60 日核心 → 120/250 日风险修正
        ↓
行业宽度、相关性与集中度约束
        ↓
本地 TOP15 → 确定性 TOP5

问财 / Agent / 盘中报价 → 独立参考与复核，不改变正式排名
```

选择日期使用其之前最近一个已完成交易日。行情日期不符、单位未知、市场覆盖不足或财务覆盖不足时返回 `incomplete`，不启用在线候选兜底。

## 数据与安全

- 证券主数据按日期保存，不用当前状态回放历史。
- 财报按报告期、发布日期、首次可见日、供应商与修订保存。
- 公告标题只能形成待确认线索；正文确认并完成结构化字段后才进入事件评分。
- Token、Cookie、提示词正文、持仓、成交和完整模型响应不得写入统计或日志。
- 所有持仓、成交、任务和环境写操作由服务端执行管理员授权。

## 部署与验证

部署入口是 `scripts/deploy.sh`，必须传入已经推送的 `EXPECTED_COMMIT`。公开 `/healthz` 无状态且不访问数据库；受保护 `/readyz` 才检查依赖。基础设施备份使用 PostgreSQL、Nginx、Identity 与 Supervisor 各自的原生工具，不由应用创建 SQLite 归档。

详细变更规格见 [openspec](openspec/)，A 股接口与选股口径见 [docs/A股本地选股与数据接口.md](docs/A股本地选股与数据接口.md)。

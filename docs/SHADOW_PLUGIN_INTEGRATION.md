# Shadow Foliant 插件接入

Foliant 是独立部署的股票、基金、选股、研究、回测和组合分析领域服务。Stock Web/PWA
仍是用户入口；Shadow Plugin 只描述 Runtime 如何用 Foliant 专属 Bearer 直接访问一组
窄化远程能力。Platform 不代理行情、Prompt、持仓或研究正文，Foliant 也不依赖 DSH、
Cordis 或 DSH Tools。

## 应用服务

- `MarketOverviewService`：市场会话、交易日和指数摘要。
- `DataQualityService`：PIT、市场日期和覆盖率的确定性质量合同。
- `SecurityResearchService`：正式研究读取与 preview Run 创建。
- `SelectionRunService`：正式 SelectionRun 读取与不发布 preview。
- `BacktestRunService`：异步、持久化的回测 preview。
- `ResearchRunQueryService`：按创建 Agent 隔离的 Run 状态与有界结果。
- `TradeEntryService`：Stock Web 管理员成交预览和确认录入；不属于研究 Profile。

Web、Agent HTTP 和兼容 MCP 从不同适配器进入上述服务。Agent HTTP 不导入
`mcp_server.py`；MCP 不再新增领域能力。

## 研究 Profile

`agent/profiles/shadow-finance-research.yaml` 显式选择七项 Capability：

- `foliant.market.read`
- `foliant.security-research.read`
- `foliant.security-research.preview`
- `foliant.selection.read`
- `foliant.selection.preview`
- `foliant.backtest.preview`
- `foliant.run.read`

读取为 L0；preview 是可撤销且不能发布的 L1 draft。创建 Run 需要运行时提供
`Idempotency-Key`，同一 Agent、Capability 和键只有相同请求可以复用。完整结果长期留在
Foliant，模型只收到有预算的摘要、`shadow://foliant/...` 引用或 continuation。

该 Profile 不含持仓、成交、监控、环境配置、任务控制、正式发布、MiniQMT 或券商执行。

## 成交录入

股票成交录入是 Stock Web 的 `stock-admins` 能力：

1. `/api/portfolio/trade-records/preview` 解析名称/代码、方向、价、量、费用和时间，不写库；
2. 页面展示服务端规范化结果；
3. 管理员确认后，`/api/portfolio/trade-records` 校验预览摘要和幂等键；
4. `TradeEntryService` 复用既有成交导入与移动加权持仓逻辑；
5. 服务端审计只记录路由、actor、结果和状态，不记录成交正文。

这里的 OIDC 管理员确认属于浏览器业务操作。Agent 交易写入仍被明确保留：Platform 完成
delegated actor、固定 portfolio grant 以及 `ConfirmationReceipt` 签发、验签、消费和防重放
之前，不能把 DSH Approval 或 `dry_run=false` 当成授权。

## Run 与来源

`foliant_runs` 只保存 `mode=preview` 的研究、选股和回测。状态为
`queued | running | complete | failed | cancelled`；超过恢复租期的遗留运行会收敛为可查询的
`worker_restarted` 失败，调用方用新幂等键显式重试。执行超时会先收敛为
`execution_timeout`，晚到结果不能覆盖终态或产生事件。完成结果和 Outbox 事件在同一事务提交。
首版 research Profile 不暴露取消工具，因此执行阶段明确返回 `cancellable=false`；preview
本身不会发布正式状态，可直接丢弃。`cancelled` 保留给 Foliant 内部队列治理。

`application.outbox.OutboxPublisher` 通过注入的发布适配器在后台投递元数据事件；失败只增加
尝试次数且不记录正文，成功后标记 `published_at`。具体消息总线和凭据属于仓库外运行配置，
未配置发布适配器时事件安全保留在 Outbox，不会假装已经交付。

每个结果包含 `decision_at`、`market_as_of`、`financial_cutoff_at`、
`universe_snapshot_id`、`input_manifest_id`、`policy_hash`、`code_revision` 和 `run_id`。
旧正式研究缺失某项时返回 warning，不用当前值伪造历史来源。LLM 派生内容只能放在
`derived_analysis`。

## 合同与本地验证

示例只使用 `https://stock.example.com` 和环境变量名。插件版本来自 `VERSION`，必须与
Definition 和 Manifest 相同。Platform Python 包约束为 `>=0.6,<1`，DSH distribution 与
Tools API 精确 Profile 基线都是 `0.1.1-rc.2`。

```bash
shadow-plugin-validate .
shadow-dsh-build \
  --profile agent/profiles/shadow-finance-research.yaml \
  --instances agent/instances.example.yaml \
  --plugin-root . \
  --output-dir build/dsh-finance-research
```

生成 Bundle 是构建产物，不提交真实地址、Token 或生产 Profile。生产部署不属于本次改造。

## 后续 P1

- `shadow-finance-personal`：delegated actor + resource grant 后的组合/自选/提醒读取；
- 轻量写入：Platform 完成通用确认回执闭环后再单独设计专用 Profile；
- `shadow-broker-execution`：MiniQMT、下单、撤单和券商同步的独立高风险项目；
- 资源下载控制面和正式 Profile 生产部署。

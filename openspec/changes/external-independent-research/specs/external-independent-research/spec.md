# ADDED Requirements

## Requirement: 外部研究身份隔离

系统 MUST 将正式选股、问财参考和外部独立研究作为三个不同身份。外部独立 TOP15 的成员集合 MUST
精确等于同一已发布 SelectionRun 的 `codex-independent-v1` TOP15；外部证据 MAY 在有界范围内调整
观察排序，但 MUST NOT 添加、删除、重排或改分正式选股及问财 artifact。

### Scenario: 外部新闻发现新证券

Given 新证券不在绑定的独立 TOP15
When 外部研究提交该发现
Then 证券只能保存到 `news_watchlist`，且标记量化字段不完整和正式 TOP15 不可用。

## Requirement: 可审计的 PIT 外部证据

每条证据 MUST 保存来源 URL、类型、发布时间、事件时间、捕获时间、决策时点、影响对象、方向、
置信度、失效时间、去重键、第一方确认和争议状态。捕获时间晚于决策时点或过期证据 MUST 失败关闭；
相同通道和去重键的冲突内容 MUST NOT 覆盖历史记录。

### Scenario: 决策后新闻回填历史排名

Given 证据在排名决策之后才被捕获
When 提交历史 decision_as_of 的外部排序
Then 整个事务失败，证据、排序和提案均不落库。

## Requirement: 有界排序和执行隔离

`event_adjustment` MUST 限制在 `[-15,+8]`，风险否决 MUST 引用已保存证据。外部研究 MUST NOT
生成执行价格、交易指令或自动下单；买卖价 MUST 继续来自同一正式快照的权威 trade_plan/行情校验。

### Scenario: 仅凭新闻尝试生成价格

When 外部研究被计划快照投影
Then 输出明确 `external_can_create_execution_price=false` 和 `auto_execution=false`，正式交易计划不变。

## Requirement: 后验、调优门槛与回滚

系统 MUST 按 1/3/5/10/20 交易日评估 base score、event adjustment 和 risk veto，并按市场状态分层。
调优建议只有在至少 20 个到期样本、覆盖 4 个交易周且通过时间切分样本外比较后 MAY 进入人工审核，
并 MUST 保持 `auto_apply=false`。提案 MUST 支持不修改策略的回滚。

### Scenario: 调用方虚报成熟样本

Given 请求声明 999 个样本和 999 周但数据库没有到期后验
When 保存调优提案
Then 服务端重算为实际数量，状态为 `evidence_insufficient`，且不应用策略。

## Requirement: 同快照一次性报告

CLI MUST 先提交严格的 `codex-external-independent-v1` bundle，再读取已经包含同一 overlay 的生产
计划快照。提交 MUST 具有请求哈希绑定的幂等键、服务端 `ranking_locked_at` 和当代
`decision_as_of`。QQ 通知 MUST 在发送前原子消费该提交的一次性 claim；相同请求重放 MUST NOT
再次发送。外部 writer MUST NOT 因此获得私人持仓读取权限。

### Scenario: 调度器重试同一外部报告

Given 同一幂等键和完全相同的 bundle 已提交并已消费通知 claim
When 调度器再次提交、读取快照并请求发送
Then 返回原 overlay，快照 ID 和锁定时间不变，第二次 QQ 被抑制且不生成任何交易动作。

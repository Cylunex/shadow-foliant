# ADDED Requirements

## Requirement: 正式候选与外部参考隔离展示

09:45 通知 MUST 展示正式 TOP5、五组问财的可用或失败状态、代表结果及与正式候选的重合项。
问财 MUST 标注仅供参考，并 MUST NOT 改变正式候选或进入可买监控池。

### Scenario: 问财失败

- **WHEN** 一组或全部问财缓存不可用而正式本地选择完成
- **THEN** 正式 TOP15/TOP5 保持原成员和顺序
- **AND** 通知把对应问财组标为失败或不可用

## Requirement: 统一盘中监控池

系统 MUST 去重合并所有真实持仓、正式 TOP5 和 TOP15 其余候选，并保存来源、rank、selection run 和
as-of。TOP5 MUST 为高优先级，TOP15 其余 MUST 为观察级。

### Scenario: 问财独有股票

- **WHEN** 股票只出现在问财参考中
- **THEN** 它只出现在 `wencai_reference`
- **AND** 它不出现在 holdings、formal_top5 或 formal_top15_watch

## Requirement: 权威价格与失败关闭

候选 MUST 展示当前价、动作、买入区间、止损、第一目标、价格依据和数据时点；持仓 MUST 展示当前价、
统一动作、卖出/止损触发价、止盈或减仓区间、依据和数据时点。价位 MUST 来自既有 trade_plan、持仓
风控和冻结技术数据，不得由 LLM 生成。缺少有效依据或新鲜报价时 MUST 显示暂不给价/数据不足。

### Scenario: 行情陈旧

- **WHEN** 批量报价时间超过新鲜度门槛或关键覆盖低于门槛
- **THEN** 任务标记 degraded、skipped 或 error
- **AND** 相关标的不得输出可执行的明确动作

## Requirement: 轻量状态化轮询

系统 MUST 在 A 股交易时段每 20 分钟批量拉取监控池报价，午休和非交易日 MUST 跳过。轮询 MUST NOT
逐股请求问财、F10、慢 K 线或调用 LLM。

### Scenario: 阈值穿越和重新进入

- **WHEN** 候选首次进入买入区，或持仓/候选首次触及止损/止盈，或持仓动作升级为减仓/卖出
- **THEN** 系统通过 notification_router 发送一次提醒
- **AND** 持续停留在该状态不重复通知
- **AND** 离开后重新进入且冷却至少 60 分钟才可再次通知

## Requirement: 可查询的当日决策快照

系统 MUST 保存区分 `holdings`、`formal_top5`、`formal_top15_watch` 和 `wencai_reference` 的完整当日快照，
并 MUST 提供 Agent 只读查询。即时通知 SHOULD 只发送有界摘要。

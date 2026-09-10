# ADDED Requirements

## Requirement: 受保护的有界计划快照

Foliant MUST 通过独立 Agent capability 提供一次性只读快照；快照 MUST 包含交易日依据、
cockpit、正式 TOP15/TOP5、`codex-independent-v1` TOP15/TOP5、五组问财参考、真实持仓、正式交易计划、组合风控行动预览、批量行情
及各分区 as-of/quality。该能力 MUST 要求个人主组合 resource grant，MUST NOT 下单或修改数据。

### Scenario: 研究 Agent 请求私人快照

Given Agent 只有市场和正式选股读取权限
When 请求计划快照
Then 返回 403，且不返回持仓数量、成本或证券明细。

## Requirement: 正式结果与外部参考隔离

正式候选 MUST 只来自完整已发布 SelectionRun。问财 MUST 位于独立 reference 分区；缺失、
失败或少于五组 MUST 降级 reference 质量，但 MUST NOT 添加、删除、重排或改分正式候选。

### Scenario: 问财五组均缺失

Given 正式 TOP15/TOP5 完整且问财 artifact 缺失
When 生成计划快照
Then 正式候选保持完整，问财状态为 missing，整体质量为 degraded。

## Requirement: 独立选股与有效性感知对照

快照 MUST 投影独立选股的版本/哈希、manifest/snapshot/as-of、固定权重和 TOP15/TOP5。
两两或三方比较 MUST 只在参与方各自有效时输出；问财任一必要组失败时，三方结果 MUST 为不可用。

### Scenario: 只有正式与独立有效

Given 正式与独立 TOP15 有效，问财存在失败组
When 生成计划快照
Then 快照返回正式/独立交集和各自独有项，三方结果为不可用。

## Requirement: 权威交易日与陈旧度

交易日结论 MUST 来自覆盖目标日期的至少两个独立来源一致证据。缺少该证据时 MUST 返回
unknown，MUST NOT 使用 weekday/weekend fallback 冒充确认。正式产物落后于最近确认开市日时
MUST 标记 stale。

### Scenario: 日历在线刷新失败且无同日共识缓存

Given 当前日期是工作日但没有覆盖当日的双源日历证据
When 读取快照
Then `trading_day.confirmed=false` 且 `is_trading_day=null`。

## Requirement: 批量行情与持仓一致性

服务 MUST 对正式 TOP15 与持仓证券去重后执行一次批量行情读取。行情期间持仓 watermark 改变时，
持仓风控结果 MUST 标记 stale，不能报告为当前有效计划。

### Scenario: 十五个候选与三个持仓有重叠

When 生成快照
Then 行情适配器只收到一次去重后的证券列表，不发生逐只行情调用。

## Requirement: 安全 CLI 与显式通知

CLI MUST 从仓库外环境或凭据文件取得 Agent 地址与 Bearer，默认不得发送通知。缺少配置、鉴权
失败和服务失败 MUST 输出结构化状态与修复提示，不输出堆栈、真实主机、Token 或响应正文。
显式 QQ 发送 MUST 复用 `notification_router`，通知正文 MUST 由字段白名单生成。

### Scenario: Bearer 缺失

Given 未配置 Agent Token 或 Token 文件
When 执行 CLI
Then 输出 `status=missing`、稳定错误码与配置名提示，不发起 HTTP 或通知。

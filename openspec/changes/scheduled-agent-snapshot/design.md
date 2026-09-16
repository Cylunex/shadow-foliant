# 设计：跨运行时计划快照

## 数据流与信任边界

```text
Codex heartbeat / cron CLI
  -- HTTPS + Agent Bearer --> Foliant Agent HTTP
  -- exact capability ----> ScheduledSnapshotService
                              |-- authoritative calendar consensus
                              |-- existing cockpit / formal + independent SelectionRun artifacts
                              |-- delegated portfolio snapshot + action-plan rules
                              `-- one batched quote request
  -- explicit --send-qq --> existing notification_router --> QQ webhook
```

服务端是数据库事实与持仓授权的唯一边界。CLI 只消费有界 JSON，不读取 PostgreSQL、不加载 MCP，
也不以网络搜索补齐缺失事实。新 capability 使用 `stock.portfolio.read` scope 和
`portfolio-primary` grant，因为结果包含真实持仓；公开市场权限不能推导出该权限。

## 退化与时点

每个分区都有 `complete | stale | missing | degraded` 状态。配置、鉴权、网络或服务错误在 CLI
中映射为稳定错误码与修复提示，不回显异常、URL、Token 或响应正文。正式 TOP15/TOP5 只来自
已发布且 Manifest 完整的 SelectionRun；五组问财保持独立 reference 分区，缺失不会改变正式
候选。行情一次按“真实持仓 ∪ 正式 TOP15 ∪ 独立 TOP15 ∪ 当前外部 overlay TOP15”去重后批量
读取，并为每只证券保留 observation time；每个来源分别输出 coverage 和 missing symbols。
行时间缺失但批次接收时间可信时，快照保留该 as-of 及来源，不把所有已存价格误标为陈旧。
独立产物只是同一 PIT manifest 的可复现对照；对照计算保留输入有效性，不把缺失源当作空候选集。

`trading_day.confirmed=true` 只在仓库内双源日历共识覆盖目标自然日时成立。在线日历失败后的
weekday fallback 不进入这个合同。正式产物日期相对最近确认开市日落后时标记 stale。

盘后模式只接受当日经行情质量层标记为 `closing_current` 的收盘价格。该价格不受盘中 TTL 二次
淘汰；组合风险、持仓复盘和次日计划均绑定同一个价格批次 snapshot ID/as-of。基金仍保留在完整
持仓报告中，但继续排除在 30 万股票预算和股票组合风险口径之外。

股票现金口径固定为“30 万股票预算减非基金股票市值”。当该预算、行情与风险快照完整时，旧的
broker/confirmed cash 仅保留为 `non_blocking_metadata`，不得令 `trade_plans` 或总体质量降级。
若盘中动作未能绑定本次行情批次，动作权威仍标记 `stale_or_missing`，但在风险和定价完整、全程
preview-only 的前提下作为非阻断退化单独披露。

## 通知

CLI 默认 dry/no-send。`--send-qq` 只把字段白名单渲染成短报告，并显式调用
`notification_router.send(..., only_channels=["qq"])`。通知结果只输出成功布尔值和稳定错误码；
Router 返回的原始异常文本不进入 CLI JSON。通知正文不包含配置、URL、认证材料或任意服务错误。

## 拒绝的方案

- 在心跳 checkout 注入生产数据库：扩大凭据分发面且重现本次故障。
- 直接导入 MCP：为一个只读消费者增加 SDK 运行时耦合。
- 为快照复制 SQL 或逐股行情调用：产生第二套口径并造成无界延迟。
- 服务端自动通知：把读取与外部副作用绑定，破坏默认只读语义。

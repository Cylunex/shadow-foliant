# Design

## 数据流与信任边界

```text
正式 selection artifact + 受权真实持仓
  -> 去重监控池（holding > formal_top5 > formal_top15_watch）
  -> 正式 manifest 本地 qfq 日线 -> trade_plan 价格计划（固定节点构建）
  -> datahub.quotes(全池单次批量) -> 新鲜度/覆盖率门
  -> 持仓规则裁决 + 候选触发判断 -> 当日盘中决策快照
  -> 固定节点有界报告 / 状态变化 alert

问财 strategy artifact -> wencai_reference 分区和 09:45 对照摘要（永不进入监控池）
同一 immutable PIT manifest -> codex-independent-v1 -> 独立 TOP15/TOP5 + 战绩提名
  -> 仅对有效输入生成 formal/independent/Wencai 两两或三方集合对照
```

正式候选的 `run_id`、rank、selection as-of 随监控项保存。持仓和动态报价只进入盘中快照，不回写不可变
正式 artifact。交易计划价位由 `analysis.trade_plan` 基于正式 manifest 引用的本地 qfq 日线计算；轮询复用
已保存计划，仅刷新一次批量行情。

独立通道固定顶层权重为基本面质量 30%、中期趋势 25%、估值 20%、资金/流动性 15%、风险折价 10%。
每只候选必须具备全部必要维度，不允许因缺值静默重配权重；完整可用标的少于 15 只时整个产物不可用。
策略输入边界不包含正式/问财候选集、排名、分数或人工评审结论。

## 数据质量与失败关闭

报价优先使用提供方 `quote_time`；缺少提供方时间时显式标记为请求接收时点。显式时间超过配置阈值、
价格非正或代码缺失均不可行动。覆盖不足时任务为 `degraded`，全无有效报价时为 `error`；关键整体覆盖
低于门槛时所有明确动作失败关闭为“数据不足/暂不给价”。完整快照保留 requested/valid/stale/missing、
coverage 和 as-of。

交易日只从已持久化的两个独立源证据确定，快照读取不发起网络请求，也不用工作日推测补位。行情源有行时间则优先使用；
无行时间时保留受信批次接收时间及其来源标识。部分批次会继续向后续源只请求缺失代码，ETF/基金仍无报价时按资产类型显式降级。

## 状态机

每个 `symbol + trigger_type` 保存当前是否处于阈值内及最近提醒尝试时间。只在阈值外到阈值内时通知；
持续处于阈值内保持静默，离开后重新武装，并且至少经过 60 分钟冷却才可再次提醒。持仓最终动作只在
升级到减仓或卖出时提醒。整体行情缺失/陈旧使用同样的进入、恢复、再进入语义。

## 调度

固定节点继续使用 `unified_selection`、`morning_portfolio`、`noon_portfolio`、
`afternoon_portfolio`。新增的 interval job 只负责 20 分钟批量报价和状态变化提醒；它在非交易日、
09:30–11:30/13:00–15:00 之外（含午休）返回 skipped。

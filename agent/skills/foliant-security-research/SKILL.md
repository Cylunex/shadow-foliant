---
name: foliant-security-research
description: 读取有时间与来源边界的 A 股正式研究；缺失时创建不发布的研究预览。
---

# Foliant Security Research

## 何时使用

用户询问某只 A 股已有研究事实、数据新鲜度、风险证据或需要创建新的研究预览时使用。
证券代码必须是服务支持的 6 位 A 股代码。

## 固定顺序

1. 调用 `foliant.market.data-quality`，先确认交易日、市场数据和财务截止边界。
2. 调用 `foliant.security-research.latest` 读取已有正式快照。
3. 检查 `provenance` 中的 `market_as_of`、`financial_cutoff_at`、
   `input_manifest_id`、`policy_hash` 和 `code_revision`。
4. 正式快照缺失、过旧或来源不完整，并且用户确实需要新分析时，调用
   `foliant.security-research.preview`。
5. 使用 `foliant.run.get` 查询状态；完成后用 `foliant.run.result` 读取有界结果。
6. 在回答中把 `data` 中的确定性事实和 `derived_analysis` 明确分开。

## 降级路径

- 数据质量降级时只总结可用事实，并列出 `warnings`；不要补猜缺失财务或行情。
- preview 失败时报告稳定错误分类和可重试条件，不要求用户提供机器 Token。
- 完整结果超过预算时保留 `resource_uri` 或 `continuation`，不要反复抓取全部正文。

## 禁止行为

- 不发布正式研究、信号或选股结果。
- 不读取或修改持仓、成交、环境配置和任务。
- 不执行买卖，不声称确定收益，不把研究意见描述为事实。
- 不把日期为空的 legacy 快照包装成完整 PIT 研究。

日期核对清单见 [references/provenance-checklist.md](references/provenance-checklist.md)。

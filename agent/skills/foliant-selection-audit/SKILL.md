---
name: foliant-selection-audit
description: 审计正式 A 股 SelectionRun 的来源、完整性、集中度和降级信息。
---

# Foliant Selection Audit

## 何时使用

用户需要查看最近正式选股、解释候选来源、检查行业集中度或复核选股数据是否完整时使用。
不用于导入持仓、成交或发布新名单。

## 固定顺序

1. 调用 `foliant.market.data-quality` 获取预期交易日与覆盖率。
2. 调用 `foliant.selection.latest`，确认返回的是 `formal: true`。
3. 核对 `provenance`、正式 TOP15/TOP5 完整性、数据缺失警告和选股日期。
4. 按候选的行业、状态、相关性与 `data_coverage` 检查集中度和证据短板。
5. 只有用户要求新鲜但不发布的复核时，调用 `foliant.selection.preview`。
6. 轮询 `foliant.run.get`，完成后读取 `foliant.run.result`；明确标记 preview 不是正式名单。

## 降级路径

- 行业字段覆盖不足时，报告无法可靠判断行业集中度。
- 相关性样本不足时，不把缺失相关性当作低相关。
- Wencai 等外部发现仅作为参考，不影响 Foliant 正式本地评分。

## 禁止行为

- 不把 preview 自动发布或替换最近正式 SelectionRun。
- 不修改组合、监控、信号或任务配置。
- 不把候选列表描述为买入指令或收益承诺。
- 不通过低层仓储表、任意任务名或 Prompt 绕过稳定能力。

审计检查项见 [references/selection-audit.md](references/selection-audit.md)。

# 研究论断核验要求

## ADDED Requirements

### Requirement: Evidence cutoff
系统 MUST 使用服务端草稿时点校验 published_at ≤ first_seen_at ≤ as_of，按真实带时区时刻比较。

#### Scenario: Future observation
引用虽已发布但首次观测晚于草稿时点时，保存 MUST 拒绝且不得写入新版本。

### Requirement: Arithmetic is distinct from entailment
系统 MUST 分别报告数字重算与文字结论状态，不得仅因数字算对而标记文字 supported。

#### Scenario: Unsupported causal wording
用户提交正确增长计算和“因此股价一定上涨”时，数字可重算，但结论 MUST 保持未核实。

### Requirement: Read-time compatibility
系统 MUST 保留历史原文和追加记录，读时重新评估旧 claim，不信任客户端核验结果。

#### Scenario: Legacy or forged support
旧 supported 或伪造 verification 字段 MUST 不能使未核实结论通过，异常引用不得使其他档案不可读。

### Requirement: Workflow visibility
私人查询和 Web MUST 展示逐项检查、失效状态和引用定位，公共研究查询 MUST 不包含私人论点。

#### Scenario: Lock and repeat
重复锁定同一草稿 MUST 幂等，沿用草稿证据截止时点，并保留未核实状态。

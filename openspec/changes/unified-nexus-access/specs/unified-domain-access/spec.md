# ADDED Requirements

## Requirement: 中央身份与领域资源权限

Foliant MUST 使用 Platform 已验证 Principal；MUST 在本地继续校验组合、Run 与来源范围；MUST NOT 从请求 owner、owner_app 或同名账户推断所有权。

### Scenario: 其他用户请求主组合

Given 中央 Session 有效但不属于该 instance 的指定组合 owner
When 请求 portfolio-primary
Then 返回无权访问且不泄漏持仓或统计。

## Requirement: 普通事实与研究任务

系统 MUST 区分 accepted Run、已提交历史交易事实和真实券商执行；明确且获授权的普通交易事实导入 SHOULD 无二次审核。

### Scenario: 导入响应丢失

Given 当前意图绑定同 command/hash 且领域已提交
When Host 查询或同键重试
Then 返回原 Receipt，不重复变化持仓，不报告下单成功。

## Requirement: 撤销与兼容

中央模式 MUST NOT 在鉴权失败时回退旧 token；新副作用阶段 MUST 重查授权，旧任务 lease MUST NOT 覆盖新终态。

### Scenario: 任务授权撤回

Given preview 任务已入队但下一模型调用尚未开始
When delegation 被撤销
Then 不开始新模型调用，保留已有冻结事实与可审计状态。

## Requirement: 模型披露与计算

通用 Agent/模型连接 MUST 由 Platform 维护，领域 MUST 保留确定性计算、Prompt 语义和数据源预算；provider fallback MUST NOT 扩大私人持仓的披露范围。

### Scenario: 备用模型未获数据用途许可

Given 主 Provider 不可用且备用 Provider 未获该持仓用途授权
When 请求 fallback
Then 返回模型不可用，不外发私人输入。

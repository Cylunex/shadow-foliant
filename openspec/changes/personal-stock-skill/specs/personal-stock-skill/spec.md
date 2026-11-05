# Personal Stock Skill

## 项目维护

Foliant MUST 提供不依赖 MCP 的 JSON CLI、严格参数目录和日常 skill；Losna MUST 消费该目录，不复制业务参数定义。

Scenario: 执行 catalog，返回成交、持仓、市场、研究、监控、策略、任务和基金能力以及实际 schema；查询配置排除写入。

## 成交事实

成交查询 MUST 按实际成交时间倒序，并区分导入日期与成交日期。导入 MUST 使用现有校验、持仓水位、原子提交、防重与回执。

Scenario: 合成买入 100 股后，原键重试只产生一条成交和 100 股持仓；limit=1 返回该成交，查询不产生写入任务；超卖缺口不写入。

## 个人参数

参数调整 MUST 验证既有 key、类型、范围和关联阈值；提交 MUST 核对 expected_value，返回修改前后实际值。新接口 MUST NOT 发布正式策略或执行证券委托。

Scenario: 预览不改变参数；第一次提交成功；另一请求携带旧值时返回 conflict，当前值保持不变。

## 聊天与工作

前台 MUST 直接查询股票事实，MUST NOT 执行写命令；明确的录入与调整由原通用工作调用相同 skill。断线或调用结果未知 MUST 保留核对原回执的恢复语义。

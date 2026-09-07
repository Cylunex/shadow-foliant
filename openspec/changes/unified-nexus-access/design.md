# 信任边界与数据流

唯一公共协议见 [Platform UA-1](https://github.com/Cylunex/shadow-platform/blob/main/docs/nexus-unified-access-design.md)，领域差异见 [详细设计](../../../docs/nexus-integration-design.md)。

Nexus/统一 MCP → 中央目录与当前意图 → Access 单 audience/instance 票据 → Foliant SDK Principal → 本地组合/Run/来源权限 → 应用服务/领域任务 → Receipt/结果。模型输入经中央 Model Gateway，模板与数值事实留领域，敏感 provider fallback 不扩权。

中央负责身份、Session、委托和确认，Foliant 负责资源/数据语义。拒绝每域独立 registry/通用 Agent loop、域间 Token 透传、中央复制持仓库、把 trade_fact.import 作为券商下单、按用户名推断全局组合 owner。

迁移先影子比对，再单能力 central；已 claim 操作按公共线性化/幂等语义恢复。中央故障暂停新 Agent 副作用，已冻结数据和已提交记录不回滚。现有 Run worker 继续执行确定性领取/恢复，新模型/写入阶段重查授权。

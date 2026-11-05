# 股票项目自有日常 Skill

Losna 已有冻结研究包通道，但日常聊天不能发现成交查询；旧持仓 skill 依赖 Nexus Proposal，无法作为通用 CLI 操作指引。

由 Foliant 维护日常 skill、JSON 命令目录与业务执行，覆盖成交录入/查询、持仓收益、行情研究、监控、个人策略参数及基金常用查询。Losna 使用独立 `foliantPersonal` 连接发现和调用，原 `foliant` / `foliantPortfolio` 研究连接保持原用途。

复用现有成交批次、版本检查、原子导入和幂等回执；普通操作无新增审核页面。实际下单、撤单、正式策略自动晋级和任务投递不纳入新目录。生产切换按明确部署任务执行，旧 Nexus 合同保留兼容。

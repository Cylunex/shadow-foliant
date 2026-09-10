# 计划任务只读数据快照

状态：实施中；2026-09-10。

## 动机

Codex 本地心跳不能假定仓库 checkout 持有 PostgreSQL、Agent SDK 或通知凭据。直接导入
`jobs.task_control` 会把本地进程误当成生产应用，配置缺失时以堆栈退出，也绕过了已经定义的
Agent HTTP 鉴权与个人组合授权边界。

## 范围

新增一个受保护、只读、有界的计划任务快照能力。Foliant 服务端聚合现有正式选股、问财参考、
cockpit、个人持仓、行动预览和批量行情；仓库外调用方通过稳定 CLI 使用 Agent Bearer 读取。
CLI 默认不通知，只有显式参数才调用既有 `notification_router` 的 QQ 渠道。

## 兼容与非目标

- 不新增数据库、Webhook、Cookie 或 Bearer 的仓库内配置，也不提供本地空库回退。
- 不导入 `mcp_server.py`，不复制正式选股或持仓 SQL，不新增下单能力。
- 不把“仅排除周末”的在线日历回退视作权威交易日；缺少双源日历证据时返回 unknown。
- 现有研究 Profile 不获得私有快照能力；部署侧必须给专用 Agent 配置精确 capability、scope
  和 `portfolio-primary` resource grant。

# 统一消息渠道存档

## 记录范围

`notification_messages` 保存一条逻辑消息的来源模块、任务/运行 ID、类别、标题、未压缩原稿、生成时间、业务时间、幂等键、敏感级别与格式版本。`notification_deliveries` 对每个实际渠道分别保存最终提交正文、正文 SHA-256、非敏感渠道标签、计划/尝试/确认时间、HTTP 或应用层回执、状态，以及兜底和重试关联。QQ 只是 `qq` 渠道；同一消息发邮件和 QQ 时是一个逻辑消息、两条独立投递。运输端接受不证明客户端已展示。

当前接入：统一路由的 QQ、邮件、钉钉、飞书、企业微信、Telegram、Discord、Slack；定时快照 QQ；`NotificationService` 通用分析邮件/Webhook、持仓分析、监控紧急直发与显式配置测试；妙想第二意见、每日扫描、智策板块定时报告及 Telegram 交互回复。配置测试标为 `test`，不混入计划任务。统一路由保持原有分类路由及显式兜底规则；每日扫描只有一次 QQ 尝试，QQ 失败时在同一逻辑消息下尝试邮件。已停用的历史发送器不作为计划任务计数。存档基础设施失效时监控紧急直发仍可运行，运行日志会标识存档缺口。

渠道正文只以 Fernet 密文存库，私钥在发布目录外的受限文件中，权限 0600，由部署脚本首次生成并在后续发布复用。数据库和私钥必须分别备份；遗失私钥会使旧正文无法解密。表内不保存 Webhook URL、Token 或收件地址，回执只记录白名单状态码和错误码。没有自动清理或截断存档正文；超过 16 MiB 的单项正文明确拒绝存档，正常发送仍继续并在日志留下无正文的存档缺口。存档系统故障不会静默取消告警或报告发送。

## 查询与导出

受保护机器接口 `/api/machine/v1/agent/message-archive` 支持 `from_date`、`to_date`、`offset`、`limit`（每页最多 50），列表只返回元数据和哈希。`/{message_id}` 返回授权范围内的解密原稿及各渠道正文。`/export` 在日期范围内按页输出 NDJSON；以 `X-Archive-Has-More` 和下一页 `offset` 继续，响应均禁止缓存。读取需要 `stock.portfolio.read` / `foliant.scheduled-report.read`，写入需要 `stock.research` / `foliant.selection.preview`。不要把导出内容复制到公开日志。

状态：`prepared` 表示未尝试，`sending`/`unknown` 表示结果不确定，`accepted` 是提供者接受，`failed` 是明确失败；一条逻辑消息的渠道结果不一致时标 `partial`。同幂等键重复调用会保留原状态并累计 `suppressed_count`，不会再次发送。不确定结果不能自动重试；如要显式重试，创建新逻辑消息并填写 `retry_of`。`fallback_from` 连接同一逻辑消息的兜底渠道。

上线前的实际发送正文、提供者回执和客户端显示状态无法从旧日志反推；迁移不伪造历史消息。旧定时 QQ slot 审计继续独立保留，用于定时去重和历史观察缺口。

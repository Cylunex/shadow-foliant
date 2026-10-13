# 定时 QQ 摘要与按槽送达审计

定时报告只通过仓库外受保护启动器调用 `scripts/foliant_scheduled_snapshot.py`。启动器注入既有只读快照凭据、独立研究写凭据和 QQ Webhook；它们不进入仓库、命令行参数或开放日志。

## 调用顺序

每个工作日 10:15、11:25、14:35、20:45（Asia/Shanghai）按以下顺序执行：

1. 不带发送参数调用 CLI，只读获取完整 `scheduled-agent-snapshot-v1`，核对交易日、正式 `selection_run_id`、独立量化底座版本、持仓、权威计划与质量缺口。此阶段不领取通知 slot。
2. 在公开来源上完成当期独立研究并锁定排名；把严格的 `codex-external-independent-v1` bundle 从标准输入交给受保护启动器的 `--receive-bundle`。启动器限制 256 KiB、校验 JSON 对象，在私有临时目录以 0600 权限保存并返回路径、大小和 SHA-256；研究失败或超时就记录降级，不复用旧排名。
3. 在同一正常时点只调用一次受保护启动器的 `--external-bundle <返回路径> --send-qq --notification-slot <HH:MM>`。CLI 先提交 bundle，再重新读取合并快照，先校验必需章节和本次外部 overlay，接着生成 QQ 摘要并领取 slot。启动器消费后删除 bundle，且定期清理超过 24 小时的孤儿文件。研究失败时省略 `--external-bundle`，仍调用一次 `--send-qq --notification-slot <HH:MM>` 发送真实降级摘要。
4. 受保护启动器在 `--send-qq` 时让 CLI 输出 `scheduled-delivery-receipt-v1` 有界回执，包含本次观测时间、快照时间、`notification.delivery_status`、`notification.sent`、`notification.prior_sent`、`notification.delivery_recorded`、`notification.message_archive_status`、`notification.message_archive_id` 和 `notification.notification_slot`。客户端只解析这份小回执。只有本次 HTTP 200/204 且审计落库才算 **Webhook 已接受**；这不是 QQ 客户端实际展示的证明。`prior_sent` 仅说明此前 Webhook 已接受。同槽 `suppressed`、`unknown`、`failed` 均不得当作本次送达，也不得人工补发。

`delivery_recorded` 只表示独立的通知 slot 审计已确认，不表示正文已存档。`message_archive_id` 只在存档 `prepare` 得到确认后返回，仍不足以证明渠道投递回执已写入；以 `message_archive_status=recorded` 判断该次存档 `finish` 已确认。存档准备或完成不确定时如实返回 `unrecorded` 或 `unknown`，不据此重发同槽 QQ。计划 slot 的幂等键保留 `+08:00` 时区偏移，存档服务允许该字符并继续拒绝控制字符。

无发送检查：不带参数读取完整快照，`--summary-only` 读取有界概览；`--audit-notifications` 只读取最近 14 天、最多 56 条脱敏审计，不能与发送或 bundle 参数并用。直接调用 CLI 时，`--delivery-receipt` 必须与 `--send-qq` 同用；启动器自动传递此参数。审计包括计划 slot、claim/尝试/确认时间、标题及有界正文的 SHA-256、压缩前后行数、类别、HTTP 状态、错误码、最终状态、版本、去重原因和次数。不返回消息正文、Webhook、Token、持仓内容或账户地址。2026-09-24 前的旧 claim 只迁移已知状态；当时未保存的哈希、HTTP 与压缩计数以零值或空值表示，不能反推为成功。工作日没有任何 ledger 行的计划 slot 标为 `unobserved`，表示历史证据缺口，不等于未发送；节假日也只标观察缺口，不推断原调度一定执行。

QQ 正文优先逐只展示当期正式 TOP5 与独立量化 TOP5 的去重结果，格式为“编号｜名称｜现价｜来源”；有意义的持仓风险另列“编号｜名称｜现价｜操作｜原因”。编号固定六位、保留前导零。来源仅标正式、独立量化或两者交集；外部独立研究和问财不冒充这两条榜单。现价只来自同批受保护快照中经新鲜度与交易日核验的 quotes，并附行情时点；现价并非可执行触发价。缺失或过期写“现价待核验”，名称冲突写“名称待核验”。只有权威执行条件齐备才直接写卖出/减仓；其他风险写观察，组合保护后的 hold 写受保护观察。普通 hold 不逐只发送。

正文最多 24 行、1500 字；超限时保留完整行并明确还有几条未展示，不做静默尾部截断。完整持仓、TOP15、风险与失效条件、归因及未压缩原稿仍在受保护快照和消息正文存档；QQ 仅展示有界结果。常规质量状态、预算、榜单数量、交集计数、内部错误码和盘后未到期状态不占正文。

快照的 `notification_policy.mode=critical_only` 和 `scheduled_delivery=silent_monitor_owns_critical_events` 仅描述盘中最高事件的发送权。`report_delivery=scheduled_slot_report` 与 `critical_event_delivery=intraday_monitor_only` 明确分流：四个正常时点的 QQ `report` 按 slot 发送，允许研究来源和部分计划降级；最高事件只由盘中共用监控以 `alert` 类别发送。`eligible_new_event_count=0` 或执行门禁未核验不取消报告。执行价位未核验只影响操作栏，不能遮蔽有效的同批行情现价。外部独立研究缺失时，正文仍展示正式与独立量化的当期名单。

## 发送语义与切换

领取 slot 前完成快照、Webhook 存在性及摘要长度校验。`claimed` 且尚无尝试时间可由同槽后续启动恢复；发送前以条件更新写入 `sending/attempted_at`，只有成功更新的调用方才发 HTTP。HTTP 200/204 记 `delivered`，明确 HTTP 拒绝记 `failed`，网络超时、进程中断或落库不确定记 `unknown`。已开始的尝试不自动重发，避免不确定回执造成双发。slot 与外部 overlay 幂等键解耦；同一 slot 最多一次 Webhook 尝试。过往外部 claim 迁入新 ledger，避免发布恰逢旧 slot 时重复发送。

发布顺序：先部署包含 migration 的服务端和 CLI，做无发送快照及审计检查，再由主计划会话切换心跳提示顺序。切换前旧调度不应在部署窗口运行；如部署窗口恰逢正常 slot，等待该时点结束再切换。回滚时保留 append-only 审计表，不恢复数据库；把心跳临时切回只读模式，待 ledger 兼容版本恢复后再启用发送。正式 QQ 效果只在下一正常时点观察，不手动补发。

仓库外受保护启动器更新必须使用 `scripts/install_scheduled_heartbeat.py`：先核对候选文件 SHA-256 和版本，再在同一目录建立旧版备份，把新文件权限设置为私有可执行的 0700 后原子替换。`scripts/deploy.sh` 在受控发布时检查该入口是否为可执行的 0700。安装后直接执行受保护入口的 `--version`、无参数完整只读快照和 `--summary-only`，确认机器回包可解析且 `notification.requested=false/sent=false`。如果某个 slot 因入口权限或进程故障错过，不改用 Python 绕过启动器，也不补发旧 slot。

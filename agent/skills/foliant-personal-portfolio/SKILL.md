---
name: foliant-personal-portfolio
description: 查询股票成交、持仓收益、行情研究、选股和基金；录入已成交交易，管理盯盘及调整 Foliant 个人策略参数。用于个人股票日常操作，不用于券商下单或撤单。
---

# Foliant 日常股票操作

股票项目维护本 skill、命令目录和业务逻辑。Foliant 账本是成交与持仓的事实来源，聊天记忆和研究建议不能替代账本。

## 入口

Losna 中调用 `foliant_skill`：`action=catalog` 按需查看命令名，`action=describe` 配合 `name` 读取参数和本 skill；`action=call` 配合 `name`、`arguments` 执行。聊天直接提供查询；明确的录入和调整通过 `companion_work` 的通用工作完成，工作中使用同一个工具。

其他受信任的本机 Agent 使用 Foliant Python 环境执行：

```sh
python /path/to/shadow-foliant/agent/skills/foliant-personal-portfolio/scripts/foliant --config /path/to/private/personal.json catalog
printf '%s' '{"limit":1}' | python /path/to/shadow-foliant/agent/skills/foliant-personal-portfolio/scripts/foliant --config /path/to/private/personal.json call trades.list
```

JSON 从 stdin 传入，stdout 是一个 JSON 结果。配置只指定固定 `actor` 和 `allow_writes`；业务连接沿用股票运行环境的私有配置。不要将凭据、真实成交样例或运行路径复制进 skill。此入口不使用 MCP，也不依赖 Nexus。只有尚未迁移、仅提供 `foliant.portfolio.summary` 等旧工具的 Nexus Host，才使用 [旧 Host 兼容流程](references/nexus-compatibility.md)；不要在 Losna 创建旧 Proposal。

常用命令导航见 [references/commands.md](references/commands.md)，参数以运行时 `catalog` 为准。旧后台 `foliant-operator` 仍仅领取和提交冻结研究包，不承担日常账本操作。

## 成交录入与查看

- “最后一笔是哪天的什么票”：直接 `trades.list {"limit":1}`，按 `trade_time` 回答日期、名称/代码、买卖方向；无记录就明确说明。不要把 `created_at` 导入时间当成交时间。
- 指定成交日用 `trade_date`，指定录入日用 `import_date`，不能同时传。使用上海时间解释无时区的用户日期。返回数量达到 limit 时不要宣称已经读完全部历史。
- 用户明确要求记下**已经成交**的事实时，使用 `trades.import`。支持 `rows` 或原始 Markdown `table`，二选一；必需事实是名称或代码、成交时间、方向、数量和价格。名称可以由服务批量解析；代码歧义、日期或买卖方向不明时只问缺少的事实。截图看不清时不猜。
- 保留已提供费用、券商成交编号、委托编号与账户标识，不补造手续费。默认 `update_position=true`；仅在用户要求只记流水时设为 false。计划买卖和研究建议不录为成交。
- 同一笔录入使用稳定的 `idempotency_key`。提交中断后，用原配置身份调用 `trades.receipt`，传 `batch_id` 或原 `idempotency_key` 其中一个。该查询只读，返回批次当前状态。`write_in_progress` 表示创建仍在进行，稍后再查。`trade_review_not_found` 表示该身份尚未查到对应批次。需重试录入时复用原键和原始参数；同键改动内容会返回 `idempotency_conflict`。
- 只有回执 `status=complete` 且存在 `receipt` 才说明成功。回执中的 `record_count` 是本批记录数，`replayed=true` 是重读原回执，不是再次新增。最后按相关代码/成交日 `trades.list` 和 `portfolio.summary` 核对；回读失败应说明已提交但核对未完成，不能重新造一笔。
- `needs_input/conflict/partial/failed` 按真实状态解释；不要通过关闭持仓更新绕过超卖或冲突。

## 调整与研究

查询和建议本身不授权修改。用户明确给定参数时先 `strategy.parameters` 获取当前值与含义，再 `strategy.update` 提交单个 key、value、expected_value、reason。默认 dry_run=true 可在后台预览；符合原委托时直接 dry_run=false 并回读。冲突先重读，不覆盖并发改动。只笼统说“优化一下”时先结合回测和证据提出具体改动，缺少会改变业务含义的目标再询问。恢复默认值也走版本检查。修改与原因在同一事务保存回执，`strategy.history` 可回查。

盯盘和推荐/信号管理同样保留 dry_run、before/after 与回读；明确的普通委托不额外索要确认。开启对外提醒必须有该意图的授权。实际证券委托、批量删除账本、修改凭据及正式策略自动晋级不在此目录。

优先已有快照与 quick 研究；深度研究会消耗模型预算。保留数据 as-of、质量、缺失和陈旧状态。TOP15 观察、TOP5 正式候选、私人持仓和外部参考分别解释；方向后验不能冒充含费用净收益。任务 queued/running 不是完成，degraded/partial/timeout 不改写成成功。基金净值与估值、成本与市值必须区分。

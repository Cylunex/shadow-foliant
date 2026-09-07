# Foliant 接入 Nexus 的详细设计

设计版本：2026-09-07 / UA-1。状态：目标设计，尚未实现。公共身份、鉴权、Agent、模型、命令与回执以 [Platform 统一规范](https://github.com/Cylunex/shadow-platform/blob/main/docs/nexus-unified-access-design.md) 为准；本文仅定义本领域差异。旧接口安全限制在对应能力通过迁移验收前继续生效。

## 1. 基线与统一范围

基线 `f0b76a9`。Foliant 有应用服务、PIT/冻结研究、持久 preview Run、worker lease、预算、TradeEntryService 和全局主组合边界。`webui/platform_auth.py` 自行维护 OIDC/Session/机器鉴权；LLM/工具调度在多个模块分散实现。

统一迁出登录/Session/Agent 注册、通用工具目录、模型 transport/fallback、通用 loop/确认。Foliant 保留 Prompt/Skill/研究 eval、所有行情与财务计算、持久业务任务、数据源配额、PIT/快照和持仓事实。统一模型预算不替代行情数据源预算，两者分别记录。

架构变更同时记录于 [OpenSpec proposal](../openspec/changes/unified-nexus-access/proposal.md)，任务未完成不得标为已实现。

## 2. 身份与资源权限

Stock Web 使用中央 SDK Session，保持 `stock-users`/管理员等应用角色映射的校验；机器从中央 Principal 得到真实 user/Agent，不能以 `owner_app=nexus` 本身授予组合权限。

当前 `portfolio-primary` 是受控单 owner/global 数据，不是天然多租户。中央明确将该 instance/resource 绑定到唯一核验 owner，其他用户拒绝；待领域完成所有权迁移后才开放更多 owner，不能仅新增 user_id 字段就宣称隔离完成。

公共行情、研究资料、私人持仓、成交与任务控制分别声明 capability/disclosure。当前逐路由分类、默认拒绝保留；目录裁剪由中央维护，但 handler 仍检查资源 owner、run creator/可见性及用途。

## 3. 领域能力和参数

| 操作 | 输入/约束 | 结果与权限 |
| --- | --- | --- |
| `market/quality.read` | 交易日、as_of、来源/覆盖范围 | public/internal 投影；缺失/陈旧明确 |
| `security/selection.read` | symbol、run/snapshot/as_of、限定模式 | 正式冻结结果与来源引用，不混入当前行情冒充历史 |
| `research/selection/backtest.preview` | 受控 specification、budget、as_of、scope | accepted run_ref；不发布策略或触发实盘 |
| `run.status/result/cancel` | exact run_ref | 当前授权的自身 Run；取消和旧 lease 失效 |
| `portfolio.read` | 明确组合引用、as_of/口径 | 私人只读，不因市场读取权限开放 |
| `trade_fact.import` | 冻结规范行、preview hash、组合水位、日期/费用 | current_intent direct；只记录已发生交易 |
| `research_note.save/update` | 研究引用、内容、revision | 私人草稿；模型观点与用户认可区分 |
| `monitor.configure` | 范围、周期、预算、通知目标 | 配置 standing policy；外部通知/权限范围变化内联确认 |

新能力名为设计示意，现有 `foliant.trade.import` 和短 scope 不仅为命名统一就破坏性重命名。Schema/operation 映射可提供兼容别名，但同一命令只走一条执行路径。

## 4. 研究任务与模型运行

Web/Agent/MCP 适配共同调用 application services，领域 worker 保留 SKIP LOCKED、lease token、子进程终止与 Outbox。统一的 Agent Runtime 负责对话工具选择；确定性 worker 不必通过 Agent loop 启动。

模型调用改走 Platform Model Gateway，按领域模板/版本、所需数据、disclosure、run_ref 和 budget reservation 请求。领域数值服务负责数据/金额计算，Gateway 不重新解释资金口径。token/时间/成本预算与行情采集预算分别限制，fallback 不能扩大持仓/敏感数据外发。

任务入队时存中央 user/Agent/delegation、command、capability 和输入 hash，重领不更换 command。进入新的模型/写入阶段重新授权，撤销后不启动新阶段；已经产出的可读结果按当前读取权限投影，不能为了取消任务删除已有冻结证据。

## 5. 已成交事实导入

复用 TradeEntryService 的 preview/confirm 和持仓水位检查，将批准来源改为 verified current_intent/central decision。用户给出已成交记录不再被要求跳 Stock 页面批准，但缺买卖方向、数量或关键事实要就地追问，不把下单意图解释成已成交。

preview 的规范行、费用、交易日期、组合水位由服务计算；Host 不自行造 hash，提交与组合版本不符返回冲突。交易记录/持仓变化/Receipt 同事务或沿用服务已有原子保证；失败不把部分结果说成完整导入。

Receipt 明确 source=historical_trade_fact、command_id、batch/row refs、affected position version，不能称为券商成交回报。真实下单、撤单、实盘策略启停当前不在接入范围。

## 6. Nexus Surface 与披露

summary 展示覆盖/观察时间与正式候选，portfolio 是独立受限卡片；search/result 仅提供有界摘要和稳定证据引用。提供 run-status 和 cancellation 命令，不把 run accepted 显示为研究完成。数字引用保留单位、币种、时点、exact/partial/projected，不用统一 UI 抹掉数据质量。

人工论点确认是研究事实：“保存模型论点草稿”可以直接做，“用户认可”必须来自真实用户事件，不能因取消审批界面而由模型自动标记。

## 7. 迁移与验收

中央影子鉴权 → 公共研究读取 → 私人组合 exact owner → preview Run → 历史交易事实命令 → Model Gateway/统一 MCP → 注销旧认证/独立工具循环。对旧功能保留实现说明，不恢复已被收紧的匿名全局 API。

验收覆盖全局组合跨用户拒绝、旧 scope/目录、不同时点/来源数据不得混算、取消后旧 worker 无法发结果、Access 故障/撤销、预算并发与超时结算、trade preview 水位冲突、同键重放、模型不冒充实盘成交。使用 `test_shadow_plugin_contract.py`、研究/决策合同及运行任务回归；生产模型/券商联调不在设计验收中。

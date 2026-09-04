# ADDED Requirements

以下是待实施规范，不表示基线已满足。

## Requirement: Versioned decision facts

系统 MUST 区分冻结研究工件与动态报价/持仓状态。正式工件 MUST 绑定数据、策略、特征与代码版本。

### Scenario: Read the same context tomorrow

- GIVEN 同一正式 run 已生成冻结工件
- WHEN 次日读取上下文且实时行情改变
- THEN 冻结内容哈希 MUST 不变；动态 envelope MUST 展示独立时间戳与权限。

## Requirement: Controlled source composition

系统 MUST 允许合规的冻结前多源补齐，并 MUST 保存字段来源、有效日期和合成规则版本。
系统 MUST NOT 在冻结后静默修改原 manifest 所引用的值。

### Scenario: Primary valuation recovers after publication

- GIVEN 正式运行引用按规则合成的估值快照
- WHEN 主源随后返回更多数据
- THEN 系统 MUST 发布新 generation；原 run 重放 MUST 使用原快照。

## Requirement: Consumption readiness

系统 MUST 分别报告完整入库与决策可用性，SHOULD 从版本化质量报告读取健康状态。

### Scenario: Market coverage is high but optional fields are missing

- GIVEN 行情覆盖达门槛且当前消费者的必需字段齐全
- WHEN 可选数据缺失
- THEN 消费者 MAY 以显式降级继续；系统 MUST NOT 伪装全部入库完成。

### Scenario: Cached quality report is obsolete

- GIVEN 缓存报告与当前 publication generation 不匹配
- WHEN 请求数据就绪状态
- THEN 系统 MUST 返回过期/重评估状态，MUST NOT 继续返回无条件 ready。

## Requirement: Strict bounded policy changes

政策控制器 MUST 拒绝重复字段、bool 数字、非整数配额和非有限数字，并相对原基线计算整体步长。
增加风险暴露 MUST 有足够成熟证据；数值减少 MUST NOT 自动视为风险减少。

### Scenario: Repeated small steps bypass the weekly bound

- GIVEN 周步长为 0.1，原值 1.0
- WHEN 同一提案包含 1.0→0.9 与 0.9→0.8
- THEN 控制器 MUST 拒绝，当前政策 MUST 保持不变。

### Scenario: Boolean submitted as a quota

- GIVEN 配额字段只允许整数
- WHEN to=true
- THEN schema MUST 拒绝，MUST NOT 转成 1 发布。

### Scenario: Another policy is activated during validation

- GIVEN 提案基于 policy A
- WHEN 发布前 active 已变成 policy B
- THEN 原子比较 MUST 失败；系统 MUST NOT 覆盖 B。

## Requirement: Separate return meanings

系统 MUST 区分信号价格后验、可成交模型账本和真实账户。
MAE 与净值峰谷最大回撤 MUST 使用不同指标键和版本。

### Scenario: Price rises and then retreats above entry

- GIVEN 观察序列为 100→120→110，入场基准 100
- WHEN 计算峰谷回撤与相对入场不利变动
- THEN 回撤 MUST 约为 -8.3333%，MAE MUST 为 0；二者 MUST NOT 混用。

### Scenario: No valid executable opening price

- GIVEN 预定执行时点 open 缺失或不可成交
- WHEN 结算模型账户
- THEN 系统 MUST 记录缺数据/未成交或显式另一个执行模型；MUST NOT 静默用 close 伪造成交。

## Requirement: Execution follows publication

模型账本 MUST 以真实决策发布时间与品种可交易规则决定 earliest_execution_at。
历史补算 MUST 标记 backfilled，MUST NOT 冒充当时已发布的前向结果。

### Scenario: Selection completes after the market opens

- GIVEN 正式选择在 09:45 之后发布
- WHEN 评估该选择的执行
- THEN MUST NOT 使用同日 09:30 成交；仅有日线时 SHOULD 采用声明过的下一交易日开盘模型。

### Scenario: One-day label for a T+1 equity

- GIVEN 股票买入后当日不可卖出
- WHEN 生成 1 日价格标签
- THEN MAY 展示价格观察；MUST NOT 描述成当日可完成的净交易收益。

## Requirement: Portfolio action is a preview

行动计划 MUST 引用正式机会、受权持仓版本和报价时间，MUST NOT 改写原排名或生成真实成交。

### Scenario: Adding exposure is allowed but no stock qualifies

- GIVEN 市场层面允许加仓，但候选均未满足账户约束
- WHEN 生成用户行动摘要
- THEN MUST 明确没有合格标的且暂不操作，MUST NOT 输出“必须买入”或强凑名单。

### Scenario: Holdings change after a preview

- GIVEN 草案基于 portfolio version A
- WHEN 用户导入新成交得到 version B
- THEN 旧草案 MUST 标记过期，重新计算前 MUST NOT 表示仍可直接执行。

## Requirement: Evaluation evidence is governed

系统 MUST 保存所有 trial、失败和数据/代码版本；MUST 处理时间重叠及重复样本。
探索性数据 MUST NOT 自动通过正式模型晋级。

### Scenario: Many nominations on a few dates

- GIVEN 100 条提名来自 2 个决策日且持有区间高度重叠
- WHEN 判断晋级证据
- THEN MUST NOT 把它当作 100 个独立样本，MUST 报告独立日期与有效样本限制。

### Scenario: Holdout has already informed research

- GIVEN 某封存区间的评估结果已用于修改候选
- WHEN 再评估修改后的候选
- THEN 该区间 MUST 不再标记盲测；需要新的封存批次或明确证据不足。

## Requirement: Agent isolation and least privilege

研究 Worker MUST 无生产写权限和封存集读取权限；普通研究 Profile MUST 无个人持仓上下文。

### Scenario: Public research token resolves a private context ID

- GIVEN 引用 ID 对应个人账户行动工件
- WHEN 普通研究 token 请求该工件
- THEN 系统 MUST 拒绝，缓存与元数据 MUST NOT 泄漏个人账户信息。

### Scenario: Research code attempts production access

- GIVEN 研究 Worker 执行候选因子代码
- WHEN 代码尝试读取宿主秘密、访问生产数据库或超预算请求外部接口
- THEN 隔离边界 MUST 阻止并记录安全错误，不影响正式服务。

## Requirement: Direct rollout without fabricated validation

通过验收的基础设施 MAY 直接启用，MUST NOT 强制整个系统等待统一影子期。
新策略获得正式资格 MUST 仍满足其独立证据门。

### Scenario: Forward ledger starts with a new release

- GIVEN 新账本代码和迁移通过验收
- WHEN 用户批准部署
- THEN 账本 MAY 当日开始记录；尚未成熟的收益 MUST 显示 pending，而不是被当作有效性证据。

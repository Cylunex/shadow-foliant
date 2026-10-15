# 基金研究与核算要求

## ADDED Requirements

### Requirement: Coverage and provenance

系统 MUST 保存请求区间、实际覆盖、来源上游、净值归属日、首次采集时点和数据版本。
系统 MUST 区分部分数据、正常待披露、超期、异常与不支持；功能关闭不得伪装成源故障。

#### Scenario: Truncated pagination

GIVEN 上游总计 400 条而每次只返回 20 条
WHEN 请求完整区间
THEN 系统 MUST 继续依据响应分页信息和日期进度取数，或显式返回 partial；
MUST NOT 仅因 20 小于请求的 200 就把历史标为完整。

#### Scenario: Missing date and expected lag

GIVEN 一只基金净值缺日期，另一只 QDII 在产品正常披露等待期
WHEN 生成持仓摘要
THEN 系统 MUST 分别保留缺失日期与 pending_disclosure；MUST NOT 写入今天来冒充已确认净值。

### Requirement: Return and cash flow semantics

系统 MUST 区分单位净值、总回报、估值和成交价；MUST 计入适用的权益事件。
组合回撤 MUST 排除外部资金流的直接影响；XIRR 与时间加权收益 MUST 明确标示各自口径。

#### Scenario: Cash dividend

GIVEN 单位净值从 1.0 变为 0.9，同期每份派现 0.1，无其他收益和费用
WHEN 计算含现金分红总收益
THEN 总收益 MUST 为 0，不能把除息记成亏损 10%。

#### Scenario: New contribution during drawdown

GIVEN 先按净值 1.0 投入 100 元，净值降至 0.5 时再投入 100 元，无费用
WHEN 计算现金流中性回撤
THEN 回撤 MUST 为 50%，不能因市值升至 150 元而返回 0。

### Requirement: Confirmed transaction ledger

系统 MUST 将计划、模拟交易和真实成交分开；MUST 对账户、费用、份额、日期、幂等与冲正保留审计。
无效交易类型和超额赎回 MUST 被拒绝，不能静默转为其他类型或截断余额。

#### Scenario: Plan with an old confirmed NAV

GIVEN 定投计划到期，只有上一日确认净值，没有成交回执
WHEN 运行到期处理
THEN 系统 MUST 只产生待确认事项或明确的模拟记录；MUST NOT 更新真实已确认持仓。

#### Scenario: Repeated import

GIVEN 相同外部交易 ID 或稳定导入指纹被重复或并发提交
WHEN 保存已确认流水
THEN 系统 MUST 只产生一次持仓变化；不同账户或不同计划的合法交易 MUST 保持独立。

### Requirement: Consistent plan calendar

定投提醒、模拟和确认流程 MUST 使用同一计划日历、锚点、顺延规则和唯一键。

#### Scenario: Biweekly and partial month

GIVEN 有确定锚点的双周计划，以及计划日为 28 日但回测止于当月 20 日的月投计划
WHEN 计算执行日期
THEN 双周计划 MUST NOT 连续两周触发；月投计划 MUST NOT 提前至当月月初执行。

#### Scenario: QDII product calendar

GIVEN 境外基金未提供已核验的产品开放日
WHEN 生成到期事项
THEN 系统 MUST 保留日历待核验状态，MUST NOT 自动使用 A 股日历替代。

### Requirement: Point in time and fair comparisons

系统 MUST 保存信号已知时点、执行规则、输入快照和资金可用计划。
同类比较 MUST 使用可解释的基准、观察窗口与缺失策略；必要筛选字段缺值 MUST NOT 判为通过。

#### Scenario: Late valuation signal

GIVEN 净值或估值在当日申购截止之后才可知
WHEN 该数据用于调整申购金额
THEN 执行 MUST 移至后续规则允许的时点，不能按当日未知净值成交。

#### Scenario: Calculation rules changed

GIVEN 已保存实验的计算代码哈希与当前规则不同
WHEN 请求重放历史实验
THEN 系统 MUST 保留原结果并明确返回版本不支持，MUST NOT 静默使用新规则覆盖旧结果。

#### Scenario: Missing fee and young fund

GIVEN 用户要求费用上限，但某基金费率未知；另一基金历史不足长期评价阈值
WHEN 筛选和评分
THEN 前者 MUST 返回不可判定，后者 MUST 仅展示适用的短期表现，不能生成长期定投等级。

### Requirement: Shared projections and disclosure limits

HTTP、MCP、任务和 WebUI MUST 复用同一基金计算契约；相同快照与规则下 MUST 得到相同结果。
穿透结果 MUST 携带报告期、可知时间与覆盖范围，AI 解释 MUST NOT 提高事实完整性或改写账务。

#### Scenario: Stale NAV across consumers

GIVEN 相同组合快照中存在滞后净值
WHEN 页面、MCP 和盘后任务读取
THEN 各入口 MUST 一致地将其排除于当日已确认收益，并展示估值日期及已确认覆盖率。

#### Scenario: Top holdings only

GIVEN 仅得到部分基金的前十大披露持仓
WHEN 生成组合暴露与研究结论
THEN 系统 MUST 标明已披露覆盖和未知部分，MUST NOT 声称已经完整穿透组合。

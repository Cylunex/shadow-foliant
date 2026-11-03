# 账户清仓日报与左侧研究

## ADDED Requirements

### Requirement: Count only confirmed equity positions
系统 MUST 按账户、持仓水位与证券身份计算正数量个股；ETF 单列，其他基金与不明品种另列；同账户重复批次 MUST NOT 重复算只数。缺少行情 MUST NOT 令已确认个股从分母消失。

#### Scenario: Exactly at limit
- **GIVEN** 15 只正数量个股、4 只 ETF、1 只 LOF、1 条已清仓记录，当日从未越线
- **WHEN** 生成日报
- **THEN** 个股为 15、ETF 为 4，不产生超限义务；独立风险规则照常。

#### Scenario: Known lower bound or indeterminate
- **GIVEN** 16 只确认个股另有未知品种，或 15 只确认个股加 1 个可能的个股
- **WHEN** 核验只数
- **THEN** 前者必报，后者报待核且不宣称已达标；账本不可用不能显示零持仓。未知数量、多个批次和超过 100 个标的的账户使用完整计数，按需分批取价。

### Requirement: Deliver the obligation each Shanghai calendar day
对已确认越线的每个自然日系统 MUST 创建设计规定的私人日报义务；合并进原生报告时 MUST 验证指定渠道确实接受了含必需字段的日报。节假日、非交易日与没有新选股批次 MUST NOT 取消该义务。

#### Scenario: Weekend and absent analytics
- **GIVEN** 周五、周六、周日均有 16 只个股，周末没有新报价、LLM 不可用
- **WHEN** 连续运行三天
- **THEN** 产生三个不同自然日的业务键；周末各标明行情属于周五、不能执行及下次复核条件，普通周末任务不随之开启。

#### Scenario: Late crossing and holding changes
- **GIVEN** 尾盘检查时 15 只，晚间新导入使持仓变为 16 只
- **WHEN** 入账后或重启时重算
- **THEN** 当天补发；若发送前已确认降回 15，只发当日结项。读取名单至冻结草稿间水位变化须重算，不再推荐已清仓股票。

#### Scenario: Market or account data outage
- **GIVEN** 账本下界 16 但报价、可卖量或所有风控证据不可用
- **WHEN** 到达当日发送时点
- **THEN** 仍发超限日报，可执行为 0，写明缺口和复核条件。若连当日账本也不可读，明确上次核实日期，发待核提示；当日恢复后依缺失义务补齐，不用待核提示抵扣已确认义务。

### Requirement: Separate exit priority, executable action and actual clearance
系统 MUST 区分整只退出、部分减仓、仅复核和继续持有，保存原始触发和组合保护后的裁决。LLM MUST NOT 增加卖出名额、更改动作或虚构原因。

#### Scenario: Three above target with only one full exit eligible
- **GIVEN** 18 只个股，其中甲全部可卖且通过风控、乙仅部分可卖、丙因当日买入无法卖出
- **WHEN** 生成建议
- **THEN** 展示超 3 只，甲建议至多释放 1 个名额，乙丙不计；仅甲成交后仍超 2 只。未导入真实成交前仍显示 18 只。

#### Scenario: Candidate but protected
- **GIVEN** 个股处于硬风险观察但被组合换手、交易限制或价格失效阻断
- **WHEN** 生成首屏
- **THEN** 显示风险原因、保护结果和待核条件；只减不称“清仓”。没有一只符合条件时仍发，不能按亏损大小硬凑足 N−15 只。

### Requirement: Freeze deliveries and distinguish their receipts
系统 MUST 对每个应发日只领取一份逻辑报告、冻结版本，并记录指定渠道各分片的接受、明确拒收、未知和归档结果。任务 success、监控事件、非指定渠道接受均 MUST NOT 抵扣应发数。

#### Scenario: Partial delivery or ambiguous receipt
- **GIVEN** 第一片已接受，第二片明确拒收、响应超时或仅归档失败
- **WHEN** 备援与原任务同时运行或服务重启
- **THEN** 不重发第一片；明确拒收用同份载荷创建有界尝试，超时先核收，归档失败只补档。当天不能确认完整结果则留欠发/未知，次日不重发旧操作指令。待核提示用独立子键；其中一片已接受或在途未知时不得用新水位悄悄改原稿。

### Requirement: Prefer earlier, verifiable left-side research
系统 MUST 从同一冻结合格全集产生独立版本的左侧研究名单，呈现估值/基本面支撑、回撤与量价、个股行业环境、失效条件和有效期。任何正式名单、个人仓位、LLM 和外部参考 MUST NOT 代替这些证据。

#### Scenario: Cheap but still deteriorating
- **GIVEN** 个股估值低且下跌，但业绩已被新公告证伪，或为放量下破且没有抛压收敛
- **WHEN** 评估左侧状态
- **THEN** 前者退出候选，后者不能标成企稳或可买；缺成交量时不标“缩量”。正式 TOP15/TOP5 成员、得分与顺序保持不变。

#### Scenario: Market changes during the day
- **GIVEN** 同范围广度午后从多数上涨转为多数下跌，或只取到了 A500 子样本
- **WHEN** 形成收盘说明、回放午间判断
- **THEN** 收盘说明列清范围和时刻，午间产物不含其后的收盘证据；缺样本不推全市场涨跌停家数，不允许仅据大盘下跌认定试仓已成立。

### Requirement: Consistent and private decisions across surfaces
实际渠道正文、Web 和私人 Skill/CLI MUST 源自同一报告，行情时点和持仓水位可追溯；公共研究 MUST NOT 取得私人持仓，默认不输出成交或执行交易。各渠道 MUST 保证标题与对象、最终动作一致。

#### Scenario: Formal top pick receives a cautious review
- **GIVEN** 正式候选排第一，复核为否决且当前未持有；组合仓位维持而另一已持仓个股需退出复核
- **WHEN** 渲染正式选股与账户通知
- **THEN** 候选注明复核意见“未支持开仓／待观察”而非“卖出”；复核不取代正式榜单或账户权威动作；账户通知区分“总仓位维持”与该股退出复核。超量正文压缩后首屏仍含已确认只数、超额、优先项和限制。

### Requirement: Evaluate with the evidence available at decision time
研究 MUST 留存未成熟及无法成交的分母，分别评估价格标签、可执行模型与私人账本；左侧策略晋级 MUST 经过已有成本、基准、时间外与风控门槛。

#### Scenario: Changed data or flattering one-day returns
- **GIVEN** 未来财报加入数据集、选择日未冻结相同字段，或仅有少数完整一日价格观察
- **WHEN** 重放选择与评估改权
- **THEN** 未来信息不应改变旧名单，无法复演则标不完整。短期表现或回填不是净收益证据，不能触发正式政策晋级。

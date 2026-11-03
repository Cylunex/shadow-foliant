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

### Requirement: Publish verified index levels wherever a report describes the market
市场通知 MUST 同时显示当前所处交易时段、五个核心宽基名称、可核验点位和较昨收涨跌幅。风险触发与去重继续依据已核风险信息。

#### Scenario: Market opens after a dated premarket brief
- **GIVEN** 09:00 只有上个交易日的收盘行情，10:08 已有当日开盘、昨收和完整指数报价
- **WHEN** 生成盘前、盘中通知并经过实际渠道压缩
- **THEN** 盘前标收盘日期。盘中两行保留五个点位和百分比，开盘与现时比较使用百分点。标题显示实际报价时间。

#### Scenario: One incomplete index or a later quote
- **GIVEN** 四只有可靠点位和幅度，第五只仅有当时核实的涨跌幅，或仅有晚于报告的报价
- **WHEN** 生成 10:05 固定报告、午报或 QQ 风险报告
- **THEN** 对第五只保留经独立核验的同时点字段，其余写待核，旧存档维持原时点。点位写入报告不会重复触发风险消息。

### Requirement: Make opening reviews useful to the portfolio holder
早盘通知 MUST 在最终正文的行数预算内区分持仓动作、场内 ETF、条件式新候选和数据时间，硬风险优先。相同行业、资讯和日内涨幅只能作各自范围内的证据。

#### Scenario: Split fund and rising non-held stock
- **GIVEN** 一只 ETF 公告拆分、账户批次尚未核对，另一只非持仓股早盘上涨且被问财提名
- **WHEN** 编排首屏
- **THEN** 保留 ETF 当日报价并提示核对拆分份额与成本，暂停成本类盈亏触发。新候选列来源与观察条件，仅当证据满足左侧模板才标左侧。缺合格候选可显示零只。

### Requirement: Distinguish Wencai evidence, performance and portfolio action
问财参考 MUST 按查询版本、组、首次可知时间和窗口记录，保存实际可复盘的排名及分母。历史价格评估 MUST 分开早盘可见名单、其他渠道和盘后补采。比较正式名单 MUST 使用共同完整日期与执行窗口。

#### Scenario: Verified groups and late retry
- **GIVEN** 盘前部分组已通过、另一组首次在 10:40 核验，另有一组无返回
- **WHEN** 回放 10:08 通知并计算表现
- **THEN** 首屏仅标当时通过的组，10:40 的新结果另记。重用的盘前结果不重复计入，报空与缺失分别计数，跨组重复股在整体只计一次。

#### Scenario: Strong historical result and weak recent returns
- **GIVEN** 旧版单日某组五只全涨，而现版同日配对回放没有稳定的扣成本优势
- **WHEN** 确定首屏优先级
- **THEN** 保留有本地证据的问财独有观察或交集出处，分别报告两版的期限、分母与缺项。不得按旧胜率给现版加买入权重。

### Requirement: Attribute Losna discoveries and directed reviews separately
Foliant MUST 锁定同一冻结输入、用途、可见资料、名单顺序与放弃项，再揭盲比较。服务端 MUST 将 Losna 与既有 Codex 外部研究按生产者、策略版本及运行独立存取和结算。

#### Scenario: A Wencai-only discovery is assigned for review
- **GIVEN** 点名工作读过问财名单并发现一条公司公告
- **WHEN** Losna 提交观点或改用独立提名的产物类型
- **THEN** 点名工作可提交有出处的复核，独立提名写入被拒绝。该标的不因榜外或被看好而获得独立发现成绩。

#### Scenario: A blind job requests another result
- **GIVEN** 盲提名工作尚未锁定，模型尝试读取正式榜、问财缓存、个人记忆或另一任务的证据 ID
- **WHEN** 服务端或执行器校验来源、对象及阶段
- **THEN** 拒绝该读取。可见范围内的证券研究继续，锁定后允许对照已冻结的结果。私人对照另用有账户授权的工作。

### Requirement: Keep Losna operation comparisons inside authoritative account constraints
私人 Skill MUST 以 Foliant 的持仓水位和最终裁决比较允许的操作，公开产物 MUST NOT 引用私人附件或账户数据。

#### Scenario: Only part of an overweight portfolio position can be sold
- **GIVEN** 18 只个股和 2 只 ETF，持仓甲只能卖部分，Losna 提出全退替换
- **WHEN** Foliant 生成私人建议
- **THEN** 显示实际可行备选、硬限制和部分减仓释放 0 个名额。新股票仍为观察；真实成交与账户刷新前，个股数保持 18。最晚截止到来时照常发超限日报。

### Requirement: Validate receipt, deadline and promotion independently
Foliant MUST 校验工作领取代次、输入版本、接收时刻及引用对象，并将实际采纳的产物版本绑定报告。迟到研究和后验反思只能用于以后的有效决定。

#### Scenario: Worker restarts and the report has closed
- **GIVEN** 旧执行者超时，备用执行者重领；正常内容已按时送出
- **WHEN** 两者回传或查询接收回执
- **THEN** 旧代次不能覆盖结果，同键同文只取原回执。迟到且仍有效的研究在后续复核中注明时间。Losna 的工作完成与成果附件均不触发第二条定时报告。

#### Scenario: A successful experiment introduces a new feature
- **GIVEN** Losna 的预登记实验提高了部分样本的指标，但变更超出已注册政策参数
- **WHEN** Foliant 复核研究成果
- **THEN** 保留影子结果、未成熟样本、成本和反例。新增条件先注册与验证；既有参数按策略证据门、版本条件及生效日处理。未来才知道的结果不能改写过去提名。

# 研究可靠性与持仓学习（0.6.0）

本轮是工程实现，不是生产发布或收益验证。保留三条本地正式赛道、五策略偏好、PIT 每行业最多 5 只、
TOP15/独立 TOP5 和问财/妙想参考隔离。没有新增下单、RAG 或强制全系统影子运行。
盘前与盘中通知模板未改；新增页面继续红涨绿跌。

后续三轮逻辑走查、恢复与人工确认闭环见 [走查记录](LOGIC_AUDIT_2026-09-04.md)。

## 1. 实际运行链

| 入口 | 现在执行什么 | 明确不代表什么 |
|---|---|---|
| 数据适配器 | SDK 准入回执、部分 urllib HTTP 响应回执；识别同上游家族并附加共享额度 | 不声称看到了所有 SDK 内部重试、分页或任意文件访问 |
| 财务/估值入库 | 关键单元格观测、原始发布时间/首次观测分离、单位口径检查、财务修订影响、按字段缺口记录 | 单位/口径未知不会自动被推断为严格语义；不会覆盖历史事实 |
| 正式选股 | 原三赛道输出不变，额外重跑去卫星/去择时分支；生成 Case 与 TOP5 核查待办 | 外部参考和 Case 不回写正式分数 |
| 盘后决策任务 | 公平有界执行事实队列、归档、原日期恢复、权益发现、最多 3 个 Case 复核、模型遥测归档 | 今日行情不能替代漏跑日期的开盘；资料缺失不说没有风险 |
| 周度委员会 | 同证据的保持不动、确定性规则、规则+LLM 三臂提案；原白名单控制器决定接受与否 | 不是每日任意调权，也不是把短期亏损直接停策略 |
| 模型账户 | 连续持仓、应收/到账、独立配对初始状态、固定执行压力情景 | 模拟成交不是实盘；覆盖不足不进入晋升证据 |
| Web/MCP/Agent | 公共研究档案、私人草稿和锁定、账户预览、可选现金/费用事实导入 | Agent 无论点锁定接口；未确认输入不会改账户事实 |

### 数据证据

`research_reliability_records` 为按 kind/object/owner/revision 追加的领域日志；不是新的任务调度器。
`research_reliability_work` 只负责可见的消费方缺口与预算准入；现有任务执行工作。
版本更新采用 CAS；PostgreSQL 事务锁避免并发覆盖。私人对象默认不进入公共研究查询。

- 同上游：TDX 适配器归为 tdx，问财归为同花顺，妙想归为东方财富；AkShare 未知底层明确 unknown。
- 既有 Baostock 单连接与日预算、Mairui/Moma 原生额度全部保留；家族门是附加限制，不抵消原门。
- 估值缺口由既有 `ValuationSynchronizer` 继续补齐，不额外启动全源扫描；补齐字段停止后续请求。
- 财务关键数字保留 cell 观测。修订先关联数据集与 Case，再对队列里的 manifest 每轮最多重算 2 项，
  比较成员、名次和分数，不替换原发布结果。未消费的字段排除，缺冻结数据返回 unavailable。
- 回执队列有界、旁路、进程崩溃可能丢遥测。SDK 准入与 HTTP 响应是不同观测层，不可直接相加当实际请求数。
- 原文默认不保存。确认源条款后才配置 `RESEARCH_RAW_RETAIN_PROVIDERS` 与外部私有 `RESEARCH_RAW_ARCHIVE_DIR`；
  只接受明确公共源白名单，单体上限 1 MiB，目录 0700、文件 0600、内容哈希校验。不记录鉴权 URL。
  未获保留许可/不能保存时返回不可完整回放，不伪称有原文归档。

### 结算与恢复

分红登记日权益数量必须有依据：除息时记应收，付款确认后应收转现金；应收计入 NAV，但不能当可用现金买入。
重复事件不重复入账。整数送转可处理；零碎股、未核实税额、配股/换股/退市等不足以核验时继续标缺口。

已按 [Tushare 官方分红接口](https://tushare.pro/document/2?doc_id=103) 增加有界适配，沿用现有 token/权限。
它只有分红送股范围，**不是全公司行为覆盖证明**，也不能仅凭 cash_div 推断当前持有人最终税额。
自动任务每轮最多查询 3 个待办，未配置休眠，失败最多 3 次后显示 exhausted。无记录、未接入、失败不同口径。

执行队列一次最多 160 项，按尝试次数、优先级、等待时间调度；超额留 pending，重试耗尽显示 exhausted。
目标执行前已登记、只是任务晚跑：仅用归档的原日期 raw 开盘、限价、成交量和执行规则恢复。
缺原日期事实保持 waiting_data；目标在执行以后才登记则 backfilled/expired，绝不补造前向意图。
已有更晚净值不可原地改漂亮；修订事实保留新 revision，晚到数据仍需原日期账本能按顺序前进。

连续账户政策切换留下历史且不把遗留头寸重新冒充新政策净证据。三臂模型共享相同初始现金/头寸快照哈希，
保留初始状态是否已核验，最多跟踪 120 个已完成估值日。政策臂按冻结生产器提名重新融合；
涉及基因组阈值/预筛的变化使用相同 PIT/基因快照重新运行生产器；相同参数缓存复用，避免重复扫描。

## 2. 怎么用研究档案

- 公共档案：MCP `research_cases()`，或 Agent `GET /api/machine/v1/agent/research/cases`。
- 原 security-research Run 完成时，在同一事务追加 owner 隔离的 Case 执行关联；公共查询不泄露私人 Run 或研究内容。
- 现有 `research_decision_loop()`/Web“研究决策闭环”包含 Case、问题与注意力摘要。
- MCP `research_thesis_draft(symbol,text,claims,expected_revision)` 起草。可以没有 Claim，但会保持未核实。
- 本机 operator 使用 `research_private_state()` 读取已有草稿版本、判断和待确认账户预览；不向公共 Agent HTTP 开放。
- Web 私人区域读取已有草稿、保存、点击确认锁定。只有已认证管理员浏览器可锁定；机器凭据不具备该权限。
- 草稿修改不会覆盖已锁定版本。无论点、过期论点不阻止风险退出或成交记录导入。

Claim 的片段必须有 source_id、quote、locator、published_at、first_seen_at。
计算片段还需 unit、currency、basis、period_kind；支持 sum/difference/ratio/growth_pct，操作数必须在引用中出现，
数字由 Decimal 重算。明确不同口径不能相减；自然语言推断/因果关系不因数字算对而自动得到证明。
Coverage、freshness、conclusion、use 分开显示，不压成一个虚假精确置信分。

TOP5 自动生成核查待办；重大/未知事件和财务修订也生成复核。每天最多 3 个 Case，
每个最多 12 个工具准入、1 次综合模型调用。当前固定流程读取档案、事件和缓存估值 3 项；
一次模型请求最多 600 输出 token、30 秒，不在该工作流无限跨源 fallback。
系统会保留 draft/缺失项，而不让摘要直接变成“已核验结论”。不具备完整材料时不伪造归因。
注意力最多展示 5 只不同证券，优先私人到期提醒与较新的紧急事件，不让同一股票反复占位。

周末研究仍避开 09–12、14–18；考虑预计时长后 23:30 前停止启动新复核。
`RESEARCH_CASE_AUTO_REVIEW_ENABLED=false` 可停自动复核，不关闭正式选股或人工工具。

## 3. 验证与有界学习

实验登记绑定 `ValidationProtocol`、指标、期限、费用模型、时间切分和日期块长度。
尝试/编译失败、候选、指标查看与盲测接触分开记录；每假设原 8 次安全预算不扩大。
`nonoverlapping_samples` 不称独立样本，额外报告日期块数量、保守下界与 insufficient_evidence。
非重叠和块下界都不是“完全消除了金融时间序列依赖”的证明。

盲测必须提供实际 evaluator：

1. 已登记且评估过的候选、已到期的预封存批次；
2. 事务先 sealed → evaluating，并增加访问次数；
3. 才读取私有、摘要固定的评估事实，隔离 worker 做固定 DSL 算术；
4. 核验时点、批次区间与事实凭证，持久化实际 EvaluationResult 后 retired。

没有 evaluator 不再允许“只改状态”。失败/崩溃保留 evaluating，不自动重新开放给另一个候选。
已消费区间不能新建重叠“盲测”重试；运行中 evaluating 也从普通研究输入排除。
当前事实文件由受信运维准备；不向 Agent 暴露任意文件读取。隔离范围是受控评估过程，不声称能封锁公开市场知识。

运维入口（示例参数均为占位符）：

```bash
python scripts/evaluate_sealed_holdout.py --batch BATCH_ID --trial TRIAL_ID \
  --facts /private/research/holdout.json --sha256 CONTENT_DIGEST \
  --image registry.example/research-worker@sha256:IMAGE_DIGEST --confirm-consume
```

容器无网络、无挂载、只读、低权限、资源上限；镜像必须 digest 固定。脚本需要实际可运行 Docker。
已打开的批次故障需要审计，不用清状态重跑来掩盖额外指标查看。
运维 CLI 的 `--void-reason` 可终止中断批次并保留审计，不重新揭示、不重新开放区间。

健康状态：两次有效不良证据进入冷却；14 天冷却后需要连续三次合格证据才能恢复增风险；
同一证据快照不重复累计。样本不足禁止增风险但保留既有 active。硬风险使用单独 risk_halted，不能被 LLM 自动解除。
原控制器仍检查用户偏好、单项/总步长、政策哈希、证据和下一交易日。

## 4. 账户、情景与判断校准

账户预览附风险快照、静态价格压力与实际/正式差异。价格缺失就 partial，不拿不完整持仓做精确压力收益。
执行模型另跑 6 类固定情景：费用滑点翻倍、流动性减半、买入涨停、卖出跌停、晚一交易日、相关簇集中。
下一日数据或相关簇缺失时显示 unavailable，不能编一个数。两类情景不是相同指标。

有可信预期收益与不确定度时，费用和最小交易单位共同构成“不交易区间”。
没有增益估计时只显示费用与约束，不将选股分数减手续费后冒充收益。始终保留不动选项。

可选账户事实：MCP `account_facts_preview(rows)` 或 Web 私人导入区域。
kind 支持 cash_balance/deposit/withdrawal/fee/dividend/equity_balance；每条 external_id、date、amount 必填。
现金是人民币非负数，方向由 kind 表达；不同币种拒绝。先预览、再用户确认，验证成交水位与账户事实版本。
同 external_id 相同事实幂等，不同内容冲突；不触碰原成交导入。期间对账保留未解释差额，没有完整流水不输出 TWR。

MCP `research_forecast` 登记未来期限的概率判断，固定 positive_net_excess 与 Brier 量尺。
到期任务只消费匹配基准、已核验的 prediction_outcome，不从价格新闻或模型文本猜输赢；
当前缺少该结果凭证时保持 pending。人工/数据适配器提供结果事实后可调用领域裁决；未提供事实不视为自动校准已完成。
未裁决、void 与 0.5 常数基准单列。少量样本不生成虚假精确胜率承诺。

## 5. 评测、迁移与交付状态

```bash
python scripts/run_agent_evals.py                  # 原零-token合同检查
python scripts/run_model_process_evals.py          # 只列 24 个合成开发案例
python scripts/run_model_process_evals.py --execute --configuration all
```

24 个开发案例分数字、正式/参考隔离、研究、账户四组；同工具/数据比较 baseline、curated、self_generated，
关键案例重复三次。执行端限制工具、检查数字/未来资料/权限/缺失，记录轨迹、模型身份、调用次数和耗时，不记录推理。
源码夹具不是盲测；独立封存案例通过私有 `--sealed-file` 提供，不能拿开发案例报告作未见样本成绩。

新增 migration 12（两张追加表），正常 `scripts/migrate.sh` 会自动执行；先迁移再启动新版。
旧表/旧 artifact 不删除。回退应用代码可保留新表，不需要破坏性删表。
本版未新增运行依赖；复用原版本锁、受限 DSL、PostgreSQL 和身份平台。

本机验收（2026-09-04）：完整 pytest 495 passed / 2 skipped / 7 subtests passed；
最终兼容修正后的重点回归 97 passed；原生 PostgreSQL 迁移/并发 CAS/队列集成 1 passed；
独立零-token Agent 合同入口 7 passed。上述为不同范围运行，不叠加成独立测试总数。
空库迁移及重复执行、前端语法、虚构数据页面展示与差异检查通过；测试数据库和预览服务已关闭。

尚未验收，**不能称全方案已在生产验证**：

- 真实模型过程入口试跑返回 `model_unavailable`；本机没有可用模型配置，不把模拟模型合同测试当真实成绩。
- 本机没有 Docker，隔离 worker 参数与失败封锁已测，实际容器执行仍待有 Docker 的环境验证。
- 供应商分红权限、全公司行为覆盖、实际券商现金/税额、历史执行缺口与独立封存案例需要真实输入；不能用代码生成事实。
- 自动获取所有类型的预测结果仍取决于事前约定的数据源；缺少已核验结果时不能自动裁决。
- 本轮没有推送、生产迁移或部署。部署仍需单独授权并完成上述相关环境验收。

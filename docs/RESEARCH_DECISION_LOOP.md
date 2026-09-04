# 研究决策闭环（0.5.0）

本页保留 0.5.0 基线。0.6.0 已增加原日期执行恢复、独立赛道重跑、研究档案、
实际评估器准入与权益应收处理，详见 [研究可靠性与持仓学习](RESEARCH_RELIABILITY.md)。

本版直接扩展现有本地三赛道选股、PostgreSQL、任务中心和 Agent 用例服务，不新增券商下单，
不恢复 RAG，不要求全系统等待数周影子运行。真实交易仍由用户决定。

## 已接入的运行链路

1. 采集任务按数据能力调用稳定优先的源链；合格的同日期字段可在发布前合成，冻结后不改旧 manifest。
2. 同步/补跑/正式选股任务刷新绑定 generation 的质量报告。健康接口只读取当前报告；
   报告缺失或 generation 改变返回 stale，不通过健康探针扫描全市场。
3. 正式选股保存决策 Capsule、TOP15/TOP5、PIT-only 和本地策略提名；同时登记前向模拟目标。
   账本辅助写入失败不会否定已经提交的正式选股，盘后任务会幂等补建；错过执行时点的补建只能标作 backfilled。
4. 现有盘后后验任务按声明的下一交易日开盘模型更新成交和连续账户。
5. 现有周度委员会任务先运行三类有界研究，再读取分版本价格后验和合格模型净收益证据。
   LLM 只提白名单参数建议；数值、证据、步长、用户偏好、CAS 和生效日由代码校验。
6. MCP、HTTP Agent 和 Web 分别通过研究视图、私人账户预览和共享收益服务读取结果。

### 不改变的选股边界

- 本地 PIT、本地五组规则、技术基因组仍是真实候选；问财、妙想仍为独立参考。
- 初始偏好保持：主力资金 / 低价擒牛 / 低估值 > 小市值 > 净利增长。
- PIT 每行业上限 5 只；账户资金暴露另按资金比例约束，两者不是同一限制。
- 正式 TOP15 与独立 TOP5 保留，不用账户持仓或实时行情重写正式排名。
- 盘前、盘中通知不缩减；本轮没有重写现有推送模板。新增收益视图遵循红涨绿跌。

## 数据源：充分利用，不等于每次全量扫一遍

已注册源按实际能力参与原有路径，未配置凭据或缺少依赖的源不假装可用。
跨域不能替代：实时行情不冒充历史 PIT 财务，未复权数据不冒充前复权 K 线，参考名单不改正式分数。

| 稳定等级 | 数据源 | 默认最小间隔 / 运行预算 |
|---|---|---|
| 优先 | ZZShare、Tushare | 1 秒；每日分别 30,000 / 10,000 次准入 |
| 优先、独立配额 | Baostock | 单机串行；原生每日预算默认 45,000，请求硬上限 50,000；继续沿用既有黑名单退避 |
| 常用 | 腾讯、新浪、巨潮、eltdx、tdx_python | 分别 0.5 / 1 / 1 / 0.1 / 0.1 秒；逐源预算 |
| 有限补充 | Mairui、Moma | 保留各自原生持久配额；默认每日 500，所有端点共用，不扩大为无限请求 |
| 有限补充 | 东方财富、同花顺、问财、妙想、财联社 | 2–3 秒；问财、妙想默认每日 500，其他源按配置上限 |
| 最后兜底 | AkShare、百度、集思录、TickFlow、easy_tdx、mootdx | 各自有限间隔、预算；既有默认关闭的协议适配器仍需显式启用 |

这里的稳定等级是**项目运营优先级**，不是厂商 SLA。免费或非官方源可继续兜底；
同等级内再按近期健康度排序，不让偶然一次快响应覆盖长期优先级。

限流统一到 `data/provider_governor.py`，旧 `core/data.rate_limiter` 调用兼容。跨进程文件锁、
请求前计费、原子状态落盘、有限等待、连续失败退避、冷却探测均使用同一共享运行目录。
路由超时只能停止调用方等待，不能杀死 Python 线程；未退出的请求保留 inflight，禁止同源重复堆积。

问财在实际分页/重试 HTTP 入口计数，并补默认网络超时；妙想两个请求入口均在整个请求期间持锁，
不再吞掉限流异常后继续发请求。其他旧 SDK 的内部分页可能只计一次外层准入，不能把准入计数当作
所有 SDK 的精确 HTTP 审计。Baostock、Mairui、Moma 的原生硬配额仍是最终约束。

可用通用变量下调运营预算，不能突破代码安全上限：

```dotenv
# 脱敏示例；同一主机各服务必须指向同一持久目录，不能随 release 更换。
FOLIANT_RUNTIME_DATA_DIR=/path/to/shared/runtime-data
SOURCE_DAILY_LIMIT_EASTMONEY=1500
RATE_LIMIT_EASTMONEY=4
SOURCE_DAILY_LIMIT_PYWENCAI=300
```

原生 Baostock 用已有 `BAOSTOCK_DAILY_REQUEST_BUDGET`，Mairui/Moma 沿用已有原生预算项。
准入状态文件损坏时 fail closed；不要通过删除状态文件重置真实厂商额度。
未使用、配额耗尽、熔断、凭据缺失和源异常在能力快照中分别呈现，不显示 Token/URL/账户值。

## 三种收益绝不混算

| 账本 | 含义 | 限制 |
|---|---|---|
| 信号价格后验 | 入场开盘之后的价格变化、盘中 MAE、收盘峰谷回撤 | 不是可成交净收益；缺 open 不退用 close；旧结果保留 legacy 版本 |
| 模型前向账本 | 等初始资金、声明交易规则、手续费、现金、持仓和净值 | 模拟不是实盘；涨跌停、停牌、可卖量、数量规则和流动性不足会未成交或部分成交 |
| 真实证券子账户 | 当前水位化的持仓收益记录 | 不与模型相加；没有完整券商现金流水时不推测全账户收益、TWR 或可用现金 |

模型包含融合 TOP15、TOP5、PIT-only、去本地五策略、去技术基因组、每周再平衡的低换手组，
以及每条本地策略的前五提名。去赛道组是正式名单去掉对应成员的机械对照，**不是重跑移除赛道后的完整最优排名**。
同一轮采用同样的下一交易日开盘、手续费和现金规则；低换手组有意只改变再平衡频率。
独立买入 cohort 与连续账户分别保存，不能把重叠 cohort 收益连乘成账户净值。

默认费用/滑点是显式的研究假设，不是用户券商实际费率。每日成交量限制只是事后容量上界，
不证明开盘时一定能成交。ETF、债券等缺少品种规则时拒绝模拟，不能硬套普通股票规则。
公司行为算子支持明确登记日权益的分红与整数拆股；不明事件和缺行情使净值成为 indicative。
**当前自动收盘采集不能保证逐证券公司行为完整，因此不自动声明 corporate_actions_complete。**
此类净值可展示，但不可用于晋级。缺失任意净值点时不输出伪完整最大回撤。

真实账户共享金额对账算子明确 full_account / securities_only；买卖不是全账户外部入金。
分红、费用、利息、税费纳入对应边界。Web 日收益、MCP 日收益和 Agent books 读取同一快照服务，
使用 Decimal 汇总。现有券商现金流未接入，因此全账户对账状态明确为 unavailable，不填造假数。

## 账户行动预览

自动读取最新正式机会、私人持仓和报价；持仓/导入水位采用同一只读事务快照，取行情后再次检查。
缺现金、陈旧报价、未核实可卖数量、未知品种或暴露/交易单位/流动性不符时给出阻断原因。
默认限制：单票 15%、行业 35%、主题 45%、总仓位 90%、换手 10%、报价有效期 120 秒。
这是预览限制，不改正式选股每行业 5 只的配额。

提供不动、减风险、增持、条件式替换四种互斥备选。替换必须先确认卖出成交、刷新现金/持仓/行情再重算，
不能预支尚未成交的卖出资金。若没有合格股票，明确“没有合格加仓标的，暂不操作”。
导入流水不是券商实时可卖量；当前未核实的可卖量不自动补成全部持仓，因此可能暂不能生成减仓单。

## 研究与政策治理

- 预登记盈利质量、资金持续性、趋势衰竭三个方向，冻结 AST、数据 manifest、代码版本、祖先、预算和淘汰条件。
- 每个假设最多 8 次尝试，失败、重复与预算耗尽也留档；不会每周悄悄重置试验预算。
- 目前三类因子用已有快照作探索诊断，标作 reconstructed_history，不能伪装多年严格 PIT。
- 模型证据需同一政策、相同交易日、完整且核实的双方净值；同日提名及重叠持有区间不重复算独立样本。
- 多次试验保守下界、滚动时间外验证共同约束晋级。不同策略同日样本不简单相加，单策略加权需自己的证据。
- 政策单路径单步长、累计最多 3 个标准步长；重复字段、重复 path、bool、非整数名额、NaN/Infinity 拒绝。
- 降低基因组准入分数视为增加风险，不误判为降风险。增加风险需净收益证据，不再使用价格后验条数放行。
- PostgreSQL advisory lock 下执行 CAS 和幂等发布，下一确认交易日 09:00 生效；回滚发布补偿版本，不删除历史。
- 新政策使用与 FusionPolicy 相同的规范化哈希；旧注册哈希保留 CAS 身份，证据另按规范化内容绑定。
- 未来封存区间不喂训练；训练标签区间与验证区间 purge/embargo；批次只可在成熟后单次消费并退役。

封存生命周期和评估算子已实现，但不会凭空生成多年 PIT、执行事实或盲测业绩。
新模型不能仅凭一次离线 JSON 导出被自动装入正式选股。当前正式策略照常使用，不因证据积累被整体暂停。

## 隔离扩展与运维入口

默认只执行受限算术 DSL：64 节点、深度 8、回看不超过 250、最多 200,000 数据单元；无 eval/exec/import。
周度因子诊断有 90 秒软预算，到点不再启动下一项计算。可选 `RESEARCH_WORKER_IMAGE` 必须指定 `@sha256:` 镜像摘要。
Worker 无网络、只读根、非 root、去 capabilities、无宿主挂载/凭据，有限内存/CPU/进程/时间。
构建上下文为本仓库，只复制 DSL 和 worker；未配置 Docker 不执行任意研究代码。

```bash
python scripts/research_loop.py status
python scripts/research_loop.py refresh-quality
python scripts/research_loop.py research-weekly
python scripts/research_loop.py seal-holdout --start YYYY-MM-DD --end YYYY-MM-DD
python scripts/research_loop.py compare-offline --input exported-results.json
python scripts/research_loop.py rollback-policy --target-hash TARGET --current-hash CURRENT --reason REASON --confirm-rollback
```

离线比较支持 factor_ast / qlib_offline / rd_agent_offline / learning_rank_offline 的 JSON 结果，
要求同一 dataset_id、execution_model、代码版本和标签区间，按共同股票/日期配对并报告未配对丢弃数。
输入含 `exports[]`，每项含 adapter、dataset_id、execution_model、code_revision、observations；
每行观察含 symbol、label_start、label_end、status、data_class、net_excess_return_pct。
不接收模型二进制、不加载外部 Python；Qlib/RD-Agent 本身不安装到常驻服务。
此入口比较已有离线输出，不声称已经训练或验证这些第三方模型。

周度 LLM 调用先持久预留预算，再调用；一次逻辑委员会/周，输出上限 1800 tokens，相同输入命中缓存。
已有 llm_usage 保留实际 token/费用；逻辑调用内的供应商故障降级不等于只发生一次物理请求。
遵守周末禁用时段与夜间预算。底层平台调用的最终超时仍由既有任务 deadline 共同约束。

## Agent / Web 合同

- MCP `research_decision_loop()`：冻结上下文、实验与模型视图，无私人持仓。
- MCP `portfolio_action_plan(available_cash=5000, allow_add=True)`：仅在用户已确认现金时填写。
  不知道现金就留空；缺参时返回 missing_information，不先查代码或估算账户现金。
- HTTP `GET /api/machine/v1/agent/decision-loop`：研究权限。
- HTTP `GET /api/machine/v1/agent/portfolio/action-plan`、`portfolio/books`：私人持仓权限，研究 token 返回 403。
- Web 驾驶舱展示三账定义、模型执行/净值、实验成熟度、上下文变化、政策时间线和账户预览。
- `formal_capsule_missing` 表示尚无新版正式上下文；`holdings_changed_during_preview` 表示取行情期间发生变更。
  资金未知/报价失效不是“可交易”，不下单；重算不会创建真实成交。

`python scripts/run_agent_evals.py` 执行 Agent 合同清单对应的 pytest 夹具，不消耗 LLM token。
覆盖正式读取/预览不发布、权限隔离、收益语义、AST 污染、发布时间和封存集边界；
这是应用合同验收，不等于证明任意模型每次都按预期措辞回答。

## 升级与验收边界

发布版本 0.5.0。新增 migration `11-research-decision-loop.sql`，正常 `scripts/migrate.sh` 会纳入；
PostgreSQL 继续是生产唯一存储，测试的 SQLite adapter 不属于生产部署。
先备份、迁移再切应用；新表/列兼容扩展，不清旧信号、不清 RAG 表，不删除原有流水。
首次切换后可显式刷新质量报告，或等待既有同步任务刷新；报告缺失是 stale，不是服务崩溃。

本地完成隔离测试、权限合同、模型/费用/水位、AST 污染、源预算及跨进程锁回归；页面以虚构数据验收。
常规全量回归 446 项通过、7 个子测试通过；独立临时 PostgreSQL 另外通过 2 项原生集成测试，
覆盖 migration 重复执行、并发 CAS、模型幂等和封存成熟度，原有数据库合同的跳过项已补验。
Agent 合同清单 7 项单独复验通过。临时服务已停止，未连接现有数据库。
生产迁移、Docker 容器边界和真实供应商连通性仍需部署环境验收。本轮未部署、未改生产配置、未生成真实成交。

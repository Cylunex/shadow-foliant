# 研究证据录入与复核

2026-10-28；本次基线 `52d62fb`。本次补齐 Web 工作台的证据编辑与阅读，不改变正式候选、
PIT 数据仓、研究预算、前向账本或策略权重。

## 使用

在工作台「研究 → 决策 → 验证」展开「人工确认研究论点（私人）」，读取已有草稿，或输入
六位股票代码与论点。每条论断可录入 1–12 段引用：来源标识、原文定位、引用原文、发布时间、
首次观测时间、有效期，以及计算使用的单位、币种、统计口径和期间类型。

- 可以只保存文字，证据状态仍为未核实。每个草稿最多 20 条论断。
- 可选择求和、相减、比值、增长率；每个数值明确选择原文来源，服务端使用原有 Decimal
  实现重算。前端不自行推算，不把引用里的文字当 HTML，也不自动打开或抓取链接。
- 时间保留显式时区，例如 `2026-10-01T09:00:00+08:00`。研究截止时点由服务端在保存
  新草稿版本时记录；锁定沿用该版本截止时点，不用随后发现的材料改写旧版本。
- 保存后可展开查看原文、定位、发布时间、首次观测、口径、算式各项和重算结果。
  **数字重算成功不代表来源已认证、主体/财报期间已绑定或文字因果成立。**
- 有未保存修改时，展示明确说明，旧核验结果仍标明其保存版本；锁定需先保存。
  移除仍被算式使用的引用前，先调整数值来源；移除未使用的引用会同步调整后续引用编号。
- 版本冲突、时间越界或网络失败会保留输入；可复制待合并内容，放弃本地修改后读取最新
  草稿，再合并保存。编辑已保存草稿时股票代码固定；新建另一只股票从空白引用开始。

没有新增审批页、外部调用、数据库迁移或运行依赖；原有人工锁定及私人读取边界保持。

## 现状与取舍

当前 `application/research_claims.py` 已实现原文数字匹配、分项核验、时点限制与历史记录
只读重评；`ResearchCases` 已有版本冲突检查、人工锁定、事件复核与概率校准。
原页面仅能修改论点文字，不能补充引用，锁定论点仅展示来源与定位，无法就地核对原文及
算式。本次使用原 API 贯通这些现有能力，不再新建证据账本或 LLM 投研团队。

## 参考源码与许可

以下为本次读取的固定修订。只借鉴机制和信息呈现，没有复制实现代码，没有安装、运行上游，
没有独立复现其宣传的覆盖率或策略收益。

| 项目与修订 | 核对的源码 / 数据链路 | 采用与不采用 |
| --- | --- | --- |
| [lumo-trade `dc166ebd2988fa073fba3e8288e41db0312f0ae2`](https://github.com/Lumonote/lumo-trade/tree/dc166ebd2988fa073fba3e8288e41db0312f0ae2)；根 LICENSE 为 MIT | `analysis/report_quality.py` 分开返回硬错误与警告；`webui/static/lumo_desktop_app.js` 的市场脉冲展示数据日期、盘中状态、缺口及规则证据。数字检查递归收集 payload，使用容差及尺度匹配。 | 借鉴结果旁展示缺口及证据；不采用宽松数字容差作为论断核验，也不导入交易入口。Foliant 继续使用精确数字与声明口径检查。 |
| [hunter-community `cb9230d5ef039bc71885cdcaf6e0ac975860f3b0`](https://github.com/agentpit-io/hunter-community/tree/cb9230d5ef039bc71885cdcaf6e0ac975860f3b0)；根 LICENSE 为 Apache-2.0，`deep_analysis` Skill 自述 MIT | `apps/api/agents/sentinel/signal_sufficiency.py` 输出缺少来源等原因；`apps/api/app/services/fin/review.py` 从报告事实与成交读取引用，拒绝不存在的引用，复盘候选不直接标 verified。`thesis_gen.py` 是模型生成文字；`skills/deep_analysis/SKILL.md` 是工具采集→规则分析→报告模板。 | 借鉴可展开的原始事实及独立核验状态；不把生成论点当事实，不复制多智能体角色、交易系统或将 Skill 描述视作验证。 |
| [ashare-quant-strategies `0f394b3acff116df489238ebae65bee3af4ded63`](https://github.com/WAYLON/ashare-quant-strategies/tree/0f394b3acff116df489238ebae65bee3af4ded63)；所读树未发现 LICENSE | 核对目录及 `articles/001_多因子精选量化策略/code/snippet_01.py`：依赖策略平台接口，设置防未来函数，但同时使用零滑点、固定因子权重和特定费用假设。 | 仅作待验证假设目录。没有授权依据时不搬运文章或片段；未复现数据、交易约束和样本外效果，不调整生产策略。 |
| [OpenBB `7df7d0c5592bf25be88e87e130a05c71e243d2d8`](https://github.com/openbq-org/OpenBB/tree/7df7d0c5592bf25be88e87e130a05c71e243d2d8)；根 LICENSE 为 Apache-2.0 | 主动搜索后核对 `openbb_platform/core/openbb_core/app/model/obbject.py`：结果、provider、warnings、extra 分别保存，结果转换不等于数据质量认证。 | 借鉴结果与质量信息分开显示。Foliant 已有来源和核验合同，无需引入 OpenBB 平台、供应商依赖或替换 PIT 仓。 |

## 验证与边界

自动化入口：

```bash
python -m pytest -q tests/test_research_claims.py tests/test_research_evidence_ui.py \
  tests/test_research_reliability.py tests/test_webui_contract.py
node tests/research_evidence_ui.mjs
```

Node 不可用时前端测试会明确跳过，不计为通过。

本地测试必须禁用实际配置加载与异步诊断持久化，使用测试适配器；浏览器检查使用独立临时
SQLite、真实研究路由、原页面组件与样式、合成引用，不连接生产或调用模型。

- 上述四个测试文件 **94 passed**；另外五个研究流水线、v4 guards、研究动作合同、就绪
  检查、operational integrity 文件 **99 passed / 1 skipped**。两组文件无重叠。
  跳过项是未启用的 PostgreSQL 集成；原有 pandas/NumPy 弃用警告 216 条。
- Node 状态测试覆盖深拷贝、剥离客户端核验标记、引用索引调整、未保存证据拒绝锁定、
  规范化时间回填、版本冲突保留输入、重复提交和晚到响应；真实 HTTP 测试继续覆盖
  时间越界、原锁定版本不可变、owner 隔离与历史坏记录。
- 本机 Chrome/Playwright 实际操作原页面组件与真实研究路由：新增引用、增长率重算为
  20、锁定、未来材料返回 422 并保留输入、修正后保存、新建股票清空旧引用，以及另一
  客户端更新后的 409 冲突。原文中的 HTML 作为文字呈现。
- 桌面 1365×950 与手机 390×844 截图已检查，无横向溢出、无页面脚本错误；测试中
  故意触发的 422/409 网络错误符合预期。JS 语法与 Git 空白检查通过。

生产鉴权、真实供应商数据、外部来源认证及收益效果不属于本轮验收；本次没有部署、推送、
APK 构建或生产定时任务变更。没有跨仓库修改需求。

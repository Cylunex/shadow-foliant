# 常用命令导航

先按目的选命令，再用目录获取准确参数。所有名称均由 `application/personal_skill.py` 注册。

| 目的 | 命令 |
| --- | --- |
| 最新/指定股票/成交日/录入日的成交 | `trades.list` |
| 成交预检、录入和回执恢复 | `trades.preview`、`trades.import`、`trades.receipt` |
| 持仓、盘中决策、每日盈亏、绩效与曲线 | `portfolio.summary`、`portfolio.intraday`、`portfolio.daily-pnl`、`portfolio.performance`、`portfolio.curve` |
| 交易习惯、持仓分级、加减仓信号 | `portfolio.habits`、`portfolio.classify`、`portfolio.actions` |
| 大盘总览、质量、指数、新闻和板块 | `market.overview`、`market.quality`、`market.indices`、`market.news`、`market.sectors` |
| 个股信息、新闻、已有研究、按需研究、回测 | `stock.info`、`stock.news`、`stock.research-latest`、`stock.research`、`stock.backtest` |
| 正式选股与盘前事实 | `selection.latest`、`selection.premarket` |
| 盯盘查看、增改和移除 | `monitors.list`、`monitors.upsert`、`monitors.remove` |
| 推荐查看、加入盯盘、关闭推荐 | `recommendations.list`、`recommendations.monitor`、`recommendations.close` |
| 信号、变化、方向后验及归档 | `signals.list`、`signals.changes`、`signals.winrate`、`signals.status` |
| 个人参数、单项调整、修改回执、正式策略集及证据 | `strategy.parameters`、`strategy.update`、`strategy.history`、`strategy.deployment`、`strategy.evidence` |
| 任务计划、历史运行和单次结果 | `tasks.list`、`tasks.runs`、`tasks.status` |
| 基金持仓、申赎记录、定投计划、资料、净值、对比与模拟 | `fund.holdings`、`fund.transactions`、`fund.plans`、`fund.info`、`fund.nav`、`fund.compare`、`fund.dca-backtest` |
| 跨资产概览、系统总览 | `assets.overview`、`system.cockpit` |

股票成交的合成输入示例：

```json
{"rows":[{"code":"600001","name":"示例股票","trade_type":"买入","quantity":100,"price":10,"trade_time":"2026-01-05 10:30:00"}],"update_position":true,"idempotency_key":"example-message-001"}
```

不得将示例写入真实账本。订单及费用字段含义沿用 `portfolio/trade_import_service.py`，不在 Losna 维护第二份业务模型。

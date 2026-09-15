# Design

## 最小 schema 增量

复用已有 `selection_runs`、`selection_artifacts` 和 `strategy_adjustment_proposals`，新增四张只承担外部
研究事实的表：证据、独立排序快照、新闻观察名单和分期限后验。证据以 `channel + dedupe_key` 唯一；
排序快照绑定正式 run、独立策略版本和独立 input snapshot。正式 artifact 全程只读。

```text
公开来源结构化证据
  -> external_research_evidence（URL/时间/去重/PIT）
  -> codex-independent-v1 精确 TOP15（成员不可变）
  -> event_adjustment[-15,+8] + risk_veto
  -> external_independent_overlays TOP15/TOP5（观察排序）

外部发现且量化字段不足 -> external_news_watchlist（observation_only）
到期行情 -> external_overlay_outcomes[1/3/5/10/20]
后验 -> strategy_adjustment_proposals（review_required，永不自动应用）
```

CLI 入口先以独立 writer token 提交严格 JSON，再以只读 scheduled token 获取已经合并该 overlay 的
生产快照。提交以 `idempotency_key` 绑定请求哈希和 overlay；通知前原子消费一次 claim，同一 key 的
重放只返回既有结果且不会再次发送 QQ。writer 与私人持仓 reader 不共享凭据。

## PIT 与来源隔离

保存时要求带时区的 `published_at <= captured_at <= decision_as_of`，且 `decision_as_of` 必须是当次
提交的当前决策时点。外部排序成员集合必须精确等于绑定的独立 TOP15；正式和问财只用于输出后的
集合比较，不参与成员资格、基础分或排序。重复 URL/公告/事件由调用方生成稳定 `dedupe_key`，服务端
对冲突内容失败关闭。

请求只能携带外部证据、精确独立 TOP15 overlay、观察名单和调优提案；持仓、任意命令和交易动作均
不在 schema 中。服务端锁定 `ranking_locked_at`，且只接受当代 `decision_as_of`，禁止事后回填排名。

## 价格与执行边界

外部表不保存 entry/target/stop/execution price。计划快照只投影研究排序；需要买卖价时仍读取同一
正式快照的权威 `trade_plan` 和批量行情。所有外部结果固定 `auto_apply=false`、
`auto_execution=false`。

## 调优门槛与回滚

调优提案中的样本数和覆盖周数不信任调用方声明，而由到期的 5 日后验重算。只有至少 20 个不同
样本、覆盖至少 4 个 ISO 交易周且通过时间切分样本外比较，状态才可进入 `review_required`；否则为
`evidence_insufficient`。回滚只把提案标记为 `rolled_back` 并清空应用哈希，不改策略版本。

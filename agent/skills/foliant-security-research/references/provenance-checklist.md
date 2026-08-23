# 研究来源核对

- `decision_at`：这次结论在什么时点形成。
- `market_as_of`：最后一根可用市场数据所属交易日。
- `financial_cutoff_at`：财务事实最晚允许被观察到的时点。
- `universe_snapshot_id`：证券身份或输入证券集合的稳定摘要。
- `input_manifest_id`：本次输入集合的内容摘要。
- `policy_hash`：研究口径或策略参数摘要。
- `code_revision`：生成结果的 Foliant 代码修订。
- `run_id`：结果对应的持久化 Run。

字段缺失必须原样披露为限制，不能用当前日期或模型推断偷偷补齐。

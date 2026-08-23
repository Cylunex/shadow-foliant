# SelectionRun 审计检查项

1. `status` 是否完成，且 `resource_uri` 指向 `shadow://foliant/selection-runs/`。
2. `market_as_of` 是否等于当次决策允许的最后交易日。
3. `financial_cutoff_at` 是否早于或等于 `decision_at`。
4. `input_manifest_id`、`universe_snapshot_id`、`policy_hash`、`code_revision` 是否存在。
5. 候选是否带 `data_coverage`、阶段或风险限制。
6. TOP15/TOP5 是否是正式 artifact，而不是外部参考或 preview。
7. 行业集中、成对相关性和缺失行业字段是否已披露。

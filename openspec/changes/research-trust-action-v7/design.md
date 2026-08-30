# Design

## Security lifecycle and research universe

The zzshare adapter requests `L`, `D` and `P` stock-basic rows through the existing
single-concurrency source contract. Provider status is preserved separately and the
canonical status is the requested lifecycle state.

`ResearchStore.load_lifecycle_universe(as_of)` reconstructs membership from the latest
complete lifecycle observation by applying `list_date <= as_of < delist_date`. This is
valid for membership research but does not turn current industry labels into historical
industry evidence. Callers therefore receive explicit `membership_basis`, observation
date and inactive-row coverage metadata.

Default research selects a deterministic, bounded cohort from that lifecycle universe.
When no lifecycle-complete snapshot exists, it uses the existing representative cohort
and marks the result exploratory.

## Feature contract

A declarative feature catalog owns the production component key, family, direction,
weight, lookback and missing-data policy. `LocalStockSelector` consumes catalog weights
instead of duplicating numeric constants. The catalog is descriptive and deterministic;
research cannot rewrite it automatically.

## Strategy deployment evidence

Genome variants remain automatically evaluated, but a live variant needs at least 12
holdout triggers, 45% holdout win rate, non-negative holdout return, a production score
of 50 and evidence no older than 14 days. Failure continues to fall back to the fixed
generation-zero strategy.

## Trade attribution

Trade input may contain `selection_run_id`, `nomination_id`, `strategy_id` and
`decision_signal_id`. Selection identifiers are resolved server-side against the
nomination store and the trade symbol. Canonical identifiers are persisted in dedicated
columns as well as the immutable normalized payload. Missing attribution means manual or
unclassified intent and is not an error.

## Action resolution

All actions normalize to `add`, `hold`, `reduce` or `sell`. Evidence precedence is:

1. hard risk and untradeable/invalid position facts;
2. portfolio-risk constraints;
3. formal deterministic signals;
4. external references;
5. LLM explanation.

LLM evidence is advisory-only and cannot upgrade or reverse an action. At equal priority,
risk reduction wins. The resolver returns the winning reason and all suppressed inputs
for audit without logging financial content.

## Rejected alternatives

- Do not auto-publish LLM-generated factors.
- Do not replace the event-driven A-share backtester with a vectorized engine.
- Do not infer historical industry membership from the current industry label.
- Do not make trade attribution mandatory for ordinary manual or broker imports.


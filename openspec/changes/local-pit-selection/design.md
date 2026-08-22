# Design

## Data flow

After market close, one whole-market request per trading date updates qfq daily
bars. Separate snapshots update valuation and four PIT financial tables. Every row
records provider, origin, as-of/effective/retrieval timestamps, adjustment/unit,
schema version and quality. Initial history is populated in 320-500 whole-market
date requests rather than thousands of per-symbol calls.

At selection time the formal rank performs no external market or financial reads:

```text
local universe + PIT fundamentals + qfq bars + structured events
  -> hard gates
  -> fundamental/value TOP200
  -> 60-day structure TOP50
  -> 120/250 corrections
  -> industry breadth/concentration TOP20
  -> local TOP15 -> deterministic TOP5

Wencai cache -> reference candidates -> overlap/difference report only
Agent/quotes -> review and display metadata only
```

## Interface boundaries

Each endpoint contract distinguishes published, protocol and conservative
operational limits. Environment overrides may only tighten minimum interval,
concurrency and retries; they cannot loosen the built-in boundary. Source adapters
own normalization and local request gates. The synchronizer owns completeness,
exact-date acceptance and source repair. The selector reads only the warehouse.

## Missing data

- Required: listed identity, current exact-date bar, positive price/volume and at
  least 60 valid trading days. Missing values fail the gate.
- Preferred: 120/250 history, valuation and individual financial metrics. Missing
  values reduce data coverage or component score.
- Informational: Wencai overlap and optional event descriptions. Missing values do
  not change the primary score.

Stale prior-trading-day data, unknown volume units, warehouse coverage below the
configured minimum, or insufficient financial coverage yields an incomplete run
with no primary candidates. It never activates an external discovery fallback.

## Scoring

The base score is 100: fundamental/value 40, 60-day technical structure 30,
industry breadth 15 and data/governance quality 15. The 120-day and 250-day layers
are risk-oriented corrections in `[-10,+5]`. Structured events are asymmetric in
`[-25,+12]` with longer half-lives for regulatory/fraud risks than ordinary news.

## Rejected alternatives

- A new natural-language screener as the primary source: not reproducible or PIT-safe.
- Per-symbol full-market bootstrap: unnecessarily exceeds provider and runtime budgets.
- Falling back to Wencai on missing local data: hides data-quality failure.
- Requiring MA250 for all securities: unfairly excludes newer listings and early reversals.

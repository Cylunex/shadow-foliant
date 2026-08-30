# Factor and Strategy Reliability V6

## Motivation

The local selector already has a deterministic PIT boundary, but several signals still mix market
Beta, industry exposure, duplicate factor families, one-period accounting growth and idealized
execution. These effects can make a strategy appear more predictive or more liquid than it is.

The low-price bull strategy has useful independent evidence and remains a formal high-priority
satellite. Its price boundary changes to a strict `< 20`, with quality checks that fail open only
when optional historical fields are genuinely unavailable.

## Scope

- Use executable forward-return labels and neutralize factor tests against available PIT exposures.
- Control multiple testing and require time-split stability before a factor is called valid.
- Balance factor-family weights and count duplicate local strategy families once.
- Replace share-volume liquidity proxies with traded amount where possible.
- Add multi-period growth quality and persistent fund-flow evidence.
- Apply dated A-share execution rules, historical stamp tax and portfolio capacity constraints.
- Keep Wencai as a parallel reference that cannot nominate or score formal candidates.

## Compatibility

- Existing local strategy IDs remain stable so historical evidence stays comparable.
- Missing optional financial history does not silently become a failing value.
- Existing APIs retain their response shapes and gain only additive diagnostics.

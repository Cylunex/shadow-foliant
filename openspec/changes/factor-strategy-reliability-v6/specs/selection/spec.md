# Factor and strategy reliability requirements

## Low-price bull preservation

The formal selector MUST retain low-price bull as an independent high-priority strategy family. Its
price boundary MUST be strictly below 20.

### Scenario: exact boundary

- Given otherwise identical eligible securities priced at 19.99 and 20.00
- When low-price bull nominations are built
- Then the 19.99 security may be nominated
- And the 20.00 security is excluded

### Scenario: optional history

- Given recent three-period growth evidence is available
- When fewer than two periods are positive
- Then the security is excluded from low-price bull
- But absent historical evidence remains unknown and does not silently become zero

## Independent confirmation

Formal shortlist confirmation MUST count distinct strategy families rather than strategy names.
Low-price bull MUST have its own family. Small-cap and profit-growth MUST share one growth family.

## Factor validation

Factor returns MUST use T+1 executable entry prices. Available Beta, PIT industry and PIT size
exposures MUST be removed before IC evaluation. A factor MUST NOT be called effective unless it has
the correct direction, passes configured multiple-testing control and remains positive in at least
two of three chronological folds.

## Execution realism

Portfolio simulation MUST apply price-limit executability, seller stamp tax effective on the trade
date and an explicit daily-amount participation cap. It MUST NOT infer traded amount from ambiguous
volume units when a provider does not supply amount.

## External reference

Wencai MAY run in parallel with a `<20` reference query. It MUST NOT change formal membership, score,
strategy-family confirmation or fallback behavior.

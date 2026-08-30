# Design

## Formal strategy layer

`低价擒牛` remains an independent high-priority strategy family. It requires a price strictly below
20 and profit growth of at least 100%. When available, positive profit, non-negative revenue growth,
cash conversion and recent three-period growth persistence protect against low-base and one-off
earnings. Market-cap growth strategies share one family so duplicate nominations do not manufacture
extra confirmation.

The main-force lane requires an exact current-date snapshot plus positive persistence over up to five
stored trading dates. Old flow data can describe persistence but can never stand in for the current
signal.

## Factor layer

Technical ranking uses 60-day market-residual and industry-residual trend, idiosyncratic volatility,
drawdown, persistence, maximum daily return and traded-amount trends. Fundamental scores allocate
budgets to profitability, growth, balance-sheet, cash-flow and valuation families, so a family does
not gain weight merely by having more fields.

Factor evaluation measures T+1 open to T+H close returns, excludes non-executable entries and removes
available Beta, PIT industry and log-size exposure. Benjamini-Hochberg FDR and positive time-fold
stability are both required for an effective verdict. Production weights accept only correctly
directed effective or weak factors, retain at most two per family and give each surviving family an
equal budget.

## Execution layer

Daily-bar execution rejects zero-volume and one-price limit bars. Board and dated ST price limits are
resolved before matching. Seller stamp tax follows the trade date unless explicitly overridden.
Entries default to at most 10% of reliable daily traded amount; missing amount disables only that
capacity check and is never reconstructed from ambiguous volume units.

## Rejected alternatives

- Removing or demoting the low-price bull strategy.
- Treating Wencai membership as independent confirmation.
- Summing every correlated factor or strategy hit.
- Calling current close-to-future close return executable.
- Reconstructing traded amount from provider volume without a declared unit.

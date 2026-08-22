# Design

## Calendar consensus

Foliant stores daily open/closed evidence from zzshare and BaoStock. A completed
market date is usable only when at least two providers cover the cutoff and both
mark the date open. Disagreement is visible and selection fails closed.

## PIT coverage

The coverage start is the later of the first security-master snapshot and the
first date on which financial facts were observed. Earlier dates are labelled
`historical_pit_unavailable`; historical report dates do not retroactively make
facts observable before Foliant first saw them.

## Valuation gate

PE/PB and market-cap fields must come from the same completed trading date as
the qfq panel. The selector records valuation date, coverage and stale trading
days. Missing or stale valuation snapshots cannot contribute a score.

## Artifacts and overlays

Formal artifacts contain identifiers, local scores, score components, technical
state, data quality, dates and rule version only. Quotes, current holdings,
names and AI review are stored under a separate display overlay and may be
merged only while formatting a notification.

## Readiness

`/readyz` remains process readiness. Protected `/research-readyz` reads only
warehouse metadata and reports calendar consensus, PIT coverage, market,
valuation, finance, latest sync and latest selection states without returning
securities, positions, trades or credentials.

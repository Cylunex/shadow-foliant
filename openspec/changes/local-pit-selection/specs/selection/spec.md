# Local PIT selection requirements

## Primary data boundary

The selector MUST derive primary candidates only from local rows whose effective
time is no later than the selection date. Quarterly financial rows MUST have a
publication date no later than the selection date.

### Scenario: future financial publication

- Given a report published after the selection date
- When the PIT snapshot is stored or selected
- Then that report is rejected and cannot affect a score

### Scenario: incomplete warehouse

- Given exact-date market coverage below the configured minimum
- When morning selection runs
- Then the run is marked incomplete and contains no primary candidates
- And Wencai reference candidates do not become primary candidates

## Trading-day periods

Technical windows MUST count valid trading rows. The 60-day layer MUST own the
primary technical score. The 120-day and 250-day layers MUST be corrections and
MUST NOT exclude a security solely because the longer history is unavailable.

### Scenario: newly listed security

- Given at least 60 but fewer than 250 valid trading days
- When selection runs
- Then the security may pass the primary technical stage
- And its long-history data coverage is reduced

## External discovery reference

Wencai MUST be stored as `wencai_reference`. Its membership and source labels MUST
NOT alter primary gates, component scores, ranking, industry quota or fallback.

### Scenario: Wencai-only stock

- Given a stock present only in the Wencai result
- When local selection completes
- Then the stock appears only in `reference_only`
- And it is absent from primary candidates

## Interface contracts

Every formal provider endpoint MUST declare authentication, hard/page limits,
operational interval, concurrency, timeout, retries, PIT support and adjustment
semantics. Runtime overrides MUST NOT loosen the built-in frequency, concurrency
or retry boundary.

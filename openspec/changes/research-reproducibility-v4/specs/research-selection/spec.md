# Research Selection Requirement Delta

## Requirements

### Decision context

The selector MUST bind every formal input read to one explicit decision context and MUST reject contradictory cutoffs.

#### Scenario: equivalent invocation

- GIVEN the same decision timestamp and market cutoff
- WHEN a caller uses the default or explicit invocation form
- THEN the selected market date and all availability cutoffs are identical

### Published universe

Formal selection MUST use only an atomically published security-master snapshot that passed count, exchange distribution, uniqueness, and change validation.

#### Scenario: truncated provider response

- GIVEN a provider returns a materially incomplete security master
- WHEN synchronization finishes validation
- THEN the snapshot remains unpublished and selection continues using the last valid PIT snapshot or fails closed

### Effective dates and statement periods

Formal valuation rows MUST expose a verified provider-effective date equal to the market cutoff. Cross-statement financial metrics MUST use a common statement period.

#### Scenario: mixed statements

- GIVEN income and cash-flow statements have no common latest period
- WHEN fundamentals are scored
- THEN cash-flow-to-profit is unavailable and a period-mismatch diagnostic is recorded

### Immutable formal artifacts

TOP15 and TOP5 MUST be append-only, hash-addressed artifacts bound to an immutable input manifest, policy hash, code revision, and dependency-specification hash.

#### Scenario: display update

- GIVEN a formal TOP5 exists
- WHEN quotes, holdings, AI review, or external references change
- THEN the formal artifact payload and hash remain unchanged

#### Scenario: provider revises historical input

- GIVEN a formal Manifest references older market, valuation and event revisions
- WHEN a provider later corrects canonical data
- THEN Replay reads the immutable referenced observations and revisions visible at the decision cutoff
- AND reports exact artifact hash match or an explicit mismatch

### Formal publication

A formal reader MUST return only a successful published run with a Manifest and both TOP15 and TOP5
artifacts. Diagnostic attempts MUST remain separately queryable.

#### Scenario: failed newer attempt

- GIVEN a published formal result exists
- AND a newer selection attempt fails or remains incomplete
- WHEN the latest formal result is requested
- THEN the older complete published result remains authoritative

### Fail-closed diversification

Unknown or insufficient pairwise correlation MUST NOT be represented as a low numeric correlation.

#### Scenario: insufficient overlap

- GIVEN two candidates have fewer than the required common trading days
- WHEN diversification runs
- THEN their correlation is marked insufficient and the configured fail-closed rule is applied

### Readiness

Data and selection readiness MUST be evaluated separately for an explicit decision context and MUST NOT read market data after that context.

#### Scenario: historical readiness

- GIVEN a historical decision date
- WHEN readiness is requested
- THEN all market, valuation, financial, and artifact queries are bounded by that date

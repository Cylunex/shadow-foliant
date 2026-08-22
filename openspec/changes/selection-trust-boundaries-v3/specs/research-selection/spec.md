# Research Selection Requirements

## Requirement: Independent trading-calendar consensus

The selector MUST use a completed trading day confirmed open by at least two
independent calendar providers and MUST reject uncovered or conflicting dates.

### Scenario: One provider omits an open day

- **WHEN** one covered provider marks a day open and another marks it closed
- **THEN** the formal selector returns `incomplete`

## Requirement: Same-date valuation

Formal valuation factors MUST come from the exact qfq market date and MUST meet
the configured universe coverage threshold.

### Scenario: Latest valuation is older than market data

- **WHEN** the qfq panel is current but the valuation snapshot is older
- **THEN** PE/PB contribute no score and the run returns `incomplete`

## Requirement: Honest historical PIT boundary

The service MUST expose the first date for which both universe and finance facts
were observable and MUST NOT present earlier runs as strict PIT playback.

### Scenario: Selection predates first observation

- **WHEN** the requested selection date is earlier than the PIT coverage start
- **THEN** the run is persisted as `incomplete` with
  `historical_pit_unavailable`

## Requirement: Immutable formal artifacts

Formal local artifacts MUST NOT contain quotes, holdings or AI review fields.

### Scenario: Display inputs change

- **WHEN** quote, portfolio or AI fields differ for the same local snapshot
- **THEN** the formal artifact and ordering remain byte-equivalent

## Requirement: Research readiness

The service MUST expose protected research-data readiness independently from
process readiness and MUST return only aggregate, non-sensitive state.

### Scenario: Database is reachable but market data is stale

- **WHEN** process readiness succeeds and research snapshots are stale
- **THEN** `/readyz` may remain ready while `/research-readyz` returns degraded

## ADDED Requirements

### Requirement: Historical selection is point-in-time reproducible

The system SHALL select security identity and each financial fact only from rows
whose snapshot, publication and first-seen dates are visible on the selection date.

#### Scenario: A report is revised later

- **GIVEN** an original report visible on day D and a revision first seen after D
- **WHEN** selection for D loads financial facts
- **THEN** it receives the original revision and never the later revision

### Requirement: Incomplete local data fails closed

The system SHALL return no primary candidates when its prior-trading-day snapshot,
normalized qfq coverage or fundamental coverage is below policy.

#### Scenario: The last market snapshot is stale

- **GIVEN** the calendar expects trading day D and the latest usable bar is D-1
- **WHEN** local selection runs
- **THEN** the run is `incomplete`, includes both dates, and does not use Wencai

### Requirement: Formal recommendations are deterministic

The system SHALL derive formal TOP5 ordering only from the persisted local PIT
score, local rank and symbol tie-breaker.

#### Scenario: Agent or quote output changes

- **GIVEN** the same local TOP15 snapshot with different Agent verdicts or quotes
- **WHEN** the formal TOP5 is finalized
- **THEN** its membership and ordering remain identical

### Requirement: Formal events require body confirmation

The system SHALL NOT persist or score a title-only disclosure classification.

#### Scenario: Only an announcement title is available

- **WHEN** the title classifier marks a potentially material event
- **THEN** it remains reference metadata and creates neither a signal nor an event

### Requirement: Production persistence is PostgreSQL-only

The application SHALL fail on incomplete PostgreSQL configuration and SHALL NOT
silently create or use a local business SQLite database.

#### Scenario: PostgreSQL is unavailable

- **WHEN** a production repository attempts a data operation
- **THEN** it returns an explicit failure and never writes a local `.db` file

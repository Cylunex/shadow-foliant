# Added requirements

## Requirement: Honest historical capture

The system MUST distinguish a current legacy capture from original historical PIT
evidence and MUST NOT repair old manifests by silently substituting live history.

### Scenario: Resume an interrupted bounded capture

Given unfrozen historical bars, when one bounded transaction succeeds and another
fails, a retry captures only remaining rows and preserves all prices and prior
observations. A replay of an old manifest still reports its original history gap.

## Requirement: Independent readiness clocks

Formal morning selection MUST remain evaluated against its preopen data boundary
after the close. Missing scheduled evening ingestion MUST NOT invalidate that
decision simply because the wall clock has passed 16:00.

### Scenario: Before the evening ingestion window

At 16:30, the data health response identifies evening acquisition as pending and
keeps the prior-day decision-input checks. At 18:05, evening data readiness uses
postclose checks; the formal selection endpoint continues using preopen checks.

## Requirement: Bounded refusal and evaluation infrastructure handling

The system MUST suppress repeated HTTP refusals during cooldown and MUST NOT queue
another Wencai request behind an unfinished timed-out query. Sealed evaluation
MUST preflight its worker before reserving a batch without reading sealed contents.

### Scenario: Missing worker

Given a private sealed file but no Docker executable, evaluator construction fails
before batch reservation or sealed-content reads.

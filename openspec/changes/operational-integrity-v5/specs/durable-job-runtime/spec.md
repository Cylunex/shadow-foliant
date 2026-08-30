# Durable Job Runtime Requirements

## Requirement: writes require a current fence

Every Run heartbeat, progress update and terminal publication MUST require the current worker identity
and fencing token.

### Scenario: expired worker completes late

- **WHEN** a lease is reclaimed and the old child later returns a result
- **THEN** the old completion is rejected and cannot emit an outbox event

## Requirement: mutable jobs are terminable

Scheduled jobs that write business state or notifications MUST execute outside the scheduler thread
in a terminable child process.

### Scenario: job exceeds its budget

- **WHEN** a mutable job exceeds its timeout
- **THEN** cancellation is requested, the child is terminated after a bounded grace period and no
  later publication from that attempt is accepted


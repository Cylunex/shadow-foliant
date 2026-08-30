# Trade Ledger Integrity Requirements

## Requirement: position-changing trades are fail-closed

The system MUST reject a sale that has no current position or exceeds available quantity when the
request changes the current position.

### Scenario: oversized sale

- **WHEN** an administrator previews or confirms a sale above the locked available quantity
- **THEN** the request is rejected without writing a trade, position change or realized profit/loss

## Requirement: confirmation is atomic and bound to preview state

The system MUST bind confirmation to normalized rows and a position watermark and MUST commit all
confirmed rows and their position effects in one transaction.

### Scenario: position changes after preview

- **WHEN** a position changes before confirmation
- **THEN** confirmation returns a conflict and writes no rows

## Requirement: trade execution idempotency

The system MUST prefer a broker execution identifier and MUST otherwise use a complete stable
fingerprint without relying on a bounded recent-row scan.

### Scenario: two equal fills with distinct execution identifiers

- **WHEN** two fills have equal security, time, side, quantity and price but different execution IDs
- **THEN** both fills are recorded


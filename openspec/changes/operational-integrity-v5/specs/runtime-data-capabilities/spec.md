# Runtime Data Capability Requirements

## Requirement: runtime status is safe and actionable

The system MUST expose configured, enabled, freshness, cooldown, quota and fallback metadata per
provider endpoint without credentials, private URLs, request bodies or raw exception text.

### Scenario: provider is cooling down

- **WHEN** a provider endpoint is unavailable until a known time
- **THEN** capability output identifies the safe error category and cooldown time and the router does
  not select it before that time

## Requirement: formal reads use a stable publication vector

Formal selection MUST compare dataset generations before and after input loading and MUST retry only
a bounded number of times.

### Scenario: publication changes during loading

- **WHEN** any required dataset generation changes during input loading
- **THEN** the partial read is discarded and retried; persistent drift yields an incomplete result


# Domain plugin requirements

## Requirement: Runtime-independent application services

Foliant MUST expose Web, machine HTTP and compatible MCP use cases through application services that
do not depend on a particular Agent runtime.

### Scenario: Machine research does not import MCP

- **WHEN** the machine research API serves a request
- **THEN** it calls `SecurityResearchService`
- **AND** neither the HTTP module nor the service imports `mcp_server.py`

## Requirement: Preview and formal results remain separate

Agent-created research, selection and backtest Runs MUST be persisted with `mode=preview` and MUST
NOT replace or mutate a formal research or selection artifact.

### Scenario: Selection preview completes

- **WHEN** an authorized Agent creates a selection preview
- **THEN** a persistent preview Run is created and can be queried after restart
- **AND** the latest formal SelectionRun is unchanged

### Scenario: Web or worker restarts

- **WHEN** the Web process exits after creating a queued Run
- **THEN** the jobs-hub worker can claim and execute the persisted canonical request
- **AND WHEN** a worker lease expires
- **THEN** another worker retries within the attempt budget without accepting a late stale result

## Requirement: Machine authorization includes a capability grant

Every versioned Agent route MUST validate audience, coarse scope and its exact `foliant.*`
capability grant in the local Agent registry.

### Scenario: Scope without capability

- **WHEN** an Agent has `stock.research` but lacks `foliant.selection.preview`
- **THEN** selection preview returns JSON 403 without a browser redirect

## Requirement: Runtime receives usable bounded results

Reference results MUST preserve Run identity and status. Structured results MUST expose only the
bounded `model_payload` plus provenance, warnings and continuation metadata.

### Scenario: Preview result retrieval

- **WHEN** a Runtime creates and later reads a completed preview Run
- **THEN** it can retain the `run_id`, observe status and consume bounded result data

## Requirement: Machine creation is idempotent

Run creation MUST require an idempotency key scoped to the authenticated Agent and capability.

### Scenario: Key reuse

- **WHEN** the same key and canonical request are submitted twice
- **THEN** both responses identify the same Run
- **BUT WHEN** the request differs
- **THEN** the service returns a conflict without starting another Run

## Requirement: Administrator transaction entry remains available

The Stock Web application MUST let an administrator preview and explicitly confirm valid A-share
transaction records through server-authorized endpoints.

### Scenario: Trade confirmation

- **WHEN** a `stock-admins` user previews a valid transaction and explicitly confirms it
- **THEN** the transaction is recorded and the configured position update is applied
- **AND** an ordinary user or Agent research token cannot use that endpoint

## Requirement: Research Profile excludes private finance writes

The `shadow-finance-research` Profile MUST enumerate only research read/preview capabilities and MUST
NOT contain portfolio, trade-entry, environment, operations or broker execution capabilities.

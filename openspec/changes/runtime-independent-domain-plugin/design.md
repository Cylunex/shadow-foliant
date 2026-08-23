# Design

## Dependency direction

```text
Web API -----------------+
Agent HTTP API ----------+--> Application Service --> Domain/analysis code --> Repository
MCP compatibility -------+
```

The application package contains use-case orchestration and stable result contracts.  It must not
import FastAPI, MCP, DSH or Cordis.  Machine HTTP must never import `mcp_server.py`.

## Formal facts and preview Runs

Existing formal `selection_runs` and their manifest-bound artifacts remain authoritative.  Agent
research, selection and backtest creation produces a separate `foliant_runs` row with
`mode=preview`.  A preview cannot publish or replace a formal result.  The repository stores request
hashes, lifecycle timestamps, bounded summary/result JSON, stable failure categories and provenance.
An outbox event is inserted in the same transaction that completes a Run.

## Machine trust boundary

Each route has one explicit capability scope and audience `foliant`.  Request bodies cannot select
an owner or portfolio.  Creation requires `Idempotency-Key`; the same key and request hash reuses the
Run, while a different hash conflicts.  Errors are stable JSON classifications and omit exception
text, prompts, SQL and paths.  Request/result budgets, timeouts and per-agent active-Run quotas are
enforced server-side.

## Transaction entry

Trade entry is an authenticated Stock Web administrator action, not a capability in the research
Profile.  The Web flow uses two distinct endpoints: preview normalizes and validates without writing;
confirm creates the transaction and updates the position through a shared `TradeEntryService`.
Authorization is provided by the OIDC Web session and server-side admin route policy; `dry_run` is
not an authorization decision.  Agent-based transaction entry remains unavailable until delegated
actor grants and the Platform `ConfirmationReceipt` issuance/verification/replay-prevention loop are
implemented.

## Rejected alternatives

- Calling MCP from Agent HTTP retains the wrong dependency direction and is rejected.
- Exposing private portfolio or transaction parameters in the research Profile is rejected.
- Treating a DSH approval or a model-supplied `dry_run=false` as transaction authorization is
  rejected.
- Reusing formal selection tables for previews is rejected because it could silently change the
  latest formal result.

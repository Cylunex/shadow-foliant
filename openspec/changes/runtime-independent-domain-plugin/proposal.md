# Runtime-independent Foliant domain plugin

## Motivation

Foliant currently exposes useful research and portfolio functions through Web and MCP, but the
machine HTTP layer still imports the MCP adapter and there is no persistent, runtime-neutral preview
Run resource.  The Stock Web trade page can read transactions but cannot enter them even though the
domain import service already exists.

## Scope

- Add a runtime-independent application layer shared by Web, machine HTTP and MCP adapters.
- Add narrow Agent HTTP contracts for market, formal research/selection reads and preview Runs.
- Persist preview Runs, idempotency records, bounded results and domain outbox events separately from
  formal research and selection artifacts.
- Add the `shadow-foliant` plugin definition, capabilities, contracts and three focused Skills.
- Add an administrator-only Web flow to preview and confirm stock trade entry.
- Keep transaction entry and all private portfolio data out of the ordinary
  `shadow-finance-research` Agent Profile.

## Compatibility

- The Web/PWA remains the user-facing Stock product and continues to use OIDC sessions.
- Existing stdio MCP remains available as a compatibility adapter, but new Agent capabilities are
  HTTP-only and MCP no longer owns their business semantics.
- No DSH, Cordis or DSH Tools dependency is added to Foliant.
- Production deployment is explicitly out of scope for this change.

# Add bounded Mairui-compatible supplemental sources

## Motivation

Foliant has independent price fallbacks, but daily order flow still largely depends on one
upstream family and it lacks normalized investor Q&A, auction and limit-board reference data.
Two available 500-request/day API credentials can fill those gaps for holdings and final
candidates without changing the primary market-data architecture.

## Scope

- Add optional Mairui and MOMA atomic providers.
- Use them only as late quote/K-line/order-flow/disclosure fallbacks or reference-only event data.
- Persist and enforce a conservative provider-wide daily request budget.
- Keep credentials, request URLs, payloads and financial data out of logs and runtime metadata.
- Expose Mairui's normalized Q&A, limit-board and auction reference capabilities through `datahub`;
  do not invent those capabilities for MOMA while its public documentation does not list them.

## Compatibility

The providers are disabled automatically when their credential is absent. Existing function
signatures and primary routes remain unchanged.

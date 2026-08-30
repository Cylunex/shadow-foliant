# Research Trust Requirements

## Security lifecycle

The system MUST ingest active, delisted and paused A-share lifecycle records without
silently rewriting all provider rows to active status.

### Scenario: historical membership reconstruction

- Given a security listed before the requested date and delisted after it
- When a research universe is loaded for that date
- Then the security is included and the result declares lifecycle-backfill provenance.

### Scenario: unavailable lifecycle evidence

- Given no complete lifecycle snapshot
- When default factor research runs
- Then the legacy bounded cohort may be used
- And the result MUST declare survivorship risk and exploratory confidence.

## Research and production feature parity

Production selection component weights MUST come from one versioned feature catalog.
Research output MUST disclose the catalog version and universe basis.

## Strategy deployment

An evolved strategy MUST NOT enter the live set without fresh holdout evidence meeting
the configured minimum observations, win rate, return and production score. Failure MUST
fall back to the fixed strategy definition.

## Trade attribution

Trade attribution is optional. When a nomination identifier is supplied, the server MUST
verify that it exists and matches the traded symbol before confirming the trade.

## Action resolution

The application MUST emit one normalized action after deterministic precedence rules.
An LLM recommendation MUST NOT override a hard-risk, portfolio-risk or formal action.

## Logging

Notification logs MUST NOT contain message bodies, securities, email addresses, webhook
destinations or credentials. They MAY contain opaque notification IDs, categories,
channels and delivery outcomes.


# Design

## Trade ledger boundary

Preview normalizes rows, derives stable execution fingerprints, reads current positions and returns
before/after quantities plus cash effects. Confirmation binds the normalized payload and position
watermark, obtains position row locks, revalidates every row and commits the whole batch atomically.
When `update_position=true`, a sale without a position or above the available quantity fails closed.
Record-only history import is an explicit mode and does not fabricate position snapshots or realized
profit/loss.

Broker execution identifiers are authoritative when present. A fallback fingerprint includes source,
account reference, order identifier, wall-clock fill time, security, side, quantity, price and fees.
Legacy rows are not rewritten to invent an execution identifier.

## Run and publication boundary

The existing `foliant_runs` lease worker is extended rather than replaced. Every claim receives a
new random fencing token and an immutable attempt record. Heartbeat, progress, completion and failure
require the current worker and token. A late process therefore cannot write after lease expiry or
reclaim. Mutable scheduled tasks execute in child processes and use the same guarded publication
boundary.

## Data capability and consistency boundary

Static endpoint contracts remain the maximum capability truth. A runtime registry overlays configured,
enabled, last-success, last-error-category, cooldown, quota and freshness state without secrets or
request payloads. Provider choice is expressed per dataset, not as one global provider.
Optional Redis coordination is attempted only when explicitly configured, uses bounded single-attempt
connection/read budgets and always degrades to the in-process gate.

Each successfully validated dataset publication advances a generation. Formal readers capture a
generation vector before loading, compare it after loading and retry a bounded number of times. A
continuously changing vector fails with `dataset_publication_unstable`; formal artifacts record both
Dataset IDs and the stable vector.

## Research artifact boundary

`research-artifact-v1` is append-only and contains a decision context, structured thesis, evidence
references, freshness, invalidation conditions, next actions, quality and provenance. Deterministic
facts are authoritative. LLM text is stored only as a separate annotation and cannot overwrite
evidence, score, membership or order.

## Rejected alternatives

- Silently reducing an oversized sale to the current position.
- Treating the last 10,000 trade rows as an idempotency boundary.
- Starting a second task framework beside `foliant_runs`.
- Automatically substituting raw TDX bars for adjusted research history.
- Falling back to Wencai when the local warehouse is incomplete.
- Logging provider exception text, URLs, credentials or financial payloads as runtime diagnostics.

# Design

## Decision boundary

Every formal run uses an immutable `DecisionContext` containing the decision timestamp, completed market cutoff, universe/financial/event cutoffs, policy identity, and code revision. Inclusion rules no longer depend on whether an optional argument happened to be supplied.

## Data publication

Provider observations are append-only. A security-master fetch creates a snapshot run, writes staging rows, validates absolute and exchange counts plus change ratios, then atomically publishes it. Failed snapshots remain diagnostic evidence and are never eligible for selection.

Calendar responses retain explicit rows and are synchronized in bounded chunks. A missing provider row is unknown, not closed. Consensus requires two explicit observations.

Valuation rows keep requested and provider-effective dates separately. Rows without a verifiable provider-effective date are not canonical selection inputs.

Financial statements are selected around the latest common statement period. Cross-statement ratios are unavailable when required statements do not share that period.

## Reproducibility

Formal runs store a canonical policy document and hash, code revision, dependency-specification hash, schema version, and immutable dataset identifiers in an input manifest. TOP15 and TOP5 are append-only artifacts with payload hashes. Display, AI review, and external references are separate append-only attachments and cannot change formal ranking.

Market and valuation Replay reads immutable observations by Dataset ID. Event corrections read the
latest append-only event revision visible at the recorded cutoff and hash revision IDs plus content
hashes. Financial Replay uses `first_seen_at` visibility. Formal publication is atomic with Manifest,
TOP15 and TOP5 creation; `latest_formal_selection` ignores every failed, incomplete or unpublished
attempt. The dependency hash includes the resolved Python minor version and installed distributions.

## Runtime and release

`/readyz` reports operational dependencies and revision. `/data-readyz` evaluates data for an explicit decision context. `/selection-readyz` requires a matching successful formal artifact. `/research-readyz` remains a compatibility aggregate.

Deployment validates tests before mutation, executes schema migration under a PostgreSQL advisory lock, probes candidate readiness, checks the loaded revision, and supports restoring the previous commit. Redis remains optional unless a configured feature declares it required.

## Rejected alternatives

- Treating missing calendar rows as closed days.
- Publishing a partial universe and relying on downstream percentage coverage.
- Hashing only ranked output while canonical inputs remain mutable.
- Treating unknown correlation as negative correlation.
- Letting environment variables silently loosen the production policy.

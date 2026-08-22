# Design

## Boundaries

Each provider module is an atomic source: it may normalize its provider's response but cannot cache,
call another provider, or call DataHub. DataHub owns source selection, timeouts, health scoring, cache,
and stale-data policy.

## TDX lifecycle

eltdx is the Python 3.10+ production fallback and keeps a bounded in-process connection pool.
tdx-python is an additional Python 3.12+ fallback running in one spawned worker per Foliant process. The worker owns the native
client, discards its stdout/stderr so selected nodes cannot enter application logs, and returns only
primitive bar/quote fields over a Pipe. Parent-side timeouts discard and recreate a stuck worker.
Logical requests are bounded and split into protocol pages of at most 800 bars. easy-tdx and mootdx
remain lower-priority compatibility sources because live probes may connect successfully yet return
empty data with the legacy protocol.

## Data contract

Providers return lower-case `date/open/high/low/close/volume/amount`; DataHub converts this to its
existing `Date` index and `Open/Close/High/Low/Volume` columns. Daily timestamps are normalized to
midnight. Intraday timestamps use bar-end semantics. Volume is expressed in shares; zzshare's unit is
verified from amount/volume/close when possible rather than cross-querying another provider.

## Failure and security

Provider errors return empty values and are counted by DataHub. The zzshare SDK logger is disabled
during calls because upstream error handling may log response bodies. Tokens are read from the
environment and never included in URLs, application logs, caches, or returned metadata. Public health
checks never establish provider connections.

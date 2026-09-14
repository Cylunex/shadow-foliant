# Fuyao official market-data provider requirements

## Requirements

The system MUST keep the API key outside versioned files and MUST NOT expose it in logs, errors,
telemetry, database rows or test artifacts.

The adapter MUST validate both HTTP status and the documented business response code. It MUST bound
timeouts, concurrency, rate, retry count, backoff and cache size. Permission, rate-limit, empty/not
ready, unsupported and upstream failures MUST remain distinguishable through safe categories.

Batch quote results MUST preserve requested symbol order, use documented units, and expose provider,
request ID, source timestamp, market-as-of date, adjustment, currency and freshness metadata.

The historical adapter MUST request only the supported daily interval and MUST map raw, forward and
backward adjustment semantics without guessing unsupported intervals.

The calendar pipeline MUST require at least two independent complete sources. Fuyao evidence MUST
only be used inside the returned rolling coverage window.

Latest-only valuation data MUST NOT be backdated. Same-day post-close snapshots MAY participate in
formal per-field valuation composition with explicit provenance.

The capital-flow capability MUST remain explicitly degraded while the official documentation marks
external access unavailable, and MUST NOT issue a request.

## Scenarios

- Given HTTP 200 with business code 2003, the adapter reports permission degradation and no payload.
- Given HTTP 429 or business code 4001, the adapter retries at most twice with bounded backoff.
- Given a current-day close snapshot read later that evening, freshness is `closing_current`, not stale.
- Given a provider failure or circuit cooldown, datahub continues to its existing fallback source.
- Given no configured key, all pre-existing public data routes remain operational and unchanged.

# Trust boundaries

Legacy capture records the current warehouse observation, not historical knowledge.
Only unfrozen qfq rows receive new dataset pointers, under bounded transactions.
Original prices, retrieval timestamps, immutable observations, and existing manifests
are not rewritten. Capture timestamp and provenance class live in the new payload.
An independent market_archive publication fence invalidates concurrent selection
reads without moving daily_market freshness backwards. A composite observation
index keeps missing-evidence audits and replay joins bounded by indexed identities.

Replay only reads frozen qfq observations within the manifest's market cutoff. It
reports actual frozen history coverage instead of substituting mutable live bars.

Morning selection readiness uses preopen context throughout the day. Default data
readiness switches to postclose at the existing 18:05 ingestion window, and the
16:00–18:05 response separately labels evening acquisition pending. Sync jobs warm
both context caches after evening publication; HTTP probes do not score the market.

Wencai uses process-local single-flight admission and HTTP-category cooldowns.
Cooldown refusal precedes even internal library retries and provider admission.
This is not an authentication repair or a distributed breaker across processes;
the shared provider governor remains authoritative for quotas.

Sealed evaluation preflight reads file metadata only and executes public synthetic
facts in the existing digest-pinned, no-network worker. Sealed contents remain
unread until atomic reservation. A later failure can still retire the reserved
batch: preflight is not a guarantee against infrastructure races.

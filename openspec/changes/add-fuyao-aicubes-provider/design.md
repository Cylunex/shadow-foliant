# Design

## Trust boundary and flow

The atomic adapter owns authentication, HTTP/business-envelope validation, provider timestamp
normalization and capability-local caching. Endpoint contracts and the host-wide governor enforce
bounded concurrency, rate, timeout, retry budget and circuit state. `datahub` owns cross-provider
routing and persistent K-line caching. Research sync owns calendar consensus and valuation merging.

Keys enter only through a protected environment value or an external dotenv file. The adapter never
logs headers, response bodies, request parameters, provider messages or exception strings from the
HTTP library. Public state is limited to provider/endpoint status, safe request IDs, business codes,
row counts and timestamps.

Calendar rows from the rolling official window become explicit open/closed evidence. Publication
still requires agreement among at least two independent configured sources. A live valuation can
join formal evidence only on its provider-effective date after the local market close.

## Rejected alternatives

- Replacing existing providers: rejected because independent fallback and cross-checking are required.
- Treating every HTTP 200 as success: rejected because the official API returns business errors in a
  successful HTTP envelope.
- Polling the unavailable capital-flow route: rejected because it violates the published boundary.
- Persisting raw responses for debugging: rejected because it expands sensitive-data retention.

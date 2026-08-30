# Design

## Data flow

`datahub` retains routing and caching ownership. Each atomic provider delegates HTTPS transport,
daily quota accounting and credential redaction to a shared path-token client, then normalizes
only data returned by its own endpoint.

## Trust and reliability boundaries

- Credentials are injected by environment variables and URL paths are never logged.
- Only the NAS backend calls the providers; browsers and the edge proxy never receive credentials.
- Each provider has its own persisted 450-request operational budget under a published 500 limit.
- The two APIs are treated as one upstream family for architecture claims. They are not described
  as independent disaster recovery even though credentials, quotas and health records are separate.
- Official CNINFO disclosures remain primary. Aggregated disclosures are explicitly non-official.
- Only Mairui currently advertises Q&A, disclosures, auction and limit-board records. They are
  reference-only and cannot directly create a trade signal; MOMA is limited to confirmed common
  quote, 5m+ K-line and order-flow endpoints.

## Rejected alternatives

- Replacing zzshare/TDX/BaoStock: wastes scarce quota and reduces source independence.
- Importing a provider SDK: direct HTTP gives tighter URL redaction and avoids another dependency.
- Using provider technical indicators/factors: duplicates local reproducible calculations.

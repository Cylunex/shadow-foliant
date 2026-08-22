# Market data requirements delta

## Requirement: A-share intraday routing

The system SHALL route supported A-share minute intervals through explicit atomic providers and SHALL
fall back without exposing provider errors to business callers.

### Scenario: credentialed primary succeeds

- **WHEN** zzshare is configured and returns valid minute bars
- **THEN** DataHub returns those bars without contacting the TDX fallbacks

### Scenario: primary fails

- **WHEN** zzshare is unavailable, empty, or times out
- **THEN** DataHub attempts verified tdx-python, then easy-tdx and mootdx compatibility sources

## Requirement: canonical bar contract

The system SHALL return ordered, deduplicated bars with bar-end intraday timestamps and volume in
shares for Shanghai, Shenzhen, and Beijing securities.

## Requirement: bounded and safe operation

The system SHALL bound provider history requests, paginate TDX requests at 800 bars or fewer, and
SHALL NOT log tokens, request headers, response bodies, selected production hosts, or bar contents.

## Requirement: stale intraday data

The system MAY return an expired intraday cache when all live sources fail, but SHALL mark it stale and
SHALL mark it non-actionable for real-time decisions after ten minutes.

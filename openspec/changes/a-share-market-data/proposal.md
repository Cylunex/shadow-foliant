# Proposal: reliable A-share minute market data

## Motivation

Foliant's current TDX adapter is optional, dependency-conflicted, limited to daily bars at the
DataHub boundary, and relies on a repository-maintained host list. Minute requests can therefore
fall through daily-oriented web scrapers or return empty without a production-ready TDX path.

## Change

- Add zzshare as a token-authenticated A-share daily/minute source.
- Add the currently maintained tdx-python implementation as the verified TDX fallback and isolate
  its native SDK in a log-suppressed worker process.
- Retain easy-tdx and mootdx only as compatibility fallbacks.
- Route intraday bars through an explicit zzshare -> tdx-python -> easy-tdx -> mootdx chain.
- Normalize Shanghai, Shenzhen, and Beijing symbols, bar timestamps, OHLC fields, and volume in shares.
- Add bounded TDX pagination, short intraday caching, stale-data decision guards, and sanitized SDK logs.
- Keep all credentials, selected hosts, and provider configuration outside the repository.

## Compatibility

The public `datahub.kline()` signature and DataFrame columns remain unchanged. Daily routing keeps
its existing sources and gains the new providers as fallbacks. Intraday `adjust=qfq` does not silently
fabricate adjusted bars; corporate-action adjustment remains a research-layer concern.

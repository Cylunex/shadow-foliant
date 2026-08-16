# Proposal: native Shadow Platform integration

## Motivation

The WebUI currently exposes every route without authentication and stores portfolio, trades, jobs,
and configuration as global resources. Foliant also uses project-local LLM provider configuration and
has no explicit HTTP Agent boundary.

## Change

- Replace the unauthenticated WebUI boundary with native OIDC Authorization Code + PKCE using the
  `shadow-stock` client and server-side opaque sessions.
- Require `stock-users` for ordinary research pages and `stock-admins` for all global or mutating
  operational data.
- Introduce an explicit, default-deny route permission matrix covering every FastAPI route.
- Add local Agent Bearer verification for machine endpoints with audience `foliant` and scopes
  `stock.read` / `stock.research`; keep stdio MCP process-local.
- Add stateless `/healthz`, protected `/readyz`, and a safe legacy `/api/health` response.
- Prefer the Shadow process-local LLM SDK when configured while preserving the existing provider
  router as a behavior-compatible fallback during migration.
- Do not add Media UI or storage without a concrete report attachment or upload use case.
- Update the Shadow Platform catalog, OIDC example, schema, doctor, documentation, and tests.

## Compatibility

This is an intentional one-way login cutover. Local, Basic, Forward Auth, Hybrid, proxy identity
headers, and browser-to-Agent credential substitution are not supported. Existing business tables are
not made multi-tenant and are not migrated in this change.

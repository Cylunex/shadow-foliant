# Web authentication requirements

## Native OIDC

The Stock Web application MUST use Authorization Code + PKCE and MUST validate state, nonce, PKCE,
signature, issuer, audience, expiration, issued-at time, and `stock-users` membership before creating
a session.

### Scenario: tampered callback

Given a callback with a missing, expired, reused, or mismatched login transaction, the application
MUST reject it without exchanging a code or creating a session.

## Server-side sessions

The browser MUST receive only an opaque cookie with Secure, HttpOnly, SameSite=Lax, host-only, and
Path=/ attributes. Tokens MUST NOT be persisted in browser storage, logs, or business databases.

## Global data authorization

Until resource ownership is migrated, portfolio, trades, environment, job controls, monitoring,
workflows, and decision signal management MUST require `stock-admins` on the server.

## Machine identity

Machine endpoints MUST ignore browser sessions, require a Bearer with audience `foliant`, enforce
`stock.read` or `stock.research`, and return JSON 401/403 without an Identity redirect.

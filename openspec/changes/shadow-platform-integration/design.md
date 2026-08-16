# Design

## Trust boundaries

1. Browser requests authenticate through OIDC and receive an opaque, host-only HttpOnly cookie.
2. Session and login transaction records live server-side; OIDC tokens are discarded after verified
   claims are normalized.
3. Machine routes ignore browser cookies and accept only a locally verified Agent Bearer.
4. Public health checks are constant and do not touch databases, LLMs, data providers, or queues.
5. Global financial and operational data remains administrator-only until ownership columns and
   resource-level authorization are implemented.

## OIDC validation

Login transactions contain one-time state, nonce, PKCE verifier, expiry, and a sanitized relative
return path. Callback processing consumes state atomically, exchanges the code with the verifier, and
verifies the ID Token signature, issuer, audience, expiry, issued-at time, nonce, and required group.
The stable internal identifier is persisted against the unique `(issuer, subject)` pair.

## Authorization

Every registered FastAPI route has an explicit policy: public, readiness, user, administrator, or
machine scope. Startup/tests fail when a route is absent from the matrix. New routes therefore default
to denial rather than inheriting a broad prefix rule.

## LLM migration

When a Shadow registry and SDK are configured, the existing `LLMRouter.call()` contract is fulfilled
through the in-process SDK using the matching chat or reasoning alias. Any initialization/request
failure falls through to the current provider chain. Prompts and responses stay in Foliant; Platform
usage events contain fixed metadata only.

## Media

No current route uploads report images or attachments. Adding a placeholder upload page would expand
the attack surface without a business owner, so Media remains declared but unused until a concrete
resource model can authorize upload and download.

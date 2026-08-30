# Market data supplements requirements

## Credential safety

The system MUST obtain provider credentials only from repository-external configuration and MUST
NOT log a credential-bearing URL, response payload or provider credential.

### Scenario: provider request fails

- GIVEN a configured provider credential
- WHEN the provider returns an HTTP, JSON or API-level error
- THEN diagnostics contain only the provider name and bounded error category
- AND no credential, query content or response body is emitted

## Daily quota

The system MUST persist one daily counter per provider across local processes and MUST reject calls
before the configured budget exceeds the published 500-request boundary.

### Scenario: budget exhausted

- GIVEN a provider has consumed its operational daily budget
- WHEN any capability attempts another request
- THEN no HTTP request is sent and the route continues to another source or returns an empty value

## Routing

Existing independent price sources MUST remain ahead of the Mairui-compatible family. CNINFO MUST
remain the primary official disclosure source. Investor Q&A, auction and limit-board data MUST be
marked reference-only and MUST NOT directly alter formal selection scores or trade actions.

### Scenario: provider capability is not publicly documented

- GIVEN a capability is published by Mairui but not by MOMA
- WHEN runtime routes and contracts are built
- THEN only Mairui exposes that capability
- AND MOMA quota is not consumed probing an assumed equivalent path

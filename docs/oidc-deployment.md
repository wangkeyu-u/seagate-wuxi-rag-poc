# OIDC / JWKS authentication boundary

Status: implemented RS256 validation path requiring deployment-specific configuration. This repository is not connected to a real enterprise identity provider and contains no real issuer, tenant, group ID, client ID, or signing key.

## Supported deployment shapes

The service supports three explicit authentication shapes:

1. `--dev-auth`: loopback-only synthetic identities for local demonstration.
2. `gateway-hs256`: the existing compatibility boundary for a trusted gateway that mints the PoC's normalized short-lived envelope.
3. `oidc-rs256`: direct validation of an enterprise JWT against a configured issuer, audience, and HTTPS JWKS endpoint.

For the browser UI, the preferred enterprise shape is an approved same-origin identity-aware proxy. The proxy completes the organization's login/session flow and injects the user's access JWT on upstream requests. The UI first attempts `/api/whoami` without creating a local development identity, so this proxy pattern works without exposing tokens to application JavaScript. API clients may send the same JWT with `Authorization: Bearer`.

This repository does not implement an authorization-code callback, token endpoint, refresh-token store, or enterprise logout. Those functions should remain with the approved identity proxy unless the security team explicitly chooses an application-owned BFF design.

## OIDC configuration

Use deployment values approved for this API. The following values are placeholders and will not connect to a real IdP:

```bash
RAG_AUTH_MODE='oidc-rs256' \
RAG_OIDC_ISSUER='https://idp.example.internal/tenant/v2.0' \
RAG_OIDC_AUDIENCE='api://yield-copilot' \
RAG_OIDC_JWKS_URL='https://idp.example.internal/tenant/discovery/keys' \
RAG_OIDC_GROUP_ROLE_MAP='{"enterprise-group-pe":"PRODUCT_ENGINEER","enterprise-group-qa":"QUALITY_ENGINEER"}' \
RAG_OIDC_PERMISSION_ALLOWLIST='investigations:read:all,sources:monitor,sources:quarantine:manage' \
RAG_ALLOWED_ORIGINS='https://yield-copilot.example.internal' \
python3 server.py --host 127.0.0.1 --port 8787
```

Required variables:

| Variable | Purpose |
| --- | --- |
| `RAG_AUTH_MODE` | Must be `oidc-rs256` for this path |
| `RAG_OIDC_ISSUER` | Exact HTTPS `iss` value expected in the token |
| `RAG_OIDC_AUDIENCE` | Exact API audience required in `aud` |
| `RAG_OIDC_JWKS_URL` | Fixed HTTPS JWK Set location; token headers cannot override it |
| `RAG_OIDC_GROUP_ROLE_MAP` | JSON object mapping approved enterprise groups to application roles |

Optional variables:

| Variable | Default | Bounds / behavior |
| --- | --- | --- |
| `RAG_OIDC_GROUPS_CLAIM` | `groups` | Top-level group array claim |
| `RAG_OIDC_LINES_CLAIM` | `line_ids` | Top-level permitted-line array claim |
| `RAG_OIDC_STATIONS_CLAIM` | `station_ids` | Top-level permitted-station array claim |
| `RAG_OIDC_PERMISSIONS_CLAIM` | `permissions` | Top-level permission array claim |
| `RAG_OIDC_PERMISSION_ALLOWLIST` | empty | Comma-separated permissions the service may honor; all others are dropped |
| `RAG_OIDC_CLOCK_SKEW_SECONDS` | `30` | 0–300 seconds |
| `RAG_OIDC_MAX_TOKEN_LIFETIME_SECONDS` | `3600` | 60–86,400 seconds |
| `RAG_OIDC_JWKS_CACHE_SECONDS` | `300` | 30–86,400 seconds |
| `RAG_OIDC_JWKS_TIMEOUT_SECONDS` | `5` | 1–30 seconds |

Claim names refer to top-level JSON members. Nested claim paths are intentionally unsupported to keep mapping explicit.

## Normalized identity

A validated token is converted into the same internal identity used by retrieval and workflow authorization:

```json
{
  "iss": "https://idp.example.internal/tenant/v2.0",
  "sub": "enterprise-user-id",
  "aud": "api://yield-copilot",
  "iat": 1785373200,
  "exp": 1785376800,
  "groups": ["enterprise-group-pe"],
  "line_ids": ["LINE-02"],
  "station_ids": ["ST-04"],
  "permissions": []
}
```

The application role is never trusted directly from a `role` token claim. It is derived only through `RAG_OIDC_GROUP_ROLE_MAP`:

- no mapped role: reject;
- more than one distinct mapped role: reject;
- several groups mapping to the same single role: accept that role;
- unknown groups: ignore;
- permissions outside the configured allowlist: drop;
- malformed scope arrays: reject.

This avoids silently choosing the most privileged role when group memberships conflict. The identity's line and station scopes then flow into the existing pre-retrieval ABAC filters and direct-resource checks.

`investigations:read:all` enables cross-owner investigation audit. `sources:monitor` independently enables the redacted source-health and quarantine-list endpoints. `sources:quarantine:manage` permits one-time `RETRY` or `REJECT` disposition of a quarantine event but does not import or modify an artifact. None is inferred from an application role; each requires an allowlisted entitlement claim.

## JWT and JWKS validation

The validator intentionally supports only `RS256` in this version:

- the JWT header permits only `alg`, `kid`, and optional `typ`;
- `alg` must be exactly `RS256`; `none`, HMAC algorithms, and algorithm switching are rejected;
- token-provided `jku`, `jwk`, `x5u`, or other key-location controls are rejected;
- `typ`, when present, is `JWT` or `at+jwt`;
- `iss`, `sub`, `aud`, `iat`, and `exp` are required;
- `nbf`, expiry, clock skew, maximum lifetime, multi-audience `azp`, and signature are checked;
- JSON objects with duplicate member names are rejected;
- the JWK `kid` is matched only against the configured JWKS;
- JWKS fetches use HTTPS, have size and timeout limits, and do not follow redirects;
- JWKS responses containing private RSA parameters, duplicate `kid` values, weak RSA keys, or no usable signing keys are rejected;
- a cache miss refreshes the JWKS once, supporting normal signing-key rotation.

The implementation follows the validation boundaries in [RFC 7519](https://www.rfc-editor.org/rfc/rfc7519), the JWK representation in [RFC 7517](https://www.rfc-editor.org/rfc/rfc7517), and the algorithm-confusion guidance in [RFC 8725](https://www.rfc-editor.org/rfc/rfc8725). The deployment owner must still confirm its provider's access-token profile and claims with the enterprise identity team.

## Failure and operations behavior

- Missing or invalid tokens return `401` with a generic error; signing material and claims are not logged in the response.
- JWKS is fetched lazily on the first token and then cached. If the cache expires and the IdP is unavailable, authentication fails closed.
- An unknown `kid` causes one immediate refresh. If it remains unknown, the token is rejected.
- Configuration errors prevent server startup.
- The `/api/dev/token` endpoint remains unavailable unless `--dev-auth` is explicitly used on a loopback bind.

Production monitoring should alert separately on configuration failures, JWKS refresh failures, unknown-key spikes, invalid issuer/audience rates, and group-mapping denials without recording raw tokens.

## Remaining enterprise work

Code support does not complete an enterprise SSO integration. Before a controlled pilot, the organization still needs to provide and approve:

- the real issuer, API audience, JWKS endpoint, and token profile;
- group ownership and group-to-role mapping, including separation-of-duties review;
- authoritative line/station entitlement claims and lifecycle process;
- identity-aware proxy or API client configuration;
- signing-key rotation exercise, IdP outage behavior, revocation/logout expectations, and audit retention;
- security review of the standard-library RS256 implementation or replacement with an enterprise-approved JOSE library.

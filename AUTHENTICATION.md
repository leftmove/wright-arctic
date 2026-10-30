# Backend authentication

Arctic verifies the access JWT already obtained by Pacific's `wright-auth`.
It does not issue credentials or accept passwords, authorization codes, refresh
tokens, API keys, cookies, query-string tokens, or `X-Account-Id` as identity.
All image operations require `Authorization: Bearer <access JWT>` over HTTPS.
TLS termination and access-log redaction belong to the deployment; do not log
Authorization headers or credential-bearing image URLs.

## Trust configuration

Set these using the existing `WRIGHT_ARCTIC_` environment, dotenv or mounted-file
configuration. See `.env.example` for a managed Convex example.

- `AUTH_ISSUER`: the exact access JWT `iss`, including any trailing slash.
- `AUTH_JWKS_URL`: the provider's public JWKS endpoint. It is configured explicitly;
  token-provided key URLs and discovery URLs are never followed.
- `AUTH_AUDIENCE`: `convex` by default, matching Pacific's current backend tokens.
- `AUTH_ALGORITHM`: `RS256` by default; `ES256` must be selected explicitly.
- `AUTH_MAX_TOKEN_LIFETIME_SECONDS`: 3600 by default; may be reduced to 60–3600.

The issuer and JWKS URL must be configured together and both must use HTTPS.
Use the provider configuration for the same environment as Pacific. The browser
login origin (`WRIGHT_SITE_URL`) can differ from the access JWT issuer. Arctic
needs public verification keys only; never install the provider's signing key.

With neither trust setting configured, public health endpoints still work and
image routes remain closed. Partial or insecure configuration fails validation.
Configure HTTPS public key hosting for local integration as well.

## Verification and key rotation

Every request verifies its signature, exact issuer, audience, required `sub`,
`iat` and `exp`, and `nbf` when present. Claims have strict types. Expiry has no
clock-skew grace period; keep server clocks synchronized. Lifetime cannot exceed
the configured maximum. Subjects are opaque strings, matching the native app,
with a maximum of 256 characters.

Public keys are cached for five minutes per worker. Unknown key IDs can trigger
a refresh, limited to once per 30 seconds; concurrent fetches share a lock.
Fetches have a five-second total deadline, a 64 KiB decoded-body limit and a
32-key limit. Redirects and environment proxies are disabled. RSA keys must be
2048–8192 bits; ES256 requires P-256. Private and duplicate signing keys are
rejected. Rotate with overlapping keys, publish new keys before issuing tokens,
and keep previous keys published until their tokens expire. Removing a key
invalidates it after the next successful refresh, at most five minutes later.
Expired key caches are never used during an outage.

JWT verification is stateless. Revoking a device session prevents renewal at the
existing identity service; an already issued access token can remain valid until
`exp`. Choose short access lifetimes at the issuer. Immediate session revocation
would require an issuer-supported online session check; Arctic does not pretend
that a signature check provides one.

## Native requests and recovery

Use `AuthManager::get_valid_access_token(false)` and send the result using
`reqwest::RequestBuilder::bearer_auth`. No Pacific source changes are required
to produce credentials Arctic accepts. Arctic has no separate login to show.

A 401 has a Bearer `WWW-Authenticate` challenge and a stable error code:
`authentication_required` for missing credentials or `invalid_token` for invalid
or expired credentials. On `invalid_token`, renew through the existing auth
manager and recreate the request once. Preserve the originating account and
auth revision, following `wright-backend`'s existing checks, so account changes
cannot move an in-flight upload into another account.

A 503 `authentication_unavailable` includes `Retry-After: 30`. Retry later without
discarding the user's session. Storage failures retain their existing codes.
Protected responses use `Cache-Control: private, no-store`. Uploads authenticate
before body buffering and multipart parsing, including when Arctic is mounted
under a path prefix such as `/api`.

## Image ownership and delivery

The verified `(issuer, subject)` pair defines ownership for create, list, read
and delete. A full SHA-256 digest of an unambiguous JSON pair produces the stable,
path-safe object prefix `accounts/<digest>/images/`. Refreshed tokens and other
devices for that account share it; different issuers or subjects do not.

Old UUID prefixes came from unauthenticated caller input. They are deliberately
not adopted by verified accounts. Any needed data migration requires an
independently verified ownership mapping; guessing would grant unauthorized
access to existing objects.

Image API authorization controls metadata and URL issuance. The default delivery
mode uses expiring S3 URLs: possession of such a URL grants read access until it
expires. Keep the bucket private. The existing opt-in `STORAGE_PUBLIC_BASE_URL`
intentionally makes image bytes public; leave it unset for private delivery.

## Checks

`flyer run wright-arctic -- make test` runs the security regression suite with
real cryptographic signatures and mocked network/storage. It includes a real
image recompression/upload path. `flyer check wright-arctic` also runs the suite
alongside the established lint, typing and Rust checks. The suite does not
contact a live identity service or object bucket.

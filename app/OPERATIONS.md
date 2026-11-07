# Supabase account and image authority

Configure one issuer, its exact `/.well-known/jwks.json` URL and a publishable
key together. The production issuer is
`https://jhacsyfrssyqukdqwxgc.supabase.co/auth/v1`, audience `authenticated`,
algorithm `ES256`, maximum signed lifetime 900 seconds. These are public
configuration values, not service credentials. No old issuer fallback exists.

Signature/issuer/audience/lifetime validation precedes a bearer-authenticated
`POST /rest/v1/rpc/account_access`. The exact user token is forwarded with a
publishable key; there is no service role or caller-selected subject. The SQL
policy checks current managed session, verified email and approved AMR, and
requires provider aal2/TOTP plus the current verified factor for password and
primary-passkey sessions. Recovery/confirmation/first-factor sessions cannot
access images. Policy failures and network outages fail closed. JWKS caching
never substitutes for the current policy check.

The policy also checks provider-owned refresh-family revocation: legacy refresh
families require an unrevoked row; counter-based sessions require their current
managed session metadata. This closes the legacy-provider case where reuse
revokes refresh tokens while leaving the session row.

The owner approved Supabase's default session lifetime after its Free plan
rejected configurable time limits. There is no fixed 30-day idle or 90-day
absolute limit. Refresh rotation/reuse detection remains provider-owned.

Objects stay in the existing S3-compatible bucket. Namespace identity remains
verified issuer plus subject, so Supabase creates fresh namespaces without
copying objects. Canonical JXL, variants, source hashes, quotas and immutable
UUIDs retain their existing rules. `/account/portrait` selects an image already
owned by the caller. Its small private S3 selection object is separate from
immutable image objects and SQL profiles. Conditional automatic selection cannot
overwrite an explicit selection. Expired delivery URLs are renewed by reads.

Atlantic's optional provider-portrait importer uses the caller's bearer token
for every Arctic operation. It downloads only bounded HTTPS Google-hosted
identity pictures, without credentials or redirects. There is no internal
signing/impersonation endpoint. Identity rendering does not wait for portraits.

All operations use full Flyer selectors. Checks: `flyer run wright-arctic-one --
make check`; build: `flyer build wright-arctic-one`. Existing deployment is the
Vercel FastAPI project `wright-arctic`, ID
`prj_scyAZWI4j1jOxRcCWlF1THEP6nAP`, at https://wright-arctic.vercel.app.
Keep object-storage variables unchanged when replacing public Auth settings.
Deployment receipts and acceptance gates belong to Atlantic's
`docs/PHASE_TWO_IMPLEMENTATION.md`.

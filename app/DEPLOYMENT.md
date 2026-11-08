# Arctic deployment

The API runs as one Python Worker, `wright-arctic`, with Supabase PostgreSQL
through `HYPERDRIVE` and the existing `wright-images` R2 bucket through `IMAGES`.
FastAPI routes, authentication policy, object namespaces and the native CRDT
protocol are shared with the portable ASGI application. Provider bindings stay
in `app/worker.py`, `app/worker_storage.py`, `app/worker_crypto.py` and
`wrangler.toml`.

## Build and deploy

Use the full Flyer selector on this machine:

```sh
flyer run wright-arctic-one -- make worker-setup
flyer run wright-arctic-one -- make check
flyer run wright-arctic-one -- make worker-build
flyer run wright-arctic-one -- make worker-deploy
```

`worker-setup` installs the pinned Node and Pyodide/Emscripten tools into the
checkout's ignored cache and adds the engine's Emscripten Rust target.
`worker-build` cross-compiles the engine into a CPython 3.14 PyEmscripten wheel,
resolves dependencies with Pywrangler and the Pyodide index, and validates the
bundle with pinned Wrangler. `app/deployment/pylock.toml` records the target
packages separately from the native `uv.lock`. Review both when upgrading.

Generated deployment files live in `dist/worker`; only its `src` directory is
application source. Virtual environments, local credentials, tests, build
tooling and the portable S3 SDK are excluded from the runtime package. The
portable PostgreSQL and OpenSSL backends are also omitted. Wrangler excludes
unused imagecodecs extensions and NumPy build utilities. The build rejects
source packages over 64 MiB before upload; Cloudflare additionally validates
the generated Python snapshot and total startup memory. Linux wheels cannot
be used in Workers.

Set `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` in the environment or
ignored `.env.cloudflare`. The token needs permission to deploy Workers in
this account; it does not become a runtime credential. Existing Hyperdrive
and R2 bindings do not require sharing their management credentials with the
app. Expiring the deployment token does not stop a deployed Worker.

Runtime settings come from the usual `WRIGHT_ARCTIC_*` names in `.env` or
the process environment. Deployment uploads these as Worker secrets while
explicitly excluding both document database URLs and the native pool size.
The privileged setup URL never leaves the local schema administration path.
CI retains secrets already installed on the Worker when no local settings
are supplied.

Deployment currently runs locally because GitHub Actions minutes are exhausted.
The optional manual GitHub deployment workflow uses `CLOUDFLARE_ACCOUNT_ID`,
`CLOUDFLARE_API_TOKEN`, and `WRIGHT_NATIVE_READ_TOKEN` Actions secrets. The last
is a fine-grained token with Contents read access to the private
`leftmove/wright-pacific` repository. GitHub's default token cannot read that
separate private dependency. Local builds use the machine's existing Git
credentials instead. The engine and both workflows pin the same immutable
upstream commit; no shared-library source is vendored into Arctic.

## Database and storage

Hyperdrive must point to Supabase's direct database endpoint, using the
restricted `arctic_api` runtime role. Hyperdrive owns origin pooling; each
request lazily opens and finally closes its own asyncpg client. asyncpg's
client statement cache is disabled; Hyperdrive owns the origin connection
lifetime. Short transactions,
row locks, statement timeouts, schema/role checks and authorization checks
remain intact. The Hyperdrive connection string supplied by the binding is
used unchanged, including its transport settings.

**Disable Hyperdrive query caching for this connection.** Document permissions,
revocations and reads after edits require fresh results. Writes do not
invalidate Hyperdrive's query cache. Connection pooling and accelerated setup
still apply with query caching disabled. See [Cloudflare's freshness guidance](https://developers.cloudflare.com/hyperdrive/concepts/query-caching/#read-after-write-behavior).

Supabase still issues and verifies identities. Arctic uses the host's signature
verifier (Web Crypto in Workers, PyJWT/cryptography in native ASGI), backed by
a bounded public-key cache, then checks `account_access` using the
caller's exact token on every authenticated request. Only public signing keys
are shared across requests; HTTP clients and SQL connections are not. PyJWT
claim validation and the application authentication rules apply on both hosts. No
Supabase service-role key is needed.

R2 bindings perform object operations directly. Existing S3 credentials remain
necessary to sign private download URLs, preserving client behavior and object
keys. New images use modular lossless JPEG XL, with cached WebP browser delivery.
This avoids the lossy JPEG XL encoder's larger working set; canonical objects
remain bounded at 1 MiB. Existing JPEG XL objects remain compatible.
The current Pyodide Pillow wheel lacks AVIF encoding: existing AVIF variants
can be served, while generating a new one returns 415. ICC conversion uses
the WASM-compatible imagecodecs LittleCMS implementation and preserves alpha.

## Runtime bounds and verification

Workers Free supplies 10 ms CPU per request and 128 MiB memory. Network waits
do not count as CPU. These limits still apply to Python and native codecs;
size limits alone are not proof that every input fits the CPU allowance.
Measure production CPU outcomes before increasing service caps.

The initial live verification on 2026-11-08 passed liveness, Rust execution,
Hyperdrive schema/privilege checks, R2 access, and missing/forged-token rejection.
Private R2 URL signatures were also accepted. The native regression suite ran
71 tests with one optional database test skipped; local Workers exercised CRDT
reconstruction, codecs, conditional object writes and both JWT algorithms.
The Worker asyncpg adapter also passed create, idempotent retry, content read,
rename, trash, restore and history operations against Supabase inside a rolled
back transaction. No verification document was persisted. This checks the
database adapter separately from the signed-in HTTP flow.

Live invocation logs reported CPU times above the published allowance, including
on small endpoints, while every observed request completed successfully. Monitor
actual invocation failures, latency and resource usage under ordinary workloads;
these samples alone do not establish a need to change languages or plans.
Immutable settings and compiled validators are reused, unused lifespan work is
skipped, and codec initialization is captured in the deployment snapshot.
Document reconstruction remains in the existing Rust engine. Optimize measured
compute bottlenecks there through the Python bindings when useful. This
deployment does not enable Workers Paid.

Current bounds are 2 MiB HTTP bodies, 512 KiB image uploads, 262,144 decoded
pixels, a 512-pixel output edge, and 10 images per account. JPEG draft decoding
can reduce larger JPEGs before allocating pixels. Document state is capped at
512 KiB, individual updates at 128 KiB, batches at 256 KiB, and native replay
at 8 MiB. Automatic checkpoints start at 2 MiB of replay or 128 events.
Pre-existing images over today's decode budget can retain their already
stored representations; generating a new representation still obeys the caps.

Use `/health` for liveness, `/hello` to exercise the compiled Rust wheel, and
`/ready` to check runtime database privileges/schema, auth configuration, and
bucket access without reading account objects. A successful readiness check
does not replace a signed-in document/image smoke test.

```sh
flyer run wright-arctic-one -- make worker-dev
```

Local Worker development loads `.env` settings into a temporary ignored binding
file, connects to the configured development database, and uses R2's remote
binding so signed URLs address the same stored objects. Supply development
resources deliberately: operations reach the configured database and bucket.
Ordinary native ASGI development remains available through `make dev`.

The Worker URL is `https://wright-arctic.anonyo.workers.dev`. Point consumers'
API base URL configuration there, or attach a custom domain in Cloudflare.
The deployment account token is separate from runtime settings and must be
renewed before future deployments after its expiry.

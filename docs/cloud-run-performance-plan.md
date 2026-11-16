# Arctic: Cloud Run migration and performance plan

Date: November 16, 2026. Status: proposed implementation plan; no runtime changes made by this document.

## 1. Recommended direction

Run Arctic as one native, asynchronous API service on Cloud Run, close to Supabase Postgres. Keep identity, metadata/grants, sync, and images as modules inside that service. Run scheduled cleanup in a separately authenticated maintenance service using the same domain code. Keep documents and images in private R2 buckets.

Optimize in this order: remove the Worker execution layers; eliminate redundant remote calls; shorten transactions and lock lifetimes; reuse connections; bound concurrency; then optimize measured application CPU. A distributed set of domain microservices would introduce network calls into precisely the operations we need to shorten.

Start with native Python/FastAPI, preserving existing service ownership and API contracts. Make database and storage operations genuinely asynchronous. Do one bounded comparison of native async Psycopg 3 and asyncpg against representative operations; adopt one driver. Psycopg's native implementation is a credible option on Linux, and the Worker measurements do not establish a native-driver winner. A standalone Rust implementation is a later, measured option, not an assumed prerequisite for low latency.

## 2. Decisions, constraints, and scope

The full target architecture at `/localscratch/workspaces/wright-target-architecture.md` was reread for this plan. The user's Cloud Run decision supersedes its Worker hosting and Worker Free CPU constraint. Storage remains independent of Google.

Preserve all of the following:

- Complete opaque checkpoint files in R2; metadata, grants, and pointers in Postgres. Arctic never reads or processes checkpoint bodies.
- UUIDv7 identifiers, existing bucket/root/key formats, and the private bucket model.
- Zero R2 object listings, conditional R2 writes, multipart uploads, or parallel uploads. Bounded concurrent HEAD/DELETE operations do not introduce concurrent uploads.
- Global five-minute batch admission, idempotent retries, conditional pointer publication, three-checkpoint retention, immediate attempted deletion of refused/retired objects, 24-hour orphan timeout, and three-day trash retention.
- AVIF-only images, 2,000,000-byte images, 100,000,000-byte checkpoints, quota accounting, and authorization of image references.
- Current authentication strength, fresh policy and grant checks, least-privilege runtime credentials, and public avatar access.
- Existing Pacific contracts during migration. Pacific, WebRTC, signaling, session tickets, and TURN remain outside this work.

Proposed contract change: signed direct image downloads. This differs from the spec's authenticated byte proxy and requires an explicit protocol decision and client compatibility validation. Build the Cloud Run migration without depending on it; keep the proxy until clients can consume the new delivery mechanism. Do not silently turn private image objects public.

This migration must preserve the new Phase 1 catalog and objects. Do not rerun the destructive fresh-catalog setup against an active database. New performance-related schema changes must be additive/versioned. This does not create a legacy document-content migration project.

## 3. What is known, and what still needs measurement

| Finding | Evidence | Implication |
| --- | --- | --- |
| Worker Python/database integration is expensive | Earlier controlled live tests: approximately 40 ms CPU for Python versus 8 ms for JavaScript on a small guarded database request; roughly 104.5 versus 20 ms for a sixteen-query adapter workload | Native hosting removes an established source of overhead; these are not Cloud Run endpoint predictions |
| Trivial SQL itself was fast | Earlier Supabase samples: simple SELECT median planning 0.023 ms, execution 0.017 ms; role-check planning 0.533 ms, execution 0.189 ms | Reduce exchanges and client work before treating Postgres compute as the primary problem |
| Authentication includes an extra remote dependency | `app/authentication/service.py` calls Supabase `account_access` for every authenticated request after JWT verification | Potentially substantial serialized latency; end-to-end impact not yet measured |
| Identity resolution is a separate transaction | `app/dependencies.py` calls `IdentityService.resolve` before invoking the domain service | A document read pays for identity lookup and operation setup separately |
| Native services currently offload blocking I/O | `app/execution.py` uses `asyncio.to_thread`; native persistence uses synchronous Psycopg and boto3 | Thread scheduling, cancellation, and concurrency can be improved; current code is not blocking the ASGI event loop directly |
| Image confirmation holds a broad lock during R2 I/O | `ImageService.confirm` locks the account and reservation before HEAD and the AVIF prefix GET | Slow storage can serialize unrelated requests by the same account |
| Publication and cleanup have serial storage loops | Sync verification loops over HEAD calls; cleanup performs up to sixteen sequential deletes | Latency accumulates across objects even though uploads remain direct |
| The schema already has useful indexes | Catalog contains grant, reverse image-reference, library, quota, retention, orphan, and pending indexes | Audit actual plans and workload shapes rather than adding duplicate indexes |

The earlier CPU experiments do not rank full authenticated production endpoints. Authentication latency, connection waits, R2 latency, and contention must be measured on Cloud Run. Never interpret elapsed coroutine or I/O time as CPU time, or sum process CPU deltas across concurrent requests as if they belonged to individual requests.

## 4. Define and measure success

Use provisional acceptance targets, then validate them in a region close to the database. These are goals, not existing measurements or provider guarantees.

| Workload | Initial target | Measurement boundary |
| --- | --- | --- |
| Warm unchanged document pointer | p95 <= 100 ms; stretch <= 50 ms | Cloud Run request latency, including authentication and database work |
| Warm metadata/library page, 50 rows | p95 <= 150 ms | Same boundary; bounded result size |
| Eight-document batch reservation | p95 <= 200 ms | Excludes client file creation/upload |
| Eight-document publication | p95 <= 500 ms under healthy R2/DB conditions | Includes required verification, commit, and immediate delete attempts |
| Image read permission, if adopted | p95 <= 100 ms; zero image bytes handled by Arctic | Includes fresh authorization and signing |
| Cold first successful pointer request | Aim p95 <= 2 seconds; stretch <= 1 second | Client-observed cold request, separately recording container initialization |
| Database pool acquisition | p95 <= 5 ms at supported steady load | Report saturation separately from SQL execution |

Do not let averages hide p99 latency, cold starts, timeouts, or failed requests. Track CPU-seconds and GiB-seconds per 1,000 operations, requests per vCPU, R2 operations per action, database round trips, transaction duration, lock wait, pool wait, and external bytes. A throughput improvement that makes tail latency or cost materially worse does not pass automatically.

Instrument wall-clock spans for JWT/key discovery, policy, identity, connection acquisition, SQL, storage verification, publication commit, deletion, and serialization. Add event-loop lag and native CPU profiling during representative loads. Do not log bearer tokens, signed URL query strings, secrets, document bodies, or high-cardinality identifiers as metric labels. Sample ordinary traces and retain useful slow/error traces with bounded logging cost.

Benchmark cold and warm traffic separately, using fixed offered arrival rates as well as concurrency sweeps. Include an eight-document batch, idempotent retry, conflicting publishers, a large staged image manifest, 50-row library pages at 1k/10k/100k-document fixture sizes, and 2 MB image delivery. Use thousands of requests for latency distributions and record failures; tiny samples are diagnostic only. Separate first-account provisioning from established accounts.

## 5. Cloud Run process and connection architecture

### Hosting

- Build a pinned multi-stage Linux image with locked Python dependencies and the existing Rust extension compiled in the build stage. No package installation, compilation, or schema provisioning during startup.
- Start one ASGI process per 1-vCPU instance initially. Start experiments at 512 MiB, request-based billing, concurrency 16, and zero minimum instances. Compare concurrency 8/16/32 and memory 512 MiB/1 GiB; select from measured CPU, memory, queueing, and cost.
- Select the region by measuring Cloud Run-to-Supabase latency and validating R2 metadata latency. Prioritize API-to-database proximity because a request can involve multiple database exchanges. Global API replicas talking to one distant writer are not an automatic improvement.
- Initialize one process-owned database pool, one reusable HTTP transport for storage, one auth HTTP client, and bounded immutable signing/key caches. Requests borrow resources; they do not create and close sockets routinely.
- Configure max instances from database connection capacity, not an arbitrary traffic goal. Google notes that configured maxima can be exceeded temporarily; leave capacity for rollout overlap and scaling overshoot.

### Connection routing and pool budget

Prefer a direct Supabase connection for persistent sessions where Cloud Run connectivity permits. Supabase direct endpoints can require IPv6; verify connectivity before choosing them. Use Supavisor session mode as the supported IPv4 alternative. Remove Hyperdrive from the Cloud Run path. Do not stack poolers without a demonstrated reason.

Start with a pool minimum of one and maximum of four per API process. The initial four is an experiment, not a universal optimum. Give maintenance its own small pool and reserved connection allocation. Size with:

`possible live API instances × processes per instance × pool maximum + maintenance + other consumers + rollout/overshoot reserve <= database connection budget`

For example, three instances with one process and four connections consume up to twelve connections before reserve. Actual allowed instance counts depend on the project's Supabase capacity. Keep connection acquisition queues bounded and short. A longer queue does not increase database throughput.

Set stable role/session isolation and timeout defaults at connection creation or in role configuration. Keep operation-specific overrides transaction-local. Verify the private runtime role when a physical connection is created and perform full catalog verification during deployment. Neither full catalog checks nor role catalog queries belong on every ordinary request once connection identity is controlled.

On direct/session connections, enable bounded prepared-statement caching after compatibility tests; native Psycopg currently sets `prepare_threshold=None`. If transaction pooling is selected later, revisit prepared statements and all assumptions about session state. Cache codecs/type metadata on persistent connections rather than rediscovering them on each request. [Supabase connectivity](https://supabase.com/docs/guides/database/connecting-to-postgres), [pooling](https://supabase.com/docs/guides/database/connecting-to-postgres/pooling-and-limits), [Psycopg pools](https://www.psycopg.org/psycopg3/docs/advanced/pool.html), [prepared statements](https://www.psycopg.org/psycopg3/docs/advanced/prepare.html).

## 6. Make request execution asynchronous end to end

Introduce typed asynchronous session/unit-of-work interfaces and convert the domain services to await database/storage operations directly. Preserve module ownership and thin routers. Remove the Worker-specific synchronous-to-async bridge from the final Cloud Run production path and remove native thread hops for ordinary database and HTTP work.

Use native async Psycopg 3 as the default candidate because it preserves the native SQL parameter contract and uses a mature native client implementation. Compare it once against native asyncpg on pointer reads, eight-document reservation/publication, UUID arrays, and cancellation. Choose based on complete operation CPU, latency, and correctness, not a one-query library benchmark. Remove the losing production adapter after cutover.

For R2, use a maintained async S3 transport with reusable connections, or a narrow async HTTP adapter for the existing HEAD, bounded range GET, DELETE, and compatibility streaming operations. Run an explicit comparison of startup cost and correctness. Reuse established signing primitives; do not casually create a second SigV4 implementation. Match existing error mapping, timeouts, signed sizes/hashes, TLS validation, and streaming cleanup. No upload API should be added to the server transport.

Ordinary network waits must be awaitable. Bound any remaining blocking library work in an explicit executor. Async improves concurrency and cancellation; it does not by itself make SQL run faster. [FastAPI concurrency guidance](https://fastapi.tiangolo.com/async/).

## 7. Authentication and identity: remove a serialized HTTP trip safely

Keep local JWT signature and claim verification, strict issuer/audience/algorithm checks, MFA requirements, key rotation behavior, and the bounded JWKS cache. Current code already shares/throttles JWKS refreshes; preserve that rather than rebuilding it.

The preferred request path is:

1. Verify the supplied JWT locally using cached public keys.
2. Through a narrow private database operation, evaluate the same live session/account/MFA policy currently enforced by `account_access` and resolve the internal UUIDv7 account.
3. Execute document authorization and the requested read/mutation in that operation's transaction where appropriate.

First retrieve and review the authoritative definition of `account_access`. Its implementation is not in this checkout. Prove semantic equivalence for session revocation, account/email state, factor association, and token claims before removing the existing HTTP call. Do not replace it with only a JWT expiration check.

If policy access requires elevated privileges, expose only a tightly scoped function with a fixed search path and restricted EXECUTE grants. The application retains a non-owner, non-superuser, non-bypass runtime role with no broad access to Supabase Auth tables. Only verified request claims reach this function. Do not invent client-supplied claim delegation, use a service-role key, or weaken checks to make the benchmark pass. Supabase documents correlating `session_id` with `auth.sessions`; that alone is not proof that the existing custom policy is reproduced. [Supabase sessions](https://supabase.com/docs/guides/auth/sessions).

If equivalent private evaluation cannot be implemented, retain the policy HTTP call with a shared connection pool and bounded timeout. Record its latency as a remaining architectural cost. Do not hide that cost with an unapproved cross-request positive authorization cache.

Pass a typed verified actor to the operation instead of unconditionally provisioning/resolving an account in a dependency that starts another transaction. First-use account and initial-space creation remain atomic and race-safe, but run only when the mapping is missing. Never cache mutable `last_sync`, avatar selection, or grant state as immutable identity.

## 8. Database operations: reduce exchanges while preserving concurrency semantics

The intended established-account operation budgets are below. These count coarse database calls, not SQL statements inside them or explicit transaction protocol messages. Measure those separately. The policy consolidation in section 7 is a prerequisite for eliminating the extra HTTP call.

| Operation | Target database calls | R2 work |
| --- | --- | --- |
| Open unchanged pointer | One authorized read call | None |
| Open changed pointer | One authorized read call | Local URL signing; client performs one GET |
| Reserve up to eight checkpoints | One atomic reservation call | Local URL signing; client uploads each file sequentially |
| Publish eight new checkpoints | Preflight, atomic publication, and deletion-result finalization calls | At most eight required HEADs, bounded concurrency; exact-key retired/refused deletes |
| Retry committed publish | Result lookup and any outstanding deletion bookkeeping | No repeated HEADs for already committed items |
| Confirm a new image | Preflight and atomic finalization, plus deletion bookkeeping if invalid | One HEAD and one bounded prefix GET; invalid object deletion |
| Issue image download permission, if adopted | One authorized read call | Local signing; client performs one GET |

First-use provisioning, large staged manifests, and failure recovery can add bounded work. The goal is to eliminate chatty coordination, not conceal required statements in a benchmark count.

### Read path

For document open, combine live actor policy, account lookup, grant/active-document check, and pointer projection into one coarse database call. Return only the fields required. Sign a GET permission locally only when the client's known revision differs. Unchanged opens cause zero R2 operations, including HEAD.

Single-statement read operations can use autocommit with connection-level defaults rather than paying for explicit BEGIN/configuration/COMMIT exchanges. Keep explicit transactions for multi-statement invariants and all mutations. Give public avatar pointer lookup a small public read path without invoking user authentication.

### Write paths

Provide explicit transaction operations such as `reserve_batch`, `publish_verified_batch`, `confirm_verified_image`, and `change_grant`. Implement them with typed repository methods and, for operations dominated by network round trips, small private SQL/PLpgSQL functions. Keep functions versioned beside the catalog, with explicit parameters, results, and errors. Avoid one giant generic RPC dispatcher.

Goal: one coarse mutation call per transaction, with any BEGIN/COMMIT protocol overhead reported separately. Server-side statements may still be numerous; the benefit is removing client/server exchanges. Do not claim that packing SQL into one call removes its database work.

- Batch reservation: check policy, resolve actor, atomically admit or reuse the batch, verify document edit access in bulk, and insert missing slots in sets. Preserve the original `last_sync` on retries. Retain the current eight-document API bound until measurements justify a contract change.
- Publication: do an authorized preflight; verify only unresolved issued objects outside the mutation transaction; then lock and re-read current attempts, grants, and pointers before conditional updates. Process eight items using set-based reads/bookkeeping. Keep each pointer's base-revision compare, per-item result, and atomic batch result commit.
- Retention: compute retired revisions inside the same transaction as publication; record exact pending keys before issuing deletes. Call R2 only after commit. Retry of a committed publish must recover the original result and drain its pending deletion work without repeating successful upload verification.
- Rename and similar operations: return updated rows directly where safe rather than rereading the full document after UPDATE.

### Locks and snapshots

Keep READ COMMITTED and deterministic lock ordering. In mutation code, acquire the common document serialization lock, then evaluate grants using a fresh statement snapshot after any wait. A single CTE containing both a lock and grant lookup is not automatically equivalent to the current safe two-statement sequence. If moved into a database function, ensure its volatility and internal statements preserve fresh-snapshot behavior and prove it with independent database sessions. Do the same for publish versus orphan cleanup.

Keep account locking for atomic quota/batch admission, but never hold it across a network wait. R2 verification belongs outside locked database transactions. [Postgres isolation semantics](https://www.postgresql.org/docs/current/transaction-iso.html).

## 9. Image and checkpoint storage paths

### Verification and deletion

- Verify independent checkpoint HEAD requests concurrently with a small per-request limit, initially four, plus a process-wide limit. Leave uploads sequential and single-object.
- Keep checkpoint verification to HEAD metadata: exact size, type, signed hash metadata, and reservation ownership. Never fetch/decompress a checkpoint to validate it.
- Refactor image confirmation into authorized preflight, external HEAD plus bounded AVIF-header inspection, and a short locked finalization transaction. Re-read reservation state, expiration, ownership, quotas/reservation accounting, and competing confirmation/cleanup results before committing. Already confirmed retries need no R2 verification.
- Retain the existing AVIF file-type validation. MIME alone would accept a renamed GIF/JPEG XL. The current HEAD plus at-most-512-byte range GET is a correctness feature, not an accidental redundant download. Validate that range responses are bounded even if the upstream ignores Range.
- Retain signed exact upload length, media type, and payload hash; preserve replay/tombstone handling. Presigned URLs are reusable until expiry, not literally single-use credentials.
- Execute exact-key deletes after the database records pending state. Attempt required refused/retired deletion immediately within the request deadline; persist failures for maintenance. Do not silently defer every retirement to cron to produce a faster response.

### Direct image delivery proposal

Add an authorized read-capability operation that checks uploader ownership or current access through a referencing active document, then returns a short-lived one-object GET URL. Initial TTL candidate: 60 seconds, tuned for retries and clock skew. It performs no R2 request while signing. Existing grants continue to be checked on every issuance; an issued URL remains usable until expiry.

Prefer an additive capability response over changing an established JSON contract. Retain `/images/{id}/content` until native redirect/capability behavior is tested in the separate Pacific work. If redirects are used for compatibility, verify that bearer headers are not forwarded to R2 across origins. Keep capability responses private/no-store and URLs out of logs.

Public avatars may use an unauthenticated pointer/redirect endpoint to ordinary image objects, with cache lifetime bounded by URL validity and desired avatar-update visibility. Buckets remain private. Never advertise custom-domain/CDN caching of S3 presigned URLs as automatic; design and validate that separately if justified. Document-image URL caching must not accidentally create public access.

This proposal eliminates Cloud Run image-byte egress and per-chunk Python scheduling once clients use it; it does not eliminate R2 GET charges or make downloads independent of file size and user bandwidth. [R2 presigned URLs](https://developers.cloudflare.com/r2/api/s3/presigned-urls/).

## 10. Metadata, indexes, and caching

Use `EXPLAIN (ANALYZE, BUFFERS)` and query statistics on realistic fixtures. Examine permission lookup, account-to-document listing, reverse image authorization, daily/stored quota counts, pending cleanup, and retention. Indexes already exist for most of these. Add or alter an index only when its plan/read benefit outweighs update/storage cost. UUIDv7 improves locality but is not a guarantee of zero index fragmentation.

The document listing orders by `updated_at` across grants, while the existing library index starts with `space`; that mismatch deserves measurement for large shared libraries. Do not assume adding another index will remove the cross-table ordering cost.

Add cursor/keyset pagination for large libraries and image lists using stable timestamp-plus-ID ordering. Retain current offset contracts during transition. Sort directions, cursor comparison, and tie-breaking must agree. Validate cursors and exclude newly inaccessible rows on every page. A future delta metadata feed needs durable deletion/revocation semantics; an `updated_at` cursor alone is not a reliable substitute.

Cache process configuration, clients, signing keys, JWKS within current rotation bounds, and compiled validators. Do not cache pointers, grants, live session policy, last-sync admission, or quota decisions across requests without an explicit consistency design. Keep Postgres the authority. Do not add Redis, read replicas, event buses, or a second metadata store without a measured need.

Remove repeated model validate -> dump -> revalidate chains where profiling finds them. Keep strict validation at untrusted boundaries and typed internal results. JSON format/library changes come after database/network work and must preserve schemas, UUID/date encoding, error behavior, and response validation. [Postgres EXPLAIN](https://www.postgresql.org/docs/current/using-explain.html), [Pydantic performance](https://pydantic.dev/docs/validation/2.12/concepts/performance/).

## 11. Deadlines, overload, and shutdown

Use layered deadlines rather than relying on Cloud Run's response timeout. Initial candidates: 15-second Cloud Run API timeout, 10-second application deadline, bounded subsecond pool acquisition, and operation-specific database/storage timeouts constrained by the remaining deadline. Interactive reads should fail faster than a batch publication. Benchmark and tune these values; don't apply a shorter global deadline than valid cleanup/publication work can meet.

Propagate cancellation to database and HTTP clients, roll back safely, and discard a connection if its protocol/transaction state is uncertain. A database commit can succeed while the HTTP response is lost: preserve idempotency and reconcile ambiguous results instead of blindly repeating mutations. Required pending deletes survive cancellation in Postgres.

Bound request body size, item count, SQL result size, storage fan-out, pool queues, and retries. Limit actual R2 concurrency per process as well as per request so sixteen requests cannot each launch unbounded work. Reject overload promptly with an appropriate 429/503 and retry guidance. Use jitter and a single bounded retry owner; avoid nested SDK/application retry multiplication. Do not automatically retry non-idempotent changes without a stable idempotency key.

Close clients and drain work on SIGTERM within the platform's shutdown window. Do not rely on shutdown hooks or in-process fire-and-forget tasks to complete durable maintenance. Cloud Run timeout closes the response connection without guaranteeing application work stops. [Request timeout](https://docs.cloud.google.com/run/docs/configuring/request-timeout), [container contract](https://docs.cloud.google.com/run/docs/container-contract).

## 12. Cleanup without harming interactive traffic

Use a second Cloud Run service for maintenance, built from the same image/domain modules, with a separate service account, small pool, and max instances of one initially. Invoke it every five minutes using Cloud Scheduler OIDC and Cloud Run IAM. Do not put a Scheduler-only route in an otherwise public API and assume the URL itself is private. A one-shot Cloud Run Job remains an alternative if execution duration justifies its startup/billing cost.

Reuse the Postgres pending-state machinery as the durable work queue. Claim exact-key work in short transactions with SKIP LOCKED and, where overlapping invocations can otherwise duplicate work, explicit expiring claims. Perform bounded concurrent deletes outside transactions and finalize outcomes in sets. Preserve enough tombstone lifetime to defeat late replay of previously signed uploads. Recheck current pointers and active reservations when claiming work; cleanup must never remove an active checkpoint.

Set per-run work/time budgets, fair shares for checkpoint/image/trash work, retry backoff, and backlog-age metrics. Preserve the 24-hour and three-day rules while improving backlog drain capacity. Cleanup should be safe on duplicate delivery and restart, and should not monopolize API database capacity. Do not run a second cron at the old Worker after cutover. [Authenticated scheduling](https://docs.cloud.google.com/run/docs/triggering/using-scheduler).

## 13. Startup and cost/latency modes

Measure import time, resource creation, first database connection, JWKS miss, and first successful authenticated response separately. A fast health endpoint alone is not evidence of a fast first document open.

Build dependencies ahead of time, remove Worker/Pyodide packaging from the Cloud Run image, and keep the native Rust extension prebuilt. Avoid runtime boto3 model loading if the chosen async storage transport eliminates that dependency. Warm one database connection and verify its role; run full schema verification before deployment traffic rather than on each probe. Do not bind the serving port and report readiness before essential resources are usable.

Use a cheap in-process liveness check and a bounded/cached readiness result; do not turn frequent probes into catalog queries. Keep startup failures explicit. JWT key discovery can be prefetched with a bounded timeout or allowed on first protected access; measure that choice and preserve fail-closed behavior.

Benchmark startup CPU boost against its additional allocation cost. Keep two documented configurations:

- Cost-first: zero minimum instances, request-based billing, bounded scaling, optimized cold path.
- Latency-first: one minimum instance, same correctness and pool controls; measure/pay idle cost. Cold starts still occur during scale-out, deployments, and replacement.

Keep costs visible for requests, allocated CPU/RAM while waiting on I/O, outbound database traffic, image proxy traffic until retired, builds, registry, logs, and maintenance. A spend cap can reduce exposure but is not an instantaneous free-tier cutoff; delayed enforcement/in-flight usage can produce charges. [Python startup guidance](https://docs.cloud.google.com/run/docs/tips/python), [Cloud Run concurrency](https://docs.cloud.google.com/run/docs/about-concurrency), [spend caps](https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps).

## 14. Implementation sequence and review gates

| Milestone | Concrete deliverables | Exit criteria |
| --- | --- | --- |
| 1. Native baseline and observability | Reproducible container, Cloud Run config, native ASGI deployment to an isolated revision, request spans/load harness, database connection budget | Existing Phase 1 behavior passes; cold/warm latency, CPU, SQL trips, R2 count, cost baseline recorded |
| 2. Resource ownership and async I/O | One selected async database driver, process pools, async storage transport, typed async services, deadlines/cancellation, connection-level configuration | No routine connection creation/thread hop per DB operation; bounded queues; cancellation and disconnect tests pass |
| 3. Fewer remote calls | Reviewed policy-equivalent private evaluation, actor resolution combined with operations, coarse reserve/publish/metadata transactions, prepared caching | Warm reads use one coarse DB call where feasible; no separate policy HTTP request if equivalence is proven; race/authorization tests pass |
| 4. Storage and lock efficiency | HEAD fan-out, image verification outside account lock, short finalization, immediate bounded delete attempts, separate scheduled cleanup | Same byte/format/quota guarantees; no lock held over R2 waits; publish/cleanup races pass; backlog drains within retention expectations |
| 5. Metadata/delivery improvements | Evidence-driven query/index adjustments, optional additive cursor APIs, signed image permission/redirect design | Explain plans improve at realistic sizes; current contracts remain compatible; image cutover occurs only after separate client support is verified |
| 6. Tuning and cutover | Concurrency/pool/memory/region experiments, cold-start work, canary, production switch, Worker retirement | SLO/cost targets met or remaining limits explicitly reported; no duplicate cleanup; rollback proven |

Each milestone should leave a reviewable working deployment and before/after measurements. Do not combine a hosting change, language rewrite, security-policy rewrite, and client protocol change into one release.

Keep experiments finite: define the representative operations, configuration matrix, and success threshold before each comparison. Run one baseline and one after-change evaluation per milestone; repeat only for a new failure, material variance, or unresolved regression. Do not turn the migration into open-ended framework/driver benchmarking.

### Required correctness and failure tests

Use real Postgres sessions for transaction/snapshot behavior and real R2 for final transport compatibility. Reuse existing fixtures and disposable verification principals rather than requiring a personal test account.

- Two publishers sharing one base revision: exactly one pointer wins; loser is refused and its object deletion is attempted; whole-batch result bookkeeping is atomic.
- Revoke access while a mutation waits for a lock: fresh grant state wins after the wait. Repeat for a viewer/editor transition.
- Concurrent batch starts across instances: one five-minute admission; retry does not move `last_sync`.
- Concurrent image issue/confirm/delete and quota edges: no double count, overshoot, confirmation of expired reservation, or deletion of a live reference.
- Publish versus orphan/trash cleanup: no live pointer to a deleted object; deterministic recovery after process death between database commit and R2 delete.
- JWT/session revocation, MFA/email/account state, JWKS rotation/unknown key, missing policy access, and authentication outages retain current behavior.
- Lost response after successful commit: retry returns the same outcome with no repeated successful HEAD verification.
- Boundary-size and malformed AVIF uploads; oversize/incorrect checkpoint metadata; no checkpoint-body access; no listing/conditional/multipart/parallel-upload calls.
- URL replay and expiry, new signed-image access after revocation, public avatar access, safe cross-origin redirects, no token/URL leakage in logs.
- Pool exhaustion, R2/Supabase slowdown, deadline cancellation, startup failure, SIGTERM, duplicate maintenance delivery, and rolling revision overlap.

### Cutover and rollback

Deploy by immutable image digest, using narrowly scoped runtime identities and CI workload identity rather than new long-lived cloud deployment keys. Keep all secrets outside the image and repository. Extend Flyer-supported build/deploy entrypoints before using them; if the pipeline lacks a required operation, stop for the user's confirmation rather than bypassing it.

Canary against the same metadata authority with compatible additive changes and bounded shared connection capacity. Use separate disposable objects for destructive verification. Switch a stable API hostname after compatibility checks; if existing clients use the workers.dev hostname, define a temporary forwarding/cutover strategy without modifying Pacific in this task. Never shadow production mutations by executing them twice.

Rollback selects the prior service revision and compatible schema, not a destructive catalog reset. Disable the old cleanup schedule before enabling the new scheduler; durable pending records tolerate a short maintenance gap. After the observation window, remove Worker-only builds, runtime adapters, Hyperdrive binding, deployment secrets that are no longer needed, and the old deployment job. Retain R2 credentials/buckets as required.

## 15. When a native Rust service is justified

If we still want the highest efficiency after removing network and transaction waste, benchmark one complete Rust operation against the optimized native Python operation under the same authentication semantics, connection budget, region, and load. Suggested candidate: unchanged-pointer open; add full publication before a broader rewrite decision.

Compare p95/p99 latency, CPU per operation, memory, cold-start behavior, and throughput at the same error rate. A faster hello endpoint or skipped authorization is not evidence. Use the measured CPU fraction to estimate the maximum benefit: if application CPU is 10% of elapsed time, making that CPU work five times faster reduces serial elapsed time by only about 8% when network work is unchanged.

Recommend moving the whole API to Rust only if the trial demonstrates material benefit for the remaining goal, such as substantially lower CPU cost at comparable tail latency or a meaningful tail-latency reduction. Keep the same domain boundaries and contracts; avoid permanent cross-language RPC for every query. This is a proposed architectural decision beyond the repository's current FastAPI conventions and would be reviewed explicitly. No Rust/Pacific rewrite is included automatically in the Cloud Run migration.

## 16. Definition of done

The Cloud Run service is observably faster and cheaper per equivalent operation, preserves Phase 1 authorization/storage/concurrency semantics, remains responsive under a documented offered load, and has a tested cutover/rollback path. Its latency budget names any remaining external-policy or R2 dependency instead of hiding it. Publish a short before/after scorecard and final tuned settings; retain only optimizations that produce a measurable benefit or a clear correctness/resource-management improvement.

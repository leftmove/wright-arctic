# Implementation task handoff

Requested model: GPT 6.1 Sol. Reasoning: Extra High (`gpt-6.1-sol`, `xhigh`).

The prompt below preserves the user's template. Instructions supplied for the Task section are enclosed in `<FILL_HERE>` tags.

---

This is Wright — a truly native, cross-platform document editor built with Rust. It's designed to be a great writing experience first, but can also act as a drop-in replacement for Google Docs.


# Task

<FILL_HERE>

Implement Arctic's migration to Google Cloud Run and the performance improvements in `docs/cloud-run-performance-plan.md`. The user has chosen Cloud Run and authorized implementation of this plan. Carry out the work, rather than producing another proposal. This task begins after the existing Phase 1 implementation and plan have been committed and pushed to main.

Before planning or coding, read all of:

1. `/localscratch/workspaces/wright-target-architecture.md`.
2. This checkout's `AGENTS.md` and any applicable subordinate instructions.
3. `docs/cloud-run-performance-plan.md` in full.
4. `app/services/README.md` for the current Phase 1 contracts and verification status.

The user is frustrated by extended investigation. Work through the plan's finite milestones, with concise progress updates. Define a bounded comparison before benchmarking. Avoid repeated profiling, speculative framework changes, and re-investigation of already established findings. Complete independent implementation and validation before asking for any genuinely missing credentials or pipeline capability.

## Architecture and boundaries

- Cloud Run hosting supersedes the original Cloudflare Worker hosting/Free CPU constraint. Keep storage in the existing private R2 buckets and metadata in Supabase Postgres. Do not migrate storage to Google.
- Preserve opaque whole-checkpoint storage: Arctic never reads, decodes, decompresses, merges, re-encodes, or queries document content. Keep UUIDv7, current schema ownership, object key formats, permission rules, size bounds, quotas, batch windows, idempotency, pointer races, and retention behavior.
- Never list R2 objects, use conditional R2 writes, multipart uploads, or parallel uploads. Bounded concurrent HEAD/DELETE operations are allowed by this plan.
- Preserve the current Phase 1 database and objects. The fresh catalog setup is destructive: do not rerun it on the active database. Add versioned metadata/function/index changes as needed; do not create a legacy document migration project.
- Do not modify `wright-pacific-one`. Pacific changes, live WebRTC, signaling, session tickets, and TURN remain a separate future phase. Existing Arctic client contracts remain compatible during cutover.

## Implement the plan in order

1. Establish a reproducible prebuilt native Cloud Run image and deployment configuration, with one modular API and separately authenticated scheduled maintenance. Add observability for complete operations, CPU, dependency waits, connection acquisition, locks, SQL exchanges, and R2 operations. Establish one native baseline.
2. Implement typed asynchronous domain/database/storage interfaces. Own database and HTTP pools at process lifetime; remove per-operation thread hops and the Worker async/synchronous bridge from the final Cloud Run path. Default to native async Psycopg 3; perform one bounded comparison against asyncpg for representative full operations and retain one production driver. Reuse TLS/HTTP/database connections and bounded prepared/type caches. Choose direct Supabase connectivity where supported or session pooling when required; Cloud Run does not need Hyperdrive. Budget connections across instances, maintenance, and rolling deployments.
3. Reduce serialized remote calls. Preserve local asymmetric JWT verification and the existing parsed JWKS public-key cache (five-minute lifetime, shared fetch/rotation behavior). That optimization already exists; it is not the remaining policy call. Review the actual authoritative `account_access` definition before consolidating live session/account/MFA checks with identity resolution and the domain operation through a narrow private database function. Its source is not in this checkout. Do not replace live checks with signature/expiry checks, positive authorization caching, a service-role key, or broadly privileged runtime credentials. If exact equivalent private evaluation cannot be established, retain the existing policy HTTP check, share its transport, and report its measured cost.
4. Implement coarse database operations for pointer reads, batch reservation, verified publication, metadata mutations, and image confirmation. Preserve READ COMMITTED, deterministic locks, fresh permission reads after lock waits, conditional pointer updates, atomic batch bookkeeping, and per-item results. One database function must not accidentally replace a safe post-lock fresh statement with a stale joined snapshot. First-time account/space provisioning stays atomic but must not impose a separate resolution transaction on every established-account request.
5. Move all R2 verification outside locked transactions. In particular, image confirmation currently holds an account lock while doing HEAD and AVIF prefix inspection: replace it with preflight, external verification, and locked revalidation/finalization. Preserve the HEAD and bounded AVIF file-type inspection, signed sizes/hashes, quota races, and reservation expiry. Use bounded concurrent checkpoint HEADs. Attempt required refused/retired deletes immediately after database commit, retaining failures durably for cleanup.
6. Tune metadata queries/indexes using realistic EXPLAIN plans; useful indexes already exist. Add keyset pagination additively where warranted. Keep mutable grants, pointers, last_sync, quotas, and session policy authoritative in Postgres. Avoid new cache databases, replicas, microservice RPC hops, or event systems without measured need.
7. Prepare the plan's signed direct image-delivery capability as an explicit additive protocol proposal. It changes the original authenticated-byte proxy contract. Keep current endpoints until the decision and client compatibility are verified; do not change Pacific or assume redirect support. Public avatars remain unauthenticated and buckets remain private. Prevent bearer leakage on cross-origin redirects, signed URL logging, and public caching of private authorization responses. R2 URLs cover one operation/object but are replayable until expiry. Do not claim image egress savings before clients use the direct path.
8. Implement layered application/DB/HTTP deadlines, cancellation, bounded admission/queues/retries, safe handling of ambiguous commits, and graceful shutdown. A Cloud Run request timeout alone does not stop application work. Keep durable work in Postgres, not fire-and-forget tasks.
9. Isolate cleanup's pool/concurrency and authenticate its scheduler invocation through IAM/OIDC. Preserve immediate delete attempts, 24-hour orphans, three checkpoints, three-day trash retention, pending retries, replay tombstones, duplicate invocation safety, and exact-key tracking. Turn off the old cleanup cron at cutover so it does not run twice.
10. Optimize startup and tune region, concurrency, memory, pool size, and CPU boost. Start experiments with one ASGI process, 1 vCPU, 512 MiB, concurrency 16, and a four-connection pool, then select settings from the bounded matrix in the plan. Document cost-first zero-minimum and latency-first one-minimum configurations. A spend cap is not an instant or guaranteed zero-dollar free-tier cutoff.

A Rust rewrite is not automatically authorized by the hosting decision. After removing I/O waste, execute the plan's bounded complete-operation Rust comparison if remaining CPU goals justify it; present measured benefit before a broader language rewrite. Existing FastAPI/domain conventions remain the implementation default.

## Verification and completion

- Follow the plan's provisional targets and state measurement boundaries: warm pointer p95 <= 100 ms (stretch 50 ms), 50-row metadata page <= 150 ms, eight-document reservation <= 200 ms, eight-document publication <= 500 ms under healthy dependencies, cold first authenticated response p95 <= 2 s as an unproven goal. Separate client geography/file transfer, cold starts, CPU, and network waiting. Never present earlier Worker diagnostics as measured Cloud Run endpoint performance.
- Run appropriate existing quality/build checks and meaningful real-Postgres race/failure tests. Cover concurrent publishes, revocation while waiting, batch admission, quota/confirm/cleanup races, idempotent lost responses, object size/format checks, canceled requests, and startup/shutdown. Verify R2 transport with disposable objects. Avoid requiring a personal test account if reusable fixtures/disposable principals suffice.
- Use a stable endpoint/canary/rollback strategy with additive compatible schema changes. Do not shadow mutations by executing them twice. Keep production healthy while implementation proceeds. Configure deployment through supported Flyer entrypoints; do not bypass the prescribed pipeline.
- No new Google credentials were supplied in the previous task. Inspect available supported configuration before asking. If credentials or permissions are missing, complete unaffected local work, then request the exact required values and where they should be created/stored. Never expose secrets. The old Cloudflare handoff text file was already deleted; do not depend on it. Existing ignored `.env` and `.env.cloudflare` contain configuration/secrets; do not commit or print them.
- The user previously authorized deployment and now asks for implementation in this new task. Prepare and validate a concrete Cloud Run deployment and proceed within that authorization once supported access is available. Do not remove the old production service until the replacement is verified and rollback is available.
- Finish with the deployed or deployment-ready result, a brief before/after scorecard, selected settings, completed checks, remaining genuine blockers, and any client-dependent items explicitly deferred. Do not claim unavailable cloud testing or a successful migration without evidence.

</FILL_HERE>

# Workspace

Your project code is `wright-arctic-one`.

For any and all local development changes, you are to **exclusively** work within the restricted workspace in `/localscratch/nooranon/workspaces/<project-code>`.

This local machine and all of its resources are at your disposal, **except** for the other workspaces defined in `/localscratch/nooranon/workspaces/`. This is a system designed to allow you (an agent) to have complete freedom on this machine, while also not conflicting with other concurrent agents making their own changes in other workspaces.

## Flyer

On this machine is Flyer, a command line tool purpose built for development in this project’s ecosystem. Use `flyer` and its many subcommands whenever you can, with documentation available through `—help`.

When what you want to do is unavailable through Flyer or any other explicitly specified development mechanisms: pause. Never try to work around these local development pipelines to complete the task; instead pause, and always ask for confirmation.

## Context

For your benefit there are several available resources present on this machine that may be useful.

Within the `codebases` folder in general there are plenty of other relevant codebases as well, with `/localscratch/nooranon/context/codebases/README.md` giving you a precise overview of how to navigate everything. This app uses many new technologies, so documentation can be sparse. Thus, code examples and targeted research here can be very valuable.

Overall, the `/localscratch/nooranon/context` folder can be a great source of research for this app.


# Philosophy

As you go along, be sure to plan thoroughly ahead, with maintainability always in mind. You want to implement this task, but you also want to significantly better the codebase along the way. More important than even writing good code, is implementing good systems which are robust.

This philosophy should be how you approach all work for this project; maintainability is the north star. Use your own, independent and thorough judgement to push maintainability to the maximum, wherever, in whatever way is necessary, pushing existing systems aside.

You should never need to patch fixes, nor create workarounds, nor make *any* decisions which fail to serve the long-term. Everything bends to maintainability. Be tenacious in this.

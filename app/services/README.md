# Arctic Phase 1 storage contract

Arctic coordinates opaque, complete checkpoint files. Only Pacific reads or writes
the Yrs V2 + zstd document format. The server treats document contents as opaque.

## Boundaries and ownership

`identity` maps a verified `(issuer, subject)` to an internal UUIDv7 account and
provisions its first space in the same transaction. Provider subject UUIDs do not
become account IDs. `documents` owns spaces, titles, grants, and trash. `sync` owns
upload slots, pointer publication, and recovery checkpoint permissions. `images`
owns AVIF reservations, confirmation, and delivery. `cleanup` handles exact-key
deletion. Hosting adapters supply database and storage clients through
`AppResources`; FastAPI routers only validate and dispatch.

The five `documents` tables and `public.accounts` / `public.images` implement the
target catalog. The private `coordination` schema holds batch admission, attempt
ownership, retry results, image quota reservations, and paged image manifests.
These auxiliary records contain IDs, sizes, hashes, and timestamps. No table
contains document bytes or content updates. SQL constraints validate RFC 9562
UUIDv7 identifiers, pointer completeness, image sizes, and canonical object keys.

The runtime role is `arctic_catalog`. Supabase client roles cannot access these
tables. The separate admin connection is used exclusively by explicit setup;
request handling never provisions or alters the schema.

## Fresh setup and operation

Copy the settings in the root `.env.example` into local configuration. Supply
separate bucket-scoped credentials for `wright-documents` and `wright-images`.
Every object key starts with `documents/` or `images/`, respectively. Both buckets
remain private; profile pictures are public through Arctic's avatar endpoint.

Use the supported development entrypoints from this checkout:

```sh
flyer run arctic-one -- uv sync --locked
flyer run arctic-one -- uv run python -m app.services.catalog setup --grant-to RUNTIME_LOGIN
flyer run arctic-one -- uv run python -m app.services.catalog verify
flyer check arctic-one
flyer build arctic-one
flyer run arctic-one -- make worker-setup
flyer run arctic-one -- make worker-build
```

The first catalog setup **discards the previous document schema and the old
Arctic account/image catalog**. It does not copy legacy documents, updates,
snapshots, identities, or images. Subsequent setup calls verify the exact source
recorded in the Supabase version ledger and preserve the new catalog. External
Supabase Auth users and profile tables are untouched.

Worker deployment binds `DOCUMENTS`, `IMAGES`, and `HYPERDRIVE`, and registers a
five-minute cleanup cron. The deploy command disables Hyperdrive query caching
before activation because permissions and pointers require fresh reads.
[Cloudflare's caching guidance](https://developers.cloudflare.com/hyperdrive/concepts/query-caching/)
describes this consistency requirement. An ordinary ASGI deployment must schedule
`uv run python -m app.services.cleanup` independently. Each invocation is bounded
to sixteen sequential deletes, and can be repeated to drain a backlog. Failed
deletes remain recorded for the next invocation.

## Identity, metadata, and media API

All IDs in paths and request bodies are UUIDv7. `GET /account` returns the
internal account ID. `GET /spaces` includes the initial space;
`POST /spaces` creates another using a caller-generated `id`.

| Operation | Route / JSON fields |
| --- | --- |
| Create metadata | `POST /documents`: `id`, `space`, `title` |
| Library / metadata | `GET /documents`, `GET /documents/{id}` |
| Rename | `PATCH /documents/{id}/title`: `title` |
| List grants | `GET /documents/{id}/grants` |
| Grant editor or viewer | `PUT /documents/{id}/grants/{account}`: `role` |
| Revoke grant | `DELETE /documents/{id}/grants/{account}` |
| Trash / restore metadata | `POST /documents/{id}/trash`, `POST /documents/{id}/restore` |
| Reserve an AVIF | `POST /images/uploads`: `id`, `size`, `hash`, `media_type: "image/avif"` |
| Confirm uploaded image | `POST /images/{id}/confirm` |
| List / deduplicate | `GET /images`, `GET /images/by-hash/{sha256}` |
| Image metadata / signed download redirect | `GET /images/{id}`, `GET /images/{id}/content` |
| Delete unreferenced image | `DELETE /images/{id}` |
| Select / clear avatar | `PUT /account/avatar`: `image_id` or null |
| Public avatar signed download redirect | `GET /avatars/{account}` |

An owner's grant is permanent for the lifetime of the document. Owners manage
sharing and trash; editors can rename and publish; viewers only read. Images are
readable by their uploader or someone with a current grant to an active document
that references them. New image references require the uploader to have edit
access at publication. Existing validated references survive uploader revocation
because committed history remains permanent. Referenced images cannot be
deleted while a document or avatar still uses them.

An upload reservation counts toward the account's 100-per-UTC-day and 300-stored
quotas. Confirmed uploads count once; deletion does not refund the daily quota.
Retries reuse the reservation. SHA-256 deduplication of confirmed images works
even when a quota is full. Images are capped at **2,000,000 bytes**, with no pixel
dimension cap. Confirmation performs a HEAD for size, type, and signed hash
metadata, plus a range GET of at most 512 bytes to check the AVIF file-type box.
This intentionally costs two Class B requests: trusting a supplied MIME type
alone would accept a renamed GIF or JPEG XL file. Arctic performs no pixel
decoding, encoding, or rendition generation. Content and public avatar routes
authorize access and issue a 307 redirect to a signed R2 GET. Redirects use
`Cache-Control: private, no-store` so a later request checks the current grant
or avatar selection.

## Sync protocol

1. Read `GET /sync/documents/{id}?revision=KNOWN_REVISION`. The response includes
   the current revision/key and a direct GET permission if changed. Matching
   revisions cause zero R2 requests. An unsaved document has a null pointer. A
   missing downloaded object requires rereading this endpoint. Authorized clients
   can request `GET /sync/documents/{id}/checkpoints` for the three retained
   recovery checkpoints; Arctic never inspects their contents.
2. Start `POST /sync/batches` with a client-generated `id` and `documents` entries
   containing `document_id`, `size`, and lowercase SHA-256 `hash` of the proposed
   complete file. Up to eight entries fit in each call. For a larger library,
   repeat with the **same batch ID** during its initial five-minute admission
   window. Account locking and `accounts.last_sync` admit at most one new batch
   per user per five minutes. A 429 includes `Retry-After` and `X-Next-Sync`.
3. Each returned slot contains the base pointer and a newly issued revision/key,
   recorded before any R2 upload. If merging a changed pointer changes the
   proposed bytes, obtain a replacement using
   `POST /sync/batches/{batch}/documents/{document}` with `previous_revision`,
   `base_revision`, `size`, and `hash`. The client must first merge that base.
   A changed base returns 409, requiring another pointer read and merge.
4. Upload directly to R2 with one **sequential PUT per file**, using the exact URL
   and every returned header. No multipart uploads, parallel uploads, conditional
   headers, server uploads, or object listings are used. The checkpoint cap is
   **100,000,000 bytes**. Capabilities expire after five minutes by default.
5. Publish `POST /sync/batches/{batch}/publish` with `documents` entries containing
   `document_id`, `base_revision`, `revision`, `key`, and `images`: every image ID
   referenced throughout the file's history. Large image manifests can be staged
   in pages using
   `POST /sync/batches/{batch}/documents/{document}/{revision}/images` with
   `{"images": [...]}`. Publication uses the union of those pages and the inline
   list. Page retries are idempotent; staged references grant no image access.
6. Arctic verifies object metadata with HEAD, then commits the batch's pointer
   decisions, newly authorized image links, and checkpoint retirements in one
   transaction. Each pointer update compares the base revision in SQL. Items
   report individual success or refusal. Idempotent publication performs no R2
   read and still succeeds after a newer checkpoint has replaced the old one.
7. A refused object and the fourth-newest published checkpoint become `pending`
   before deletion. Successful deletes mark them `deleted`. A failed delete
   cannot roll back a committed publication: `cleanup_pending` reports it and
   cron retries. Three refusals end that document's retries in the batch.

An admitted batch lasts at most 24 hours for retries. The original `last_sync`
timestamp governs retries and the initial window for admitting documents.
After a successful publication that document's batch item is finished. A later
save trigger uses a newly admitted batch. Expired `issued` slots are deleted
after 24 hours and their rows purged. Trash becomes unrestoreable after three
days by default, with records purged after all checkpoint deletes succeed.

Transactions explicitly use READ COMMITTED. A mutation first locks the document,
then reads its grants in a fresh statement. This avoids retaining a joined grant
snapshot from before a lock wait while a concurrent revocation commits.
Publication also locks its checkpoint rows before rereading their states, so
orphan cleanup cannot retire a checkpoint during a successful publish.

PUT permissions sign the exact length, MIME type, SHA-256 payload hash, and hash
metadata. R2 permissions cover one **operation and object** until expiry. A client
can repeat an identical request while its URL remains valid. [Cloudflare presigned URLs](https://developers.cloudflare.com/r2/api/s3/presigned-urls/).
The signed payload binds a replay to the same bytes. Deleted-slot tombstones are
retained for another sweep after the 24-hour upload timeout so an early replay
cannot permanently resurrect a retired object.

## Request and database work

`/ready` in the native process requires a full connection pool and a loaded
catalog snapshot. The process opens the full pool at startup. Native Postgres
reads use one autocommit statement. Each grouped write ships BEGIN, work, and
COMMIT in one pipeline with READ COMMITTED isolation. The same pipeline reads
the committed change log so the process applies its own writes before replying.
Publication dispatches R2 HEADs between its preflight and commit phases.

The native runtime URL uses a session pooler or direct Postgres connection on
port 5432. It needs the additive migrations in the catalog ledger. One dedicated
connection subscribes to `arctic_catalog_changes` before loading the snapshot.
If needed, `WRIGHT_ARCTIC_DOCUMENTS_LISTENER_URL` supplies a restricted direct
connection for that socket. Reads fall back to Postgres while the listener
reconnects or applies changes it has been notified about.

The container starts with `python -m app` and does not listen until that startup
finishes. TCP keepalives and a pool check every 30 seconds replace sessions the
platform or the pooler dropped while the instance was idle, without adding a
round trip to a request. The server keeps HTTP connections open long enough for
the load balancer to reuse them, and it returns its database sessions within the
platform's stop window. Run one process per instance and set the service
concurrency to the admission limit (16 by default). A transaction-mode pooler
on port 6543 cannot serve this process.

Hyperdrive sockets remain request-owned. The driver's bounded statement cache
reuses prepared statements within that request. Hyperdrive query-result caching
remains disabled. The Worker verifies the runtime role once per request and
executes the same grouped SQL operations in request-owned transactions.

Domain services are created when used. The Worker caches immutable settings,
bucket signers, and date-scoped SigV4 signing keys. Health requests and immediate
authentication failures avoid constructing database and storage adapters.

## Verification and deployment limits

`make check` exercises real PostgreSQL statements in the repository's PGlite
fixture, including simultaneous publication attempts, rollback, quota races,
revocation, retention, timeout cleanup, paged image manifests, public avatars,
and idempotency. SigV4 output is compared against AWS's independent signer.
Hosted PostgreSQL's independent-connection lock behavior is covered by opt-in
tests using `WRIGHT_ARCTIC_TEST_POSTGRES_URL` against
an already provisioned **test catalog**. These check two publishers, a
revocation committed while a publisher waits for the document lock, and cleanup
committed while publication waits for a checkpoint lock. The opt-in
`flyer run arctic-one -- uv run python -m app.tests.r2_conformance` probe checks
live R2 acceptance, hash and size binding, operation/key restrictions, expiry,
2 MB image confirmation, and deletion using disposable cataloged keys. It never
uses a hosted application database. Server tests cannot verify client merges, preserved notes, format
V2/zstd correctness, or recovery of local unsaved notes.

R2's presigned PUT canonical payload marker is `UNSIGNED-PAYLOAD` even when a
signed `x-amz-content-sha256` header binds the body. The live probe verifies that
changed bytes are rejected. Other S3 endpoints retain AWS's payload-header
canonicalization. Unit checks use AWS's signer with only that R2 marker adjusted.

Requests are limited to 128,000 control bytes and eight document items. Image
manifests and large libraries page within the same batch. Publication needs at
most eight R2 HEADs plus eight immediate deletes. Each cleanup run uses at most
sixteen sequential deletes. No checkpoint bytes enter Worker memory.
The Worker uses one request-owned Hyperdrive socket. Object downloads go to R2
after Arctic authorizes them.

Workers Free allows 10 ms CPU, 128 MB memory, and 100,000 requests/day. The local
dry-run checks the 64 MiB bundle bound. Hyperdrive has a 100,000-query daily
allowance. [Workers limits](https://developers.cloudflare.com/workers/platform/limits/),
[Hyperdrive pricing](https://developers.cloudflare.com/hyperdrive/platform/pricing/).

Live verification on November 15, 2026 passed both independent PostgreSQL races,
a 100 MB direct checkpoint upload, eight sequential 2 MB public-avatar reads,
and scheduled orphan-image, orphan-checkpoint, and trash cleanup. All disposable
objects and catalog rows were removed afterward.

**The Workers Free CPU requirement remains unmet.** Small before/after live
samples measured median public-avatar CPU of 88 ms across eight traces and
39 ms across three traces. All eight 2 MB reads in each run arrived complete.
A full scheduled cleanup deleted eight checkpoints, eight images, and one
trashed document, using 420 ms CPU. All disposable records and objects were
removed. Direct native R2 stream delivery fixed observed image truncation.
An experiment freezing the startup heap failed to bring CPU within the allowance
and was reverted.

Eight-document publication and idempotent retries also passed in an isolated
remote Worker against hosted PostgreSQL with statement caching enabled. That
check used a metadata-only object test transport. Production signed-in sync CPU
remains unmeasured.

Pacific changes, live sessions, signaling, admission tickets, TURN, and native
client format/history verification belong to Phase 2. The retired content and
image-conversion endpoints have no compatibility implementation in Phase 1.

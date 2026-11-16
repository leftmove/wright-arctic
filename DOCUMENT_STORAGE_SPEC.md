# Wright document storage — implementation handoff

**Target:** Pacific edits and persists locally. One Cloudflare Durable Object (DO) per document owns current cloud content. Arctic owns the Postgres catalog and publishes archive pointers. R2 holds complete compressed CRDT checkpoints. The next agent should use this as the design reference; the source notes below describe the starting point, not the deployed state.

## Scope and decisions

- Start from empty document stores. There are no existing documents to preserve. The future reset applies to document data in the target environment; leave unrelated accounts, media, and services alone. No migration upgrade path, backfill, compatibility phase, or dual writes.
- Remove the arbitrary Arctic document and update caps as part of replacing that content path. Future rate limiting is separate. Actual platform and decoder constraints still apply.
- Stay within the **R2 Standard free allowance**. Optimize for request count. Combine bytes into a single operation; no multipart uploads, parallel part requests, per-edit R2 objects, or normal-path bucket scans.
- Search is separate (planned local hybrid keyword/semantic index). No baseline measurements, version history, or backup/recovery feature in this project. Correct saves, retry, and offline work still matter.
- This is a spec only. No reset, code changes, or deployment were performed.

## Who owns what

| Component | Responsibility |
| --- | --- |
| Pacific (native Rust/Yrs) | Immediate editing, rendering, and offline persistence. SQLite stores identifiers, index fields, file references, and sync bookkeeping; document payloads live in files. |
| Document DO (PartyServer/Yjs) | One active cloud replica per immutable document ID. Integrates edits, tracks revisions, and maintains durable working state in DO SQLite. |
| Arctic (Python Worker/FastAPI) | Document identity, titles/names/placement, access, lifecycle, and the current published checkpoint pointer in Postgres via Hyperdrive. |
| R2 | Durable archive objects. Each published checkpoint contains the entire native CRDT state at its revision. |
| Atlantic | Platform identity and general web services. No new document byte path. |

There must be **one cloud content writer**. All cloud mutations go through the engine. Arctic's old REST content writer and permanent SQL change/checkpoint payloads must give way to that path. A checkpoint in R2 may lag the engine; its revision expresses archive freshness, not the latest live edit.

Pacific opens the local file first when available. For cloud sync, Arctic authorizes the session and directs the client to the engine. The DO reuses its in-memory document if present, otherwise restores from DO SQLite. Only when neither working copy exists does the DO resolve the published pointer and GET the exact R2 object. Concurrent opens share initialization. A genuinely new document may start from a valid initial state; a missing or invalid checkpoint for an existing document is a load failure. Never silently overwrite it with an empty document or independently create the same document lineage twice.

The client and engine exchange missing native updates when they can, or the full state when needed. Arctic need not relay these bytes. Hibernation can discard DO memory while sockets remain connected; wake-ups with a durable working copy read it rather than R2. Do not rely on a last-close or shutdown callback to save. [Durable Object lifecycle](https://developers.cloudflare.com/durable-objects/concepts/durable-object-lifecycle/)

## Checkpoint format, keys, and request cost

Use the same **versioned Wright file format** in Pacific and R2: a small envelope identifying the format and schema/profile, followed by a standard Zstandard frame containing a **complete Yjs/Yrs V2 update**. Use the complete-state encoder (Yjs `encodeStateAsUpdateV2` or the Yrs equivalent against an empty state vector). Yjs's `Snapshot` API is a historical marker, not a stand-alone document. Do not store the projected JSON/text and regenerate CRDT identities. Keep the original IDs, structure, and deletion information so offline edits can still merge. [Yjs updates](https://docs.yjs.dev/api/document-updates) · [Yrs V2](https://docs.rs/yrs/0.25.0/yrs/#update-encoding-v1-vs-v2)

Zstandard is lossless and is applied after encoding. Compress once per checkpoint, outside the typing/rendering path. Start with a modest level and no required dictionary; different writers can select different levels and still share a format. V2 is a good fit for complete state; file encoding does not require changing the existing V1 sync wire protocol. Choose the exact binary envelope, integrity checks, and Worker-compatible compressor together, with cross-language fixtures. [Zstandard](https://github.com/meta/zstd)

Use **one dedicated private documents bucket**, with `<document-id>/<checkpoint-revision>.wright` at the root. Keep UUIDs and immutable revisions in keys; no mutable titles or slugs. The slash is part of a flat object key. No need to create folders or pay for directory lookups. A `documents/` prefix would be equivalent for exact reads and useful only for grouping in a shared bucket. [S3 object keys](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-keys.html)

Separate buckets when access, environment, service ownership, or lifecycle differs. Keep private documents separate from public media. Similar private services may share a bucket with explicit prefixes, but a prefix is not an isolation boundary for an ordinary bucket binding. Do not create a bucket per document. Bucket count and key depth do not improve the account's free request allowance. [R2 pricing](https://developers.cloudflare.com/r2/pricing/) · [Public buckets](https://developers.cloudflare.com/r2/buckets/public-buckets/) · [Access credentials](https://developers.cloudflare.com/r2/api/tokens/)

| Event | Normal R2 cost |
| --- | --- |
| Cold activation of an archived document | One Class B GET |
| Additional collaborator, ordinary edit, or hibernation wake with working state | None |
| Export of a dirty session | One Class A PUT |
| End of an unchanged session | No PUT |

The savings come from **batching exports and needing just one object for a cold load**. A base-plus-delta archive introduces multiple reads and replay. The engine absorbs incremental edits, so rewriting the whole compressed file is an occasional CPU and byte cost, not a per-edit expense. V2 and Zstandard save bytes; checkpoint layout and timing save requests. R2's free egress does not make GETs or PUTs free beyond the allowance. Immutable revisioned keys improve publication safety; a single overwritten key would have similar request counts. No manifest, LIST, or preliminary HEAD is needed on a healthy read. Cleanup and exceptional retries are outside the normal GET/PUT pair.

## Three independent clocks

1. **Pacific local persistence and sends.** Save locally and keep a durable outbox or equivalent unsent state. Batch outgoing edits when realtime delivery is unnecessary. Delete unsent work only when the engine confirms durable coverage, including deletions. Reconnects can retry and merge. Future realtime fan-out can happen promptly without changing the persistence interval.
2. **DO working persistence.** The desired longer debounce is **about ten seconds** for cost. Use a bounded dirty window; resetting a trailing timer at each edit must not postpone saves forever. The reference has a 1 s debounce and 10 s max wait, so the change chiefly combines more paused bursts. An edit applied or broadcast in RAM is not yet durably saved; do not use PartyServer's debounced callback or a sync message as proof of durability. A single complete DO SQLite checkpoint is a reasonable first choice where it fits; a bounded journal or a different DO-local layout is an option for acknowledgement, replay, and genuine platform constraints. No working journal goes to R2. JS timers can prevent hibernation and durable alarms have their own cost, including writes to set them. Prefer scheduling a pending deadline once and reevaluating on wake over moving it on every edit. [DO lifecycle](https://developers.cloudflare.com/durable-objects/concepts/durable-object-lifecycle/) · [DO pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/)
3. **R2 publication and working-copy release.** Publish dirty documents after an editing session is inactive for a grace period. Use content activity and editing sessions; pings and presence are not edits. A durable alarm must recheck these conditions after wake. An optional maximum archive age addresses continuously active documents if the product requires one. Publishing and releasing are separate: a clean working copy may stay briefly for quick reopen, without keeping the isolate alive. Release only after publication covers all working state and the room is still eligible. [DO alarms](https://developers.cloudflare.com/durable-objects/api/alarms/)

## Publish R without losing R+1

The DO owns a monotonic content revision. Track live, durably stored, and archived progress explicitly. Count deletion-only changes; a state vector, a timestamp, and the old catalog event sequence cannot substitute for content revision. Maintain continuity after hibernation and reloading from the published pointer. Keep catalog-only mutations separate.

1. Capture the complete state at revision **R** before asynchronous work. Ensure it remains durable. Encode V2 and compress, then PUT the immutable object directly from the DO through its R2 binding.
2. After upload success, call a **private Arctic publication operation** through a Worker binding. Pass document ID, exact object key, revision, and required format/integrity metadata. Arctic updates the Postgres pointer atomically. Repeating the same publication is safe; older revisions cannot move it back, and a late save cannot revive a removed document.
3. Mark R archived only after publication is confirmed. If **R+1** arrived during I/O, keep it dirty and retain its working state. Recheck revision and idle eligibility at the point of releasing the working copy.
4. Persist enough pending state to resume failures; retain the old authoritative pointer until publication succeeds. Reuse the same verified checkpoint identity on retry; when only Postgres publication failed, do not upload the bytes again. Handle repeated alarms and retry exhaustion. Delete superseded or abandoned objects when they have no pointer or outstanding consumers, using known keys where possible. Keep only the current object long term; do not apply age-based expiry to it.

Why split the write: the DO already has the bytes, and Arctic already owns the catalog rules. Direct DO-to-R2 avoids transferring the bytes through Python; engine-to-Arctic publication passes only metadata and requires no extra R2 GET to verify normal successful uploads.

## Runtime and downloads

**Recommended first implementation:** make the PartyServer/Yjs DO an Arctic-owned component with a separate TypeScript Worker deployment and private Worker bindings. Arctic can own the architecture without taking over the room runtime or granting the engine direct Postgres access. [Service bindings](https://developers.cloudflare.com/workers/runtime-apis/bindings/service-bindings/)

In-process consolidation is possible: Cloudflare supports Python DOs, and Arctic already cross-builds its PyO3/Yrs bridge for Python Workers. But that bridge reconstructs per operation. A Python/Rust DO must provide persistent CRDT state, the sync protocol, lifecycle and socket handling, durable acknowledgements, and hibernation behavior. Revisit only if sharing the runtime actually simplifies behavior and maintenance. Merely deploying both on Workers is not a reason to port PartyServer. [Python Durable Objects](https://developers.cloudflare.com/changelog/post/2025-05-14-python-worker-durable-object/)

Normal editable opens use the engine after Arctic authorizes access. Having both Pacific and the engine download the same checkpoint can double cold R2 reads, and R2 may be behind the active engine. For a deliberate archived checkpoint download or export, Arctic may issue a short-lived, object-specific presigned GET URL. Pacific receives bytes directly from private R2. Signing makes no R2 request; fetching still counts as one GET. This saves Arctic relay work, not R2 operations. The existing image signer is a precedent; a Worker R2 binding does not sign S3 URLs by itself. [R2 presigned URLs](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)

## Compaction and local detail

- **CRDT cleanup:** keep ordinary engine GC and structural merging enabled. Transactions remove eligible deleted payloads and pack compatible records while preserving CRDT identities, deletion markers, and references needed for merging. Undo can retain content needed locally. An R2 checkpoint is not an undo stack or time machine. Do not remove old IDs arbitrarily or regenerate a document from visible text. [Yjs transaction cleanup](https://github.com/yjs/yjs/blob/v13.6.30/src/utils/Transaction.js)
- **Journal consolidation:** when a local or DO working tail exists, integrate it, finish transaction cleanup, encode a new full checkpoint, then retire only records that the replacement covers. Preserve pending dependencies or reject them explicitly before durable acknowledgement. Binary merging alone does not run deleted-content GC. Use replay backlog or bytes to decide when a journal needs folding. R2 has no tail to consolidate; session boundaries are useful for batching exports. Daily/hourly rewrites of archived documents add I/O without making metadata safe to remove.
- **Local files:** Pacific's SQLite index must reference committed files. Keep any finer-grained local journal payloads in file storage too. Use safe file replacement and index/write ordering so interruption cannot leave a truncated sole copy or erase unsent work. Local and R2 checkpoints share a format; local storage may retain more detailed edit and undo information separately.
- **Merge semantics:** prioritize preserving pending local edits, not the local replica winning every conflict. A less compacted representation is not more authoritative. Merge downloaded native state into the local lineage; a local spelling correction and a remote paragraph addition should both survive. Concurrent updates to the same field follow convergent CRDT or explicit application rules.

## Source map (research checkout code two)

These observations are from the inspected files. Verify current assigned checkouts before editing.

| Source | What the next agent needs to know |
| --- | --- |
| [Arctic document service](/localscratch/nooranon/workspaces/wright-arctic-two/app/services/documents/service.py:425) | Currently reconstructs V1 native state from Postgres `changes.payload` and `snapshots.state` (`bytea`). Changes also carry catalog events and receipts. Creating a snapshot does not remove old rows. Replace the content mechanism while preserving required access, title, and lifecycle behavior. |
| [Arctic Worker](/localscratch/nooranon/workspaces/wright-arctic-two/app/worker.py) · [Signer](/localscratch/nooranon/workspaces/wright-arctic-two/app/services/images/signing.py) | Python FastAPI on Workers, Postgres through Hyperdrive, existing image R2 binding and S3 signing code. |
| [Arctic Rust bridge](/localscratch/nooranon/workspaces/wright-arctic-two/engine/src/documents.rs:55) · [Worker packaging](/localscratch/nooranon/workspaces/wright-arctic-two/app/deployment/__main__.py:115) | Pinned Pacific/Yrs code; inherits normal GC, encodes after load completes, also projects content and infers titles. Assign derived metadata explicitly when replacing the current path. |
| [Engine reference](/localscratch/nooranon/context/codebases/wright-engine/src/server.ts:28) · [PartyServer source](/localscratch/nooranon/context/codebases/partykit/packages/y-partyserver/src/server/index.ts:72) | Read-only references. The engine saves and loads complete V1 state in DO SQLite, with no document R2 archive in that source. PartyServer reference explicitly enables GC. Check the pinned/deployed versions before assuming exact close, wake, or persistence behavior. |
| [Pacific Yrs construction](/localscratch/nooranon/workspaces/wright-pacific-two/crates/wright-crdt/src/document.rs:48) · [Local compactor](/localscratch/nooranon/workspaces/wright-pacific-two/crates/wright-document/src/native.rs:185) · [REST reconciliation](/localscratch/nooranon/workspaces/wright-pacific-two/crates/wright-backend/src/document_sync.rs:1) | Yrs defaults enable GC. The old local compactor serializes before transaction cleanup, missing possible size savings; encode after cleanup in the new path. Current SQLite content/outbox and periodic REST reconciliation are separate from the room engine. Trace actual app wiring before replacing them. |

## Open implementation choices and acceptance

- Set the exact idle grace, clean reuse period, and whether active rooms need a maximum archive age. Separate these from the roughly ten-second DO persistence target.
- Set the binary envelope and codec, local file commit protocol, DO storage representation, sync transport and durable receipt semantics, and which component produces derived catalog metadata. Choose the deletion grace around in-flight readers or issued URLs. Optional direct archive downloads and a Python engine port are independent.
- Check format round trips in Rust and JS (rich text, Unicode, deletions and offline concurrent edits), local open/save with pending work, a cold room shared by concurrent opens, wake with no R2 request, unchanged close with no PUT, one PUT on clean dirty-session export, edits during R publication, upload vs. pointer failure/retry, and reopen during release. These are correctness scenarios, not baseline or performance measurements.

Use only assigned workspaces and Flyer for the implementation and checks. Use `flyer build <assigned-Pacific-project> --target macos` for the final Pacific build, and the managed project commands for backend validation. If a required action is unavailable through the approved pipeline, stop and ask before bypassing it. Desktop interaction testing is manual.

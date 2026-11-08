# Documents API

Arctic owns document creation, durable native content, sharing, authorization,
history, checkpoints, restoration, and database setup. PostgreSQL stores the
bytes. All request authorization runs in Arctic; the seven document tables have
no RLS policies and are inaccessible to Supabase's `anon`/`authenticated` roles.

## Configure and start

Use the existing Supabase project with its account/profile migrations already
applied. Configure Arctic's existing `WRIGHT_ARCTIC_AUTH_*` settings, then:

```dotenv
WRIGHT_ARCTIC_DOCUMENTS_SETUP_URL=postgresql://ADMIN:PASSWORD@HOST:5432/postgres?sslmode=require
WRIGHT_ARCTIC_DOCUMENTS_DATABASE_URL=postgresql://arctic_api:PASSWORD@HOST:5432/postgres?sslmode=require
WRIGHT_ARCTIC_DOCUMENTS_POOL_SIZE=4
```

The setup connection needs schema/role creation privileges. Choose a separate
runtime login and password in the second URL. From Arctic's root, run through the
workspace's Flyer runner:

```sh
uv run python -m app.services.documents setup --grant-to arctic_api
uv run python -m app.services.documents verify
make run
```

`setup` transactionally creates the private schema, seven tables, profile trigger,
runtime login and grants. If the login is absent, its password comes from
`DOCUMENTS_DATABASE_URL`; existing passwords are preserved. For Supabase pooler
usernames with a project suffix, `--grant-to` names the actual PostgreSQL role.
Existing profiles receive their personal spaces and namespace claims. Later
profile inserts/username changes provision and claim names automatically, keeping
old names reserved. Reserved application routes cannot become namespaces.

Version `20261107000600` is recorded in the existing
`supabase_migrations.schema_migrations` ledger. Identical replay is safe. Different
source under that version or an untracked document schema aborts without changes.
Do not apply the superseded Atlantic document draft.

Web startup only opens the runtime pool; it never runs setup or uses the admin
URL. It verifies the seven tables, disabled RLS and a non-owner runtime login.
Prepared statements are disabled for Supabase transaction pooler compatibility.
Without a runtime URL, document requests return 503. Pool size is per process.

## HTTP contract

Every route requires the account's Supabase bearer token, verified by Arctic's
existing signature and current-account-policy checks. Mutations carry a UUID
`operation_id`; append batches carry one UUID per update. Retain these IDs and
frozen input until Arctic returns committed receipts. Responses are private.

| Method and route | Input / result |
| --- | --- |
| `POST /documents` | JSON create command; UUIDv7 document and initial native state |
| `PUT /documents/{uuid}` | Native initial upload, retaining existing UUIDv4/UUIDv7 identity |
| `GET /documents` | Owned/shared active library; `limit`, `offset`; owner-only `trashed=true` |
| `GET /documents/{uuid}` | Metadata, effective role and permanent `/d/{uuid}` path |
| `GET /documents/resolve/{namespace}/{slug}` | Exact address lookup with document authorization |
| `GET /documents/{uuid}/content` | Current clean native model envelope; owner/editor/viewer |
| `GET /documents/{uuid}/state` | Binary native state and format/schema/head headers; owner/editor |
| `POST /documents/{uuid}/changes` | Atomic native update batch, one receipt per update |
| `GET /documents/{uuid}/changes` | Event metadata; `after=-1`, bounded `limit` |
| `GET /documents/{uuid}/changes/{seq}` | Original bytes; 204 for a metadata event |
| `PATCH /documents/{uuid}/title` | Rename/reset automatic mode with metadata watermark |
| `POST /documents/{uuid}/share` | Explicit short address, optionally with grant; owner only |
| `GET /documents/{uuid}/access` | Additional grants; owner only |
| `PUT /documents/{uuid}/access/{user_uuid}` | Editor/viewer grant, or `role:null` to revoke; owner only |
| `POST /documents/{uuid}/trash` | Trash and audit; owner only |
| `POST /documents/{uuid}/restore` | Restore from trash and audit; owner only |
| `POST /documents/{uuid}/restore-content` | Historical content as a new native edit; owner/editor |
| `POST /documents/{uuid}/snapshots` | Verified checkpoint; optional `through_seq`; owner/editor |

Create JSON accepts `operation_id`, optional `schema_name` (`wright-rich-text`
default, or `basic`), optional explicit `title`, and optional native node `content`:

```json
{
  "operation_id": "01952a2c-7900-7000-8000-000000000001",
  "content": {
    "type": "doc",
    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]}]
  }
}
```

Omitting content creates a schema-valid empty document. Rich attributes, marks,
page layout and styles use the actual native schema and its defaults.

Initial upload is multipart: text field `metadata` containing JSON
`{operation_id,title,title_is_user_set,format_version:1,schema_name,schema_version:1}`,
and one binary file `state`. Append is multipart: `metadata` JSON
`{operations:[uuid,...]}` and repeated binary `updates` files in matching order.
There are at most 128 updates, each at most 4 MiB, together at most 16 MiB. Initial
and reconstructed states are at most 64 MiB. Body limits cover chunked requests
and run after authentication, before multipart parsing.

Receipts contain `document_id`, `space_id`, `operation_id`, `head_seq`, and optional
`path`. The sequence identifies that operation even when later events exist.
Identical retries return the original receipt; reusing an ID with different input
returns 409. Current permissions and trash state apply to retries. A failed
transaction produces no successful receipt.

Title JSON is `{operation_id,expected_metadata_seq,title}`; `title:null` returns to
automatic mode. Typing does not advance `metadata_seq`; explicit title commands
do. Explicit titles support the 255-character storage limit. Inferred titles use
native first-line, whitespace, hard-break and 80-character rules. Empty automatic
documents retain valid placeholders or receive the lowest available `Untitled N`.

Share JSON is `{operation_id}` or `{operation_id,user_id,role}`. Address creation
and an optional grant commit atomically. The path is `/{namespace}/{sqid}`; no
title or `@` participates. The official pinned Sqids encoder uses a per-space
counter, fixed alphabet, minimum length six and checked-in blocklist. Issued
strings remain permanent and resolve exactly, including case. Links grant no
access. Namespace renames preserve old links; a new share command uses the current
namespace and existing slug. An old operation retry returns its original path.

Restore-content JSON is `{operation_id,through_seq}`. Canonical `state` also
accepts `through_seq` and returns `X-Document-Head`, `X-Document-Format`,
`X-Document-Schema` and `X-Document-Schema-Version`. Viewers only receive current
clean content. Canonical bytes/history/checkpoints are forbidden to viewers
because CRDT state may retain deleted information.

Errors use `{code,message}`. Unknown/inaccessible/trashed documents return 404;
insufficient privileges 403; malformed native input 422; size bounds 413.
`missing_dependencies` (409) means retain the operation, send its missing
predecessors, then retry unchanged. Metadata conflicts, changed history heads
and operation ID reuse are explicit 409 results too.

## Storage, compatibility and verification

The seven tables are `spaces`, `names`, `documents`, `slugs`, `access`, `changes`,
and `snapshots`. Bytes stay in `bytea`. Creation is event zero; each submitted
update keeps its own permanent event. Metadata, access, trash and content share
one contiguous history. State, metadata and audit records commit together under
document locks. Native reconstruction runs outside write locks, followed by
current permission/head checks and bounded retries.

Automatic checkpoints run after 128 events since the previous checkpoint or
when replay input reaches 96 MiB. They contain the exact committed prefix and
historical title metadata. Creation bytes are never duplicated at snapshot zero.
Original events are never deleted. Restoring content generates a new transaction
against the current lineage; historical checkpoint bytes are not merged as edits.

The Rust binding consumes native crates at commit
`e7b66ef489af24e7c089d9cb13b8545b051abbc8`, Yrs 0.25.0, update-v1, and the
`wright`/`wright_meta` roots. Native application source is unchanged. Source builds
need read access to the private `leftmove/wright-pacific` repository. CI checks out
that exact revision with a read-only Contents token in `WRIGHT_NATIVE_READ_TOKEN`.
The built engine wheel needs no Git credentials at runtime. FastAPI hosting and
native synchronization UI/outbox work are outside this change.

Run `make check` and `make build` through Flyer. `make test` uses Bun to install
the pinned PGlite dependency under `app/tests`, then runs the Python suite. Tests
use real JWT signatures, the production service/SQL/native codec, and in-memory
PostgreSQL with the actual runtime role. Setup tests replay the real schema,
login and ledger commands, including rollback. Ordinary tests ignore configured
document database credentials. No database server or hosted project is provisioned
by tests.

After setup, run the separate hosted PostgreSQL checks explicitly through Flyer:

```sh
WRIGHT_ARCTIC_TEST_HOSTED_DOCUMENTS=1 uv run python -m unittest app.tests.test_document_postgres -v
```

These checks use the configured setup/runtime URLs and an existing profile. They
verify the actual runtime pool and grants, native document lifecycle/history,
serialization of concurrent creation requests on independent connections, and a
deferred constraint failure at commit. Document fixtures always roll back; Auth
users and profiles are never edited. The default suite skips these checks.

Hosted setup version `20261107000600` has been applied and these four checks passed
against project `jhacsyfrssyqukdqwxgc`. The generated runtime URL and pool size are
saved in the ignored local `.env`. Set `WRIGHT_ARCTIC_DOCUMENTS_DATABASE_URL` and
`WRIGHT_ARCTIC_DOCUMENTS_POOL_SIZE` in the existing API host's environment; the
setup/admin URL stays with the setup operator.

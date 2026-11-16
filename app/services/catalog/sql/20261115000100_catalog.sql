-- Fresh Phase 1 catalog. Legacy document data is intentionally discarded.
-- No content, update, snapshot, or legacy identity data is copied.
drop schema if exists documents cascade;
drop schema if exists coordination cascade;
drop table if exists public.images cascade;
drop table if exists public.accounts cascade;

create schema documents;
create schema coordination;
revoke all on schema documents, coordination from public, anon, authenticated;
do $$ begin
 if not exists(select 1 from pg_roles where rolname='arctic_catalog') then
  create role arctic_catalog nologin nosuperuser nocreatedb nocreaterole nobypassrls;
 end if;
end $$;

create function coordination.is_uuid7(value uuid) returns boolean
language sql immutable strict set search_path='' as $$
 select (get_byte(uuid_send(value),6) >> 4)=7
    and (get_byte(uuid_send(value),8) & 192)=128
$$;

create table public.accounts (
 id uuid primary key check(coordination.is_uuid7(id)),
 issuer text not null,
 subject text not null,
 avatar_id uuid,
 last_sync timestamptz,
 created_at timestamptz not null default clock_timestamp(),
 unique(issuer,subject)
);
create table public.images (
 id uuid primary key check(coordination.is_uuid7(id)),
 account uuid not null references public.accounts(id),
 key text not null unique check(key='images/'||account::text||'/'||id::text),
 size integer not null check(size between 1 and 2000000),
 hash text collate "C" not null check(hash ~ '^[0-9a-f]{64}$'),
 created_at timestamptz not null default clock_timestamp(),
 deleted_at timestamptz
);
alter table public.accounts add foreign key(avatar_id) references public.images(id);
create index images_account on public.images(account,created_at desc,id) where deleted_at is null;
create index images_hash on public.images(account,hash) where deleted_at is null;
create index avatars on public.accounts(avatar_id) where avatar_id is not null;

create table documents.spaces (
 id uuid primary key check(coordination.is_uuid7(id)),
 owner uuid not null references public.accounts(id),
 created_at timestamptz not null default clock_timestamp()
);
create index spaces_owner on documents.spaces(owner,id);
create table documents.documents (
 id uuid primary key check(coordination.is_uuid7(id)),
 space uuid not null references documents.spaces(id),
 title text not null check(char_length(title)<=255),
 trashed_at timestamptz,
 revision uuid check(coordination.is_uuid7(revision)),
 key text unique,
 published_at timestamptz,
 created_at timestamptz not null default clock_timestamp(),
 updated_at timestamptz not null default clock_timestamp(),
 check((revision is null)=(key is null) and (revision is null)=(published_at is null)),
 check(key='documents/'||space::text||'/'||id::text||'/'||revision::text)
);
create index library on documents.documents(space,updated_at desc,id) where trashed_at is null;
create index trash on documents.documents(trashed_at,id) where trashed_at is not null;
create table documents.checkpoints (
 document_id uuid not null references documents.documents(id) on delete cascade,
 revision uuid not null check(coordination.is_uuid7(revision)),
 key text not null unique,
 state text not null check(state in ('issued','published','pending','deleted')),
 issued_at timestamptz not null default clock_timestamp(),
 published_at timestamptz,
 primary key(document_id,revision),
 unique(document_id,revision,key),
 check(split_part(key,'/',1)='documents'
   and split_part(key,'/',3)=document_id::text
   and split_part(key,'/',4)=revision::text
   and array_length(string_to_array(key,'/'),1)=4),
 check(state<>'published' or published_at is not null),
 check(state<>'issued' or published_at is null)
);
alter table documents.documents add constraint current_checkpoint
 foreign key(id,revision,key) references documents.checkpoints(document_id,revision,key)
 deferrable initially deferred;
create index checkpoint_retention on documents.checkpoints(document_id,revision desc) where state='published';
create index checkpoint_orphans on documents.checkpoints(issued_at,document_id,revision) where state='issued';
create index checkpoint_pending on documents.checkpoints(document_id,revision) where state='pending';
create table documents.grants (
 document_id uuid not null references documents.documents(id) on delete cascade,
 account_id uuid not null references public.accounts(id),
 role text not null check(role in ('owner','editor','viewer')),
 primary key(document_id,account_id)
);
create unique index document_owner on documents.grants(document_id) where role='owner';
create index shared_documents on documents.grants(account_id,document_id);
create table documents.images (
 document_id uuid not null references documents.documents(id) on delete cascade,
 image_id uuid not null references public.images(id),
 primary key(document_id,image_id)
);
create index image_documents on documents.images(image_id,document_id);

-- Coordination is metadata only. Durable reservations make retries, ownership,
-- quota accounting, and orphan deletion independent of process lifetime.
create table coordination.batches (
 id uuid primary key check(coordination.is_uuid7(id)),
 account uuid not null references public.accounts(id),
 started_at timestamptz not null,
 expires_at timestamptz not null,
 check(expires_at>started_at)
);
create index batch_expiry on coordination.batches(expires_at);
create table coordination.items (
 batch uuid not null references coordination.batches(id) on delete cascade,
 document_id uuid not null references documents.documents(id) on delete cascade,
 refusals integer not null default 0 check(refusals between 0 and 3),
 primary key(batch,document_id)
);
create table coordination.attempts (
 document_id uuid not null,
 revision uuid not null,
 batch uuid not null,
 base_revision uuid check(coordination.is_uuid7(base_revision)),
 size integer not null check(size between 1 and 100000000),
 hash text collate "C" not null check(hash ~ '^[0-9a-f]{64}$'),
 expires_at timestamptz not null,
 reaped_at timestamptz,
 result text not null default 'issued' check(result in
  ('issued','published','conflict','forbidden','invalid','expired','replaced')),
 primary key(document_id,revision),
 foreign key(document_id,revision) references documents.checkpoints(document_id,revision) on delete cascade,
 foreign key(batch,document_id) references coordination.items(batch,document_id) on delete cascade
);
create index batch_attempts on coordination.attempts(batch,document_id,revision desc);
-- Large history image manifests are staged in bounded control requests. These
-- are metadata reservations, invisible to readers until the pointer publishes.
create table coordination.images (
 document_id uuid not null,
 revision uuid not null,
 image_id uuid not null check(coordination.is_uuid7(image_id)),
 primary key(document_id,revision,image_id),
 foreign key(document_id,revision) references coordination.attempts(document_id,revision) on delete cascade
);
create table coordination.uploads (
 id uuid primary key check(coordination.is_uuid7(id)),
 account uuid not null references public.accounts(id),
 key text not null unique check(key='images/'||account::text||'/'||id::text),
 size integer not null check(size between 1 and 2000000),
 hash text collate "C" not null check(hash ~ '^[0-9a-f]{64}$'),
 state text not null default 'issued' check(state in ('issued','confirmed','pending','deleted')),
 issued_at timestamptz not null,
 expires_at timestamptz not null
);
create index daily_uploads on coordination.uploads(account,issued_at);
create index upload_cleanup on coordination.uploads(state,issued_at);

revoke all on all tables in schema documents,coordination from public,anon,authenticated;
revoke all on public.accounts,public.images from public,anon,authenticated;
revoke execute on all functions in schema coordination from public,anon,authenticated;
grant usage on schema public,documents,coordination to arctic_catalog;
grant select,insert,update,delete on all tables in schema documents,coordination to arctic_catalog;
grant select,insert,update,delete on public.accounts,public.images to arctic_catalog;
grant execute on function coordination.is_uuid7(uuid) to arctic_catalog;

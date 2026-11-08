-- Arctic's private document storage. Applied explicitly by `python -m
-- app.services.documents setup`, using the existing Supabase version ledger.
-- No client CRUD, RLS, request GUCs or SECURITY DEFINER command layer.
create schema documents;
revoke all on schema documents from public, anon, authenticated;
do $$ begin
  if not exists(select 1 from pg_roles where rolname='arctic_documents') then
    create role arctic_documents nologin nosuperuser nocreatedb nocreaterole nobypassrls;
  end if;
end $$;
grant usage on schema documents to arctic_documents;

create function documents.valid_name(value text) returns boolean
language sql immutable set search_path='' as $$
 select value=lower(btrim(value)) and char_length(value) between 3 and 30
 and value collate "C" ~ '^[a-z0-9_.]+$'
 and value not in ('d','api','auth','native','login','logout','signup','signin','signout',
 'account','accounts','settings','documents','images','health','help','about','privacy',
 'terms','share','static','assets','admin','support','www','_next','.well-known')
$$;
create table documents.spaces (
 id uuid primary key default gen_random_uuid(),
 owner_user_id uuid not null unique references public.profiles(id) on delete restrict,
 canonical_name text collate "C" not null,
 next_slug bigint not null default 1 check(next_slug>0),
 created_at timestamptz not null default clock_timestamp()
);
create table documents.names (
 name text collate "C" primary key check(documents.valid_name(name)),
 space_id uuid not null references documents.spaces(id) on delete restrict,
 created_at timestamptz not null default clock_timestamp(), unique(space_id,name)
);
alter table documents.spaces add constraint canonical_name_claim
 foreign key(id,canonical_name) references documents.names(space_id,name) deferrable initially deferred;

-- This narrow provisioning trigger extends the existing profile transaction.
create function documents.provision_space() returns trigger
language plpgsql security definer set search_path='' as $$
declare container uuid; claimed uuid;
begin
 if not documents.valid_name(new.username) then raise exception 'Username unavailable' using errcode='23514'; end if;
 if tg_op='INSERT' then
  insert into documents.spaces(owner_user_id,canonical_name) values(new.id,new.username) returning id into container;
 else
  select id into strict container from documents.spaces where owner_user_id=new.id for update;
 end if;
 insert into documents.names(name,space_id) values(new.username,container) on conflict(name) do nothing;
 select space_id into strict claimed from documents.names where name=new.username;
 if claimed<>container then raise exception 'Username unavailable' using errcode='23505'; end if;
 update documents.spaces set canonical_name=new.username where id=container;
 return new;
end $$;
create trigger provision_document_space after insert or update of username on public.profiles
 for each row execute function documents.provision_space();

create table documents.documents (
 id uuid primary key,
 space_id uuid not null references documents.spaces(id) on delete restrict,
 created_by uuid not null references auth.users(id) on delete restrict,
 title text not null check(char_length(title)<=255), title_is_user_set boolean not null,
 format_version integer not null check(format_version>0), schema_name text not null,
 schema_version integer not null check(schema_version>0),
 head_seq bigint not null default 0 check(head_seq>=0),
 metadata_seq bigint not null default 0 check(metadata_seq between 0 and head_seq),
 access_version bigint not null default 0 check(access_version>=0),
 created_at timestamptz not null default clock_timestamp(),
 updated_at timestamptz not null default clock_timestamp(), trashed_at timestamptz
);
create index library on documents.documents(space_id,updated_at desc,id) where trashed_at is null;
create table documents.slugs (
 space_id uuid not null references documents.spaces(id) on delete restrict,
 number bigint not null check(number>0),
 value text collate "C" not null check(char_length(value) between 6 and 255 and value collate "C" ~ '^[a-zA-Z0-9]+$'),
 document_id uuid not null references documents.documents(id) on delete restrict,
 created_by uuid not null references auth.users(id) on delete restrict,
 created_at timestamptz not null default clock_timestamp(),
 primary key(space_id,value), unique(space_id,number), unique(space_id,document_id)
);
create index aliases on documents.slugs(document_id);
create table documents.access (
 document_id uuid not null references documents.documents(id) on delete restrict,
 user_id uuid not null references auth.users(id) on delete restrict,
 role text not null check(role in ('editor','viewer')),
 granted_by uuid not null references auth.users(id) on delete restrict,
 granted_at timestamptz not null default clock_timestamp(), primary key(document_id,user_id)
);
create index shared on documents.access(user_id,document_id);
create table documents.changes (
 document_id uuid not null references documents.documents(id) on delete restrict,
 seq bigint not null check(seq>=0), operation_id uuid not null,
 actor_user_id uuid not null references auth.users(id) on delete restrict,
 source text not null check(source in ('native','arctic')),
 kind text not null check(kind in ('create','content','title','share','access','trash','restore','restore_content')),
 format_version integer not null check(format_version>0), schema_name text not null,
 schema_version integer not null check(schema_version>0), payload bytea,
 metadata jsonb not null check(jsonb_typeof(metadata)='object'),
 request_sha256 bytea not null check(octet_length(request_sha256)=32),
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(document_id,seq), unique(document_id,operation_id),
 check((seq=0)=(kind='create')),
 check((kind in ('create','content','restore_content'))=(payload is not null)),
 check(payload is null or octet_length(payload) between 1 and 67108864)
);
alter table documents.documents add constraint committed_head
 foreign key(id,head_seq) references documents.changes(document_id,seq) deferrable initially deferred;
create unique index server_creation_operations on documents.changes(actor_user_id,operation_id)
 where kind='create' and source='arctic';
create table documents.snapshots (
 document_id uuid not null references documents.documents(id) on delete restrict,
 through_seq bigint not null check(through_seq>0),
 format_version integer not null check(format_version>0), schema_name text not null,
 schema_version integer not null check(schema_version>0),
 state bytea not null check(octet_length(state) between 1 and 67108864),
 document_metadata jsonb not null check(jsonb_typeof(document_metadata)='object'),
 state_sha256 bytea not null check(octet_length(state_sha256)=32),
 created_at timestamptz not null default clock_timestamp(), primary key(document_id,through_seq),
 foreign key(document_id,through_seq) references documents.changes(document_id,seq) on delete restrict
);

create function documents.immutable_record() returns trigger language plpgsql set search_path='' as $$
begin raise exception 'Permanent document record' using errcode='42501'; end $$;
create trigger immutable_names before update or delete on documents.names for each row execute function documents.immutable_record();
create trigger immutable_slugs before update or delete on documents.slugs for each row execute function documents.immutable_record();
create trigger immutable_changes before update or delete on documents.changes for each row execute function documents.immutable_record();
create trigger immutable_snapshots before update or delete on documents.snapshots for each row execute function documents.immutable_record();
revoke all on all tables in schema documents from public,anon,authenticated;
revoke execute on all functions in schema documents from public,anon,authenticated;
grant select on all tables in schema documents to arctic_documents;
grant execute on function documents.valid_name(text), documents.immutable_record() to arctic_documents;
grant insert on documents.documents,documents.slugs,documents.access,documents.changes,documents.snapshots to arctic_documents;
grant update(next_slug) on documents.spaces to arctic_documents;
grant update(title,title_is_user_set,head_seq,metadata_seq,access_version,updated_at,trashed_at) on documents.documents to arctic_documents;
grant update(role,granted_by,granted_at),delete on documents.access to arctic_documents;

insert into documents.spaces(owner_user_id,canonical_name) select id,username from public.profiles;
insert into documents.names(space_id,name) select id,canonical_name from documents.spaces;

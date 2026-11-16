-- Order the feed by commit, not by the order in which concurrent writes begin.
create table coordination.catalog_changes (
 number bigint generated always as identity primary key,
 relation text not null,
 previous jsonb,
 current jsonb,
 check(previous is not null or current is not null)
);
revoke all on coordination.catalog_changes from public, anon, authenticated;
grant select on coordination.catalog_changes to arctic_catalog;

create function coordination.log_catalog_change() returns trigger
language plpgsql security definer set search_path='' as $$
declare previous jsonb; current jsonb; position bigint;
begin
 if tg_argv[0]='checkpoints' then
  -- Only published recovery pointers belong in the read model.
  if tg_op<>'INSERT' and old.state='published' then
   previous=jsonb_build_object('document_id',old.document_id,'revision',old.revision,
       'key',old.key,'published_at',old.published_at);
  end if;
  if tg_op<>'DELETE' and new.state='published' then
   current=jsonb_build_object('document_id',new.document_id,'revision',new.revision,
       'key',new.key,'published_at',new.published_at);
  end if;
 else
  if tg_op<>'INSERT' then previous=to_jsonb(old); end if;
  if tg_op<>'DELETE' then current=to_jsonb(new); end if;
 end if;
 if previous is distinct from current then
  -- Deferred triggers acquire this after the write has taken its usual row
  -- locks. Holding it through commit makes the visible feed a safe prefix.
  perform pg_advisory_xact_lock(hashtextextended('wright/catalog/changes',0));
  insert into coordination.catalog_changes(relation,previous,current)
   values(tg_argv[0],previous,current) returning number into position;
  perform pg_notify('arctic_catalog_changes',position::text);
 end if;
 return null;
end $$;
revoke all on function coordination.log_catalog_change() from public, anon, authenticated;

create constraint trigger catalog_accounts after insert or update or delete on public.accounts
 deferrable initially deferred for each row execute function coordination.log_catalog_change('accounts');
create constraint trigger catalog_spaces after insert or update or delete on documents.spaces
 deferrable initially deferred for each row execute function coordination.log_catalog_change('spaces');
create constraint trigger catalog_documents after insert or update or delete on documents.documents
 deferrable initially deferred for each row execute function coordination.log_catalog_change('documents');
create constraint trigger catalog_grants after insert or update or delete on documents.grants
 deferrable initially deferred for each row execute function coordination.log_catalog_change('grants');
create constraint trigger catalog_checkpoints after insert or update or delete on documents.checkpoints
 deferrable initially deferred for each row execute function coordination.log_catalog_change('checkpoints');
create constraint trigger catalog_images after insert or update or delete on public.images
 deferrable initially deferred for each row execute function coordination.log_catalog_change('images');
create constraint trigger catalog_image_references after insert or update or delete on documents.images
 deferrable initially deferred for each row execute function coordination.log_catalog_change('image_references');

create function coordination.catalog_snapshot()
returns table(number bigint, records jsonb)
language sql stable set search_path='' as $$
 select coalesce((select max(c.number) from coordination.catalog_changes c),0),
  jsonb_build_object(
   'accounts',coalesce((select jsonb_agg(a) from public.accounts a),'[]'),
   'spaces',coalesce((select jsonb_agg(s) from documents.spaces s),'[]'),
   'documents',coalesce((select jsonb_agg(d) from documents.documents d),'[]'),
   'grants',coalesce((select jsonb_agg(g) from documents.grants g),'[]'),
   'checkpoints',coalesce((select jsonb_agg(c) from (
    select document_id,revision,key,published_at from documents.checkpoints where state='published'
   ) c),'[]'),
   'images',coalesce((select jsonb_agg(i) from public.images i),'[]'),
   'image_references',coalesce((select jsonb_agg(r) from documents.images r),'[]')
  )
$$;
revoke all on function coordination.catalog_snapshot() from public, anon, authenticated;
grant execute on function coordination.catalog_snapshot() to arctic_catalog;

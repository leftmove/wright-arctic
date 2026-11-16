-- A grouped write may have changed rows before returning a domain failure.
-- Turn that result into a SQL error before COMMIT, including the domain data.
create function coordination.checked_result(result jsonb) returns jsonb
language plpgsql set search_path='' as $$
begin
 if result ? 'error' then
  raise exception using errcode='PZ001',message='catalog operation rejected',detail=result::text;
 end if;
 return result;
end $$;

create function coordination.create_space(p_account uuid,p_space uuid) returns jsonb
language plpgsql set search_path='' as $$
declare s documents.spaces;
begin
 insert into documents.spaces(id,owner) values(p_space,p_account)
 on conflict(id) do nothing returning * into s;
 if not found then
  select * into s from documents.spaces where id=p_space and owner=p_account;
  if not found then return jsonb_build_object('error','conflict'); end if;
 end if;
 return to_jsonb(s);
end $$;

-- All document writers lock before looking at grants, using a fresh statement
-- under READ COMMITTED. Keep the lock until the transaction commits.
create function coordination.lock_document(p_document uuid,p_account uuid)
returns table(document documents.documents,role text)
language plpgsql set search_path='' as $$
begin
 perform id from documents.documents where id=p_document for update;
 return query select d,g.role from documents.documents d join documents.grants g on g.document_id=d.id
  where d.id=p_document and g.account_id=p_account;
end $$;

create function coordination.create_document(p_account uuid,p_document uuid,p_space uuid,p_title text) returns jsonb
language plpgsql set search_path='' as $$
declare d documents.documents; r text; locked record;
begin
 if not exists(select 1 from documents.spaces where id=p_space and owner=p_account) then
  return jsonb_build_object('error','not_found');
 end if;
 insert into documents.documents(id,space,title) values(p_document,p_space,p_title)
 on conflict(id) do nothing;
 if found then insert into documents.grants values(p_document,p_account,'owner'); end if;
 select * into locked from coordination.lock_document(p_document,p_account);
 if not found then return jsonb_build_object('error','not_found'); end if;
 d=locked.document; r=locked.role;
 if d.space<>p_space or r<>'owner' then return jsonb_build_object('error','conflict'); end if;
 return to_jsonb(d)||jsonb_build_object('role',r);
end $$;

create function coordination.set_grant(p_account uuid,p_document uuid,p_target uuid,p_role text) returns jsonb
language plpgsql set search_path='' as $$
declare d documents.documents; r text; locked record;
begin
 select * into locked from coordination.lock_document(p_document,p_account);
 if not found then return jsonb_build_object('error','not_found'); end if;
 d=locked.document; r=locked.role;
 if r<>'owner' then return jsonb_build_object('error','forbidden'); end if;
 if d.trashed_at is not null then return jsonb_build_object('error','not_found'); end if;
 if p_target=p_account or p_role not in ('editor','viewer') then
  return jsonb_build_object('error','forbidden');
 end if;
 if p_role is null then
  delete from documents.grants where document_id=p_document and account_id=p_target and role<>'owner';
 else
  if not exists(select 1 from public.accounts where id=p_target) then
   return jsonb_build_object('error','not_found');
  end if;
  insert into documents.grants values(p_document,p_target,p_role)
   on conflict(document_id,account_id) do update set role=excluded.role
   where documents.grants.role<>'owner';
 end if;
 return '{}'::jsonb;
end $$;

create function coordination.document_lifecycle(p_account uuid,p_document uuid,p_trashed boolean,p_days integer) returns jsonb
language plpgsql set search_path='' as $$
declare d documents.documents; r text; locked record;
begin
 select * into locked from coordination.lock_document(p_document,p_account);
 if not found then return jsonb_build_object('error','not_found'); end if;
 d=locked.document; r=locked.role;
 if r<>'owner' then return jsonb_build_object('error','forbidden'); end if;
 if clock_timestamp()>=d.trashed_at+make_interval(days=>p_days) then
  return jsonb_build_object('error','trash_expired');
 end if;
 update documents.documents
  set trashed_at=case when p_trashed then coalesce(trashed_at,clock_timestamp()) end,
      updated_at=clock_timestamp()
  where id=p_document returning * into d;
 return to_jsonb(d)||jsonb_build_object('role',r);
end $$;

create function coordination.select_avatar(p_account uuid,p_image uuid) returns jsonb
language plpgsql set search_path='' as $$
declare a public.accounts;
begin
 perform id from public.accounts where id=p_account for update;
 if p_image is not null and not exists(
  select id from public.images where id=p_image and account=p_account and deleted_at is null for share
 ) then return jsonb_build_object('error','not_found'); end if;
 update public.accounts set avatar_id=p_image where id=p_account returning * into a;
 if not found then return jsonb_build_object('error','not_found'); end if;
 return to_jsonb(a);
end $$;

create function coordination.issue_image(p_account uuid,p_id uuid,p_size integer,p_hash text,p_seconds integer,p_daily integer,p_total integer)
returns jsonb language plpgsql set search_path='' as $$
declare i public.images; u coordination.uploads; t timestamptz;
begin
 perform id from public.accounts where id=p_account for update;
 t=clock_timestamp();
 select * into i from public.images where id=p_id;
 if found then
  if i.account<>p_account or i.deleted_at is not null or i.hash<>p_hash or i.size<>p_size then
   return jsonb_build_object('error','image_id_reused');
  end if;
  return jsonb_build_object('image',to_jsonb(i));
 end if;
 select * into u from coordination.uploads where id=p_id;
 if found then
  if u.account<>p_account then return jsonb_build_object('error','not_found'); end if;
  if u.hash<>p_hash or u.size<>p_size or u.state<>'issued' then
   return jsonb_build_object('error','image_id_reused');
  end if;
 else
  select * into i from public.images where account=p_account and hash=p_hash and deleted_at is null order by id limit 1;
  if found then return jsonb_build_object('image',to_jsonb(i)); end if;
  if (select count(*) from coordination.uploads where account=p_account and issued_at>=date_trunc('day',t,'UTC'))>=p_daily or
     ((select count(*) from public.images where account=p_account and deleted_at is null) +
      (select count(*) from coordination.uploads where account=p_account and state='issued'))>=p_total then
   return jsonb_build_object('error','image_quota_exceeded');
  end if;
  insert into coordination.uploads(id,account,key,size,hash,issued_at,expires_at)
   values(p_id,p_account,'images/'||p_account||'/'||p_id,p_size,p_hash,t,t+make_interval(secs=>p_seconds))
   returning * into u;
 end if;
 return jsonb_build_object('upload',to_jsonb(u),'now',t);
end $$;

create function coordination.delete_image(p_account uuid,p_image uuid) returns jsonb
language plpgsql set search_path='' as $$
declare i public.images;
begin
 perform id from public.accounts where id=p_account for update;
 select * into i from public.images where id=p_image and account=p_account for update;
 if not found then return jsonb_build_object('error','not_found'); end if;
 if exists(select 1 from documents.images where image_id=p_image) or
    exists(select 1 from public.accounts where avatar_id=p_image) then
  return jsonb_build_object('error','image_referenced');
 end if;
 update public.images set deleted_at=coalesce(deleted_at,clock_timestamp()) where id=p_image;
 insert into coordination.uploads(id,account,key,size,hash,state,issued_at,expires_at)
  values(i.id,i.account,i.key,i.size,i.hash,'pending',i.created_at,clock_timestamp())
  on conflict(id) do update set state='pending';
 return to_jsonb(i);
end $$;

-- A volatile function sees the first space created by resolve_account earlier
-- in the same SQL command.
create function coordination.account_spaces(p_account uuid) returns setof documents.spaces
language sql volatile set search_path='' as $$
 select * from documents.spaces where owner=p_account order by id
$$;

revoke all on function coordination.checked_result(jsonb), coordination.create_space(uuid,uuid),
 coordination.lock_document(uuid,uuid), coordination.create_document(uuid,uuid,uuid,text),
 coordination.set_grant(uuid,uuid,uuid,text), coordination.document_lifecycle(uuid,uuid,boolean,integer),
 coordination.select_avatar(uuid,uuid), coordination.issue_image(uuid,uuid,integer,text,integer,integer,integer),
 coordination.delete_image(uuid,uuid), coordination.account_spaces(uuid) from public, anon, authenticated;
grant execute on function coordination.checked_result(jsonb), coordination.create_space(uuid,uuid),
 coordination.lock_document(uuid,uuid), coordination.create_document(uuid,uuid,uuid,text),
 coordination.set_grant(uuid,uuid,uuid,text), coordination.document_lifecycle(uuid,uuid,boolean,integer),
 coordination.select_avatar(uuid,uuid), coordination.issue_image(uuid,uuid,integer,text,integer,integer,integer),
 coordination.delete_image(uuid,uuid), coordination.account_spaces(uuid) to arctic_catalog;

create function coordination.stage_images(p_account uuid,p_batch uuid,p_document uuid,p_revision uuid,p_images uuid[])
returns jsonb language plpgsql set search_path='' as $$
declare d record; b coordination.batches;
begin
 select * into b from coordination.batches where id=p_batch and account=p_account for update;
 if not found then return jsonb_build_object('error','not_found'); end if;
 select * into d from coordination.lock_document(p_document,p_account);
 if not found then return jsonb_build_object('error','not_found'); end if;
 if d.role='viewer' then return jsonb_build_object('error','forbidden'); end if;
 if (d.document).trashed_at is not null then return jsonb_build_object('error','not_found'); end if;
 perform 1 from coordination.attempts a where a.batch=p_batch and a.document_id=p_document and a.revision=p_revision;
 if not found then return jsonb_build_object('error','not_found'); end if;
 if clock_timestamp()>=b.expires_at or not exists(
  select 1 from coordination.attempts a join documents.checkpoints c using(document_id,revision)
  where a.batch=p_batch and a.document_id=p_document and a.revision=p_revision and a.result='issued' and c.state='issued'
 ) then return jsonb_build_object('error','upload_slot_closed'); end if;
 insert into coordination.images(document_id,revision,image_id)
  select p_document,p_revision,unnest(p_images) on conflict do nothing;
 return '{}'::jsonb;
end $$;

create function coordination.retry_upload(p_account uuid,p_batch uuid,p_document uuid,p_previous uuid,p_base uuid,
 p_size integer,p_hash text,p_candidate uuid,p_seconds integer) returns jsonb
language plpgsql set search_path='' as $$
declare b coordination.batches; d record; a record; t timestamptz; rev uuid; obj text; retired jsonb;
begin
 select * into b from coordination.batches where id=p_batch and account=p_account for update;
 if not found then return jsonb_build_object('error','not_found'); end if;
 t=clock_timestamp();
 if t>=b.expires_at then return jsonb_build_object('error','batch_expired'); end if;
 select * into d from coordination.lock_document(p_document,p_account);
 if not found then return jsonb_build_object('error','not_found'); end if;
 if d.role='viewer' then return jsonb_build_object('error','forbidden'); end if;
 if (d.document).trashed_at is not null then return jsonb_build_object('error','not_found'); end if;
 select at.*,c.key,c.state,c.issued_at,c.published_at,i.refusals into a from coordination.attempts at
  join documents.checkpoints c using(document_id,revision)
  join coordination.items i on i.batch=at.batch and i.document_id=at.document_id
  where at.batch=p_batch and at.document_id=p_document order by at.revision desc limit 1;
 if not found then return jsonb_build_object('error','not_found'); end if;
 if a.refusals>=3 then return jsonb_build_object('error','retry_limit'); end if;
 if a.revision<>p_previous then
  -- A lost retry response returns the replacement slot that already committed.
  if a.size<>p_size or a.hash<>p_hash or a.base_revision is distinct from p_base then
   return jsonb_build_object('error','retry_changed');
  end if;
 else
  if a.published_at is not null then return jsonb_build_object('error','batch_item_published'); end if;
  if p_base is distinct from (d.document).revision then return jsonb_build_object('error','base_revision_changed'); end if;
  if a.state='issued' then
   update documents.checkpoints set state='pending' where document_id=p_document and revision=a.revision;
   update coordination.attempts set result='replaced' where document_id=p_document and revision=a.revision;
   retired=jsonb_build_object('document_id',p_document,'revision',a.revision,'key',a.key);
  end if;
  rev=coordination.next_revision(p_candidate,greatest(a.revision,(d.document).revision));
  obj='documents/'||(d.document).space||'/'||p_document||'/'||rev;
  insert into documents.checkpoints(document_id,revision,key,state,issued_at) values(p_document,rev,obj,'issued',t);
  insert into coordination.attempts(document_id,revision,batch,base_revision,size,hash,expires_at)
   values(p_document,rev,p_batch,p_base,p_size,p_hash,least(b.expires_at,t+make_interval(secs=>p_seconds)));
  select at.*,c.key,c.state,c.issued_at,c.published_at,i.refusals into a from coordination.attempts at
   join documents.checkpoints c using(document_id,revision)
   join coordination.items i on i.batch=at.batch and i.document_id=at.document_id
   where at.document_id=p_document and at.revision=rev;
 end if;
 return jsonb_build_object('document',to_jsonb(d.document)||jsonb_build_object('role',d.role),
  'attempt',to_jsonb(a),'now',t,'retired',retired);
end $$;

revoke all on function coordination.stage_images(uuid,uuid,uuid,uuid,uuid[]),
 coordination.retry_upload(uuid,uuid,uuid,uuid,uuid,integer,text,uuid,integer)
 from public, anon, authenticated;
grant execute on function coordination.stage_images(uuid,uuid,uuid,uuid,uuid[]),
 coordination.retry_upload(uuid,uuid,uuid,uuid,uuid,integer,text,uuid,integer) to arctic_catalog;

create function coordination.claim_cleanup(p_trash_days integer,p_upload_seconds integer) returns jsonb
language plpgsql set search_path='' as $$
declare t timestamptz; cutoff timestamptz; trash_cutoff timestamptz; trash uuid[];
 checkpoints jsonb; images jsonb;
begin
 t=clock_timestamp(); cutoff=t-make_interval(secs=>p_upload_seconds);
 trash_cutoff=t-make_interval(days=>p_trash_days);
 select array(select id from documents.documents where trashed_at<=trash_cutoff
  order by trashed_at,id limit 4 for update skip locked) into trash;
 update documents.documents set revision=null,key=null,published_at=null where id=any(trash);
 update documents.checkpoints set state='pending' where document_id=any(trash) and state in ('issued','published');
 with expired as (
  select document_id,revision from documents.checkpoints where state='issued' and issued_at<=cutoff
  order by issued_at limit 16 for update skip locked
 )
 update documents.checkpoints c set state='pending' from expired e
 where c.document_id=e.document_id and c.revision=e.revision;
 with expired as (
  select id from coordination.uploads where state='issued' and issued_at<=cutoff
  order by issued_at limit 16 for update skip locked
 )
 update coordination.uploads u set state='pending' from expired e where u.id=e.id;
 with jobs as (
  select c.document_id,c.revision,c.key,c.issued_at<=cutoff as expired,c.published_at is null as unpublished
  from documents.checkpoints c left join coordination.attempts a using(document_id,revision)
  where c.cleanup_after<=clock_timestamp()
   and (c.state='pending' or (c.state='deleted' and c.issued_at<=cutoff and a.reaped_at is null))
   and not exists(select 1 from documents.documents d where d.id=c.document_id and d.revision=c.revision)
  order by c.issued_at,c.document_id,c.revision limit 8 for update of c skip locked
 ), claimed as (
  update documents.checkpoints c set cleanup_after=clock_timestamp()+interval '30 seconds'
  from jobs j where c.document_id=j.document_id and c.revision=j.revision returning j.*
 )
 select coalesce(jsonb_agg(claimed),'[]') into checkpoints from claimed;
 with jobs as (
  select id,key,issued_at<=cutoff as expired from coordination.uploads where cleanup_after<=clock_timestamp()
   and (state='pending' or (state='deleted' and issued_at<=cutoff))
  order by issued_at,id limit 8 for update skip locked
 ), claimed as (
  update coordination.uploads u set cleanup_after=clock_timestamp()+interval '30 seconds'
  from jobs j where u.id=j.id returning j.*
 )
 select coalesce(jsonb_agg(claimed),'[]') into images from claimed;
 return jsonb_build_object('checkpoints',checkpoints,'images',images,'cutoff',cutoff,'trash_cutoff',trash_cutoff);
end $$;

create function coordination.defer_cleanup(p_checkpoints jsonb,p_images jsonb) returns void
language plpgsql set search_path='' as $$
begin
 update documents.checkpoints c set cleanup_failures=cleanup_failures+1,
  cleanup_after=clock_timestamp()+make_interval(secs=>least(300,30*power(2,least(cleanup_failures,4)))::integer)
 from jsonb_to_recordset(p_checkpoints) j(document_id uuid,revision uuid)
 where c.document_id=j.document_id and c.revision=j.revision and c.state in ('pending','deleted');
 update coordination.uploads u set cleanup_failures=cleanup_failures+1,
  cleanup_after=clock_timestamp()+make_interval(secs=>least(300,30*power(2,least(cleanup_failures,4)))::integer)
 from jsonb_to_recordset(p_images) j(id uuid) where u.id=j.id and u.state in ('pending','deleted');
end $$;

create function coordination.finish_cleanup(p_checkpoints jsonb,p_images jsonb) returns void
language plpgsql set search_path='' as $$
begin
 delete from documents.checkpoints c using jsonb_to_recordset(p_checkpoints)
  j(document_id uuid,revision uuid,expired boolean,unpublished boolean)
 where c.document_id=j.document_id and c.revision=j.revision and j.expired and j.unpublished
  and c.state in ('pending','deleted');
 update documents.checkpoints c set state='deleted',cleanup_after='-infinity',cleanup_failures=0
 from jsonb_to_recordset(p_checkpoints) j(document_id uuid,revision uuid)
 where c.document_id=j.document_id and c.revision=j.revision and c.state='pending';
 update coordination.attempts a set reaped_at=clock_timestamp()
 from jsonb_to_recordset(p_checkpoints) j(document_id uuid,revision uuid,expired boolean)
 where a.document_id=j.document_id and a.revision=j.revision and j.expired;
 delete from coordination.uploads u using jsonb_to_recordset(p_images) j(id uuid,expired boolean)
 where u.id=j.id and j.expired and u.state in ('pending','deleted');
 update coordination.uploads u set state='deleted',cleanup_after='-infinity',cleanup_failures=0
 from jsonb_to_recordset(p_images) j(id uuid) where u.id=j.id and u.state='pending';
end $$;

create function coordination.purge_cleanup(p_cutoff timestamptz,p_trash_cutoff timestamptz) returns integer
language plpgsql set search_path='' as $$
declare purged integer;
begin
 delete from documents.documents d where d.id in (
  select id from documents.documents where trashed_at<=p_trash_cutoff order by trashed_at,id limit 4
 ) and not exists(select 1 from documents.checkpoints c where c.document_id=d.id and c.state<>'deleted');
 get diagnostics purged=row_count;
 delete from coordination.uploads where state='confirmed' and issued_at<=p_cutoff;
 return purged;
end $$;

revoke all on function coordination.claim_cleanup(integer,integer), coordination.defer_cleanup(jsonb,jsonb),
 coordination.finish_cleanup(jsonb,jsonb), coordination.purge_cleanup(timestamptz,timestamptz) from public, anon, authenticated;
grant execute on function coordination.claim_cleanup(integer,integer), coordination.defer_cleanup(jsonb,jsonb),
 coordination.finish_cleanup(jsonb,jsonb), coordination.purge_cleanup(timestamptz,timestamptz) to arctic_catalog;

-- Additive native-operation functions. Never run the fresh catalog setup here.
-- VOLATILE PL/pgSQL uses a new READ COMMITTED snapshot for each internal
-- statement. Locks and permission reads deliberately remain separate statements.
create function coordination.resolve_account(p_issuer text,p_subject text,p_id uuid,p_space uuid)
returns setof public.accounts language plpgsql volatile set search_path='' as $$
declare a public.accounts;
begin
 select * into a from public.accounts where issuer=p_issuer and subject=p_subject;
 if not found then
  insert into public.accounts(id,issuer,subject) values(p_id,p_issuer,p_subject)
   on conflict(issuer,subject) do nothing returning * into a;
  if found then
   insert into documents.spaces(id,owner) values(p_space,a.id);
  else
   select * into strict a from public.accounts where issuer=p_issuer and subject=p_subject;
  end if;
 end if;
 return next a;
end $$;

create function coordination.next_revision(candidate uuid,previous uuid)
returns uuid language sql immutable set search_path='' as $$
 select case when previous is not null and candidate<=previous then
  (lpad(to_hex((('x'||substr(replace(previous::text,'-',''),1,12))::bit(48)::bigint)+1),12,'0')
    ||substr(replace(candidate::text,'-',''),13))::uuid else candidate end
$$;

create function coordination.reserve_batch(p_account uuid,p_batch uuid,p_items jsonb,p_seconds integer)
returns jsonb language plpgsql volatile set search_path='' as $$
declare a public.accounts; b coordination.batches; t timestamptz; n timestamptz;
 item jsonb; d record; existing record; rev uuid; obj text; expiry timestamptz;
 output jsonb='[]';
begin
 if jsonb_array_length(p_items) not between 1 and 8 or p_seconds not between 30 and 900 then
  raise exception using errcode='22023',message='invalid reservation';
 end if;
 select * into a from public.accounts where id=p_account for update;
 if not found then return jsonb_build_object('error','not_found'); end if;
 t=clock_timestamp();
 select * into b from coordination.batches where id=p_batch;
 if found then
  if b.account<>p_account then return jsonb_build_object('error','not_found'); end if;
  if t>=b.expires_at then return jsonb_build_object('error','batch_expired'); end if;
 else
  n=a.last_sync+interval '5 minutes';
  if t<n then return jsonb_build_object('error','sync_window','next_start',n,'now',t); end if;
  insert into coordination.batches values(p_batch,p_account,t,t+interval '24 hours') returning * into b;
  update public.accounts set last_sync=t where id=p_account;
 end if;
 perform id from documents.documents where id in(select (x->>'document_id')::uuid from jsonb_array_elements(p_items) x) order by id for update;
 -- This is a fresh statement after all document lock waits.
 for item in select x from jsonb_array_elements(p_items) x loop
  select doc.*,g.role into d from documents.documents doc join documents.grants g on g.document_id=doc.id
   where doc.id=(item->>'document_id')::uuid and g.account_id=p_account;
  if not found or d.trashed_at is not null then return jsonb_build_object('error','not_found'); end if;
  if d.role='viewer' then return jsonb_build_object('error','forbidden'); end if;
  select at.*,c.key,c.state,c.issued_at,c.published_at,i.refusals into existing
   from coordination.attempts at join documents.checkpoints c using(document_id,revision)
   join coordination.items i on i.batch=at.batch and i.document_id=at.document_id
   where at.batch=p_batch and at.document_id=d.id order by at.revision desc limit 1;
  if found then
   if existing.size<>(item->>'size')::integer or existing.hash<>item->>'hash' then
    return jsonb_build_object('error','batch_item_changed');
   end if;
  else
   if t>=b.started_at+interval '5 minutes' then return jsonb_build_object('error','batch_sealed'); end if;
   rev=coordination.next_revision((item->>'candidate')::uuid,d.revision);
   obj='documents/'||d.space||'/'||d.id||'/'||rev;
   expiry=least(b.expires_at,t+make_interval(secs=>p_seconds));
   insert into coordination.items(batch,document_id) values(p_batch,d.id);
   insert into documents.checkpoints(document_id,revision,key,state,issued_at) values(d.id,rev,obj,'issued',t);
   insert into coordination.attempts(document_id,revision,batch,base_revision,size,hash,expires_at)
    values(d.id,rev,p_batch,d.revision,(item->>'size')::integer,item->>'hash',expiry);
   select at.*,c.key,c.state,c.issued_at,c.published_at,0 as refusals into existing
    from coordination.attempts at join documents.checkpoints c using(document_id,revision)
    where at.document_id=d.id and at.revision=rev;
  end if;
  output=output||jsonb_build_array(jsonb_build_object('document',to_jsonb(d),'attempt',to_jsonb(existing)));
 end loop;
 return jsonb_build_object('batch',to_jsonb(b),'now',t,'items',output);
end $$;

create function coordination.publish_preflight(p_account uuid,p_batch uuid,p_items jsonb)
returns jsonb language plpgsql volatile set search_path='' as $$
declare output jsonb;
begin
 if not exists(select 1 from coordination.batches where id=p_batch and account=p_account) then
  return jsonb_build_object('error','not_found');
 end if;
 select coalesce(jsonb_agg(to_jsonb(at)||jsonb_build_object('key',c.key,'state',c.state,'issued_at',c.issued_at,
   'published_at',c.published_at,'refusals',i.refusals,'permitted',g.role in ('owner','editor') and d.trashed_at is null)), '[]')
 into output from coordination.attempts at join documents.checkpoints c using(document_id,revision)
 join coordination.items i on i.batch=at.batch and i.document_id=at.document_id
 join documents.documents d on d.id=at.document_id
 left join documents.grants g on g.document_id=d.id and g.account_id=p_account
 join jsonb_to_recordset(p_items) as requested(document_id uuid,revision uuid) on requested.document_id=at.document_id and requested.revision=at.revision
 where at.batch=p_batch;
 return jsonb_build_object('attempts',output);
end $$;

create function coordination.publish_verified_batch(p_account uuid,p_batch uuid,p_items jsonb)
returns jsonb language plpgsql volatile set search_path='' as $$
declare b coordination.batches; item jsonb; d record; a record; t timestamptz;
 reason text; allowed boolean; images_ok boolean; moved uuid; refusals integer;
 outcomes jsonb='[]'; pending jsonb='[]'; retired record; current_document jsonb;
begin
 select * into b from coordination.batches where id=p_batch and account=p_account for update;
 if not found then return jsonb_build_object('error','not_found'); end if;
 perform id from documents.documents where id in(select (x->>'document_id')::uuid from jsonb_array_elements(p_items) x) order by id for update;
 perform c.document_id,c.revision from documents.checkpoints c
  join jsonb_to_recordset(p_items) as x(document_id uuid,revision uuid) on x.document_id=c.document_id and x.revision=c.revision
  order by c.document_id,c.revision for update of c;
 t=clock_timestamp();
 for item in select x from jsonb_array_elements(p_items) x order by (x->>'document_id')::uuid loop
  -- Separate statements after lock waits are essential for revocation/cleanup.
  select doc.*,g.role into d from documents.documents doc left join documents.grants g on g.document_id=doc.id and g.account_id=p_account
   where doc.id=(item->>'document_id')::uuid;
  if not found then return jsonb_build_object('error','not_found'); end if;
  select at.*,c.key,c.state,c.issued_at,c.published_at,i.refusals into a
   from coordination.attempts at join documents.checkpoints c using(document_id,revision)
   join coordination.items i on i.batch=at.batch and i.document_id=at.document_id
   where at.batch=p_batch and at.document_id=d.id and at.revision=(item->>'revision')::uuid;
  if not found then return jsonb_build_object('error','not_found'); end if;
  if a.key<>item->>'key' or a.base_revision is distinct from (item->>'base_revision')::uuid then
   return jsonb_build_object('error','upload_slot_mismatch');
  end if;
  allowed=coalesce(d.role in ('owner','editor') and d.trashed_at is null,false);
  reason=null; refusals=a.refusals;
  if not allowed then reason='forbidden';
  elsif a.published_at is not null then reason=null;
  elsif a.result<>'issued' or a.state<>'issued' then
   reason=case when a.result in ('conflict','forbidden','invalid','expired') then a.result else 'expired' end;
  elsif t>=b.expires_at or t>=a.issued_at+interval '24 hours' then reason='expired';
  else
   insert into coordination.images(document_id,revision,image_id)
    select d.id,a.revision,value::uuid from jsonb_array_elements_text(item->'images') on conflict do nothing;
   with locked as materialized (
    select i.id,i.account,i.deleted_at from public.images i join coordination.images r on r.image_id=i.id
    where r.document_id=d.id and r.revision=a.revision order by i.id for share of i)
   select count(*)=(select count(*) from coordination.images where document_id=d.id and revision=a.revision)
    and coalesce(bool_and(i.deleted_at is null and (
     exists(select 1 from documents.images r where r.document_id=d.id and r.image_id=i.id)
     or exists(select 1 from documents.grants g where g.document_id=d.id and g.account_id=i.account and g.role in ('owner','editor')))),true)
   into images_ok from locked i;
   if not coalesce((item->>'verified')::boolean,false) or not images_ok then reason='invalid';
   else
    update documents.documents set revision=a.revision,key=a.key,published_at=t,updated_at=t
     where id=d.id and revision is not distinct from a.base_revision and trashed_at is null returning id into moved;
    if not found then reason='conflict'; end if;
   end if;
  end if;
  if reason is not null then
   if a.state='issued' and a.result='issued' then
    update documents.checkpoints set state='pending' where document_id=d.id and revision=a.revision;
    update coordination.attempts set result=reason where document_id=d.id and revision=a.revision;
    update coordination.items i set refusals=least(i.refusals+1,3) where i.batch=p_batch and i.document_id=d.id;
    refusals=a.refusals+1;
    pending=pending||jsonb_build_array(jsonb_build_object('document_id',d.id,'revision',a.revision,'key',a.key));
   elsif a.state='pending' then
    pending=pending||jsonb_build_array(jsonb_build_object('document_id',d.id,'revision',a.revision,'key',a.key));
   end if;
  elsif a.published_at is null then
   update documents.checkpoints set state='published',published_at=t where document_id=d.id and revision=a.revision;
   update coordination.attempts set result='published' where document_id=d.id and revision=a.revision;
   insert into documents.images(document_id,image_id) select document_id,image_id from coordination.images
    where document_id=d.id and revision=a.revision on conflict do nothing;
   delete from coordination.images where document_id=d.id and revision=a.revision;
   for retired in with ranked as (
    select revision,row_number() over(order by revision desc) as position from documents.checkpoints where document_id=d.id and state='published')
    update documents.checkpoints c set state='pending' from ranked r where c.document_id=d.id and c.revision=r.revision and r.position>3
    returning c.document_id,c.revision,c.key loop
     pending=pending||jsonb_build_array(to_jsonb(retired));
   end loop;
   d.revision=a.revision; d.key=a.key; d.published_at=t; d.updated_at=t;
  else
   -- Lost-response retries also recover deletion work from a committed publish.
   for retired in select c.document_id,c.revision,c.key from documents.checkpoints c where c.document_id=d.id and c.state='pending' order by c.revision limit 1 loop
    pending=pending||jsonb_build_array(to_jsonb(retired));
   end loop;
  end if;
  current_document=case when allowed then to_jsonb(d) else null end;
  outcomes=outcomes||jsonb_build_array(jsonb_build_object('document_id',d.id,'revision',a.revision,
   'result',coalesce(reason,'published'),'document',current_document,'retry_allowed',coalesce(reason in ('conflict','invalid') and refusals<3 and t<b.expires_at,false)));
 end loop;
 return jsonb_build_object('outcomes',outcomes,'pending',pending);
end $$;

-- Expiring claims suppress duplicate Scheduler delivery while allowing recovery
-- after process death. Pending exact keys remain the durable source of truth.
alter table documents.checkpoints add column cleanup_after timestamptz not null default '-infinity';
alter table coordination.uploads add column cleanup_after timestamptz not null default '-infinity';
alter table documents.checkpoints add column cleanup_failures integer not null default 0;
alter table coordination.uploads add column cleanup_failures integer not null default 0;

create function coordination.open_pointer(p_issuer text,p_subject text,p_id uuid,p_space uuid,p_document uuid)
returns jsonb language plpgsql volatile set search_path='' as $$
declare a public.accounts; output jsonb;
begin
 select * into strict a from coordination.resolve_account(p_issuer,p_subject,p_id,p_space);
 select to_jsonb(d)||jsonb_build_object('role',g.role) into output from documents.documents d
  join documents.grants g on g.document_id=d.id where d.id=p_document and g.account_id=a.id and d.trashed_at is null;
 return output;
end $$;

create function coordination.rename_document(p_account uuid,p_document uuid,p_title text)
returns jsonb language plpgsql volatile set search_path='' as $$
declare r text; output jsonb;
begin
 perform id from documents.documents where id=p_document for update;
 select g.role into r from documents.documents d join documents.grants g on g.document_id=d.id
  where d.id=p_document and g.account_id=p_account and d.trashed_at is null;
 if not found then return jsonb_build_object('error','not_found'); end if;
 if r='viewer' then return jsonb_build_object('error','forbidden'); end if;
 update documents.documents d set title=p_title,updated_at=clock_timestamp() where d.id=p_document
  returning to_jsonb(d)||jsonb_build_object('role',r) into output;
 return jsonb_build_object('document',output);
end $$;

create function coordination.image_preflight(p_account uuid,p_image uuid)
returns jsonb language plpgsql volatile set search_path='' as $$
declare i public.images; u coordination.uploads;
begin
 select * into i from public.images where id=p_image and account=p_account and deleted_at is null;
 if found then return jsonb_build_object('image',to_jsonb(i)); end if;
 select * into u from coordination.uploads where id=p_image and account=p_account;
 if not found then return jsonb_build_object('error','not_found'); end if;
 if u.state<>'issued' or clock_timestamp()>=u.issued_at+interval '24 hours' then
  return jsonb_build_object('error','image_upload_expired');
 end if;
 return jsonb_build_object('upload',to_jsonb(u));
end $$;

create function coordination.confirm_verified_image(p_account uuid,p_image uuid,p_key text,p_hash text,p_size integer,p_issued timestamptz,p_valid boolean)
returns jsonb language plpgsql volatile set search_path='' as $$
declare state jsonb; u coordination.uploads; i public.images;
begin
 perform id from public.accounts where id=p_account for update;
 perform id from coordination.uploads where id=p_image and account=p_account for update;
 state=coordination.image_preflight(p_account,p_image);
 if state ? 'error' or state ? 'image' then return state; end if;
 select * into strict u from coordination.uploads where id=p_image and account=p_account;
 if (u.key,u.hash,u.size,u.issued_at) is distinct from (p_key,p_hash,p_size,p_issued) then
  return jsonb_build_object('error','image_upload_changed');
 end if;
 if not p_valid then
  update coordination.uploads set state='pending' where id=p_image;
  return jsonb_build_object('invalid',true);
 end if;
 insert into public.images(id,account,key,size,hash) values(u.id,u.account,u.key,u.size,u.hash) returning * into i;
 update coordination.uploads set state='confirmed' where id=p_image;
 return jsonb_build_object('image',to_jsonb(i));
end $$;

revoke execute on all functions in schema coordination from public,anon,authenticated;
grant execute on function coordination.resolve_account(text,text,uuid,uuid),coordination.next_revision(uuid,uuid),
 coordination.reserve_batch(uuid,uuid,jsonb,integer),coordination.publish_preflight(uuid,uuid,jsonb),coordination.publish_verified_batch(uuid,uuid,jsonb),
 coordination.open_pointer(text,text,uuid,uuid,uuid),coordination.rename_document(uuid,uuid,text),coordination.image_preflight(uuid,uuid),
 coordination.confirm_verified_image(uuid,uuid,text,text,integer,timestamptz,boolean) to arctic_catalog;

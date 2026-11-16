"""Coordinate whole-file uploads without accessing document content."""

from datetime import datetime, timedelta
from math import ceil
from typing import Literal
from uuid import UUID

from pydantic import UUID7, AwareDatetime

from app.schemas import (
    BatchResponse,
    Pointer,
    PublishBatch,
    PublishResponse,
    PublishResult,
    RetryUpload,
    StartBatch,
    UploadSlot,
)
from app.services.database import Database, Session
from app.services.documents.repository import document_for, documents_for
from app.services.errors import Conflict, NotFound, ServiceError, Unavailable
from app.services.identifiers import checkpoint_key, new_revision
from app.services.limits import (
    CHECKPOINTS_RETAINED,
    MAX_CHECKPOINT_BYTES,
    MAX_REFUSALS,
    SYNC_WINDOW_SECONDS,
    UPLOAD_TIMEOUT_SECONDS,
)
from app.services.models import Account, CheckpointState, Document, Record
from app.services.storage.objects import (
    CheckpointObjects,
    ObjectMissing,
    StorageUnavailable,
)
from app.services.storage.signing import S3Signer
from app.services.time import database_time

type AttemptResult = Literal[
    "issued", "published", "conflict", "forbidden", "invalid", "expired", "replaced"
]
type Refusal = Literal["conflict", "forbidden", "invalid", "expired"]


class Batch(Record):
    id: UUID7
    account: UUID7
    started_at: AwareDatetime
    expires_at: AwareDatetime


class Attempt(Record):
    document_id: UUID7
    revision: UUID7
    batch: UUID7
    base_revision: UUID7 | None
    size: int
    hash: str
    expires_at: AwareDatetime
    result: AttemptResult
    key: str
    state: CheckpointState
    issued_at: AwareDatetime
    published_at: AwareDatetime | None
    refusals: int


def batch_for(
    session: Session, account: Account, batch_id: UUID, *, lock: bool = False
) -> Batch:
    rows = session.execute(
        "select * from coordination.batches where id=%s and account=%s"
        + (" for update" if lock else ""),
        (batch_id, account.id),
    )
    if not rows:
        raise NotFound()
    return Batch.model_validate(rows[0])


def attempt_for(
    session: Session, batch_id: UUID, document_id: UUID, revision: UUID | None = None
) -> Attempt | None:
    return attempts_for(
        session, batch_id, [document_id], [revision] if revision is not None else None
    ).get(document_id)


def attempts_for(
    session: Session,
    batch_id: UUID,
    document_ids: list[UUID],
    revisions: list[UUID] | None = None,
) -> dict[UUID, Attempt]:
    query = """select distinct on(a.document_id) a.*,c.key,c.state,c.issued_at,c.published_at,i.refusals
        from coordination.attempts a
        join documents.checkpoints c using(document_id,revision)
        join coordination.items i on i.batch=a.batch and i.document_id=a.document_id
        where a.batch=%s and a.document_id=any(%s::uuid[])"""
    parameters: list[object] = [batch_id, document_ids]
    if revisions is not None:
        query += " and (a.document_id,a.revision) in (select * from unnest(%s::uuid[],%s::uuid[]))"
        parameters.extend([document_ids, revisions])
    rows = session.execute(
        query + " order by a.document_id,a.revision desc", parameters
    )
    return {
        attempt.document_id: attempt for attempt in map(Attempt.model_validate, rows)
    }


class SyncService:
    def __init__(
        self,
        database: Database,
        objects: CheckpointObjects,
        signer: S3Signer,
        *,
        presign_seconds: int = 300,
    ) -> None:
        self._database = database
        self._objects = objects
        self._signer = signer
        self._seconds = presign_seconds

    def pointer(
        self, document: Document, known_revision: UUID | None = None
    ) -> Pointer:
        read = None
        if document.revision is not None and document.revision != known_revision:
            assert document.key is not None
            read = self._signer.permission(document.key, self._seconds, method="GET")
        return Pointer(
            document_id=document.id,
            revision=document.revision,
            key=document.key,
            published_at=document.published_at,
            read=read,
        )

    def open(
        self, account: Account, document_id: UUID, known_revision: UUID | None
    ) -> Pointer:
        with self._database.transaction() as session:
            return self.pointer(
                document_for(session, account, document_id), known_revision
            )

    def checkpoints(self, account: Account, document_id: UUID) -> list[Pointer]:
        with self._database.transaction() as session:
            document_for(session, account, document_id)
            rows = session.execute(
                """select document_id,revision,key,published_at
                from documents.checkpoints where document_id=%s and state='published'
                order by revision desc limit %s""",
                (document_id, CHECKPOINTS_RETAINED),
            )
            records = [_Published.model_validate(row) for row in rows]
            return [
                Pointer(
                    document_id=value.document_id,
                    revision=value.revision,
                    key=value.key,
                    published_at=value.published_at,
                    read=self._signer.permission(
                        value.key, self._seconds, method="GET"
                    ),
                )
                for value in records
            ]

    def stage_images(
        self,
        account: Account,
        batch_id: UUID,
        document_id: UUID,
        revision: UUID,
        images: list[UUID],
    ) -> None:
        with self._database.transaction() as session:
            batch = batch_for(session, account, batch_id, lock=True)
            document_for(session, account, document_id, edit=True, lock=True)
            attempt = attempt_for(session, batch_id, document_id, revision)
            if attempt is None:
                raise NotFound()
            if (
                attempt.state != "issued"
                or attempt.result != "issued"
                or database_time(session) >= batch.expires_at
            ):
                raise Conflict("upload_slot_closed")
            self._stage_images(session, document_id, revision, images)

    def _stage_images(
        self, session: Session, document_id: UUID, revision: UUID, images: list[UUID]
    ) -> None:
        if images:
            session.execute(
                """insert into coordination.images(document_id,revision,image_id)
                select %s,%s,unnest(%s::uuid[]) on conflict do nothing""",
                (document_id, revision, images),
            )

    def _slot(self, attempt: Attempt, document: Document, now: datetime) -> UploadSlot:
        seconds = min(self._seconds, int((attempt.expires_at - now).total_seconds()))
        permission = (
            self._signer.permission(
                attempt.key,
                seconds,
                method="PUT",
                size=attempt.size,
                sha256=attempt.hash,
                media_type="application/octet-stream",
                now=now,
            )
            if attempt.state == "issued" and attempt.result == "issued" and seconds > 0
            else None
        )
        return UploadSlot(
            document_id=document.id,
            current=self.pointer(document),
            base_revision=attempt.base_revision,
            revision=attempt.revision,
            key=attempt.key,
            upload=permission,
            refusals=attempt.refusals,
            result=attempt.result,
        )

    def _issue(
        self,
        session: Session,
        batch: Batch,
        document: Document,
        size: int,
        digest: str,
        now: datetime,
        *,
        previous: UUID | None = None,
    ) -> Attempt:
        lower_bound = document.revision
        if previous is not None and (lower_bound is None or previous > lower_bound):
            lower_bound = previous
        revision = new_revision(lower_bound)
        key = checkpoint_key(document.space, document.id, revision)
        expires = min(batch.expires_at, now + timedelta(seconds=self._seconds))
        session.execute(
            """insert into documents.checkpoints(document_id,revision,key,state,issued_at)
                values(%s,%s,%s,'issued',%s)""",
            (document.id, revision, key, now),
        )
        session.execute(
            """insert into coordination.attempts(document_id,revision,batch,base_revision,size,hash,expires_at)
                values(%s,%s,%s,%s,%s,%s,%s)""",
            (document.id, revision, batch.id, document.revision, size, digest, expires),
        )
        attempt = attempt_for(session, batch.id, document.id, revision)
        assert attempt is not None
        return attempt

    def start(self, account: Account, command: StartBatch) -> BatchResponse:
        with self._database.transaction() as session:
            # Account locking makes the window global across devices and isolates.
            accounts = session.execute(
                "select * from public.accounts where id=%s for update", (account.id,)
            )
            if not accounts:
                raise NotFound()
            current_account = Account.model_validate(accounts[0])
            now = database_time(session)
            existing = session.execute(
                "select * from coordination.batches where id=%s", (command.id,)
            )
            if existing:
                batch = Batch.model_validate(existing[0])
                if batch.account != account.id:
                    raise NotFound()
                if now >= batch.expires_at:
                    raise ServiceError("batch_expired", "This batch has expired.", 410)
            else:
                if current_account.last_sync is not None:
                    next_start = current_account.last_sync + timedelta(
                        seconds=SYNC_WINDOW_SECONDS
                    )
                    if now < next_start:
                        raise ServiceError(
                            "sync_window",
                            f"The next batch may start at {next_start.isoformat()}.",
                            429,
                            headers={
                                "Retry-After": str(
                                    max(1, ceil((next_start - now).total_seconds()))
                                ),
                                "X-Next-Sync": next_start.isoformat(),
                            },
                        )
                batch = Batch.model_validate(
                    session.execute(
                        "insert into coordination.batches(id,account,started_at,expires_at) values(%s,%s,%s,%s) returning *",
                        (
                            command.id,
                            account.id,
                            now,
                            now + timedelta(seconds=UPLOAD_TIMEOUT_SECONDS),
                        ),
                    )[0]
                )
                session.execute(
                    "update public.accounts set last_sync=%s where id=%s",
                    (now, account.id),
                )
            # All multi-document mutations acquire document locks in UUID order.
            document_ids = [item.document_id for item in command.documents]
            documents = documents_for(
                session, account, document_ids, lock=True, edit=True
            )
            attempts = attempts_for(session, batch.id, document_ids)
            issued: list[Attempt] = []
            for item in command.documents:
                document = documents[item.document_id]
                attempt = attempts.get(document.id)
                if attempt is not None:
                    if attempt.hash != item.hash or attempt.size != item.size:
                        raise Conflict("batch_item_changed")
                else:
                    # Large dirty libraries use several bounded calls with the
                    # same batch ID, all inside the original admission window.
                    if now >= batch.started_at + timedelta(seconds=SYNC_WINDOW_SECONDS):
                        raise ServiceError(
                            "batch_sealed",
                            "New documents must join within the batch admission window.",
                            410,
                        )
                    revision = new_revision(document.revision)
                    attempt = Attempt(
                        document_id=document.id,
                        revision=revision,
                        batch=batch.id,
                        base_revision=document.revision,
                        size=item.size,
                        hash=item.hash,
                        expires_at=min(
                            batch.expires_at, now + timedelta(seconds=self._seconds)
                        ),
                        result="issued",
                        key=checkpoint_key(document.space, document.id, revision),
                        state="issued",
                        issued_at=now,
                        published_at=None,
                        refusals=0,
                    )
                    issued.append(attempt)
                    attempts[document.id] = attempt
            if issued:
                ids = [attempt.document_id for attempt in issued]
                revisions = [attempt.revision for attempt in issued]
                session.execute(
                    "insert into coordination.items(batch,document_id) select %s,unnest(%s::uuid[])",
                    (batch.id, ids),
                )
                session.execute(
                    """insert into documents.checkpoints(document_id,revision,key,state,issued_at)
                    select document_id,revision,key,'issued',%s from unnest(%s::uuid[],%s::uuid[],%s::text[]) as slots(document_id,revision,key)""",
                    (now, ids, revisions, [attempt.key for attempt in issued]),
                )
                session.execute(
                    """insert into coordination.attempts(document_id,revision,batch,base_revision,size,hash,expires_at)
                    select document_id,revision,%s,base_revision,size,hash,%s
                    from unnest(%s::uuid[],%s::uuid[],%s::uuid[],%s::integer[],%s::text[])
                    as slots(document_id,revision,base_revision,size,hash)""",
                    (
                        batch.id,
                        issued[0].expires_at,
                        ids,
                        revisions,
                        [attempt.base_revision for attempt in issued],
                        [attempt.size for attempt in issued],
                        [attempt.hash for attempt in issued],
                    ),
                )
            return BatchResponse(
                id=batch.id,
                started_at=batch.started_at,
                expires_at=batch.expires_at,
                documents=[
                    self._slot(
                        attempts[item.document_id], documents[item.document_id], now
                    )
                    for item in command.documents
                ],
            )

    def retry(
        self, account: Account, batch_id: UUID, document_id: UUID, command: RetryUpload
    ) -> UploadSlot:
        retired: Attempt | None = None
        with self._database.transaction() as session:
            batch = batch_for(session, account, batch_id, lock=True)
            now = database_time(session)
            if now >= batch.expires_at:
                raise ServiceError("batch_expired", "This batch has expired.", 410)
            document = document_for(session, account, document_id, lock=True, edit=True)
            current = attempt_for(session, batch.id, document.id)
            if current is None:
                raise NotFound()
            if current.refusals >= MAX_REFUSALS:
                raise ServiceError(
                    "retry_limit",
                    "Three uploads were refused. Wait for the next save trigger.",
                    429,
                )
            if current.revision != command.previous_revision:
                # A dropped retry response must return its previously issued slot.
                if (
                    current.size == command.size
                    and current.hash == command.hash
                    and current.base_revision == command.base_revision
                ):
                    return self._slot(current, document, now)
                raise Conflict("retry_changed")
            if current.published_at is not None:
                raise Conflict("batch_item_published")
            if command.base_revision != document.revision:
                raise Conflict("base_revision_changed")
            if current.state == "issued":
                session.execute(
                    "update documents.checkpoints set state='pending' where document_id=%s and revision=%s",
                    (document.id, current.revision),
                )
                session.execute(
                    "update coordination.attempts set result='replaced' where document_id=%s and revision=%s",
                    (document.id, current.revision),
                )
                retired = current
            attempt = self._issue(
                session,
                batch,
                document,
                command.size,
                command.hash,
                now,
                previous=current.revision,
            )
            slot = self._slot(attempt, document, now)
        if retired is not None:
            self.delete_pending([(retired.document_id, retired.revision, retired.key)])
        return slot

    def _uploaded(self, attempt: Attempt) -> bool:
        try:
            metadata = self._objects.head(attempt.key)
        except ObjectMissing:
            return False
        return (
            0 < metadata.size <= MAX_CHECKPOINT_BYTES
            and metadata.size == attempt.size
            and metadata.media_type == "application/octet-stream"
            and metadata.metadata.get("sha256") == attempt.hash
        )

    def _images_allowed(
        self, session: Session, document_id: UUID, revision: UUID
    ) -> bool:
        # Aggregate in Postgres so even large staged manifests need one scalar
        # result. Share locks serialize publication against image deletion.
        rows = session.execute(
            """with locked as materialized (
            select i.id,i.account,i.deleted_at from public.images i
            join coordination.images r on r.image_id=i.id
            where r.document_id=%s and r.revision=%s order by i.id for share of i)
            select count(*)=(select count(*) from coordination.images where document_id=%s and revision=%s)
              and coalesce(bool_and(i.deleted_at is null and (
                exists(select 1 from documents.images r where r.document_id=%s and r.image_id=i.id)
                or exists(select 1 from documents.grants g where g.document_id=%s
                  and g.account_id=i.account and g.role in ('owner','editor')))),true) as allowed
            from locked i""",
            (document_id, revision, document_id, revision, document_id, document_id),
        )
        return rows[0]["allowed"] is True

    def _refuse(
        self, session: Session, batch_id: UUID, refusals: list[tuple[Attempt, Refusal]]
    ) -> None:
        ids = [attempt.document_id for attempt, _ in refusals]
        revisions = [attempt.revision for attempt, _ in refusals]
        session.execute(
            """update documents.checkpoints set state='pending'
            where (document_id,revision) in(select * from unnest(%s::uuid[],%s::uuid[])) and state='issued'""",
            (ids, revisions),
        )
        session.execute(
            """update coordination.attempts a set result=refused.result from
            unnest(%s::uuid[],%s::uuid[],%s::text[]) as refused(document_id,revision,result)
            where a.document_id=refused.document_id and a.revision=refused.revision and a.result='issued'""",
            (ids, revisions, [reason for _, reason in refusals]),
        )
        session.execute(
            "update coordination.items set refusals=least(refusals+1,3) where batch=%s and document_id=any(%s::uuid[])",
            (batch_id, ids),
        )

    def publish(
        self, account: Account, batch_id: UUID, command: PublishBatch
    ) -> PublishResponse:
        verified: dict[UUID, bool] = {}
        to_verify: list[Attempt] = []
        document_ids = [item.document_id for item in command.documents]
        revisions = [item.revision for item in command.documents]
        with self._database.transaction() as session:
            batch_for(session, account, batch_id)
            attempts = attempts_for(session, batch_id, document_ids, revisions)
            permitted = {
                UUID(str(row["id"]))
                for row in session.execute(
                    """select d.id from documents.documents d
                    join documents.grants g on g.document_id=d.id
                    where d.id=any(%s::uuid[]) and g.account_id=%s
                    and g.role in ('owner','editor') and d.trashed_at is null""",
                    (document_ids, account.id),
                )
            }
            for item in command.documents:
                attempt = attempts.get(item.document_id)
                if (
                    attempt is None
                    or item.key != attempt.key
                    or item.base_revision != attempt.base_revision
                ):
                    raise Conflict("upload_slot_mismatch")
                # No R2 request for an idempotent publication or a retired slot.
                if (
                    attempt.state == "issued"
                    and attempt.result == "issued"
                    and item.document_id in permitted
                ):
                    to_verify.append(attempt)
        for attempt in to_verify:
            verified[attempt.revision] = self._uploaded(attempt)

        pending: list[tuple[UUID, UUID, str]] = []
        outcomes: dict[UUID, PublishResult] = {}
        published: list[Attempt] = []
        refused: list[tuple[Attempt, Refusal]] = []
        with self._database.transaction() as session:
            batch = batch_for(session, account, batch_id, lock=True)
            session.execute(
                "select id from documents.documents where id=any(%s::uuid[]) order by id for update",
                (document_ids,),
            )
            session.execute(
                """select document_id,revision from documents.checkpoints
                where (document_id,revision) in(select * from unnest(%s::uuid[],%s::uuid[]))
                order by document_id,revision for update""",
                (document_ids, revisions),
            )
            now = database_time(session)
            rows = session.execute(
                """select d.*,g.role from documents.documents d
                left join documents.grants g on g.document_id=d.id and g.account_id=%s
                where d.id=any(%s::uuid[])""",
                (account.id, document_ids),
            )
            documents = {UUID(str(row["id"])): row for row in rows}
            attempts = attempts_for(session, batch.id, document_ids, revisions)
            for item in sorted(command.documents, key=lambda item: item.document_id):
                # Lock the document even if access was revoked, so disposal and
                # grant changes cannot race a successful conditional update.
                row = documents.get(item.document_id)
                attempt = attempts.get(item.document_id)
                if row is None or attempt is None:
                    raise NotFound()
                document = (
                    Document.model_validate(row) if row["role"] is not None else None
                )
                allowed = (
                    document is not None
                    and document.role != "viewer"
                    and document.trashed_at is None
                )
                reason: Refusal | None = None
                if not allowed:
                    reason = "forbidden"
                elif attempt.published_at is not None:
                    assert document is not None
                    outcomes[item.document_id] = PublishResult(
                        document_id=item.document_id,
                        revision=item.revision,
                        result="published",
                        current=self.pointer(document, document.revision),
                        retry_allowed=False,
                    )
                    continue
                elif attempt.result != "issued" or attempt.state != "issued":
                    reason = (
                        attempt.result
                        if attempt.result
                        in {"conflict", "forbidden", "invalid", "expired"}
                        else "expired"
                    )
                elif now >= batch.expires_at or now >= attempt.issued_at + timedelta(
                    seconds=UPLOAD_TIMEOUT_SECONDS
                ):
                    reason = "expired"
                else:
                    self._stage_images(
                        session, item.document_id, item.revision, item.images
                    )
                    if not verified.get(
                        item.revision, False
                    ) or not self._images_allowed(
                        session, item.document_id, item.revision
                    ):
                        reason = "invalid"
                if reason is None:
                    moved = session.execute(
                        """update documents.documents set revision=%s,key=%s,published_at=%s,updated_at=%s
                            where id=%s and revision is not distinct from %s::uuid and trashed_at is null returning id""",
                        (
                            item.revision,
                            item.key,
                            now,
                            now,
                            item.document_id,
                            item.base_revision,
                        ),
                    )
                    if not moved:
                        reason = "conflict"
                if reason is not None:
                    if attempt.state == "issued" and attempt.result == "issued":
                        refused.append((attempt, reason))
                        pending.append((item.document_id, item.revision, item.key))
                        refusals = attempt.refusals + 1
                    else:
                        refusals = attempt.refusals
                        if attempt.state == "pending":
                            pending.append((item.document_id, item.revision, item.key))
                    outcomes[item.document_id] = PublishResult(
                        document_id=item.document_id,
                        revision=item.revision,
                        result=reason,
                        current=self.pointer(document)
                        if allowed and document is not None
                        else None,
                        retry_allowed=reason in {"conflict", "invalid"}
                        and refusals < MAX_REFUSALS
                        and now < batch.expires_at,
                    )
                    continue
                published.append(attempt)
                assert document is not None
                updated = document.model_copy(
                    update={
                        "revision": item.revision,
                        "key": item.key,
                        "published_at": now,
                        "updated_at": now,
                    }
                )
                outcomes[item.document_id] = PublishResult(
                    document_id=item.document_id,
                    revision=item.revision,
                    result="published",
                    current=self.pointer(updated, updated.revision),
                    retry_allowed=False,
                )
            if refused:
                self._refuse(session, batch.id, refused)
            if published:
                ids = [attempt.document_id for attempt in published]
                revs = [attempt.revision for attempt in published]
                session.execute(
                    """update documents.checkpoints set state='published',published_at=%s
                    where (document_id,revision) in(select * from unnest(%s::uuid[],%s::uuid[]))""",
                    (now, ids, revs),
                )
                session.execute(
                    """update coordination.attempts set result='published'
                    where (document_id,revision) in(select * from unnest(%s::uuid[],%s::uuid[]))""",
                    (ids, revs),
                )
                session.execute(
                    """insert into documents.images(document_id,image_id)
                    select document_id,image_id from coordination.images
                    where (document_id,revision) in(select * from unnest(%s::uuid[],%s::uuid[]))
                    on conflict do nothing""",
                    (ids, revs),
                )
                session.execute(
                    """delete from coordination.images
                    where (document_id,revision) in(select * from unnest(%s::uuid[],%s::uuid[]))""",
                    (ids, revs),
                )
                retired = session.execute(
                    """with ranked as (
                    select document_id,revision,row_number() over(partition by document_id order by revision desc) as position
                    from documents.checkpoints where document_id=any(%s::uuid[]) and state='published')
                    update documents.checkpoints c set state='pending' from ranked r
                    where c.document_id=r.document_id and c.revision=r.revision and r.position>%s
                    returning c.document_id,c.revision,c.key""",
                    (ids, CHECKPOINTS_RETAINED),
                )
                pending.extend(
                    (row.document_id, row.revision, row.key)
                    for row in [_Retired.model_validate(value) for value in retired]
                )
        cleanup_ok = self.delete_pending(pending)
        return PublishResponse(
            id=batch_id,
            documents=[outcomes[item.document_id] for item in command.documents],
            cleanup_pending=not cleanup_ok,
        )

    def delete_pending(self, objects: list[tuple[UUID, UUID, str]]) -> bool:
        complete = True
        deleted: list[tuple[UUID, UUID]] = []
        for document_id, revision, key in objects:
            try:
                self._objects.delete(key)
                deleted.append((document_id, revision))
            except StorageUnavailable:
                complete = False
        if deleted:
            try:
                with self._database.transaction() as session:
                    session.execute(
                        """update documents.checkpoints set state='deleted'
                        where (document_id,revision) in(select * from unnest(%s::uuid[],%s::uuid[]))
                        and state='pending'""",
                        (
                            [value[0] for value in deleted],
                            [value[1] for value in deleted],
                        ),
                    )
            except Conflict, Unavailable:
                complete = False
        return complete


class _Retired(Record):
    document_id: UUID7
    revision: UUID7
    key: str


class _Published(_Retired):
    published_at: AwareDatetime

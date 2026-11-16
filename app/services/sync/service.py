"""Coordinate whole-file uploads without accessing document content."""

import json
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid7

from pydantic import UUID7, AwareDatetime

from app.accounts import VerifiedActor
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
from app.services.concurrency import bounded_map
from app.services.database import AsyncDatabase as Database
from app.services.database import AsyncSession as Session
from app.services.documents.repository import document_for
from app.services.errors import Conflict, NotFound, ServiceError, Unavailable
from app.services.identifiers import checkpoint_key, new_revision
from app.services.identity.actor import Actor, resolve_actor
from app.services.limits import (
    CHECKPOINTS_RETAINED,
    MAX_CHECKPOINT_BYTES,
    MAX_REFUSALS,
)
from app.services.models import Account, CheckpointState, Document, Record
from app.services.operations import operation
from app.services.storage.objects import (
    AsyncCheckpointObjects as CheckpointObjects,
)
from app.services.storage.objects import (
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


async def batch_for(
    session: Session, account: Account, batch_id: UUID, *, lock: bool = False
) -> Batch:
    rows = await session.execute(
        "select * from coordination.batches where id=%s and account=%s"
        + (" for update" if lock else ""),
        (batch_id, account.id),
    )
    if not rows:
        raise NotFound()
    return Batch.model_validate(rows[0])


async def attempt_for(
    session: Session, batch_id: UUID, document_id: UUID, revision: UUID | None = None
) -> Attempt | None:
    return (
        await attempts_for(
            session,
            batch_id,
            [document_id],
            [revision] if revision is not None else None,
        )
    ).get(document_id)


async def attempts_for(
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
    rows = await session.execute(
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

    async def open(
        self, account: Actor, document_id: UUID, known_revision: UUID | None
    ) -> Pointer:
        async with self._database.read() as session:
            if isinstance(account, VerifiedActor):
                rows = await session.execute(
                    "select coordination.open_pointer(%s,%s,%s,%s,%s) as result",
                    (
                        account.identity.issuer,
                        account.identity.subject,
                        uuid7(),
                        uuid7(),
                        document_id,
                    ),
                )
                if rows[0]["result"] is None:
                    raise NotFound()
                document = Document.model_validate(rows[0]["result"])
            else:
                document = await document_for(session, account, document_id)
            return self.pointer(document, known_revision)

    async def checkpoints(self, account: Actor, document_id: UUID) -> list[Pointer]:
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            (await document_for(session, account, document_id))
            rows = await session.execute(
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

    async def stage_images(
        self,
        account: Actor,
        batch_id: UUID,
        document_id: UUID,
        revision: UUID,
        images: list[UUID],
    ) -> None:
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            batch = await batch_for(session, account, batch_id, lock=True)
            (await document_for(session, account, document_id, edit=True, lock=True))
            attempt = await attempt_for(session, batch_id, document_id, revision)
            if attempt is None:
                raise NotFound()
            if (
                attempt.state != "issued"
                or attempt.result != "issued"
                or (await database_time(session)) >= batch.expires_at
            ):
                raise Conflict("upload_slot_closed")
            (await self._stage_images(session, document_id, revision, images))

    async def _stage_images(
        self, session: Session, document_id: UUID, revision: UUID, images: list[UUID]
    ) -> None:
        if images:
            (
                await session.execute(
                    """insert into coordination.images(document_id,revision,image_id)
                select %s,%s,unnest(%s::uuid[]) on conflict do nothing""",
                    (document_id, revision, images),
                )
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

    async def _issue(
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
        (
            await session.execute(
                """insert into documents.checkpoints(document_id,revision,key,state,issued_at)
                values(%s,%s,%s,'issued',%s)""",
                (document.id, revision, key, now),
            )
        )
        (
            await session.execute(
                """insert into coordination.attempts(document_id,revision,batch,base_revision,size,hash,expires_at)
                values(%s,%s,%s,%s,%s,%s,%s)""",
                (
                    document.id,
                    revision,
                    batch.id,
                    document.revision,
                    size,
                    digest,
                    expires,
                ),
            )
        )
        attempt = await attempt_for(session, batch.id, document.id, revision)
        assert attempt is not None
        return attempt

    async def start(self, account: Actor, command: StartBatch) -> BatchResponse:
        items = [
            {**item.model_dump(mode="json"), "candidate": str(new_revision())}
            for item in command.documents
        ]
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            state = await operation(
                session,
                "reserve_batch",
                (account.id, command.id, json.dumps(items), self._seconds),
                _Reservation,
            )
            return BatchResponse(
                id=state.batch.id,
                started_at=state.batch.started_at,
                expires_at=state.batch.expires_at,
                documents=[
                    self._slot(item.attempt, item.document, state.now)
                    for item in state.items
                ],
            )

    async def retry(
        self, account: Actor, batch_id: UUID, document_id: UUID, command: RetryUpload
    ) -> UploadSlot:
        retired: Attempt | None = None
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            batch = await batch_for(session, account, batch_id, lock=True)
            now = await database_time(session)
            if now >= batch.expires_at:
                raise ServiceError("batch_expired", "This batch has expired.", 410)
            document = await document_for(
                session, account, document_id, lock=True, edit=True
            )
            current = await attempt_for(session, batch.id, document.id)
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
                (
                    await session.execute(
                        "update documents.checkpoints set state='pending' where document_id=%s and revision=%s",
                        (document.id, current.revision),
                    )
                )
                (
                    await session.execute(
                        "update coordination.attempts set result='replaced' where document_id=%s and revision=%s",
                        (document.id, current.revision),
                    )
                )
                retired = current
            attempt = await self._issue(
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
            (
                await self.delete_pending(
                    [(retired.document_id, retired.revision, retired.key)]
                )
            )
        return slot

    async def _uploaded(self, attempt: Attempt) -> bool:
        try:
            metadata = await self._objects.head(attempt.key)
        except ObjectMissing:
            return False
        return (
            0 < metadata.size <= MAX_CHECKPOINT_BYTES
            and metadata.size == attempt.size
            and metadata.media_type == "application/octet-stream"
            and metadata.metadata.get("sha256") == attempt.hash
        )

    async def publish(
        self, account: Actor, batch_id: UUID, command: PublishBatch
    ) -> PublishResponse:
        items = command.model_dump(mode="json")["documents"]
        async with self._database.read() as session:
            account = await resolve_actor(session, account)
            state = await operation(
                session,
                "publish_preflight",
                (account.id, batch_id, json.dumps(items)),
                _Preflight,
            )
        attempts = {item.document_id: item for item in state.attempts}
        to_verify: list[Attempt] = []
        for item in command.documents:
            attempt = attempts.get(item.document_id)
            if (
                attempt is None
                or item.key != attempt.key
                or item.base_revision != attempt.base_revision
            ):
                raise Conflict("upload_slot_mismatch")
            if (
                attempt.state == "issued"
                and attempt.result == "issued"
                and attempt.permitted
            ):
                to_verify.append(attempt)
        verified = await bounded_map(self._uploaded, to_verify, limit=4)
        by_revision = {
            attempt.revision: value
            for attempt, value in zip(to_verify, verified, strict=True)
        }
        payload = [
            {
                **item.model_dump(mode="json"),
                "verified": by_revision.get(item.revision, False),
            }
            for item in command.documents
        ]
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            result = await operation(
                session,
                "publish_verified_batch",
                (account.id, batch_id, json.dumps(payload)),
                _Publication,
            )
        outcomes = {
            item.document_id: PublishResult(
                document_id=item.document_id,
                revision=item.revision,
                result=item.result,
                current=self.pointer(
                    item.document,
                    item.document.revision if item.result == "published" else None,
                )
                if item.document
                else None,
                retry_allowed=item.retry_allowed,
            )
            for item in result.outcomes
        }
        cleanup_ok = await self.delete_pending(
            [(item.document_id, item.revision, item.key) for item in result.pending]
        )
        return PublishResponse(
            id=batch_id,
            documents=[outcomes[item.document_id] for item in command.documents],
            cleanup_pending=not cleanup_ok,
        )

    async def delete_pending(self, objects: list[tuple[UUID, UUID, str]]) -> bool:
        async def remove(value: tuple[UUID, UUID, str]) -> tuple[UUID, UUID] | None:
            try:
                await self._objects.delete(value[2])
                return value[0], value[1]
            except StorageUnavailable:
                return None

        results = await bounded_map(remove, objects, limit=4)
        complete = all(value is not None for value in results)
        deleted = [value for value in results if value is not None]
        if deleted:
            try:
                async with self._database.transaction() as session:
                    (
                        await session.execute(
                            """update documents.checkpoints set state='deleted'
                        where (document_id,revision) in(select * from unnest(%s::uuid[],%s::uuid[]))
                        and state='pending'""",
                            (
                                [value[0] for value in deleted],
                                [value[1] for value in deleted],
                            ),
                        )
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


class _Reserved(Record):
    document: Document
    attempt: Attempt


class _Reservation(Record):
    batch: Batch
    now: AwareDatetime
    items: list[_Reserved]


class _PreflightAttempt(Attempt):
    permitted: bool | None


class _Preflight(Record):
    attempts: list[_PreflightAttempt]


class _Outcome(Record):
    document_id: UUID7
    revision: UUID7
    result: Literal["published", "conflict", "forbidden", "invalid", "expired"]
    document: Document | None
    retry_allowed: bool


class _Publication(Record):
    outcomes: list[_Outcome]
    pending: list[_Retired]

"""Coordinate whole-file uploads without accessing document content."""

import json
from datetime import datetime
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
from app.services.catalog.reader import CatalogReader
from app.services.concurrency import bounded_map
from app.services.database import AsyncDatabase as Database
from app.services.errors import Conflict, Unavailable
from app.services.identifiers import new_revision
from app.services.identity.actor import Actor
from app.services.limits import (
    MAX_CHECKPOINT_BYTES,
)
from app.services.models import CheckpointState, Document, Record
from app.services.operations import operation
from app.services.storage.objects import (
    AsyncCheckpointObjects as CheckpointObjects,
)
from app.services.storage.objects import (
    ObjectMissing,
    StorageUnavailable,
)
from app.services.storage.signing import S3Signer

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


class SyncService:
    def __init__(
        self,
        database: Database,
        objects: CheckpointObjects,
        signer: S3Signer,
        *,
        presign_seconds: int = 300,
        reader: CatalogReader | None = None,
    ) -> None:
        self._database = database
        self._objects = objects
        self._signer = signer
        self._seconds = presign_seconds
        self._reader = reader or CatalogReader(database)

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
        return self.pointer(
            await self._reader.document(account, document_id), known_revision
        )

    async def checkpoints(self, account: Actor, document_id: UUID) -> list[Pointer]:
        return [
            Pointer(
                document_id=value.document_id,
                revision=value.revision,
                key=value.key,
                published_at=value.published_at,
                read=self._signer.permission(value.key, self._seconds, method="GET"),
            )
            for value in await self._reader.checkpoints(account, document_id)
        ]

    async def stage_images(
        self,
        account: Actor,
        batch_id: UUID,
        document_id: UUID,
        revision: UUID,
        images: list[UUID],
    ) -> None:
        await operation(
            self._database,
            "stage_images",
            account,
            (batch_id, document_id, revision, images),
            Record,
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

    async def start(self, account: Actor, command: StartBatch) -> BatchResponse:
        items = [
            {**item.model_dump(mode="json"), "candidate": str(new_revision())}
            for item in command.documents
        ]
        state = await operation(
            self._database,
            "reserve_batch",
            account,
            (command.id, json.dumps(items), self._seconds),
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
        state = await operation(
            self._database,
            "retry_upload",
            account,
            (
                batch_id,
                document_id,
                command.previous_revision,
                command.base_revision,
                command.size,
                command.hash,
                new_revision(),
                self._seconds,
            ),
            _Retry,
        )
        if (retired := state.retired) is not None:
            await self.delete_pending(
                [(retired.document_id, retired.revision, retired.key)]
            )
        return self._slot(state.attempt, state.document, state.now)

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
        state = await operation(
            self._database,
            "publish_preflight",
            account,
            (batch_id, json.dumps(items)),
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
        result = await operation(
            self._database,
            "publish_verified_batch",
            account,
            (batch_id, json.dumps(payload)),
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
                await self._database.write(
                    """update documents.checkpoints set state='deleted'
                    where (document_id,revision) in(select * from unnest(%s::uuid[],%s::uuid[]))
                    and state='pending'""",
                    ([value[0] for value in deleted], [value[1] for value in deleted]),
                )
            except Conflict, Unavailable:
                complete = False
        return complete


class _Retired(Record):
    document_id: UUID7
    revision: UUID7
    key: str


class _Reserved(Record):
    document: Document
    attempt: Attempt


class _Retry(_Reserved):
    now: AwareDatetime
    retired: _Retired | None


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

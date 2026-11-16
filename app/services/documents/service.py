"""Spaces, titles, explicit grants, and trash. No document content operations."""

from datetime import timedelta
from uuid import UUID

from app.schemas import CreateDocument, DocumentGrant, DocumentSummary, SpaceResponse
from app.services.database import AsyncDatabase as Database
from app.services.documents.repository import document_for, summary
from app.services.errors import Conflict, Forbidden, NotFound, ServiceError
from app.services.identity.actor import Actor, resolve_actor
from app.services.models import Document, Record, Space
from app.services.operations import operation
from app.services.pagination import PageCursor
from app.services.time import database_time


class DocumentService:
    def __init__(self, database: Database, *, trash_days: int = 3) -> None:
        self._database = database
        self._trash_days = trash_days

    async def spaces(self, account: Actor) -> list[SpaceResponse]:
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            return [
                SpaceResponse(**Space.model_validate(row).model_dump())
                for row in (
                    await session.execute(
                        "select * from documents.spaces where owner=%s order by id",
                        (account.id,),
                    )
                )
            ]

    async def create_space(self, account: Actor, space_id: UUID) -> SpaceResponse:
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            rows = (
                await session.execute(
                    """insert into documents.spaces(id,owner) values(%s,%s)
                    on conflict(id) do nothing returning *""",
                    (space_id, account.id),
                )
            ) or (
                await session.execute(
                    "select * from documents.spaces where id=%s and owner=%s",
                    (space_id, account.id),
                )
            )
            if not rows:
                raise Conflict()
            return SpaceResponse(**Space.model_validate(rows[0]).model_dump())

    async def create(self, account: Actor, command: CreateDocument) -> DocumentSummary:
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            if not (
                await session.execute(
                    "select id from documents.spaces where id=%s and owner=%s",
                    (command.space, account.id),
                )
            ):
                raise NotFound()
            rows = await session.execute(
                """insert into documents.documents(id,space,title) values(%s,%s,%s)
                    on conflict(id) do nothing returning id""",
                (command.id, command.space, command.title),
            )
            if rows:
                (
                    await session.execute(
                        "insert into documents.grants(document_id,account_id,role) values(%s,%s,'owner')",
                        (command.id, account.id),
                    )
                )
            document = await document_for(session, account, command.id, trash=True)
            if document.space != command.space or document.role != "owner":
                raise Conflict()
            return summary(document)

    async def list_documents(
        self, account: Actor, *, trashed: bool, limit: int, offset: int, cursor: PageCursor | None = None
    ) -> list[DocumentSummary]:
        async with self._database.read() as session:
            account = await resolve_actor(session, account)
            boundary = " and (d.updated_at<%s or (d.updated_at=%s and d.id>%s))" if cursor else ""
            parameters: list[object] = [account.id, trashed]
            if cursor:
                parameters.extend([cursor.timestamp, cursor.timestamp, cursor.id])
            parameters.extend([limit, offset])
            rows = await session.execute(
                """select d.*,g.role from documents.documents d
                    join documents.grants g on g.document_id=d.id
                    where g.account_id=%s and (d.trashed_at is not null)=%s"""
                    +boundary+" order by d.updated_at desc,d.id limit %s offset %s",
                parameters,
            )
            return [summary(Document.model_validate(row)) for row in rows]

    async def metadata(self, account: Actor, document_id: UUID) -> DocumentSummary:
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            return summary(
                await document_for(session, account, document_id, trash=True)
            )

    async def rename(
        self, account: Actor, document_id: UUID, title: str
    ) -> DocumentSummary:
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            state = await operation(
                session, "rename_document", (account.id, document_id, title), _Renamed
            )
            return summary(state.document)

    async def grants(self, account: Actor, document_id: UUID) -> list[DocumentGrant]:
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            (await document_for(session, account, document_id, owner=True, trash=True))
            return [
                DocumentGrant.model_validate(row, strict=False)
                for row in (
                    await session.execute(
                        "select document_id,account_id,role from documents.grants where document_id=%s order by account_id",
                        (document_id,),
                    )
                )
            ]

    async def grant(
        self, account: Actor, document_id: UUID, target: UUID, role: str | None
    ) -> None:
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            (await document_for(session, account, document_id, lock=True, owner=True))
            if target == account.id:
                raise Forbidden()
            if role is None:
                (
                    await session.execute(
                        "delete from documents.grants where document_id=%s and account_id=%s and role<>'owner'",
                        (document_id, target),
                    )
                )
                return
            if role not in {"editor", "viewer"}:
                raise Forbidden()
            if not (
                await session.execute(
                    "select id from public.accounts where id=%s", (target,)
                )
            ):
                raise NotFound()
            (
                await session.execute(
                    """insert into documents.grants(document_id,account_id,role) values(%s,%s,%s)
                    on conflict(document_id,account_id) do update set role=excluded.role
                    where documents.grants.role<>'owner'""",
                    (document_id, target, role),
                )
            )

    async def lifecycle(
        self, account: Actor, document_id: UUID, *, trashed: bool
    ) -> DocumentSummary:
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            document = await document_for(
                session, account, document_id, lock=True, owner=True, trash=True
            )
            if document.trashed_at is not None:
                now = await database_time(session)
                if now >= document.trashed_at + timedelta(days=self._trash_days):
                    raise ServiceError(
                        "trash_expired", "The trash retention period has elapsed.", 410
                    )
            if trashed:
                (
                    await session.execute(
                        "update documents.documents set trashed_at=coalesce(trashed_at,clock_timestamp()),updated_at=clock_timestamp() where id=%s",
                        (document_id,),
                    )
                )
            else:
                (
                    await session.execute(
                        "update documents.documents set trashed_at=null,updated_at=clock_timestamp() where id=%s",
                        (document_id,),
                    )
                )
            return summary(
                await document_for(session, account, document_id, trash=True)
            )


class _Renamed(Record):
    document: Document

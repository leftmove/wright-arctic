"""Spaces, titles, explicit grants, and trash. No document content operations."""

from uuid import UUID

from app.schemas import CreateDocument, DocumentGrant, DocumentSummary, SpaceResponse
from app.services.catalog.reader import CatalogReader
from app.services.database import AsyncDatabase
from app.services.identity.actor import Actor
from app.services.models import Document, Record, Space
from app.services.operations import operation
from app.services.pagination import PageCursor


def summary(document: Document) -> DocumentSummary:
    return DocumentSummary(**document.model_dump())


class DocumentService:
    def __init__(
        self,
        database: AsyncDatabase,
        *,
        trash_days: int = 3,
        reader: CatalogReader | None = None,
    ) -> None:
        self._database = database
        self._trash_days = trash_days
        self._reader = reader or CatalogReader(database)

    async def spaces(self, account: Actor) -> list[SpaceResponse]:
        return [
            SpaceResponse(**row.model_dump())
            for row in await self._reader.spaces(account)
        ]

    async def create_space(self, account: Actor, space_id: UUID) -> SpaceResponse:
        space = await operation(
            self._database, "create_space", account, (space_id,), Space
        )
        return SpaceResponse(**space.model_dump())

    async def create(self, account: Actor, command: CreateDocument) -> DocumentSummary:
        document = await operation(
            self._database,
            "create_document",
            account,
            (command.id, command.space, command.title),
            Document,
        )
        return summary(document)

    async def list_documents(
        self,
        account: Actor,
        *,
        trashed: bool,
        limit: int,
        offset: int,
        cursor: PageCursor | None = None,
    ) -> list[DocumentSummary]:
        rows = await self._reader.library(
            account, trashed=trashed, limit=limit, offset=offset, cursor=cursor
        )
        return [summary(row) for row in rows]

    async def metadata(self, account: Actor, document_id: UUID) -> DocumentSummary:
        return summary(await self._reader.document(account, document_id, trash=True))

    async def rename(
        self, account: Actor, document_id: UUID, title: str
    ) -> DocumentSummary:
        state = await operation(
            self._database, "rename_document", account, (document_id, title), _Renamed
        )
        return summary(state.document)

    async def grants(self, account: Actor, document_id: UUID) -> list[DocumentGrant]:
        return [
            DocumentGrant.model_validate(row.model_dump())
            for row in await self._reader.grants(account, document_id)
        ]

    async def grant(
        self, account: Actor, document_id: UUID, target: UUID, role: str | None
    ) -> None:
        await operation(
            self._database, "set_grant", account, (document_id, target, role), Record
        )

    async def lifecycle(
        self, account: Actor, document_id: UUID, *, trashed: bool
    ) -> DocumentSummary:
        document = await operation(
            self._database,
            "document_lifecycle",
            account,
            (document_id, trashed, self._trash_days),
            Document,
        )
        return summary(document)


class _Renamed(Record):
    document: Document

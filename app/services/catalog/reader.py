"""Catalog queries and the corresponding lookups in a caught-up process copy."""

import json
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from uuid import UUID

from pydantic import TypeAdapter

from app.accounts import VerifiedActor
from app.services.catalog.copy import (
    CatalogCopy,
    CatalogRecords,
    Grant,
    PublishedCheckpoint,
)
from app.services.database import AsyncDatabase, Row
from app.services.errors import Forbidden, NotFound
from app.services.identity.actor import Actor, actor_query
from app.services.limits import CHECKPOINTS_RETAINED
from app.services.models import Account, Document, Image, Space
from app.services.pagination import PageCursor

DOCUMENT = """select d.*,g.role {extra} from actor
    join documents.grants g on g.account_id=actor.id
    join documents.documents d on d.id=g.document_id
    where d.id=%s"""
LIBRARY = """select d.*,g.role from actor
    join documents.grants g on g.account_id=actor.id
    join documents.documents d on d.id=g.document_id
    where (d.trashed_at is not null)=%s"""
UPLOADED_IMAGES = """select i.* from actor join public.images i on i.account=actor.id
    where i.deleted_at is null"""
IMAGE = """select i.*, actor.id as caller, exists(
    select 1 from documents.images r
    join documents.grants g on g.document_id=r.document_id
    join documents.documents d on d.id=r.document_id
    where r.image_id=i.id and g.account_id=actor.id and d.trashed_at is null
    ) as referenced from actor cross join public.images i where i.id=%s"""


def document_access(
    document: Document | None, *, trash: bool = False, owner: bool = False
) -> Document:
    if document is None:
        raise NotFound()
    if owner and document.role != "owner":
        raise Forbidden()
    if not trash and document.trashed_at is not None:
        raise NotFound()
    return document


def image_access(image: Image | None, *, allowed: bool) -> Image:
    if not allowed or image is None or image.deleted_at is not None:
        raise NotFound()
    return image


def _page[T](
    rows: Iterable[T],
    fields: Callable[[T], tuple[datetime, UUID]],
    limit: int,
    offset: int,
    cursor: PageCursor | None,
) -> list[T]:
    def after(value: T) -> bool:
        timestamp, identifier = fields(value)
        return (
            cursor is None
            or timestamp < cursor.timestamp
            or (timestamp == cursor.timestamp and identifier > cursor.id)
        )

    # Sort UUIDs ascending within descending timestamp groups.
    ordered = sorted(filter(after, rows), key=lambda value: fields(value)[1])
    ordered.sort(key=lambda value: fields(value)[0], reverse=True)
    return ordered[offset : offset + limit]


def _page_query(
    field: str,
    identifier: str,
    limit: int,
    offset: int,
    cursor: PageCursor | None,
) -> tuple[str, tuple[object, ...]]:
    order = f" order by {field} desc,{identifier} limit %s offset %s"
    if cursor is None:
        return order, (limit, offset)
    return (
        f" and ({field}<%s or ({field}=%s and {identifier}>%s))" + order,
        (cursor.timestamp, cursor.timestamp, cursor.id, limit, offset),
    )


def _json(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


class CatalogReader:
    def __init__(
        self, database: AsyncDatabase, copy: CatalogCopy | None = None
    ) -> None:
        self._database = database
        self._copy = copy

    def _records(self) -> CatalogRecords | None:
        return self._copy.readable if self._copy is not None else None

    def _view(self, actor: Actor) -> tuple[CatalogRecords, UUID] | None:
        records = self._records()
        if records is None:
            return None
        account = (
            records.identities.get(actor.identity)
            if isinstance(actor, VerifiedActor)
            else actor
        )
        return (records, account.id) if account is not None else None

    async def _actor_rows(
        self,
        actor: Actor,
        query: str,
        parameters: Sequence[object] = (),
    ) -> list[Row]:
        sql, values = actor_query(actor, query, parameters)
        records = self._records()
        # First use can provision an account. Bring those committed rows into
        # this process before responding, just like any other local write.
        if (
            records is not None
            and isinstance(actor, VerifiedActor)
            and records.identities.get(actor.identity) is None
        ):
            return await self._database.write(sql, values)
        return await self._database.fetch(sql, values)

    async def account(self, actor: Actor) -> Account:
        if (view := self._view(actor)) is not None:
            records, identifier = view
            account = records.accounts.get(identifier)
            if account is not None:
                return account
            raise NotFound()
        rows = await self._actor_rows(
            actor,
            "select * from actor"
            if isinstance(actor, VerifiedActor)
            else "select a.* from actor join public.accounts a using(id)",
        )
        if not rows:
            raise NotFound()
        return Account.model_validate(rows[0])

    async def spaces(self, actor: Actor) -> list[Space]:
        if (view := self._view(actor)) is not None:
            records, identifier = view
            return sorted(
                records.spaces.members(identifier).values(), key=lambda s: s.id
            )
        rows = await self._actor_rows(
            actor,
            "select s.* from actor cross join lateral coordination.account_spaces(actor.id) s order by s.id",
        )
        return [Space.model_validate(row) for row in rows]

    async def library(
        self,
        actor: Actor,
        *,
        trashed: bool,
        limit: int,
        offset: int,
        cursor: PageCursor | None,
    ) -> list[Document]:
        if (view := self._view(actor)) is not None:
            records, identifier = view
            documents = (
                document
                for doc_id in records.library.members(identifier)
                if (document := records.document(identifier, doc_id)) is not None
                and (document.trashed_at is not None) == trashed
            )
            return _page(
                documents, lambda d: (d.updated_at, d.id), limit, offset, cursor
            )
        order, values = _page_query("d.updated_at", "d.id", limit, offset, cursor)
        return [
            Document.model_validate(row)
            for row in await self._actor_rows(
                actor, LIBRARY + order, (trashed, *values)
            )
        ]

    async def document(
        self, actor: Actor, identifier: UUID, *, trash: bool = False
    ) -> Document:
        if (view := self._view(actor)) is not None:
            records, caller = view
            return document_access(records.document(caller, identifier), trash=trash)
        rows = await self._actor_rows(actor, DOCUMENT.format(extra=""), (identifier,))
        return document_access(
            Document.model_validate(rows[0]) if rows else None, trash=trash
        )

    async def grants(self, actor: Actor, identifier: UUID) -> list[Grant]:
        if (view := self._view(actor)) is not None:
            records, caller = view
            document_access(
                records.document(caller, identifier), trash=True, owner=True
            )
            return sorted(
                records.grants.members(identifier).values(), key=lambda g: g.account_id
            )
        rows = await self._actor_rows(
            actor,
            DOCUMENT.format(
                extra=""", (
            select coalesce(jsonb_agg(to_jsonb(share) order by share.account_id),'[]')
            from documents.grants share where share.document_id=d.id
        ) as grants"""
            ),
            (identifier,),
        )
        document_access(
            Document.model_validate(rows[0]) if rows else None, trash=True, owner=True
        )
        return TypeAdapter(list[Grant]).validate_python(_json(rows[0]["grants"]))

    async def checkpoints(
        self, actor: Actor, identifier: UUID
    ) -> list[PublishedCheckpoint]:
        if (view := self._view(actor)) is not None:
            records, caller = view
            document_access(records.document(caller, identifier))
            return sorted(
                records.checkpoints.members(identifier).values(),
                key=lambda c: c.revision,
                reverse=True,
            )[:CHECKPOINTS_RETAINED]
        rows = await self._actor_rows(
            actor,
            DOCUMENT.format(
                extra=""", (
            select coalesce(jsonb_agg(to_jsonb(checkpoint) order by checkpoint.revision desc),'[]')
            from (select document_id,revision,key,published_at from documents.checkpoints
                  where document_id=d.id and state='published' order by revision desc limit %s) checkpoint
        ) as checkpoints"""
            ),
            (CHECKPOINTS_RETAINED, identifier),
        )
        document_access(Document.model_validate(rows[0]) if rows else None)
        return TypeAdapter(list[PublishedCheckpoint]).validate_python(
            _json(rows[0]["checkpoints"])
        )

    async def images(
        self,
        actor: Actor,
        *,
        limit: int,
        offset: int,
        cursor: PageCursor | None,
    ) -> list[Image]:
        if (view := self._view(actor)) is not None:
            records, caller = view
            live = (
                image
                for image in records.uploaded_images.members(caller).values()
                if image.deleted_at is None
            )
            return _page(live, lambda i: (i.created_at, i.id), limit, offset, cursor)
        order, values = _page_query("i.created_at", "i.id", limit, offset, cursor)
        return [
            Image.model_validate(row)
            for row in await self._actor_rows(actor, UPLOADED_IMAGES + order, values)
        ]

    async def image_by_hash(self, actor: Actor, digest: str) -> Image:
        if (view := self._view(actor)) is not None:
            records, caller = view
            match = min(
                (
                    i
                    for i in records.uploaded_images.members(caller).values()
                    if i.deleted_at is None and i.hash == digest
                ),
                key=lambda i: i.id,
                default=None,
            )
            return image_access(match, allowed=True)
        rows = await self._actor_rows(
            actor, UPLOADED_IMAGES + " and i.hash=%s order by i.id limit 1", (digest,)
        )
        return image_access(
            Image.model_validate(rows[0]) if rows else None, allowed=True
        )

    async def image(self, actor: Actor, identifier: UUID) -> Image:
        if (view := self._view(actor)) is not None:
            records, caller = view
            image = records.images.get(identifier)
            referenced = any(
                (document := records.document(caller, ref_id)) is not None
                and document.trashed_at is None
                for ref_id in records.image_references.members(identifier)
            )
            return image_access(
                image,
                allowed=referenced or (image is not None and image.account == caller),
            )
        rows = await self._actor_rows(actor, IMAGE, (identifier,))
        if not rows:
            raise NotFound()
        image = Image.model_validate(rows[0])
        return image_access(
            image,
            allowed=rows[0]["referenced"] is True
            or str(image.account) == str(rows[0]["caller"]),
        )

    async def avatar(self, identifier: UUID) -> Image:
        if (records := self._records()) is not None:
            account = records.accounts.get(identifier)
            image = (
                records.images.get(account.avatar_id)
                if account and account.avatar_id
                else None
            )
        else:
            rows = await self._database.fetch(
                """select i.* from public.accounts a join public.images i on i.id=a.avatar_id
                where a.id=%s""",
                (identifier,),
            )
            image = Image.model_validate(rows[0]) if rows else None
        return image_access(image, allowed=True)

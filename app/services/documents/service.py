"""Spaces, titles, explicit grants, and trash. No document content operations."""

from datetime import timedelta
from uuid import UUID

from app.schemas import CreateDocument, DocumentGrant, DocumentSummary, SpaceResponse
from app.services.database import Database
from app.services.documents.repository import document_for, summary
from app.services.errors import Conflict, Forbidden, NotFound, ServiceError
from app.services.models import Account, Document, Space
from app.services.time import database_time


class DocumentService:
    def __init__(self, database: Database, *, trash_days: int = 3) -> None:
        self._database = database
        self._trash_days = trash_days

    def spaces(self, account: Account) -> list[SpaceResponse]:
        with self._database.transaction() as session:
            return [
                SpaceResponse(**Space.model_validate(row).model_dump())
                for row in session.execute(
                    "select * from documents.spaces where owner=%s order by id",
                    (account.id,),
                )
            ]

    def create_space(self, account: Account, space_id: UUID) -> SpaceResponse:
        with self._database.transaction() as session:
            rows = session.execute(
                """insert into documents.spaces(id,owner) values(%s,%s)
                    on conflict(id) do nothing returning *""",
                (space_id, account.id),
            ) or session.execute(
                "select * from documents.spaces where id=%s and owner=%s",
                (space_id, account.id),
            )
            if not rows:
                raise Conflict()
            return SpaceResponse(**Space.model_validate(rows[0]).model_dump())

    def create(self, account: Account, command: CreateDocument) -> DocumentSummary:
        with self._database.transaction() as session:
            if not session.execute(
                "select id from documents.spaces where id=%s and owner=%s",
                (command.space, account.id),
            ):
                raise NotFound()
            rows = session.execute(
                """insert into documents.documents(id,space,title) values(%s,%s,%s)
                    on conflict(id) do nothing returning id""",
                (command.id, command.space, command.title),
            )
            if rows:
                session.execute(
                    "insert into documents.grants(document_id,account_id,role) values(%s,%s,'owner')",
                    (command.id, account.id),
                )
            document = document_for(session, account, command.id, trash=True)
            if document.space != command.space or document.role != "owner":
                raise Conflict()
            return summary(document)

    def list_documents(
        self, account: Account, *, trashed: bool, limit: int, offset: int
    ) -> list[DocumentSummary]:
        with self._database.transaction() as session:
            rows = session.execute(
                """select d.*,g.role from documents.documents d
                    join documents.grants g on g.document_id=d.id
                    where g.account_id=%s and (d.trashed_at is not null)=%s
                    order by d.updated_at desc,d.id limit %s offset %s""",
                (account.id, trashed, limit, offset),
            )
            return [summary(Document.model_validate(row)) for row in rows]

    def metadata(self, account: Account, document_id: UUID) -> DocumentSummary:
        with self._database.transaction() as session:
            return summary(document_for(session, account, document_id, trash=True))

    def rename(
        self, account: Account, document_id: UUID, title: str
    ) -> DocumentSummary:
        with self._database.transaction() as session:
            document_for(session, account, document_id, lock=True, edit=True)
            session.execute(
                "update documents.documents set title=%s,updated_at=clock_timestamp() where id=%s",
                (title, document_id),
            )
            return summary(document_for(session, account, document_id))

    def grants(self, account: Account, document_id: UUID) -> list[DocumentGrant]:
        with self._database.transaction() as session:
            document_for(session, account, document_id, owner=True, trash=True)
            return [
                DocumentGrant.model_validate(row, strict=False)
                for row in session.execute(
                    "select document_id,account_id,role from documents.grants where document_id=%s order by account_id",
                    (document_id,),
                )
            ]

    def grant(
        self, account: Account, document_id: UUID, target: UUID, role: str | None
    ) -> None:
        with self._database.transaction() as session:
            document_for(session, account, document_id, lock=True, owner=True)
            if target == account.id:
                raise Forbidden()
            if role is None:
                session.execute(
                    "delete from documents.grants where document_id=%s and account_id=%s and role<>'owner'",
                    (document_id, target),
                )
                return
            if role not in {"editor", "viewer"}:
                raise Forbidden()
            if not session.execute(
                "select id from public.accounts where id=%s", (target,)
            ):
                raise NotFound()
            session.execute(
                """insert into documents.grants(document_id,account_id,role) values(%s,%s,%s)
                    on conflict(document_id,account_id) do update set role=excluded.role
                    where documents.grants.role<>'owner'""",
                (document_id, target, role),
            )

    def lifecycle(
        self, account: Account, document_id: UUID, *, trashed: bool
    ) -> DocumentSummary:
        with self._database.transaction() as session:
            document = document_for(
                session, account, document_id, lock=True, owner=True, trash=True
            )
            if document.trashed_at is not None:
                now = database_time(session)
                if now >= document.trashed_at + timedelta(days=self._trash_days):
                    raise ServiceError(
                        "trash_expired", "The trash retention period has elapsed.", 410
                    )
            if trashed:
                session.execute(
                    "update documents.documents set trashed_at=coalesce(trashed_at,clock_timestamp()),updated_at=clock_timestamp() where id=%s",
                    (document_id,),
                )
            else:
                session.execute(
                    "update documents.documents set trashed_at=null,updated_at=clock_timestamp() where id=%s",
                    (document_id,),
                )
            return summary(document_for(session, account, document_id, trash=True))

"""Document authorization and row locking shared by metadata and sync services."""

from uuid import UUID

from app.schemas import DocumentSummary
from app.services.database import AsyncSession as Session
from app.services.errors import Forbidden, NotFound
from app.services.models import Account, Document


async def document_for(
    session: Session,
    account: Account,
    document_id: UUID,
    *,
    lock: bool = False,
    edit: bool = False,
    owner: bool = False,
    trash: bool = False,
) -> Document:
    return (
        await documents_for(
            session,
            account,
            [document_id],
            lock=lock,
            edit=edit,
            owner=owner,
            trash=trash,
        )
    )[document_id]


async def documents_for(
    session: Session,
    account: Account,
    document_ids: list[UUID],
    *,
    lock: bool = False,
    edit: bool = False,
    owner: bool = False,
    trash: bool = False,
) -> dict[UUID, Document]:
    if lock:
        # A joined grant read can retain its pre-wait statement snapshot while
        # waiting on the document row. Read grants in a fresh statement after
        # acquiring the shared serialization lock used by grant mutations.
        (
            await session.execute(
                "select id from documents.documents where id=any(%s::uuid[]) order by id for update",
                (document_ids,),
            )
        )
    rows = await session.execute(
        """select d.*,g.role from documents.documents d
            join documents.grants g on g.document_id=d.id
            where d.id=any(%s::uuid[]) and g.account_id=%s""",
        (document_ids, account.id),
    )
    documents = {
        document.id: document for document in map(Document.model_validate, rows)
    }
    for document_id in sorted(document_ids):
        if document_id not in documents:
            raise NotFound()
        document = documents[document_id]
        if (edit and document.role == "viewer") or (owner and document.role != "owner"):
            raise Forbidden()
        if document.trashed_at is not None and not trash:
            raise NotFound()
    return documents


def summary(document: Document) -> DocumentSummary:
    return DocumentSummary(**document.model_dump())

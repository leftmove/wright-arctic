"""Process-local copy of the small catalog relations and their lookup indexes."""

from collections.abc import Callable, Iterable
from typing import Literal
from uuid import UUID

from pydantic import UUID7, AwareDatetime

from app.accounts import Identity
from app.services.models import (
    Account,
    Document,
    DocumentRow,
    Image,
    Record,
    Role,
    Space,
)

CHANNEL = "arctic_catalog_changes"
SNAPSHOT = "select * from coordination.catalog_snapshot()"
CHANGES = """select number,relation,previous,current from coordination.catalog_changes
             where number>%s order by number"""


class Grant(Record):
    document_id: UUID7
    account_id: UUID7
    role: Role


class PublishedCheckpoint(Record):
    document_id: UUID7
    revision: UUID7
    key: str
    published_at: AwareDatetime


class ImageReference(Record):
    document_id: UUID7
    image_id: UUID7


class SnapshotRows(Record):
    accounts: list[Account]
    spaces: list[Space]
    documents: list[DocumentRow]
    grants: list[Grant]
    checkpoints: list[PublishedCheckpoint]
    images: list[Image]
    image_references: list[ImageReference]


class Snapshot(Record):
    number: int
    records: SnapshotRows


class Change(Record):
    number: int
    relation: Literal[
        "accounts",
        "spaces",
        "documents",
        "grants",
        "checkpoints",
        "images",
        "image_references",
    ]
    previous: dict[str, object] | None
    current: dict[str, object] | None


class Index[K, T: Record]:
    def __init__(self, key: Callable[[T], K], rows: Iterable[T]) -> None:
        self._key = key
        self._values = {key(row): row for row in rows}

    def get(self, key: K) -> T | None:
        return self._values.get(key)

    def apply(self, previous: T | None, current: T | None) -> None:
        if previous is not None:
            self._values.pop(self._key(previous), None)
        if current is not None:
            self._values[self._key(current)] = current


class Groups[K, T: Record]:
    def __init__(
        self, group: Callable[[T], UUID], key: Callable[[T], K], rows: Iterable[T]
    ) -> None:
        self._group = group
        self._key = key
        self._groups: dict[UUID, dict[K, T]] = {}
        for row in rows:
            self.apply(None, row)

    def members(self, key: UUID) -> dict[K, T]:
        return self._groups.get(key, {})

    def apply(self, previous: T | None, current: T | None) -> None:
        if previous is not None:
            group = self._group(previous)
            rows = self._groups.get(group)
            if rows is not None:
                rows.pop(self._key(previous), None)
                if not rows:
                    del self._groups[group]
        if current is not None:
            self._groups.setdefault(self._group(current), {})[self._key(current)] = (
                current
            )


class CatalogRecords:
    def __init__(self, rows: SnapshotRows) -> None:
        self.accounts = Index(lambda a: a.id, rows.accounts)
        self.identities = Index(
            lambda a: Identity(issuer=a.issuer, subject=a.subject), rows.accounts
        )
        self.spaces = Groups(lambda s: s.owner, lambda s: s.id, rows.spaces)
        self.documents = Index(lambda d: d.id, rows.documents)
        self.grants = Groups(
            lambda g: g.document_id, lambda g: g.account_id, rows.grants
        )
        self.library = Groups(
            lambda g: g.account_id, lambda g: g.document_id, rows.grants
        )
        self.checkpoints = Groups(
            lambda c: c.document_id, lambda c: c.revision, rows.checkpoints
        )
        self.images = Index(lambda i: i.id, rows.images)
        self.uploaded_images = Groups(lambda i: i.account, lambda i: i.id, rows.images)
        self.image_references = Groups(
            lambda r: r.image_id, lambda r: r.document_id, rows.image_references
        )

    def apply(self, change: Change) -> None:
        match change.relation:
            case "accounts":
                accounts = _delta(Account, change)
                self.accounts.apply(*accounts)
                self.identities.apply(*accounts)
            case "spaces":
                self.spaces.apply(*_delta(Space, change))
            case "documents":
                self.documents.apply(*_delta(DocumentRow, change))
            case "grants":
                grants = _delta(Grant, change)
                self.grants.apply(*grants)
                self.library.apply(*grants)
            case "checkpoints":
                self.checkpoints.apply(*_delta(PublishedCheckpoint, change))
            case "images":
                images = _delta(Image, change)
                self.images.apply(*images)
                self.uploaded_images.apply(*images)
            case "image_references":
                self.image_references.apply(*_delta(ImageReference, change))

    def document(self, account: UUID, document_id: UUID) -> Document | None:
        grant = self.grants.members(document_id).get(account)
        row = self.documents.get(document_id)
        return (
            row.with_role(grant.role) if row is not None and grant is not None else None
        )


def _delta[T: Record](model: type[T], change: Change) -> tuple[T | None, T | None]:
    return (
        model.model_validate(change.previous) if change.previous is not None else None,
        model.model_validate(change.current) if change.current is not None else None,
    )


class CatalogCopy:
    def __init__(self) -> None:
        self._records: CatalogRecords | None = None
        self.applied = 0
        self.notified = 0
        self.connected = False

    @property
    def loaded(self) -> bool:
        return self._records is not None

    @property
    def readable(self) -> CatalogRecords | None:
        if self.connected and self.applied >= self.notified:
            return self._records
        return None

    def load(self, snapshot: Snapshot) -> None:
        self._records = CatalogRecords(snapshot.records)
        self.applied = snapshot.number

    def apply(self, changes: Iterable[Change]) -> None:
        if self._records is None:
            return
        for change in changes:
            if change.number > self.applied:
                self._records.apply(change)
                self.applied = change.number

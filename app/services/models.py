"""Validated catalog rows. No document bytes cross these boundaries."""

from typing import Literal

from pydantic import UUID7, AwareDatetime, BaseModel, ConfigDict

type Role = Literal["owner", "editor", "viewer"]
type CheckpointState = Literal["issued", "published", "pending", "deleted"]


class Record(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Account(Record):
    id: UUID7
    issuer: str
    subject: str
    avatar_id: UUID7 | None
    last_sync: AwareDatetime | None
    created_at: AwareDatetime


class Space(Record):
    id: UUID7
    owner: UUID7
    created_at: AwareDatetime


class DocumentRow(Record):
    id: UUID7
    space: UUID7
    title: str
    trashed_at: AwareDatetime | None
    revision: UUID7 | None
    key: str | None
    published_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    def with_role(self, role: Role) -> Document:
        return Document.model_validate({**self.model_dump(), "role": role})


class Document(DocumentRow):
    role: Role


class Image(Record):
    id: UUID7
    account: UUID7
    key: str
    size: int
    hash: str
    created_at: AwareDatetime
    deleted_at: AwareDatetime | None

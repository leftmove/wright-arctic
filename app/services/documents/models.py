"""Typed database rows and native reconstruction results."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue

type Role = Literal["owner", "editor", "viewer"]
type SchemaName = Literal["basic", "wright-rich-text"]


class Document(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: UUID
    space_id: UUID
    created_by: UUID
    owner_user_id: UUID
    canonical_name: str
    title: str
    title_is_user_set: bool
    format_version: int
    schema_name: SchemaName
    schema_version: int
    head_seq: int
    metadata_seq: int
    access_version: int
    created_at: datetime
    updated_at: datetime
    trashed_at: datetime | None
    role: Role


@dataclass(frozen=True)
class Native:
    state: bytes
    inferred_title: str | None
    projection: JsonValue
    update: bytes = b""


@dataclass(frozen=True)
class Prefix:
    document: Document
    through_seq: int
    updates: list[bytes]
    metadata: dict[str, JsonValue]

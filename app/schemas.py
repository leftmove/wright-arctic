"""Public metadata and capability contracts. Document content is never an API body."""

from typing import Annotated, Literal, Self

from pydantic import (
    UUID7,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from app.services.limits import (
    MAX_BATCH_ITEMS,
    MAX_CHECKPOINT_BYTES,
    MAX_IMAGE_BYTES,
)
from app.services.models import Role

type EntityId = Annotated[UUID7, Field(strict=False)]
type Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RootResponse(ResponseModel):
    service: str
    docs_url: Literal["/docs"] = "/docs"
    health_url: Literal["/health"] = "/health"


class HealthResponse(ResponseModel):
    status: Literal["ok"] = "ok"
    service: str


class HelloResponse(ResponseModel):
    message: str


class ErrorResponse(ResponseModel):
    code: str
    message: str


def error_body(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


class AccountResponse(ResponseModel):
    id: UUID7
    avatar_id: UUID7 | None
    last_sync: AwareDatetime | None
    created_at: AwareDatetime


class AvatarSelection(Command):
    image_id: EntityId | None


class SpaceResponse(ResponseModel):
    id: UUID7
    owner: UUID7
    created_at: AwareDatetime


class CreateSpace(Command):
    id: EntityId


class CreateDocument(Command):
    id: EntityId
    space: EntityId
    title: str = Field(default="Untitled", max_length=255)


class RenameDocument(Command):
    title: str = Field(max_length=255)


class DocumentSummary(ResponseModel):
    id: UUID7
    space: UUID7
    title: str
    trashed_at: AwareDatetime | None
    revision: UUID7 | None
    key: str | None
    published_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    role: Role


class GrantDocument(Command):
    role: Literal["editor", "viewer"]


class DocumentGrant(ResponseModel):
    document_id: UUID7
    account_id: UUID7
    role: Role


class Permission(ResponseModel):
    method: Literal["GET", "PUT"]
    url: str
    headers: dict[str, str]
    expires_at: AwareDatetime


class Pointer(ResponseModel):
    document_id: UUID7
    revision: UUID7 | None
    key: str | None
    published_at: AwareDatetime | None
    read: Permission | None


class CheckpointUpload(Command):
    document_id: EntityId
    size: int = Field(ge=1, le=MAX_CHECKPOINT_BYTES)
    hash: Sha256


class StartBatch(Command):
    id: EntityId
    documents: list[CheckpointUpload] = Field(min_length=1, max_length=MAX_BATCH_ITEMS)

    @model_validator(mode="after")
    def _unique_documents(self) -> Self:
        if len({item.document_id for item in self.documents}) != len(self.documents):
            raise ValueError("Each document may appear once per request")
        return self


class RetryUpload(Command):
    previous_revision: EntityId
    base_revision: EntityId | None
    size: int = Field(ge=1, le=MAX_CHECKPOINT_BYTES)
    hash: Sha256


class UploadSlot(ResponseModel):
    document_id: UUID7
    current: Pointer
    base_revision: UUID7 | None
    revision: UUID7
    key: str
    upload: Permission | None
    refusals: int
    result: Literal[
        "issued", "published", "conflict", "forbidden", "invalid", "expired", "replaced"
    ]


class BatchResponse(ResponseModel):
    id: UUID7
    started_at: AwareDatetime
    expires_at: AwareDatetime
    documents: list[UploadSlot]


class ImageReferences(Command):
    images: list[EntityId] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_images(self) -> Self:
        if len(set(self.images)) != len(self.images):
            raise ValueError("Image identifiers must be unique")
        return self


class PublishItem(ImageReferences):
    document_id: EntityId
    base_revision: EntityId | None
    revision: EntityId
    key: str = Field(min_length=1, max_length=160)


class PublishBatch(Command):
    documents: list[PublishItem] = Field(min_length=1, max_length=MAX_BATCH_ITEMS)

    @model_validator(mode="after")
    def _unique_documents(self) -> Self:
        if len({item.document_id for item in self.documents}) != len(self.documents):
            raise ValueError("Each document may appear once per request")
        return self


class PublishResult(ResponseModel):
    document_id: UUID7
    revision: UUID7
    result: Literal["published", "conflict", "forbidden", "invalid", "expired"]
    current: Pointer | None
    retry_allowed: bool


class PublishResponse(ResponseModel):
    id: UUID7
    documents: list[PublishResult]
    cleanup_pending: bool


class IssueImage(Command):
    id: EntityId
    size: int = Field(ge=1, le=MAX_IMAGE_BYTES)
    hash: Sha256
    media_type: Literal["image/avif"] = "image/avif"


class ImageResponse(ResponseModel):
    id: UUID7
    account: UUID7
    size: int
    hash: str
    created_at: AwareDatetime
    media_type: Literal["image/avif"] = "image/avif"
    url: str


class ImageUploadResponse(ResponseModel):
    id: UUID7
    key: str
    upload: Permission | None
    image: ImageResponse | None

"""Validated API response contracts."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
)


class ResponseModel(BaseModel):
    """Use explicit, immutable response contracts without implicit coercion."""

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


class ImageSummary(ResponseModel):
    id: UUID
    media_type: Literal["image/jxl", "image/avif", "image/webp"]
    byte_size: int = Field(ge=1)
    url: AnyHttpUrl
    url_expires_in: int | None = Field(
        default=None,
        ge=1,
        description="Seconds until url stops working. Null when url does not expire.",
    )
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _created_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("created_at must be timezone-aware")
        return value


class ImageResponse(ImageSummary):
    width: int = Field(ge=1)
    height: int = Field(ge=1)


class ImageListResponse(ResponseModel):
    images: list[ImageSummary]


class PortraitSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    image_id: UUID = Field(strict=False)
    if_absent: bool = False


def error_body(code: str, message: str) -> dict[str, str]:
    """Return the JSON object used for image API failures."""
    payload = ErrorResponse(code=code, message=message)
    return {"code": payload.code, "message": payload.message}


class DocumentCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    operation_id: UUID = Field(strict=False)


class UploadDocument(DocumentCommand):
    title: str = Field(max_length=255)
    title_is_user_set: bool
    format_version: Literal[1] = 1
    schema_name: Literal["basic", "wright-rich-text"] = "wright-rich-text"
    schema_version: Literal[1] = 1


class CreateDocument(DocumentCommand):
    schema_name: Literal["basic", "wright-rich-text"] = "wright-rich-text"
    title: str | None = Field(default=None, max_length=255)
    content: dict[str, JsonValue] | None = None


class AppendDocuments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    operations: list[Annotated[UUID, Field(strict=False)]] = Field(
        min_length=1, max_length=128
    )


class RenameDocument(DocumentCommand):
    expected_metadata_seq: int = Field(ge=0)
    title: str | None = Field(default=None, max_length=255)


class ShareDocument(DocumentCommand):
    user_id: UUID | None = Field(default=None, strict=False)
    role: Literal["editor", "viewer"] | None = None


class GrantDocument(DocumentCommand):
    role: Literal["editor", "viewer"] | None


class RestoreDocument(DocumentCommand):
    through_seq: int = Field(ge=0)


class DocumentSummary(ResponseModel):
    id: UUID
    space_id: UUID
    title: str
    title_is_user_set: bool
    format_version: int
    schema_name: str
    schema_version: int
    head_seq: int
    metadata_seq: int
    access_version: int
    role: Literal["owner", "editor", "viewer"]
    created_at: datetime
    updated_at: datetime
    trashed_at: datetime | None
    path: str


class DocumentReceipt(ResponseModel):
    document_id: UUID
    space_id: UUID
    operation_id: UUID
    head_seq: int
    path: str | None = None


class DocumentContent(ResponseModel):
    document: DocumentSummary
    content: JsonValue


class DocumentGrant(ResponseModel):
    user_id: UUID
    role: Literal["editor", "viewer"]
    granted_by: UUID
    granted_at: datetime


class DocumentChange(ResponseModel):
    seq: int
    operation_id: UUID
    actor_user_id: UUID
    source: str
    kind: str
    metadata: dict[str, JsonValue]
    recorded_at: datetime

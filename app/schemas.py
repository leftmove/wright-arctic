"""Validated API response contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator


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
    media_type: Literal["image/avif", "image/webp"]
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


def error_body(code: str, message: str) -> dict[str, str]:
    """Return the JSON object used for image API failures."""
    payload = ErrorResponse(code=code, message=message)
    return {"code": payload.code, "message": payload.message}

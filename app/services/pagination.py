"""Additive, validated cursors for descending timestamps with ascending UUID ties."""

import base64
import binascii
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import UUID7, AwareDatetime, BaseModel, ConfigDict, ValidationError

from app.services.errors import ServiceError


class PageCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    timestamp: AwareDatetime
    id: UUID7


def decode_cursor(value: str | None, *, offset: int) -> PageCursor | None:
    if value is None:
        return None
    try:
        if offset or len(value)>256:
            raise ValueError("Use a cursor without an offset")
        return PageCursor.model_validate_json(base64.b64decode(value+"="*(-len(value)%4),altchars=b"-_",validate=True))
    except ValueError, ValidationError, binascii.Error:
        raise ServiceError("invalid_cursor", "Use a valid page cursor without an offset.", 422) from None


def encode_cursor(timestamp: datetime, identifier: UUID) -> str:
    return base64.urlsafe_b64encode(PageCursor(timestamp=timestamp,id=identifier).model_dump_json().encode()).rstrip(b"=").decode("ascii")

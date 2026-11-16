"""Typed boundaries for narrow private catalog operations."""

from collections.abc import Sequence
from datetime import datetime
from math import ceil
from typing import Literal

from pydantic import AwareDatetime, TypeAdapter

from app.services.database import AsyncSession
from app.services.errors import Conflict, Forbidden, NotFound, ServiceError
from app.services.models import Record


class Failure(Record):
    error: str | None = None
    next_start: AwareDatetime | None = None
    now: AwareDatetime | None = None


async def operation[T: Record](
    session: AsyncSession,
    function: Literal[
        "reserve_batch",
        "publish_preflight",
        "publish_verified_batch",
        "rename_document",
        "image_preflight",
        "confirm_verified_image",
    ],
    parameters: Sequence[object],
    result_type: type[T],
) -> T:
    placeholders = {
        "reserve_batch": "%s,%s,%s::jsonb,%s",
        "publish_preflight": "%s,%s,%s::jsonb",
        "publish_verified_batch": "%s,%s,%s::jsonb",
        "rename_document": "%s,%s,%s",
        "image_preflight": "%s,%s",
        "confirm_verified_image": "%s,%s,%s,%s,%s,%s,%s",
    }[function]
    rows = await session.execute(
        f"select coordination.{function}({placeholders}) as result", parameters
    )
    result = rows[0]["result"]
    failure = Failure.model_validate(result)
    if failure.error is not None:
        error = failure.error
        if error == "not_found":
            raise NotFound()
        if error == "forbidden":
            raise Forbidden()
        if error == "sync_window":
            start = TypeAdapter(datetime).validate_python(failure.next_start)
            now = TypeAdapter(datetime).validate_python(failure.now)
            raise ServiceError(
                error,
                f"The next batch may start at {start.isoformat()}.",
                429,
                headers={
                    "Retry-After": str(max(1, ceil((start - now).total_seconds()))),
                    "X-Next-Sync": start.isoformat(),
                },
            )
        if error in {"batch_expired", "batch_sealed", "image_upload_expired"}:
            raise ServiceError(error, "This batch admission window has closed.", 410)
        raise Conflict(error)
    return result_type.model_validate(result)

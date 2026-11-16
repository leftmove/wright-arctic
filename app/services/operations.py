"""Typed boundaries for grouped catalog operations."""

import json
from collections.abc import Sequence
from datetime import datetime
from math import ceil
from typing import Literal, NoReturn

from pydantic import AwareDatetime, TypeAdapter

from app.services.database import AsyncDatabase
from app.services.errors import Conflict, Forbidden, NotFound, ServiceError, Unavailable
from app.services.identity.actor import Actor, actor_query
from app.services.models import Record

type Operation = Literal[
    "reserve_batch",
    "publish_preflight",
    "publish_verified_batch",
    "rename_document",
    "image_preflight",
    "confirm_verified_image",
    "create_space",
    "create_document",
    "set_grant",
    "document_lifecycle",
    "select_avatar",
    "issue_image",
    "delete_image",
    "stage_images",
    "retry_upload",
]


class Failure(Record):
    error: str | None = None
    next_start: AwareDatetime | None = None
    now: AwareDatetime | None = None


def rejection(failure: Failure) -> NoReturn:
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
    if error == "trash_expired":
        raise ServiceError(error, "The trash retention period has elapsed.", 410)
    if error == "retry_limit":
        raise ServiceError(
            error, "Three uploads were refused. Wait for the next save trigger.", 429
        )
    if error == "image_quota_exceeded":
        raise ServiceError(
            error, "The daily or stored image quota has been reached.", 429
        )
    if error == "image_referenced":
        raise ServiceError(
            error,
            "Images referenced by document history or a profile cannot be deleted.",
        )
    if error is None:
        raise Unavailable()
    raise Conflict(error)


def check_rejection(sqlstate: str | None, detail: str | None) -> None:
    if sqlstate == "PZ001" and detail is not None:
        rejection(Failure.model_validate_json(detail))


async def operation[T: Record](
    database: AsyncDatabase,
    function: Operation,
    actor: Actor,
    parameters: Sequence[object],
    result_type: type[T],
) -> T:
    placeholders = {
        "reserve_batch": "%s,%s::jsonb,%s",
        "publish_preflight": "%s,%s::jsonb",
        "publish_verified_batch": "%s,%s::jsonb",
        "rename_document": "%s,%s",
        "image_preflight": "%s",
        "confirm_verified_image": "%s,%s,%s,%s,%s,%s",
        "create_space": "%s",
        "create_document": "%s,%s,%s",
        "set_grant": "%s,%s,%s",
        "document_lifecycle": "%s,%s,%s",
        "select_avatar": "%s",
        "issue_image": "%s,%s,%s,%s,%s,%s",
        "delete_image": "%s",
        "stage_images": "%s,%s,%s,%s::uuid[]",
        "retry_upload": "%s,%s,%s,%s,%s,%s,%s,%s",
    }[function]
    call = f"coordination.{function}(actor.id,{placeholders})"
    read_only = function in {"image_preflight", "publish_preflight"}
    if not read_only:
        call = f"coordination.checked_result({call})"
    query, values = actor_query(
        actor, f"select {call} as result from actor", parameters
    )
    execute = database.fetch if read_only else database.write
    rows = await execute(query, values)
    result = rows[0]["result"]
    if isinstance(result, str):
        # asyncpg uses text for JSON by default.
        result = json.loads(result)
    failure = Failure.model_validate(result)
    if failure.error is not None:
        rejection(failure)
    return result_type.model_validate(result)

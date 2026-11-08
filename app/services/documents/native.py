"""The only Python boundary for actual native document processing."""

import json

from pydantic import JsonValue, TypeAdapter

import engine
from app.services.documents.errors import Conflict, InvalidNative, TooLarge
from app.services.documents.models import Native, SchemaName

MAX_STATE = 512 * 1024
MAX_UPDATE = 128 * 1024
MAX_BATCH = 256 * 1024

_PROJECTION = TypeAdapter(JsonValue)


def checked(result: engine.NativeDocument) -> Native:
    return Native(
        result.state,
        result.inferred_title,
        _PROJECTION.validate_json(result.projection),
        result.update,
    )


def merge(schema: SchemaName, updates: list[bytes]) -> Native:
    try:
        return checked(engine.native_merge(schema, updates))
    except ValueError as error:
        code = str(error)
        if code == "native_state_too_large":
            raise TooLarge() from None
        if code == "missing_dependencies":
            raise Conflict("missing_dependencies") from None
        raise InvalidNative() from None


def create(schema: SchemaName, content: dict[str, JsonValue] | None) -> Native:
    try:
        value = (
            None
            if content is None
            else json.dumps(
                {"version": 1, "schema": schema, "doc": content}, allow_nan=False
            )
        )
        return checked(engine.native_create(schema, value))
    except ValueError:
        raise InvalidNative("invalid_native_model") from None


def restore(schema: SchemaName, current: bytes, historical: bytes) -> Native:
    try:
        return checked(engine.native_restore(schema, current, historical))
    except ValueError:
        raise InvalidNative("invalid_native_restore") from None

"""Validated API response contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


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

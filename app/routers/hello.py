"""Minimal API example backed by Rust."""

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import StringConstraints

from app.schemas import HelloResponse
from app.services.greeting import create_greeting

type GreetingName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]

router = APIRouter(tags=["hello"])


@router.get("/hello", response_model=HelloResponse)
def hello(
    name: Annotated[GreetingName, Query(description="Name to greet.")] = "world",
) -> HelloResponse:
    """Return a greeting produced by the compiled Rust engine."""
    return HelloResponse(message=create_greeting(name))

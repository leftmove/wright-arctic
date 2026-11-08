"""Explicit SQL values, independent of the PostgreSQL client library."""

from dataclasses import dataclass


@dataclass(frozen=True)
class JsonParameter:
    obj: object

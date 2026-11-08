"""Encode a space's allocated sharing ordinal with the official Sqids library.

Call while holding the space lock in Arctic's sharing transaction. Never decode
a URL to find a document: resolve the exact stored key.
"""

from importlib.metadata import version
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqids import Sqids


class _Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    library_version: Literal["0.5.2"]
    alphabet: Literal["abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"]
    min_length: Literal[6]
    blocklist: tuple[str, ...]


_CONFIG = _Configuration.model_validate_json(
    Path(__file__).with_name("sqids.json").read_text(encoding="utf-8")
)
if version("sqids") != _CONFIG.library_version:
    raise RuntimeError("Review the document Sqids configuration before upgrading")
_ENCODER = Sqids(
    alphabet=_CONFIG.alphabet,
    min_length=_CONFIG.min_length,
    blocklist=list(_CONFIG.blocklist),
)


def encode_sharing_number(number: int) -> str:
    """Return the exact string to persist; existing slugs are never re-encoded."""
    if isinstance(number, bool) or not 1 <= number <= 2**63 - 1:
        raise ValueError("Sharing ordinal must be a positive Postgres bigint")
    return _ENCODER.encode([number])

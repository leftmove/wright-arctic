"""Temporary account identity.

Authentication will replace the dependency that builds this value. Image
storage only uses `Account.id`.
"""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Account:
    """Caller the image API attributes uploads to."""

    id: UUID

"""Stable, account-scoped identities for exact source bytes."""

import hashlib
import re
from uuid import UUID

from app.accounts import Account

SOURCE_SHA256_PATTERN = r"^[0-9a-f]{64}$"


def source_digest(payload: bytes) -> str:
    """Fingerprint the bounded upload, before any lossy processing."""
    return hashlib.sha256(payload).hexdigest()


def content_id(account: Account, digest: str) -> UUID:
    """Keep content identities separate across accounts, issuers and portraits.

    The full digest is also stored and checked as object metadata. UUIDv8 keeps
    the existing image API and storage layout while making concurrent uploads
    of the same source converge on one immutable object.
    """
    if re.fullmatch(SOURCE_SHA256_PATTERN, digest) is None:
        raise ValueError("Invalid image source digest")
    identity = f"wright:images:source:v1:{account.storage_key}:{digest}".encode()
    return UUID(bytes=hashlib.sha256(identity).digest()[:16], version=8)

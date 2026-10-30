"""Verified account identity, independent of token rotation and device sessions."""

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Account:
    """An issuer-scoped subject obtained exclusively from a verified access token."""

    id: str
    issuer: str

    @property
    def storage_key(self) -> str:
        """A stable, path-safe namespace that cannot collide across issuers.

        Never map identities onto the old, caller-selected UUID namespaces.
        The full SHA-256 digest avoids truncation and keeps opaque provider IDs
        out of object keys. This is namespacing, not a secret or a credential.
        """
        identity = json.dumps(
            [self.issuer, self.id], ensure_ascii=True, separators=(",", ":")
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

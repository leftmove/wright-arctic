"""External sign-in identity; internal accounts are allocated by the catalog."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Identity:
    """An issuer-scoped subject obtained exclusively from a verified access token."""

    subject: str
    issuer: str


@dataclass(frozen=True, slots=True)
class VerifiedActor:
    """Request-local actor; resolution belongs to the domain unit of work."""

    identity: Identity

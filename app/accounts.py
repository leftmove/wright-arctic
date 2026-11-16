"""External sign-in identity; internal accounts are allocated by the catalog."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Identity:
    """An issuer-scoped subject obtained exclusively from a verified access token."""

    subject: str
    issuer: str

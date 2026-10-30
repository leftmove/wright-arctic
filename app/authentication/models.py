"""Strict boundaries around untrusted JWT and public-key metadata."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

type KeyId = Annotated[str, Field(min_length=1, max_length=128)]
type SigningAlgorithm = Literal["RS256", "ES256"]


class TokenHeader(BaseModel):
    """Only support the JOSE features used by native access tokens.

    Embedded keys, key URLs, critical extensions and unencoded payloads are
    deliberately unsupported. The configured endpoint is the only key source.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    alg: SigningAlgorithm
    kid: KeyId
    typ: Literal["JWT"] = "JWT"


class AccessClaims(BaseModel):
    """Claims used after cryptographic verification; extra profile claims are ignored."""

    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    iss: str = Field(min_length=1)
    sub: str = Field(min_length=1, max_length=256)
    aud: str | list[str]
    exp: int = Field(gt=0)
    iat: int = Field(gt=0)
    nbf: int | None = None


class PublicKeySet(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    keys: list[dict[str, JsonValue]] = Field(min_length=1, max_length=32)


class RSAPublicParameters(BaseModel):
    """Bound public integers before passing them to the cryptography library."""

    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    n: str = Field(min_length=1, max_length=1366, pattern=r"^[A-Za-z0-9_-]+$")
    e: str = Field(min_length=1, max_length=8, pattern=r"^[A-Za-z0-9_-]+$")


class ECPublicParameters(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    crv: Literal["P-256"]
    x: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")
    y: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")

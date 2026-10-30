"""Verify native access JWTs without handling passwords or renewal secrets."""

import re

import httpx
import jwt
from pydantic import ValidationError

from app.accounts import Account
from app.authentication.errors import InvalidCredentials
from app.authentication.jwks import SigningKeys
from app.authentication.models import AccessClaims, TokenHeader
from app.settings import Settings

# Reject malformed/oversized compact JWTs before JSON parsing or any key fetch.
_COMPACT_JWT = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", re.ASCII)
_MAX_TOKEN_LENGTH = 8192


class AuthenticationService:
    """Verify every request against one explicitly trusted issuer and audience."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
    ) -> None:
        if settings.auth_issuer is None or settings.auth_jwks_url is None:
            raise ValueError("Authentication requires an issuer and a JWKS URL")
        self._issuer = settings.auth_issuer
        self._audience = settings.auth_audience
        self._algorithm = settings.auth_algorithm
        self._max_lifetime = settings.auth_max_token_lifetime_seconds
        self._client = client
        self._keys = SigningKeys(client, str(settings.auth_jwks_url), self._algorithm)

    async def close(self) -> None:
        """Release the public-key HTTP connection pool."""
        await self._client.aclose()

    async def authenticate(self, token: str) -> Account:
        """Return identity only after signature, scope and time checks succeed."""
        if len(token) > _MAX_TOKEN_LENGTH or _COMPACT_JWT.fullmatch(token) is None:
            raise InvalidCredentials()
        try:
            header = TokenHeader.model_validate(jwt.get_unverified_header(token))
            if header.alg != self._algorithm:
                raise InvalidCredentials()
            key = await self._keys.get(header.kid)
            claims = AccessClaims.model_validate(
                jwt.decode(
                    token,
                    key,
                    algorithms=[self._algorithm],
                    issuer=self._issuer,
                    audience=self._audience,
                    options={"require": ["iss", "sub", "aud", "exp", "iat"]},
                )
            )
        except jwt.PyJWTError, ValidationError, ValueError:
            raise InvalidCredentials() from None
        if (
            claims.exp <= claims.iat
            or claims.exp - claims.iat > self._max_lifetime
            or claims.sub != claims.sub.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in claims.sub)
        ):
            raise InvalidCredentials()
        return Account(id=claims.sub, issuer=claims.iss)


def build_authentication_service(settings: Settings) -> AuthenticationService | None:
    """Leave protected routes closed when authentication is unconfigured."""
    if settings.auth_issuer is None:
        return None
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(3, connect=2),
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        follow_redirects=False,
        trust_env=False,
    )
    return AuthenticationService(settings, client)

"""Bounded, rotating public-key cache for one configured HTTPS endpoint."""

import asyncio
import logging
from time import monotonic

import httpx
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt import PyJWK, PyJWTError
from pydantic import JsonValue, ValidationError

from app.authentication.errors import AuthenticationUnavailable, InvalidCredentials
from app.authentication.models import (
    ECPublicParameters,
    PublicKeySet,
    RSAPublicParameters,
    SigningAlgorithm,
)

logger = logging.getLogger(__name__)

_KEY_CACHE_SECONDS = 300
_REFRESH_INTERVAL_SECONDS = 30
_MAX_JWKS_BYTES = 65_536
_PRIVATE_PARAMETERS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "k"})


class SigningKeys:
    """Share fetches and bound unknown-kid traffic without ever using stale keys."""

    def __init__(
        self, client: httpx.AsyncClient, url: str, algorithm: SigningAlgorithm
    ) -> None:
        self._client = client
        self._url = url
        self._algorithm = algorithm
        self._keys: dict[str, PyJWK] = {}
        self._expires_at = 0.0
        self._refresh_after = 0.0
        self._lock = asyncio.Lock()

    async def get(self, key_id: str) -> PyJWK:
        now = monotonic()
        if now < self._expires_at and key_id in self._keys:
            return self._keys[key_id]
        async with self._lock:
            now = monotonic()
            if now < self._expires_at and key_id in self._keys:
                return self._keys[key_id]
            if now < self._refresh_after:
                if now < self._expires_at:
                    raise InvalidCredentials()
                raise AuthenticationUnavailable()
            # Both failed and successful fetches are throttled. The cache is
            # replaced atomically, so removed keys disappear on the next fetch.
            self._refresh_after = now + _REFRESH_INTERVAL_SECONDS
            try:
                keys = await self._fetch()
            except AuthenticationUnavailable:
                logger.warning("authentication public keys are unavailable")
                raise
            self._keys = keys
            self._expires_at = monotonic() + _KEY_CACHE_SECONDS
            key = keys.get(key_id)
            if key is None:
                raise InvalidCredentials()
            return key

    async def _fetch(self) -> dict[str, PyJWK]:
        try:
            async with asyncio.timeout(5):
                async with self._client.stream(
                    "GET",
                    self._url,
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    if response.status_code != 200:
                        raise AuthenticationUnavailable()
                    # A compressed response can allocate far beyond the body
                    # budget during decompression. We explicitly request identity.
                    if (
                        response.headers.get("Content-Encoding", "identity").lower()
                        != "identity"
                    ):
                        raise AuthenticationUnavailable()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > _MAX_JWKS_BYTES:
                            raise AuthenticationUnavailable()
                        body.extend(chunk)
            document = PublicKeySet.model_validate_json(body)
            return _signing_keys(document, self._algorithm)
        except httpx.HTTPError, TimeoutError, ValidationError, PyJWTError, ValueError:
            raise AuthenticationUnavailable() from None


def _signing_keys(
    document: PublicKeySet, algorithm: SigningAlgorithm
) -> dict[str, PyJWK]:
    keys: dict[str, PyJWK] = {}
    for raw in document.keys:
        if not _eligible(raw, algorithm):
            continue
        key_id = raw.get("kid")
        if not isinstance(key_id, str) or not 1 <= len(key_id) <= 128:
            raise AuthenticationUnavailable()
        if key_id in keys or _PRIVATE_PARAMETERS.intersection(raw):
            raise AuthenticationUnavailable()
        if algorithm == "RS256":
            RSAPublicParameters.model_validate(raw)
        else:
            ECPublicParameters.model_validate(raw)
        key = PyJWK.from_dict(raw, algorithm=algorithm)
        public_key = key.key
        if algorithm == "RS256":
            if not isinstance(public_key, rsa.RSAPublicKey):
                raise AuthenticationUnavailable()
            if not 2048 <= public_key.key_size <= 8192:
                raise AuthenticationUnavailable()
        elif not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
            public_key.curve, ec.SECP256R1
        ):
            raise AuthenticationUnavailable()
        keys[key_id] = key
    if not keys:
        raise AuthenticationUnavailable()
    return keys


def _eligible(raw: dict[str, JsonValue], algorithm: SigningAlgorithm) -> bool:
    if raw.get("kty") != ("RSA" if algorithm == "RS256" else "EC"):
        return False
    if raw.get("alg") not in (None, algorithm) or raw.get("use") not in (None, "sig"):
        return False
    operations = raw.get("key_ops")
    return operations is None or (
        isinstance(operations, list) and "verify" in operations
    )

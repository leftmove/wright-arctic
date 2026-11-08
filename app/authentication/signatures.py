"""Signature verification supplied by the host's cryptographic implementation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

from pydantic import JsonValue

from app.authentication.errors import AuthenticationUnavailable
from app.authentication.models import SigningAlgorithm

if TYPE_CHECKING:
    from jwt import PyJWK


class SignatureKey(Protocol):
    async def verify(self, token: str) -> None: ...


type KeyLoader = Callable[
    [dict[str, JsonValue], SigningAlgorithm], Awaitable[SignatureKey]
]


class NativeSignatureKey:
    def __init__(self, key: PyJWK, algorithm: SigningAlgorithm) -> None:
        self._key = key
        self._algorithm = algorithm

    async def verify(self, token: str) -> None:
        from jwt import PyJWS

        PyJWS().decode(token, self._key, algorithms=[self._algorithm])


async def load_native_key(
    raw: dict[str, JsonValue], algorithm: SigningAlgorithm
) -> SignatureKey:
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    from jwt import PyJWK

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
    return NativeSignatureKey(key, algorithm)

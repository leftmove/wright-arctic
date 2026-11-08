"""Web Crypto signature checks without shipping an OpenSSL runtime to Workers."""

import importlib
import json
from collections.abc import Awaitable, Callable
from typing import Protocol, cast

from jwt import InvalidSignatureError
from jwt.utils import base64url_decode
from pydantic import JsonValue

from app.authentication.errors import AuthenticationUnavailable
from app.authentication.models import SigningAlgorithm
from app.authentication.signatures import SignatureKey


class CryptoKey(Protocol):
    algorithm: object


class SubtleCrypto(Protocol):
    def importKey(
        self,
        format: str,
        data: object,
        algorithm: object,
        extractable: bool,
        usages: object,
    ) -> Awaitable[CryptoKey]: ...
    def verify(
        self, algorithm: object, key: CryptoKey, signature: object, data: object
    ) -> Awaitable[bool]: ...


class Crypto(Protocol):
    subtle: SubtleCrypto


class JSON(Protocol):
    def parse(self, value: str) -> object: ...


_js = importlib.import_module("js")
_json = cast(JSON, _js.JSON)
_to_js = cast(Callable[[object], object], importlib.import_module("pyodide.ffi").to_js)


def _subtle() -> SubtleCrypto:
    # Deployment snapshots temporarily wrap crypto to prevent entropy use.
    # Access the host API only while handling a request.
    return cast(Crypto, _js.crypto).subtle


class WebSignatureKey:
    def __init__(self, key: CryptoKey, algorithm: dict[str, str]) -> None:
        self._key = key
        self._algorithm = _json.parse(json.dumps(algorithm))

    async def verify(self, token: str) -> None:
        message, signature = token.rsplit(".", 1)
        signature_bytes = base64url_decode(signature)
        try:
            valid = await _subtle().verify(
                self._algorithm,
                self._key,
                _to_js(signature_bytes),
                _to_js(message.encode("ascii")),
            )
        except Exception:
            raise AuthenticationUnavailable() from None
        if not valid:
            raise InvalidSignatureError()


async def load_web_key(
    raw: dict[str, JsonValue], algorithm: SigningAlgorithm
) -> SignatureKey:
    import_algorithm = (
        {"name": "RSASSA-PKCS1-v1_5", "hash": "SHA-256"}
        if algorithm == "RS256"
        else {"name": "ECDSA", "namedCurve": "P-256"}
    )
    try:
        key = await _subtle().importKey(
            "jwk",
            _json.parse(json.dumps(raw)),
            _json.parse(json.dumps(import_algorithm)),
            False,
            _json.parse('["verify"]'),
        )
    except Exception:
        raise AuthenticationUnavailable() from None
    if algorithm == "RS256":
        size: object = getattr(key.algorithm, "modulusLength", None)
        if not isinstance(size, int) or not 2048 <= size <= 8192:
            raise AuthenticationUnavailable()
    verify_algorithm = (
        {"name": "RSASSA-PKCS1-v1_5"}
        if algorithm == "RS256"
        else {"name": "ECDSA", "hash": "SHA-256"}
    )
    return WebSignatureKey(key, verify_algorithm)

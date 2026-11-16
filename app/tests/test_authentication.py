"""Exercise real signatures, hostile credentials, key rotation and image isolation."""

import asyncio
import json
import time
import unittest
from typing import ClassVar
from unittest.mock import patch

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from pydantic import JsonValue, ValidationError
from pydantic_settings import SettingsConfigDict

from app.accounts import Identity
from app.authentication.errors import AuthenticationUnavailable, InvalidCredentials
from app.authentication.jwks import SigningKeys
from app.authentication.service import (
    AuthenticationService,
    build_authentication_service,
)
from app.settings import Settings

_ISSUER = "https://issuer.example/auth/v1"
_JWKS = _ISSUER + "/.well-known/jwks.json"


class TestSettings(Settings):
    model_config = SettingsConfigDict(env_file=None, env_prefix="WRIGHT_ARCTIC_TEST_")


def configuration(**overrides: JsonValue) -> Settings:
    return TestSettings.model_validate(
        {
            "documents_database_url": None,
            "documents_setup_url": None,
            "auth_issuer": _ISSUER,
            "auth_jwks_url": _JWKS,
            "auth_publishable_key": None
            if overrides.get("auth_issuer", _ISSUER) is None
            else "sb_publishable_test",
            "auth_algorithm": "RS256",
            **overrides,
        }
    )


def token(
    key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey,
    *,
    claims: dict[str, JsonValue] | None = None,
    headers: dict[str, str] | None = None,
    algorithm: str = "RS256",
) -> str:
    now = int(time.time())
    payload: dict[str, JsonValue] = {
        "iss": _ISSUER,
        "sub": "11111111-1111-4111-8111-111111111111",
        "aud": "authenticated",
        "iat": now - 1,
        "role": "authenticated",
        "is_anonymous": False,
        "session_id": "33333333-3333-4333-8333-333333333333",
        "aal": "aal2",
        "amr": [
            {"method": "password", "timestamp": now - 1},
            {"method": "totp", "timestamp": now - 1},
        ],
        "exp": now + 899,
    }
    if claims is not None:
        payload.update(claims)
    return jwt.encode(
        payload, key, algorithm=algorithm, headers={"kid": "one", **(headers or {})}
    )


def public_jwk(key: rsa.RSAPrivateKey) -> dict[str, JsonValue]:
    raw: dict[str, JsonValue] = json.loads(
        jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())
    )
    return {**raw, "kid": "one", "use": "sig", "alg": "RS256"}


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    private_key: ClassVar[rsa.RSAPrivateKey]
    other_key: ClassVar[rsa.RSAPrivateKey]

    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    async def asyncSetUp(self) -> None:
        self.policy = True
        self.policy_status = 200
        self.policy_fetches = 0
        self.fetches = 0
        self.status = 200
        self.document: dict[str, JsonValue] = {"keys": [public_jwk(self.private_key)]}
        self.body: bytes | None = None

        def fetch(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/v1/rpc/account_access":
                self.policy_fetches += 1
                self.assertTrue(request.headers["authorization"].startswith("Bearer "))
                self.assertEqual(request.headers["apikey"], "sb_publishable_test")
                return httpx.Response(self.policy_status, json=self.policy)
            self.fetches += 1
            self.assertEqual(str(request.url), _JWKS)
            self.assertNotIn("authorization", request.headers)
            if self.body is not None:
                return httpx.Response(self.status, content=self.body)
            return httpx.Response(self.status, json=self.document)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(fetch))
        self.service = AuthenticationService(configuration(), self.client)

    async def asyncTearDown(self) -> None:
        await self.service.close()

    async def test_native_token_and_renewal_share_an_issuer_scoped_account(
        self,
    ) -> None:
        first = await self.service.authenticate(token(self.private_key))
        renewed = await self.service.authenticate(
            token(self.private_key, claims={"jti": "renewed"})
        )
        self.assertEqual(
            first,
            Identity(subject="11111111-1111-4111-8111-111111111111", issuer=_ISSUER),
        )
        self.assertEqual(first, renewed)
        self.assertNotEqual(
            first,
            Identity(subject=first.subject, issuer="https://other.example"),
        )
        self.assertEqual(self.fetches, 1)

    async def test_audience_arrays_are_accepted(self) -> None:
        account = await self.service.authenticate(
            token(self.private_key, claims={"aud": ["other", "authenticated"]})
        )
        self.assertEqual(account.subject, "11111111-1111-4111-8111-111111111111")

    async def test_signature_forgery_is_rejected(self) -> None:
        with self.assertRaises(InvalidCredentials):
            await self.service.authenticate(token(self.other_key))

    async def test_current_session_factor_policy_is_rechecked_on_every_request(
        self,
    ) -> None:
        value = token(self.private_key)
        await self.service.authenticate(value)
        self.policy = False
        with self.assertRaises(InvalidCredentials):
            await self.service.authenticate(value)
        self.policy = True
        self.policy_status = 503
        with self.assertRaises(AuthenticationUnavailable):
            await self.service.authenticate(value)
        self.assertEqual(self.policy_fetches, 3)
        self.assertEqual(self.fetches, 1)

    async def test_first_factor_maintenance_and_linked_google_do_not_grant_access(
        self,
    ) -> None:
        for methods, assurance in (
            (["password"], "aal1"),
            (["passkey"], "aal1"),
            (["recovery", "totp"], "aal2"),
            (["email/signup", "totp"], "aal2"),
            (["oauth_provider/authorization_code"], "aal1"),
        ):
            claims: dict[str, JsonValue] = {
                "aal": assurance,
                "amr": [
                    {"method": method, "timestamp": int(time.time())}
                    for method in methods
                ],
                "app_metadata": {"providers": ["google"]},
            }
            with self.subTest(methods=methods), self.assertRaises(InvalidCredentials):
                await self.service.authenticate(token(self.private_key, claims=claims))
        self.assertEqual(self.policy_fetches, 0)
        google: dict[str, JsonValue] = {
            "aal": "aal1",
            "amr": [{"method": "oauth", "timestamp": int(time.time())}],
        }
        await self.service.authenticate(token(self.private_key, claims=google))
        self.assertEqual(self.policy_fetches, 1)

    async def test_invalid_scope_and_claim_types_are_rejected(self) -> None:
        now = int(time.time())
        cases: list[dict[str, JsonValue]] = [
            {"iss": "https://other.example"},
            {"iss": _ISSUER + "/"},
            {"aud": "other"},
            {"aud": []},
            {"sub": ""},
            {"sub": 123},
            {"sub": " account"},
            {"sub": "a\x00b"},
            {"sub": "a" * 257},
            {"exp": now - 1},
            {"exp": str(now + 60)},
            {"exp": True},
            {"exp": []},
            {"iat": {}},
            {"nbf": None},
            {"iat": now + 60},
            {"iat": str(now - 1)},
            {"iat": False},
            {"nbf": now + 60},
            {"nbf": "0"},
            # Pin both endpoints: minting the token across a second boundary
            # must not turn this overlong lifetime into an exactly valid one.
            {"exp": now + 3600, "iat": now - 1},
            {"exp": now - 1, "iat": now},
        ]
        for claims in cases:
            with self.subTest(claims=claims), self.assertRaises(InvalidCredentials):
                await self.service.authenticate(token(self.private_key, claims=claims))

    async def test_required_claims_cannot_be_omitted(self) -> None:
        payload = jwt.decode(
            token(self.private_key), options={"verify_signature": False}
        )
        for name in ("iss", "sub", "aud", "exp", "iat"):
            incomplete = {key: value for key, value in payload.items() if key != name}
            value = jwt.encode(
                incomplete, self.private_key, algorithm="RS256", headers={"kid": "one"}
            )
            with self.subTest(name=name), self.assertRaises(InvalidCredentials):
                await self.service.authenticate(value)

    async def test_unsupported_headers_and_malformed_tokens_do_not_fetch_keys(
        self,
    ) -> None:
        values = ["", "opaque", "a.b.c", "a" * 8193, "a.b.c.d"]
        for headers in (
            {"jku": "https://attacker.example/keys"},
            {"typ": "other"},
            {"crit": "b64"},
            {"kid": ""},
        ):
            values.append(token(self.private_key, headers=headers))
        values.append(
            jwt.encode(
                {"sub": "attacker"}, "s" * 32, algorithm="HS256", headers={"kid": "one"}
            )
        )
        values.append(jwt.encode({"sub": "attacker"}, "", algorithm="none"))
        for value in values:
            with self.subTest(value=value[:20]), self.assertRaises(InvalidCredentials):
                await self.service.authenticate(value)
        self.assertEqual(self.fetches, 0)

    async def test_parallel_requests_share_one_fetch(self) -> None:
        accounts = await asyncio.gather(
            *(self.service.authenticate(token(self.private_key)) for _ in range(20))
        )
        self.assertEqual(len(accounts), 20)
        self.assertEqual(self.fetches, 1)

    async def test_unknown_key_ids_cannot_amplify_fetches(self) -> None:
        await self.service.authenticate(token(self.private_key))
        for index in range(20):
            with self.assertRaises(InvalidCredentials):
                await self.service.authenticate(
                    token(self.private_key, headers={"kid": str(index)})
                )
        self.assertEqual(self.fetches, 1)

    async def test_rotation_refreshes_and_removes_old_keys(self) -> None:
        keys = SigningKeys(self.client, _JWKS, "RS256")
        with patch("app.authentication.jwks.monotonic", return_value=100):
            await keys.get("one")
        new = {**public_jwk(self.other_key), "kid": "two"}
        self.document = {"keys": [new]}
        with patch("app.authentication.jwks.monotonic", return_value=131):
            await keys.get("two")
            with self.assertRaises(InvalidCredentials):
                await keys.get("one")
        self.assertEqual(self.fetches, 2)

    async def test_expired_cache_is_not_used_during_an_outage(self) -> None:
        keys = SigningKeys(self.client, _JWKS, "RS256")
        with patch("app.authentication.jwks.monotonic", return_value=100):
            await keys.get("one")
        self.status = 503
        with patch("app.authentication.jwks.monotonic", return_value=401):
            for _ in range(3):
                with self.assertRaises(AuthenticationUnavailable):
                    await keys.get("one")
        self.assertEqual(self.fetches, 2)
        self.status = 200
        with patch("app.authentication.jwks.monotonic", return_value=432):
            await keys.get("one")
        self.assertEqual(self.fetches, 3)

    async def test_bad_key_documents_fail_closed(self) -> None:
        valid = public_jwk(self.private_key)
        cases: list[dict[str, JsonValue]] = [
            {"keys": []},
            {"keys": [valid, valid]},
            {"keys": [{**valid, "n": 42}]},
            {"keys": [{**valid, "e": None}]},
            {"keys": [{**valid, "d": "secret"}]},
            {"keys": [{**valid, "use": "enc"}]},
            {"keys": [{**valid, "key_ops": ["sign"]}]},
            {"keys": [{**valid, "alg": "HS256"}]},
        ]
        for document in cases:
            self.document = document
            with (
                self.subTest(document=document),
                self.assertRaises(AuthenticationUnavailable),
            ):
                await SigningKeys(self.client, _JWKS, "RS256").get("one")
        for body in (b"not json", b"x" * 65_537):
            self.body = body
            with self.assertRaises(AuthenticationUnavailable):
                await SigningKeys(self.client, _JWKS, "RS256").get("one")

    async def test_redirects_are_not_followed(self) -> None:
        self.status = 302
        with self.assertRaises(AuthenticationUnavailable):
            await self.service.authenticate(token(self.private_key))
        self.assertEqual(self.fetches, 1)

    async def test_encoded_key_responses_are_rejected_before_decompression(
        self,
    ) -> None:
        async def encoded_response(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["Accept-Encoding"], "identity")
            return httpx.Response(
                200,
                headers={"Content-Encoding": "gzip"},
                stream=httpx.ByteStream(b"not actually gzip"),
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(encoded_response)
        ) as client:
            service = AuthenticationService(configuration(), client)
            with self.assertRaises(AuthenticationUnavailable):
                await service.authenticate(token(self.private_key))

    async def test_es256_requires_explicit_configuration(self) -> None:
        private = ec.generate_private_key(ec.SECP256R1())
        raw: dict[str, JsonValue] = json.loads(
            jwt.algorithms.ECAlgorithm.to_jwk(private.public_key())
        )
        self.document = {"keys": [{**raw, "kid": "one", "alg": "ES256"}]}
        es_service = AuthenticationService(
            configuration(auth_algorithm="ES256"), self.client
        )
        account = await es_service.authenticate(token(private, algorithm="ES256"))
        self.assertEqual(account.subject, "11111111-1111-4111-8111-111111111111")
        with self.assertRaises(InvalidCredentials):
            await self.service.authenticate(token(private, algorithm="ES256"))


class ConfigurationAndStorageTests(unittest.TestCase):
    def test_authentication_configuration_is_complete_and_https_only(self) -> None:
        for overrides in (
            {"auth_issuer": None},
            {"auth_jwks_url": None},
            {"auth_issuer": "http://issuer.example"},
            {"auth_jwks_url": "http://issuer.example/keys"},
            {"auth_issuer": "https://user:secret@issuer.example"},
            {"auth_jwks_url": "https://issuer.example/keys?secret=x"},
            {"auth_algorithm": "HS256"},
            {"auth_max_token_lifetime_seconds": 3601},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                configuration(**overrides)
        with self.assertRaises(ValidationError):
            configuration(auth_issuer=_ISSUER + "/")
        self.assertIsNone(
            build_authentication_service(
                configuration(auth_issuer=None, auth_jwks_url=None)
            )
        )

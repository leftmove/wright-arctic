"""Exercise real signatures, hostile credentials, key rotation and image isolation."""

import asyncio
import json
import time
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from io import BytesIO
from typing import ClassVar
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
import imagecodecs
import jwt
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI
from PIL import Image
from pydantic import AnyHttpUrl, JsonValue, ValidationError

from app.accounts import Account
from app.authentication.errors import AuthenticationUnavailable, InvalidCredentials
from app.authentication.jwks import SigningKeys
from app.authentication.service import (
    AuthenticationService,
    build_authentication_service,
)
from app.main import create_app
from app.schemas import ImageListResponse, ImageResponse
from app.services.images.errors import ImageNotFound
from app.services.images.limits import MAX_UPLOAD_BODY_BYTES
from app.services.images.service import ImageService
from app.services.images.storage import S3ImageStore, account_prefix, object_key
from app.settings import Settings
from app.tests.image_storage import memory_storage

_ISSUER = "https://issuer.example"
_JWKS = _ISSUER + "/.well-known/jwks.json"


def configuration(**overrides: JsonValue) -> Settings:
    return Settings.model_validate(
        {
            "auth_issuer": _ISSUER,
            "auth_jwks_url": _JWKS,
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
        "sub": "native-account-id",
        "aud": "convex",
        "iat": now - 1,
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
        self.fetches = 0
        self.status = 200
        self.document: dict[str, JsonValue] = {"keys": [public_jwk(self.private_key)]}
        self.body: bytes | None = None

        def fetch(request: httpx.Request) -> httpx.Response:
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
        self.assertEqual(first, Account(id="native-account-id", issuer=_ISSUER))
        self.assertEqual(first.storage_key, renewed.storage_key)
        self.assertNotEqual(
            first.storage_key,
            Account(id=first.id, issuer="https://other.example").storage_key,
        )
        self.assertEqual(self.fetches, 1)
        self.assertEqual(len(first.storage_key), 64)

    async def test_audience_arrays_are_accepted(self) -> None:
        account = await self.service.authenticate(
            token(self.private_key, claims={"aud": ["other", "convex"]})
        )
        self.assertEqual(account.id, "native-account-id")

    async def test_signature_forgery_is_rejected(self) -> None:
        with self.assertRaises(InvalidCredentials):
            await self.service.authenticate(token(self.other_key))

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
            {"exp": now + 3600},
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
        self.assertEqual(account.id, "native-account-id")
        with self.assertRaises(InvalidCredentials):
            await self.service.authenticate(token(private, algorithm="ES256"))

    @asynccontextmanager
    async def application(
        self, image_service: ImageService | None = None
    ) -> AsyncIterator[tuple[httpx.AsyncClient, FastAPI]]:
        with (
            patch("app.main.get_settings", return_value=configuration()),
            patch("app.main.build_authentication_service", return_value=self.service),
            patch("app.main.build_image_service", return_value=image_service),
        ):
            app = create_app()
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://arctic.example",
            ) as client,
        ):
            yield client, app

    async def test_all_image_operations_reject_the_old_account_header(self) -> None:
        async with self.application() as (client, _):
            for method, path in (
                ("GET", "/images"),
                ("POST", "/images"),
                ("GET", "/images/" + str(uuid4())),
                ("DELETE", "/images/" + str(uuid4())),
            ):
                response = await client.request(
                    method, path, headers={"X-Account-Id": str(uuid4())}
                )
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["code"], "authentication_required")
                self.assertIn("Bearer", response.headers["WWW-Authenticate"])
        self.assertEqual(self.fetches, 0)

    async def test_bad_bearer_transport_is_rejected(self) -> None:
        valid = "Bearer " + token(self.private_key)
        cases: list[list[tuple[str, str]]] = [
            [("Authorization", "Basic anything")],
            [("Authorization", "Bearer")],
            [("Authorization", "Bearer ")],
            [("Authorization", "Bearer  token")],
            [("Authorization", valid), ("Authorization", valid)],
        ]
        async with self.application() as (client, _):
            for headers in cases:
                response = await client.get("/images", headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["code"], "invalid_token")
            response = await client.get(
                "/images", params={"access_token": token(self.private_key)}
            )
            self.assertEqual(response.status_code, 401)

    async def test_upload_authentication_happens_before_body_reading(self) -> None:
        read_attempts = 0

        async def unreadable_body() -> AsyncIterator[bytes]:
            nonlocal read_attempts
            read_attempts += 1
            yield b"upload"

        async with self.application() as (client, _):
            response = await client.post("/images", content=unreadable_body())
            self.assertEqual(response.status_code, 401)
            self.assertEqual(read_attempts, 0)

    async def test_mounted_upload_authenticates_before_body_reading(self) -> None:
        read_attempts = 0

        async def body() -> AsyncIterator[bytes]:
            nonlocal read_attempts
            read_attempts += 1
            yield b"--wright--\r\n"

        async with self.application() as (_, app):
            parent = FastAPI()
            parent.mount("/api", app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=parent),
                base_url="https://arctic.example",
            ) as client:
                response = await client.post(
                    "/api/images",
                    content=body(),
                    headers={"Content-Type": "multipart/form-data; boundary=wright"},
                )
                self.assertEqual(response.status_code, 401)
                self.assertEqual(read_attempts, 0)

    async def test_mounted_authenticated_upload_enforces_the_size_limit(self) -> None:
        async with self.application() as (_, app):
            parent = FastAPI()
            parent.mount("/api", app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=parent),
                base_url="https://arctic.example",
            ) as client:
                response = await client.post(
                    "/api/images",
                    content=b"",
                    headers={
                        "Authorization": "Bearer " + token(self.private_key),
                        "Content-Length": str(MAX_UPLOAD_BODY_BYTES + 1),
                    },
                )
                self.assertEqual(response.status_code, 413)
                self.assertEqual(response.json()["code"], "image_too_large")

    async def test_health_is_public_and_authenticated_requests_reach_storage(
        self,
    ) -> None:
        async with self.application() as (client, app):
            self.assertEqual((await client.get("/health")).status_code, 200)
            response = await client.get(
                "/images",
                headers={"Authorization": "bearer " + token(self.private_key)},
            )
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["code"], "storage_unconfigured")
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")
            schema = app.openapi()
            self.assertEqual(
                schema["paths"]["/images"]["get"]["security"],
                [{"NativeAccessToken": []}],
            )
            self.assertNotIn("X-Account-Id", json.dumps(schema))
        self.assertTrue(self.client.is_closed)

    async def test_image_owner_is_only_the_verified_subject(self) -> None:
        images = Mock(spec=ImageService)
        images.list_images.return_value = ImageListResponse(images=[])
        async with self.application(images) as (client, _):
            response = await client.get(
                "/images",
                headers={
                    "Authorization": "Bearer " + token(self.private_key),
                    "X-Account-Id": "someone-else",
                },
            )
            self.assertEqual(response.status_code, 200)
            images.list_images.assert_called_once_with(
                Account(id="native-account-id", issuer=_ISSUER)
            )

    async def test_jwks_outage_is_retryable_and_does_not_leak_credentials(self) -> None:
        self.status = 503
        async with self.application() as (client, _):
            value = token(self.private_key)
            response = await client.get(
                "/images", headers={"Authorization": "Bearer " + value}
            )
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["Retry-After"], "30")
            self.assertNotIn(value, response.text)
            self.assertNotIn(_JWKS, response.text)

    async def test_create_read_and_delete_use_the_verified_account(self) -> None:
        images = Mock(spec=ImageService)
        image_id = uuid4()
        images.create.return_value = ImageResponse(
            id=image_id,
            media_type="image/webp",
            byte_size=10,
            width=1,
            height=1,
            url=AnyHttpUrl("https://images.example/signed"),
            url_expires_in=900,
            created_at=datetime.now(UTC),
        )
        images.get_image.return_value = images.create.return_value
        headers = {"Authorization": "Bearer " + token(self.private_key)}
        account = Account(id="native-account-id", issuer=_ISSUER)
        async with self.application(images) as (client, _):
            response = await client.post(
                "/images",
                headers=headers,
                files={"file": ("image.png", b"upload", "image/png")},
            )
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.headers["Location"], "/images/" + str(image_id))
            self.assertEqual(images.create.call_args.args[0], account)
            response = await client.get("/images/" + str(image_id), headers=headers)
            self.assertEqual(response.status_code, 200)
            images.get_image.assert_called_once_with(account, image_id, format="webp")
            response = await client.delete("/images/" + str(image_id), headers=headers)
            self.assertEqual(response.status_code, 204)
            images.delete_image.assert_called_once_with(account, image_id)

    async def test_real_image_upload_is_stored_in_the_verified_namespace(self) -> None:
        storage_client = Mock()
        storage_client.list_objects_v2.return_value = {"Contents": []}
        storage_client.generate_presigned_url.return_value = (
            "https://images.example/signed"
        )
        images = ImageService(S3ImageStore(storage_client, "images", None, 900))
        source = BytesIO()
        Image.new("RGB", (2, 2), "red").save(source, format="PNG")
        async with self.application(images) as (client, _):
            response = await client.post(
                "/images",
                headers={"Authorization": "Bearer " + token(self.private_key)},
                files={"file": ("image.png", source.getvalue(), "image/png")},
            )
            self.assertEqual(response.status_code, 201, response.text)
            key = storage_client.put_object.call_args.kwargs["Key"]
            self.assertTrue(
                key.startswith(
                    account_prefix(Account(id="native-account-id", issuer=_ISSUER))
                )
            )
            self.assertIn(
                storage_client.put_object.call_args.kwargs["ContentType"],
                ("image/jxl",),
            )

    async def test_portrait_import_is_webp_bounded_and_idempotent(self) -> None:
        storage_client = memory_storage()
        images = ImageService(S3ImageStore(storage_client, "images", None, 900))
        image_id = uuid4()
        source = BytesIO()
        Image.new("RGB", (1200, 800), "red").save(source, format="JPEG")
        async with self.application(images) as (client, _):
            headers = {"Authorization": "Bearer " + token(self.private_key)}
            path = f"/images/{image_id}/portrait"
            first = await client.put(
                path,
                headers=headers,
                files={"file": ("portrait.jpg", source.getvalue(), "image/jpeg")},
            )
            self.assertEqual(first.status_code, 200, first.text)
            payload = first.json()
            self.assertEqual(payload["id"], str(image_id))
            self.assertEqual(payload["media_type"], "image/webp")
            self.assertLessEqual(max(payload["width"], payload["height"]), 512)
            stored = storage_client.put_object.call_args_list[0].kwargs
            self.assertEqual(stored["IfNoneMatch"], "*")
            self.assertEqual(stored["ContentType"], "image/jxl")
            pixels = imagecodecs.jpegxl_decode(stored["Body"])
            self.assertEqual(pixels.shape[:2], (payload["height"], payload["width"]))
            second = await client.put(
                path,
                headers=headers,
                files={"file": ("ignored.png", b"invalid retry bytes", "image/png")},
            )
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(second.json()["id"], payload["id"])
            self.assertEqual(storage_client.put_object.call_count, 2)

    async def test_portrait_upload_budget_is_enforced_before_parsing(self) -> None:
        async with self.application(Mock(spec=ImageService)) as (client, _):
            response = await client.put(
                f"/images/{uuid4()}/portrait",
                headers={"Authorization": "Bearer " + token(self.private_key)},
                content=b"x" * (MAX_UPLOAD_BODY_BYTES + 1),
            )
            self.assertEqual(response.status_code, 413)

    async def test_unconfigured_authentication_fails_closed(self) -> None:
        with (
            patch(
                "app.main.get_settings",
                return_value=configuration(auth_issuer=None, auth_jwks_url=None),
            ),
            patch("app.main.build_image_service", return_value=None),
        ):
            app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://arctic.example"
        ) as client:
            response = await client.get(
                "/images", headers={"Authorization": "Bearer anything"}
            )
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["code"], "authentication_unavailable")
            self.assertEqual((await client.get("/images")).status_code, 401)
            self.assertEqual((await client.get("/health")).status_code, 200)


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
        self.assertEqual(
            configuration(auth_issuer=_ISSUER + "/").auth_issuer, _ISSUER + "/"
        )
        self.assertIsNone(
            build_authentication_service(
                configuration(auth_issuer=None, auth_jwks_url=None)
            )
        )

    def test_storage_is_isolated_for_opaque_subjects_and_issuers(self) -> None:
        account = Account(id="../../opaque/provider:id", issuer=_ISSUER)
        other = Account(id=account.id, issuer="https://other.example")
        image_id = uuid4()
        client = Mock()
        store = S3ImageStore(client, "images", None, 900)
        self.assertNotEqual(account_prefix(account), account_prefix(other))
        self.assertNotIn("..", account_prefix(account))
        self.assertNotIn("provider", account_prefix(account))
        client.list_objects_v2.return_value = {"Contents": []}
        self.assertEqual(store.list_images(account, limit=10), [])
        client.list_objects_v2.assert_called_once_with(
            Bucket="images", Prefix=account_prefix(account), MaxKeys=1000
        )
        client.generate_presigned_url.return_value = "https://images.example/signed"
        store.url_for(account, image_id, "webp")
        self.assertEqual(
            client.generate_presigned_url.call_args.kwargs["Params"]["Key"],
            object_key(account, image_id, "webp"),
        )

    def test_other_accounts_cannot_read_delete_or_list_an_image(self) -> None:
        owner = Account(id="native-account-id", issuer=_ISSUER)
        other = Account(id="other-account-id", issuer=_ISSUER)
        image_id = uuid4()
        owner_key = object_key(owner, image_id, "webp")
        client = Mock()
        modified = datetime.now(UTC)

        def head(*, Bucket: str, Key: str) -> dict[str, object]:
            if Key != owner_key:
                raise ClientError(
                    {
                        "Error": {"Code": "NoSuchKey"},
                    },
                    "HeadObject",
                )
            return {
                "ContentLength": 10,
                "LastModified": modified,
                "Metadata": {"width": "1", "height": "1"},
            }

        client.head_object.side_effect = head
        client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": owner_key, "Size": 10, "LastModified": modified},
                {
                    "Key": "accounts/"
                    + str(uuid4())
                    + "/images/"
                    + str(image_id)
                    + ".webp",
                    "Size": 10,
                    "LastModified": modified,
                },
            ]
        }
        store = S3ImageStore(client, "images", None, 900)
        self.assertEqual(store.get_image(owner, image_id).id, image_id)
        self.assertEqual(len(store.list_images(owner, limit=10)), 1)
        self.assertEqual(store.list_images(other, limit=10), [])
        with self.assertRaises(ImageNotFound):
            store.get_image(other, image_id)
        with self.assertRaises(ImageNotFound):
            store.delete_image(other, image_id)
        client.delete_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()

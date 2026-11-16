"""Narrow reusable async R2 transport; it exposes no upload or listing operation."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

import httpx

from app.observability import span
from app.services.limits import MAX_IMAGE_BYTES
from app.services.storage.objects import (
    AsyncCheckpointObjects,
    AsyncImageBody,
    AsyncImageObjects,
    ObjectMetadata,
    ObjectMissing,
    StorageUnavailable,
)
from app.services.storage.signing import S3Signer
from app.settings import Settings


class HTTPObjects:
    def __init__(
        self, client: httpx.AsyncClient, signer: S3Signer, limit: asyncio.Semaphore
    ) -> None:
        self._client, self._signer, self._limit = client, signer, limit

    @asynccontextmanager
    async def _request(
        self,
        method: Literal["HEAD", "GET", "DELETE"],
        key: str,
        *,
        maximum: int | None = None,
    ) -> AsyncIterator[httpx.Response]:
        permission = self._signer.request(key, 60, method=method)
        headers = {"Accept-Encoding": "identity"}
        if maximum is not None:
            headers["Range"] = f"bytes=0-{maximum - 1}"
        try:
            async with self._limit:
                with span("r2." + method.lower()):
                    async with self._client.stream(
                        method, permission.url, headers=headers
                    ) as response:
                        if response.status_code == 404:
                            raise ObjectMissing()
                        if (
                            response.status_code not in {200, 204, 206}
                            or response.headers.get("content-encoding", "identity")
                            != "identity"
                        ):
                            raise StorageUnavailable()
                        yield response
        except httpx.HTTPError, TimeoutError, ValueError:
            raise StorageUnavailable() from None

    async def head(self, key: str) -> ObjectMetadata:
        async with self._request("HEAD", key) as response:
            return ObjectMetadata(
                int(response.headers.get("content-length", "-1")),
                response.headers.get("content-type", ""),
                {
                    name.removeprefix("x-amz-meta-"): value
                    for name, value in response.headers.items()
                    if name.startswith("x-amz-meta-")
                },
            )

    async def delete(self, key: str) -> None:
        async with self._request("DELETE", key):
            pass

    async def prefix(self, key: str, maximum: int) -> bytes:
        if not 1 <= maximum <= 512:
            raise ValueError("Image inspection is bounded to 512 bytes")
        async with self._request("GET", key, maximum=maximum) as response:
            # Closing the streaming response bounds reading even if Range is
            # ignored. Never use response.aread() on an untrusted object.
            async for chunk in response.aiter_bytes(chunk_size=maximum):
                return chunk[:maximum]
        return b""

    async def stream(self, key: str) -> AsyncImageBody:
        context = self._request("GET", key)
        response = await context.__aenter__()

        async def close() -> None:
            await context.__aexit__(None, None, None)

        try:
            size = int(response.headers.get("content-length", "-1"))
            if (
                not 0 < size <= MAX_IMAGE_BYTES
                or response.headers.get("content-type") != "image/avif"
            ):
                raise StorageUnavailable()

            async def chunks() -> AsyncIterator[bytes]:
                remaining = size
                try:
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(chunk) > remaining:
                            raise StorageUnavailable()
                        remaining -= len(chunk)
                        yield chunk
                    if remaining:
                        raise StorageUnavailable()
                except httpx.HTTPError:
                    raise StorageUnavailable() from None

            return AsyncImageBody(size, chunks(), close)
        except BaseException:
            await close()
            raise


@dataclass(frozen=True)
class AsyncStorage:
    checkpoints: AsyncCheckpointObjects
    checkpoint_signer: S3Signer
    images: AsyncImageObjects
    image_signer: S3Signer
    client: httpx.AsyncClient | None = None
    close_transport: Callable[[], Awaitable[None]] | None = None

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()
        if self.close_transport is not None:
            await self.close_transport()


def build_native_storage(settings: Settings) -> AsyncStorage | None:
    from app.services.storage.factory import signer

    documents, images = signer(settings, "documents"), signer(settings, "images")
    if documents is None or images is None:
        return None
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(4, connect=2, pool=0.25),
        limits=httpx.Limits(
            max_connections=settings.storage_concurrency,
            max_keepalive_connections=settings.storage_concurrency,
        ),
        follow_redirects=False,
        trust_env=False,
    )
    limit = asyncio.Semaphore(settings.storage_concurrency)
    return AsyncStorage(
        HTTPObjects(client, documents, limit),
        documents,
        HTTPObjects(client, images, limit),
        images,
        client,
    )

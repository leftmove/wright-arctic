"""Opt-in live R2 protocol verification using only disposable, cataloged image keys.

Run through Flyer: uv run python -m app.tests.r2_conformance. No document content
is read, and all created keys are deleted in a finally block. The metadata
catalog is the local PGlite fixture, never a hosted application database.
"""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import httpx

from app.accounts import Identity
from app.schemas import IssueImage
from app.services.errors import ServiceError
from app.services.identity.service import IdentityService
from app.services.images.service import ImageService
from app.services.limits import MAX_IMAGE_BYTES
from app.services.storage.objects import ImageObjects, ObjectMissing
from app.services.storage.signing import S3Signer
from app.tests import async_support
from app.tests.document_database import FixtureDatabase


class ConformanceError(RuntimeError):
    pass


def verify(objects: ImageObjects, signer: S3Signer) -> dict[str, bool]:
    header = b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00avifmif1"
    payload = header + b"\x00" * (MAX_IMAGE_BYTES - len(header))
    digest = hashlib.sha256(payload).hexdigest()
    report: dict[str, bool] = {}
    database = FixtureDatabase()
    keys: list[str] = []
    try:
        account = async_support.run(
            IdentityService(async_support.database(database)).resolve(
                Identity(issuer="https://r2-conformance.invalid", subject=str(uuid7()))
            )
        )
        media = ImageService(
            async_support.database(database), async_support.images(objects), signer
        )
        slot = async_support.run(
            media.issue(account, IssueImage(id=uuid7(), size=len(payload), hash=digest))
        )
        other = async_support.run(
            media.issue(account, IssueImage(id=uuid7(), size=len(payload), hash=digest))
        )
        keys.extend([slot.key, other.key])
        assert slot.upload is not None
        upload = slot.upload
        with httpx.Client(
            timeout=30, follow_redirects=False, trust_env=False
        ) as client:

            def require(name: str, response: httpx.Response, *, accepted: bool) -> None:
                matched = (
                    response.status_code == 200
                    if accepted
                    else 400 <= response.status_code < 500
                )
                if not matched:
                    code = re.search(
                        rb"<Code>([A-Za-z0-9]+)</Code>", response.content[:4096]
                    )
                    detail = (
                        "" if code is None else " (" + code[1].decode("ascii") + ")"
                    )
                    raise ConformanceError(
                        f"{name}: unexpected HTTP status {response.status_code}{detail}"
                    )
                report[name] = True

            require(
                "put",
                client.put(upload.url, headers=upload.headers, content=payload),
                accepted=True,
            )
            require(
                "identical_replay",
                client.put(upload.url, headers=upload.headers, content=payload),
                accepted=True,
            )
            require(
                "key_scope",
                client.put(
                    upload.url.replace(str(slot.id), str(other.id)),
                    headers=upload.headers,
                    content=payload,
                ),
                accepted=False,
            )
            require(
                "size_binding",
                client.put(
                    upload.url,
                    headers={**upload.headers, "content-length": str(len(payload) + 1)},
                    content=payload + b"x",
                ),
                accepted=False,
            )
            require(
                "hash_binding",
                client.put(
                    upload.url, headers=upload.headers, content=payload[:-1] + b"x"
                ),
                accepted=False,
            )
            expired = signer.permission(
                slot.key,
                60,
                method="GET",
                now=datetime.now(UTC) - timedelta(minutes=10),
            )
            require("expiry", client.get(expired.url), accepted=False)
            confirmed = async_support.run(media.confirm(account, slot.id))
            report["image_confirm_2mb"] = confirmed.size == MAX_IMAGE_BYTES
            read = signer.permission(slot.key, 60, method="GET")
            response = client.get(read.url)
            require("get", response, accepted=True)
            require(
                "method_scope", client.put(read.url, content=payload), accepted=False
            )
            if response.content != payload:
                raise ConformanceError(
                    "The stored bytes did not match the authorized payload"
                )
    finally:
        try:
            failed: list[str] = []
            for key in keys:
                try:
                    objects.delete(key)
                    objects.head(key)
                except ObjectMissing:
                    continue
                except ServiceError:
                    failed.append(key)
                else:
                    failed.append(key)
            if failed:
                raise ConformanceError(
                    "Disposable keys still need exact-key cleanup: " + ", ".join(failed)
                )
            report["cleanup"] = True
        finally:
            database.close()
    return report


def main() -> None:
    from app.dependencies import get_settings
    from app.services.storage.factory import build_storage

    storage = build_storage(get_settings())
    if storage is None:
        raise SystemExit("Configure both private buckets before live R2 verification")
    try:
        print(json.dumps(verify(storage.images, storage.image_signer), sort_keys=True))
    finally:
        storage.close()


if __name__ == "__main__":
    main()

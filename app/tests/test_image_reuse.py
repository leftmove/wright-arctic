"""Account-scoped reuse survives devices, quota, concurrent uploads and deletion."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Barrier
from unittest.mock import patch

from PIL import Image

from app.accounts import Account
from app.services.images.content import content_id, source_digest
from app.services.images.errors import ImageNotFound, ImageQuotaExceeded, StorageInvalid
from app.services.images.limits import TEMPORARY_ACCOUNT_IMAGE_CAP
from app.services.images.models import ProcessedImage
from app.services.images.processing import ImagePipeline, process_image
from app.services.images.service import ImageService
from app.services.images.storage import S3ImageStore
from app.tests.image_storage import memory_storage


def png(color: tuple[int, int, int] = (240, 10, 20)) -> bytes:
    stream = BytesIO()
    with Image.new("RGB", (32, 16), color) as image:
        image.save(stream, format="PNG")
    return stream.getvalue()


class ImageReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = memory_storage()
        self.store = S3ImageStore(self.client, "images", None, 900)
        self.service = ImageService(self.store)
        self.account = Account(id="writer", issuer="https://issuer.example")

    def test_duplicate_survives_service_restart_without_processing_or_writing(
        self,
    ) -> None:
        source = png()
        first = self.service.create(self.account, BytesIO(source), len(source))
        restarted = ImageService(self.store)
        with patch(
            "app.services.images.service.process_image",
            side_effect=AssertionError("A cache hit must not run the codec"),
        ):
            second = restarted.create(self.account, BytesIO(source), None)
            cached = restarted.get_by_source(self.account, source_digest(source))
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.id, cached.id)
        self.assertEqual(first.created_at, second.created_at)
        self.assertEqual(self.client.put_object.call_count, 1)
        self.assertEqual(self.store.count_images(self.account, maximum=11), 1)

    def test_source_lookup_cannot_populate_the_cache_or_cross_an_account(self) -> None:
        source = png()
        digest = source_digest(source)
        with self.assertRaises(ImageNotFound):
            self.service.get_by_source(self.account, digest)
        self.assertEqual(self.client.put_object.call_count, 0)
        first = self.service.create(self.account, BytesIO(source), None)
        for account in (
            Account(id="another-writer", issuer=self.account.issuer),
            Account(id=self.account.id, issuer="https://another-issuer.example"),
        ):
            with self.assertRaises(ImageNotFound):
                self.service.get_by_source(account, digest)
            with self.assertRaises(ImageNotFound):
                self.service.get_image(account, first.id)
            own = self.service.create(account, BytesIO(source), None)
            self.assertNotEqual(first.id, own.id)
            self.assertEqual(self.service.get_by_source(account, digest).id, own.id)

    def test_full_account_reuses_an_image_but_rejects_new_bytes(self) -> None:
        first = self.service.create(self.account, BytesIO(png()), None)
        for index in range(TEMPORARY_ACCOUNT_IMAGE_CAP - 1):
            self.service.create(self.account, BytesIO(png((index, 0, 0))), None)
        duplicate = self.service.create(self.account, BytesIO(png()), None)
        self.assertEqual(first.id, duplicate.id)
        self.assertEqual(
            self.store.count_images(self.account, maximum=11),
            TEMPORARY_ACCOUNT_IMAGE_CAP,
        )
        with self.assertRaises(ImageQuotaExceeded):
            self.service.create(self.account, BytesIO(png((0, 255, 0))), None)

    def test_deleted_source_is_a_miss_and_can_be_uploaded_again(self) -> None:
        source = png()
        first = self.service.create(self.account, BytesIO(source), None)
        self.service.delete_image(self.account, first.id)
        with self.assertRaises(ImageNotFound):
            self.service.get_by_source(self.account, source_digest(source))
        second = self.service.create(self.account, BytesIO(source), None)
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.store.count_images(self.account, maximum=11), 1)
        self.assertEqual(self.client.put_object.call_count, 2)

    def test_concurrent_duplicates_converge_on_one_conditional_object(self) -> None:
        source = png()
        barrier = Barrier(2)

        def process(payload: bytes, pipeline: ImagePipeline) -> ProcessedImage:
            result = process_image(payload, pipeline)
            barrier.wait(timeout=10)
            return result

        with (
            patch("app.services.images.service.process_image", side_effect=process),
            ThreadPoolExecutor(max_workers=2) as workers,
        ):
            futures = [
                workers.submit(self.service.create, self.account, BytesIO(source), None)
                for _ in range(2)
            ]
            images = [future.result(timeout=20) for future in futures]
        self.assertEqual(images[0].id, images[1].id)
        self.assertEqual(self.store.count_images(self.account, maximum=11), 1)
        self.assertTrue(
            all(
                call.kwargs["IfNoneMatch"] == "*"
                for call in self.client.put_object.call_args_list
            )
        )

    def test_full_digest_is_checked_in_addition_to_the_uuid(self) -> None:
        source = png()
        digest = source_digest(source)
        self.store.put_image(
            self.account,
            content_id(self.account, digest),
            process_image(source, ImagePipeline()),
            source_sha256="0" * 64,
        )
        with self.assertRaises(StorageInvalid):
            self.service.get_by_source(self.account, digest)

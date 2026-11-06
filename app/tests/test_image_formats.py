"""Canonical codec, compatibility, quota and migration regressions."""

import hashlib
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import imagecodecs
import numpy as np
from PIL import Image, ImageCms

from app.accounts import Account
from app.services.images import processing
from app.services.images.errors import (
    ImageNotFound,
    ImageQuotaExceeded,
    ImageUnsupported,
    StorageInvalid,
)
from app.services.images.models import ProcessedImage
from app.services.images.processing import ImagePipeline, process_image
from app.services.images.service import ImageService
from app.services.images.storage import S3ImageStore, object_key
from app.tests.image_storage import memory_storage


def png(*, alpha: bool = False, profile: bytes | None = None) -> bytes:
    stream = BytesIO()
    with Image.new(
        "RGBA" if alpha else "RGB",
        (32, 16),
        (240, 10, 20, 128) if alpha else (240, 10, 20),
    ) as image:
        image.save(stream, format="PNG", icc_profile=profile)
    return stream.getvalue()


class ImageFormatTests(unittest.TestCase):
    def test_oversized_graphics_fit_the_budget_without_repeated_refinement(
        self,
    ) -> None:
        pixels = np.random.default_rng(42).integers(
            0, 256, (300, 400, 3), dtype=np.uint8
        )
        stream = BytesIO()
        Image.fromarray(pixels).save(stream, format="PNG")
        with patch(
            "app.services.images.processing._encode", wraps=processing._encode
        ) as encode:
            fitted = process_image(
                stream.getvalue(), ImagePipeline(max_stored_bytes=32 * 1024)
            )
        self.assertLessEqual(len(fitted.data), 32 * 1024)
        self.assertGreater(min(fitted.width, fitted.height), 1)
        self.assertLessEqual(encode.call_count, 3)
        decoded = imagecodecs.jpegxl_decode(fitted.data)
        self.assertEqual(decoded.shape, (fitted.height, fitted.width, 3))

    def test_source_reuse_survives_restart_quota_and_deletion(self) -> None:
        client = memory_storage()
        store = S3ImageStore(client, "images", None, 900)
        account = Account(id="writer", issuer="https://issuer.example")
        payload = png()
        digest = hashlib.sha256(payload).hexdigest()
        service = ImageService(store)
        first = service.create(account, BytesIO(payload), None)
        self.assertEqual(client.put_object.call_args.kwargs["IfNoneMatch"], "*")
        restarted = ImageService(store)
        with patch(
            "app.services.images.service.process_image",
            side_effect=AssertionError("encoded a duplicate"),
        ):
            self.assertEqual(restarted.get_by_source(account, digest).id, first.id)
            with patch.object(store, "count_images", return_value=10):
                self.assertEqual(
                    restarted.create(account, BytesIO(payload), None).id, first.id
                )
                with self.assertRaises(ImageQuotaExceeded):
                    restarted.create(account, BytesIO(png(alpha=True)), None)
        self.assertEqual(client.put_object.call_count, 1)
        for other in (
            Account(id="other", issuer=account.issuer),
            Account(id=account.id, issuer="https://other.example"),
        ):
            with self.assertRaises(ImageNotFound):
                restarted.get_by_source(other, digest)
        restarted.delete_image(account, first.id)
        with self.assertRaises(ImageNotFound):
            restarted.get_by_source(account, digest)
        replacement = restarted.create(account, BytesIO(payload), None)
        self.assertEqual(replacement.id, first.id)
        self.assertEqual(client.put_object.call_count, 2)

    def test_source_lookup_checks_the_full_digest_and_cannot_overwrite_mismatched_metadata(
        self,
    ) -> None:
        client = memory_storage()
        store = S3ImageStore(client, "images", None, 900)
        account = Account(id="writer", issuer="https://issuer.example")
        payload = png()
        service = ImageService(store)
        image = service.create(account, BytesIO(payload), None)
        store.put_image(
            account,
            image.id,
            process_image(payload, ImagePipeline()),
            source_sha256="0" * 64,
        )
        with self.assertRaises(StorageInvalid):
            service.get_by_source(account, hashlib.sha256(payload).hexdigest())
        with self.assertRaises(StorageInvalid):
            service.create(account, BytesIO(payload), None)
        self.assertEqual(client.put_object.call_count, 2)

    def test_concurrent_duplicate_uploads_converge_on_one_immutable_object(
        self,
    ) -> None:
        client = memory_storage()
        store = S3ImageStore(client, "images", None, 900)
        account = Account(id="writer", issuer="https://issuer.example")
        payload = png()
        service = ImageService(store)
        ready = Barrier(2)

        def encode(payload: bytes, pipeline: ImagePipeline) -> ProcessedImage:
            # Both requests must miss before either can publish its object.
            ready.wait(timeout=5)
            return process_image(payload, pipeline)

        with (
            patch("app.services.images.service.process_image", side_effect=encode),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(service.create, account, BytesIO(payload), None)
            second = pool.submit(service.create, account, BytesIO(payload), None)
            self.assertEqual(first.result(timeout=10).id, second.result(timeout=10).id)
        self.assertEqual(store.count_images(account, maximum=11), 1)
        self.assertEqual(client.put_object.call_count, 2)

    def test_indexed_png_profile_is_applied_after_expanding_palette(self) -> None:
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        stream = BytesIO()
        with Image.open(BytesIO(png())) as image, image.quantize() as indexed:
            indexed.save(stream, format="PNG", icc_profile=profile)
        encoded = process_image(stream.getvalue(), ImagePipeline())
        pixels = imagecodecs.jpegxl_decode(encoded.data)
        np.testing.assert_array_equal(pixels[0, 0], [240, 10, 20])

    def test_lossless_graphics_preserve_rgb_alpha_and_strip_metadata(self) -> None:
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        for alpha in (False, True):
            processed = process_image(
                png(alpha=alpha, profile=profile), ImagePipeline()
            )
            self.assertEqual(processed.media_type, "image/jxl")
            pixels = imagecodecs.jpegxl_decode(processed.data)
            self.assertEqual(pixels.shape, (16, 32, 4 if alpha else 3))
            np.testing.assert_array_equal(
                pixels[0, 0], [240, 10, 20, 128] if alpha else [240, 10, 20]
            )
            self.assertNotIn(b"acsp", processed.data)

    def test_upload_decoder_does_not_decode_arbitrary_jxl_without_header_limits(
        self,
    ) -> None:
        encoded = process_image(png(), ImagePipeline())
        with self.assertRaises(ImageUnsupported):
            process_image(encoded.data, ImagePipeline())

    def test_variants_share_an_id_are_cached_and_do_not_consume_quota(self) -> None:
        client = memory_storage()
        store = S3ImageStore(client, "images", None, 900)
        service = ImageService(store)
        account = Account(id="writer", issuer="https://issuer.example")
        image = service.create(account, BytesIO(png(alpha=True)), None)
        self.assertEqual(image.media_type, "image/jxl")
        for format in ("avif", "webp"):
            delivered = service.get_image(account, image.id, format=format)
            self.assertEqual(delivered.id, image.id)
            self.assertEqual(delivered.media_type, "image/" + format)
            variant = store.get_representation(account, image.id, format, variant=True)
            with Image.open(BytesIO(store.read_image(account, variant))) as decoded:
                self.assertEqual(decoded.size, (32, 16))
                pixel = decoded.convert("RGBA").getpixel((0, 0))
                assert isinstance(pixel, tuple)
                self.assertEqual(pixel[3], 128)
        self.assertEqual(client.put_object.call_count, 3)
        reused = service.create(account, BytesIO(png(alpha=True)), None, format="webp")
        self.assertEqual(reused.id, image.id)
        self.assertEqual(reused.media_type, "image/webp")
        service.get_image(account, image.id, format="avif")
        self.assertEqual(client.put_object.call_count, 3)
        self.assertEqual(store.count_images(account, maximum=11), 1)
        self.assertEqual(len(service.list_images(account).images), 1)
        service.delete_image(account, image.id)
        self.assertEqual(store.count_images(account, maximum=11), 0)
        with self.assertRaises(ImageNotFound):
            store.get_representation(account, image.id, "avif", variant=True)

    def test_legacy_migration_keeps_id_urls_pixels_and_deduplicates_inventory(
        self,
    ) -> None:
        client = memory_storage()
        store = S3ImageStore(client, "images", None, 900)
        account = Account(id="writer", issuer="https://issuer.example")
        id = uuid4()
        stream = BytesIO()
        with Image.open(BytesIO(png())) as image:
            image.save(stream, format="WEBP", lossless=True)
        legacy = stream.getvalue()
        store.put_image(
            account, id, ProcessedImage(legacy, 32, 16, "image/webp", "webp")
        )
        original_key = object_key(account, id, "webp")
        converted = ImageService(store).get_image(account, id, format="jxl")
        self.assertEqual(converted.id, id)
        self.assertEqual(converted.media_type, "image/jxl")
        canonical = store.get_image(account, id)
        pixels = imagecodecs.jpegxl_decode(store.read_image(account, canonical))
        np.testing.assert_array_equal(pixels[0, 0], [240, 10, 20])
        self.assertEqual(
            store.read_image(account, store.get_representation(account, id, "webp")),
            legacy,
        )
        self.assertEqual(
            client.put_object.call_args_list[0].kwargs["Key"], original_key
        )
        self.assertEqual(store.count_images(account, maximum=11), 1)
        self.assertEqual(
            [entry.media_type for entry in store.list_images(account, limit=10)],
            ["image/jxl"],
        )
        self.assertLess(converted.created_at, datetime.now(UTC))

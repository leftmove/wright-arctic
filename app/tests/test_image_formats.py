"""Canonical codec, compatibility, quota and migration regressions."""

import unittest
from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

import imagecodecs
import numpy as np
from PIL import Image, ImageCms

from app.accounts import Account
from app.services.images.errors import ImageNotFound, ImageUnsupported
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

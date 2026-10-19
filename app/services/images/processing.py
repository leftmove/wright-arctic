"""Recompress uploaded images into a bounded, metadata-free derivative.

JPEG, PNG, WebP, GIF, and AVIF are accepted. Animations are rejected.
Photographs become AVIF. PNG, GIF, and images with transparency become
lossless WebP when that stays within the stored-byte budget, and AVIF
otherwise. When the preferred encoding is still too large, the long edge is
reduced until the stored object fits. Original bytes, EXIF, XMP, and ICC
profiles are never stored.
"""

import logging
import math
import warnings
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageFile, ImageOps, PngImagePlugin, UnidentifiedImageError

from app.services.images.errors import (
    ImageAnimated,
    ImageDimensions,
    ImageEmpty,
    ImageInvalid,
    ImageServiceError,
    ImageTooLarge,
    ImageUnsupported,
)
from app.services.images.limits import (
    MAX_DECODE_EDGE,
    MAX_DECODE_PIXELS,
    MAX_STORED_BYTES,
    MAX_UPLOAD_BYTES,
)
from app.services.images.models import ImageExtension, ImageMediaType, ProcessedImage

logger = logging.getLogger(__name__)

_ACCEPTED_FORMATS = ("JPEG", "PNG", "WEBP", "GIF", "AVIF")
_PNG_TEXT_CHUNK_BYTES = 1024 * 1024
_PNG_TEXT_MEMORY_BYTES = 8 * 1024 * 1024
_SHRINK_STEPS = 8

Image.MAX_IMAGE_PIXELS = MAX_DECODE_PIXELS
ImageFile.LOAD_TRUNCATED_IMAGES = False
PngImagePlugin.MAX_TEXT_CHUNK = _PNG_TEXT_CHUNK_BYTES
PngImagePlugin.MAX_TEXT_MEMORY = _PNG_TEXT_MEMORY_BYTES


@dataclass(frozen=True, slots=True)
class ImagePipeline:
    """Bounds and encoder settings for one class of stored image."""

    max_upload_bytes: int = MAX_UPLOAD_BYTES
    max_stored_bytes: int = MAX_STORED_BYTES
    max_decode_edge: int = MAX_DECODE_EDGE
    max_decode_pixels: int = MAX_DECODE_PIXELS
    avif_quality: int = 60
    avif_speed: int = 8
    avif_threads: int = 1
    lossless_webp_effort: int = 80
    lossless_webp_method: int = 6
    graphic_lossless_max_bytes: int = 256 * 1024


def process_image(payload: bytes, pipeline: ImagePipeline) -> ProcessedImage:
    """Decode, strip metadata, and recompress an upload within the stored budget."""
    if len(payload) > pipeline.max_upload_bytes:
        raise ImageTooLarge()
    if not payload:
        raise ImageEmpty()
    try:
        return _process(payload, pipeline)
    except ImageServiceError:
        raise
    except Image.DecompressionBombError, Image.DecompressionBombWarning:
        logger.warning("rejected an image above the decode budget")
        raise ImageDimensions() from None
    except UnidentifiedImageError:
        raise ImageUnsupported() from None
    except OSError, SyntaxError, ValueError, EOFError:
        raise ImageInvalid() from None


def _process(payload: bytes, pipeline: ImagePipeline) -> ProcessedImage:
    raw = BytesIO(payload)
    try:
        with Image.open(raw, formats=_ACCEPTED_FORMATS) as opened:
            source_format = opened.format
            if source_format is None or source_format not in _ACCEPTED_FORMATS:
                raise ImageUnsupported()
            _prepare_decode(opened, source_format, pipeline)
            _reject_animation(opened)
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                opened.load()
            _reject_loaded_dimensions(opened.width, opened.height, pipeline)
            working = ImageOps.exif_transpose(opened)
            if working is None:
                raise ImageInvalid()
            try:
                if working is opened:
                    working = opened.copy()
                normalized = _display_image(working)
                if normalized is not working:
                    working.close()
                    working = normalized
                working.info.clear()
                data, media_type, extension, width, height = _encode_within_budget(
                    working, source_format, pipeline
                )
                return ProcessedImage(
                    data=data,
                    width=width,
                    height=height,
                    media_type=media_type,
                    extension=extension,
                )
            finally:
                if working is not opened:
                    working.close()
    finally:
        raw.close()


def _prepare_decode(
    image: Image.Image, source_format: str, pipeline: ImagePipeline
) -> None:
    if image.width < 1 or image.height < 1:
        raise ImageInvalid()
    if not _exceeds_decode_budget(image.width, image.height, pipeline):
        return
    if source_format != "JPEG":
        raise ImageDimensions()
    image.draft("RGB", _draft_box(image.width, image.height, pipeline))


def _reject_loaded_dimensions(width: int, height: int, pipeline: ImagePipeline) -> None:
    if width < 1 or height < 1:
        raise ImageInvalid()
    if _exceeds_decode_budget(width, height, pipeline):
        raise ImageDimensions()


def _exceeds_decode_budget(width: int, height: int, pipeline: ImagePipeline) -> bool:
    return (
        width > pipeline.max_decode_edge
        or height > pipeline.max_decode_edge
        or width * height > pipeline.max_decode_pixels
    )


def _draft_box(width: int, height: int, pipeline: ImagePipeline) -> tuple[int, int]:
    scale = 1.0
    longest = max(width, height)
    if longest > pipeline.max_decode_edge:
        scale = min(scale, pipeline.max_decode_edge / longest)
    pixels = width * height
    if pixels > pipeline.max_decode_pixels:
        scale = min(scale, math.sqrt(pipeline.max_decode_pixels / pixels))
    return (max(1, int(width * scale)), max(1, int(height * scale)))


def _reject_animation(image: Image.Image) -> None:
    animated = getattr(image, "is_animated", False)
    if isinstance(animated, bool) and animated:
        raise ImageAnimated()


def _display_image(image: Image.Image) -> Image.Image:
    if image.has_transparency_data:
        if image.mode == "RGBA":
            return image
        return image.convert("RGBA")
    if image.mode == "RGB":
        return image
    return image.convert("RGB")


def _encode_within_budget(
    image: Image.Image,
    source_format: str,
    pipeline: ImagePipeline,
) -> tuple[bytes, ImageMediaType, ImageExtension, int, int]:
    try:
        data, media_type, extension = _encode(image, source_format, pipeline)
    except OSError, ValueError:
        measured: int | None = None
    else:
        if len(data) <= pipeline.max_stored_bytes:
            return data, media_type, extension, image.width, image.height
        measured = len(data)
    fitted, width, height = _shrink_to_budget(image, measured, pipeline)
    return fitted, "image/avif", "avif", width, height


def _shrink_to_budget(
    image: Image.Image,
    measured_bytes: int | None,
    pipeline: ImagePipeline,
) -> tuple[bytes, int, int]:
    limit = pipeline.max_stored_bytes
    long_edge = max(image.width, image.height)
    low = 1
    high = long_edge if measured_bytes is None else long_edge - 1
    if high < 1:
        raise ImageInvalid()
    probe = _probe_edge(long_edge, measured_bytes, limit, low, high)
    best_data: bytes | None = None
    best_width = 0
    best_height = 0
    for _ in range(_SHRINK_STEPS):
        if low > high:
            break
        probe = min(max(probe, low), high)
        encoded = _encode_edge(image, probe, pipeline)
        if encoded is None:
            high = min(high, max(1, probe // 2)) - 1
            probe = (low + high) // 2 if high >= low else low
            continue
        data, width, height = encoded
        actual_edge = max(width, height)
        if len(data) <= limit:
            best_data = data
            best_width = width
            best_height = height
            low = actual_edge + 1
            if low > high:
                break
            probe = (low + high) // 2
            continue
        high = min(high, actual_edge - 1)
        if high < low:
            break
        probe = _probe_edge(actual_edge, len(data), limit, low, high)
    if best_data is None:
        raise ImageInvalid()
    return best_data, best_width, best_height


def _probe_edge(
    edge: int,
    measured_bytes: int | None,
    limit: int,
    low: int,
    high: int,
) -> int:
    if measured_bytes is None or measured_bytes <= limit:
        return max(low, min(high, (low + high) // 2))
    estimate = int(edge * math.sqrt(limit / measured_bytes) * 0.95)
    return max(low, min(high, max(1, estimate)))


def _encode_edge(
    image: Image.Image,
    edge: int,
    pipeline: ImagePipeline,
) -> tuple[bytes, int, int] | None:
    resized = image.copy()
    try:
        if max(resized.size) > edge:
            resized.thumbnail(
                (edge, edge),
                Image.Resampling.LANCZOS,
                reducing_gap=2.0,
            )
        if resized.width < 1 or resized.height < 1:
            return None
        resized.info.clear()
        return _encode_avif(resized, pipeline), resized.width, resized.height
    except OSError, ValueError:
        return None
    finally:
        resized.close()


def _encode(
    image: Image.Image,
    source_format: str,
    pipeline: ImagePipeline,
) -> tuple[bytes, ImageMediaType, ImageExtension]:
    if _prefer_lossless(image, source_format):
        lossless = _encode_lossless_webp(image, pipeline)
        if len(lossless) <= min(
            pipeline.graphic_lossless_max_bytes, pipeline.max_stored_bytes
        ):
            return lossless, "image/webp", "webp"
        avif = _encode_avif(image, pipeline)
        if len(lossless) <= len(avif) and len(lossless) <= pipeline.max_stored_bytes:
            return lossless, "image/webp", "webp"
        if len(avif) <= pipeline.max_stored_bytes or len(avif) <= len(lossless):
            return avif, "image/avif", "avif"
        return lossless, "image/webp", "webp"
    return _encode_avif(image, pipeline), "image/avif", "avif"


def _prefer_lossless(image: Image.Image, source_format: str) -> bool:
    if image.mode == "RGBA":
        return True
    return source_format in {"PNG", "GIF"}


def _encode_lossless_webp(image: Image.Image, pipeline: ImagePipeline) -> bytes:
    buffer = BytesIO()
    image.save(
        buffer,
        format="WEBP",
        lossless=True,
        quality=pipeline.lossless_webp_effort,
        method=pipeline.lossless_webp_method,
        exact=False,
    )
    return _nonempty(buffer.getvalue())


def _encode_avif(image: Image.Image, pipeline: ImagePipeline) -> bytes:
    buffer = BytesIO()
    image.save(
        buffer,
        format="AVIF",
        quality=pipeline.avif_quality,
        speed=pipeline.avif_speed,
        max_threads=pipeline.avif_threads,
    )
    return _nonempty(buffer.getvalue())


def _nonempty(payload: bytes) -> bytes:
    if not payload:
        raise ImageInvalid()
    return payload

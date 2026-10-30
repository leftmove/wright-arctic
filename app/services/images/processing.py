"""Normalize still uploads to sRGB JPEG XL; derive web variants from that source."""

import logging
import math
import warnings
from dataclasses import dataclass, replace
from io import BytesIO

import imagecodecs
import numpy as np
from PIL import (
    Image,
    ImageCms,
    ImageFile,
    ImageOps,
    PngImagePlugin,
    UnidentifiedImageError,
)

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
from app.services.images.models import (
    DeliveryFormat,
    ImageExtension,
    ImageMediaType,
    ImageObject,
    ProcessedImage,
)

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
    avif_quality: int = 80
    avif_speed: int = 6
    avif_threads: int = 1
    jxl_distance: float = 1.0
    jxl_effort: int = 5
    lossless: bool = False
    max_output_edge: int | None = None
    output_format: DeliveryFormat = "jxl"


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
    except (
        OSError,
        SyntaxError,
        ValueError,
        EOFError,
        RuntimeError,
        ImageCms.PyCMSError,
    ):
        raise ImageInvalid() from None


def delivery_variant(
    payload: bytes, source: ImageObject, format: DeliveryFormat
) -> ProcessedImage:
    """Decode only Arctic-generated, bounded sRGB JPEG XL, never arbitrary uploads.

    The reference decoder writes into a bounded caller-owned buffer. Canonical
    dimensions must agree with storage metadata before a derivative is stored.
    JPEG XL uploads are intentionally excluded until a header/animation/color
    aware upload decoder is available; the encoder is not an input validator.
    """
    pipeline = ImagePipeline(output_format=format)
    width, height = source.width, source.height
    if (
        source.extension != "jxl"
        or width is None
        or height is None
        or source.channels not in (3, 4)
    ):
        raise ImageInvalid()
    _reject_loaded_dimensions(width, height, pipeline)
    if len(payload) > MAX_STORED_BYTES:
        raise ImageTooLarge()
    try:
        pixels = imagecodecs.jpegxl_decode(
            payload,
            index=0,
            numthreads=1,
            out=np.empty((height, width, source.channels), dtype=np.uint8),
        )
        if pixels.dtype != np.uint8 or pixels.shape not in (
            (height, width, 3),
            (height, width, 4),
        ):
            raise ImageInvalid()
        with Image.fromarray(pixels) as image:
            data, media_type, extension, width, height = _encode_within_budget(
                image, "JXL", pipeline
            )
        return ProcessedImage(
            data, width, height, media_type, extension, pixels.shape[2]
        )
    except OSError, ValueError, RuntimeError:
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
                profile = working.info.get("icc_profile")
                if isinstance(profile, bytes):
                    # ICC transforms operate on color samples, not palette
                    # indexes. Expand indexed PNG/GIF pixels (including alpha)
                    # before applying their RGB profile.
                    if working.mode == "P":
                        expanded = _display_image(working)
                        working.close()
                        working = expanded
                    converted = ImageCms.profileToProfile(
                        working,
                        ImageCms.ImageCmsProfile(BytesIO(profile)),
                        ImageCms.createProfile("sRGB"),
                        outputMode="RGBA" if working.has_transparency_data else "RGB",
                    )
                    if converted is None:
                        raise ImageInvalid()
                    if working is not opened:
                        working.close()
                    working = converted
                normalized = _display_image(working)
                if normalized is not working:
                    working.close()
                    working = normalized
                working.info.clear()
                if pipeline.max_output_edge is not None:
                    working.thumbnail(
                        (pipeline.max_output_edge, pipeline.max_output_edge),
                        Image.Resampling.LANCZOS,
                        reducing_gap=2.0,
                    )
                data, media_type, extension, width, height = _encode_within_budget(
                    working, source_format, pipeline
                )
                return ProcessedImage(
                    data=data,
                    width=width,
                    height=height,
                    media_type=media_type,
                    extension=extension,
                    channels=4 if working.mode == "RGBA" else 3,
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
    if pipeline.output_format == "jxl" and source_format in {"PNG", "GIF"}:
        pipeline = replace(pipeline, lossless=True)
    try:
        data, media_type, extension = _encode(image, source_format, pipeline)
    except OSError, ValueError:
        measured: int | None = None
    else:
        if len(data) <= pipeline.max_stored_bytes:
            return data, media_type, extension, image.width, image.height
        measured = len(data)
    fitted, width, height = _shrink_to_budget(image, measured, pipeline)
    media_type, extension = _format(pipeline.output_format)
    return fitted, media_type, extension, width, height


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
        data, _, _ = _encode(resized, "", pipeline)
        return data, resized.width, resized.height
    except OSError, ValueError:
        return None
    finally:
        resized.close()


def _encode(
    image: Image.Image,
    source_format: str,
    pipeline: ImagePipeline,
) -> tuple[bytes, ImageMediaType, ImageExtension]:
    if pipeline.output_format == "webp":
        return _encode_portrait_webp(image), "image/webp", "webp"
    if pipeline.output_format == "avif":
        return _encode_avif(image, pipeline), "image/avif", "avif"
    lossless = (
        pipeline.lossless or image.mode == "RGBA" or source_format in {"PNG", "GIF"}
    )
    data = imagecodecs.jpegxl_encode(
        np.asarray(image),
        lossless=lossless,
        distance=pipeline.jxl_distance,
        effort=pipeline.jxl_effort,
        numthreads=1,
    )
    return _nonempty(bytes(data)), "image/jxl", "jxl"


def _format(format: DeliveryFormat) -> tuple[ImageMediaType, ImageExtension]:
    if format == "webp":
        return "image/webp", "webp"
    if format == "avif":
        return "image/avif", "avif"
    return "image/jxl", "jxl"


def _encode_portrait_webp(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="WEBP", quality=85, method=6)
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

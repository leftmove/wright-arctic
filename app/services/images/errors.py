"""Failures the image API reports to clients."""

from app.services.images.limits import MAX_STORED_BYTES, MAX_UPLOAD_BYTES


class ImageServiceError(Exception):
    """A request failed for a reason the client can act on."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class ImageTooLarge(ImageServiceError):
    def __init__(self) -> None:
        super().__init__(
            413,
            "image_too_large",
            f"Image uploads are limited to {_mebibytes(MAX_UPLOAD_BYTES)} MiB.",
        )


class ImageEmpty(ImageServiceError):
    def __init__(self) -> None:
        super().__init__(400, "image_empty", "The upload is empty.")


class ImageUnsupported(ImageServiceError):
    def __init__(self) -> None:
        super().__init__(
            415,
            "image_unsupported",
            "Upload a JPEG, PNG, WebP, AVIF, or static GIF.",
        )


class ImageInvalid(ImageServiceError):
    def __init__(self) -> None:
        super().__init__(400, "image_invalid", "The file is not a readable image.")


class ImageAnimated(ImageServiceError):
    def __init__(self) -> None:
        super().__init__(415, "image_animated", "Animated images are not supported.")


class ImageDimensions(ImageServiceError):
    def __init__(self) -> None:
        super().__init__(
            400,
            "image_dimensions",
            "The image could not be decoded.",
        )


class StoredImageTooLarge(ImageServiceError):
    def __init__(self) -> None:
        super().__init__(
            500,
            "stored_image_too_large",
            f"The compressed image exceeded {_mebibytes(MAX_STORED_BYTES)} MiB.",
        )


class ImageQuotaExceeded(ImageServiceError):
    def __init__(self, limit: int) -> None:
        super().__init__(
            409,
            "image_quota_exceeded",
            f"This account is limited to {limit} images.",
        )


class ImageNotFound(ImageServiceError):
    def __init__(self) -> None:
        super().__init__(404, "image_not_found", "Image not found.")


class StorageUnconfigured(ImageServiceError):
    def __init__(self) -> None:
        super().__init__(
            503,
            "storage_unconfigured",
            "Image storage is not configured.",
        )


class StorageUnavailable(ImageServiceError):
    def __init__(self) -> None:
        super().__init__(502, "storage_unavailable", "Image storage is unavailable.")


class StorageInvalid(ImageServiceError):
    def __init__(self) -> None:
        super().__init__(
            500,
            "storage_invalid",
            "Stored image metadata is incomplete.",
        )


def _mebibytes(value: int) -> int:
    return value // (1024 * 1024)

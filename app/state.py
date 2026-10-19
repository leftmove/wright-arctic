"""Process-wide objects created with the application."""

from dataclasses import dataclass

from app.services.images.service import ImageService


@dataclass(frozen=True, slots=True)
class AppResources:
    """Services that outlive a single request."""

    image_service: ImageService | None

    def close(self) -> None:
        """Release clients held for the life of the process."""
        if self.image_service is not None:
            self.image_service.close()

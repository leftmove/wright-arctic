"""Process-wide objects created with the application."""

from dataclasses import dataclass

from app.authentication.service import AuthenticationService
from app.services.documents.service import DocumentService
from app.services.images.service import ImageService


@dataclass(frozen=True, slots=True)
class AppResources:
    """Services that outlive a single request."""

    authentication: AuthenticationService | None
    image_service: ImageService | None
    document_service: DocumentService | None = None

    async def close(self) -> None:
        """Release clients held for the life of the process."""
        try:
            if self.authentication is not None:
                await self.authentication.close()
        finally:
            try:
                if self.image_service is not None:
                    self.image_service.close()
            finally:
                if self.document_service is not None:
                    self.document_service.close()

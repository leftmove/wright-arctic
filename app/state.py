"""Services owned by an ASGI process or by one stateless request."""

from dataclasses import dataclass, field

from starlette.types import Scope

from app.authentication.service import AuthenticationService
from app.execution import Executor, ThreadExecutor
from app.services.documents.service import DocumentService
from app.services.images.service import ImageService
from app.settings import Settings


@dataclass(frozen=True, slots=True)
class AppResources:
    """Explicit service lifetime, supplied by the hosting adapter."""

    authentication: AuthenticationService | None
    image_service: ImageService | None
    document_service: DocumentService | None = None
    settings: Settings | None = None
    executor: Executor = field(default_factory=ThreadExecutor)

    async def close(self) -> None:
        """Release every client owned by this resource scope."""
        try:
            if self.authentication is not None:
                await self.authentication.close()
        finally:
            try:
                if self.image_service is not None:
                    await self.executor.run(self.image_service.close)
            finally:
                if self.document_service is not None:
                    await self.executor.run(self.document_service.close)


def scope_resources(scope: Scope) -> AppResources | None:
    """Return resources explicitly owned by this request, if the host supplied them."""
    state = scope.get("state", {})
    value = state.get("resources")
    return value if isinstance(value, AppResources) else None

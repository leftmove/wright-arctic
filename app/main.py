"""FastAPI application factory and configured entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.authentication import build_authentication_service
from app.authentication.http import (
    AuthenticationMiddleware,
    install_authentication_error_handler,
)
from app.dependencies import get_settings
from app.middleware import ImageUploadLimitMiddleware
from app.routers import health, hello, images, root
from app.routers.images import install_image_error_handler
from app.services.images import build_image_service
from app.services.images.limits import MAX_UPLOAD_BODY_BYTES
from app.state import AppResources


def create_app() -> FastAPI:
    """Construct the API with validated settings and modular routers."""
    settings = get_settings()
    resources = AppResources(
        authentication=build_authentication_service(settings),
        image_service=build_image_service(settings),
    )

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await resources.close()

    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        debug=settings.debug,
        lifespan=lifespan,
    )
    application.state.resources = resources
    install_authentication_error_handler(application)
    install_image_error_handler(application)
    application.include_router(root.router)
    application.include_router(health.router)
    application.include_router(hello.router)
    application.include_router(images.router)
    application.add_middleware(
        ImageUploadLimitMiddleware,
        max_body_bytes=MAX_UPLOAD_BODY_BYTES,
    )
    # Starlette runs the last registered middleware first. Authenticate before
    # the upload limiter reads a body or the multipart parser creates files.
    application.add_middleware(
        AuthenticationMiddleware,
        service=resources.authentication,
        protected_prefixes=(images.router.prefix,),
    )
    return application


app = create_app()

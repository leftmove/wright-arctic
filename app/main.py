"""FastAPI application factory and configured entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache

from fastapi import FastAPI

from app.authentication import build_authentication_service
from app.authentication.http import (
    AuthenticationMiddleware,
    install_authentication_error_handler,
)
from app.dependencies import get_settings
from app.execution import Executor, ThreadExecutor
from app.middleware import DocumentBodyLimitMiddleware, ImageUploadLimitMiddleware
from app.routers import account, documents, health, hello, images, root
from app.routers.documents import install_document_error_handler
from app.routers.images import install_image_error_handler
from app.services.documents.database import Database, PostgresDatabase
from app.services.documents.service import DocumentService
from app.services.images import build_image_service
from app.services.images.limits import MAX_UPLOAD_BODY_BYTES
from app.settings import Settings
from app.state import AppResources


def create_app(
    settings: Settings | None = None,
    *,
    executor: Executor | None = None,
    database: Database | None = None,
) -> FastAPI:
    """Construct the API with validated settings and modular routers."""
    settings = settings if settings is not None else get_settings()
    executor = executor if executor is not None else ThreadExecutor()
    if database is None and settings.documents_database_url is not None:
        database = PostgresDatabase(
            settings.documents_database_url.get_secret_value(),
            max_connections=settings.documents_pool_size,
        )
    resources = AppResources(
        authentication=build_authentication_service(settings),
        image_service=build_image_service(settings),
        document_service=None if database is None else DocumentService(database),
        settings=settings,
        executor=executor,
    )

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        try:
            if resources.document_service is not None:
                await executor.run(resources.document_service.open)
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
    install_document_error_handler(application)
    application.include_router(root.router)
    application.include_router(health.router)
    application.include_router(hello.router)
    application.include_router(images.router)
    application.include_router(account.router)
    application.include_router(documents.router)
    application.add_middleware(
        DocumentBodyLimitMiddleware,
    )
    application.add_middleware(
        ImageUploadLimitMiddleware,
        max_body_bytes=MAX_UPLOAD_BODY_BYTES,
    )
    # Starlette runs the last registered middleware first. Authenticate before
    # the upload limiter reads a body or the multipart parser creates files.
    application.add_middleware(
        AuthenticationMiddleware,
        service=resources.authentication,
        protected_prefixes=(
            images.router.prefix,
            account.router.prefix,
            documents.router.prefix,
        ),
    )
    return application


@cache
def _default_application() -> FastAPI:
    return create_app()


def __getattr__(name: str) -> FastAPI:
    """Construct the default ASGI app only when that entrypoint is requested.

    Hosts that use the factory supply their own resources and must not also
    construct an unused default application during import.
    """
    if name == "app":
        return _default_application()
    raise AttributeError(name)

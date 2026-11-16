"""Application factory. Hosting adapters own clients; routers own no persistence."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache

from fastapi import FastAPI
from starlette.middleware import Middleware

from app.authentication.http import (
    AuthenticationMiddleware,
    install_authentication_error_handler,
)
from app.dependencies import get_settings
from app.middleware import ControlBodyLimitMiddleware, OperationMiddleware
from app.request_paths import PROTECTED_PREFIXES
from app.routers import account, documents, health, hello, images, root, spaces, sync
from app.services.errors import install_service_error_handler
from app.settings import Settings
from app.state import AppResources


def create_app(resources: AppResources, settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else resources.settings

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        try:
            await resources.open()
            yield
        finally:
            await resources.close()

    application = FastAPI(
        title=settings.app_name,
        version="0.2.0",
        debug=settings.debug,
        lifespan=lifespan,
        middleware=[
            Middleware(
                OperationMiddleware,
                deadline=settings.application_deadline_seconds,
                limit=settings.admission_limit,
            ),
            Middleware(
                AuthenticationMiddleware,
                service=resources.authentication,
                protected_prefixes=PROTECTED_PREFIXES,
            ),
            Middleware(ControlBodyLimitMiddleware),
        ],
    )
    application.state.resources = resources
    install_authentication_error_handler(application)
    install_service_error_handler(application)
    for router in (
        root.router,
        health.router,
        hello.router,
        account.router,
        spaces.router,
        documents.router,
        sync.router,
        images.router,
        images.avatars,
    ):
        application.include_router(router)
    return application


@cache
def _default_application() -> FastAPI:
    from app.hosting.resources import build_resources

    settings = get_settings()
    return create_app(build_resources(settings), settings)


def __getattr__(name: str) -> FastAPI:
    if name == "app":
        return _default_application()
    raise AttributeError(name)

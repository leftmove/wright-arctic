"""Application factory. Hosting adapters own clients; routers own no persistence."""

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
from app.middleware import ControlBodyLimitMiddleware
from app.request_paths import PROTECTED_PREFIXES
from app.routers import account, documents, health, hello, images, root, spaces, sync
from app.services.database import Database, PostgresDatabase
from app.services.errors import install_service_error_handler
from app.services.storage.factory import Storage, build_storage
from app.settings import Settings
from app.state import build_resources


def create_app(
    settings: Settings | None = None,
    *,
    executor: Executor | None = None,
    database: Database | None = None,
    storage: Storage | None = None,
) -> FastAPI:
    settings = settings if settings is not None else get_settings()
    executor = executor if executor is not None else ThreadExecutor()
    if database is None and settings.documents_database_url is not None:
        database = PostgresDatabase(
            settings.documents_database_url.get_secret_value(),
            max_connections=settings.documents_pool_size,
        )
    storage = storage if storage is not None else build_storage(settings)
    resources = build_resources(
        settings,
        executor,
        database=database,
        storage=storage,
        authentication=build_authentication_service(settings),
    )

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        try:
            if resources.database is not None:
                await executor.run(resources.database.open)
            yield
        finally:
            await resources.close()

    application = FastAPI(
        title=settings.app_name,
        version="0.2.0",
        debug=settings.debug,
        lifespan=lifespan,
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
    application.add_middleware(ControlBodyLimitMiddleware)
    application.add_middleware(
        AuthenticationMiddleware,
        service=resources.authentication,
        protected_prefixes=PROTECTED_PREFIXES,
    )
    return application


@cache
def _default_application() -> FastAPI:
    return create_app()


def __getattr__(name: str) -> FastAPI:
    if name == "app":
        return _default_application()
    raise AttributeError(name)

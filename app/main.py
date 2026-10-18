"""FastAPI application factory and configured entrypoint."""

from fastapi import FastAPI

from app.dependencies import get_settings
from app.routers import health, hello, root


def create_app() -> FastAPI:
    """Construct the API with validated settings and modular routers."""
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        debug=settings.debug,
    )
    application.include_router(root.router)
    application.include_router(health.router)
    application.include_router(hello.router)
    return application


app = create_app()

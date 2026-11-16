"""Build the clients an ASGI process owns for its whole lifetime."""

from app.authentication import build_authentication_service
from app.services.native_database import NativeDatabase
from app.services.storage.native import build_native_storage
from app.settings import Settings
from app.state import AppResources


def build_resources(settings: Settings) -> AppResources:
    database = (
        NativeDatabase(
            settings.documents_database_url.get_secret_value(),
            max_connections=settings.documents_pool_size,
        )
        if settings.documents_database_url is not None
        else None
    )
    return AppResources(
        settings=settings,
        authentication=build_authentication_service(settings),
        database=database,
        storage=build_native_storage(settings),
    )

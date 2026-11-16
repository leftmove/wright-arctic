"""Build the clients an ASGI process owns for its whole lifetime."""

from app.authentication import build_authentication_service
from app.services.catalog.copy import CatalogCopy
from app.services.native_database import NativeDatabase
from app.services.storage.native import build_native_storage
from app.settings import Settings
from app.state import AppResources


def build_resources(settings: Settings) -> AppResources:
    catalog = CatalogCopy()
    database = (
        NativeDatabase(
            settings.documents_database_url.get_secret_value(),
            max_connections=settings.documents_pool_size,
            catalog=catalog,
            listener_url=(
                settings.documents_listener_url.get_secret_value()
                if settings.documents_listener_url is not None
                else None
            ),
        )
        if settings.documents_database_url is not None
        else None
    )
    return AppResources(
        settings=settings,
        authentication=build_authentication_service(settings),
        database=database,
        catalog=catalog,
        database_ready=(lambda: database.ready) if database is not None else None,
        storage=build_native_storage(settings),
    )

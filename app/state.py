"""Explicit service ownership for process and Worker request lifetimes."""

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from functools import cached_property

from starlette.types import Scope

from app.authentication.errors import AuthenticationUnavailable
from app.authentication.service import AuthenticationService
from app.execution import Executor
from app.services.catalog.copy import CatalogCopy
from app.services.catalog.reader import CatalogReader
from app.services.cleanup import CleanupService
from app.services.database import AsyncDatabase, Database
from app.services.documents.service import DocumentService
from app.services.errors import ServiceError, Unavailable
from app.services.identity.service import IdentityService
from app.services.images.service import ImageService
from app.services.storage.factory import Storage
from app.services.storage.native import AsyncStorage
from app.services.storage.objects import ObjectMissing
from app.services.sync.service import SyncService
from app.settings import Settings


@dataclass
class _Startup:
    ready: bool = False


@dataclass(frozen=True)
class AppResources:
    settings: Settings
    authentication: AuthenticationService | None = None
    database: AsyncDatabase | None = None
    storage: AsyncStorage | None = None
    probes_dependencies: bool = False
    catalog: CatalogCopy | None = None
    database_ready: Callable[[], bool] | None = field(
        default=None, repr=False, compare=False
    )
    _startup: _Startup = field(default_factory=_Startup, repr=False, compare=False)

    @cached_property
    def catalog_reader(self) -> CatalogReader | None:
        return (
            CatalogReader(self.database, self.catalog)
            if self.database is not None
            else None
        )

    @cached_property
    def identity(self) -> IdentityService | None:
        return (
            IdentityService(self.database, reader=self.catalog_reader)
            if self.database is not None
            else None
        )

    @cached_property
    def documents(self) -> DocumentService | None:
        return (
            DocumentService(
                self.database,
                trash_days=self.settings.trash_retention_days,
                reader=self.catalog_reader,
            )
            if self.database is not None
            else None
        )

    @cached_property
    def sync(self) -> SyncService | None:
        if self.database is None or self.storage is None:
            return None
        return SyncService(
            self.database,
            self.storage.checkpoints,
            self.storage.checkpoint_signer,
            presign_seconds=self.settings.storage_presign_seconds,
            reader=self.catalog_reader,
        )

    @cached_property
    def images(self) -> ImageService | None:
        if self.database is None or self.storage is None:
            return None
        return ImageService(
            self.database,
            self.storage.images,
            self.storage.image_signer,
            presign_seconds=self.settings.storage_presign_seconds,
            reader=self.catalog_reader,
            daily_quota=self.settings.image_daily_quota,
            total_quota=self.settings.image_total_quota,
        )

    @cached_property
    def cleanup(self) -> CleanupService | None:
        if self.database is None or self.storage is None:
            return None
        return CleanupService(
            self.database, self.storage, trash_days=self.settings.trash_retention_days
        )

    async def open(self) -> None:
        if self.database is not None:
            await self.database.open()
        self._startup.ready = True

    async def confirm_ready(self) -> None:
        if self.probes_dependencies:
            await self._probe_dependencies()
            return
        if (
            not self._startup.ready
            or (self.database_ready is not None and not self.database_ready())
            or self.database is None
            or self.storage is None
            or self.authentication is None
        ):
            raise Unavailable()

    async def _probe_dependencies(self) -> None:
        if self.settings.auth_issuer is None:
            raise AuthenticationUnavailable()
        if self.database is None:
            raise Unavailable()
        await self.database.open()
        if self.storage is None:
            raise ServiceError(
                "storage_unconfigured", "Object storage is not configured.", 503
            )
        for objects, key in (
            (self.storage.checkpoints, "documents/.readiness"),
            (self.storage.images, "images/.readiness"),
        ):
            with suppress(ObjectMissing):
                await objects.head(key)

    async def close(self) -> None:
        self._startup.ready = False
        try:
            if self.authentication is not None:
                await self.authentication.close()
        finally:
            try:
                if self.storage is not None:
                    await self.storage.close()
            finally:
                if self.database is not None:
                    await self.database.close()


def legacy_resources(
    settings: Settings,
    executor: Executor,
    *,
    database: Database | None,
    storage: Storage | None,
    authentication: AuthenticationService | None,
) -> AppResources:
    adapted_database: AsyncDatabase | None = None
    adapted_storage: AsyncStorage | None = None
    if database is not None or storage is not None:
        from app.services.compatibility import (
            LegacyCheckpoints,
            LegacyDatabase,
            LegacyImages,
        )

        if database is not None:
            adapted_database = LegacyDatabase(database, executor)
        if storage is not None:

            async def close_storage(bound: Storage = storage) -> None:
                await executor.run(bound.close)

            adapted_storage = AsyncStorage(
                LegacyCheckpoints(storage.checkpoints, executor),
                storage.checkpoint_signer,
                LegacyImages(storage.images, executor),
                storage.image_signer,
                close_transport=close_storage,
            )
    return AppResources(
        settings=settings,
        authentication=authentication,
        database=adapted_database,
        storage=adapted_storage,
        probes_dependencies=True,
    )


def scope_resources(scope: Scope) -> AppResources | None:
    state = scope.get("state", {})
    value = state.get("resources")
    return value if isinstance(value, AppResources) else None

"""Explicit service ownership for process and Worker request lifetimes."""

from dataclasses import dataclass
from functools import cached_property

from starlette.types import Scope

from app.authentication.service import AuthenticationService
from app.execution import Executor
from app.services.cleanup import CleanupService
from app.services.database import Database
from app.services.documents.service import DocumentService
from app.services.identity.service import IdentityService
from app.services.images.service import ImageService
from app.services.storage.factory import Storage
from app.services.sync.service import SyncService
from app.settings import Settings


@dataclass(frozen=True)
class AppResources:
    settings: Settings
    executor: Executor
    authentication: AuthenticationService | None = None
    database: Database | None = None
    storage: Storage | None = None

    @cached_property
    def identity(self) -> IdentityService | None:
        return IdentityService(self.database) if self.database is not None else None

    @cached_property
    def documents(self) -> DocumentService | None:
        return (
            DocumentService(
                self.database, trash_days=self.settings.trash_retention_days
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

    async def close(self) -> None:
        try:
            if self.authentication is not None:
                await self.authentication.close()
        finally:
            try:
                if self.storage is not None:
                    await self.executor.run(self.storage.close)
            finally:
                if self.database is not None:
                    await self.executor.run(self.database.close)


def build_resources(
    settings: Settings,
    executor: Executor,
    *,
    database: Database | None,
    storage: Storage | None,
    authentication: AuthenticationService | None,
) -> AppResources:
    return AppResources(
        settings=settings,
        executor=executor,
        authentication=authentication,
        database=database,
        storage=storage,
    )


def scope_resources(scope: Scope) -> AppResources | None:
    state = scope.get("state", {})
    value = state.get("resources")
    return value if isinstance(value, AppResources) else None

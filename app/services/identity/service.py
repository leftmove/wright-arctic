"""Map provider identities to UUIDv7 accounts without depending on provider IDs."""

from uuid import UUID

from app.accounts import Identity, VerifiedActor
from app.schemas import AccountResponse
from app.services.catalog.reader import CatalogReader
from app.services.database import AsyncDatabase
from app.services.identity.actor import Actor
from app.services.models import Account
from app.services.operations import operation


def account_response(account: Account) -> AccountResponse:
    return AccountResponse(
        id=account.id,
        avatar_id=account.avatar_id,
        last_sync=account.last_sync,
        created_at=account.created_at,
    )


class IdentityService:
    def __init__(
        self, database: AsyncDatabase, *, reader: CatalogReader | None = None
    ) -> None:
        self._database = database
        self._reader = reader or CatalogReader(database)

    async def resolve(self, identity: Identity) -> Account:
        return await self._reader.account(VerifiedActor(identity))

    async def current(self, account: Actor) -> AccountResponse:
        return account_response(await self._reader.account(account))

    async def select_avatar(
        self, account: Actor, image_id: UUID | None
    ) -> AccountResponse:
        row = await operation(
            self._database, "select_avatar", account, (image_id,), Account
        )
        return account_response(row)

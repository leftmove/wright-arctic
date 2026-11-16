"""Map provider identities to UUIDv7 accounts without depending on provider IDs."""

from uuid import UUID

from app.accounts import Identity
from app.schemas import AccountResponse
from app.services.database import AsyncDatabase as Database
from app.services.errors import NotFound
from app.services.identity.actor import Actor, resolve_actor, resolve_identity
from app.services.models import Account


def account_response(account: Account) -> AccountResponse:
    return AccountResponse(
        id=account.id,
        avatar_id=account.avatar_id,
        last_sync=account.last_sync,
        created_at=account.created_at,
    )


class IdentityService:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def resolve(self, identity: Identity) -> Account:
        async with self._database.read() as session:
            return await resolve_identity(session, identity)

    async def current(self, account: Actor) -> AccountResponse:
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            rows = await session.execute(
                "select * from public.accounts where id=%s", (account.id,)
            )
            if not rows:
                raise NotFound()
            return account_response(Account.model_validate(rows[0]))

    async def select_avatar(
        self, account: Actor, image_id: UUID | None
    ) -> AccountResponse:
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            (
                await session.execute(
                    "select id from public.accounts where id=%s for update",
                    (account.id,),
                )
            )
            if image_id is not None and not (
                await session.execute(
                    "select id from public.images where id=%s and account=%s and deleted_at is null for share",
                    (image_id, account.id),
                )
            ):
                raise NotFound()
            rows = await session.execute(
                "update public.accounts set avatar_id=%s where id=%s returning *",
                (image_id, account.id),
            )
            return account_response(Account.model_validate(rows[0]))

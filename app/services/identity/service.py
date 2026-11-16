"""Map provider identities to UUIDv7 accounts without depending on provider IDs."""

from uuid import UUID, uuid7

from app.accounts import Identity
from app.schemas import AccountResponse
from app.services.database import Database
from app.services.errors import NotFound
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

    def resolve(self, identity: Identity) -> Account:
        with self._database.transaction() as session:
            rows = session.execute(
                "select * from public.accounts where issuer=%s and subject=%s",
                (identity.issuer, identity.subject),
            )
            if rows:
                return Account.model_validate(rows[0])
            rows = session.execute(
                """insert into public.accounts(id,issuer,subject) values(%s,%s,%s)
                    on conflict(issuer,subject) do nothing returning *""",
                (uuid7(), identity.issuer, identity.subject),
            )
            if rows:
                account = Account.model_validate(rows[0])
                session.execute(
                    "insert into documents.spaces(id,owner) values(%s,%s)",
                    (uuid7(), account.id),
                )
                return account
            return Account.model_validate(
                session.execute(
                    "select * from public.accounts where issuer=%s and subject=%s",
                    (identity.issuer, identity.subject),
                )[0]
            )

    def current(self, account: Account) -> AccountResponse:
        with self._database.transaction() as session:
            rows = session.execute(
                "select * from public.accounts where id=%s", (account.id,)
            )
            if not rows:
                raise NotFound()
            return account_response(Account.model_validate(rows[0]))

    def select_avatar(self, account: Account, image_id: UUID | None) -> AccountResponse:
        with self._database.transaction() as session:
            session.execute(
                "select id from public.accounts where id=%s for update", (account.id,)
            )
            if image_id is not None and not session.execute(
                "select id from public.images where id=%s and account=%s and deleted_at is null for share",
                (image_id, account.id),
            ):
                raise NotFound()
            rows = session.execute(
                "update public.accounts set avatar_id=%s where id=%s returning *",
                (image_id, account.id),
            )
            return account_response(Account.model_validate(rows[0]))

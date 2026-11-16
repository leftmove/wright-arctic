"""Resolve verified identities within an operation, never a dependency transaction."""

from uuid import uuid7

from app.accounts import Identity, VerifiedActor
from app.observability import span
from app.services.database import AsyncSession
from app.services.models import Account

type Actor = Account | VerifiedActor


async def resolve_identity(session: AsyncSession, identity: Identity) -> Account:
    with span("identity"):
        rows = await session.execute(
            "select * from coordination.resolve_account(%s,%s,%s,%s)",
            (identity.issuer, identity.subject, uuid7(), uuid7()),
        )
        return Account.model_validate(rows[0])


async def resolve_actor(session: AsyncSession, actor: Actor) -> Account:
    return (
        actor
        if isinstance(actor, Account)
        else await resolve_identity(session, actor.identity)
    )

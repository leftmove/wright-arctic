"""Bind verified identities to one catalog statement."""

from collections.abc import Sequence
from uuid import uuid7

from app.accounts import VerifiedActor
from app.services.models import Account

type Actor = Account | VerifiedActor


def actor_query(
    actor: Actor, query: str, parameters: Sequence[object] = ()
) -> tuple[str, tuple[object, ...]]:
    if isinstance(actor, Account):
        return f"with actor as (select %s::uuid as id) {query}", (actor.id, *parameters)
    identity = actor.identity
    return (
        "with actor as materialized (select * from coordination.resolve_account(%s,%s,%s,%s)) "
        + query,
        (identity.issuer, identity.subject, uuid7(), uuid7(), *parameters),
    )

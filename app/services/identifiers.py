"""RFC 9562 identifiers and the only permitted object key layouts."""

from uuid import UUID, uuid7


def new_revision(after: UUID | None = None) -> UUID:
    """Keep pointer revisions increasing even across hosts with skewed clocks.

    UUIDv7 retains 74 random bits. Advancing the millisecond prefix avoids a late
    issued revision sorting behind the pointer it is intended to replace.
    """
    value = uuid7()
    if after is not None and value <= after:
        milliseconds = (after.int >> 80) + 1
        value = UUID(int=(milliseconds << 80) | (value.int & ((1 << 80) - 1)))
    return value


def checkpoint_key(space: UUID, document: UUID, revision: UUID) -> str:
    return f"documents/{space}/{document}/{revision}"


def image_key(account: UUID, image: UUID) -> str:
    return f"images/{account}/{image}"

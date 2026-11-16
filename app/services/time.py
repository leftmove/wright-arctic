"""Use the database clock for shared windows and retention decisions."""

from datetime import datetime

from pydantic import AwareDatetime, TypeAdapter

from app.services.database import AsyncSession as Session

_timestamp = TypeAdapter(AwareDatetime)


async def database_time(session: Session) -> datetime:
    return _timestamp.validate_python(
        (await session.execute("select clock_timestamp() as now"))[0]["now"]
    )

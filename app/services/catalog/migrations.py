"""Apply immutable additive migrations only to an existing Phase 1 catalog."""

from pathlib import Path

from app.services.database import Session

DIRECTORY = Path(__file__).with_name("sql")


def migrate(session: Session, *, verify: bool = False) -> None:
    for path in sorted(DIRECTORY.glob("*_*.sql")):
        version, name = path.stem.split("_", 1)
        source = path.read_text()
        rows = session.execute(
            "select statements from supabase_migrations.schema_migrations where version=%s",
            (version,),
        )
        if rows:
            if rows[0]["statements"] != [source]:
                raise RuntimeError(
                    "Catalog migration source differs from the version ledger"
                )
        elif name == "catalog" or verify:
            raise RuntimeError("Required catalog migration has not been applied")
        else:
            session.execute(source)
            session.execute(
                "insert into supabase_migrations.schema_migrations(version,name,statements) values(%s,%s,%s)",
                (version, name, [source]),
            )

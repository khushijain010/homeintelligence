from sqlalchemy import inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from guardian import models  # noqa: F401  (registers tables)
from guardian.config import settings

engine = None

# Tables a demo reset must not wipe: the ledger is append-only, OAuth grants outlive demos.
KEEP_ON_RESET = {"ledgerentry", "oauthcode", "oauthrefreshtoken"}


def init_db(url: str | None = None) -> None:
    """Create the engine and tables. Tests pass "sqlite://" for a private in-memory DB."""
    global engine
    url = url or settings.database_url
    kwargs: dict = {"connect_args": {"check_same_thread": False}}
    if url == "sqlite://":
        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """create_all doesn't alter existing tables; add new nullable columns to older guardian.db files."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in existing:
                    ddl = col.type.compile(dialect=engine.dialect)
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}'))


def session() -> Session:
    if engine is None:
        init_db()
    return Session(engine, expire_on_commit=False)


def clear_all() -> None:
    with session() as s:
        for table in reversed(SQLModel.metadata.sorted_tables):
            if table.name not in KEEP_ON_RESET:
                s.exec(table.delete())  # type: ignore[call-overload]
        s.commit()

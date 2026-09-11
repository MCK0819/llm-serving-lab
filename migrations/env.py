import asyncio
import sys

from alembic import context
from sqlalchemy import Connection

from app.core.database import Base, create_database
from app.core.settings import Settings
from app.users import models  # noqa: F401


def migrate(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def online() -> None:
    url = Settings().database_url
    if url is None:
        raise RuntimeError("LLM_LAB_DATABASE_URL is required for migrations")
    engine = create_database(url.get_secret_value())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url="postgresql+psycopg://", target_metadata=Base.metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(
        online(), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None
    )

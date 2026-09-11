from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def create_database(url: str) -> AsyncEngine:
    # Construction is lazy: no connection is required for liveness.
    return create_async_engine(
        url,
        hide_parameters=True,
        pool_size=5,
        max_overflow=0,
        pool_timeout=5,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 5,
            "application_name": "llm-serving-lab-api",
            "options": "-c statement_timeout=5000",
        },
    )

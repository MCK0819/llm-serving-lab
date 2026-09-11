from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.database import create_database
from app.core.settings import Settings
from app.inference.admission import Admission
from app.inference.client import InferenceClient
from app.users.repository import UserRepository
from app.users.service import AuthService


@dataclass(frozen=True)
class ApplicationServices:
    engine: AsyncEngine | None
    auth: AuthService | None
    admission: Admission


def build_services(settings: Settings) -> ApplicationServices:
    engine = (
        create_database(settings.database_url.get_secret_value()) if settings.database_url else None
    )
    auth = (
        AuthService(UserRepository(async_sessionmaker(engine, expire_on_commit=False)))
        if engine
        else None
    )
    return ApplicationServices(
        engine,
        auth,
        Admission(settings.running_limit, settings.waiting_limit, settings.waiting_timeout),
    )


def application_lifespan(
    settings: Settings, services: ApplicationServices
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            async with inference_lifespan(settings)(app):
                yield
        finally:
            if services.engine is not None:
                await services.engine.dispose()

    return lifespan


def inference_lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(
            base_url=str(settings.inference_url),
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(
                connect=settings.connect_timeout,
                read=settings.read_timeout,
                write=settings.connect_timeout,
                pool=settings.connect_timeout,
            ),
            limits=httpx.Limits(
                max_connections=settings.running_limit,
                max_keepalive_connections=settings.running_limit,
            ),
        ) as http:
            app.state.inference = InferenceClient(
                http, "Qwen/Qwen3-4B-Instruct-2507", settings.output_tokens
            )
            yield

    return lifespan

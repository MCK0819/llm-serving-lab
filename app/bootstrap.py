from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.database import create_database
from app.core.settings import Settings
from app.documents.notifications import JobNotifier
from app.documents.repository import DocumentRepository
from app.documents.service import DocumentService
from app.documents.storage import FileStorage
from app.inference.admission import Admission
from app.inference.client import InferenceClient
from app.users.repository import UserRepository
from app.users.service import AuthService


@dataclass(frozen=True)
class ApplicationServices:
    engine: AsyncEngine | None
    auth: AuthService | None
    admission: Admission
    documents: DocumentService | None


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
        DocumentService(
            DocumentRepository(
                async_sessionmaker(engine, expire_on_commit=False),
                organization_job_limit=settings.organization_job_limit,
                global_job_limit=settings.global_job_limit,
            ),
            FileStorage(
                settings.upload_root,
                maximum_bytes=settings.document_file_bytes,
                minimum_free_bytes=settings.document_minimum_free_bytes,
            ),
            JobNotifier(settings.broker_url.get_secret_value()) if settings.broker_url else None,
        )
        if engine
        else None,
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

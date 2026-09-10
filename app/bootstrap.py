from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
from fastapi import FastAPI

from app.core.settings import Settings
from app.inference.client import InferenceClient


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

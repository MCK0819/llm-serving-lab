import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import FastAPI
from starlette.responses import StreamingResponse


def create_upstream(closed: asyncio.Event) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completion() -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            try:
                payload = {
                    "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}]
                }
                yield f"data: {json.dumps(payload)}\n\n".encode()
                await asyncio.Event().wait()
            finally:
                closed.set()

        return StreamingResponse(body(), media_type="text/event-stream")

    return app

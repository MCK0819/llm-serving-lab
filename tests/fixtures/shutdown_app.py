"""Private routes used only by the owned-container SIGTERM regression."""

import asyncio
import json
import sys
from contextlib import AsyncExitStack

import uvicorn
from fastapi import FastAPI, Request
from starlette.responses import StreamingResponse

from app.core.settings import Settings
from app.inference.admission import Admission
from app.inference.response import PreparedStream, StreamResponse
from app.main import create_app


def upstream_app() -> FastAPI:
    app = FastAPI()
    active = closed = 0
    release = asyncio.Event()

    @app.get("/test/state")
    async def state():
        return {"active": active, "closed": closed}

    @app.post("/test/release")
    async def finish():
        release.set()
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def completion():
        async def body():
            nonlocal active, closed
            active += 1
            try:
                value = {
                    "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}]
                }
                yield f"data: {json.dumps(value)}\n\n"
                await release.wait()
                value = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                yield f"data: {json.dumps(value)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                active -= 1
                closed += 1

        return StreamingResponse(body(), media_type="text/event-stream")

    return app


def api_app() -> FastAPI:
    app = create_app(Settings(inference_url="http://upstream:8000", read_timeout=120))
    gate = Admission(1, 0, 1)

    @app.post("/test/stream")
    async def stream(request: Request):
        async def prepare(stack: AsyncExitStack):
            events = await stack.enter_async_context(request.app.state.inference.open([]))
            return PreparedStream([], events)

        return StreamResponse(prepare, gate, request.state.request_id)

    return app


if __name__ == "__main__":
    if sys.argv[1] == "upstream":
        uvicorn.run(upstream_app(), host="0.0.0.0", port=8000, access_log=False)
    else:
        from app.server import run

        run(api_app())

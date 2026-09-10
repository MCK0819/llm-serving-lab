import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from app.core.errors import AppError
from app.inference.admission import Admission
from app.inference.client import InferenceClient
from app.inference.response import PreparedStream, StreamResponse
from tests.fakes.upstream_app import create_upstream

pytestmark = pytest.mark.integration


async def wait_for_slot(gate: Admission) -> None:
    # Resource cleanup can signal before the response task releases admission.
    async with asyncio.timeout(1):
        while True:
            try:
                async with gate.slot():
                    return
            except AppError:
                await asyncio.sleep(0.01)


@asynccontextmanager
async def serve(app: FastAPI) -> AsyncIterator[str]:
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", ws="none", timeout_graceful_shutdown=1)
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("server did not start")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 3)
        finally:
            sock.close()


@pytest.mark.parametrize("mode", ["disconnect", "upstream_timeout"])
async def test_tcp_stream_ends_and_releases_real_upstream(mode: str) -> None:
    closed = asyncio.Event()
    gate = Admission(1, 0, 1)
    async with serve(create_upstream(closed)) as upstream_url:
        async with httpx.AsyncClient(
            base_url=upstream_url, timeout=httpx.Timeout(1, read=0.1)
        ) as http:
            inference = InferenceClient(http, "test")
            backend = FastAPI()

            @backend.post("/questions")
            async def question() -> StreamResponse:
                async def prepare(stack: AsyncExitStack) -> PreparedStream:
                    events = await stack.enter_async_context(inference.open([]))
                    return PreparedStream([], events)

                return StreamResponse(prepare, gate, "tcp-test", execution_seconds=2)

            async with serve(backend) as backend_url:
                async with httpx.AsyncClient(base_url=backend_url, timeout=3) as caller:
                    lines = []
                    async with caller.stream("POST", "/questions") as response:
                        assert response.status_code == 200
                        async for line in response.aiter_lines():
                            lines.append(line)
                            if mode == "disconnect" and line == "event: delta":
                                break
                    await asyncio.wait_for(closed.wait(), 1)
                    await wait_for_slot(gate)
                    assert "event: sources" in lines
                    assert "event: delta" in lines
                    if mode == "upstream_timeout":
                        assert lines.count("event: error") == 1
                        assert "event: done" not in lines


async def test_tcp_nonreading_consumer_stops_producer_and_releases_slot() -> None:
    from contextlib import asynccontextmanager

    from app.inference.types import Completed, Delta

    gate = Admission(1, 0, 1)
    closed = asyncio.Event()
    produced = 0
    app = FastAPI()

    @asynccontextmanager
    async def resource() -> AsyncIterator[None]:
        try:
            yield
        finally:
            closed.set()

    async def events() -> AsyncIterator[Delta | Completed]:
        nonlocal produced
        while True:
            produced += 1
            yield Delta("x" * 60000)

    @app.post("/questions")
    async def question() -> StreamResponse:
        async def prepare(stack: AsyncExitStack) -> PreparedStream:
            await stack.enter_async_context(resource())
            return PreparedStream([], events())

        return StreamResponse(prepare, gate, "slow", send_seconds=0.05, execution_seconds=5)

    async with serve(app) as url:
        peer = socket.socket()
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        peer.setblocking(False)
        loop = asyncio.get_running_loop()
        try:
            await loop.sock_connect(peer, ("127.0.0.1", int(url.rsplit(":", 1)[1])))
            await loop.sock_sendall(
                peer, b"POST /questions HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n"
            )
            # Deliberately never read. Must stop well before the execution deadline.
            await asyncio.wait_for(closed.wait(), 2)
            assert 0 < produced < 256
            await wait_for_slot(gate)
        finally:
            peer.close()

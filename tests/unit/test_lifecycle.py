import asyncio
import signal

import httpx
import pytest
from starlette.responses import Response


async def test_draining_rejects_new_upload_without_reading_body_and_keeps_live() -> None:
    from app.core.lifecycle import LifecycleCoordinator, LifecycleMiddleware
    from app.core.request_context import RequestContextMiddleware

    entered = []

    async def endpoint(scope, receive, send):
        entered.append(scope["path"])
        await Response(status_code=200)(scope, receive, send)

    lifecycle = LifecycleCoordinator()
    app = RequestContextMiddleware(LifecycleMiddleware(endpoint, lifecycle))
    lifecycle.begin_draining()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        rejected = await client.post("/documents", content=b"unread upload")
        live = await client.get("/health/live")
    assert rejected.status_code == 503
    assert rejected.json()["error"]["code"] == "draining"
    assert rejected.json()["request_id"] == rejected.headers["x-request-id"]
    assert live.status_code == 200
    assert entered == ["/health/live"]


async def test_drain_waits_for_active_upload_and_rejects_new_work() -> None:
    from app.core.lifecycle import LifecycleCoordinator, LifecycleMiddleware

    lifecycle = LifecycleCoordinator(drain_seconds=1, cleanup_seconds=0.1)
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def upload(scope, receive, send):
        entered.set()
        try:
            await release.wait()
            await Response(status_code=202)(scope, receive, send)
        finally:
            closed.set()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=LifecycleMiddleware(upload, lifecycle)),
        base_url="http://test",
    ) as client:
        request = asyncio.create_task(client.post("/documents"))
        await entered.wait()
        lifecycle.begin_draining()
        drain = asyncio.create_task(lifecycle.shutdown())
        await asyncio.sleep(0)
        assert not drain.done()
        assert (await client.post("/questions")).status_code == 503
        release.set()
        assert (await request).status_code == 202
        await drain
        assert closed.is_set()
        assert lifecycle.active_count == 0


async def test_expired_drain_cancels_stream_and_waits_for_upstream_close() -> None:
    from contextlib import AsyncExitStack

    from app.core.lifecycle import LifecycleCoordinator, LifecycleMiddleware
    from app.inference.admission import Admission
    from app.inference.response import PreparedStream, StreamResponse

    lifecycle = LifecycleCoordinator(drain_seconds=0.02, cleanup_seconds=0.2)
    entered, closed = asyncio.Event(), asyncio.Event()
    admission = Admission(1, 0, 1)

    async def upstream_close():
        await asyncio.sleep(0.01)
        closed.set()

    async def events():
        entered.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover

    async def prepare(stack: AsyncExitStack):
        stack.push_async_callback(upstream_close)
        return PreparedStream([], events())

    response = StreamResponse(prepare, admission, "test")
    app = LifecycleMiddleware(response, lifecycle)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        request = asyncio.create_task(client.post("/questions"))
        await entered.wait()
        started = asyncio.get_running_loop().time()
        await lifecycle.shutdown()
        assert asyncio.get_running_loop().time() - started < 0.3
        assert closed.is_set()
        assert lifecycle.active_count == 0
        with pytest.raises(asyncio.CancelledError):
            await request
        async with admission.slot():
            pass


async def test_sigterm_enters_drain_immediately_before_server_shutdown() -> None:
    import uvicorn

    from app.core.lifecycle import LifecycleCoordinator
    from app.server import DrainingServer

    lifecycle = LifecycleCoordinator()
    server = DrainingServer(uvicorn.Config("unused:app"), lifecycle)
    server.handle_exit(signal.SIGTERM, None)
    assert lifecycle.draining
    assert server.should_exit


async def test_application_wires_drain_admission_and_readiness(tmp_path) -> None:
    from app.core.settings import Settings
    from app.main import create_app

    app = create_app(Settings(upload_root=tmp_path))
    app.state.lifecycle.begin_draining()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 503
        assert (await client.post("/questions")).json()["error"]["code"] == "draining"
        assert (await client.post("/documents")).json()["error"]["code"] == "draining"

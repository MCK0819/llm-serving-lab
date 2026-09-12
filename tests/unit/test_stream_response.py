import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import pytest

from app.core.errors import AppError
from app.inference.admission import Admission
from app.inference.types import Completed, Delta


@pytest.mark.parametrize("failure", [None, AppError(502, "upstream_failed", "안전한 오류")])
async def test_events_and_resources_are_finalized(failure: AppError | None) -> None:
    from app.inference.response import PreparedStream, StreamResponse

    closed = asyncio.Event()
    gate = Admission(1, 0, 1)
    messages = []

    @asynccontextmanager
    async def resource() -> AsyncIterator[None]:
        try:
            yield
        finally:
            closed.set()

    async def events() -> AsyncIterator[Delta | Completed]:
        yield Delta("안녕")
        if failure:
            raise failure
        yield Completed("stop", None)

    async def prepare(stack: AsyncExitStack) -> PreparedStream:
        await stack.enter_async_context(resource())
        return PreparedStream([], events())

    async def send(message: dict) -> None:
        messages.append(message)

    async def receive() -> dict:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    await StreamResponse(prepare, gate, "request-1")({"type": "http"}, receive, send)
    bodies = b"".join(m.get("body", b"") for m in messages).decode()
    assert bodies.count("event: sources") == 1
    assert bodies.count("event: delta") == 1
    terminal = "error" if failure else "done"
    assert bodies.count(f"event: {terminal}") == 1
    assert f"event: {'done' if failure else 'error'}" not in bodies
    assert closed.is_set()
    async with gate.slot():
        pass


async def test_preflight_failure_returns_http_error_before_sse() -> None:
    from app.inference.response import PreparedStream, StreamResponse

    messages = []

    async def prepare(stack: AsyncExitStack) -> PreparedStream:
        raise AppError(503, "inference_unavailable", "연결 불가")

    async def receive() -> dict:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)

    await StreamResponse(prepare, Admission(1, 0, 1), "id")({"type": "http"}, receive, send)
    assert messages[0]["status"] == 503
    assert json.loads(messages[1]["body"])["error"]["code"] == "inference_unavailable"


@pytest.mark.parametrize(
    "mode", ["disconnect", "send_stall", "execution_timeout", "header_failure", "cancel"]
)
async def test_interruption_closes_upstream_and_returns_slot(mode: str) -> None:
    from app.inference.response import PreparedStream, StreamResponse

    closed = asyncio.Event()
    entered = asyncio.Event()
    disconnected = asyncio.Event()
    gate = Admission(1, 0, 1)

    @asynccontextmanager
    async def resource() -> AsyncIterator[None]:
        try:
            entered.set()
            yield
        finally:
            closed.set()

    async def events() -> AsyncIterator[Delta | Completed]:
        yield Delta("text")
        await asyncio.Event().wait()

    async def prepare(stack: AsyncExitStack) -> PreparedStream:
        await stack.enter_async_context(resource())
        return PreparedStream([], events())

    async def send(message: dict) -> None:
        if mode == "header_failure":
            raise OSError("closed")
        if mode == "send_stall":
            await asyncio.Event().wait()
        if mode == "disconnect":
            disconnected.set()

    async def receive() -> dict:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    response = StreamResponse(prepare, gate, "id", execution_seconds=0.1, send_seconds=0.01)
    task = asyncio.create_task(response({"type": "http"}, receive, send))
    if mode == "cancel":
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await asyncio.wait_for(task, 1)
    assert closed.is_set()
    async with gate.slot():
        pass


async def test_disconnect_while_waiting_never_opens_upstream() -> None:
    from app.inference.response import PreparedStream, StreamResponse

    gate = Admission(1, 1, 10)
    disconnect = asyncio.Event()

    async def prepare(stack: AsyncExitStack) -> PreparedStream:
        pytest.fail("disconnected waiter opened upstream")

    async def receive() -> dict:
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        pytest.fail("disconnected waiter sent response")

    async with gate.slot():
        task = asyncio.create_task(
            StreamResponse(prepare, gate, "id")({"type": "http"}, receive, send)
        )
        await asyncio.sleep(0)
        disconnect.set()
        await asyncio.wait_for(task, 1)
    async with gate.slot():
        pass


async def test_cleanup_precedes_blocked_error_transmission() -> None:
    from app.inference.response import PreparedStream, StreamResponse

    closed = asyncio.Event()
    error_send_attempted = asyncio.Event()

    @asynccontextmanager
    async def resource() -> AsyncIterator[None]:
        try:
            yield
        finally:
            await asyncio.sleep(0)
            closed.set()

    async def events() -> AsyncIterator[Delta | Completed]:
        # Trigger the failure directly; this test checks ordering, not clock precision.
        raise TimeoutError
        yield Delta("never")

    async def prepare(stack: AsyncExitStack) -> PreparedStream:
        await stack.enter_async_context(resource())
        return PreparedStream([], events())

    async def receive() -> dict:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if b"event: error" in message.get("body", b""):
            assert closed.is_set()
            error_send_attempted.set()
            await asyncio.Event().wait()

    await StreamResponse(prepare, Admission(1, 0, 1), "id", execution_seconds=1, send_seconds=0.01)(
        {"type": "http"}, receive, send
    )
    assert closed.is_set()
    assert error_send_attempted.is_set()


async def test_cleanup_failure_does_not_send_second_http_start() -> None:
    from app.inference.response import PreparedStream, StreamResponse

    messages = []

    async def fail_close() -> None:
        raise AppError(500, "cleanup_failed", "cleanup")

    async def prepare(stack: AsyncExitStack) -> PreparedStream:
        stack.push_async_callback(fail_close)
        raise AppError(503, "unavailable", "unavailable")

    async def receive() -> dict:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)

    await StreamResponse(prepare, Admission(1, 0, 1), "id")({"type": "http"}, receive, send)
    assert sum(m["type"] == "http.response.start" for m in messages) == 1


async def test_unexpected_stream_error_has_safe_terminal() -> None:
    from app.inference.response import PreparedStream, StreamResponse

    messages = []

    async def events() -> AsyncIterator[Delta | Completed]:
        yield Delta("text")
        raise RuntimeError("secret-marker")

    async def prepare(stack: AsyncExitStack) -> PreparedStream:
        return PreparedStream([], events())

    async def receive() -> dict:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)

    await StreamResponse(prepare, Admission(1, 0, 1), "id")({"type": "http"}, receive, send)
    body = b"".join(m.get("body", b"") for m in messages)
    assert body.count(b"event: error") == 1
    assert b"secret-marker" not in body


async def test_disconnect_after_final_body_does_not_cancel_cleanup() -> None:
    from app.inference.response import PreparedStream, StreamResponse

    sent = asyncio.Event()
    closed = asyncio.Event()

    async def close() -> None:
        await asyncio.sleep(0.01)
        closed.set()

    async def events() -> AsyncIterator[Delta | Completed]:
        yield Completed("stop", None)

    async def prepare(stack: AsyncExitStack) -> PreparedStream:
        stack.push_async_callback(close)
        return PreparedStream([], events())

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            sent.set()

    async def receive() -> dict:
        await sent.wait()
        return {"type": "http.disconnect"}

    await StreamResponse(prepare, Admission(1, 0, 1), "id")({"type": "http"}, receive, send)
    assert closed.is_set()

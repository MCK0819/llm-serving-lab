import asyncio
import json
import logging
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass

from starlette.responses import JSONResponse, Response
from starlette.types import Message, Receive, Scope, Send

from app.core.errors import AppError
from app.inference.admission import Admission
from app.inference.types import Completed, Delta

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedStream:
    sources: list[dict[str, object]]
    events: AsyncIterator[Delta | Completed]


def encode_event(event: str, payload: dict[str, object]) -> bytes:
    if event not in {"sources", "delta", "done", "error"}:
        raise ValueError("Unsupported SSE event")
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    wire = f"event: {event}\ndata: {data}\n\n".encode()
    if len(wire) > 65536:
        raise AppError(502, "event_too_large", "응답 이벤트가 너무 큽니다.")
    return wire


class SendStopped(Exception):
    """The downstream connection can no longer be used."""


class StreamResponse(Response):
    """One request's admission, preparation, stream and cleanup lifetime.

    prepare registers upstream resources on the supplied exit stack. No DB or
    network work starts before admission. The request body must already be read.
    """

    def __init__(
        self,
        prepare: Callable[[AsyncExitStack], Awaitable[PreparedStream]],
        admission: Admission,
        request_id: str,
        execution_seconds: float = 120,
        send_seconds: float = 10,
    ) -> None:
        if not all(0 < value < math.inf for value in (execution_seconds, send_seconds)):
            raise ValueError("Timeouts must be finite and positive")
        super().__init__(media_type="text/event-stream")
        self.raw_headers = [
            (b"content-type", b"text/event-stream; charset=utf-8"),
            (b"cache-control", b"no-cache"),
            (b"x-accel-buffering", b"no"),
        ]
        self.prepare = prepare
        self.admission = admission
        self.request_id = request_id
        self.execution_seconds = execution_seconds
        self.send_seconds = send_seconds
        self._started = False
        self._terminal = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def disconnect() -> None:
            while (await receive())["type"] != "http.disconnect":
                pass

        worker = asyncio.create_task(self._run(scope, receive, send))
        watcher = asyncio.create_task(disconnect())
        try:
            done, _ = await asyncio.wait((worker, watcher), return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                await watcher
            else:
                await worker
        finally:
            for task in (worker, watcher):
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(worker, watcher, return_exceptions=True)

    async def _send(self, message: Message, send: Send) -> None:
        try:
            async with asyncio.timeout(self.send_seconds):
                await send(message)
        except TimeoutError, OSError:
            raise SendStopped from None

    async def _event(self, name: str, payload: dict[str, object], send: Send) -> None:
        body = encode_event(name, {**payload, "request_id": self.request_id})
        if name in {"done", "error"}:
            self._terminal = True
        await self._send({"type": "http.response.body", "body": body, "more_body": True}, send)

    async def _failure(self, exc: AppError, scope: Scope, receive: Receive, send: Send) -> None:
        if self._terminal:
            return
        if not self._started:
            self._terminal = True
            response = JSONResponse(
                {
                    "error": {"code": exc.code, "message": exc.message},
                    "request_id": self.request_id,
                },
                status_code=exc.status,
                headers={"X-Request-ID": self.request_id},
            )
            await response(scope, receive, lambda message: self._send(message, send))
        else:
            await self._event("error", {"code": exc.code, "message": exc.message}, send)
            await self._send({"type": "http.response.body", "body": b"", "more_body": False}, send)

    async def _run(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            try:
                async with self.admission.slot():
                    await self._admitted(scope, receive, send)
            except AppError as exc:
                await self._failure(exc, scope, receive, send)
        except SendStopped, TimeoutError:
            return

    async def _admitted(self, scope: Scope, receive: Receive, send: Send) -> None:
        deadline = asyncio.get_running_loop().time() + self.execution_seconds
        reserve = min(1.0, self.execution_seconds / 10)
        stack = AsyncExitStack()
        error: AppError | None = None
        try:
            try:
                async with asyncio.timeout_at(deadline - reserve):
                    prepared = await self.prepare(stack)
                    await self._stream(prepared, send)
            except TimeoutError:
                error = AppError(504, "execution_timeout", "처리 시간이 초과되었습니다.")
            except AppError as exc:
                error = exc
            except SendStopped:
                raise
            except Exception:
                error = AppError(500, "internal_error", "답변 처리 중 오류가 발생했습니다.")
        finally:
            # Spend the reserve on cleanup BEFORE attempting an error write.
            # Slot return is outside this block, even if cleanup itself fails.
            cleanup = asyncio.create_task(self._close(stack, deadline))
            try:
                cleanup_error = await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # A disconnect after the final body must not interrupt aclose.
                await cleanup
                raise
            if error is None:
                error = cleanup_error
        if error is not None:
            async with asyncio.timeout_at(deadline):
                await self._failure(error, scope, receive, send)

    async def _close(self, stack: AsyncExitStack, deadline: float) -> AppError | None:
        try:
            async with asyncio.timeout_at(deadline):
                await stack.aclose()
        except Exception:
            logger.warning("stream_cleanup_failed", extra={"request_id": self.request_id})
            return AppError(500, "cleanup_failed", "응답 연결 정리에 실패했습니다.")
        return None

    async def _stream(self, prepared: PreparedStream, send: Send) -> None:
        encode_event("sources", {"sources": prepared.sources, "request_id": self.request_id})
        await self._send(
            {"type": "http.response.start", "status": 200, "headers": self.raw_headers}, send
        )
        self._started = True
        await self._event("sources", {"sources": prepared.sources}, send)
        async for event in prepared.events:
            if isinstance(event, Delta):
                await self._event("delta", {"text": event.text}, send)
            else:
                await self._event(
                    "done",
                    {
                        "finish_reason": event.reason,
                        "usage": asdict(event.usage) if event.usage is not None else None,
                    },
                    send,
                )
                break
        if not self._terminal:
            raise AppError(502, "incomplete_stream", "답변이 중간에 종료되었습니다.")
        await self._send({"type": "http.response.body", "body": b"", "more_body": False}, send)

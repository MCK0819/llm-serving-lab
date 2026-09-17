"""Per-application ownership of HTTP request lifetimes during shutdown."""

import asyncio
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class LifecycleCoordinator:
    def __init__(self, drain_seconds: float = 30, cleanup_seconds: float = 3) -> None:
        if not all(0 < value < math.inf for value in (drain_seconds, cleanup_seconds)):
            raise ValueError("Shutdown budgets must be finite and positive")
        self.drain_seconds = drain_seconds
        self.cleanup_seconds = cleanup_seconds
        self._deadline: float | None = None
        self._tasks: set[asyncio.Task[object]] = set()

    @property
    def draining(self) -> bool:
        return self._deadline is not None

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def begin_draining(self) -> None:
        # Synchronous: the signal handler closes admission before yielding.
        if self._deadline is None:
            self._deadline = time.monotonic() + self.drain_seconds

    @contextmanager
    def own_request(self) -> Iterator[None]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("HTTP work must run in an asyncio task")
        self._tasks.add(task)
        try:
            yield
        finally:
            self._tasks.discard(task)

    async def shutdown(self) -> None:
        self.begin_draining()
        assert self._deadline is not None
        pending = set(self._tasks)
        if not pending:
            return
        _, pending = await asyncio.wait(pending, timeout=max(0, self._deadline - time.monotonic()))
        for task in pending:
            task.cancel()
        if pending:
            # StreamResponse joins its worker and closes its upstream exit stack.
            # Bound cancellation cleanup too; external stop grace is the last limit.
            _, pending = await asyncio.wait(pending, timeout=self.cleanup_seconds)
            for task in pending:
                task.cancel()


class LifecycleMiddleware:
    def __init__(self, app: ASGIApp, lifecycle: LifecycleCoordinator) -> None:
        self.app = app
        self.lifecycle = lifecycle

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in {"/health/live", "/health/ready"}:
            await self.app(scope, receive, send)
            return
        if self.lifecycle.draining:
            response = JSONResponse(
                {
                    "error": {"code": "draining", "message": "서비스를 종료하고 있습니다."},
                    "request_id": scope.get("state", {}).get("request_id"),
                },
                status_code=503,
                headers={"Connection": "close"},
            )
            await response(scope, receive, send)
            return
        # No await between admission check and task registration. Upload body reads,
        # preparation, SSE sends, and their resource cleanup all belong to this task.
        with self.lifecycle.own_request():
            await self.app(scope, receive, send)

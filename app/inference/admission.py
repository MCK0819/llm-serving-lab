import asyncio
import math
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.core.errors import AppError


class Admission:
    """Single-event-loop FIFO admission; never share between processes or threads."""

    def __init__(self, running_limit: int, waiting_limit: int, wait_seconds: float) -> None:
        if running_limit < 1 or waiting_limit < 0 or not (0 < wait_seconds < math.inf):
            raise ValueError("Invalid admission limits")
        self.running_limit = running_limit
        self.waiting_limit = waiting_limit
        self.wait_seconds = wait_seconds
        self._active = 0
        self._waiters: deque[asyncio.Future[None]] = deque()

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        # No await between inspecting and mutating counters: atomic on one loop.
        # Release is synchronous so cancellation cannot interrupt slot return.
        if self._active < self.running_limit:
            self._active += 1
        else:
            if len(self._waiters) >= self.waiting_limit:
                raise AppError(503, "overloaded", "처리할 자리가 없습니다.")
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            try:
                async with asyncio.timeout(self.wait_seconds):
                    await asyncio.shield(waiter)
            except BaseException as exc:
                if waiter.done():
                    self._release()
                else:
                    self._waiters.remove(waiter)
                    waiter.cancel()
                if isinstance(exc, TimeoutError):
                    raise AppError(
                        503, "admission_timeout", "대기 시간이 초과되었습니다."
                    ) from None
                raise
        try:
            yield
        finally:
            self._release()

    def _release(self) -> None:
        if self._waiters:
            self._waiters.popleft().set_result(None)
        else:
            self._active -= 1

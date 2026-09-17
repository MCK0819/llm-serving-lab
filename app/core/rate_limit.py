import math
import time
from collections import deque
from collections.abc import Callable
from typing import Literal
from uuid import UUID

from app.core.errors import AppError

type RateCategory = Literal["question", "upload", "read"]

_WINDOW_SECONDS = 60.0
_LIMITS: dict[RateCategory, int] = {"question": 20, "upload": 3, "read": 60}


class RateLimiter:
    """In-process sliding-window limits confined to one event loop.

    ``check`` contains no await, so inspecting and updating a user's deque is
    atomic while the application remains on one event loop. Instances must not
    be shared across threads or processes.
    """

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, *, max_keys: int = 10_000
    ) -> None:
        if max_keys < 1:
            raise ValueError("max_keys must be positive")
        self._clock = clock
        self._max_keys = max_keys
        self._events: dict[tuple[UUID, RateCategory], deque[float]] = {}

    def check(self, user_id: UUID, category: RateCategory) -> None:
        now = self._clock()
        if not math.isfinite(now):
            raise RuntimeError("Rate-limit clock must be finite")
        cutoff = now - _WINDOW_SECONDS
        key = (user_id, category)
        events = self._events.get(key)
        if events is not None:
            self._prune(events, cutoff)
            if not events:
                del self._events[key]
                events = None

        if events is None:
            if len(self._events) >= self._max_keys:
                self._expire_inactive(cutoff)
            if len(self._events) >= self._max_keys:
                retry_after = min(queue[0] for queue in self._events.values()) + _WINDOW_SECONDS
                self._reject(retry_after - now)
            events = deque(maxlen=_LIMITS[category])
            self._events[key] = events

        if len(events) >= _LIMITS[category]:
            self._reject(events[0] + _WINDOW_SECONDS - now)
        events.append(now)

    def _expire_inactive(self, cutoff: float) -> None:
        for key, events in list(self._events.items()):
            self._prune(events, cutoff)
            if not events:
                del self._events[key]

    @staticmethod
    def _prune(events: deque[float], cutoff: float) -> None:
        while events and events[0] <= cutoff:
            events.popleft()

    @staticmethod
    def _reject(remaining: float) -> None:
        raise AppError(
            429,
            "rate_limited",
            "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
            headers={"Retry-After": str(max(1, math.ceil(remaining)))},
        )

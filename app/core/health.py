import asyncio
import math
import os
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryFile

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class HealthService:
    """Coalesced readiness checks; model and broker availability are independent."""

    def __init__(
        self,
        engine: AsyncEngine | None,
        storage_root: Path,
        is_draining: Callable[[], bool],
        *,
        timeout: float = 2,
    ) -> None:
        if not 0 < timeout < math.inf:
            raise ValueError("Health timeout must be finite and positive")
        self.engine = engine
        self.storage_root = storage_root
        self.is_draining = is_draining
        self.timeout = timeout
        self._probe: asyncio.Task[bool] | None = None
        self._closed = False

    async def ready(self) -> bool:
        if self._closed or self.engine is None or self.is_draining():
            return False
        if self._probe is None or self._probe.done():
            self._probe = asyncio.create_task(self._check())
        try:
            async with asyncio.timeout(self.timeout):
                result = await asyncio.shield(self._probe)
            return result and not self.is_draining() and not self._closed
        except TimeoutError:
            return False

    async def _check(self) -> bool:
        assert self.engine is not None
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            await asyncio.to_thread(self._check_storage)
        except Exception:
            return False
        return True

    def _check_storage(self) -> None:
        self.storage_root.mkdir(parents=True, exist_ok=True)
        with TemporaryFile(dir=self.storage_root, prefix=".readiness-") as probe:
            probe.write(b"ready")
            probe.flush()
            os.fsync(probe.fileno())

    async def aclose(self) -> None:
        self._closed = True
        if self._probe is not None:
            self._probe.cancel()
            await asyncio.gather(self._probe, return_exceptions=True)
